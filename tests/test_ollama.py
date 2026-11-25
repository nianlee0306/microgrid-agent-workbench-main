import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ollama import Ollama, is_local_model_name
from app.settings import Settings
from tests.replay_fixtures import ReplayFixtureStore

MODEL = "qwen3:4b"
DIGEST = "4" * 64
WINDOW = {"run_id": "demo-voltage-a", "start": 2, "end": 10, "mode": "local", "model": MODEL}


@pytest.mark.parametrize("wrong_tool", [False, True])
def test_ollama_structured_tool_handoff_and_model_metadata(tmp_path, wrong_tool):
    audit_path = tmp_path / "audit.jsonl"
    requests = []

    def handle(request):
        assert request.url.host == "127.0.0.1"
        assert request.headers.get("Authorization") is None
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": MODEL, "digest": DIGEST, "details": {"quantization_level": "Q4_K_M"}},
                {"name": "granite3.3:2b-cloud", "digest": DIGEST},
                {"name": "remote-alias:latest", "remote_model": "provider-model"},
            ]})
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == MODEL and not payload["stream"] and not payload["think"]
        assert "tools" not in payload and payload["format"]["additionalProperties"] is False
        properties = payload["format"]["properties"]
        if "tool" in properties:
            expected = properties["tool"]["enum"]
            assert len(expected) == 1
            name = "controller__set_voltage" if wrong_tool else expected[0]
            raw = {"tool": name}
        else:
            content = payload["messages"][-1]["content"]
            outputs, _ = json.JSONDecoder().raw_decode(content.split("WORKBENCH CONTROLLER EVIDENCE (server-generated JSON):\n")[1])
            assert len(outputs) == 2
            assert {item["tool"] for item in outputs} == {"controller.get_window", "controller.compute_diagnostics"}
            assert all("run_id" not in item["output"] and "values" not in item["output"] for item in outputs)
            assert audit_path.read_text(encoding="utf-8").count('"event": "controller_tool"') == len(outputs)
            raw = {"recommendation": "CONTINUE_MONITORING", "rationale": "Recorded phase RMS is within the research limits.",
                   "evidence_ids": [outputs[-1]["output"]["evidence_id"]]}
        message = {"role": "assistant", "content": json.dumps(raw)}
        return httpx.Response(200, json={"model": MODEL, "message": message, "done": True, "done_reason": "stop"})

    provider = Ollama(5, httpx.MockTransport(handle))
    cfg = Settings(local_models=(MODEL, "llama3.2:3b"), audit_file=audit_path)
    with TestClient(create_app(cfg, ollama=provider, run_store=ReplayFixtureStore())) as client:
        config = client.get("/api/agents").json()
        assert config["default_mode"] == "local" and config["modes"] == ["demo", "local"]
        assert [(row["id"], row["installed"]) for row in config["model_options"]["local"]] == [
            (MODEL, True), ("llama3.2:3b", False),
        ]
        assert client.post("/api/agents/voltage/review", json={**WINDOW, "model": "unlisted:tag"}).status_code == 422
        missing = client.post("/api/agents/voltage/review", json={**WINDOW, "model": "llama3.2:3b"}).json()
        assert missing["model_status"] == "local_model_not_installed" and missing["model_used"] is None
        review = client.post("/api/agents/voltage/review", json=WINDOW).json()
    assert review["provider"] == "Ollama" and review["model_digest"] == DIGEST
    assert review["model_used"] == MODEL
    if wrong_tool:
        assert review["model_status"] == "unexpected_tool_call"
        assert review["status"] == "unavailable" and review["tool_calls"] == []
        assert len(requests) == 1
    else:
        assert review["model_status"] == "completed" and review["verification"]["status"] == "passed"
        assert len(requests) == 3 and len(review["tool_calls"]) == 2
        assert sum(item["event"] == "tool_output_returned" for item in review["model_trace"]) == 2
        assert [item["protocol"] for item in review["model_trace"] if item["event"] == "model_request"] == [
            "structured_tool", "structured_tool", "structured_recommendation",
        ]
    assert set(provider.inventory()) == {MODEL}


def test_unreachable_ollama_does_not_use_any_other_provider(tmp_path):
    def handle(request):
        raise httpx.ConnectError("daemon offline", request=request)

    provider = Ollama(5, httpx.MockTransport(handle))
    cfg = Settings(local_models=(MODEL,), audit_file=tmp_path / "audit.jsonl")
    with TestClient(create_app(cfg, ollama=provider, run_store=ReplayFixtureStore())) as client:
        config = client.get("/api/agents").json()
        assert config["local_runtime_available"] is False
        result = client.post("/api/agents/voltage/review", json=WINDOW).json()
        assert result["model_status"] == "local_api_unavailable"
        assert result["model_used"] is None and result["tool_calls"] == []


@pytest.mark.parametrize("model", ["ollama.com/qwen3:4b", "https://example.com/llm", "qwen3:cloud", "qwen3:4b-cloud"])
def test_cloud_model_names_are_rejected(model):
    assert not is_local_model_name(model)
