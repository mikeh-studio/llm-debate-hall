from llm_debate_hall.prompts import build_persistent_turn_prompt, build_turn_prompt, summarize_messages


PERSONAS = [
    {
        "id": "stoic_rationalist",
        "name": "Stoic Rationalist",
        "style": "Calm, precise, and principle-driven.",
        "core_values": ["clarity", "discipline"],
        "debate_rules": ["state claims plainly"],
    }
]


def test_prompt_summary_prefers_thread_entries_and_keeps_moderator_context() -> None:
    session = {
        "thread_entries": [
            {
                "kind": "agent",
                "display_name": "Athena",
                "display_text": "Opening claim.",
                "round_type": "opening",
                "round_index": 1,
                "agent_id": "agent-1",
            },
            {
                "kind": "moderator",
                "display_name": "Moderator",
                "display_text": "Challenge the strongest assumption.",
                "round_type": None,
                "round_index": None,
                "agent_id": None,
            },
        ],
        "messages": [
            {
                "round_type": "opening",
                "round_index": 1,
                "agent_id": "agent-2",
                "agent_name": "Burke",
                "display_text": "Stored message fallback.",
            }
        ],
    }

    summary = summarize_messages(session)

    assert "Moderator" in summary
    assert "Challenge the strongest assumption." in summary
    assert "Stored message fallback." not in summary


def test_turn_prompt_includes_persona_intensity_without_brittle_full_text() -> None:
    session = {"thread_entries": [], "messages": []}
    agent = {
        "id": "agent-1",
        "display_name": "Athena",
        "persona_id": "stoic_rationalist",
        "persona_intensity": 1.45,
    }

    prompt = build_turn_prompt(session, "Should personas vary in intensity?", agent, "opening", PERSONAS)

    assert "PERSONA INTENSITY: 1.45" in prompt
    assert "INTENSITY GUIDANCE:" in prompt
    assert "No prior turns." in prompt


def test_conversational_turn_prompt_uses_chat_style_constraints() -> None:
    session = {"debate_mode": "conversational", "thread_entries": [], "messages": []}
    agent = {
        "id": "agent-1",
        "display_name": "Athena",
        "persona_id": "stoic_rationalist",
        "persona_intensity": 1.0,
    }

    prompt = build_turn_prompt(session, "Should debaters talk more naturally?", agent, "reply", PERSONAS)

    assert "DEBATE MODE: conversational" in prompt
    assert "2-4 direct, conversational sentences" in prompt
    assert "sharp group chat" in prompt
    assert "ask at most one concise question" in prompt


def test_persistent_reply_prompt_uses_updates_since_agent_last_turn() -> None:
    session = {
        "thread_entries": [
            {
                "kind": "agent",
                "display_name": "Athena",
                "display_text": "Athena old claim.",
                "round_type": "opening",
                "round_index": 1,
                "agent_id": "agent-1",
            },
            {
                "kind": "moderator",
                "display_name": "Moderator",
                "display_text": "Old moderator note.",
                "round_type": None,
                "round_index": None,
                "agent_id": None,
            },
            {
                "kind": "agent",
                "display_name": "Athena",
                "display_text": "Athena latest response.",
                "round_type": "reply",
                "round_index": 2,
                "agent_id": "agent-1",
            },
            {
                "kind": "agent",
                "display_name": "Burke",
                "display_text": "Burke new chamber update.",
                "round_type": "reply",
                "round_index": 2,
                "agent_id": "agent-2",
            },
        ],
        "messages": [],
    }
    agent = {
        "id": "agent-1",
        "display_name": "Athena",
        "persona_id": "stoic_rationalist",
        "persona_intensity": 1.0,
    }

    prompt = build_persistent_turn_prompt(
        session=session,
        topic="Should persistent sessions replay less context?",
        agent=agent,
        round_type="reply",
        provider_session={"mode": "persistent", "status": "active"},
        personas=PERSONAS,
    )

    assert "NEW CHAMBER UPDATES SINCE YOUR LAST TURN:" in prompt
    assert "Burke new chamber update." in prompt
    assert "Athena old claim." not in prompt
    assert "Old moderator note." not in prompt


def test_conversational_persistent_reply_prompt_uses_chat_style_constraints() -> None:
    session = {
        "debate_mode": "conversational",
        "thread_entries": [
            {
                "kind": "agent",
                "display_name": "Athena",
                "display_text": "Athena old claim.",
                "round_type": "opening",
                "round_index": 1,
                "agent_id": "agent-1",
            },
            {
                "kind": "agent",
                "display_name": "Burke",
                "display_text": "Burke latest point.",
                "round_type": "reply",
                "round_index": 2,
                "agent_id": "agent-2",
            },
        ],
        "messages": [],
    }
    agent = {
        "id": "agent-1",
        "display_name": "Athena",
        "persona_id": "stoic_rationalist",
        "persona_intensity": 1.0,
    }

    prompt = build_persistent_turn_prompt(
        session=session,
        topic="Should persistent debates feel conversational?",
        agent=agent,
        round_type="reply",
        provider_session={"mode": "persistent", "status": "active"},
        personas=PERSONAS,
    )

    assert "DEBATE MODE: conversational" in prompt
    assert "latest relevant point" in prompt
    assert "Burke latest point." in prompt
