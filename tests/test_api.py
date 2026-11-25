import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data import import_recordings, write_json
from app.main import create_app
from app.model import Responses
from app.settings import Settings
from tests.replay_fixtures import ReplayFixtureStore

WINDOW = {"run_id": "demo-voltage-a", "start": 2, "end": 10, "session_id": "test-session-123"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(audit_file=tmp_path / "audit.jsonl"), run_store=ReplayFixtureStore()))


def test_agents_and_window_replay(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/agents").json()["modes"] == ["demo"]
    profiles = [
        ("voltage", "demo-voltage-a", ["controller.get_window", "controller.compute_diagnostics"]),
        ("frequency", "demo-frequency-a", ["controller.get_window", "controller.compute_diagnostics"]),
        ("security", "demo-voltage-a", ["controller.get_evidence_quality"]),
    ]
    for agent, run_id, tools in profiles:
        body = {**WINDOW, "run_id": run_id}
        review = client.post(f"/api/agents/{agent}/review", json=body).json()
        assert review["mode"] == "demo" and review["model_status"] == "demo"
        assert [event["tool"] for event in review["tool_calls"]] == tools
        assert all(event["backend"] == "Python Replay" for event in review["tool_calls"])
        replay = client.get(f"/api/runs/{run_id}/window?start=2&end=10").json()
        assert review["recommendation"]["evidence_ids"] == [replay["evidence_id"]]
        if agent == "security":
            quality = review["tool_calls"][0]["result"]
            assert len(quality["prior_diagnostics"]) == 1
    history = client.get("/api/sessions/test-session-123/reviews?agent_id=frequency").json()["reviews"]
    assert [review["agent_id"] for review in history] == ["frequency"]
    assert client.post("/api/agents/voltage/review", json={**WINDOW, "run_id": "demo-mechanical-a"}).json()["status"] == "unavailable"
    assert client.post("/api/runs/demo-voltage-a/window", json={}).status_code == 405
    assert client.post("/api/agents/security/review", json={**WINDOW, "task": "offline_pi"}).status_code == 422


def test_windows_are_validated(client):
    assert client.get("/api/runs/unknown/window").status_code == 404
    assert client.get("/api/runs/demo-voltage-a/window?start=2000&end=3000").status_code == 416
    assert client.post("/api/agents/voltage/review", json={**WINDOW, "start": 12, "end": 7}).status_code == 422
    assert client.post("/api/agents/voltage/review", json={**WINDOW, "start": "2"}).status_code == 422
    assert client.post("/api/agents/voltage/review", json={**WINDOW, "evidence_id": "ev_user"}).status_code == 422


def test_fixed_cases_use_the_same_output_for_both_paired_arms(client):
    result = client.post("/api/evaluation", json={"mode": "demo"}).json()
    assert {case["condition"] for case in result["cases"]} == {
        "clean", "missing", "stale", "unit_mismatch", "source_mismatch"
    }
    assert all(case["raw_output_id"] == case["validated_raw_output_id"] == case["review_id"]
               for case in result["cases"])
    expected_reasons = {"REQUIRED_FIELDS_MISSING", "REPLAY_EVIDENCE_STALE", "INCONSISTENT_UNITS", "INCONSISTENT_SOURCE"}
    assert expected_reasons.issubset({reason for row in result["cases"] for reason in row["verification"]["reasons"]})
    assert result["metrics"]["validated"]["task_accuracy"]["count"] == len(result["cases"])
    assert result["metrics"]["validated"]["unsupported_pass_through"]["count"] == 0


def test_production_starts_without_demo_runs_or_evaluation(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path))) as client:
        assert client.get("/api/runs").json() == {"runs": []}
        assert client.get("/api/evaluation/cases").json() == {"cases": []}
        blocked = client.post("/api/evaluation", json={})
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "evaluation_unavailable"
        assert client.get("/api/runs/demo-voltage-a/window").status_code == 404


