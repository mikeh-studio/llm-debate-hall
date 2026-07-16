from __future__ import annotations

import re
from typing import Any

from llm_debate_hall.payloads import single_paragraph

PERSONA_INTENSITY_DEFAULT = 1.0
PERSONA_INTENSITY_MIN = 0.5
PERSONA_INTENSITY_MAX = 1.5

ROUND_INSTRUCTIONS = {
    "opening": "State your position in exactly one concise paragraph.",
    "reply": "Respond to the chamber in exactly one concise paragraph.",
}
CONVERSATIONAL_ROUND_INSTRUCTIONS = {
    "opening": "State your position in 2-3 direct, conversational sentences.",
    "reply": "Respond to the latest relevant point in 2-4 direct, conversational sentences.",
}
STYLE_CONSTRAINT = "Return exactly one paragraph."
CONVERSATIONAL_STYLE_CONSTRAINT = (
    "Write like a sharp group chat, not an essay. Engage the latest relevant point directly, "
    "name another debater when useful, and ask at most one concise question."
)


def persona_intensity_value(agent: dict[str, Any]) -> float:
    raw = agent.get("persona_intensity")
    try:
        value = float(raw if raw is not None else PERSONA_INTENSITY_DEFAULT)
    except (TypeError, ValueError):
        value = PERSONA_INTENSITY_DEFAULT
    return min(PERSONA_INTENSITY_MAX, max(PERSONA_INTENSITY_MIN, value))


def persona_intensity_guidance(value: float) -> str:
    if value <= 0.7:
        return "Play the persona subtly. Keep the voice restrained and avoid exaggerating the worldview."
    if value <= 0.95:
        return "Play the persona with mild coloration. Let the worldview shape emphasis more than theatrics."
    if value < 1.2:
        return "Play the persona at a balanced default intensity."
    if value < 1.4:
        return "Play the persona vividly. Let the worldview strongly shape framing, tone, and attacks."
    return "Play the persona at high intensity. Make the worldview unmistakable, but remain coherent and concise."


def opponent_names(session: dict[str, Any], agent: dict[str, Any]) -> list[str]:
    return [
        item["display_name"]
        for item in session.get("agents", [])
        if item.get("role") == "debater" and item.get("id") != agent.get("id")
    ]


def debate_actions_block(session: dict[str, Any], agent: dict[str, Any], round_type: str) -> str:
    if round_type == "opening":
        return ""
    opponents = opponent_names(session, agent)
    tokens = int(agent.get("moves_remaining") or 0)
    lines = [
        "DEBATE ACTIONS:",
        f"OPPONENTS: {', '.join(opponents) if opponents else 'none'}",
        f"ACTION TOKENS REMAINING: {tokens}",
    ]
    if tokens > 0:
        lines.append(
            "You may spend ONE action token this turn by adding an optional \"move\" key to your JSON: "
            '{"type":"challenge"|"objection","target":"<opponent name>","quote":"<their exact words>",'
            '"question":"<one direct question>"}. '
            "A challenge forces the target to answer your question on their next turn. "
            "An objection formally flags the target's latest claim as unsupported or fallacious. "
            "Tokens cover the entire debate and never replenish — play one only when it will change the debate. "
            "Most turns should not include a move."
        )
    else:
        lines.append("You have no action tokens left. Do not include a move.")
    for pending in agent.get("pending_challenges") or []:
        quoted = f' (they quoted you: "{pending.get("quote")}")' if pending.get("quote") else ""
        question = pending.get("question") or "Defend your quoted claim."
        lines.append(
            f'INCOMING CHALLENGE from {pending.get("challenger_name", "an opponent")}{quoted}: "{question}" '
            "You must answer this challenge directly at the start of your response."
        )
    return "\n".join(lines) + "\n"


def _return_keys_line(round_type: str) -> str:
    if round_type == "opening":
        return "Return JSON with keys: display_text, claim, reasoning, attack, question, confidence"
    return (
        "Return JSON with keys: display_text, claim, reasoning, attack, question, confidence, "
        "reaction (agree|disagree|skeptical|intrigued — your honest reaction to the most recent "
        "opposing argument), and an optional move."
    )


def build_persona_prompt(
    topic: str,
    agent: dict[str, Any],
    selectable_personas: list[dict[str, Any]],
) -> str:
    persona_lines = "\n".join(
        f"- {persona['id']}: {persona['name']} | {persona['style']}" for persona in selectable_personas
    )
    return (
        "Select exactly one persona for this debate.\n"
        f"TOPIC: {topic}\n"
        f"AGENT: {agent['display_name']}\n"
        "AVAILABLE PERSONAS:\n"
        f"{persona_lines}\n"
        'Return JSON: {"persona_id":"...", "justification":"..."}'
    )


