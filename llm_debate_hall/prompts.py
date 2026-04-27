from __future__ import annotations

from typing import Any

from llm_debate_hall.payloads import single_paragraph

PERSONA_INTENSITY_DEFAULT = 1.0
PERSONA_INTENSITY_MIN = 0.5
PERSONA_INTENSITY_MAX = 1.5

ROUND_INSTRUCTIONS = {
    "opening": "State your position in exactly one concise paragraph.",
    "reply": "Respond to the chamber in exactly one concise paragraph.",
}


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
    return (
        "You are participating in a structured debate.\n"
        f"TOPIC: {topic}\n"
        f"ROUND: {round_type}\n"
        f"PERSONA: {persona['name']} | {persona['style']}\n"
        f"PERSONA INTENSITY: {intensity:.2f}\n"
        f"INTENSITY GUIDANCE: {persona_intensity_guidance(intensity)}\n"
        f"VALUES: {', '.join(persona['core_values'])}\n"
        f"RULES: {', '.join(persona['debate_rules'])}\n"
        f"INSTRUCTION: {ROUND_INSTRUCTIONS[round_type]}\n"
        "STYLE CONSTRAINT: Return exactly one paragraph.\n"
        "TRANSCRIPT SUMMARY:\n"
        f"{transcript}\n"
        "Return JSON with keys: display_text, claim, reasoning, attack, question, confidence"
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
    return (
        "You are continuing the same structured debate session.\n"
        f"TOPIC: {topic}\n"
        f"ROUND: {round_type}\n"
        f"PERSONA: {persona['name']} | {persona['style']}\n"
        f"PERSONA INTENSITY: {intensity:.2f}\n"
        f"INTENSITY GUIDANCE: {persona_intensity_guidance(intensity)}\n"
        f"VALUES: {', '.join(persona['core_values'])}\n"
        f"RULES: {', '.join(persona['debate_rules'])}\n"
        f"INSTRUCTION: {ROUND_INSTRUCTIONS[round_type]}\n"
        "STYLE CONSTRAINT: Return exactly one paragraph.\n"
        "NEW CHAMBER UPDATES SINCE YOUR LAST TURN:\n"
        f"{updates}\n"
        "Return JSON with keys: display_text, claim, reasoning, attack, question, confidence"
    )


def build_judge_prompt(topic: str, session: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    transcript = summarize_messages(session, max_items=16)
    candidate_ids = ", ".join(agent["id"] for agent in candidates)
    return (
        "Judge the debate.\n"
        f"TOPIC: {topic}\n"
        f"CANDIDATES: {candidate_ids}\n"
        "CRITERIA: coherence, responsiveness, evidence, style\n"
        "TRANSCRIPT SUMMARY:\n"
        f"{transcript}\n"
        'Return JSON: {"winner_agent_id":"...", "rationale":"...", "criteria":{...}}'
    )


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


def _format_conversation_entry(item: dict[str, Any]) -> str:
    display_name = item.get("display_name", item.get("agent_name", item.get("agent_id", "Moderator")))
    return f"{item.get('round_type') or item['kind']} | {display_name} | {single_paragraph(item['display_text'])}"
