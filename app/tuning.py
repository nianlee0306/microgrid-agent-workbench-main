"""Revisioned controller gains for one isolated synthetic sandbox per browser session."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from uuid import uuid4

from .model import ModelFailure
from .pi_proposal import propose_gains
from .pi_sandbox import sandbox_scenarios, simulate_pi_sandbox, tuning_gate
from .settings import PROVIDERS

MODEL_FAILURES = {
    "invalid_gain_proposal": "The model did not return a distinct gain pair in the allowed trial range.",
    "local_api_unavailable": "The local Ollama service is unavailable.",
    "local_model_not_installed": "The selected Ollama model is unavailable locally.",
    "local_model_mismatch": "Ollama returned a different or unverifiable model identity.",
    "timeout": "The local model exceeded the proposal time limit.",
}


class TuningError(ValueError):
    def __init__(self, detail, status=422):
        super().__init__(detail)
        self.status = status


class Tuning:
    def __init__(self, settings, ollama, state_directory=None):
        self.settings = settings
        self.ollama = ollama
        self.state_directory = Path(state_directory or settings.root / "var" / "tuning")
        self.scenarios = {scenario["id"]: scenario for scenario in sandbox_scenarios()}
        self.lock = Lock()

    def _path(self, session_id, scenario):
        if scenario not in self.scenarios:
            raise TuningError("Select a supported synthetic PI scenario.")
        key = sha256(f"{session_id}:{scenario}".encode()).hexdigest()
        return self.state_directory / f"{key}.json"

    def _read(self, path):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"revision": 0, "active": None, "previous": None, "proposal": None, "events": []}

    def _write(self, path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as target:
                scratch = Path(target.name)
                json.dump(state, target, ensure_ascii=False, allow_nan=False)
            os.replace(scratch, path)
        finally:
            if scratch is not None:
                scratch.unlink(missing_ok=True)

    @staticmethod
    def _check_revision(state, revision):
        if state["revision"] != revision:
            raise TuningError("The active controller revision changed. Refresh this sandbox before retrying.", 409)

    def _simulate(self, scenario, gains):
        return simulate_pi_sandbox(
            scenario, gains["kp"], gains["ki"], backend=self.settings.tuning_backend,
            matlab_directory=self.settings.root / "matlab",
        )

    def _response(self, scenario, state, active=None):
        active = active or state["active"] or self._simulate(scenario, self.scenarios[scenario]["baseline_gains"])
        return {
            "scenario": scenario, "scenarios": list(self.scenarios.values()),
            "gain_bounds": self.scenarios[scenario]["gain_bounds"],
            "controller_function": "simulate_pi_sandbox.m",
            "revision": state["revision"], "active": active, "can_rollback": state["previous"] is not None,
            "proposal": state["proposal"], "events": state["events"],
        }

    @staticmethod
    def _record(state, action, proposal, result=None, initiator="agent"):
        event = {
            "action": action, "revision": state["revision"], "initiator": initiator,
            "model": proposal["model_used"] if action != "rolled_back" else None,
            "proposal_id": proposal["id"], "proposal_status": proposal["status"],
            "provider": proposal["provider"], "model_digest": proposal["model_digest"],
            "gains": result["gains"] if result else None, "backend": result["backend"] if result else None,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        state["events"] = [*state["events"][-15:], event]

    @staticmethod
    def _check_candidate(state, proposal, proposal_id):
        if not proposal or proposal["id"] != proposal_id:
            raise TuningError("This candidate has been superseded. Run a new tuning trial.", 409)
        if proposal["status"] != "candidate_ready" or not proposal["assessment"]["eligible"]:
            raise TuningError("Select a validated candidate from the current revision.", 409)
        if proposal["base_revision"] != state["revision"]:
            raise TuningError("This candidate was simulated against an earlier controller revision.", 409)

    def _activation_trial(self, scenario, proposal):
        result = self._simulate(scenario, proposal["candidate"]["gains"])
        assessment = tuning_gate(proposal["baseline"], result)
        return result, assessment

    def _activate(self, state, proposal, active, initiator):
        state["previous"] = state["active"] or proposal["baseline"]
        state["active"] = active
        state["revision"] += 1
        proposal["status"] = "applied"
        proposal["trace"].append({"event": "controller_activate_gains", "revision": state["revision"],
                                  "backend": active["backend"], "gains": active["gains"],
                                  "metrics": active["metrics"]})
        self._record(state, "applied", proposal, active, initiator)

    def get(self, session_id, scenario):
        path = self._path(session_id, scenario)
        with self.lock:
            state = self._read(path)
        return self._response(scenario, state)

    def propose(self, session_id, scenario, revision, mode, model, apply_if_valid):
        path = self._path(session_id, scenario)
        if mode == "local" and self.settings.local_models:
            model = model or self.settings.local_models[0]
        if mode == "local" and model not in self.settings.local_models:
            raise TuningError("Select a configured local Ollama model for the PI sandbox.")
        if mode == "demo" and model is not None:
            raise TuningError("Deterministic Demo uses no model tag.")
        with self.lock:
            state = self._read(path)
            self._check_revision(state, revision)
        active_gains = state["active"]["gains"] if state["active"] else self.scenarios[scenario]["baseline_gains"]
        baseline = self._simulate(scenario, active_gains)
        trace = [{
            "event": "controller_read_active", "revision": revision, "backend": baseline["backend"],
            "gains": baseline["gains"],
        }]
        proposal = {
            "id": uuid4().hex, "base_revision": revision, "mode": mode, "model": model, "model_used": None,
            "provider": PROVIDERS[mode], "model_digest": None, "status": "unavailable",
            "baseline": baseline, "candidate": None, "rationale": "", "assessment": None, "trace": trace,
        }
        try:
            if mode == "local":
                inventory = self.ollama.inventory()
                if inventory is None:
                    raise ModelFailure("local_api_unavailable")
                if model not in inventory:
                    raise ModelFailure("local_model_not_installed")
                proposal["model_digest"] = inventory[model].get("digest")
            gains = propose_gains(
                self.ollama, mode=mode, model=model, scenario=scenario, gains=baseline["gains"],
                metrics=baseline["metrics"], bounds=self.scenarios[scenario]["gain_bounds"], trace=trace,
            )
            proposal["rationale"] = gains["rationale"]
            candidate = self._simulate(scenario, gains)
            trace.append({"event": "controller_simulate_candidate", "backend": candidate["backend"],
                          "gains": candidate["gains"], "metrics": candidate["metrics"]})
            assessment = tuning_gate(baseline, candidate)
            proposal.update(candidate=candidate, assessment=assessment,
                            status="candidate_ready" if assessment["eligible"] else "rejected")
            trace.append({"event": "controller_validate_candidate", **assessment})
        except ModelFailure as exc:
            proposal["model_status"] = str(exc)
            reason = MODEL_FAILURES.get(str(exc), "The local model did not return a verified gain proposal.")
            proposal["assessment"] = {"eligible": False, "reasons": [reason]}
        proposal["model_used"] = next(
            (item["model"] for item in reversed(trace) if item.get("event") == "model_response" and item.get("model")),
            None,
        )
        activation = None
        if apply_if_valid and proposal["status"] == "candidate_ready":
            activation, confirmation = self._activation_trial(scenario, proposal)
            if not confirmation["eligible"]:
                proposal.update(status="rejected", assessment=confirmation)
                trace.append({"event": "controller_recheck_failed", **confirmation})
                activation = None
        with self.lock:
            state = self._read(path)
            self._check_revision(state, revision)
            state["proposal"] = proposal
            self._record(state, "proposed", proposal, proposal["candidate"])
            if activation is not None:
                self._activate(state, proposal, activation, "agent")
            self._write(path, state)
        return self._response(scenario, state, state["active"] or baseline)

    def apply(self, session_id, scenario, revision, proposal_id):
        path = self._path(session_id, scenario)
        with self.lock:
            state = self._read(path)
            self._check_revision(state, revision)
            proposal = state["proposal"]
            self._check_candidate(state, proposal, proposal_id)
        active, confirmation = self._activation_trial(scenario, proposal)
        if not confirmation["eligible"]:
            raise TuningError("The activation rerun did not pass: " + " ".join(confirmation["reasons"]), 409)
        with self.lock:
            state = self._read(path)
            self._check_revision(state, revision)
            proposal = state["proposal"]
            self._check_candidate(state, proposal, proposal_id)
            self._activate(state, proposal, active, "operator")
            self._write(path, state)
        return self._response(scenario, state)

    def rollback(self, session_id, scenario, revision):
        path = self._path(session_id, scenario)
        with self.lock:
            state = self._read(path)
            self._check_revision(state, revision)
            if state["previous"] is None:
                raise TuningError("There is no previous controller revision to restore.", 409)
            gains = state["previous"]["gains"]
        restored = self._simulate(scenario, gains)
        with self.lock:
            state = self._read(path)
            self._check_revision(state, revision)
            state["active"], state["previous"] = restored, None
            state["revision"] += 1
            proposal = state["proposal"]
            if proposal["status"] == "applied":
                proposal["status"] = "rolled_back"
                proposal["trace"].append({"event": "controller_restore_gains", "revision": state["revision"],
                                          "backend": restored["backend"], "gains": restored["gains"]})
            else:
                state["proposal"] = None
            self._record(state, "rolled_back", proposal, restored, "operator")
            self._write(path, state)
        return self._response(scenario, state)