def build_turn_prompt(
    session: dict[str, Any],
    topic: str,
    agent: dict[str, Any],
    round_type: str,
    personas: list[dict[str, Any]],
) -> str:
    transcript = summarize_messages(session)
    persona = _persona_for_agent(personas, agent)
    intensity = persona_intensity_value(agent)
    debate_mode = _debate_mode(session)
    return (
        "You are participating in a structured debate.\n"
        f"TOPIC: {topic}\n"
        f"DEBATE MODE: {debate_mode}\n"
        f"ROUND: {round_type}\n"
        f"PERSONA: {persona['name']} | {persona['style']}\n"
        f"PERSONA INTENSITY: {intensity:.2f}\n"
        f"INTENSITY GUIDANCE: {persona_intensity_guidance(intensity)}\n"
        f"VALUES: {', '.join(persona['core_values'])}\n"
        f"RULES: {', '.join(persona['debate_rules'])}\n"
        f"INSTRUCTION: {_round_instruction(round_type, debate_mode)}\n"
        f"STYLE CONSTRAINT: {_style_constraint(debate_mode)}\n"
        f"{debate_actions_block(session, agent, round_type)}"
        "TRANSCRIPT SUMMARY:\n"
        f"{transcript}\n"
        f"{_return_keys_line(round_type)}"
    )


def build_persistent_turn_prompt(
    *,
    session: dict[str, Any],
    topic: str,
    agent: dict[str, Any],
    round_type: str,
    provider_session: dict[str, Any] | None,
    personas: list[dict[str, Any]],
) -> str:
    if provider_session is None or round_type == "opening":
        return build_turn_prompt(session, topic, agent, round_type, personas)

    persona = _persona_for_agent(personas, agent)
    intensity = persona_intensity_value(agent)
    updates = summarize_messages_since_last_turn(session, agent["id"])
    debate_mode = _debate_mode(session)
    return (
        "You are continuing the same structured debate session.\n"
        f"TOPIC: {topic}\n"
        f"DEBATE MODE: {debate_mode}\n"
        f"ROUND: {round_type}\n"
        f"PERSONA: {persona['name']} | {persona['style']}\n"
        f"PERSONA INTENSITY: {intensity:.2f}\n"
        f"INTENSITY GUIDANCE: {persona_intensity_guidance(intensity)}\n"
        f"VALUES: {', '.join(persona['core_values'])}\n"
        f"RULES: {', '.join(persona['debate_rules'])}\n"
        f"INSTRUCTION: {_round_instruction(round_type, debate_mode)}\n"
        f"STYLE CONSTRAINT: {_style_constraint(debate_mode)}\n"
        f"{debate_actions_block(session, agent, round_type)}"
        "NEW CHAMBER UPDATES SINCE YOUR LAST TURN:\n"
        f"{updates}\n"
        f"{_return_keys_line(round_type)}"
    )


JUDGE_CRITERIA = ("coherence", "responsiveness", "evidence", "style")


def build_judge_prompt(
    topic: str,
    session: dict[str, Any],
    label_by_agent_id: dict[str, str],
) -> str:
    transcript = _blinded_transcript(session, label_by_agent_id)
    candidate_labels = ", ".join(sorted(label_by_agent_id.values()))
    score_shape = ", ".join(f'"{label}": 0' for label in sorted(label_by_agent_id.values()))
    return (
        "Judge the debate without trying to infer candidate identity, provider, persona, or seat order.\n"
        f"TOPIC: {topic}\n"
        f"CANDIDATES: {candidate_labels}\n"
        f"CRITERIA: {', '.join(JUDGE_CRITERIA)}\n"
        "Score every candidate from 0 to 10 on every criterion. Select exactly one winner.\n"
        "BLINDED FULL TRANSCRIPT:\n"
        f"{transcript}\n"
        "Return only JSON with this shape: "
        f'{{"winner_label":"A", "rationale":"...", "criteria":'
        f'{{"coherence":{{"scores":{{{score_shape}}},"notes":"..."}},'
        f'"responsiveness":{{"scores":{{{score_shape}}},"notes":"..."}},'
        f'"evidence":{{"scores":{{{score_shape}}},"notes":"..."}},'
        f'"style":{{"scores":{{{score_shape}}},"notes":"..."}}}}}}'
    )


