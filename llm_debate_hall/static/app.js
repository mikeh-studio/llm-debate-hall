const SAMPLE_NAMES = ["Athena", "Burke", "Cassius", "Diotima", "Erasmus"];
const SAMPLE_PRESETS = ["openai", "anthropic", "openai", "anthropic", "ollama"];
const SEAT_ANGLES = {
  2: [205, 335],
  3: [165, 270, 15],
  4: [150, 225, 315, 30],
  5: [145, 205, 270, 335, 25],
};

const state = {
  presets: [],
  personas: [],
  sessions: [],
  activeView: "arena",
  activeSessionId: null,
  activeSession: null,
  socket: null,
  seatConfigs: [],
  nextSeatId: 0,
  setupLocked: false,
  questionStageOpen: false,
  questionFeedback: "",
  questionFeedbackKind: "",
  questionSuggestions: [],
  combatLog: [],
  activeSpeaker: null,
  typingInterval: null,
  startInFlight: false,
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

function personaOptions(selectedValue = "auto") {
  const options = [`<option value="auto" ${selectedValue === "auto" ? "selected" : ""}>Auto pick</option>`];
  state.personas
    .filter((persona) => persona.is_selectable)
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

function modelsForPreset(presetId) {
  return presetById(presetId)?.models || [];
}

function verifiedPresetIds() {
  return state.presets
    .filter((preset) => preset.is_available !== false && !preset.requires_command_override)
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
  return verified[index % verified.length] || state.presets[0]?.id || "";
}

function presetNoteText(presetId) {
  const preset = presetById(presetId);
  if (!preset) return "";
  if (preset.is_available === false) {
    return `${preset.description} This CLI is not installed locally on this machine.`;
  }
  if ((preset.missing_env_vars || []).length) {
    return `${preset.description} Missing env: ${preset.missing_env_vars.join(", ")}. Add them below as a JSON object or start the server with those variables set.`;
  }
  return preset.requires_command_override
    ? `${preset.description} Add a command override before starting.`
    : preset.description;
}

function modelOptions(presetId, selectedModel = "") {
  const models = modelsForPreset(presetId);
  return models
    .map(
      (model) =>
        `<option value="${escapeHtml(model)}" ${model === selectedModel ? "selected" : ""}>${escapeHtml(model)}</option>`
    )
    .join("");
}

function defaultModelForPreset(presetId) {
  return modelsForPreset(presetId)[0] || "";
}

function buildSeatConfig(index = 0) {
  const presetId = defaultSeatPresetId(index);
  return {
    id: `seat-${state.nextSeatId++}`,
    display_name: SAMPLE_NAMES[index] || `Debater ${index + 1}`,
    preset_id: presetId,
    model_name: defaultModelForPreset(presetId),
    persona_choice: "auto",
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
  if (!personaId) return "Auto pick";
  return state.personas.find((persona) => persona.id === personaId)?.name || personaId.replaceAll("_", " ");
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

function isArenaLive() {
  return state.setupLocked || Boolean(state.activeSession);
}

function shouldShowQuestionStage() {
  return state.questionStageOpen && state.setupLocked && (!state.activeSession || state.activeSession.status === "draft");
}

function setQuestionFeedback(message = "", kind = "") {
  state.questionFeedback = message;
  state.questionFeedbackKind = kind;
}

function renderSetupLayout() {
  const layout = $("view-arena").querySelector(".arena-layout");
  layout.classList.toggle("setup-screen", !isArenaLive());
  layout.classList.toggle("arena-live", isArenaLive());
}

function renderLockedSummary() {
  const summary = $("locked-summary");
  summary.classList.toggle("hidden", !state.setupLocked);
  if (!state.setupLocked) return;

  const judge = currentJudge();
  const debaters = currentDebaters()
    .map((seat) => `${seat.display_name} · ${personaLabel(seat.persona_id)}`)
    .join(" | ");
  $("locked-summary-body").innerHTML = `
    <div class="locked-summary-title">Chamber locked</div>
    <div>${escapeHtml(currentDebaters().length)} seats · ${escapeHtml(judge.display_name)} presiding</div>
    <div>${escapeHtml(debaters)}</div>
  `;
  $("edit-seats").classList.toggle("hidden", Boolean(state.activeSession));
}

function renderQuestionStage() {
  $("question-stage").classList.toggle("hidden", !shouldShowQuestionStage());
  const feedback = $("question-feedback");
  feedback.textContent = state.questionFeedback || "";
  feedback.className = `question-feedback ${state.questionFeedbackKind ? `is-${state.questionFeedbackKind}` : ""}`;
  $("suggestion-list").innerHTML = state.questionSuggestions
    .map(
      (suggestion) =>
        `<button class="suggestion-chip" data-suggestion="${escapeHtml(suggestion)}">${escapeHtml(suggestion)}</button>`
    )
    .join("");
  $("ask-suggestions").disabled = state.startInFlight;
  $("start-debate").disabled = state.startInFlight;
  $("start-debate").textContent = state.startInFlight ? "Starting..." : "Start Debate";
}

function renderJudgeModelDropdown() {
  const selectedPreset = $("judge-preset").value || state.presets[0]?.id || "";
  const currentValue = $("judge-model").dataset.value || "";
  const nextValue = modelsForPreset(selectedPreset).includes(currentValue)
    ? currentValue
    : defaultModelForPreset(selectedPreset);
  $("judge-model").innerHTML = modelOptions(selectedPreset, nextValue);
  $("judge-model").dataset.value = nextValue;
  $("judge-preset-note").textContent = presetNoteText(selectedPreset);
}

function renderSeatSetup() {
  const container = $("seat-setup");
  container.innerHTML = state.seatConfigs
    .map(
      (seat, index) => `
        <article class="seat-card" data-seat-id="${escapeHtml(seat.id)}">
          <div class="seat-card-head">
            <div class="seat-card-index">Seat ${index + 1}</div>
            <button data-action="remove-seat" ${state.seatConfigs.length <= 2 ? "disabled" : ""}>Remove</button>
          </div>
          <label class="field">
            <span>Name</span>
            <input data-field="display_name" value="${escapeHtml(seat.display_name)}" />
          </label>
          <label class="field">
            <span>Preset</span>
            <select data-field="preset_id">${presetOptions(seat.preset_id)}</select>
          </label>
          <div class="preset-note">${escapeHtml(presetNoteText(seat.preset_id))}</div>
          <label class="field">
            <span>Model</span>
            <select data-field="model_name">${modelOptions(seat.preset_id, seat.model_name)}</select>
          </label>
          <label class="field">
            <span>Persona</span>
            <select data-field="persona_choice">${personaOptions(seat.persona_choice)}</select>
          </label>
          <label class="field">
            <span>Command Override (JSON array)</span>
            <textarea data-field="command" rows="2" placeholder='["openai"]'>${escapeHtml(seat.command)}</textarea>
          </label>
          <label class="field">
            <span>Args Override (JSON array)</span>
            <textarea data-field="args_template" rows="2" placeholder='["--model","{model}"]'>${escapeHtml(seat.args_template)}</textarea>
          </label>
          <label class="field">
            <span>Env Override (JSON object)</span>
            <textarea data-field="env_json" rows="2" placeholder='{"OPENAI_API_KEY":"..."}'>${escapeHtml(seat.env_json)}</textarea>
          </label>
        </article>
      `
    )
    .join("");

  $("add-seat").disabled = state.seatConfigs.length >= 5;
  renderArena();
}

function arenaSeatPosition(count, index) {
  const angle = SEAT_ANGLES[count]?.[index] ?? (360 / count) * index;
  const radians = (angle * Math.PI) / 180;
  const radius = count >= 4 ? 43 : 41;
  const x = 50 + Math.cos(radians) * radius;
  const y = 50 + Math.sin(radians) * radius;
  return { x: `${x}%`, y: `${y}%` };
}

function renderActiveSessionPill() {
  const pill = $("active-session-pill");
  if (!state.activeSession) {
    pill.textContent = state.setupLocked ? "Question step" : "Setup phase";
    return;
  }
  pill.textContent = `${state.activeSession.topic} · ${state.activeSession.status}`;
}

function renderJudgeThrone() {
  const judge = currentJudge();
  $("judge-throne").innerHTML = `
    <div>
      <div class="panel-kicker">Judge Throne</div>
      <div class="seat-name">${escapeHtml(judge.display_name)}</div>
      <div class="seat-model">${escapeHtml(judge.preset_id)} · ${escapeHtml(judge.model_name)}</div>
    </div>
  `;
}

function renderDialoguePanel() {
  const card = $("dialogue-card");
  if (!state.activeSpeaker) {
    card.innerHTML = `
      <div class="dialogue-speaker">No speaker yet</div>
      <div class="dialogue-round">Waiting for the chamber to open.</div>
      <p class="dialogue-text">The current paragraph will appear here one character at a time.</p>
    `;
    return;
  }
  card.innerHTML = `
    <div class="dialogue-speaker">${escapeHtml(state.activeSpeaker.agentName)}</div>
    <div class="dialogue-round">${escapeHtml(state.activeSpeaker.roundType)}</div>
    <p class="dialogue-text">${escapeHtml(state.activeSpeaker.displayedText || "Speaking...")}</p>
  `;
}

function renderArenaSeats() {
  const seats = currentDebaters();
  $("arena-seats").innerHTML = seats
    .map((seat, index) => {
      const position = arenaSeatPosition(seats.length, index);
      const isActive = state.activeSpeaker?.agentId === seat.id;
      const personaText = personaLabel(seat.persona_id);
      const providerMode = seat.provider_session?.mode === "persistent"
        ? "Persistent thread"
        : seat.provider_session?.mode === "replay_fallback"
          ? "Replay fallback"
          : state.activeSession
            ? "Stateless"
            : "Seat ready";
      return `
        <article class="arena-seat ${isActive ? "active streaming" : ""}" style="left: ${position.x}; top: ${position.y};">
          <div class="seat-order">Seat ${index + 1}</div>
          <div class="seat-name">${escapeHtml(seat.display_name)}</div>
          <div class="seat-model">${escapeHtml(seat.preset_id)} · ${escapeHtml(seat.model_name)}</div>
          <div class="seat-persona">${escapeHtml(personaText)}</div>
          <div class="seat-status">${escapeHtml(isActive ? "Speaking now" : providerMode)}</div>
        </article>
      `;
    })
    .join("");
}

function renderArenaMeta() {
  const debaters = currentDebaters();
  const topic = state.activeSession?.topic || $("debate-question").value.trim() || "Chamber preview";
  $("arena-topic").textContent = topic;
  $("arena-status").textContent = state.activeSession?.status || (shouldShowQuestionStage() ? "Question" : "Setup");
  $("arena-seat-count").textContent = `${debaters.length} seats`;
  $("table-subtitle").textContent = state.activeSession
    ? "One paragraph at a time. Two reply rounds each before the chamber pauses."
    : state.setupLocked
      ? "Question stage is open. Ask the judge for suggestions or start the debate."
      : "Lock the seats, then bring the debate question to the chamber.";
  renderActiveSessionPill();
}

function renderArena() {
  renderSetupLayout();
  renderLockedSummary();
  renderArenaMeta();
  renderJudgeThrone();
  renderDialoguePanel();
  renderArenaSeats();
}

function judgeWinnerLabel(session) {
  if (!session?.judge_score) return "Pending";
  return (
    session.agents.find((agent) => agent.id === session.judge_score.winner_agent_id)?.display_name ||
    session.judge_score.winner_agent_id
  );
}

function humanWinnerLabel(session) {
  if (!session?.winner_human) return "Pending";
  return session.agents.find((agent) => agent.id === session.winner_human)?.display_name || session.winner_human;
}

function renderSessionMeta() {
  const meta = $("session-meta");
  if (!state.activeSession) {
    meta.textContent = "No active session.";
    return;
  }
  meta.textContent = `${state.activeSession.topic} | Status: ${state.activeSession.status} | Judge: ${judgeWinnerLabel(
    state.activeSession
  )} | Manual winner: ${humanWinnerLabel(state.activeSession)}`;
}

function buildCombatLogFromSession(session) {
  const entries = [];
  const debaters = session.agents.filter((agent) => agent.role === "debater");
  debaters.forEach((agent) => {
    if (agent.persona_id) {
      entries.push({
        type: "persona_selected",
        title: `${agent.display_name} persona`,
        body: personaLabel(agent.persona_id),
      });
    }
  });

  session.rounds.forEach((round) => {
    entries.push({
      type: "round_started",
      title: `Round ${round.round_index}`,
      body: round.round_type,
    });
    session.messages
      .filter((message) => message.round_index === round.round_index)
      .forEach((message) => {
        entries.push({
          type: "message_saved",
          title: `${message.agent_name} · ${message.round_type}`,
          body: message.display_text,
        });
      });
  });

  if (session.judge_score) {
    entries.push({
      type: "judge_result",
      title: "Judge decision",
      body: session.judge_score.rationale,
    });
  }
  return entries;
}

function renderCombatLog() {
  const log = $("combat-log");
  if (!state.combatLog.length) {
    log.innerHTML = `<div class="combat-entry"><div class="combat-entry-body">The feed is empty.</div></div>`;
    return;
  }
  log.innerHTML = state.combatLog
    .map(
      (entry) => `
        <article class="combat-entry event-${escapeHtml(entry.type)}">
          <div class="combat-entry-head">
            <span>${escapeHtml(entry.title)}</span>
            <span>${escapeHtml(entry.meta || "")}</span>
          </div>
          <div class="combat-entry-body">${escapeHtml(entry.body)}</div>
        </article>
      `
    )
    .join("");
}

function renderCycleControls() {
  const container = $("cycle-controls");
  container.innerHTML = "";
  if (!state.activeSession) return;
  if (state.activeSession.status === "awaiting_continue") {
    container.innerHTML = `
      <button id="continue-debate">Keep Debating</button>
      <button id="end-debate">End Debate</button>
    `;
    $("continue-debate").addEventListener("click", () => continueDebate().catch(alert));
    $("end-debate").addEventListener("click", () => endDebate().catch(alert));
  } else if (state.activeSession.status === "running") {
    container.innerHTML = `<div class="step-note">The chamber is live. Each speaker gets one paragraph per turn.</div>`;
  } else if (state.activeSession.status === "failed") {
    container.innerHTML =
      '<div class="question-feedback is-error">The chamber failed to start or continue. Check the session feed for the reported error.</div>';
  }
}

function renderWinnerControls() {
  const container = $("winner-controls");
  container.innerHTML = "";
  if (!state.activeSession) return;
  if (state.activeSession.status === "awaiting_winner") {
    const manualButtons = state.activeSession.agents
      .filter((agent) => agent.role === "debater")
      .map(
        (agent) =>
          `<button class="manual-winner" data-agent-id="${escapeHtml(agent.id)}">Pick ${escapeHtml(agent.display_name)}</button>`
      )
      .join("");
    container.innerHTML = `
      <div class="step-note">The debate has ended. Pick a winner yourself or ask the judge to decide now.</div>
      <div class="cycle-controls">${manualButtons}</div>
      <button id="judge-pick-winner">Let Judge Pick</button>
    `;
    container.querySelectorAll(".manual-winner").forEach((button) => {
      button.addEventListener("click", () => voteWinner(button.dataset.agentId).catch(alert));
    });
    $("judge-pick-winner").addEventListener("click", () => judgePickWinner().catch(alert));
  }
}

function renderPersonas() {
  $("persona-list").innerHTML = state.personas
    .map(
      (persona) => `
        <article class="persona-item">
          <h3>${escapeHtml(persona.name)}</h3>
          <p>${escapeHtml(persona.philosophy_family)}</p>
          <p>${escapeHtml(persona.style)}</p>
          <p>Values: ${escapeHtml(persona.core_values.join(", ") || "None")}</p>
          <p>Rules: ${escapeHtml(persona.debate_rules.join(", ") || "None")}</p>
          <p>${persona.is_builtin ? "Built-in" : "Custom"} · ${persona.is_selectable ? "Selectable" : "Hidden"}</p>
          ${persona.is_user_editable ? `<button data-persona-id="${escapeHtml(persona.id)}">Edit</button>` : ""}
        </article>
      `
    )
    .join("");
}

function renderSessions() {
  $("session-list").innerHTML = state.sessions.length
    ? state.sessions
        .map(
          (session) => `
            <article class="session-item">
              <h3>${escapeHtml(session.topic)}</h3>
              <p>Status: ${escapeHtml(session.status)}</p>
              <p>Updated: ${escapeHtml(new Date(session.updated_at).toLocaleString())}</p>
              <button data-session-id="${escapeHtml(session.id)}">Open In Arena</button>
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
  $("debate-question").value = session.topic;
  state.setupLocked = true;
  state.questionStageOpen = session.status === "draft";
  state.seatConfigs = session.agents
    .filter((agent) => agent.role === "debater")
    .map((agent, index) => ({
      id: agent.id || `seat-${index}`,
      display_name: agent.display_name,
      preset_id: agent.preset_id,
      model_name: agent.model_name,
      persona_choice: agent.persona_id || "auto",
      command: agent.command?.length ? JSON.stringify(agent.command) : "",
      args_template: agent.args_template?.length ? JSON.stringify(agent.args_template) : "",
      env_json: agent.env && Object.keys(agent.env).length ? JSON.stringify(agent.env) : "",
    }));
  const judge = session.agents.find((agent) => agent.role === "judge");
  if (judge) {
    $("judge-name").value = judge.display_name;
    $("judge-preset").value = judge.preset_id;
    $("judge-model").dataset.value = judge.model_name;
    renderJudgeModelDropdown();
    $("judge-model").value = judge.model_name;
    $("judge-command").value = judge.command?.length ? JSON.stringify(judge.command) : "";
    $("judge-args").value = judge.args_template?.length ? JSON.stringify(judge.args_template) : "";
    $("judge-env").value = judge.env && Object.keys(judge.env).length ? JSON.stringify(judge.env) : "";
  }
}

function stopTyping() {
  if (state.typingInterval) {
    clearInterval(state.typingInterval);
    state.typingInterval = null;
  }
}

function ensureTypingPump() {
  if (state.typingInterval) return;
  state.typingInterval = setInterval(() => {
    if (!state.activeSpeaker || !state.activeSpeaker.pendingText) {
      stopTyping();
      return;
    }
    state.activeSpeaker.displayedText += state.activeSpeaker.pendingText[0];
    state.activeSpeaker.pendingText = state.activeSpeaker.pendingText.slice(1);
    renderDialoguePanel();
    if (!state.activeSpeaker.pendingText) {
      stopTyping();
    }
  }, 18);
}

function queueDialogue(event) {
  if (!state.activeSpeaker || state.activeSpeaker.agentId !== event.agent_id) {
    state.activeSpeaker = {
      agentId: event.agent_id,
      agentName: event.agent_name,
      roundType: event.round_type,
      displayedText: "",
      pendingText: "",
    };
  }
  state.activeSpeaker.pendingText += event.chunk;
  ensureTypingPump();
  renderArena();
}

function finalizeDialogue(message) {
  stopTyping();
  state.activeSpeaker = {
    agentId: message.agent_id,
    agentName: message.agent_name || message.agent_id,
    roundType: message.round_type,
    displayedText: message.display_text,
    pendingText: "",
  };
  renderArena();
}

function connectSocket(sessionId) {
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
        state.combatLog.push({
          type: event.type,
          title: `${event.agent_name} persona`,
          body: personaLabel(event.persona_id),
        });
        renderArena();
        renderCombatLog();
        return;
      }

      if (event.type === "round_started") {
        state.combatLog.push({
          type: event.type,
          title: `Round ${event.round_index}`,
          body: event.round_type,
        });
        renderCombatLog();
        return;
      }

      if (event.type === "message_chunk") {
        queueDialogue(event);
        return;
      }

      if (event.type === "message_saved") {
        state.activeSession.messages.push(event.message);
        finalizeDialogue(event.message);
        state.combatLog.push({
          type: event.type,
          title: `${event.message.agent_name} · ${event.message.round_type}`,
          body: event.message.display_text,
        });
        renderCombatLog();
        return;
      }

      if (event.type === "judge_result") {
        state.activeSession.judge_score = event.judge_score;
        state.combatLog.push({
          type: event.type,
          title: "Judge decision",
          body: event.judge_score.rationale,
        });
        renderCombatLog();
        renderSessionMeta();
        renderWinnerControls();
        return;
      }

      if (event.type === "provider_session_state") {
        const agent = state.activeSession.agents.find((item) => item.id === event.agent_id);
        if (agent) agent.provider_session = event.provider_session;
        state.combatLog.push({
          type: event.type,
          title: `${event.agent_name} session`,
          body:
            event.provider_session.mode === "persistent"
              ? "Persistent provider thread active."
              : `Replay fallback active. ${event.provider_session.last_error || ""}`.trim(),
        });
        renderArena();
        renderCombatLog();
        return;
      }

      if (event.type === "status") {
        state.activeSession.status = event.status;
        if (event.status === "failed" && event.error) {
          state.combatLog.push({
            type: event.type,
            title: "Chamber error",
            body: event.error,
          });
        }
        renderArena();
        renderSessionMeta();
        renderCycleControls();
        renderWinnerControls();
        renderCombatLog();
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

async function loadSession(sessionId) {
  const session = await fetchJson(`/api/sessions/${sessionId}`);
  state.activeSessionId = sessionId;
  state.activeSession = session;
  state.combatLog = buildCombatLogFromSession(session);
  state.activeSpeaker = session.messages.length
    ? {
        agentId: session.messages.at(-1).agent_id,
        agentName: session.messages.at(-1).agent_name,
        roundType: session.messages.at(-1).round_type,
        displayedText: session.messages.at(-1).display_text,
        pendingText: "",
      }
    : null;
  applySessionToDraft(session);
  renderSeatSetup();
  renderQuestionStage();
  renderArena();
  renderSessionMeta();
  renderCycleControls();
  renderWinnerControls();
  renderCombatLog();
  await connectSocket(sessionId);
  setView("arena");
}

async function openQuestionStep() {
  validateSeatConfig();
  state.setupLocked = true;
  state.questionStageOpen = true;
  state.activeSessionId = null;
  state.activeSession = null;
  state.combatLog = [];
  state.activeSpeaker = null;
  setQuestionFeedback("Seats locked. Now enter a debate question or ask the judge for suggestions.", "success");
  renderQuestionStage();
  renderArena();
  renderSessionMeta();
  renderCycleControls();
  renderWinnerControls();
  renderCombatLog();
}

async function askJudgeSuggestions() {
  validateSeatConfig();
  const payload = {
    question: $("debate-question").value.trim(),
    judge: getJudgePayload(),
  };
  const result = await fetchJson("/api/questions/suggestions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.questionSuggestions = result.suggestions || [];
  setQuestionFeedback("The judge suggested three candidate questions.", "success");
  renderQuestionStage();
}

async function validateQuestionOnly() {
  const payload = {
    question: $("debate-question").value.trim(),
    judge: getJudgePayload(),
  };
  const result = await fetchJson("/api/questions/validate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.questionSuggestions = result.suggestions || [];
  setQuestionFeedback(result.reason, result.accepted ? "success" : "error");
  renderQuestionStage();
  return result.accepted;
}

async function startDebate() {
  if (state.startInFlight) return;
  state.startInFlight = true;
  setQuestionFeedback("Validating the debate question and opening the chamber...", "success");
  renderQuestionStage();
  try {
    validateSeatConfig();
    const accepted = await validateQuestionOnly();
    if (!accepted) return;

    const payload = {
      topic: $("debate-question").value.trim(),
      agents: state.seatConfigs.map((seat, index) => ({
        display_name: seat.display_name.trim() || `Debater ${index + 1}`,
        preset_id: seat.preset_id,
        model_name: seat.model_name,
        persona_id: seat.persona_choice === "auto" ? null : seat.persona_choice,
        persona_mode: seat.persona_choice === "auto" ? "auto" : "manual",
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
    setQuestionFeedback("Chamber opened. Starting the debate...", "success");
    await loadSession(session.id);
    await fetchJson(`/api/sessions/${session.id}/start`, { method: "POST" });
    state.questionStageOpen = false;
    renderQuestionStage();
    renderArena();
  } catch (error) {
    console.error(error);
    if (state.activeSession) {
      state.activeSession.status = "failed";
      state.combatLog.push({
        type: "status",
        title: "Chamber error",
        body: error.message || "Start Debate failed.",
      });
      renderArena();
      renderSessionMeta();
      renderCycleControls();
      renderCombatLog();
    } else {
      setQuestionFeedback(error.message || "Start Debate failed.", "error");
      renderQuestionStage();
    }
  } finally {
    state.startInFlight = false;
    renderQuestionStage();
  }
}

async function continueDebate() {
  await fetchJson(`/api/sessions/${state.activeSession.id}/continue`, { method: "POST" });
}

async function endDebate() {
  const session = await fetchJson(`/api/sessions/${state.activeSession.id}/end`, { method: "POST" });
  state.activeSession = session;
  renderArena();
  renderSessionMeta();
  renderCycleControls();
  renderWinnerControls();
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
  const session = await fetchJson(`/api/sessions/${state.activeSession.id}/judge-decision`, {
    method: "POST",
    body: JSON.stringify({ judge: getJudgePayload() }),
  });
  state.activeSession = session;
  state.combatLog = buildCombatLogFromSession(session);
  renderArena();
  renderSessionMeta();
  renderWinnerControls();
  renderCombatLog();
  await refreshSessions();
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
  if (personaId) {
    await fetchJson(`/api/personas/${personaId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  } else {
    await fetchJson("/api/personas", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }
  clearPersonaForm();
  await loadPersonas();
  renderSeatSetup();
}

async function loadPersonas() {
  state.personas = await fetchJson("/api/personas");
  renderPersonas();
}

async function refreshSessions() {
  state.sessions = await fetchJson("/api/sessions");
  renderSessions();
}

function registerViewListeners() {
  document.querySelectorAll(".nav-tab").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
}

function registerSeatListeners() {
  $("seat-setup").addEventListener("input", (event) => {
    const field = event.target.dataset.field;
    const card = event.target.closest("[data-seat-id]");
    if (!field || !card) return;
    const seat = state.seatConfigs.find((item) => item.id === card.dataset.seatId);
    if (!seat) return;
    seat[field] = event.target.value;
    renderArena();
  });

  $("seat-setup").addEventListener("change", (event) => {
    const field = event.target.dataset.field;
    const card = event.target.closest("[data-seat-id]");
    if (!field || !card) return;
    const seat = state.seatConfigs.find((item) => item.id === card.dataset.seatId);
    if (!seat) return;
    seat[field] = event.target.value;
    if (field === "preset_id") {
      seat.model_name = defaultModelForPreset(seat.preset_id);
      renderSeatSetup();
      return;
    }
    renderArena();
  });

  $("seat-setup").addEventListener("click", (event) => {
    if (event.target.dataset.action !== "remove-seat") return;
    const card = event.target.closest("[data-seat-id]");
    if (!card || state.seatConfigs.length <= 2) return;
    state.seatConfigs = state.seatConfigs.filter((item) => item.id !== card.dataset.seatId);
    renderSeatSetup();
  });
}

function registerArenaListeners() {
  $("add-seat").addEventListener("click", () => {
    if (state.seatConfigs.length >= 5) return;
    state.seatConfigs.push(buildSeatConfig(state.seatConfigs.length));
    renderSeatSetup();
  });

  $("seats-set").addEventListener("click", () => openQuestionStep().catch(alert));
  $("edit-seats").addEventListener("click", () => {
    state.setupLocked = false;
    state.questionStageOpen = false;
    setQuestionFeedback();
    renderQuestionStage();
    renderArena();
  });
  $("ask-suggestions").addEventListener("click", async () => {
    try {
      await askJudgeSuggestions();
    } catch (error) {
      console.error(error);
      setQuestionFeedback(error.message || "The judge could not provide suggestions.", "error");
      renderQuestionStage();
    }
  });
  $("start-debate").addEventListener("click", () => startDebate());
  $("export-session").addEventListener("click", () => {
    if (!state.activeSessionId) return;
    window.open(`/api/sessions/${state.activeSessionId}/export`, "_blank");
  });
  $("suggestion-list").addEventListener("click", (event) => {
    const suggestion = event.target.dataset.suggestion;
    if (!suggestion) return;
    $("debate-question").value = suggestion;
    setQuestionFeedback("Suggestion applied. Start the debate when ready.", "success");
    renderQuestionStage();
    renderArena();
  });
  $("debate-question").addEventListener("input", () => {
    setQuestionFeedback();
    renderQuestionStage();
    renderArena();
  });
}

function registerJudgeListeners() {
  $("judge-preset").addEventListener("change", () => {
    $("judge-model").dataset.value = defaultModelForPreset($("judge-preset").value);
    renderJudgeModelDropdown();
    renderArena();
  });
  $("judge-model").addEventListener("change", () => {
    $("judge-model").dataset.value = $("judge-model").value;
    renderArena();
  });
  ["judge-name", "judge-command", "judge-args"].forEach((id) => {
    $(id).addEventListener("input", () => renderArena());
  });
  $("judge-env").addEventListener("input", () => renderArena());
}

function registerPersonaListeners() {
  $("save-persona").addEventListener("click", () => savePersona().catch(alert));
  $("persona-list").addEventListener("click", (event) => {
    const personaId = event.target.dataset.personaId;
    if (!personaId) return;
    const persona = state.personas.find((item) => item.id === personaId);
    if (persona) fillPersonaForm(persona);
  });
}

function registerSessionListeners() {
  $("refresh-sessions").addEventListener("click", () => refreshSessions().catch(alert));
  $("session-list").addEventListener("click", (event) => {
    const sessionId = event.target.dataset.sessionId;
    if (!sessionId) return;
    loadSession(sessionId).catch(alert);
  });
}

async function boot() {
  registerViewListeners();
  registerSeatListeners();
  registerArenaListeners();
  registerJudgeListeners();
  registerPersonaListeners();
  registerSessionListeners();

  state.presets = await fetchJson("/api/presets");
  const defaultJudgePresetId = preferredPresetId();
  $("judge-preset").innerHTML = presetOptions(defaultJudgePresetId);
  $("judge-preset").value = defaultJudgePresetId;
  renderJudgeModelDropdown();

  await loadPersonas();
  initializeSeatConfigs();
  renderSeatSetup();
  renderQuestionStage();
  renderArena();
  await refreshSessions();
}

boot().catch((error) => {
  console.error(error);
  alert(error.message);
});
