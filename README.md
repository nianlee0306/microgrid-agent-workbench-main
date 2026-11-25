# Microgrid Supervisory Agent Workbench

Review and replay authorized microgrid recordings, compare Agent recommendations, and test PI gains in a separate simulation.

## Install

From the repository root, run the following commands with Python 3.12.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On macOS and Linux, use `./.venv/bin/python` in place of the Windows executable.

## Import the Google Drive recordings

Ask the data owner for Drive access and the original `manifest.json`. Download the referenced CSVs, extract any ZIP, and keep the exact names and relative paths. Put the manifest beside the CSVs to preserve the original run IDs and evidence references.

```powershell
$recordings = 'C:\path\to\the\recording-bundle'
.\.venv\Scripts\python.exe -m app.cli import --source-dir "$recordings" --manifest (Join-Path $recordings 'manifest.json')
```

Check that `imported_runs` equals `requested_runs` and `input_error_count` is zero. The importer writes `data/imported/catalog.json` and `coverage-report.json`. You can select an existing catalog elsewhere with `WORKBENCH_CATALOG_PATH` in `.env`. Git ignores `data/raw/`, `data/imported/`, and `.env`.

## Start

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Select a recorded run and an Agent role. **Deterministic Rules** runs without an LLM or API key. Restart the server after importing or changing the catalog. [Open the Workbench](http://127.0.0.1:8000)

## Optional backends

Install and start Ollama to enable local model inference. Pull a model and create `.env` if needed.

```powershell
ollama pull qwen3:4b
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
```

Set `WORKBENCH_OLLAMA_MODEL=qwen3:4b` in `.env`, restart the server, and select **Local · Ollama**. For MATLAB calculations, install the matching MATLAB Engine for Python and set `CONTROLLER_BACKEND=matlab_offline`.

## Evaluation and tests

For recorded evaluation, provide a labeled `evaluation_cases.json` beside the catalog with each case's `case_id`, `condition`, `agent_id`, `task`, `run_id`, `start`, `end`, `signal_ids`, and `ground_truth`.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

The automated tests use isolated fixtures.
