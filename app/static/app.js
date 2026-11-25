"use strict";

const $ = (id) => document.getElementById(id);
const sessionKey = "microgrid-workbench.v1";
const saved = readSession();
const state = {
  page: "workspace", agents: [], runs: [], recordingsLoaded: false, agentId: saved.agentId ?? "voltage",
  mode: null, model: null, modelOptions: {}, defaultModels: {}, localRuntimeAvailable: null,
  modePreference: saved.runtimeMode ?? (saved.mode === "live" ? "live" : null),
  modelPreferences: saved.modelPreferences ?? (saved.model ? { live: saved.model } : {}),
  run: null, window: null, dirty: false, windowRequest: 0, loading: false,
  sessionId: saved.sessionId || crypto.randomUUID(),
  history: saved.history ?? {}, activeReview: {}, pending: new Set(), reviewErrors: {},
  evaluation: null, cases: null, evaluationPending: false,
  tuning: { scenario: null, snapshot: null, request: 0, pending: null, error: "", message: "", signal: "output", plot: null },
  unit: null, hidden: new Set(), hover: null, plot: null, paintRequest: 0,
  replay: { playing: false, position: 0, previousTime: null, frame: 0 },
};

const pageCopy = {
  workspace: ["Agent Workspace", "Inspect imported controller telemetry and review a recorded experiment window."],
  tuning: ["Controller Tuning", "Tune PI gains with the selected Agent and compare the simulated closed-loop responses."],
  replay: ["Run Replay", "Play back the recorded signals in the selected experiment window."],
  evaluation: ["Evaluation", "Measure recommendation and evidence quality across fixed research cases."],
};
const modeLabels = { demo: "Local rules", local: "Local · Ollama", live: "OpenAI Responses API" };
const providerLabels = { demo: "Deterministic rules", local: "Ollama", live: "OpenAI" };
const recommendationLabels = {
  CONTINUE_MONITORING: "Continue monitoring",
  REQUEST_REVIEW: "Request review",
  INSUFFICIENT_EVIDENCE: "Insufficient evidence",
};
const palette = ["#087d6b", "#456fa4", "#af6e29", "#8b62a0", "#ad5058", "#358597", "#688245", "#796752"];
const normalizedStatuses = new Set(["ok", "passed", "pass", "success", "completed", "accepted", "valid", "continue_monitoring"]);

function readSession() {
  try { return JSON.parse(sessionStorage.getItem(sessionKey)) ?? {}; } catch { return {}; }
}

