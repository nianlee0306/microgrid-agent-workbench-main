"""FastAPI entry point for the local replay and Agent workbench."""

from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent_defs import AGENTS
from .audit import Audit
from .data import DataError, RunStore
from .reviews import ReviewError, Reviews
from .settings import LOCAL_LABELS, PROVIDERS, ROOT, Settings
from .tuning import Tuning, TuningError

SessionId = Annotated[str, StringConstraints(min_length=8, max_length=96, pattern=r"^[a-zA-Z0-9_-]+$")]
TuningScenario = Literal["voltage", "frequency"]


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["demo", "local", "live"] = "demo"
    model: str | None = Field(default=None, max_length=100)


class ReviewRequest(ModelSelection):
    run_id: str = Field(min_length=1, max_length=128)
    start: float = Field(allow_inf_nan=False, strict=True)
    end: float = Field(allow_inf_nan=False, strict=True)
    task: str | None = Field(default=None, max_length=80)
    question: str | None = Field(default=None, max_length=1500)
    session_id: SessionId | None = None


class TuningAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: SessionId
    scenario: TuningScenario
    revision: int = Field(ge=0, strict=True)


class TuningProposal(TuningAction):
    mode: Literal["demo", "local"] = "demo"
    model: str | None = Field(default=None, max_length=100)
    apply_if_valid: bool = Field(default=False, strict=True)


class TuningApply(TuningAction):
    proposal_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def create_app(settings=None, responses=None, ollama=None, tuning_state_dir=None, *, run_store=None):
    settings = settings or Settings.load()
    store = run_store if run_store is not None else RunStore(settings.root, catalog_path=settings.catalog_path)
    audit = Audit(settings.root / (settings.audit_file or "var/audit.jsonl"))
    reviews = Reviews(settings, store, audit, responses, ollama)
    tuning = Tuning(settings, reviews.ollama, tuning_state_dir)
    service = FastAPI(title="Microgrid Supervisory Agent Workbench", version="1.2.0")
    service.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    service.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "app" / "templates")

    @service.exception_handler(DataError)
    def data_error(request, exc):
        status = {"run_not_found": 404, "unavailable_window": 416, "evaluation_unavailable": 409}.get(exc.code, 422)
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=status)

    @service.exception_handler(ReviewError)
    def review_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": "review_unavailable"}, status_code=422)

    @service.exception_handler(TuningError)
    def tuning_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": "tuning_unavailable"}, status_code=exc.status)

    @service.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @service.get("/api/agents")
    def agents():
        inventory = reviews.ollama.inventory() if settings.local_models else None
        models_by_mode = settings.models_by_mode
        modes = ["demo", *(mode for mode, model_ids in models_by_mode.items() if model_ids)]
        options = {}
        for mode, model_ids in models_by_mode.items():
            options[mode] = []
            for ident in model_ids:
                name, family = LOCAL_LABELS.get(ident, (ident, "Open weights")) if mode == "local" else (ident, "OpenAI")
                model_data = inventory.get(ident, {}) if inventory is not None and mode == "local" else {}
                options[mode].append({
                    "id": ident, "label": name, "family": family, "provider": PROVIDERS[mode],
                    "installed": ident in inventory if mode == "local" and inventory is not None else None,
                    "digest": model_data.get("digest"),
                    "quantization": model_data.get("details", {}).get("quantization_level"),
                })
        default_models = {mode: model_ids[0] if model_ids else None for mode, model_ids in models_by_mode.items()}
        return {
            "agents": [agent.public(modes) for agent in AGENTS.values()],
            "modes": modes,
            "models": settings.models,
            "default_model": settings.models[0] if settings.models else None,
            "default_mode": "local" if settings.local_models else "demo",
            "default_models": default_models,
            "model_options": options,
            "local_runtime_available": inventory is not None if settings.local_models else None,
            "allow_recorded_model": settings.allow_recorded_model,
        }

    @service.get("/api/runs")
    def runs():
        return {"runs": store.list_runs()}

    @service.get("/api/runs/{run_id}/window")
    def window(run_id: str, start: float | None = None, end: float | None = None,
               signal_ids: Annotated[list[str] | None, Query()] = None):
        return store.get_window(run_id, start, end, signal_ids)

    @service.post("/api/agents/{agent_id}/review")
    def review(agent_id: str, body: ReviewRequest):
        if agent_id not in AGENTS:
            raise HTTPException(404, "Agent was not found.")
        return reviews.review(agent_id=agent_id, **body.model_dump())

    @service.get("/api/sessions/{session_id}/reviews")
    def history(session_id: SessionId, agent_id: str | None = None):
        if agent_id is not None and agent_id not in AGENTS:
            raise HTTPException(404, "Agent was not found.")
        return {"reviews": audit.read(session_id, agent_id)}

    @service.get("/api/evaluation/cases")
    def evaluation_cases():
        return {"cases": store.evaluation_cases()}

    @service.post("/api/evaluation")
    def evaluation(body: ModelSelection):
        return reviews.evaluate(**body.model_dump())

    @service.get("/api/controller-tuning")
    def get_tuning(session_id: SessionId, scenario: TuningScenario = "voltage"):
        return tuning.get(session_id, scenario)

    @service.post("/api/controller-tuning/propose")
    def propose_tuning(body: TuningProposal):
        return tuning.propose(**body.model_dump())

    @service.post("/api/controller-tuning/apply")
    def apply_tuning(body: TuningApply):
        return tuning.apply(**body.model_dump())

    @service.post("/api/controller-tuning/rollback")
    def rollback_tuning(body: TuningAction):
        return tuning.rollback(**body.model_dump())

    return service


app = create_app()