def test_labeled_recorded_case_uses_the_selected_catalog(tmp_path, monkeypatch):
    source = tmp_path / "raw"
    source.mkdir()
    (source / "voltage.csv").write_text("time_s,Va_RMS,Vb_RMS,Vc_RMS\n0,230,231,232\n1,231,231,231\n", encoding="utf-8")
    manifest = {"schema_version": 1, "runs": [
        {"run_id": "recorded-voltage", "file": "voltage.csv", "source": "Test lab export", "category": "test"},
    ]}
    catalog, report = import_recordings(source, manifest)
    assert report["valid_rows"] == 2
    catalog_path = tmp_path / "custom" / "catalog.json"
    write_json(catalog_path, catalog)
    monkeypatch.setenv("WORKBENCH_CATALOG_PATH", "custom/catalog.json")
    data_file = catalog_path.with_name("evaluation_cases.json")
    case = {
        "case_id": "recorded-01", "condition": "thresholds unavailable", "agent_id": "voltage",
        "task": "diagnose_voltage", "run_id": "recorded-voltage", "start": 0, "end": 1,
        "signal_ids": ["va_rms", "vb_rms", "vc_rms"], "ground_truth": "INSUFFICIENT_EVIDENCE",
    }
    write_json(data_file, [case])
    with TestClient(create_app(Settings.load(root=tmp_path))) as client:
        runs = client.get("/api/runs").json()["runs"]
        assert [run["run_id"] for run in runs] == ["recorded-voltage"]
        assert not runs[0]["synthetic"]
        assert client.get("/api/evaluation/cases").json() == {"cases": [case]}
        result = client.post("/api/evaluation", json={})
        assert result.status_code == 200
        row = result.json()["cases"][0]
        assert row["run_id"] == "recorded-voltage"
        assert row["recommendation"]["recommendation"] == case["ground_truth"]
        assert row["data_source"] == "Recorded MATLAB Export"

        write_json(data_file, [{**case, "quality_overrides": {"replay_clock": 99}}])
        invalid = client.post("/api/evaluation", json={})
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "invalid_evaluation"


@pytest.mark.parametrize("bad_citation", [False, True])
def test_responses_function_call_handoff(tmp_path, bad_citation):
    audit_path = tmp_path / "audit.jsonl"
    payloads = []
    evidence_ids = []

    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        response = {"id": f"resp_{len(payloads)}", "status": "completed", "output": []}
        assert not payload["store"] and payload["model"] == "test-model"
        if len(payloads) > 1:
            outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
            last = json.loads(outputs[-1]["output"])
            assert last["status"] == "completed" and last["sample_count"] > 1
            assert "run_id" not in last and "values" not in last
            assert audit_path.read_text(encoding="utf-8").count('"event": "controller_tool"') == len(outputs)
            evidence_ids.append(last["evidence_id"])
        if payload["tools"]:
            tool = payload["tools"][0]
            schema = tool["parameters"]
            assert tool["strict"] and not schema["additionalProperties"]
            params = {
                key: field["items"]["enum"] if field["type"] == "array" else field["enum"][0]
                for key, field in schema["properties"].items()
            }
            response["output"] = [{"type": "function_call", "id": f"fc_{len(payloads)}",
                                   "name": tool["name"], "arguments": json.dumps(params),
                                   "call_id": f"call_{len(payloads)}"}]
        else:
            assert payload["text"]["format"]["strict"]
            result = {"recommendation": "CONTINUE_MONITORING", "rationale": "The reported values are within the configured limits.",
                      "evidence_ids": ["ev_unrelated"] if bad_citation else [evidence_ids[-1]]}
            response["output"] = [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]
        return httpx.Response(200, json=response)

    provider = Responses("test-key", 5, httpx.MockTransport(handle))
    app = create_app(Settings(models=("test-model",), audit_file=audit_path), provider, run_store=ReplayFixtureStore())
    with TestClient(app) as test_client:
        result = test_client.post("/api/agents/voltage/review", json={**WINDOW, "mode": "live"}).json()
    assert len(payloads) == 3
    assert result["model_status"] == "completed"
    assert [item["event"] for item in result["model_trace"]].count("tool_output_returned") == 2
    if bad_citation:
        assert "INVALID_EVIDENCE_REFERENCE" in result["verification"]["reasons"]
        assert result["recommendation"]["recommendation"] == "INSUFFICIENT_EVIDENCE"
    else:
        assert result["verification"]["status"] == "passed"


def test_provider_timeout_has_no_fabricated_tool_calls(tmp_path):
    def handle(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    provider = Responses("test-key", 5, httpx.MockTransport(handle))
    app = create_app(
        Settings(models=("test-model",), audit_file=tmp_path / "audit.jsonl"),
        provider,
        run_store=ReplayFixtureStore(),
    )
    with TestClient(app) as test_client:
        result = test_client.post("/api/agents/voltage/review", json={**WINDOW, "mode": "live"}).json()
    assert result["status"] == "unavailable" and result["model_status"] == "timeout"
    assert result["tool_calls"] == [] and result["raw_recommendation"] is None