function saveSession() {
  try {
    sessionStorage.setItem(sessionKey, JSON.stringify({
      sessionId: state.sessionId, agentId: state.agentId,
      runtimeMode: state.modePreference, modelPreferences: state.modelPreferences,
      history: state.history, runId: state.run?.run_id, start: state.window?.start, end: state.window?.end,
    }));
  } catch { /* The open tab retains its state when storage is unavailable. */ }
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function icon(name) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  const use = document.createElementNS(namespace, "use");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function option(text, value) { return new Option(text, value); }
function label(value) {
  if (value == null || value === "") return "Unavailable";
  if (Object.hasOwn(recommendationLabels, value)) return recommendationLabels[value];
  const text = String(value).replaceAll("_", " ");
  if (text === text.toUpperCase() && /[A-Z]/.test(text)) return text[0] + text.slice(1).toLowerCase();
  return text[0].toUpperCase() + text.slice(1);
}

function number(value, significant = 7) {
  return Number.isFinite(value) ? Number(value.toPrecision(significant)).toLocaleString("en", { maximumFractionDigits: 10 }) : "—";
}

function duration(start, end, axis) {
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
  return axis === "sample_index" ? `Samples ${number(start)}–${number(end)}` : `${number(start)}–${number(end)} s`;
}

function setMessage(id, message) {
  const node = $(id);
  node.textContent = message || "";
  node.hidden = !message;
}

function errorDescription(error, fallback) { return error?.message || fallback; }

async function api(path, body) {
  const request = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const response = await fetch(path, request);
  let data;
  try { data = await response.json(); } catch {
    throw new Error(`Workbench API returned HTTP ${response.status}.`);
  }
  if (!response.ok) {
    const detail = data.detail ?? data.error;
    const reason = Array.isArray(detail) ? detail.map((issue) => issue.msg).join(" ") : detail?.message ?? detail;
    const error = new Error(typeof reason === "string" ? reason : `Workbench API returned HTTP ${response.status}.`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function runLabel(run) { return run?.label || run?.run_id || "Experiment"; }
function agent() { return state.agents.find(({ id }) => id === state.agentId); }
function sourceLabel(run) {
  return run.data_source || "Recorded experiment";
}

function statusBadge(status) {
  const key = String(status ?? "").toLowerCase().replaceAll(" ", "_");
  let style = "";
  if (normalizedStatuses.has(key)) style = " positive";
  else if (/failed|error|timeout|rejected/.test(key)) style = " negative";
  else if (/review|unavailable|insufficient|blocked|invalid/.test(key)) style = " warning";
  return element("span", `badge badge-status${style}`, label(status));
}

function canRunModel() {
  return state.mode === "demo" || Boolean(state.model);
}

function localAvailability(choice) {
  if (choice.installed === true) return "Installed";
  if (choice.installed === false) return "Install required";
  if (state.localRuntimeAvailable === false) return "Runtime unavailable";
  return "Availability unconfirmed";
}

function modelChoice(choice) {
  const button = element("button", "model-choice");
  button.type = "button";
  button.dataset.model = choice.id;
  button.title = choice.id;
  button.setAttribute("aria-pressed", String(choice.id === state.model));
  const availability = state.mode === "local" ? localAvailability(choice) : null;
  const meta = element("span", "model-choice-meta", [choice.family, availability].filter(Boolean).join(" · "));
  if (availability) meta.title = availability;
  button.append(element("span", "model-choice-name", choice.label || choice.id));
  if (meta.textContent) button.append(meta);
  button.addEventListener("click", () => {
    state.modelPreferences[state.mode] = choice.id;
    renderModelControls();
    $("model-options").querySelector('[aria-pressed="true"]')?.focus();
  });
  return button;
}

function renderModelControls() {
  const options = state.modelOptions[state.mode] ?? [];
  const preferred = state.modelPreferences[state.mode] ?? state.defaultModels[state.mode];
  const selected = options.find(({ id }) => id === preferred) ?? options[0];
  const provider = selected?.provider ?? providerLabels[state.mode];
  state.model = selected?.id ?? null;
  $("mode-select").value = state.mode;
  $("mode-label").textContent = selected ? `${provider} · ${selected.id}` : modeLabels[state.mode];
  $("mode-badge").title = $("mode-label").textContent;
  const runtimeUnavailable = state.mode === "local" && state.localRuntimeAvailable === false;
  $("mode-badge").classList.toggle("model-warning", state.mode !== "demo" && (!selected || selected.installed === false || runtimeUnavailable));
  if (selected) {
    const availability = state.mode === "local" ? ` · ${localAvailability(selected)}` : "";
    $("selected-model").replaceChildren(element("code", "", selected.id), document.createTextNode(` · ${provider}${availability}`));
  } else $("selected-model").textContent = state.mode === "demo" ? "Deterministic rules" : "Model configuration required";
  const entries = options.map(modelChoice);
  if (state.mode === "demo") entries.push(element("p", "", "Rule-based review of the selected recording."));
  $("model-options").replaceChildren(...entries);
  updateAvailability();
  updateEvaluationAvailability();
  renderTuning();
  saveSession();
}

function setPage(page, updateURL = false) {
  if (!Object.hasOwn(pageCopy, page)) return;
  if (state.page !== page) stopReplay();
  state.page = page;
  const workspace = page === "workspace";
  const replay = page === "replay";
  const tuning = page === "tuning";
  $("heading").textContent = pageCopy[page][0];
  $("breadcrumb-page").textContent = pageCopy[page][0];
  $("subheading").textContent = pageCopy[page][1];
  $("role-settings").hidden = !workspace && !tuning;
  $("model-settings").hidden = replay;
  $("tuning-entry").hidden = !workspace;
  $("scope").hidden = !workspace && !replay;
  $("workspace").hidden = !workspace && !replay;
  $("workspace").classList.toggle("replay-view", replay);
  $("agent-panel").hidden = !workspace;
  $("controller-actions").hidden = !workspace;
  $("replay-controls").hidden = !replay;
  $("replay-evidence").hidden = !replay;
  $("evaluation").hidden = page !== "evaluation";
  $("tuning").hidden = !tuning;
  for (const item of document.querySelectorAll("[data-page]")) {
    const active = item.dataset.page === page;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  if (updateURL) {
    if (location.hash !== `#${page}`) history.pushState(null, "", `#${page}`);
    window.scrollTo({ top: 0, behavior: "instant" });
  }
  renderWindow();
  renderReview();
  if (page === "evaluation" && state.cases === null) loadCases();
  if (tuning) loadTuning();
}

function controllerName() {
  if (state.run?.controller && !["none", "unknown"].includes(state.run.controller)) return state.run.controller;
  if (state.run && state.run.axis === "sample_index") return "Mechanical telemetry";
  return state.page === "workspace" ? agent()?.controller || "Controller telemetry" : "Controller telemetry";
}

function isCompatible() {
  if (!state.run || !agent()) return false;
  const available = state.run.signals ?? {};
  if (state.agentId === "voltage") return ["va_rms", "vb_rms", "vc_rms", "v_positive_rms"].some((id) => id in available);
  if (state.agentId === "frequency") return ["frequency_actual_hz", "frequency_feedback_hz", "speed_mechanical_rpm", "speed_estimated_rpm"].some((id) => id in available);
  return (agent().signal_ids ?? []).some((id) => id in available);
}

function updateAvailability() {
  const current = Boolean(state.window) && !state.loading && !state.dirty;
  const reviewing = state.pending.has(state.agentId);
  const allowed = current && isCompatible() && !reviewing && canRunModel();
  $("analyze").disabled = !allowed;
  $("question-submit").disabled = !allowed;
  for (const task of $("quick-tasks").querySelectorAll("button")) task.disabled = !allowed;
  for (const id of ["replay-play", "replay-reset", "replay-seek"]) $(id).disabled = !current;
  const mismatch = state.run && agent() && state.page === "workspace" && !isCompatible();
  const requirements = { voltage: "three-phase or positive-sequence RMS voltage", frequency: "frequency or speed measurements", security: "supported archived measurements" }[state.agentId];
  setMessage("scope-notice", mismatch ? `${agent()?.name || "This agent"} can analyze runs with ${requirements}.` : "");
}

function renderAgents() {
  const choices = state.agents.map((candidate) => {
    const button = element("button", "agent-choice");
    button.type = "button";
    button.setAttribute("aria-pressed", String(candidate.id === state.agentId));
    button.setAttribute("aria-label", candidate.name);
    button.setAttribute("aria-describedby", `role-duty-${candidate.id}`);
    const avatar = element("span", "agent-avatar");
    avatar.dataset.agent = candidate.id;
    avatar.append(icon(candidate.id));
    const indicator = element("span", "agent-indicator");
    indicator.setAttribute("aria-hidden", "true");
    const text = element("span", "agent-choice-copy");
    const duty = element("span", "agent-choice-description", candidate.description);
    duty.id = `role-duty-${candidate.id}`;
    text.append(element("span", "agent-choice-label", candidate.name), duty);
    button.append(avatar, text, indicator);
    button.addEventListener("click", () => {
      if (state.agentId === candidate.id) return;
      state.agentId = candidate.id;
      state.unit = null;
      state.hidden.clear();
      $("question").value = "";
      renderAgents();
      $("agent-picker").querySelector('[aria-pressed="true"]')?.focus();
      renderWindow();
      renderReview();
      if (state.page === "tuning") loadTuning();
      saveSession();
    });
    return button;
  });
  $("agent-picker").replaceChildren(...choices);
  const selected = agent();
  if (!selected) return;
  $("agent-heading").textContent = selected.name;
  $("agent-description").textContent = selected.description;
  $("agent-avatar").dataset.agent = selected.id;
  $("agent-avatar").replaceChildren(icon(selected.id));
  const taskButtons = (selected.tasks || []).map((task) => {
    const button = element("button", "task-button");
    button.type = "button";
    button.append(element("span", "", task.label));
    button.addEventListener("click", () => requestReview(task.id));
    return button;
  });
  $("quick-tasks").replaceChildren(...taskButtons);
  $("allowed-tools").replaceChildren(...(selected.tools || []).map((tool) => element("li", "", tool)));
  $("tool-list-details").hidden = !(selected.tools || []).length;
  updateAvailability();
}

function setRun(run, start = run?.axis_min, end = run?.axis_max) {
  state.run = run;
  state.window = null;
  state.unit = null;
  state.hidden.clear();
  $("run-select").value = run?.run_id || "";
  $("run-detail").textContent = run ? [sourceLabel(run), run.category ? label(run.category) : null, run.historical_event ? "Historical event" : null].filter(Boolean).join(" · ") : "Import recordings to select an experiment.";
  $("run-select").disabled = !run;
  const index = run?.axis === "sample_index";
  $("start-label").textContent = index ? "Start sample" : "Start (s)";
  $("end-label").textContent = index ? "End sample" : "End (s)";
  for (const id of ["window-start", "window-end"]) {
    const field = $(id);
    field.min = run?.axis_min ?? "";
    field.max = run?.axis_max ?? "";
    field.step = index ? "1" : "any";
    field.disabled = !run;
  }
  $("window-start").value = start ?? "";
  $("window-end").value = end ?? "";
  $("apply-window").disabled = !run;
  if (run) return loadWindow();
  renderWindow();
  return Promise.resolve(false);
}

async function loadWindow() {
  const { run } = state;
  const start = $("window-start").valueAsNumber;
  const end = $("window-end").valueAsNumber;
  if (!run || !Number.isFinite(start) || !Number.isFinite(end) || start > end) {
    setMessage("scope-error", "Choose a valid interval with a start at or before its end.");
    return false;
  }
  const ticket = ++state.windowRequest;
  state.loading = true;
  state.dirty = false;
  state.window = null;
  state.hover = null;
  stopReplay();
  setMessage("scope-error", "");
  renderWindow();
  try {
    const query = new URLSearchParams({ start, end });
    const result = await api(`/api/runs/${encodeURIComponent(run.run_id)}/window?${query}`);
    if (ticket !== state.windowRequest) return false;
    state.window = result;
    setReplayPosition(0);
    saveSession();
    return true;
  } catch (error) {
    if (ticket === state.windowRequest) setMessage("scope-error", errorDescription(error, "Telemetry for this window is unavailable."));
    return false;
  } finally {
    if (ticket === state.windowRequest) {
      state.loading = false;
      renderWindow();
    }
  }
}

function putTable(container, headers, rows) {
  const wrap = element("div", "table-scroll");
  const table = element("table", "data-table");
  const heading = table.createTHead().insertRow();
  for (const value of headers) {
    const cell = element("th", "", value);
    cell.scope = "col";
    heading.append(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const value of row) {
      const cell = tr.insertCell();
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value;
    }
  }
  wrap.append(table);
  container.replaceChildren(wrap);
}

function renderWindow() {
  const { run, window: scope } = state;
  const recordingPage = state.page === "workspace" || state.page === "replay";
  $("recording-import").hidden = !recordingPage || !state.recordingsLoaded || state.runs.length > 0;
  $("controller-heading").textContent = controllerName();
  $("source-badge").hidden = !run;
  if (run) {
    const source = sourceLabel(run);
    $("source-badge").textContent = source;
    $("source-badge").classList.toggle("recorded", source !== "Synthetic Demo");
  }
  $("window-caption").textContent = run ? [run.run_id, scope ? duration(scope.start, scope.end, scope.axis) : null, run.historical_event ? "Historical event" : null].filter(Boolean).join(" · ") : "Select an experiment to display Controller telemetry.";
  $("sample-count").textContent = scope ? number(scope.sample_count) : "—";
  $("time-basis").textContent = scope ? (scope.axis === "sample_index" ? "Sample index" : "Elapsed seconds") : "—";
  const values = scope?.values?.controller_state ?? [];
  const recorded = [...new Set(values.filter((value) => value !== null && value !== undefined && value !== ""))];
  $("recorded-state").textContent = recorded.length ? recorded.map(String).join(" / ") : scope ? "Unavailable" : "—";
  $("chart-window-id").textContent = scope?.evidence_id ? scope.evidence_id : "";
  const report = activeReview();
  const matching = scope && report?.run_id === scope.run_id && report?.start === scope.start && report?.end === scope.end;
  const backends = matching ? [...new Set((report.tool_calls || []).filter((call) => call.backend && normalizedStatuses.has(String(call.status).toLowerCase())).map((call) => call.backend))] : [];
  $("backend-label").textContent = scope ? (backends.length ? `Tool backend · ${backends.join(" / ")}` : "Replay backend · Python Replay") : "";
  const metadata = Object.entries(scope?.signals ?? {});
  $("signal-details").hidden = !metadata.length;
  if (metadata.length) {
    putTable($("signal-list"), ["Signal", "Unit", "Provenance"], metadata.map(([id, meta]) => [meta.label || label(id), meta.unit || "—", meta.provenance || "Unavailable"]));
  }
  if (scope?.evidence_id) {
    $("replay-evidence-body").replaceChildren(evidenceButton(scope.evidence_id, scope), element("span", "", duration(scope.start, scope.end, scope.axis)));
  } else $("replay-evidence-body").textContent = "Window evidence is unavailable.";
  renderChartControls();
  updateAvailability();
}

function chartSeries() {
  const scope = state.window;
  if (!scope) return [];
  const allowed = state.page === "workspace" && isCompatible() && state.agentId !== "security"
    ? new Set(agent()?.signal_ids ?? []) : null;
  return Object.entries(scope.signals ?? {}).filter(([id, meta]) => {
    return meta.unit && (!allowed?.size || allowed.has(id)) && scope.values[id]?.some(Number.isFinite);
  }).map(([id, meta], index) => ({
    id, label: meta.label || label(id), unit: meta.unit, color: palette[index % palette.length],
    values: scope.values[id], dashed: id === "frequency_feedback_hz",
  }));
}

function renderChartControls() {
  const series = chartSeries();
  const units = [...new Set(series.map(({ unit }) => unit))];
  if (!units.includes(state.unit)) {
    const preferred = state.agentId === "frequency" ? "Hz" : "V";
    state.unit = units.includes(preferred) ? preferred : (units[0] ?? null);
  }
  const labels = { V: "Voltage", A: "Current", Hz: "Frequency", rpm: "Speed", "N·m": "Torque", W: "Power", Wh: "Energy" };
  $("chart-units").replaceChildren(...units.map((unit) => {
    const button = element("button", "unit-button", labels[unit] || unit);
    button.type = "button";
    button.setAttribute("aria-pressed", String(unit === state.unit));
    button.addEventListener("click", () => {
      state.unit = unit;
      state.hover = null;
      renderChartControls();
      $("chart-units").querySelector('[aria-pressed="true"]')?.focus();
    });
    return button;
  }));
  $("chart-legend").replaceChildren(...series.filter(({ unit }) => unit === state.unit).map((entry) => {
    const button = element("button", "legend-button");
    button.type = "button";
    button.setAttribute("aria-pressed", String(!state.hidden.has(entry.id)));
    const swatch = element("span", "legend-swatch");
    swatch.style.setProperty("--series-color", entry.color);
    button.append(swatch, document.createTextNode(entry.label));
    button.addEventListener("click", () => {
      if (state.hidden.has(entry.id)) state.hidden.delete(entry.id);
      else state.hidden.add(entry.id);
      button.setAttribute("aria-pressed", String(!state.hidden.has(entry.id)));
      schedulePaint();
    });
    return button;
  }));
  schedulePaint();
}

function schedulePaint() {
  if (state.paintRequest) return;
  state.paintRequest = requestAnimationFrame(() => {
    state.paintRequest = 0;
    paintChart();
  });
}

function chartDomain(series) {
  let min = Infinity;
  let max = -Infinity;
  for (const { values } of series) {
    for (const value of values) {
      if (!Number.isFinite(value)) continue;
      min = Math.min(min, value);
      max = Math.max(max, value);
    }
  }
  const padding = (max - min) * 0.09 || Math.max(Math.abs(max) * 0.02, 0.2);
  return [min - padding, max + padding];
}

function paintChart() {
  const canvas = $("chart");
  const { width, height } = canvas.getBoundingClientRect();
  if (!width || !height) return;
  const pixelRatio = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * pixelRatio) || canvas.height !== Math.round(height * pixelRatio)) {
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
  }
  const context = canvas.getContext("2d");
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, width, height);
  const scope = state.window;
  const visible = chartSeries().filter(({ id, unit }) => unit === state.unit && !state.hidden.has(id));
  if (!scope?.axis_values?.length || !visible.length) {
    state.plot = null;
    $("chart-tooltip").hidden = true;
    setMessage("chart-empty", state.loading ? "Loading telemetry…" : scope ? "No visible numeric signals in this window." : "Telemetry is unavailable for the selected window.");
    return;
  }
  setMessage("chart-empty", "");
  const left = 54;
  const right = width - 18;
  const top = 13;
  const bottom = height - 29;
  const [min, max] = chartDomain(visible);
  const span = scope.end - scope.start;
  const x = (value) => span ? left + ((value - scope.start) / span) * (right - left) : (left + right) / 2;
  const y = (value) => bottom - ((value - min) / (max - min)) * (bottom - top);
  state.plot = { left, right, top, bottom, width, scope, visible, x };

  context.font = '10px "Segoe UI", sans-serif';
  context.lineWidth = 1;
  context.strokeStyle = "#e3edef";
  context.fillStyle = "#536d77";
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let index = 0; index <= 4; index += 1) {
    const value = min + (index / 4) * (max - min);
    const py = y(value);
    context.beginPath();
    context.moveTo(left, py);
    context.lineTo(right, py);
    context.stroke();
    context.fillText(number(value, 5), left - 7, py, left - 9);
  }
  context.textAlign = "center";
  context.textBaseline = "top";
  const xTicks = span ? Math.max(2, Math.floor((right - left) / 100)) : 0;
  const drawnTicks = new Set();
  for (let index = 0; index <= xTicks; index += 1) {
    const coordinate = xTicks ? scope.start + (index / xTicks) * span : scope.start;
    const value = scope.axis === "sample_index" ? Math.round(coordinate) : coordinate;
    if (drawnTicks.has(value)) continue;
    drawnTicks.add(value);
    const px = x(value);
    context.beginPath();
    context.moveTo(px, top);
    context.lineTo(px, bottom);
    context.stroke();
    context.fillText(number(value, 5), Math.min(right, Math.max(left, px)), bottom + 7, 54);
  }

  context.save();
  context.beginPath();
  context.rect(left - 3, top - 3, right - left + 6, bottom - top + 6);
  context.clip();
  for (const series of visible) {
    context.strokeStyle = series.color;
    context.fillStyle = series.color;
    context.lineWidth = 2;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.setLineDash(series.dashed ? [5, 3] : []);
    context.beginPath();
    let previous = false;
    for (let index = 0; index < scope.axis_values.length; index += 1) {
      const coordinate = scope.axis_values[index];
      const value = series.values[index];
      if (!Number.isFinite(coordinate) || !Number.isFinite(value)) {
        previous = false;
        continue;
      }
      if (previous) context.lineTo(x(coordinate), y(value));
      else {
        context.moveTo(x(coordinate), y(value));
        context.lineTo(x(coordinate) + 0.01, y(value));
      }
      previous = true;
    }
    context.stroke();
  }
  if (state.page === "replay") drawCursor(context, x(scope.start + state.replay.position * span), top, bottom, "#24584f");
  if (state.hover !== null) {
    const px = x(scope.axis_values[state.hover]);
    drawCursor(context, px, top, bottom, "#6e8d95");
    for (const { color, values } of visible) {
      if (!Number.isFinite(values[state.hover])) continue;
      context.fillStyle = color;
      context.beginPath();
      context.arc(px, y(values[state.hover]), 3.25, 0, Math.PI * 2);
      context.fill();
    }
  }
  context.restore();
}

function drawCursor(context, x, top, bottom, color) {
  context.strokeStyle = color;
  context.lineWidth = 1;
  context.setLineDash([3, 3]);
  context.beginPath();
  context.moveTo(x, top);
  context.lineTo(x, bottom);
  context.stroke();
}

function closestSample(axis, value) {
  let low = 0;
  let high = axis.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (axis[middle] < value) low = middle + 1;
    else high = middle;
  }
  return low && value - axis[low - 1] < axis[low] - value ? low - 1 : low;
}

