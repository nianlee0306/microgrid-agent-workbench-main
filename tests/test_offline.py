import unittest
from pathlib import Path

from app.offline import PYTHON_BACKEND, evaluate_offline_pi, review_window


class OfflineTest(unittest.TestCase):
    def test_window_metrics_retain_missingness_and_reference(self):
        result = review_window(
            {"frequency_actual_hz": [49.5, None, 50.0], "frequency_feedback_hz": [51.0, None, 50.0]},
            {"frequency_actual_hz": 50.0},
        )
        self.assertEqual(result["backend"], PYTHON_BACKEND)
        actual = result["metrics"]["frequency_actual_hz"]
        self.assertEqual((actual["sample_count"], actual["valid_count"], actual["missing_count"]), (3, 2, 1))
        self.assertEqual((actual["min"], actual["max"], actual["range"]), (49.5, 50.0, 0.5))
        self.assertEqual(actual["mean_deviation"], -0.25)
        self.assertEqual(actual["max_abs_deviation"], 0.5)
        feedback = result["metrics"]["frequency_feedback_hz"]
        self.assertEqual(feedback["mean"], 50.5)
        self.assertIsNone(feedback["reference"])
        self.assertIsNone(feedback["max_abs_deviation"])

    def test_unregistered_and_infinite_diagnostic_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            review_window({"unregistered": [50.0]})
        with self.assertRaises(ValueError):
            review_window({"frequency_actual_hz": [float("inf")]})

    def test_missing_matlab_review_adapter_reports_python_execution(self):
        result = review_window({"v_positive_rms": [230.0]}, backend="matlab_offline",
                               matlab_directory=Path("missing-matlab-adapter"))
        self.assertEqual(result["backend"], PYTHON_BACKEND)
        self.assertIn("adapter", result["fallback_reason"])

    def test_preset_pi_is_repeatable_and_uses_frequency_feedback(self):
        kwargs = {
            "role": "frequency", "preset_id": "frequency-example-v1",
            "elapsed_seconds": [0.0, 0.05, 0.10, 0.15],
            "signals": {"frequency_feedback_hz": [50.0, 49.0, 49.0, 49.0],
                        "frequency_actual_hz": [70.0, 71.0, 72.0, 73.0]},
        }
        result = evaluate_offline_pi(**kwargs)
        self.assertEqual(result, evaluate_offline_pi(**kwargs))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["signal_id"], "frequency_feedback_hz")
        self.assertEqual(result["update_count"], 3)
        self.assertEqual(result["model_output"]["unit"], "ratio")
        self.assertEqual(result["model_output"]["min"], 0.5)
        self.assertAlmostEqual(result["model_output"]["last"], 0.53)

    def test_voltage_pi_uses_normalized_error_and_respects_update_period(self):
        result = evaluate_offline_pi(
            role="voltage", preset_id="voltage-example-v1", elapsed_seconds=[0.0, 0.02, 0.07, 0.12],
            signals={"v_positive_rms": [230.0, 207.0, 207.0, 207.0]},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["update_count"], 2)
        self.assertAlmostEqual(result["model_output"]["last"], 0.51175)

    def test_pi_reports_unavailable_inputs_and_adapter(self):
        kwargs = {"role": "frequency", "preset_id": "frequency-example-v1",
                  "elapsed_seconds": [0.0, 0.05], "signals": {"frequency_feedback_hz": [50.0, 50.0]}}
        for missing, reason in (
            ({"role": "security"}, "preset"),
            ({"elapsed_seconds": None}, "clock"),
            ({"elapsed_seconds": [0.0, 0.0]}, "strictly increasing"),
            ({"elapsed_seconds": [0.0, 0.001]}, "control period"),
            ({"signals": {"frequency_actual_hz": [50.0, 50.0]}}, "frequency_feedback_hz"),
            ({"backend": "matlab_offline", "matlab_directory": Path("missing-matlab-adapter")}, "adapter"),
        ):
            with self.subTest(reason=reason):
                result = evaluate_offline_pi(**dict(kwargs, **missing))
                self.assertEqual(result["status"], "unavailable")
                self.assertIsNone(result["backend"])
                self.assertIn(reason, result["reason"])


if __name__ == "__main__":
    unittest.main()
