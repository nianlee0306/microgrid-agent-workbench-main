"""Explicit live-service checks against an imported, recorded window."""

import argparse
import json
import sys

from .audit import Audit
from .data import DataError, RunStore
from .offline import MATLAB_BACKEND, review_window
from .reviews import ReviewError, Reviews, supports_signals
from .settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("ollama", "responses", "matlab"))
    parser.add_argument("--model", help="Exact configured model tag for a provider smoke test.")
    parser.add_argument("--agent", choices=("voltage", "frequency", "security"), default="voltage")
    parser.add_argument("--run-id", help="Imported run ID, or use the first compatible recording.")
    args = parser.parse_args()
    settings = Settings.load()
    try:
        store = RunStore(settings.root, catalog_path=settings.catalog_path)
        candidates = [store.get_run(args.run_id)] if args.run_id else store.list_runs()
        run = next((item for item in candidates if supports_signals(args.agent, item["signals"])), None)
        if run is None:
            raise ReviewError("Import a recording with signals supported by the selected Agent.")
        run_id, start, end = run["run_id"], run["axis_min"], run["axis_max"]
        if args.service == "matlab":
            window = store.get_window(run_id, start, end)
            numeric = {ident: values for ident, values in window["values"].items() if ident != "controller_state"}
            outcome = review_window(numeric, backend="matlab_offline", matlab_directory=settings.root / "matlab")
            success = outcome.get("backend") == MATLAB_BACKEND and outcome.get("status") == "completed"
        else:
            runner = Reviews(settings, store, Audit(settings.root / (settings.audit_file or "var/audit.jsonl")))
            mode = "local" if args.service == "ollama" else "live"
            result = runner.review(args.agent, run_id, start, end, mode=mode, model=args.model)
            outcome = {key: result[key] for key in
                       ("review_id", "agent_id", "mode", "provider", "model", "model_used", "model_digest",
                        "model_status", "model_trace", "raw_recommendation", "recommendation", "verification")}
            outcome["tool_calls"] = [{key: call[key] for key in ("tool", "status", "backend", "evidence_id")}
                                     for call in result["tool_calls"]]
            success = result["model_status"] == "completed" and bool(result["tool_calls"])
            success = success and any(item["event"] == "tool_output_returned" for item in result["model_trace"])
    except (DataError, ReviewError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    outcome["recording"] = {"run_id": run_id, "start": start, "end": end}
    print(json.dumps(outcome, indent=2, ensure_ascii=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