function hoverChart(event) {
  const plot = state.plot;
  if (!plot) return;
  const rect = $("chart").getBoundingClientRect();
  const x = event.clientX - rect.left;
  const tooltip = $("chart-tooltip");
  if (x < plot.left || x > plot.right) {
    state.hover = null;
    tooltip.hidden = true;
    schedulePaint();
    return;
  }
  const { scope } = plot;
  const coordinate = scope.start + ((x - plot.left) / (plot.right - plot.left)) * (scope.end - scope.start);
  const index = closestSample(scope.axis_values, coordinate);
  state.hover = index;
  tooltip.replaceChildren(element("strong", "", scope.axis === "sample_index" ? `Sample ${number(scope.axis_values[index])}` : `${number(scope.axis_values[index])} s`));
  for (const { label: name, unit, values } of plot.visible) {
    const row = element("div");
    row.append(element("span", "", name), element("span", "tabular", Number.isFinite(values[index]) ? `${number(values[index], 6)} ${unit}` : "Unavailable"));
    tooltip.append(row);
  }
  tooltip.hidden = false;
  tooltip.style.top = "8px";
  const right = plot.width - tooltip.offsetWidth - 6;
  tooltip.style.left = `${Math.max(6, Math.min(x + 10, right))}px`;
  schedulePaint();
}

