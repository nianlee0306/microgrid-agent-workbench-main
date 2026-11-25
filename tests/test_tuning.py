import pytest
from fastapi.testclient import TestClient

from app import tuning
from app.main import create_app
from app.settings import Settings

BASE = {"session_id": "test-tuning-session", "scenario": "voltage"}


@pytest.fixture
def sandbox(tmp_path):
    settings = Settings(tuning_backend="python", audit_file=tmp_path / "reviews.jsonl")
    state = tmp_path / "tuning"
    client = TestClient(create_app(settings, tuning_state_dir=state))
    return client, settings, state


def get(client, scope=BASE):
    return client.get("/api/controller-tuning", params=scope).json()


def post(client, action, revision, scope=BASE, **fields):
    return client.post(f"/api/controller-tuning/{action}", json={**scope, "revision": revision, **fields})


def test_tuning_applies_validated_gains_and_reruns_the_controller(sandbox, monkeypatch):
    client, settings, state_directory = sandbox
    calls = []
    simulate = tuning.simulate_pi_sandbox

    def record_simulation(scenario, kp, ki, **kwargs):
        calls.append((scenario, kp, ki))
        return simulate(scenario, kp, ki, **kwargs)

    monkeypatch.setattr(tuning, "simulate_pi_sandbox", record_simulation)
    initial = get(client)
    assert initial["revision"] == 0 and initial["active"]["gains"] == {"kp": .6, "ki": .5}
    trial = post(client, "propose", 0).json()
    candidate = trial["proposal"]
    assert trial["revision"] == 0 and trial["active"]["gains"] == initial["active"]["gains"]
    assert candidate["status"] == "candidate_ready" and candidate["assessment"]["eligible"]
    assert candidate["candidate"]["metrics"]["iae"] < candidate["baseline"]["metrics"]["iae"]

    call_count = len(calls)
    update = post(client, "apply", 0, proposal_id=candidate["id"]).json()
    assert calls[call_count:] == [("voltage", 1.2, 1.2)]
    assert update["revision"] == 1 and update["can_rollback"]
    assert update["active"]["gains"] == candidate["candidate"]["gains"]
    assert update["proposal"]["trace"][-1]["event"] == "controller_activate_gains"
    assert [event["action"] for event in update["events"]] == ["proposed", "applied"]
    assert post(client, "apply", 0, proposal_id=candidate["id"]).status_code == 409

    restarted = TestClient(create_app(settings, tuning_state_dir=state_directory))
    assert get(restarted)["active"]["gains"] == update["active"]["gains"]
    rollback = post(restarted, "rollback", 1).json()
    assert calls[-1] == ("voltage", .6, .5)
    assert rollback["revision"] == 2 and not rollback["can_rollback"]
    assert rollback["active"]["gains"] == initial["active"]["gains"]
    assert rollback["events"][-1]["action"] == "rolled_back"
    assert post(restarted, "rollback", 2).status_code == 409


def test_auto_apply_is_limited_to_the_requested_sandbox(sandbox):
    client, _, _ = sandbox
    scope = {**BASE, "scenario": "frequency"}
    auto = post(client, "propose", 0, scope, apply_if_valid=True).json()
    assert auto["revision"] == 1 and auto["proposal"]["status"] == "applied"
    assert auto["active"]["gains"] == auto["proposal"]["candidate"]["gains"]
    assert get(client)["revision"] == 0
    assert get(client, {**scope, "session_id": "another-session"})["revision"] == 0


def test_bad_simulation_blocks_auto_apply_and_client_gain_overrides(sandbox, monkeypatch):
    client, _, _ = sandbox
    monkeypatch.setattr(tuning, "propose_gains", lambda *args, **kwargs: {"kp": 0, "ki": 0, "rationale": "Test poor fit."})
    result = post(client, "propose", 0, apply_if_valid=True).json()
    candidate = result["proposal"]
    assert result["revision"] == 0 and candidate["status"] == "rejected"
    assert not candidate["assessment"]["eligible"]
    assert post(client, "apply", 0, proposal_id=candidate["id"]).status_code == 409
    assert post(client, "propose", 0, kp=1, ki=1).status_code == 422
    assert post(client, "propose", 0, mode="live").status_code == 422
    assert post(client, "propose", 0, {**BASE, "scenario": "hardware"}).status_code == 422


def test_proposed_gain_update_is_bound_to_the_latest_candidate(sandbox):
    client, _, _ = sandbox
    earlier = post(client, "propose", 0).json()["proposal"]["id"]
    current = post(client, "propose", 0).json()["proposal"]["id"]
    assert earlier != current
    assert post(client, "apply", 0, proposal_id=earlier).status_code == 409
    assert post(client, "apply", 0, proposal_id=current).status_code == 200
