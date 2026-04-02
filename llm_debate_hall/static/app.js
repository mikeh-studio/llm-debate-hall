const SAMPLE_NAMES = ["Athena", "Burke", "Cassius", "Diotima", "Erasmus"];
const SAMPLE_PRESETS = ["openai", "anthropic", "openai", "anthropic", "ollama"];
const DEFAULT_PERSONA_IDS = [
  "stoic_rationalist",
  "pragmatic_engineer",
  "humanist_mediator",
  "skeptical_historian",
  "utilitarian_analyst",
  "nietzschean_iconoclast",
];
const DRAFT_STORAGE_KEY = "llm-debate-hall:draft:v1";
const ARENA_PRESENTATION_STORAGE_KEY = "llm-debate-hall:arena-presentation:v1";
const SESSION_SYNC_TIMEOUT_MS = 8000;
const SESSION_SYNC_INTERVAL_MS = 250;

const state = {
  presets: [],
  personas: [],
  sessions: [],
  activeView: "setup",
  activeSessionId: null,
  activeSession: null,
  socket: null,
  seatConfigs: [],
  nextSeatId: 0,
  threadEntries: [],
  traceEvents: [],
  pendingThreadEntry: null,
  questionSuggestions: [],
  arenaFeedback: "",
  arenaFeedbackKind: "",
  personaFeedback: "",
  personaFeedbackKind: "",
  startInFlight: false,
  popoverMode: null,
  popoverSeatId: null,
  presentationMode: "classic",
};

const $ = (id) => document.getElementById(id);

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(error || `Request failed: ${response.status}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function parseJsonArray(value) {
  if (!value.trim()) return null;
  return JSON.parse(value);
}

function parseJsonObject(value) {
  if (!value.trim()) return {};
  const parsed = JSON.parse(value);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("Env override must be a JSON object.");
  }
  return parsed;
}

function parseCsv(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function clampPersonaIntensity(value) {
  const numeric = Number.parseFloat(String(value ?? 1));
  if (!Number.isFinite(numeric)) return 1;
  return Math.min(1.5, Math.max(0.5, numeric));
}

function selectablePersonas() {
  return state.personas.filter((persona) => persona.is_selectable);
}

function personaById(personaId) {
  return state.personas.find((persona) => persona.id === personaId) || null;
}

function personaIconPath(personaId) {
  return personaById(personaId)?.icon_path || "/static/assets/persona-icons/builtin/fallback.svg";
}

function defaultPersonaChoiceForSeat(index = 0) {
  const selectable = selectablePersonas();
  if (!selectable.length) return "auto";

  const preferredId = DEFAULT_PERSONA_IDS[index % DEFAULT_PERSONA_IDS.length];
  if (selectable.some((persona) => persona.id === preferredId)) return preferredId;
  return selectable[index % selectable.length]?.id || selectable[0]?.id || "auto";
}

function readArenaPresentationMode() {
  try {
    const saved = window.localStorage.getItem(ARENA_PRESENTATION_STORAGE_KEY);
    return saved === "pixel-stage" ? "pixel-stage" : "classic";
  } catch {
    return "classic";
  }
}

function persistArenaPresentationMode() {
  window.localStorage.setItem(ARENA_PRESENTATION_STORAGE_KEY, state.presentationMode);
}

function readDraft() {
  try {
    const raw = window.localStorage.getItem(DRAFT_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function persistDraft() {
  if (state.activeSession) return;
  const payload = {
    seatConfigs: state.seatConfigs,
    judge: {
      name: $("judge-name")?.value ?? "",
      preset_id: $("judge-preset")?.value ?? "",
      model_name: $("judge-model")?.value ?? "",
      command: $("judge-command")?.value ?? "",
      args: $("judge-args")?.value ?? "",
      env: $("judge-env")?.value ?? "",
    },
    composer: $("main-composer")?.value ?? "",
  };
  window.localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify(payload));
}

function restoreDraft() {
  const draft = readDraft();
  if (!draft) return false;
  if (Array.isArray(draft.seatConfigs) && draft.seatConfigs.length >= 2 && draft.seatConfigs.length <= 5) {
    state.seatConfigs = draft.seatConfigs.map((seat, index) => ({
      id: seat.id || `seat-${state.nextSeatId++}`,
      display_name: seat.display_name || SAMPLE_NAMES[index] || `Debater ${index + 1}`,
      preset_id: seat.preset_id || defaultSeatPresetId(index),
      model_name: normalizedModelForPreset(seat.preset_id || defaultSeatPresetId(index), seat.model_name || ""),
      persona_choice: seat.persona_choice || defaultPersonaChoiceForSeat(index),
      persona_intensity: clampPersonaIntensity(seat.persona_intensity),
      command: seat.command || "",
      args_template: seat.args_template || "",
      env_json: seat.env_json || "",
    }));
  }
  if (draft.judge) {
    $("judge-name").value = draft.judge.name || "Solon";
    $("judge-preset").value = draft.judge.preset_id || preferredPresetId();
    $("judge-model").dataset.value = normalizedModelForPreset($("judge-preset").value, draft.judge.model_name || "");
    $("judge-command").value = draft.judge.command || "";
    $("judge-args").value = draft.judge.args || "";
    $("judge-env").value = draft.judge.env || "";
  }
  $("main-composer").value = draft.composer || "";
  return true;
}

function setArenaFeedback(message = "", kind = "") {
  state.arenaFeedback = message;
  state.arenaFeedbackKind = kind;
}

function setPersonaFeedback(message = "", kind = "") {
  state.personaFeedback = message;
  state.personaFeedbackKind = kind;
}

function personaOptions(selectedValue = "auto") {
  const options = [`<option value="auto" ${selectedValue === "auto" ? "selected" : ""}>Auto pick</option>`];
  selectablePersonas()
    .forEach((persona) => {
      options.push(
        `<option value="${escapeHtml(persona.id)}" ${persona.id === selectedValue ? "selected" : ""}>
          ${escapeHtml(persona.name)}
        </option>`
      );
    });
  return options.join("");
}

function presetOptions(selectedId = "") {
  return state.presets
    .map(
      (preset) =>
        `<option value="${escapeHtml(preset.id)}" ${preset.id === selectedId ? "selected" : ""}>
          ${escapeHtml(preset.label)}
        </option>`
    )
    .join("");
}

function presetById(presetId) {
  return state.presets.find((preset) => preset.id === presetId) || null;
}

function activeModelsForPreset(presetId) {
  return presetById(presetId)?.active_models || [];
}

function verifiedPresetIds() {
  return state.presets
    .filter(
      (preset) =>
        preset.is_available !== false &&
        !preset.requires_command_override &&
        !(preset.missing_env_vars || []).length &&
        (preset.active_models || []).length
    )
    .map((preset) => preset.id);
}

function preferredPresetId() {
  const verified = verifiedPresetIds();
  return verified.find((presetId) => ["openai", "anthropic", "ollama"].includes(presetId)) || verified[0] || state.presets[0]?.id || "";
}

function defaultSeatPresetId(index = 0) {
  const verified = verifiedPresetIds();
  const samplePresetId = SAMPLE_PRESETS[index];
  if (samplePresetId && verified.includes(samplePresetId)) return samplePresetId;
  return verified[index % Math.max(verified.length, 1)] || state.presets[0]?.id || "";
}

function presetNoteText(presetId) {
  const preset = presetById(presetId);
  if (!preset) return "";
  if ((preset.missing_env_vars || []).length) {
    return `${preset.description} Missing env: ${preset.missing_env_vars.join(", ")}. Add them below as JSON if needed.`;
  }
  if (preset.model_validation_mode === "fallback" && preset.is_available === false) {
    return `${preset.description} This CLI is not installed locally, so the selector is using the preset's built-in model list until the CLI is available.`;
  }
  if (preset.is_available === false) {
    return `${preset.description} This CLI is not installed locally on this machine.`;
  }
  if (preset.model_validation_mode === "fallback") {
    return `${preset.description} Live model validation did not succeed here, so the preset is using its built-in fallback model list.`;
  }
  if (!(preset.active_models || []).length && !preset.requires_command_override) {
    return `${preset.description} No models are currently selectable for this preset.`;
  }
  return preset.requires_command_override
    ? `${preset.description} Add a command override before running.`
    : preset.description;
}