function setReplayPosition(progress) {
  state.replay.position = Math.max(0, Math.min(1, progress));
  $("replay-seek").value = Math.round(state.replay.position * 1000);
  const scope = state.window;
  if (scope) {
    const coordinate = scope.start + (scope.end - scope.start) * state.replay.position;
    $("replay-position").textContent = scope.axis === "sample_index" ? `Sample ${number(Math.round(coordinate))}` : `${number(coordinate, 5)} s`;
  } else $("replay-position").textContent = "—";
  schedulePaint();
}

function stopReplay() {
  const replay = state.replay;
  replay.playing = false;
  replay.previousTime = null;
  cancelAnimationFrame(replay.frame);
  $("replay-play").replaceChildren(icon("play"));
  $("replay-play").setAttribute("aria-label", "Play replay");
  $("replay-play").title = "Play replay";
}

function advanceReplay(timestamp) {
  if (!state.replay.playing || !state.window) return;
  const elapsed = state.replay.previousTime === null ? 0 : timestamp - state.replay.previousTime;
  state.replay.previousTime = timestamp;
  const span = state.window.end - state.window.start;
  const scale = state.window.axis === "sample_index" ? 60 : 1;
  const durationMs = Math.max(1000, span * 1000 / scale);
  setReplayPosition(state.replay.position + (elapsed / durationMs) * Number($("replay-rate").value));
  if (state.replay.position < 1) state.replay.frame = requestAnimationFrame(advanceReplay);
  else stopReplay();
}

function playReplay() {
  if (!state.window) return;
  if (state.replay.playing) {
    stopReplay();
    return;
  }
  if (state.replay.position >= 1) setReplayPosition(0);
  state.replay.playing = true;
  $("replay-play").replaceChildren(icon("pause"));
  $("replay-play").setAttribute("aria-label", "Pause replay");
  $("replay-play").title = "Pause replay";
  state.replay.frame = requestAnimationFrame(advanceReplay);
}

function activeReview() {
  const reviews = state.history[state.agentId] ?? [];
  return reviews.find(({ review_id }) => review_id === state.activeReview[state.agentId]) ?? reviews.at(-1);
}

function download(content, filename, mimetype) {
  const href = URL.createObjectURL(new Blob([content], { type: mimetype }));
  const button = element("a");
  button.href = href;
  button.download = filename;
  document.body.append(button);
  button.click();
  button.remove();
  setTimeout(() => URL.revokeObjectURL(href), 2000);
}

function downloadJson(data, filename) {
  download(`${JSON.stringify(data, null, 2)}\n`, filename, "application/json;charset=utf-8");
}

function evidenceButton(id, context, page = state.page === "evaluation" ? "replay" : state.page) {
  const button = element("button", "evidence-link");
  button.type = "button";
  button.title = `Locate ${id} on the chart`;
  button.append(icon("link"), document.createTextNode(id));
  button.addEventListener("click", () => openWindow(context, page, id));
  return button;
}

async function openWindow(context, page, evidenceId = null) {
  const run = state.runs.find(({ run_id }) => run_id === context.run_id);
  if (!run) {
    setMessage("global-error", "The referenced experiment is unavailable in this workspace.");
    return;
  }
  setPage(page, true);
  const same = state.window?.run_id === run.run_id && state.window.start === context.start && state.window.end === context.end && !state.dirty;
  if (!same && !await setRun(run, context.start, context.end)) return;
  if (state.page !== page) return;
  if (evidenceId && state.window?.evidence_id !== evidenceId) {
    setMessage("scope-error", "This evidence ID does not match the current recording revision.");
  }
  $("controller").classList.add("evidence-focus");
  $("controller-heading").setAttribute("tabindex", "-1");
  $("controller-heading").focus({ preventScroll: true });
  $("controller").scrollIntoView({ block: "start", behavior: "instant" });
  setTimeout(() => $("controller").classList.remove("evidence-focus"), 2400);
}

function relevantEvidence(report, ids = []) {
  const links = element("div", "evidence-list");
  for (const id of new Set(Array.isArray(ids) ? ids : [])) {
    const context = (report.evidence ?? []).find((item) => item.evidence_id === id);
    if (typeof id !== "string") continue;
    if (context) links.append(evidenceButton(id, context));
    else links.append(element("span", "badge badge-status warning", `Unverified ID · ${id}`));
  }
  return links;
}

function modelName(report) {
  return report.model_used || report.model || providerLabels[report.mode] || "Unavailable";
}

function modelProvider(report) {
  return report.provider || providerLabels[report.mode] || "Unavailable";
}

function executionModel(report) {
  const display = element("div", "execution-model");
  const provider = modelProvider(report);
  const status = report.mode === "demo" ? label(report.status) : label(report.model_status);
  const executed = report.model_used || normalizedStatuses.has(String(report.model_status).toLowerCase());
  const heading = report.mode === "demo" ? "Execution engine" : executed ? "Executed model" : "Requested model";
  const model = element("strong");
  if (report.mode === "demo") model.textContent = provider;
  else model.append(element("code", "", modelName(report)));
  const role = state.agents.find(({ id }) => id === report.agent_id)?.name;
  const metadata = [role, report.mode === "demo" ? null : provider, status, `${number(report.latency_ms)} ms`].filter(Boolean).join(" · ");
  display.append(element("span", "execution-model-label", heading), model, element("span", "review-meta", metadata));
  if (report.model_used && report.model && report.model_used !== report.model) {
    display.append(element("span", "review-meta", `Requested · ${report.model}`));
  }
  return display;
}

function showReviewResult(report) {
  const review = element("div", "review-result");
  review.append(executionModel(report));
  if (report.user_question) review.append(element("p", "review-meta", `Question · ${report.user_question}`));
  const recommendation = element("div", "result-block");
  const final = report.recommendation;
  recommendation.append(element("strong", "", "Validated recommendation"), statusBadge(final?.recommendation),
    element("p", "", final?.rationale ?? "A recommendation is unavailable."));
  const links = relevantEvidence(report, final?.evidence_ids);
  if (links.childElementCount) recommendation.append(links);
  const verification = element("div", "result-block");
  verification.append(element("strong", "", `Deterministic verification · ${label(report.verification?.status)}`));
  if (report.verification?.reasons?.length) {
    const reasons = element("ul", "verification-reasons");
    reasons.append(...report.verification.reasons.map((reason) => element("li", "", label(reason))));
    verification.append(reasons);
  }
  if (Number.isInteger(report.verification?.cited_count)) {
    verification.append(element("p", "", `${report.verification.valid_count} of ${report.verification.cited_count} cited evidence IDs verified.`));
  }
  const scopeMeta = element("div", "review-meta", `${report.run_id} · ${duration(report.start, report.end, report.axis)}`);
  const exportButton = element("button", "button button-secondary");
  exportButton.type = "button";
  exportButton.append(icon("download"), document.createTextNode("Download review JSON"));
  exportButton.addEventListener("click", () => downloadJson(report, `review-${report.review_id}.json`));
  review.append(recommendation, verification, scopeMeta, exportButton);
  if (report.raw_recommendation) {
    const original = element("details", "agent-tool-list");
    original.append(element("summary", "", "Original recommendation"), element("p", "review-meta", `${label(report.raw_recommendation.recommendation)} · ${report.raw_recommendation.rationale}`));
    const originalLinks = relevantEvidence(report, report.raw_recommendation.evidence_ids);
    if (originalLinks.childElementCount) original.append(originalLinks);
    review.append(original);
  }
  if (report.model_trace?.length) review.append(codeDetails("Model and Controller execution trace", report.model_trace));
  return review;
}