def _blinded_transcript(session: dict[str, Any], label_by_agent_id: dict[str, str]) -> str:
    entries = conversation_entries(session)
    if not entries:
        return "No prior turns."
    name_to_label = {
        agent["display_name"]: f"Candidate {label_by_agent_id[agent['id']]}"
        for agent in session.get("agents", [])
        if agent.get("id") in label_by_agent_id
    }
    lines: list[str] = []
    for item in entries:
        agent_id = item.get("agent_id")
        if agent_id in label_by_agent_id:
            speaker = f"Candidate {label_by_agent_id[agent_id]}"
        else:
            speaker = "Moderator"
        phase = item.get("round_type") or item.get("kind", "entry")
        display_text = single_paragraph(item["display_text"]) + _move_annotation(item)
        for display_name, replacement in name_to_label.items():
            display_text = re.sub(re.escape(display_name), replacement, display_text, flags=re.IGNORECASE)
        lines.append(f"{phase} | {speaker} | {display_text}")
    return "\n".join(lines)


def conversation_entries(session: dict[str, Any]) -> list[dict[str, Any]]:
    thread_entries = [
        entry
        for entry in session.get("thread_entries", [])
        if entry["kind"] in {"agent", "moderator"}
    ]
    if thread_entries:
        return thread_entries
    return [
        {
            "kind": "agent",
            "round_type": item["round_type"],
            "round_index": item["round_index"],
            "agent_id": item["agent_id"],
            "display_name": item.get("agent_name", item["agent_id"]),
            "display_text": item["display_text"],
            "payload": item.get("normalized_payload") or {},
        }
        for item in session.get("messages", [])
    ]


def summarize_messages(session: dict[str, Any], max_items: int = 10) -> str:
    entries = conversation_entries(session)
    if not entries:
        return "No prior turns."
    selected = entries[-max_items:]
    lines = [_format_conversation_entry(item) for item in selected]
    return "\n".join(lines)


def summarize_messages_since_last_turn(
    session: dict[str, Any],
    agent_id: str,
    max_items: int = 8,
) -> str:
    messages = conversation_entries(session)
    last_agent_index = -1
    for index, item in enumerate(messages):
        if item.get("agent_id") == agent_id:
            last_agent_index = index
    if last_agent_index == -1:
        return summarize_messages(session, max_items=max_items)
    selected = messages[last_agent_index + 1 :][-max_items:]
    if not selected:
        return "No new chamber turns since your last response."
    lines = [_format_conversation_entry(item) for item in selected]
    return "\n".join(lines)


def persona_selection_text(auto_agents: list[dict[str, Any]]) -> str:
    names = ", ".join(agent["display_name"] for agent in auto_agents)
    return f"Selecting personas for {names} before opening statements."


def _persona_for_agent(personas: list[dict[str, Any]], agent: dict[str, Any]) -> dict[str, Any]:
    return next(persona for persona in personas if persona["id"] == agent["persona_id"])


def _debate_mode(session: dict[str, Any]) -> str:
    raw = session.get("debate_mode")
    if raw in {"conversational", "theater", "serious"}:
        return raw
    return "serious"


def _round_instruction(round_type: str, debate_mode: str) -> str:
    if debate_mode == "conversational":
        return CONVERSATIONAL_ROUND_INSTRUCTIONS[round_type]
    return ROUND_INSTRUCTIONS[round_type]


def _style_constraint(debate_mode: str) -> str:
    if debate_mode == "conversational":
        return CONVERSATIONAL_STYLE_CONSTRAINT
    return STYLE_CONSTRAINT


def _move_annotation(item: dict[str, Any]) -> str:
    payload = item.get("payload") or {}
    move = payload.get("move")
    if not move:
        return ""
    target = move.get("target_name") or move.get("target") or "an opponent"
    detail = move.get("question") if move.get("type") == "challenge" else move.get("quote")
    detail_text = f': "{detail}"' if detail else ""
    return f" [{str(move.get('type', 'move')).upper()} -> {target}{detail_text}]"


def _format_conversation_entry(item: dict[str, Any]) -> str:
    display_name = item.get("display_name", item.get("agent_name", item.get("agent_id", "Moderator")))
    return (
        f"{item.get('round_type') or item['kind']} | {display_name} | "
        f"{single_paragraph(item['display_text'])}{_move_annotation(item)}"
    )