function modelOptions(presetId, selectedModel = "") {
  const models = activeModelsForPreset(presetId);
  if (!models.length) return '<option value="">No active models available</option>';
  return models
    .map(
      (model) =>
        `<option value="${escapeHtml(model)}" ${model === selectedModel ? "selected" : ""}>${escapeHtml(model)}</option>`
    )
    .join("");
}

function defaultModelForPreset(presetId) {
  return presetById(presetId)?.default_model || activeModelsForPreset(presetId)[0] || "";
}

function normalizedModelForPreset(presetId, requestedModel = "") {
  return activeModelsForPreset(presetId).includes(requestedModel) ? requestedModel : defaultModelForPreset(presetId);
}

function buildSeatConfig(index = 0) {
  const presetId = defaultSeatPresetId(index);
  return {
    id: `seat-${state.nextSeatId++}`,
    display_name: SAMPLE_NAMES[index] || `Debater ${index + 1}`,
    preset_id: presetId,
    model_name: defaultModelForPreset(presetId),
    persona_choice: defaultPersonaChoiceForSeat(index),
    persona_intensity: 1,
    command: "",
    args_template: "",
    env_json: "",
  };
}

function initializeSeatConfigs() {
  state.seatConfigs = [buildSeatConfig(0), buildSeatConfig(1)];
}

function currentDebaters() {
  if (state.activeSession) {
    return state.activeSession.agents.filter((agent) => agent.role === "debater");
  }
  return state.seatConfigs.map((seat, index) => ({
    id: seat.id,
    display_name: seat.display_name || `Debater ${index + 1}`,
    preset_id: seat.preset_id,
    model_name: seat.model_name || defaultModelForPreset(seat.preset_id),
    persona_id: seat.persona_choice === "auto" ? null : seat.persona_choice,
  }));
}

function currentJudge() {
  if (state.activeSession) {
    return state.activeSession.agents.find((agent) => agent.role === "judge");
  }
  return {
    display_name: $("judge-name").value.trim() || "Judge",
    preset_id: $("judge-preset").value || preferredPresetId(),
    model_name: $("judge-model").value || defaultModelForPreset($("judge-preset").value),
  };
}

function personaLabel(personaId) {
  if (!personaId) return "Auto";
  return state.personas.find((persona) => persona.id === personaId)?.name || personaId.replaceAll("_", " ");
}

function personaIntensityLabel(value) {
  return `${Math.round(clampPersonaIntensity(value) * 100)}%`;
}

function setView(view) {
  state.activeView = view;
  document.querySelectorAll(".nav-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("is-active", section.id === `view-${view}`);
  });
}

function capitalize(str) {
  return str.charAt(0).toUpperCase() + str.slice(1);
}

function renderArenaHeader() {
  const topic = state.activeSession?.topic || "Waiting for a debate to begin";
  const status = state.activeSession?.status || "setup";
  const label = capitalize(status.replaceAll("_", " "));
  $("arena-topic").textContent = topic;
  $("arena-status").textContent = label;
  $("arena-subtitle").textContent = state.activeSession
    ? "A structured multi-agent debate rendered as one continuous transcript."
    : "Configure debaters in Setup, then start the debate.";
  const sessionPill = $("active-session-pill");
  if (sessionPill) sessionPill.textContent = state.activeSession ? `${topic} · ${label}` : "";
  $("export-session").disabled = !state.activeSessionId;
  $("export-trace").disabled = !state.activeSessionId;
  const composerShell = $("arena-composer-shell");
  if (composerShell) {
    composerShell.hidden = !state.activeSession || status === "completed" || status === "failed";
  }
}

function renderParticipantStrip() {
  const judge = currentJudge();
  const participants = [
    ...currentDebaters().map(
      (seat) => {
        const sessionStatusNote =
          seat.provider_session?.status === "fallback" || seat.provider_session?.last_error
            ? " · reset session available"
            : "";
        return `
        <div class="participant-chip debater" data-seat-id="${escapeHtml(seat.id)}" role="button" tabindex="0">
          <strong>${escapeHtml(seat.display_name)}</strong>
          <span>${escapeHtml((seat.model_name || "No model") + " · " + personaLabel(seat.persona_id) + " · " + personaIntensityLabel(seat.persona_intensity) + sessionStatusNote)}</span>
        </div>
      `;
      }
    ),
    `
      <div class="participant-chip judge" data-role="judge" role="button" tabindex="0">
        <strong>${escapeHtml(judge.display_name)}</strong>
        <span>Judge · ${escapeHtml(judge.model_name || "No model")}</span>
      </div>
    `,
  ];
  $("participant-strip").innerHTML = participants.join("");
}

function renderArenaFeedback() {
  const feedback = $("thread-feedback");
  const visible = Boolean(state.arenaFeedback);
  feedback.textContent = state.arenaFeedback || "";
  feedback.className = `thread-feedback${visible ? " is-visible" : ""}${state.arenaFeedbackKind ? ` is-${state.arenaFeedbackKind}` : ""}`;
}

function renderSuggestionList() {
  $("suggestion-list").innerHTML = state.questionSuggestions
    .map(
      (suggestion) =>
        `<button class="suggestion-chip" data-suggestion="${escapeHtml(suggestion)}">${escapeHtml(suggestion)}</button>`
    )
    .join("");
}

function deriveThreadEntriesFromSession(session) {
  if (session.thread_entries?.length) return session.thread_entries;
  const entries = [];
  session.rounds.forEach((round) => {
    entries.push({
      id: `round-${round.id}`,
      kind: "system",
      display_name: "Debate Hall",
      display_text: `Round ${round.round_index} started: ${round.round_type}.`,
      round_type: round.round_type,
      round_index: round.round_index,
      agent_id: null,
      payload: { event: "round_started" },
      created_at: round.started_at,
    });
    session.messages
      .filter((message) => message.round_index === round.round_index)
      .forEach((message) => {
        entries.push({
          id: `message-${message.id}`,
          kind: "agent",
          display_name: message.agent_name || message.agent_id,
          display_text: message.display_text,
          round_type: message.round_type,
          round_index: message.round_index,
          agent_id: message.agent_id,
          payload: message.normalized_payload || {},
          created_at: message.created_at,
        });
      });
  });
  if (session.judge_score) {
    entries.push({
      id: `judge-${session.judge_score.id}`,
      kind: "judge",
      display_name: currentJudge().display_name,
      display_text: session.judge_score.rationale,
      round_type: null,
      round_index: null,
      agent_id: session.judge_score.judge_agent_id,
      payload: {
        winner_agent_id: session.judge_score.winner_agent_id,
        criteria: session.judge_score.criteria,
      },
      created_at: session.judge_score.created_at,
    });
  }
  return entries;
}

function syncThreadEntriesFromSession(session) {
  state.threadEntries = deriveThreadEntriesFromSession(session);
  state.pendingThreadEntry = null;
  state.traceEvents = Array.isArray(session.trace_events) ? [...session.trace_events] : [];
}

function threadEntriesForRender() {
  const entries = [...state.threadEntries];
  if (state.pendingThreadEntry) entries.push(state.pendingThreadEntry);
  return entries;
}

function formatLatency(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return "n/a";
  return `${Math.round(Number(ms))} ms`;
}

function formatEstimatedCost(value) {
  if (value == null || value === "") return "n/a";
  return `$${Number(value).toFixed(Number(value) < 0.01 ? 4 : 3)}`;
}

function latestTurnTraceEvent() {
  return [...state.traceEvents].reverse().find((event) => event.event_type === "turn_completed") || null;
}