function rowDetails(pairs) {
  const list = element("dl", "tool-meta");
  for (const [name, value] of pairs) {
    const pair = element("div");
    pair.append(element("dt", "", name), element("dd", "tabular", value));
    list.append(pair);
  }
  return list;
}

function codeDetails(title, payload) {
  const detail = element("details");
  detail.append(element("summary", "payload-summary", title), element("pre", "", JSON.stringify(payload, null, 2)));
  return detail;
}

function qualitySummary(quality) {
  if (!quality) return "Unavailable";
  const missing = Object.values(quality.missing_counts ?? {}).reduce((total, count) => total + (Number(count) || 0), 0);
  return [
    `${quality.missing_key_fields?.length ?? 0} required field gaps`,
    `${missing} missing values`,
    `${(quality.unit_mismatches?.length ?? 0) + (quality.source_conflicts?.length ?? 0)} unit or source conflicts`,
  ].join(" · ");
}

function toolCard(call, expanded) {
  const entry = element("details", "tool-entry");
  entry.open = expanded;
  const summary = element("summary");
  summary.append(icon("terminal"), element("span", "tool-name", call.tool), statusBadge(call.status), icon("chevron"));
  const body = element("div", "tool-body");
  const quality = call.data_quality;
  body.append(rowDetails([
    ["Calculation", call.result?.calculation_type ?? "Controller tool"],
    ["Compute backend", call.backend ?? "Unavailable"],
    ["Window samples", number(call.sample_count)],
    ["Run", call.run_id],
    ["Input window", duration(call.start, call.end, call.axis)],
    ["Data source", call.data_source ?? "Unavailable"],
    ["Data quality", qualitySummary(quality)],
    ["Threshold version", quality?.threshold_version ?? "Unavailable"],
    ["Replay evidence age", Number.isFinite(quality?.evidence_age_s) ? `${number(quality.evidence_age_s)} s` : "Unavailable"],
  ]));
  if (call.evidence_id) body.append(evidenceButton(call.evidence_id, call));
  if (call.result?.reason) body.append(element("p", "review-meta", call.result.reason));
  if (call.signal_ids?.length) body.append(element("p", "review-meta", `Inputs · ${call.signal_ids.join(", ")}`));
  if (call.result) body.append(codeDetails("Tool result and quality details", call.result));
  entry.append(summary, body);
  return entry;
}

function renderReview() {
  const report = activeReview();
  const pending = state.pending.has(state.agentId);
  const error = state.reviewErrors[state.agentId];
  const show = state.page === "workspace";
  $("audit").hidden = !show || !report;
  if (!show) return;
  const reviews = [...(state.history[state.agentId] ?? [])].reverse();
  const message = pending ? "Review in progress…" : error || (!report ? "Select a task to review the current window." : "");
  setMessage("review-message", message);
  $("review-message").classList.toggle("notice-error", Boolean(error) && !pending);
  $("review-result").hidden = !report;
  if (!report) {
    $("review-result").replaceChildren();
    return;
  }
  $("review-result").replaceChildren(showReviewResult(report));
  $("review-selector").hidden = reviews.length < 2;
  $("review-select").replaceChildren(...reviews.map((item, index) => option(`${index === 0 ? "Latest" : "Earlier"} · ${modelName(item)} · ${item.run_id} · ${duration(item.start, item.end, item.axis)}`, item.review_id)));
  $("review-select").value = report.review_id;
  const calls = report.tool_calls ?? [];
  $("audit-description").textContent = `${calls.length} ${calls.length === 1 ? "call" : "calls"} · ${report.run_id} · ${duration(report.start, report.end, report.axis)}`;
  $("tool-calls").replaceChildren(...(calls.length ? calls.map((call) => toolCard(call, true)) : [element("p", "muted", "This review recorded no Controller calls.")]));
}

function modelPayload() {
  return { mode: state.mode, model: state.mode === "demo" ? null : state.model };
}

async function requestReview(task, question = null) {
  if ($("analyze").disabled || !state.window) return;
  const { agentId, window: scope } = state;
  const payload = {
    ...modelPayload(), run_id: scope.run_id, start: scope.start, end: scope.end,
    task: task || null, question, session_id: state.sessionId,
  };
  state.pending.add(agentId);
  delete state.reviewErrors[agentId];
  updateAvailability();
  renderReview();
  try {
    const review = await api(`/api/agents/${encodeURIComponent(agentId)}/review`, payload);
    if (question) review.user_question = question;
    state.history[agentId] = [...(state.history[agentId] ?? []), review].slice(-30);
    state.activeReview[agentId] = review.review_id;
    if (question && state.agentId === agentId && $("question").value.trim() === question) $("question").value = "";
    saveSession();
  } catch (error) {
    state.reviewErrors[agentId] = errorDescription(error, "This review could not be completed.");
  } finally {
    state.pending.delete(agentId);
    renderReview();
    renderWindow();
  }
}

const metricLabels = {
  task_accuracy: "Task accuracy",
  unsupported_recommendations: "Unsupported recommendations",
  unsupported_pass_through: "Unsupported originals passed through",
  clean_false_rejection: "False rejection on clean cases",
  valid_evidence_citations: "Valid evidence citations",
};
const arms = ["baseline", "raw", "validated"];

function addCells(tableBody, rows) {
  const content = rows.map((row) => {
    const tr = element("tr");
    for (const item of row) {
      const cell = tr.insertCell();
      if (item instanceof Node) cell.append(item);
      else cell.textContent = item;
    }
    return tr;
  });
  tableBody.replaceChildren(...content);
}

function caseWindow(row) {
  const run = state.runs.find(({ run_id }) => run_id === row.run_id);
  return duration(row.start, row.end, row.axis ?? run?.axis);
}

function caseJump(row) {
  const button = element("button", "evidence-link", caseWindow(row));
  button.type = "button";
  button.title = "Open this evaluation window in Run Replay";
  button.addEventListener("click", () => openWindow(row, "replay"));
  return button;
}

async function loadCases() {
  state.cases = [];
  try {
    const { cases } = await api("/api/evaluation/cases");
    state.cases = cases;
    addCells($("case-list"), cases.map((row) => [
      `${row.case_id} · ${label(row.condition)}`, row.run_id, caseJump(row), label(row.ground_truth),
    ]));
    if (cases.length === 0) {
      addCells($("case-list"), [["Add labeled windows for the imported recordings to evaluation_cases.json beside the catalog, then restart FastAPI."]]);
      $("case-list").rows[0].cells[0].colSpan = 4;
    }
    updateEvaluationAvailability();
  } catch (error) {
    addCells($("case-list"), [[errorDescription(error, "Evaluation cases are unavailable.")]]);
    $("case-list").rows[0].cells[0].colSpan = 4;
    state.cases = null;
  }
}

function updateEvaluationAvailability() {
  $("run-evaluation").disabled = state.evaluationPending || !state.cases?.length || !canRunModel();
}

function metricValue(metric) {
  if (!metric) return "Unavailable";
  const fraction = `${number(metric.count)}/${number(metric.denominator)}`;
  const percent = Number.isFinite(metric.rate) ? metric.rate.toLocaleString("en", { style: "percent", maximumFractionDigits: 1 }) : "—";
  return `${percent} (${fraction})`;
}

function caseResult(row) {
  const entry = element("details", "tool-entry");
  const summary = element("summary");
  summary.append(icon("file"), element("span", "tool-name", `${row.case_id} · ${label(row.condition)}`), statusBadge(row.verification?.status), icon("chevron"));
  const body = element("div", "tool-body");
  body.append(rowDetails([
    ["Expected", label(row.ground_truth)],
    ["Rules baseline", label(row.baseline?.recommendation)],
    ["Original agent", label(row.raw_recommendation?.recommendation)],
    ["Agent + validator", label(row.recommendation?.recommendation)],
    ["Agent role", state.agents.find(({ id }) => id === row.agent_id)?.name || label(row.agent_id)],
    ["Model", modelName(row)],
    ["Provider", modelProvider(row)],
    ["Model status", label(row.model_status)],
    ["Latency", `${number(row.latency_ms)} ms`],
  ]));
  if (row.verification?.reasons?.length) body.append(element("p", "review-meta", row.verification.reasons.map(label).join(" · ")));
  const actions = element("div", "case-actions");
  actions.append(caseJump(row));
  for (const scope of row.evidence ?? []) actions.append(evidenceButton(scope.evidence_id, scope, "replay"));
  body.append(actions, codeDetails("Case audit details", row));
  entry.append(summary, body);
  return entry;
}

