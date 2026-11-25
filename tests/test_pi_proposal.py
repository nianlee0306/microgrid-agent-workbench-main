import json

import httpx
import pytest

from app.model import ModelFailure
from app.ollama import Ollama
from app.pi_proposal import propose_gains

REQUEST = {
    "scenario": "voltage",
    "gains": {"kp": 0.6, "ki": 0.5},
    "metrics": {"iae": 2.1, "overshoot_pct": 0.0, "raw_samples": [100.0]},
    "bounds": {"kp": [0, 4], "ki": [0, 4]},
}


def test_ollama_proposal_uses_only_synthetic_summary_and_validates_model():
    def request_handler(request):
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3:4b"
        for schema in payload["format"]["anyOf"]:
            assert schema["required"] == ["kp", "ki", "rationale"]
            assert schema["additionalProperties"] is False
        assert '"iae":2.1' in payload["messages"][1]["content"]
        assert "raw_samples" not in payload["messages"][1]["content"]
        return httpx.Response(200, json={
            "model": "qwen3:4b", "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": '{"kp":0.9,"ki":0.8,"rationale":"Trial gains"}'},
        })

    trace = []
    adapter = Ollama(5, httpx.MockTransport(request_handler))
    proposal = propose_gains(adapter, mode="local", model="qwen3:4b", trace=trace, **REQUEST)
    assert proposal == {"kp": 0.9, "ki": 0.8, "rationale": "Trial gains"}
    assert trace[-1]["model"] == "qwen3:4b"
    assert trace[-1]["protocol"] == "structured_pi_proposal"


@pytest.mark.parametrize("result", [
    {"kp": 4.5, "ki": 0.8, "rationale": "Out of range"},
    {"kp": True, "ki": 0.8, "rationale": "Boolean"},
    {"kp": 0.9, "ki": 0.8, "rationale": "Unexpected field", "filename": "controller.m"},
    {"kp": 0.6, "ki": 0.5, "rationale": "Unchanged gains"},
])
def test_malformed_proposals_are_rejected(result):
    class Provider:
        def propose_pi_gains(self, *args):
            return result

    with pytest.raises(ModelFailure, match="invalid_gain_proposal"):
        propose_gains(Provider(), mode="local", model="qwen3:4b", trace=[], **REQUEST)


def test_demo_proposal_has_deterministic_parameters():
    proposal = propose_gains(None, mode="demo", model=None, trace=[], **REQUEST)
    assert proposal.keys() == {"kp", "ki", "rationale"}
    assert (proposal["kp"], proposal["ki"]) == (1.2, 1.2)