function traceEventSummary(event) {
  if (!event) return "";
  if (event.payload?.summary) return event.payload.summary;
  const label = event.event_type.replaceAll("_", " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function renderObservability() {
  const summary = $("observability-summary");
  const timeline = $("trace-timeline");
  if (!summary || !timeline) return;
  if (!state.activeSession) {
    summary.innerHTML = "";
    timeline.innerHTML = "";
    return;
  }

  const latestTurn = latestTurnTraceEvent();
  summary.innerHTML = latestTurn
    ? `
      <article class="observability-card">
        <div class="observability-card-head">
          <div>
            <span class="arena-presentation-badge">Observability</span>
            <h3>${escapeHtml(latestTurn.agent_name || "Latest turn")}</h3>
          </div>
          <span class="trace-event-type">${escapeHtml((latestTurn.round_type || "turn").replaceAll("_", " "))}</span>
        </div>
        <div class="observability-metrics">
          <div><strong>Latency</strong><span>${escapeHtml(formatLatency(latestTurn.payload?.latency_ms))}</span></div>
          <div><strong>Provider</strong><span>${escapeHtml(latestTurn.payload?.preset_id || "n/a")}</span></div>
          <div><strong>Model</strong><span>${escapeHtml(latestTurn.payload?.model_name || "n/a")}</span></div>
          <div><strong>Fallback</strong><span>${latestTurn.payload?.used_fallback ? "Yes" : "No"}</span></div>
          <div><strong>Tokens</strong><span>${escapeHtml(String(latestTurn.payload?.estimated_total_tokens ?? "n/a"))}</span></div>
          <div><strong>Cost</strong><span>${escapeHtml(formatEstimatedCost(latestTurn.payload?.estimated_cost_usd))}</span></div>
        </div>
      </article>
    `
    : `
      <article class="observability-card empty">
        <span class="arena-presentation-badge">Observability</span>
        <p>No completed turn metrics yet. Start a debate to populate the trace timeline.</p>
      </article>
    `;

  timeline.innerHTML = `
    <div class="observability-card">
      <div class="observability-card-head">
        <div>
          <span class="arena-presentation-badge">Timeline</span>
          <h3>Session Trace</h3>
        </div>
        <span class="trace-event-type">${escapeHtml(String(state.traceEvents.length))} events</span>
      </div>
      <div class="timeline-list">
        ${
          state.traceEvents.length
            ? state.traceEvents
                .map(
                  (event) => `
                    <article class="timeline-item">
                      <div class="timeline-item-head">
                        <strong>${escapeHtml(traceEventSummary(event))}</strong>
                        <span>${escapeHtml(new Date(event.created_at).toLocaleTimeString())}</span>
                      </div>
                      <div class="timeline-item-meta">
                        <span>${escapeHtml(event.agent_name || "System")}</span>
                        <span>${escapeHtml(event.event_type)}</span>
                        ${event.payload?.latency_ms != null ? `<span>${escapeHtml(formatLatency(event.payload.latency_ms))}</span>` : ""}
                        ${event.payload?.provider_session_status ? `<span>${escapeHtml(event.payload.provider_session_status)}</span>` : ""}
                      </div>
                    </article>
                  `
                )
                .join("")
            : '<p class="timeline-empty">No trace events yet.</p>'
        }
      </div>
    </div>
  `;
}

function activeRoundForPendingSpeaker() {
  if (!state.activeSession?.rounds?.length) return null;
  return [...state.activeSession.rounds]
    .sort((a, b) => b.round_index - a.round_index)
    .find((round) => !round.completed_at) || null;
}

function buildPredictedDebaterPendingEntry() {
  if (!state.activeSession || state.activeSession.status !== "running") return null;
  const activeRound = activeRoundForPendingSpeaker();
  if (!activeRound) return null;

  const spokenAgentIds = new Set(
    state.threadEntries
      .filter((entry) => entry.kind === "agent" && entry.round_index === activeRound.round_index && entry.agent_id)
      .map((entry) => entry.agent_id)
  );
  const nextDebater = currentDebaters().find((agent) => agent.id && !spokenAgentIds.has(agent.id));
  if (!nextDebater) return null;

  return {
    id: `predicted-${activeRound.round_type}-${activeRound.round_index}-${nextDebater.id}`,
    kind: "agent",
    display_name: nextDebater.display_name,
    display_text: "",
    round_type: activeRound.round_type,
    round_index: activeRound.round_index,
    agent_id: nextDebater.id,
    payload: { local: true, predicted: true },
    created_at: new Date().toISOString(),
    pending: true,
  };
}

function syncPredictedPendingEntry() {
  if (state.pendingThreadEntry && !state.pendingThreadEntry.payload?.predicted) {
    return;
  }

  const predicted = buildPredictedDebaterPendingEntry();
  if (predicted) {
    if (!state.pendingThreadEntry || state.pendingThreadEntry.id !== predicted.id) {
      state.pendingThreadEntry = predicted;
    }
    return;
  }

  if (state.pendingThreadEntry?.payload?.predicted) {
    state.pendingThreadEntry = null;
  }
}

function threadEntryRoleLabel(entry) {
  if (entry.kind === "moderator") return "Moderator";
  if (entry.kind === "judge") return "Judge";
  if (entry.kind === "system") return "System";
  return entry.round_type || "Debater";
}

function thinkingDotsMarkup(label = "Waiting for response") {
  return `
    <div class="thinking-dots" aria-label="${escapeHtml(label)}">
      <span></span><span></span><span></span>
    </div>
  `;
}

function renderThread() {
  syncPredictedPendingEntry();
  const thread = $("thread-view");
  const entries = threadEntriesForRender();
  if (!entries.length) {
    thread.innerHTML = `
      <div class="thread-empty">
        <div>
          <h3>No transcript yet</h3>
          <p>Use the composer to start a debate, then the entire exchange will unfold here as one conversation.</p>
        </div>
      </div>
    `;
    return;
  }
  thread.innerHTML = entries
    .map(
      (entry) => `
        <article class="thread-entry ${escapeHtml(entry.kind)} ${entry.pending ? "pending" : ""}">
          <div class="thread-entry-head">
            <div>
              <div class="thread-entry-title">${escapeHtml(entry.display_name)}</div>
              <div class="thread-entry-meta">${escapeHtml(threadEntryRoleLabel(entry))}</div>
            </div>
            <div class="thread-entry-role">${escapeHtml(entry.round_type || entry.kind)}</div>
          </div>
          <div class="thread-entry-body">${entry.display_text ? escapeHtml(entry.display_text) : thinkingDotsMarkup(`${entry.display_name || "Speaker"} is thinking`)}</div>
        </article>
      `
    )
    .join("");
  thread.scrollTop = thread.scrollHeight;
}

function truncateStageBubble(text, limit = 148) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) return "...";
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, limit - 1).trimEnd()}…`;
}

function activeStageSpeakerEntry() {
  if (state.pendingThreadEntry && ["agent", "judge"].includes(state.pendingThreadEntry.kind)) {
    return state.pendingThreadEntry;
  }
  return [...state.threadEntries].reverse().find((entry) => ["agent", "judge"].includes(entry.kind)) || null;
}

function spriteForActor(actor, index = 0) {
  if (actor.role === "judge") {
    return "/static/assets/sprites/judge-archivist.svg";
  }
  if (actor.persona_id) {
    return personaIconPath(actor.persona_id);
  }
  const variants = [
    "/static/assets/sprites/debater-sage.svg",
    "/static/assets/sprites/debater-ember.svg",
    "/static/assets/sprites/debater-ink.svg",
  ];
  return variants[index % variants.length] || "/static/assets/sprites/debater-fallback.svg";
}

function isStageSpeaker(actor, entry) {
  if (!entry) return false;
  if (actor.role === "judge") return entry.kind === "judge";
  if (entry.kind !== "agent") return false;
  if (entry.agent_id && actor.id) return entry.agent_id === actor.id;
  return entry.display_name === actor.display_name;
}

function renderArenaPresentation() {
  syncPredictedPendingEntry();
  const toggle = $("arena-presentation-toggle");
  if (toggle) {
    toggle.querySelectorAll("[data-presentation-mode]").forEach((button) => {
      const active = button.dataset.presentationMode === state.presentationMode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }
  renderPixelStage();
}

function renderPixelStage() {
  const shell = $("pixel-stage-shell");
  const stage = $("pixel-stage");
  if (!shell || !stage) return;

  if (state.presentationMode !== "pixel-stage") {
    shell.hidden = true;
    stage.innerHTML = "";
    return;
  }

  shell.hidden = false;
  if (!state.activeSession) {
    stage.innerHTML = `
      <div class="pixel-stage-empty">
        <strong>No debate in the chamber</strong>
        <p>Start a debate to see the council enter the Pixel Stage.</p>
      </div>
    `;
    return;
  }

  const debaters = currentDebaters().map((actor, index) => ({ ...actor, role: "debater", spriteIndex: index }));
  const judge = { ...currentJudge(), role: "judge", spriteIndex: debaters.length };
  const actors = [...debaters, judge];
  const activeEntry = activeStageSpeakerEntry();

  stage.innerHTML = `
    <div class="pixel-stage-backdrop" aria-hidden="true"></div>
    <div class="pixel-stage-actors">
      ${actors
        .map((actor, index) => {
          const active = isStageSpeaker(actor, activeEntry);
          const waitingForText = active && activeEntry?.pending && !String(activeEntry?.display_text || "").trim();
          const bubbleContent = waitingForText
            ? thinkingDotsMarkup(`${actor.display_name} is thinking`)
            : escapeHtml(truncateStageBubble(activeEntry?.display_text || ""));
          const roleLabel = actor.role === "judge" ? "Judge" : personaLabel(actor.persona_id);
          return `
            <article
              class="stage-actor ${escapeHtml(actor.role)} ${active ? "is-active-speaker" : ""} ${waitingForText ? "is-thinking" : ""}"
              style="--actor-drop:${index % 2 === 0 ? 0 : 8}px"
              aria-label="${escapeHtml(`${actor.display_name}, ${actor.role}`)}"
            >
              ${
                active
                  ? `
                <div
                  class="pixel-bubble ${escapeHtml(actor.role)} ${activeEntry?.pending ? "is-live" : ""} ${waitingForText ? "is-thinking" : ""}"
                  aria-live="polite"
                >
                  ${bubbleContent}
                </div>
              `
                  : ""
              }
              <div class="pixel-sprite-frame">
                <img
                  class="pixel-sprite"
                  src="${escapeHtml(spriteForActor(actor, actor.spriteIndex))}"
                  alt="${escapeHtml(`${actor.display_name} sprite`)}"
                />
              </div>
              <div class="stage-actor-label">
                <strong>${escapeHtml(actor.display_name)}</strong>
                <span>${escapeHtml(roleLabel)}</span>
              </div>
            </article>
          `;
        })
        .join("")}
    </div>
    <div class="pixel-stage-floor" aria-hidden="true"></div>
  `;
}

function renderQuickActions() {
  const container = $("quick-actions");
  container.innerHTML = "";
  if (!state.activeSession) return;

  if (state.activeSession.status === "awaiting_continue") {
    container.innerHTML = `
      <button id="continue-debate">Continue Round</button>
      <button id="judge-now">Judge Now</button>
      <button id="end-debate">End Debate</button>
    `;
    $("continue-debate").addEventListener("click", () => continueDebate().catch(alert));
    $("judge-now").addEventListener("click", () => judgeNow().catch(alert));
    $("end-debate").addEventListener("click", () => endDebate().catch(alert));
    return;
  }

  if (state.activeSession.status === "awaiting_winner") {
    const buttons = state.activeSession.agents
      .filter((agent) => agent.role === "debater")
      .map(
        (agent) =>
          `<button class="manual-winner" data-agent-id="${escapeHtml(agent.id)}">Pick ${escapeHtml(agent.display_name)}</button>`
      )
      .join("");
    container.innerHTML = `${buttons}<button id="judge-pick-winner">Let Judge Decide</button>`;
    container.querySelectorAll(".manual-winner").forEach((button) => {
      button.addEventListener("click", () => voteWinner(button.dataset.agentId).catch(alert));
    });
    $("judge-pick-winner").addEventListener("click", () => judgePickWinner().catch(alert));
  }
}

function composerMode() {
  if (!state.activeSession) return "start";
  if (state.activeSession.status === "awaiting_continue") return "moderator";
  if (state.activeSession.status === "selecting_personas") return "running";
  if (state.activeSession.status === "running") return "running";
  if (state.activeSession.status === "awaiting_winner") return "winner";
  if (state.activeSession.status === "completed") return "completed";
  if (state.activeSession.status === "failed") return "failed";
  return "running";
}

function renderComposer() {
  const textarea = $("main-composer");
  const suggestionsButton = $("ask-suggestions");
  const submitButton = $("composer-submit");
  const locked = Boolean(state.activeSession);

  $("composer-label").textContent = "Question";
  $("composer-hint").textContent = "Ask a real question. The judge can suggest better prompts before you start.";
  textarea.disabled = locked;
  textarea.placeholder = "Should autonomous coding agents be allowed to negotiate directly with each other?";
  suggestionsButton.hidden = locked;
  suggestionsButton.disabled = state.startInFlight;
  submitButton.textContent = state.startInFlight ? "Starting..." : "Start Debate";
  submitButton.disabled = state.startInFlight || locked;
}

function renderJudgeModelDropdown() {
  const selectedPreset = $("judge-preset").value || state.presets[0]?.id || "";
  const currentValue = $("judge-model").dataset.value || "";
  const nextValue = normalizedModelForPreset(selectedPreset, currentValue);
  $("judge-model").innerHTML = modelOptions(selectedPreset, nextValue);
  $("judge-model").dataset.value = nextValue;
  $("judge-model").value = nextValue;
  $("judge-model").disabled = Boolean(state.activeSession) || !activeModelsForPreset(selectedPreset).length;
  $("judge-preset-note").textContent = presetNoteText(selectedPreset);
}

function renderSeatSetup() {
  const container = $("seat-setup");
  const locked = Boolean(state.activeSession);
  container.innerHTML = state.seatConfigs
    .map((seat, index) => {
      const liveAgent = state.activeSession?.agents.find((agent) => agent.id === seat.id) || null;
      const providerSession = liveAgent?.provider_session || null;
      const providerSessionNotice = providerSession
        ? `
          <div class="provider-session-panel ${escapeHtml(providerSession.status || "")}">
            <strong>Native session</strong>
            <p>Status: ${escapeHtml(providerSession.status || "unknown")}</p>
            <p>Mode: ${escapeHtml(providerSession.mode || "unknown")}</p>
            ${
              providerSession.last_error
                ? `<p class="provider-session-error">${escapeHtml(providerSession.last_error)}</p>`
                : ""
            }
            <button
              data-action="reset-provider-session"
              data-agent-id="${escapeHtml(seat.id)}"
              ${liveAgent?.role !== "debater" ? "disabled" : ""}
            >
              Reset Debater Session
            </button>
          </div>
        `
        : locked
          ? `
          <div class="provider-session-panel empty">
            <strong>Native session</strong>
            <p>No stored provider session for this debater. The next turn already starts fresh.</p>
            <button data-action="reset-provider-session" data-agent-id="${escapeHtml(seat.id)}">Reset Debater Session</button>
          </div>
        `
          : "";
      return `
        <article class="seat-card" data-seat-id="${escapeHtml(seat.id)}">
          <div class="seat-card-head">
            <div class="seat-card-index">Seat ${index + 1}</div>
            <button data-action="remove-seat" ${state.seatConfigs.length <= 2 || locked ? "disabled" : ""}>Remove</button>
          </div>
          <label class="field">
            <span>Name</span>
            <input data-field="display_name" value="${escapeHtml(seat.display_name)}" ${locked ? "disabled" : ""} />
          </label>
          <label class="field">
            <span>Preset</span>
            <select data-field="preset_id" ${locked ? "disabled" : ""}>${presetOptions(seat.preset_id)}</select>
          </label>
          <div class="preset-note">${escapeHtml(presetNoteText(seat.preset_id))}</div>
          <label class="field">
            <span>Model</span>
            <select data-field="model_name" ${locked || !activeModelsForPreset(seat.preset_id).length ? "disabled" : ""}>${modelOptions(seat.preset_id, seat.model_name)}</select>
          </label>
          <label class="field">
            <span>Persona</span>
            <select data-field="persona_choice" ${locked ? "disabled" : ""}>${personaOptions(seat.persona_choice)}</select>
          </label>
          <label class="field">
            <span>Persona Intensity <strong>${escapeHtml(personaIntensityLabel(seat.persona_intensity))}</strong></span>
            <input
              data-field="persona_intensity"
              type="range"
              min="0.5"
              max="1.5"
              step="0.05"
              value="${escapeHtml(clampPersonaIntensity(seat.persona_intensity))}"
              ${locked ? "disabled" : ""}
            />
          </label>
          <details class="advanced-panel">
            <summary>Advanced backend settings</summary>
            <label class="field">
              <span>Command Override (JSON array)</span>
              <textarea data-field="command" rows="2" ${locked ? "disabled" : ""}>${escapeHtml(seat.command)}</textarea>
            </label>
            <label class="field">
              <span>Args Override (JSON array)</span>
              <textarea data-field="args_template" rows="2" ${locked ? "disabled" : ""}>${escapeHtml(seat.args_template)}</textarea>
            </label>
            <label class="field">
              <span>Env Override (JSON object)</span>
              <textarea data-field="env_json" rows="2" ${locked ? "disabled" : ""}>${escapeHtml(seat.env_json)}</textarea>
            </label>
          </details>
          ${providerSessionNotice}
        </article>
      `;
    })
    .join("");
  $("add-seat").disabled = locked || state.seatConfigs.length >= 5;
}

function openSeatPopover(seatId) {
  state.popoverMode = "debater";
  state.popoverSeatId = seatId;
  syncPopoverMode();
  $("popover-title").textContent = "Edit Debater";
  $("seat-popover").hidden = false;
}

function openJudgePopover() {
  state.popoverMode = "judge";
  state.popoverSeatId = null;
  syncPopoverMode();
  $("popover-title").textContent = "Edit Judge";
  $("seat-popover").hidden = false;
}

function closePopover() {
  state.popoverMode = null;
  state.popoverSeatId = null;
  $("seat-popover").hidden = true;
}

function syncPopoverMode() {
  const seatSetup = $("seat-setup");
  const judgeConfig = $("judge-config-container");
  const showingDebater = state.popoverMode === "debater";
  const showingJudge = state.popoverMode === "judge";

  seatSetup.hidden = !showingDebater;
  judgeConfig.hidden = !showingJudge;

  seatSetup.querySelectorAll(".seat-card").forEach((card) => {
    card.hidden = !showingDebater || card.dataset.seatId !== state.popoverSeatId;
  });
}

function renderSettingsDrawer() {
  renderSeatSetup();
  renderJudgeModelDropdown();
  ["judge-name", "judge-preset", "judge-command", "judge-args", "judge-env"].forEach((id) => {
    $(id).disabled = Boolean(state.activeSession);
  });
  syncPopoverMode();
}

function renderArena() {
  renderArenaHeader();
  renderSettingsDrawer();
  renderParticipantStrip();
  renderArenaFeedback();
  renderSuggestionList();
  renderObservability();
  renderArenaPresentation();
  renderThread();
  renderQuickActions();
  renderComposer();
}

function renderPersonas() {
  $("persona-list").innerHTML = state.personas
    .map(
      (persona) => `
        <article class="persona-item">
          <div class="persona-item-head">
            <div class="persona-item-art">
              <img
                class="persona-item-icon"
                src="${escapeHtml(persona.icon_path || "/static/assets/persona-icons/builtin/fallback.svg")}"
                alt="${escapeHtml(`${persona.name} icon`)}"
              />
            </div>
            <div class="persona-item-copy">
              <div class="persona-item-meta-row">
                <h3>${escapeHtml(persona.name)}</h3>
                <span class="persona-origin">${persona.is_builtin ? "Built-in" : "Custom"}</span>
              </div>
              <p>${escapeHtml(persona.philosophy_family)}</p>
              <p>${escapeHtml(persona.style)}</p>
            </div>
          </div>
          <p>Values: ${escapeHtml(persona.core_values.join(", ") || "None")}</p>
          <p>Rules: ${escapeHtml(persona.debate_rules.join(", ") || "None")}</p>
          <div class="persona-item-footer">
            <p>${persona.is_selectable ? "Selectable" : "Hidden"}${persona.icon_style_tag ? ` · ${escapeHtml(persona.icon_style_tag.replaceAll("-", " "))}` : ""}</p>
            ${persona.is_user_editable ? `<button data-persona-id="${escapeHtml(persona.id)}">Edit</button>` : ""}
          </div>
        </article>
      `
    )
    .join("");
  const feedback = $("persona-feedback");
  if (feedback) {
    feedback.className = `thread-feedback${state.personaFeedback ? " is-visible" : ""}${state.personaFeedbackKind ? ` is-${state.personaFeedbackKind}` : ""}`;
    feedback.textContent = state.personaFeedback;
  }
}

function renderPersonaGeneratorModelDropdown() {
  const presetId = $("persona-generator-preset").value || preferredPresetId();
  const currentValue = $("persona-generator-model").dataset.value || "";
  const nextValue = normalizedModelForPreset(presetId, currentValue);
  $("persona-generator-model").innerHTML = modelOptions(presetId, nextValue);
  $("persona-generator-model").dataset.value = nextValue;
  $("persona-generator-model").value = nextValue;
  $("persona-generator-note").textContent = presetNoteText(presetId);
}

function renderSessions() {
  $("session-list").innerHTML = state.sessions.length
    ? state.sessions
        .map(
          (session) => `
            <article class="session-item" data-id="${escapeHtml(session.id)}">
              <h3>${escapeHtml(session.topic)}</h3>
              <p>Status: ${escapeHtml(capitalize(session.status.replaceAll("_", " ")))}</p>
              <p>Updated: ${escapeHtml(new Date(session.updated_at).toLocaleString())}</p>
              <div class="session-item-actions">
                <button data-session-id="${escapeHtml(session.id)}">Open In Arena</button>
                <button data-reuse-id="${escapeHtml(session.id)}">Reuse Settings</button>
                <button class="danger-action" data-delete-id="${escapeHtml(session.id)}">Delete</button>
              </div>
            </article>
          `
        )
        .join("")
    : `<div class="session-item"><p>No saved chambers yet.</p></div>`;
}

function fillPersonaForm(persona) {
  $("persona-id").value = persona.id;
  $("persona-name").value = persona.name;
  $("persona-family").value = persona.philosophy_family;
  $("persona-style").value = persona.style;
  $("persona-values").value = persona.core_values.join(", ");
  $("persona-rules").value = persona.debate_rules.join(", ");
  $("persona-selectable").checked = Boolean(persona.is_selectable);
}

function clearPersonaForm() {
  $("persona-id").value = "";
  $("persona-name").value = "";
  $("persona-family").value = "";
  $("persona-style").value = "";
  $("persona-values").value = "";
  $("persona-rules").value = "";
  $("persona-selectable").checked = true;
}

function getJudgePayload() {
  return {
    display_name: $("judge-name").value.trim(),
    preset_id: $("judge-preset").value,
    model_name: $("judge-model").value,
    command: parseJsonArray($("judge-command").value),
    args_template: parseJsonArray($("judge-args").value),
    env: parseJsonObject($("judge-env").value),
  };
}

function getPersonaGeneratorPayload() {
  return {
    display_name: "Persona Smith",
    preset_id: $("persona-generator-preset").value,
    model_name: $("persona-generator-model").value,
  };
}

function validateSeatConfig() {
  if (state.seatConfigs.length < 2 || state.seatConfigs.length > 5) {
    throw new Error("Debates must have between 2 and 5 debaters.");
  }
  state.seatConfigs.forEach((seat, index) => {
    if (!seat.display_name.trim()) throw new Error(`Seat ${index + 1} needs a name.`);
    if (!seat.preset_id) throw new Error(`Seat ${index + 1} needs a preset.`);
    if (!seat.model_name) throw new Error(`Seat ${index + 1} needs a model.`);
  });
  const judge = getJudgePayload();
  if (!judge.display_name || !judge.preset_id || !judge.model_name) {
    throw new Error("Judge configuration is incomplete.");
  }
}

function applySessionToDraft(session) {
  state.seatConfigs = session.agents
    .filter((agent) => agent.role === "debater")
    .map((agent, index) => ({
      id: agent.id || `seat-${index}`,
      display_name: agent.display_name,
      preset_id: agent.preset_id,
      model_name: normalizedModelForPreset(agent.preset_id, agent.model_name),
      persona_choice: agent.persona_id || "auto",
      command: agent.command?.length ? JSON.stringify(agent.command) : "",
      args_template: agent.args_template?.length ? JSON.stringify(agent.args_template) : "",
      env_json: agent.env && Object.keys(agent.env).length ? JSON.stringify(agent.env) : "",
      persona_intensity: clampPersonaIntensity(agent.persona_intensity),
    }));
  const judge = session.agents.find((agent) => agent.role === "judge");
  if (judge) {
    $("judge-name").value = judge.display_name;
    $("judge-preset").value = judge.preset_id;
    $("judge-model").dataset.value = normalizedModelForPreset(judge.preset_id, judge.model_name);
    $("judge-command").value = judge.command?.length ? JSON.stringify(judge.command) : "";
    $("judge-args").value = judge.args_template?.length ? JSON.stringify(judge.args_template) : "";
    $("judge-env").value = judge.env && Object.keys(judge.env).length ? JSON.stringify(judge.env) : "";
  }
}

function upsertThreadEntry(entry) {
  const existingIndex = state.threadEntries.findIndex((item) => item.id === entry.id);
  if (existingIndex >= 0) {
    state.threadEntries[existingIndex] = entry;
  } else {
    state.threadEntries.push(entry);
  }
}

function pushLocalSystemEntry(text) {
  upsertThreadEntry({
    id: `local-${Date.now()}`,
    kind: "system",
    display_name: "Debate Hall",
    display_text: text,
    round_type: null,
    round_index: null,
    agent_id: null,
    payload: { local: true },
    created_at: new Date().toISOString(),
  });
}

function queueThreadChunk(event) {
  if (!state.pendingThreadEntry || state.pendingThreadEntry.id !== event.entry_id) {
    state.pendingThreadEntry = {
      id: event.entry_id,
      kind: event.kind || "agent",
      display_name: event.display_name || "Speaker",
      display_text: "",
      round_type: event.round_type,
      round_index: event.round_index,
      agent_id: event.agent_id,
      payload: {},
      created_at: new Date().toISOString(),
      pending: true,
    };
  }
  state.pendingThreadEntry.display_text += event.chunk;
  renderArenaPresentation();
  renderThread();
}

async function connectSocket(sessionId) {
  if (state.socket) state.socket.close();
  return new Promise((resolve) => {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/sessions/${sessionId}`);
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    const readyTimer = window.setTimeout(settle, 1200);

    socket.onopen = () => {
      window.clearTimeout(readyTimer);
      settle();
    };
    socket.onerror = () => {
      window.clearTimeout(readyTimer);
      settle();
    };
    socket.onclose = () => {
      window.clearTimeout(readyTimer);
      settle();
    };
    socket.onmessage = async (raw) => {
      const event = JSON.parse(raw.data);
      if (!state.activeSession || state.activeSession.id !== sessionId) return;

      if (event.type === "persona_selected") {
        const agent = state.activeSession.agents.find((item) => item.id === event.agent_id);
        if (agent) agent.persona_id = event.persona_id;
        renderParticipantStrip();
        renderArenaPresentation();
        return;
      }

      if (event.type === "thread_entry_chunk") {
        queueThreadChunk(event);
        return;
      }

      if (event.type === "thread_entry_saved") {
        if (
          state.pendingThreadEntry?.id === event.entry.id ||
          (
            state.pendingThreadEntry?.payload?.predicted &&
            state.pendingThreadEntry.kind === event.entry.kind &&
            state.pendingThreadEntry.agent_id === event.entry.agent_id &&
            state.pendingThreadEntry.round_index === event.entry.round_index
          )
        ) {
          state.pendingThreadEntry = null;
        }
        upsertThreadEntry(event.entry);
        renderArenaPresentation();
        renderThread();
        return;
      }

      if (event.type === "provider_session_state") {
        const agent = state.activeSession.agents.find((item) => item.id === event.agent_id);
        if (agent) agent.provider_session = event.provider_session;
        renderParticipantStrip();
        renderObservability();
        return;
      }

      if (event.type === "trace_event_saved") {
        state.traceEvents.push(event.trace_event);
        renderObservability();
        return;
      }

      if (event.type === "judge_result") {
        state.activeSession.judge_score = event.judge_score;
        renderQuickActions();
        return;
      }

      if (event.type === "status") {
        state.activeSession.status = event.status;
        if (event.status === "selecting_personas") {
          setArenaFeedback("Selecting personas before opening statements...", "success");
        } else if (event.status === "running") {
          setArenaFeedback("Debate started.", "success");
        } else if (event.status === "failed" && event.error) {
          setArenaFeedback(event.error, "error");
        }
        if (
          event.status === "selecting_personas" ||
          event.status === "awaiting_continue" ||
          event.status === "awaiting_winner" ||
          event.status === "completed" ||
          event.status === "failed"
        ) {
          await loadSession(sessionId, { reconnect: false, clearFeedback: false, setArenaView: false });
        }
        renderArena();
        if (
          event.status === "awaiting_continue" ||
          event.status === "awaiting_winner" ||
          event.status === "completed" ||
          event.status === "failed"
        ) {
          await refreshSessions();
        }
      }
    };
    state.socket = socket;
  });
}