function renderEvaluation() {
  const result = state.evaluation;
  if (!result) return;
  $("evaluation-results").hidden = false;
  const runtime = result.mode === "demo" ? modelProvider(result) : `${modelName(result)} · ${modelProvider(result)}`;
  const completion = result.model_completion;
  const completed = completion ? `${completion.count}/${completion.denominator} model workflows completed` : null;
  $("evaluation-summary").textContent = [`${result.cases.length} cases`, runtime, completed, result.evaluation_id].filter(Boolean).join(" · ");
  addCells($("metric-list"), Object.entries(metricLabels).map(([key, name]) => [
    name, ...arms.map((arm) => metricValue(result.metrics[arm]?.[key])),
  ]));
  $("evaluation-notes").replaceChildren(...(result.notes ?? []).map((note) => element("p", "", note)));
  $("case-results").replaceChildren(...result.cases.map(caseResult));
}

async function runEvaluation() {
  if ($("run-evaluation").disabled) return;
  state.evaluationPending = true;
  $("run-evaluation").disabled = true;
  $("evaluation-message").classList.remove("notice-error");
  setMessage("evaluation-message", "Running fixed evaluation cases…");
  try {
    state.evaluation = await api("/api/evaluation", modelPayload());
    setMessage("evaluation-message", "");
    renderEvaluation();
  } catch (error) {
    $("evaluation-message").classList.add("notice-error");
    setMessage("evaluation-message", errorDescription(error, "The evaluation could not be completed."));
  } finally {
    state.evaluationPending = false;
    updateEvaluationAvailability();
  }
}

