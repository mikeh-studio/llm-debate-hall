from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from typing import Any, Callable

from llm_debate_hall.adapters.base import PRESET_REGISTRY, AdapterRequest, DebateAdapter
from llm_debate_hall.adapters.mock_adapter import MockDebateAdapter
from llm_debate_hall.adapters.subprocess_adapter import SubprocessDebateAdapter
from llm_debate_hall.events import EventBroker
from llm_debate_hall.storage import Storage

REPLY_ROUNDS_PER_CYCLE = 2


def _extract_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _single_paragraph(text: str) -> str:
    cleaned = " ".join(part.strip() for part in text.replace("\r", "\n").splitlines() if part.strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def default_adapter_factory(agent: dict[str, Any]) -> DebateAdapter:
    if agent["preset_id"] == "mock":
        return MockDebateAdapter()
    return SubprocessDebateAdapter()


class DebateEngine:
    def __init__(
        self,
        *,
        storage: Storage,
        broker: EventBroker,
        adapter_factory: Callable[[dict[str, Any]], DebateAdapter] | None = None,
    ) -> None:
        self.storage = storage
        self.broker = broker
        self.adapter_factory = adapter_factory or default_adapter_factory
        self._threads: dict[str, threading.Thread] = {}

    def start_session(self, session_id: str) -> None:
        self._spawn(session_id, lambda: self.run_segment(session_id))

    def continue_session(self, session_id: str) -> None:
        self._spawn(session_id, lambda: self.run_segment(session_id))

    async def end_session(self, session_id: str) -> None:
        self.storage.update_session_status(session_id, "awaiting_winner")
        await self.broker.publish(session_id, {"type": "status", "status": "awaiting_winner"})

    async def decide_winner(self, session_id: str, judge_override: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self.storage.get_session(session_id)
        judge = judge_override or next(agent for agent in session["agents"] if agent["role"] == "judge")
        await self._judge_session(session_id, session["topic"], judge)
        self.storage.update_session_status(session_id, "completed")
        await self.broker.publish(session_id, {"type": "status", "status": "completed"})
        return self.storage.get_session(session_id)

    async def run_segment(self, session_id: str) -> None:
        self.storage.update_session_status(session_id, "running")
        await self.broker.publish(session_id, {"type": "status", "status": "running"})
        try:
            session = self.storage.get_session(session_id)
            selectable_personas = self.storage.get_selectable_personas()
            agents = [agent for agent in session["agents"] if agent["role"] == "debater"]

            await self._select_personas(session, agents, selectable_personas)

            next_round_index = self._next_round_index(session)
            if not session["messages"]:
                next_round_index = await self._play_round(
                    session_id=session_id,
                    topic=session["topic"],
                    agents=agents,
                    round_type="opening",
                    round_index=next_round_index,
                )

            for _ in range(REPLY_ROUNDS_PER_CYCLE):
                next_round_index = await self._play_round(
                    session_id=session_id,
                    topic=session["topic"],
                    agents=agents,
                    round_type="reply",
                    round_index=next_round_index,
                )

            self.storage.update_session_status(session_id, "awaiting_continue")
            await self.broker.publish(session_id, {"type": "status", "status": "awaiting_continue"})
        except Exception as exc:
            self.storage.update_session_status(session_id, "failed")
            await self.broker.publish(
                session_id,
                {"type": "status", "status": "failed", "error": str(exc)},
            )
            raise

    async def _play_round(
        self,
        *,
        session_id: str,
        topic: str,
        agents: list[dict[str, Any]],
        round_type: str,
        round_index: int,
    ) -> int:
        round_id = self.storage.create_round(session_id, round_index, round_type)
        await self.broker.publish(
            session_id,
            {"type": "round_started", "round_type": round_type, "round_index": round_index},
        )
        for agent in agents:
            await self._run_turn(
                session_id=session_id,
                topic=topic,
                round_type=round_type,
                round_index=round_index,
                agent=agent,
            )
        self.storage.complete_round(round_id)
        await self.broker.publish(
            session_id,
            {"type": "round_completed", "round_type": round_type, "round_index": round_index},
        )
        return round_index + 1

    async def _select_personas(
        self,
        session: dict[str, Any],
        agents: list[dict[str, Any]],
        selectable_personas: list[dict[str, Any]],
    ) -> None:
        for agent in agents:
            if agent.get("persona_id"):
                continue
            adapter = self.adapter_factory(agent)
            prompt = self._build_persona_prompt(session["topic"], agent, selectable_personas)
            request = AdapterRequest(
                session_id=session["id"],
                agent_id=agent["id"],
                agent_name=agent["display_name"],
                preset_id=agent["preset_id"],
                role=agent["role"],
                side=agent["side"],
                topic=session["topic"],
                prompt=prompt,
                output_mode="persona",
                model_name=agent["model_name"],
                command=agent["command"],
                args_template=agent["args_template"],
                env=agent["env"],
            )
            response = await adapter.generate(request, lambda chunk: self._noop(chunk))
            payload = _extract_json(response.raw_text) or {}
            persona_id = payload.get("persona_id")
            if not any(persona["id"] == persona_id for persona in selectable_personas):
                persona_id = selectable_personas[0]["id"]
            self.storage.update_agent_persona(agent["id"], persona_id)
            agent["persona_id"] = persona_id
            await self.broker.publish(
                session["id"],
                {
                    "type": "persona_selected",
                    "agent_id": agent["id"],
                    "agent_name": agent["display_name"],
                    "persona_id": persona_id,
                    "justification": payload.get("justification", ""),
                },
            )

    async def _run_turn(
        self,
        *,
        session_id: str,
        topic: str,
        round_type: str,
        round_index: int,
        agent: dict[str, Any],
    ) -> None:
        session = self.storage.get_session(session_id)
        adapter = self.adapter_factory(agent)
        provider_session = self.storage.get_provider_session(agent["id"])
        active_provider_session = (
            provider_session
            if provider_session and provider_session["mode"] == "persistent" and provider_session["status"] == "active"
            else None
        )

        async def on_chunk(chunk: str) -> None:
            await self.broker.publish(
                session_id,
                {
                    "type": "message_chunk",
                    "round_type": round_type,
                    "round_index": round_index,
                    "agent_id": agent["id"],
                    "agent_name": agent["display_name"],
                    "chunk": chunk,
                },
            )

        prompt = self._build_turn_prompt(session, topic, agent, round_type)
        request = AdapterRequest(
            session_id=session_id,
            agent_id=agent["id"],
            agent_name=agent["display_name"],
            preset_id=agent["preset_id"],
            role=agent["role"],
            side=agent["side"],
            topic=topic,
            prompt=prompt,
            output_mode=round_type,
            model_name=agent["model_name"],
            command=agent["command"],
            args_template=agent["args_template"],
            env=agent["env"],
        )

        should_try_persistent = adapter.supports_persistent_sessions(request) and (
            provider_session is None or active_provider_session is not None
        )

        if should_try_persistent:
            request.prompt = self._build_persistent_turn_prompt(
                session=session,
                topic=topic,
                agent=agent,
                round_type=round_type,
                provider_session=active_provider_session,
            )
            try:
                persistent_result = await adapter.generate_persistent(
                    request,
                    active_provider_session["provider_session_id"] if active_provider_session else None,
                    on_chunk,
                )
                response = persistent_result.response
                persisted_session = self.storage.upsert_provider_session(
                    session_id=session_id,
                    agent_id=agent["id"],
                    preset_id=agent["preset_id"],
                    provider_session_id=persistent_result.provider_session_id,
                    mode="persistent",
                    status="active",
                )
                await self.broker.publish(
                    session_id,
                    {
                        "type": "provider_session_state",
                        "agent_id": agent["id"],
                        "agent_name": agent["display_name"],
                        "provider_session": persisted_session,
                    },
                )
            except Exception as exc:
                fallback_session = self.storage.upsert_provider_session(
                    session_id=session_id,
                    agent_id=agent["id"],
                    preset_id=agent["preset_id"],
                    provider_session_id=active_provider_session["provider_session_id"] if active_provider_session else None,
                    mode="replay_fallback",
                    status="fallback",
                    last_error=str(exc),
                )
                await self.broker.publish(
                    session_id,
                    {
                        "type": "provider_session_state",
                        "agent_id": agent["id"],
                        "agent_name": agent["display_name"],
                        "provider_session": fallback_session,
                    },
                )
                request.prompt = prompt
                response = await adapter.generate(request, on_chunk)
        else:
            response = await adapter.generate(request, on_chunk)

        payload = self._normalize_turn_payload(response.raw_text, agent["display_name"], round_type)
        message = self.storage.add_message(
            session_id=session_id,
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            persona_id=agent["persona_id"],
            stance=agent["side"],
            display_text=payload["display_text"],
            normalized_payload=payload,
            stream_status=response.stream_status,
        )
        await self.broker.publish(
            session_id,
            {"type": "message_saved", "message": {**message, "agent_name": agent["display_name"]}},
        )

    async def _judge_session(self, session_id: str, topic: str, judge: dict[str, Any]) -> None:
        session = self.storage.get_session(session_id)
        candidates = [agent for agent in session["agents"] if agent["role"] == "debater"]
        prompt = self._build_judge_prompt(topic, session, candidates)
        request = AdapterRequest(
            session_id=session_id,
            agent_id=judge["id"],
            agent_name=judge["display_name"],
            preset_id=judge["preset_id"],
            role="judge",
            side="judge",
            topic=topic,
            prompt=prompt,
            output_mode="judge",
            model_name=judge["model_name"],
            command=judge["command"],
            args_template=judge["args_template"],
            env=judge["env"],
        )
        adapter = self.adapter_factory(judge)
        response = await adapter.generate(request, lambda chunk: self._noop(chunk))
        payload = _extract_json(response.raw_text) or {}
        winner_agent_id = payload.get("winner_agent_id")
        if not any(agent["id"] == winner_agent_id for agent in candidates):
            winner_agent_id = candidates[0]["id"]
        score = self.storage.add_judge_score(
            session_id=session_id,
            judge_agent_id=judge["id"],
            winner_agent_id=winner_agent_id,
            rationale=payload.get("rationale", "No rationale provided."),
            criteria=payload.get("criteria", {}),
            raw_text=response.raw_text,
        )
        await self.broker.publish(session_id, {"type": "judge_result", "judge_score": score})

    def _build_persona_prompt(
        self, topic: str, agent: dict[str, Any], selectable_personas: list[dict[str, Any]]
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

    def _build_turn_prompt(
        self, session: dict[str, Any], topic: str, agent: dict[str, Any], round_type: str
    ) -> str:
        messages = session["messages"]
        transcript = self._summarize_messages(messages)
        persona = next(
            persona for persona in self.storage.list_personas() if persona["id"] == agent["persona_id"]
        )
        round_instructions = {
            "opening": "State your position in exactly one concise paragraph.",
            "reply": "Respond to the chamber in exactly one concise paragraph.",
        }
        return (
            "You are participating in a structured debate.\n"
            f"TOPIC: {topic}\n"
            f"ROUND: {round_type}\n"
            f"PERSONA: {persona['name']} | {persona['style']}\n"
            f"VALUES: {', '.join(persona['core_values'])}\n"
            f"RULES: {', '.join(persona['debate_rules'])}\n"
            f"INSTRUCTION: {round_instructions[round_type]}\n"
            "STYLE CONSTRAINT: Return exactly one paragraph.\n"
            "TRANSCRIPT SUMMARY:\n"
            f"{transcript}\n"
            'Return JSON with keys: display_text, claim, reasoning, attack, question, confidence'
        )

    def _build_persistent_turn_prompt(
        self,
        *,
        session: dict[str, Any],
        topic: str,
        agent: dict[str, Any],
        round_type: str,
        provider_session: dict[str, Any] | None,
    ) -> str:
        if provider_session is None or round_type == "opening":
            return self._build_turn_prompt(session, topic, agent, round_type)

        persona = next(
            persona for persona in self.storage.list_personas() if persona["id"] == agent["persona_id"]
        )
        round_instructions = {
            "opening": "State your position in exactly one concise paragraph.",
            "reply": "Respond to the chamber in exactly one concise paragraph.",
        }
        updates = self._summarize_messages_since_last_turn(session["messages"], agent["id"])
        return (
            "You are continuing the same structured debate session.\n"
            f"TOPIC: {topic}\n"
            f"ROUND: {round_type}\n"
            f"PERSONA: {persona['name']} | {persona['style']}\n"
            f"VALUES: {', '.join(persona['core_values'])}\n"
            f"RULES: {', '.join(persona['debate_rules'])}\n"
            f"INSTRUCTION: {round_instructions[round_type]}\n"
            "STYLE CONSTRAINT: Return exactly one paragraph.\n"
            "NEW CHAMBER UPDATES SINCE YOUR LAST TURN:\n"
            f"{updates}\n"
            'Return JSON with keys: display_text, claim, reasoning, attack, question, confidence'
        )

    def _build_judge_prompt(
        self, topic: str, session: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> str:
        transcript = self._summarize_messages(session["messages"], max_items=16)
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

    def _summarize_messages(self, messages: list[dict[str, Any]], max_items: int = 10) -> str:
        if not messages:
            return "No prior turns."
        selected = messages[-max_items:]
        lines = [
            f"{item['round_type']} | {item.get('agent_name', item['agent_id'])} | {_single_paragraph(item['display_text'])}"
            for item in selected
        ]
        return "\n".join(lines)

    def _summarize_messages_since_last_turn(
        self, messages: list[dict[str, Any]], agent_id: str, max_items: int = 8
    ) -> str:
        last_agent_index = -1
        for index, item in enumerate(messages):
            if item["agent_id"] == agent_id:
                last_agent_index = index
        if last_agent_index == -1:
            return self._summarize_messages(messages, max_items=max_items)
        selected = messages[last_agent_index + 1 :][-max_items:]
        if not selected:
            return "No new chamber turns since your last response."
        lines = [
            f"{item['round_type']} | {item.get('agent_name', item['agent_id'])} | {_single_paragraph(item['display_text'])}"
            for item in selected
        ]
        return "\n".join(lines)

    def _normalize_turn_payload(self, raw_text: str, agent_name: str, round_type: str) -> dict[str, Any]:
        payload = _extract_json(raw_text)
        if payload:
            display = payload.get("display_text") or payload.get("claim") or raw_text
            display = _single_paragraph(display)
            return {
                "display_text": display,
                "claim": _single_paragraph(payload.get("claim", display)),
                "reasoning": payload.get("reasoning", []),
                "attack": _single_paragraph(payload.get("attack", "")),
                "question": _single_paragraph(payload.get("question", "")),
                "confidence": float(payload.get("confidence", 0.5)),
                "raw_text": raw_text,
            }
        fallback = _single_paragraph(raw_text or f"{agent_name} produced no output during {round_type}.")
        return {
            "display_text": fallback,
            "claim": fallback,
            "reasoning": [],
            "attack": "",
            "question": "",
            "confidence": 0.5,
            "raw_text": raw_text,
        }

    def _next_round_index(self, session: dict[str, Any]) -> int:
        if not session["rounds"]:
            return 1
        return max(round_item["round_index"] for round_item in session["rounds"]) + 1

    def _spawn(self, session_id: str, coroutine_factory: Callable[[], asyncio.Future | Any]) -> None:
        existing = self._threads.get(session_id)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(
            target=lambda: asyncio.run(coroutine_factory()),
            name=f"debate-session-{session_id}",
            daemon=True,
        )
        self._threads[session_id] = thread
        thread.start()

    async def _noop(self, _: str) -> None:
        return None


def visible_presets() -> list[dict[str, Any]]:
    presets: list[dict[str, Any]] = []
    for preset in PRESET_REGISTRY.values():
        if preset.hidden:
            continue
        payload = preset.model_dump()
        payload["is_available"] = shutil.which(preset.command[0]) is not None
        payload["missing_env_vars"] = [name for name in preset.required_env_vars if not os.environ.get(name)]
        presets.append(payload)
    return presets