async function loadSession(sessionId, options = {}) {
  const { reconnect = true, clearFeedback = true, setArenaView = true } = options;
  const session = await fetchJson(`/api/sessions/${sessionId}`);
  state.activeSessionId = sessionId;
  state.activeSession = session;
  state.questionSuggestions = [];
  if (clearFeedback) setArenaFeedback();
  applySessionToDraft(session);
  syncThreadEntriesFromSession(session);
  renderArena();
  if (reconnect) {
    await connectSocket(sessionId);
  }
  if (setArenaView) {
    setView("arena");
  }
  return session;
}

async function syncSessionUntil(sessionId, predicate, options = {}) {
  const {
    timeoutMs = SESSION_SYNC_TIMEOUT_MS,
    intervalMs = SESSION_SYNC_INTERVAL_MS,
    reconnect = false,
    clearFeedback = false,
    setArenaView = false,
  } = options;

  const deadline = Date.now() + timeoutMs;
  let latestSession = null;
  while (Date.now() < deadline) {
    latestSession = await loadSession(sessionId, { reconnect, clearFeedback, setArenaView });
    if (predicate(latestSession)) {
      return latestSession;
    }
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
  }
  return latestSession;
}

async function askJudgeSuggestions() {
  validateSeatConfig();
  const payload = {
    question: $("main-composer").value.trim(),
    judge: getJudgePayload(),
  };
  setArenaFeedback("The judge is thinking about suggestions...", "success");
  renderArena();
  const result = await fetchJson("/api/questions/suggestions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.questionSuggestions = result.suggestions || [];
  setArenaFeedback("The judge suggested three debate questions.", "success");
  renderArena();
}

async function validateQuestionOnly(question) {
  const payload = {
    question,
    judge: getJudgePayload(),
  };
  const result = await fetchJson("/api/questions/validate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.questionSuggestions = result.suggestions || [];
  setArenaFeedback(result.reason, result.accepted ? "success" : "error");
  renderArena();
  return result.accepted;
}

async function startDebate() {
  if (state.startInFlight) return;
  state.startInFlight = true;
  const previousView = state.activeView;
  const hadActiveSession = Boolean(state.activeSessionId);
  setArenaFeedback("Opening the chamber...", "success");
  if (!hadActiveSession) {
    state.threadEntries = [];
    state.traceEvents = [];
    state.pendingThreadEntry = null;
    pushLocalSystemEntry("Opening the chamber. The council is taking the stage...");
  }
  setView("arena");
  renderArena();
  try {
    validateSeatConfig();
    const topic = $("main-composer").value.trim();
    if (!topic || topic.split(/\s+/).length < 3) {
      setArenaFeedback("Please write a question with at least a few words.", "error");
      renderArena();
      return;
    }

    const payload = {
      topic,
      agents: state.seatConfigs.map((seat, index) => ({
        display_name: seat.display_name.trim() || `Debater ${index + 1}`,
        preset_id: seat.preset_id,
        model_name: seat.model_name,
        persona_id: seat.persona_choice === "auto" ? null : seat.persona_choice,
        persona_mode: seat.persona_choice === "auto" ? "auto" : "manual",
        persona_intensity: clampPersonaIntensity(seat.persona_intensity),
        command: parseJsonArray(seat.command),
        args_template: parseJsonArray(seat.args_template),
        env: parseJsonObject(seat.env_json),
      })),
      judge: getJudgePayload(),
    };

    const session = await fetchJson("/api/sessions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await loadSession(session.id);
    setArenaFeedback("Summoning the debaters into the hall...", "success");
    renderArena();
    await fetchJson(`/api/sessions/${session.id}/start`, { method: "POST" });
    setArenaFeedback("Waiting for opening statements...", "success");
    renderArena();
    const liveSession = await syncSessionUntil(
      session.id,
      (currentSession) =>
        currentSession.status !== "draft" ||
        (currentSession.thread_entries?.length || 0) > 0 ||
        (currentSession.messages?.length || 0) > 0,
      { reconnect: false, clearFeedback: false, setArenaView: false }
    );
    if (liveSession?.status === "selecting_personas") {
      setArenaFeedback("Selecting personas before opening statements...", "success");
    } else if (liveSession?.status === "failed") {
      setArenaFeedback("Debate failed to start.", "error");
    } else {
      setArenaFeedback("Debate started.", "success");
    }
    setView("arena");
    renderArena();
    await refreshSessions();
  } catch (error) {
    console.error(error);
    if (!state.activeSessionId) {
      state.threadEntries = [];
      state.traceEvents = [];
      state.pendingThreadEntry = null;
      setView(previousView);
    }
    setArenaFeedback(error.message || "Start Debate failed.", "error");
    renderArena();
  } finally {
    state.startInFlight = false;
    renderArena();
  }
}

async function continueDebate() {
  const currentEntries = state.threadEntries.length;
  setArenaFeedback("Continuing the debate...", "success");
  renderArena();
  await fetchJson(`/api/sessions/${state.activeSession.id}/continue`, { method: "POST" });
  await syncSessionUntil(
    state.activeSession.id,
    (session) =>
      session.status !== "awaiting_continue" ||
      (session.thread_entries?.length || 0) > currentEntries,
    { reconnect: false, clearFeedback: false, setArenaView: false }
  );
  renderArena();
}

async function sendModeratorNote() {
  const input = $("arena-moderator-input");
  const text = input.value.trim();
  if (!text) throw new Error("Enter a moderator note or use Continue Round.");
  const currentEntries = state.threadEntries.length;
  await fetchJson(`/api/sessions/${state.activeSession.id}/moderator-note`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  input.value = "";
  setArenaFeedback("Moderator note sent.", "success");
  await syncSessionUntil(
    state.activeSession.id,
    (session) =>
      session.status !== "awaiting_continue" ||
      (session.thread_entries?.length || 0) > currentEntries,
    { reconnect: false, clearFeedback: false, setArenaView: false }
  );
  renderArena();
}

async function endDebate() {
  const session = await fetchJson(`/api/sessions/${state.activeSession.id}/end`, { method: "POST" });
  state.activeSession = session;
  syncThreadEntriesFromSession(session);
  renderArena();
  await refreshSessions();
}

async function voteWinner(agentId) {
  await fetchJson(`/api/sessions/${state.activeSession.id}/vote`, {
    method: "POST",
    body: JSON.stringify({ winner_agent_id: agentId }),
  });
  await loadSession(state.activeSession.id);
  await refreshSessions();
}

async function judgePickWinner() {
  pushLocalSystemEntry("The judge is deliberating...");
  state.pendingThreadEntry = {
    id: "judge-thinking",
    kind: "judge",
    display_name: currentJudge().display_name,
    display_text: "",
    pending: true,
    round_type: null,
    round_index: null,
    agent_id: null,
    payload: {},
    created_at: new Date().toISOString(),
  };
  renderArenaPresentation();
  renderThread();
  const session = await fetchJson(`/api/sessions/${state.activeSession.id}/judge-decision`, {
    method: "POST",
    body: JSON.stringify({ judge: getJudgePayload() }),
  });
  state.activeSession = session;
  syncThreadEntriesFromSession(session);
  renderArena();
  await refreshSessions();
}

async function judgeNow() {
  await endDebate();
  await judgePickWinner();
}

async function resetDebaterSession(agentId) {
  const session = await fetchJson(`/api/sessions/${state.activeSession.id}/agents/${agentId}/reset-session`, {
    method: "POST",
  });
  state.activeSession = session;
  syncThreadEntriesFromSession(session);
  const agent = session.agents.find((item) => item.id === agentId);
  if (agent) {
    setArenaFeedback(
      session.status === "failed"
        ? `${agent.display_name} was reset. Use Reuse Settings from Sessions to start a fresh chamber.`
        : `${agent.display_name} was reset. The next turn will start with a fresh provider session.`,
      "success"
    );
  }
  renderArena();
}

async function savePersona() {
  const personaId = $("persona-id").value;
  const payload = {
    name: $("persona-name").value.trim(),
    philosophy_family: $("persona-family").value.trim(),
    style: $("persona-style").value.trim(),
    core_values: parseCsv($("persona-values").value),
    debate_rules: parseCsv($("persona-rules").value),
    is_selectable: $("persona-selectable").checked,
  };
  setPersonaFeedback(personaId ? "Saving persona..." : "Forging persona icon and saving...", "success");
  renderPersonas();
  let savedPersona;
  if (personaId) {
    savedPersona = await fetchJson(`/api/personas/${personaId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  } else {
    savedPersona = await fetchJson("/api/personas", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }
  clearPersonaForm();
  await loadPersonas();
  setPersonaFeedback(
    savedPersona?.icon_style_tag === "fallback-pixel-creature"
      ? "Persona saved, but icon generation fell back to the default crest."
      : "Persona saved with its pixel icon.",
    savedPersona?.icon_style_tag === "fallback-pixel-creature" ? "error" : "success"
  );
  renderPersonas();
  renderSettingsDrawer();
}

async function generatePersonaDraft() {
  const description = $("persona-description").value.trim();
  if (!description) throw new Error("Describe the persona you want first.");
  setPersonaFeedback("Generating persona draft...", "success");
  renderPersonas();
  const payload = {
    description,
    name_hint: $("persona-name-hint").value.trim() || null,
    philosophy_family_hint: $("persona-family-hint").value.trim() || null,
    generator: getPersonaGeneratorPayload(),
  };
  const persona = await fetchJson("/api/personas/generate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  fillPersonaForm(persona);
  setPersonaFeedback("Generated a persona draft. Review and save it to add it to the library.", "success");
  renderPersonas();
}

async function loadPersonas() {
  state.personas = await fetchJson("/api/personas");
  renderPersonas();
}

async function refreshSessions() {
  state.sessions = await fetchJson("/api/sessions");
  renderSessions();
}

async function reuseSession(sessionId) {
  const session = await fetchJson(`/api/sessions/${sessionId}`);
  state.activeSessionId = null;
  state.activeSession = null;
  state.threadEntries = [];
  state.traceEvents = [];
  applySessionToDraft(session);
  $("main-composer").value = session.topic;
  persistDraft();
  renderArena();
  setView("setup");
}

async function deleteSession(sessionId) {
  if (!confirm("Delete this chamber permanently?")) return;
  await fetchJson(`/api/sessions/${sessionId}`, { method: "DELETE" });
  if (state.activeSessionId === sessionId) {
    state.activeSessionId = null;
    state.activeSession = null;
    state.threadEntries = [];
    state.traceEvents = [];
    renderArena();
  }
  await refreshSessions();
}

async function handleComposerSubmit() {
  await startDebate();
}

function registerViewListeners() {
  document.querySelectorAll(".nav-tab").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
}

function registerSeatListeners() {
  $("seat-setup").addEventListener("input", (event) => {
    if (state.activeSession) return;
    const field = event.target.dataset.field;
    const card = event.target.closest("[data-seat-id]");
    if (!field || !card) return;
    const seat = state.seatConfigs.find((item) => item.id === card.dataset.seatId);
    if (!seat) return;
    seat[field] = field === "persona_intensity" ? clampPersonaIntensity(event.target.value) : event.target.value;
    renderParticipantStrip();
    persistDraft();
    if (field === "persona_intensity") renderSettingsDrawer();
  });

  $("seat-setup").addEventListener("change", (event) => {
    if (state.activeSession) return;
    const field = event.target.dataset.field;
    const card = event.target.closest("[data-seat-id]");
    if (!field || !card) return;
    const seat = state.seatConfigs.find((item) => item.id === card.dataset.seatId);
    if (!seat) return;
    seat[field] = field === "persona_intensity" ? clampPersonaIntensity(event.target.value) : event.target.value;
    if (field === "preset_id") {
      seat.model_name = defaultModelForPreset(seat.preset_id);
      persistDraft();
      renderArena();
      return;
    }
    if (field === "model_name") {
      seat.model_name = normalizedModelForPreset(seat.preset_id, seat.model_name);
      persistDraft();
      renderArena();
      return;
    }
    renderParticipantStrip();
    persistDraft();
  });

  $("seat-setup").addEventListener("click", (event) => {
    if (event.target.dataset.action === "reset-provider-session") {
      const agentId = event.target.dataset.agentId;
      if (!state.activeSession || !agentId) return;
      resetDebaterSession(agentId).catch(alert);
      return;
    }
    if (state.activeSession) return;
    if (event.target.dataset.action !== "remove-seat") return;
    const card = event.target.closest("[data-seat-id]");
    if (!card || state.seatConfigs.length <= 2) return;
    state.seatConfigs = state.seatConfigs.filter((item) => item.id !== card.dataset.seatId);
    closePopover();
    persistDraft();
    renderArena();
  });
}

function registerArenaListeners() {
  $("add-seat").addEventListener("click", () => {
    if (state.activeSession || state.seatConfigs.length >= 5) return;
    state.seatConfigs.push(buildSeatConfig(state.seatConfigs.length));
    persistDraft();
    renderArena();
  });

  $("ask-suggestions").addEventListener("click", () => askJudgeSuggestions().catch((error) => {
    console.error(error);
    setArenaFeedback(error.message || "The judge could not provide suggestions.", "error");
    renderArena();
  }));

  $("composer-submit").addEventListener("click", () => handleComposerSubmit().catch(alert));

  $("export-session").addEventListener("click", () => {
    if (!state.activeSessionId) return;
    window.open(`/api/sessions/${state.activeSessionId}/export`, "_blank");
  });
  $("export-trace").addEventListener("click", () => {
    if (!state.activeSessionId) return;
    window.open(`/api/sessions/${state.activeSessionId}/trace/export`, "_blank");
  });

  $("arena-moderator-submit").addEventListener("click", () => sendModeratorNote().catch(alert));

  $("suggestion-list").addEventListener("click", (event) => {
    const suggestion = event.target.dataset.suggestion;
    if (!suggestion) return;
    $("main-composer").value = suggestion;
    setArenaFeedback("Suggestion applied.", "success");
    renderArena();
  });

  $("main-composer").addEventListener("input", () => {
    if (!state.activeSession) {
      setArenaFeedback();
      persistDraft();
      renderArenaHeader();
      renderArenaFeedback();
    }
  });

  $("arena-presentation-toggle").addEventListener("click", (event) => {
    const button = event.target.closest("[data-presentation-mode]");
    if (!button) return;
    state.presentationMode = button.dataset.presentationMode === "pixel-stage" ? "pixel-stage" : "classic";
    persistArenaPresentationMode();
    renderArenaPresentation();
  });
}

function registerJudgeListeners() {
  $("judge-preset").addEventListener("change", () => {
    if (state.activeSession) return;
    $("judge-model").dataset.value = defaultModelForPreset($("judge-preset").value);
    persistDraft();
    renderJudgeModelDropdown();
    renderParticipantStrip();
  });
  $("judge-model").addEventListener("change", () => {
    $("judge-model").dataset.value = $("judge-model").value;
    persistDraft();
    renderParticipantStrip();
  });
  ["judge-name", "judge-command", "judge-args"].forEach((id) => {
    $(id).addEventListener("input", () => {
      persistDraft();
      renderParticipantStrip();
    });
  });
  $("judge-env").addEventListener("input", () => {
    persistDraft();
    renderParticipantStrip();
  });
}

function registerPersonaListeners() {
  $("save-persona").addEventListener("click", () => savePersona().catch(alert));
  $("generate-persona").addEventListener("click", () => generatePersonaDraft().catch((error) => {
    console.error(error);
    setPersonaFeedback(error.message || "Persona generation failed.", "error");
    renderPersonas();
  }));
  $("persona-generator-preset").addEventListener("change", () => {
    $("persona-generator-model").dataset.value = defaultModelForPreset($("persona-generator-preset").value);
    renderPersonaGeneratorModelDropdown();
  });
  $("persona-generator-model").addEventListener("change", () => {
    $("persona-generator-model").dataset.value = $("persona-generator-model").value;
  });
  $("persona-list").addEventListener("click", (event) => {
    const personaId = event.target.dataset.personaId;
    if (!personaId) return;
    const persona = state.personas.find((item) => item.id === personaId);
    if (persona) fillPersonaForm(persona);
  });
}

function registerPopoverListeners() {
  $("participant-strip").addEventListener("click", (event) => {
    const chip = event.target.closest(".participant-chip");
    if (!chip) return;
    if (chip.dataset.role === "judge") {
      openJudgePopover();
    } else if (chip.dataset.seatId) {
      openSeatPopover(chip.dataset.seatId);
    }
  });

  $("popover-close").addEventListener("click", () => closePopover());

  document.addEventListener("click", (event) => {
    const popover = $("seat-popover");
    if (popover.hidden) return;
    const content = popover.querySelector(".seat-popover-content");
    if (!content.contains(event.target) && !$("participant-strip").contains(event.target) && event.target !== $("add-seat")) {
      closePopover();
    }
  });
}

function registerSessionListeners() {
  $("refresh-sessions").addEventListener("click", () => refreshSessions().catch(alert));
  $("session-list").addEventListener("click", (event) => {
    const sessionId = event.target.dataset.sessionId;
    if (sessionId) {
      loadSession(sessionId).catch(alert);
      return;
    }
    const reuseId = event.target.dataset.reuseId;
    if (reuseId) {
      reuseSession(reuseId).catch(alert);
      return;
    }
    const deleteId = event.target.dataset.deleteId;
    if (deleteId) {
      deleteSession(deleteId).catch(alert);
    }
  });
}

async function boot() {
  registerViewListeners();
  registerSeatListeners();
  registerArenaListeners();
  registerJudgeListeners();
  registerPopoverListeners();
  registerPersonaListeners();
  registerSessionListeners();

  state.presets = await fetchJson("/api/presets");
  const defaultJudgePresetId = preferredPresetId();
  $("judge-preset").innerHTML = presetOptions(defaultJudgePresetId);
  $("judge-preset").value = defaultJudgePresetId;
  $("persona-generator-preset").innerHTML = presetOptions(defaultJudgePresetId);
  $("persona-generator-preset").value = defaultJudgePresetId;

  await loadPersonas();
  state.presentationMode = readArenaPresentationMode();
  initializeSeatConfigs();
  restoreDraft();
  $("persona-generator-model").dataset.value = defaultModelForPreset($("persona-generator-preset").value);
  renderPersonaGeneratorModelDropdown();
  renderArena();
  await refreshSessions();
}

boot().catch((error) => {
  console.error(error);
  alert(error.message);
});

(function initThemeToggle() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  function updateLabel() {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    btn.textContent = isDark ? "Light Mode" : "Dark Mode";
  }
  updateLabel();
  btn.addEventListener("click", () => {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    if (isDark) {
      document.documentElement.removeAttribute("data-theme");
      localStorage.removeItem("theme");
    } else {
      document.documentElement.setAttribute("data-theme", "dark");
      localStorage.setItem("theme", "dark");
    }
    updateLabel();
  });
})();