function csvCell(value) {
  let text = value == null ? "" : String(value);
  if (typeof value === "string" && /^[\s]*[=+@-]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

function downloadEvaluationCsv() {
  const result = state.evaluation;
  if (!result) return;
  const header = ["row_type", "evaluation_id", "mode", "provider", "model", "model_used", "model_digest", "model_status", "case_id", "condition", "agent_id", "task", "run_id", "start", "end", "axis", "data_source", "arm", "metric", "count", "denominator", "rate", "expected", "recommendation", "rationale", "evidence_ids", "tool_calls", "verification_status", "verification_reasons", "raw_output_id", "latency_ms"];
  const rows = [];
  for (const arm of arms) {
    for (const [metric, { count, denominator, rate }] of Object.entries(result.metrics[arm] ?? {})) {
      rows.push({ row_type: "metric", arm, metric, count, denominator, rate });
    }
  }
  if (result.model_completion) rows.push({ row_type: "metric", arm: "model", metric: "model_completion", ...result.model_completion });
  for (const row of result.cases) {
    for (const [index, field] of ["baseline", "raw_recommendation", "recommendation"].entries()) {
      const output = row[field];
      rows.push({
        ...row, row_type: "case", arm: arms[index], expected: row.ground_truth,
        recommendation: output?.recommendation, rationale: output?.rationale,
        evidence_ids: output?.evidence_ids?.join("|"), tool_calls: row.tool_calls?.map((call) => call.tool).join("|"),
        verification_status: row.verification?.status, verification_reasons: row.verification?.reasons?.join("|"),
      });
    }
  }
  const common = { evaluation_id: result.evaluation_id, mode: result.mode, provider: modelProvider(result), model: result.model, model_digest: result.model_digest };
  const values = rows.map((row) => {
    const expanded = { ...common, ...row };
    return header.map((key) => csvCell(expanded[key])).join(",");
  });
  download(`\uFEFF${[header.join(","), ...values].join("\r\n")}\r\n`, `evaluation-${result.evaluation_id}.csv`, "text/csv;charset=utf-8");
}

const tuningMetricFields = [
  ["iae", "Integrated absolute error", ""],
  ["rmse", "Root mean square error", ""],
  ["overshoot_pct", "Overshoot", "%"],
  ["settling_time_s", "Settling time", " s"],
  ["saturation_pct", "Actuator saturation", "%"],
  ["final_error", "Final error", ""],
];
const tuningActionLabels = {
  propose: "Propose and simulate gains", apply: "Apply gains to sandbox", rollback: "Roll back gains",
};
const tuningTraceLabels = {
  controller_read_active: "Read and simulated active gains",
  model_request: "Requested gains from the model",
  model_response: "Received model proposal",
  controller_simulate_candidate: "Simulated candidate gains",
  controller_validate_candidate: "Checked tuning criteria",
  controller_activate_gains: "Activated gains in the simulator",
  controller_restore_gains: "Restored gains in the simulator",
  controller_recheck_failed: "Held after activation trial",
  proposed: "Recorded proposal",
  applied: "Saved gain revision",
  rolled_back: "Saved rollback",
};

function tuningScenario() {
  return ["voltage", "frequency"].includes(state.agentId) ? state.agentId : null;
}

async function loadTuning(force = false) {
  const scenario = tuningScenario();
  const tuning = state.tuning;
  if (!force && tuning.scenario === scenario && (tuning.snapshot || tuning.loading || !scenario)) {
    renderTuning();
    return;
  }
  const request = ++tuning.request;
  Object.assign(tuning, { scenario, snapshot: null, error: "", message: "", loading: Boolean(scenario) });
  renderTuning();
  if (!scenario) return;
  try {
    const query = new URLSearchParams({ session_id: state.sessionId, scenario });
    const snapshot = await api(`/api/controller-tuning?${query}`);
    if (request === tuning.request) tuning.snapshot = snapshot;
  } catch (error) {
    if (request === tuning.request) tuning.error = errorDescription(error, "Controller tuning is unavailable.");
  } finally {
    if (request === tuning.request) {
      tuning.loading = false;
      renderTuning();
    }
  }
}

async function runTuning(action) {
  const tuning = state.tuning;
  const { scenario, snapshot } = tuning;
  if (!snapshot || tuning.pending || $(`tuning-${action}`).disabled) return;
  const request = { session_id: state.sessionId, scenario, revision: snapshot.revision };
  if (action === "propose") Object.assign(request, modelPayload(), { apply_if_valid: $("tuning-auto-apply").checked });
  if (action === "apply") request.proposal_id = snapshot.proposal.id;
  Object.assign(tuning, { pending: action, error: "", message: "" });
  renderTuning();
  try {
    const updated = await api(`/api/controller-tuning/${action}`, request);
    if (scenario === tuning.scenario) {
      tuning.snapshot = updated;
      const status = updated.proposal?.status;
      tuning.message = action === "rollback" ? "The previous gain revision is active." :
        status === "applied" || action === "apply" ? "The candidate gains are active in the sandbox." :
        status === "candidate_ready" ? "The simulated candidate is eligible to apply." :
        status === "rejected" ? "The candidate did not pass the simulation checks." : "The proposal was unavailable.";
    }
  } catch (error) {
    if (scenario === tuning.scenario && error.status === 409) await loadTuning(true);
    if (scenario === tuning.scenario) tuning.error = errorDescription(error, "The sandbox request could not be completed.");
  } finally {
    tuning.pending = null;
    renderTuning();
  }
}

function tuningGainText(gains) {
  return gains ? `Kp ${number(gains.kp)} · Ki ${number(gains.ki)}` : "";
}

function renderTuningMetrics(baseline, candidate) {
  $("tuning-metrics").hidden = !baseline?.metrics;
  const rows = tuningMetricFields.map(([key, title, unit]) => {
    const row = element("tr");
    const heading = element("th", "", title);
    heading.scope = "row";
    const left = baseline?.metrics?.[key];
    const right = candidate?.metrics?.[key];
    const change = Number.isFinite(left) && Number.isFinite(right) ? right - left : null;
    const delta = change === null ? "—" : `${change > 0 ? "+" : ""}${number(change, 4)}${unit === "%" ? " pp" : unit}`;
    const trend = key === "final_error" ? Math.abs(right) - Math.abs(left) : change;
    const color = change === null ? "" : trend < 0 ? "gain-improved" : trend > 0 ? "gain-worse" : "";
    const value = (metric) => Number.isFinite(metric) ? `${number(metric, 5)}${unit}` : "—";
    row.append(heading, element("td", "", value(left)), element("td", "", value(right)), element("td", color, delta));
    return row;
  });
  $("tuning-metric-rows").replaceChildren(...rows);
}

function renderTuningTrace(snapshot) {
  const proposal = snapshot?.proposal;
  const events = snapshot?.events ?? [];
  const trace = proposal?.trace ?? [];
  const visible = Boolean(proposal || events.length);
  $("tuning-trace").hidden = !visible;
  if (!visible) return;
  const identity = proposal?.model_used || proposal?.model;
  $("tuning-trace-caption").textContent = [agent()?.name, proposal?.provider,
    identity, snapshot.controller_function].filter(Boolean).join(" · ");
  const entries = [...trace, ...events.slice(-6)].map((event) => {
    const key = event.action || event.event || event.phase || event.tool;
    const row = element("div", "tuning-event");
    const details = [event.backend, event.initiator ? label(event.initiator) : null,
      event.revision == null ? null : `Revision ${event.revision}`,
      tuningGainText(event.gains), event.model].filter(Boolean).join(" · ");
    row.append(element("strong", "", tuningTraceLabels[key] || label(key)));
    if (details) row.append(element("small", "", details));
    const eventStatus = event.status ?? (event.eligible == null ? null : event.eligible ? "passed" : "blocked");
    if (eventStatus) row.append(statusBadge(eventStatus));
    return row;
  });
  $("tuning-trace-events").replaceChildren(...entries, codeDetails("Interaction record", { trace, events }));
}

function renderTuning() {
  if (state.page !== "tuning") return;
  const { snapshot, scenario, pending, loading, error, message } = state.tuning;
  const meta = snapshot?.scenarios?.find(({ id }) => id === scenario);
  const { active, proposal } = snapshot ?? {};
  const current = active?.gains;
  const candidate = proposal?.candidate?.gains;
  const ready = proposal?.status === "candidate_ready" && proposal.assessment?.eligible;
  const supportsModel = ["local", "demo"].includes(state.mode) && canRunModel();
  $("tuning-propose").disabled = !snapshot || Boolean(pending || loading) || !supportsModel;
  $("tuning-apply").disabled = !ready || Boolean(pending || loading);
  $("tuning-rollback").disabled = !snapshot?.can_rollback || Boolean(pending || loading);
  $("tuning-auto-apply").disabled = $("tuning-propose").disabled;
  for (const [action, name] of Object.entries(tuningActionLabels)) {
    const button = $(`tuning-${action}`);
    button.lastChild.textContent = action === pending ? {
      propose: "Proposing gains…", apply: "Applying gains…", rollback: "Rolling back…",
    }[action] : name;
  }
  $("active-controller-heading").textContent = scenario === "frequency" ? "Frequency PI controller" : scenario === "voltage" ? "Voltage PI controller" : "PI controller";
  $("tuning-revision").textContent = snapshot ? `Revision ${snapshot.revision}` : loading ? "Loading" : "Unavailable";
  $("tuning-active-kp").textContent = number(current?.kp);
  $("tuning-active-ki").textContent = number(current?.ki);
  const bounds = snapshot?.gain_bounds;
  const operating = bounds?.kp && bounds?.ki ? `Gain bounds · Kp ${bounds.kp.join("–")} · Ki ${bounds.ki.join("–")}` : "";
  $("tuning-scenario").textContent = [meta?.label, snapshot?.controller_function, operating].filter(Boolean).join(" · ");
  $("tuning-agent-caption").textContent = [agent()?.name, state.model || providerLabels[state.mode]].filter(Boolean).join(" · ");
  $("tuning-candidate").hidden = !candidate;
  $("tuning-candidate-kp").textContent = number(candidate?.kp);
  $("tuning-candidate-ki").textContent = number(candidate?.ki);
  const badge = $("tuning-validation");
  const status = proposal?.status;
  badge.hidden = !status;
  badge.className = `badge badge-status ${ready || status === "applied" ? "positive" : status === "rolled_back" ? "" : "warning"}`;
  badge.textContent = { candidate_ready: "Eligible to apply", rejected: "Held by validation", applied: "Applied to sandbox",
    rolled_back: "Rolled back", unavailable: "Proposal unavailable" }[status] ?? "";
  const startingGains = proposal?.baseline?.gains;
  const gainDelta = (key) => {
    const delta = candidate[key] - startingGains[key];
    return `${delta > 0 ? "+" : ""}${number(delta)}`;
  };
  setMessage("tuning-gain-change", candidate && startingGains ?
    `Change from baseline · Kp ${gainDelta("kp")} · Ki ${gainDelta("ki")}` : "");
  $("tuning-rationale-details").hidden = !proposal?.rationale;
  setMessage("tuning-rationale", proposal?.rationale);
  const reasons = proposal?.assessment?.reasons ?? [];
  $("tuning-reasons").hidden = !reasons.length;
  $("tuning-reasons").replaceChildren(...reasons.map((reason) => element("li", "", reason)));
  const backend = $("tuning-backend");
  const initialBackend = proposal?.baseline?.backend ?? active?.backend;
  const candidateBackend = proposal?.candidate?.backend;
  backend.hidden = !initialBackend;
  backend.textContent = candidateBackend && candidateBackend !== initialBackend ?
    `Baseline · ${initialBackend} / Candidate · ${candidateBackend}` : initialBackend ?? "";
  setMessage("tuning-backend-note", proposal?.candidate?.fallback_reason ?? proposal?.baseline?.fallback_reason ?? active?.fallback_reason);
  const notice = error || (!scenario ? "Choose Voltage Control or Frequency and Speed to tune a controller." :
    loading ? "Loading sandbox configuration…" :
    pending === "propose" ? "The Agent is proposing gains and comparing both closed-loop responses." :
    pending === "apply" ? "Saving the verified gain revision…" :
    pending === "rollback" ? "Restoring the previous gain revision…" :
    !supportsModel && state.mode ? "Select Local · Ollama or Demo to run sandbox tuning." : message);
  $("tuning-message").classList.toggle("notice-error", Boolean(error));
  setMessage("tuning-message", notice);
  renderTuningMetrics(proposal?.baseline ?? active, proposal?.candidate);
  renderTuningTrace(snapshot);
  paintTuningChart();
}

function tuningSeries() {
  const snapshot = state.tuning.snapshot;
  const proposal = snapshot?.proposal;
  const baseline = proposal?.baseline ?? snapshot?.active;
  const candidate = proposal?.candidate;
  const signal = state.tuning.signal;
  const series = [];
  if (signal === "output" && baseline?.trace?.reference) {
    series.push({ name: "Reference", color: "#697f89", dashed: true,
      time: baseline.trace.time_s, values: baseline.trace.reference });
  }
  if (baseline?.trace?.[signal]) {
    series.push({ name: candidate ? "Baseline" : "Active gains", color: "#456fa4",
      time: baseline.trace.time_s, values: baseline.trace[signal] });
  }
  if (candidate?.trace?.[signal]) {
    series.push({ name: "Candidate", color: "#087d6b", time: candidate.trace.time_s, values: candidate.trace[signal] });
  }
  return series.filter(({ time, values }) => time?.length > 1 && values?.length === time.length);
}

function paintTuningChart() {
  if (state.page !== "tuning") return;
  const canvas = $("tuning-chart");
  const { width, height } = canvas.getBoundingClientRect();
  if (!width || !height) return;
  const pixelRatio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * pixelRatio);
  canvas.height = Math.round(height * pixelRatio);
  const context = canvas.getContext("2d");
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  const series = tuningSeries();
  const legend = $("tuning-legend");
  legend.hidden = !series.length;
  setMessage("tuning-chart-empty", series.length ? "" : state.tuning.loading ?
    "Loading simulated response…" : "Run a gain proposal to compare responses.");
  state.tuning.plot = null;
  $("tuning-chart-tooltip").hidden = true;
  if (!series.length) return;
  legend.replaceChildren(...series.map(({ name, color, dashed }) => {
    const key = element("li");
    const swatch = element("i", dashed ? "dashed" : "");
    swatch.style.setProperty("--series-color", color);
    swatch.setAttribute("aria-hidden", "true");
    key.append(swatch, document.createTextNode(name));
    return key;
  }));
  const left = 52, right = width - 16, top = 15, bottom = height - 43;
  const [min, max] = chartDomain(series);
  const start = series[0].time[0], end = series[0].time.at(-1);
  const x = (value) => left + ((value - start) / (end - start)) * (right - left);
  const y = (value) => bottom - ((value - min) / (max - min)) * (bottom - top);
  state.tuning.plot = { left, right, start, end, width, series };
  context.font = '11px "Segoe UI", sans-serif';
  context.strokeStyle = "#e1ebed";
  context.fillStyle = "#516b76";
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = min + (tick / 4) * (max - min);
    context.beginPath();
    context.moveTo(left, y(value));
    context.lineTo(right, y(value));
    context.stroke();
    context.fillText(number(value, 4), left - 7, y(value));
  }
  context.textAlign = "center";
  for (let tick = 0; tick <= 5; tick += 1) {
    const coordinate = start + (tick / 5) * (end - start);
    context.beginPath();
    context.moveTo(x(coordinate), top);
    context.lineTo(x(coordinate), bottom);
    context.stroke();
    context.fillText(number(coordinate, 4), x(coordinate), bottom + 13);
  }
  context.fillText("Simulation time (s)", (left + right) / 2, bottom + 31);
  for (const { time, values, color, dashed } of series) {
    context.strokeStyle = color;
    context.lineWidth = dashed ? 1.6 : 2.35;
    context.setLineDash(dashed ? [5, 3] : []);
    context.beginPath();
    context.moveTo(x(time[0]), y(values[0]));
    for (let index = 1; index < time.length; index += 1) context.lineTo(x(time[index]), y(values[index]));
    context.stroke();
  }
}

