"""Server configuration for the local workbench."""

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

PROVIDERS = {"demo": "Deterministic rules", "local": "Ollama", "live": "OpenAI"}
LOCAL_LABELS = {
    "qwen3:4b": ("Qwen3 4B", "Qwen"),
    "llama3.2:3b": ("Llama 3.2 3B", "Llama"),
    "granite3.3:2b": ("Granite 3.3 2B", "IBM Granite"),
    "mistral:7b-instruct-v0.3-q4_K_M": ("Mistral 7B Instruct v0.3", "Mistral"),
}


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    api_key: str = ""
    models: tuple[str, ...] = ()
    allow_recorded_model: bool = False
    backend: str = "python"
    model_timeout: float = 20.0
    threshold_file: Path | None = None
    audit_file: Path | None = None
    local_models: tuple[str, ...] = ()
    local_timeout: float = 120.0
    tuning_backend: str = "matlab_offline"
    catalog_path: Path | None = None

    @property
    def models_by_mode(self):
        return {"local": self.local_models, "live": self.models}

    @classmethod
    def load(cls, root: Path = ROOT):
        load_dotenv(root / ".env")
        key = os.getenv("OPENAI_API_KEY", "").strip()
        model = os.getenv("OPENAI_MODEL", "").strip()
        extras = (item.strip() for item in os.getenv("OPENAI_MODELS", "").split(","))
        local_model = os.getenv("WORKBENCH_OLLAMA_MODEL", "").strip()
        local_extras = (item.strip() for item in os.getenv("WORKBENCH_OLLAMA_MODELS", "").split(","))
        return cls(
            root=root,
            api_key=key,
            models=tuple(dict.fromkeys(item for item in (model, *extras) if item))
            if key and model else (),
            allow_recorded_model=os.getenv("ALLOW_RECORDED_MODEL", "").lower() == "true",
            backend=os.getenv("CONTROLLER_BACKEND", "python").strip().lower(),
            model_timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "20")),
            threshold_file=Path(value) if (value := os.getenv("RESEARCH_THRESHOLDS")) else None,
            audit_file=Path(value) if (value := os.getenv("WORKBENCH_AUDIT_FILE")) else None,
            local_models=tuple(dict.fromkeys(item for item in (local_model, *local_extras) if item)),
            local_timeout=float(os.getenv("WORKBENCH_OLLAMA_TIMEOUT_SECONDS", "120")),
            tuning_backend=os.getenv("WORKBENCH_TUNING_BACKEND", "matlab_offline").strip().lower(),
            catalog_path=Path(value) if (value := os.getenv("WORKBENCH_CATALOG_PATH", "").strip()) else None,
        )

    def thresholds(self, synthetic: bool) -> dict:
        if synthetic:
            return {
                "version": "synthetic-v1",
                "voltage_reference_v": 230.0,
                "voltage_max_abs_deviation_v": 11.5,
                "phase_max_spread_v": 5.0,
                "frequency_reference_hz": 50.0,
                "frequency_max_abs_deviation_hz": 0.5,
                "max_evidence_age_s": 3.0,
            }
        if self.threshold_file is None:
            return {"version": None}
        config_path = self.root / self.threshold_file
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not isinstance(config.get("version"), str) or not config["version"].strip():
            raise ValueError("The research threshold file needs a nonempty version.")
        allowed = self.thresholds(True).keys() - {"version"}
        for field in allowed & config.keys():
            value = config[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid research threshold: {field}")
        return {key: value for key, value in config.items() if key in allowed | {"version"}}
