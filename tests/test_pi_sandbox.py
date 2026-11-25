from math import inf, nan

import pytest

from app import pi_sandbox


@pytest.mark.parametrize("scenario", ["voltage", "frequency"])
def test_fixed_response_and_improving_candidate(scenario):
    baseline = pi_sandbox.simulate_pi_sandbox(scenario, 0.6, 0.5)
    candidate = pi_sandbox.simulate_pi_sandbox(scenario, 1.2, 1.2)

    trace = baseline["trace"]
    assert baseline["backend"] == "Python Simulation"
    assert all(len(series) == 501 for series in trace.values())
    assert trace["time_s"][20] == 1.0
    assert trace["time_s"][-1] == 25.0
    assert trace["reference"][:21] == [0.0] * 20 + [1.0]
    assert trace["output"][:21] == [0.0] * 21
    assert trace["command"][20] == 0.6
    assert max(trace["command"]) <= 2.0
    assert candidate["metrics"]["iae"] < baseline["metrics"]["iae"]
    assert pi_sandbox.tuning_gate(baseline, candidate) == {"eligible": True, "reasons": []}


def test_gate_rejects_no_improvement_and_nonsettling_candidate():
    baseline = pi_sandbox.simulate_pi_sandbox("voltage", 0.6, 0.5)
    candidate = pi_sandbox.simulate_pi_sandbox("voltage", 0, 0)

    assert not pi_sandbox.tuning_gate(baseline, baseline)["eligible"]
    gate = pi_sandbox.tuning_gate(baseline, candidate)
    assert candidate["metrics"]["settling_time_s"] is None
    assert not gate["eligible"]
    assert any("settle" in reason for reason in gate["reasons"])


@pytest.mark.parametrize("invalid", [nan, inf, -0.01, 4.01, True, "0.5"])
def test_gain_validation_rejects_non_numeric_or_unbounded_values(invalid):
    with pytest.raises(ValueError, match="kp"):
        pi_sandbox.simulate_pi_sandbox("voltage", invalid, 0.5)


def test_failed_matlab_request_provides_an_explicit_python_fallback(monkeypatch):
    def unavailable(*_args):
        raise ImportError

    monkeypatch.setattr(pi_sandbox, "_call_fixed_matlab", unavailable)
    result = pi_sandbox.simulate_pi_sandbox("voltage", 0.6, 0.5, backend="matlab_offline")
    assert result["backend"] == "Python Simulation"
    assert "MATLAB Engine" in result["fallback_reason"]