function hoverTuningChart(event) {
  const plot = state.tuning.plot;
  if (!plot) return;
  const rect = $("tuning-chart").getBoundingClientRect();
  const pointer = event.clientX - rect.left;
  const tooltip = $("tuning-chart-tooltip");
  if (pointer < plot.left || pointer > plot.right) {
    tooltip.hidden = true;
    return;
  }
  const coordinate = plot.start + ((pointer - plot.left) / (plot.right - plot.left)) * (plot.end - plot.start);
  tooltip.replaceChildren(element("strong", "", `${number(coordinate, 4)} s`));
  for (const { name, time, values } of plot.series) {
    const summary = element("div");
    summary.append(element("span", "", name), element("span", "tabular", number(values[closestSample(time, coordinate)], 5)));
    tooltip.append(summary);
  }
  tooltip.style.left = `${Math.min(pointer + 9, Math.max(6, plot.width - 160))}px`;
  tooltip.style.top = "12px";
  tooltip.hidden = false;
}

async function loadReviewHistory() {
  try {
    const response = await api(`/api/sessions/${encodeURIComponent(state.sessionId)}/reviews`);
    const runIds = new Set(state.runs.map(({ run_id }) => run_id));
    for (const report of response.reviews) {
      if (!runIds.has(report.run_id)) continue;
      const history = state.history[report.agent_id] ?? [];
      if (history.some(({ review_id }) => review_id === report.review_id)) continue;
      state.history[report.agent_id] = [...history, report].slice(-30);
    }
    if (response.reviews.length) {
      saveSession();
      renderWindow();
      renderReview();
    }
  } catch { /* The current tab can retain reviews during an audit connection error. */ }
}

async function initialize() {
  const initial = location.hash.slice(1);
  setPage(Object.hasOwn(pageCopy, initial) ? initial : "workspace");
  const [agents, runs] = await Promise.allSettled([api("/api/agents"), api("/api/runs")]);
  if (agents.status === "fulfilled") {
    state.agents = agents.value.agents;
    if (!agent()) state.agentId = state.agents[0]?.id;
    const { modes, models, model_options, default_model, default_mode, default_models } = agents.value;
    const preferredMode = state.modePreference ?? default_mode;
    state.mode = modes.includes(preferredMode) ? preferredMode : modes[0];
    state.modelOptions = model_options ?? { live: (models ?? []).map((id) => ({ id, label: id, provider: "OpenAI" })) };
    state.defaultModels = default_models ?? { live: default_model };
    state.localRuntimeAvailable = agents.value.local_runtime_available;
    $("mode-select").replaceChildren(...modes.map((mode) => option(modeLabels[mode] || label(mode), mode)));
    $("mode-control").hidden = modes.length < 2;
    renderModelControls();
    renderAgents();
  }
  if (runs.status === "fulfilled") {
    state.runs = runs.value.runs;
    state.recordingsLoaded = true;
    const runIds = new Set(state.runs.map(({ run_id }) => run_id));
    for (const [agentId, history] of Object.entries(state.history)) {
      state.history[agentId] = history.filter(({ run_id }) => runIds.has(run_id));
    }
    $("run-select").replaceChildren(...state.runs.map((run) => option(runLabel(run), run.run_id)));
    if (!state.runs.length) $("run-select").append(option("No recordings imported", ""));
    const previous = state.runs.find(({ run_id }) => run_id === saved.runId);
    const first = state.runs.find(({ signals }) => signals.va_rms) ?? state.runs[0];
    if (previous) {
      if (!await setRun(previous, saved.start, saved.end)) await setRun(previous);
    } else await setRun(first);
    loadReviewHistory();
  }
  if (agents.status === "rejected" || runs.status === "rejected") {
    const failure = agents.status === "rejected" ? agents : runs;
    setMessage("global-error", errorDescription(failure.reason, "The workbench API is unavailable."));
  }
  renderReview();
}

for (const navigation of document.querySelectorAll("[data-page]")) {
  navigation.addEventListener("click", (event) => {
    event.preventDefault();
    setPage(navigation.dataset.page, true);
  });
}
document.querySelector(".brand").addEventListener("click", (event) => {
  event.preventDefault();
  setPage("workspace", true);
});
window.addEventListener("popstate", () => setPage(location.hash.slice(1) || "workspace"));
$("mode-select").addEventListener("change", (event) => {
  state.mode = event.target.value;
  state.modePreference = state.mode;
  renderModelControls();
});
$("run-select").addEventListener("change", (event) => setRun(state.runs.find(({ run_id }) => run_id === event.target.value)));
$("scope").addEventListener("submit", (event) => { event.preventDefault(); loadWindow(); });
for (const id of ["window-start", "window-end"]) {
  $(id).addEventListener("input", () => {
    state.dirty = $("window-start").valueAsNumber !== state.window?.start || $("window-end").valueAsNumber !== state.window?.end;
    if (state.dirty) stopReplay();
    updateAvailability();
  });
}
$("analyze").addEventListener("click", () => requestReview(agent()?.tasks?.[0]?.id));
$("question-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (question) requestReview(null, question);
});
$("review-select").addEventListener("change", (event) => {
  state.activeReview[state.agentId] = event.target.value;
  renderReview();
  renderWindow();
});
$("chart").addEventListener("pointermove", hoverChart);
$("chart").addEventListener("pointerleave", () => {
  state.hover = null;
  $("chart-tooltip").hidden = true;
  schedulePaint();
});
new ResizeObserver(schedulePaint).observe($("chart-plot"));
$("replay-play").addEventListener("click", playReplay);
$("replay-reset").addEventListener("click", () => { stopReplay(); setReplayPosition(0); });
$("replay-seek").addEventListener("input", (event) => { stopReplay(); setReplayPosition(Number(event.target.value) / 1000); });
$("run-evaluation").addEventListener("click", runEvaluation);
$("export-json").addEventListener("click", () => {
  if (state.evaluation) downloadJson(state.evaluation, `evaluation-${state.evaluation.evaluation_id}.json`);
});
$("export-csv").addEventListener("click", downloadEvaluationCsv);
$("open-tuning").addEventListener("click", (event) => {
  event.preventDefault();
  setPage("tuning", true);
});
for (const action of Object.keys(tuningActionLabels)) {
  $(`tuning-${action}`).addEventListener("click", () => runTuning(action));
}
for (const signal of ["output", "command"]) {
  $(`tuning-show-${signal}`).addEventListener("click", () => {
    state.tuning.signal = signal;
    $("tuning-show-output").setAttribute("aria-pressed", String(signal === "output"));
    $("tuning-show-command").setAttribute("aria-pressed", String(signal === "command"));
    paintTuningChart();
  });
}
$("tuning-chart").addEventListener("pointermove", hoverTuningChart);
$("tuning-chart").addEventListener("pointerleave", () => { $("tuning-chart-tooltip").hidden = true; });
new ResizeObserver(paintTuningChart).observe($("tuning-chart-plot"));

initialize();
