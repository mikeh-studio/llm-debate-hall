from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Callable

from llm_debate_hall.adapters.base import DebateAdapter
from llm_debate_hall.adapters.mock_adapter import MockDebateAdapter
from llm_debate_hall.adapters.subprocess_adapter import SubprocessDebateAdapter
from llm_debate_hall.events import EventBroker
from llm_debate_hall.judging import JudgeService
from llm_debate_hall.model_catalog import active_models_for_preset, visible_presets
from llm_debate_hall.observability import (
    MODEL_PRICING_USD_PER_1K_TOKENS,
    TracePublisher,
    estimate_usage,
)
from llm_debate_hall.payloads import (
    extract_json,
    normalize_turn_payload,
    required_json,
    single_paragraph,
)
from llm_debate_hall.persona_selection import PersonaSelectionService
from llm_debate_hall.prompts import (
    PERSONA_INTENSITY_DEFAULT,
    PERSONA_INTENSITY_MAX,
    PERSONA_INTENSITY_MIN,
    build_judge_prompt,
    build_persistent_turn_prompt,
    build_persona_prompt,
    build_turn_prompt,
    conversation_entries,
    persona_intensity_guidance,
    persona_intensity_value,
    persona_selection_text,
    summarize_messages,
    summarize_messages_since_last_turn,
)
from llm_debate_hall.provider_sessions import ProviderSessionService
from llm_debate_hall.storage import Storage

REPLY_ROUNDS_PER_CYCLE = 2
REPLY_ROUNDS_PER_CYCLE_BY_MODE = {
    "serious": 2,
    "theater": 2,
    "conversational": 4,
}
PERSONA_SELECTION_STATUS = "selecting_personas"

# Compatibility aliases for older tests and local imports that reached into engine internals.
_extract_json = extract_json
_single_paragraph = single_paragraph
_required_json = required_json
_estimate_usage = estimate_usage
_persona_intensity_value = persona_intensity_value
_persona_intensity_guidance = persona_intensity_guidance


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
        self.trace_publisher = TracePublisher(storage, broker)
        self.persona_selection_service = PersonaSelectionService(
            storage=storage,
            broker=broker,
            adapter_factory=self.adapter_factory,
        )
        self.provider_session_service = ProviderSessionService(
            storage=storage,
            broker=broker,
            trace_publisher=self.trace_publisher,
        )
        self.judge_service = JudgeService(
            storage=storage,
            broker=broker,
            adapter_factory=self.adapter_factory,
            trace_publisher=self.trace_publisher,
        )
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
        try:
            session = self.storage.get_session(session_id)
            selectable_personas = self.storage.get_selectable_personas()
            agents = [agent for agent in session["agents"] if agent["role"] == "debater"]
            auto_agents = [agent for agent in agents if not agent.get("persona_id")]

            if auto_agents:
                await self._set_session_status(session_id, PERSONA_SELECTION_STATUS)
                entry = self.storage.add_thread_entry(
                    session_id=session_id,
                    kind="system",
                    display_name="Council",
                    display_text=self._persona_selection_text(auto_agents),
                    payload={"event": "persona_selection_started"},
                )
                await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": entry})
                await self._select_personas(session, agents, selectable_personas)
                session = self.storage.get_session(session_id)
                agents = [agent for agent in session["agents"] if agent["role"] == "debater"]

            await self._set_session_status(session_id, "running")
            next_round_index = self._next_round_index(session)
            if not session["messages"]:
                next_round_index = await self._play_round(
                    session_id=session_id,
                    topic=session["topic"],
                    agents=agents,
                    round_type="opening",
                    round_index=next_round_index,
                )

            for _ in range(self._reply_rounds_per_cycle(session)):
                next_round_index = await self._play_round(
                    session_id=session_id,
                    topic=session["topic"],
                    agents=agents,
                    round_type="reply",
                    round_index=next_round_index,
                )

            await self._set_session_status(session_id, "awaiting_continue")
        except Exception as exc:
            await self._publish_trace_event(
                session_id,
                event_type="session_failed",
                payload={"summary": str(exc)},
            )
            entry = self.storage.add_thread_entry(
                session_id=session_id,
                kind="system",
                display_name="Council",
                display_text=str(exc),
                payload={"event": "session_failed"},
            )
            await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": entry})
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
        await self._publish_trace_event(
            session_id,
            event_type="round_started",
            round_type=round_type,
            round_index=round_index,
            payload={"summary": f"Round {round_index} started: {round_type}."},
        )
        thread_entry = self.storage.add_thread_entry(
            session_id=session_id,
            kind="system",
            display_name="Council",
            display_text=f"Round {round_index} started: {round_type}.",
            round_type=round_type,
            round_index=round_index,
            payload={"event": "round_started"},
        )
        await self.broker.publish(
            session_id,
            {"type": "round_started", "round_type": round_type, "round_index": round_index},
        )
        await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": thread_entry})
        for agent in agents:
            await self._run_turn(
                session_id=session_id,
                topic=topic,
                round_type=round_type,
                round_index=round_index,
                agent=agent,
            )
        self.storage.complete_round(round_id)
        await self._publish_trace_event(
            session_id,
            event_type="round_completed",
            round_type=round_type,
            round_index=round_index,
            payload={"summary": f"Round {round_index} completed: {round_type}."},
        )
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
        await self.persona_selection_service.select_personas(session, agents, selectable_personas)

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
        thread_entry_id = uuid.uuid4().hex
        turn_started_at = time.perf_counter()
        await self._publish_trace_event(
            session_id,
            event_type="turn_started",
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            payload={
                "summary": f"{agent['display_name']} started a {round_type} turn.",
                "preset_id": agent["preset_id"],
                "model_name": agent["model_name"],
                "persona_id": agent["persona_id"],
                "persona_intensity": persona_intensity_value(agent),
            },
        )
        first_chunk_latency_ms: int | None = None

        async def on_chunk(chunk: str) -> None:
            nonlocal first_chunk_latency_ms
            if first_chunk_latency_ms is None:
                first_chunk_latency_ms = round((time.perf_counter() - turn_started_at) * 1000)
                await self._publish_trace_event(
                    session_id,
                    event_type="turn_first_chunk",
                    round_type=round_type,
                    round_index=round_index,
                    agent_id=agent["id"],
                    payload={
                        "summary": f"{agent['display_name']} started streaming.",
                        "latency_ms": first_chunk_latency_ms,
                    },
                )
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
            await self.broker.publish(
                session_id,
                {
                    "type": "thread_entry_chunk",
                    "entry_id": thread_entry_id,
                    "kind": "agent",
                    "display_name": agent["display_name"],
                    "round_type": round_type,
                    "round_index": round_index,
                    "agent_id": agent["id"],
                    "chunk": chunk,
                },
            )

        generation = await self.provider_session_service.generate_turn(
            session_id=session_id,
            session=session,
            topic=topic,
            round_type=round_type,
            round_index=round_index,
            agent=agent,
            adapter=adapter,
            personas=self.storage.list_personas(),
            on_chunk=on_chunk,
        )
        response = generation.response
        payload = normalize_turn_payload(response.raw_text, agent["display_name"], round_type)
        usage = estimate_usage(generation.prompt, response.raw_text, agent["preset_id"], agent["model_name"])
        latency_ms = round((time.perf_counter() - turn_started_at) * 1000)
        current_provider_session = self.storage.get_provider_session(agent["id"])
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
        thread_entry = self.storage.add_thread_entry(
            session_id=session_id,
            kind="agent",
            display_name=agent["display_name"],
            display_text=payload["display_text"],
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            payload=payload,
            entry_id=thread_entry_id,
        )
        await self._publish_trace_event(
            session_id,
            event_type="turn_completed",
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            payload={
                "summary": f"{agent['display_name']} completed a {round_type} turn.",
                "preset_id": agent["preset_id"],
                "model_name": agent["model_name"],
                "stream_status": response.stream_status,
                "latency_ms": latency_ms,
                "first_chunk_latency_ms": first_chunk_latency_ms,
                "persona_id": agent["persona_id"],
                "persona_intensity": persona_intensity_value(agent),
                "provider_session_mode": current_provider_session["mode"] if current_provider_session else "stateless",
                "provider_session_status": current_provider_session["status"] if current_provider_session else "stateless",
                "used_fallback": bool(current_provider_session and current_provider_session["status"] == "fallback"),
                **usage,
            },
        )
        await self.broker.publish(
            session_id,
            {"type": "message_saved", "message": {**message, "agent_name": agent["display_name"]}},
        )
        await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": thread_entry})

    async def _judge_session(self, session_id: str, topic: str, judge: dict[str, Any]) -> None:
        await self.judge_service.judge_session(session_id, topic, judge)

    def _build_persona_prompt(
        self,
        topic: str,
        agent: dict[str, Any],
        selectable_personas: list[dict[str, Any]],
    ) -> str:
        return build_persona_prompt(topic, agent, selectable_personas)

    def _build_turn_prompt(
        self,
        session: dict[str, Any],
        topic: str,
        agent: dict[str, Any],
        round_type: str,
    ) -> str:
        return build_turn_prompt(session, topic, agent, round_type, self.storage.list_personas())

    def _build_persistent_turn_prompt(
        self,
        *,
        session: dict[str, Any],
        topic: str,
        agent: dict[str, Any],
        round_type: str,
        provider_session: dict[str, Any] | None,
    ) -> str:
        return build_persistent_turn_prompt(
            session=session,
            topic=topic,
            agent=agent,
            round_type=round_type,
            provider_session=provider_session,
            personas=self.storage.list_personas(),
        )

    def _build_judge_prompt(
        self,
        topic: str,
        session: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> str:
        return build_judge_prompt(topic, session, candidates)

    def _conversation_entries(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        return conversation_entries(session)

    def _summarize_messages(self, session: dict[str, Any], max_items: int = 10) -> str:
        return summarize_messages(session, max_items=max_items)

    def _summarize_messages_since_last_turn(
        self,
        session: dict[str, Any],
        agent_id: str,
        max_items: int = 8,
    ) -> str:
        return summarize_messages_since_last_turn(session, agent_id, max_items=max_items)

    def _normalize_turn_payload(self, raw_text: str, agent_name: str, round_type: str) -> dict[str, Any]:
        return normalize_turn_payload(raw_text, agent_name, round_type)

    def _next_round_index(self, session: dict[str, Any]) -> int:
        if not session["rounds"]:
            return 1
        return max(round_item["round_index"] for round_item in session["rounds"]) + 1

    def _reply_rounds_per_cycle(self, session: dict[str, Any]) -> int:
        return REPLY_ROUNDS_PER_CYCLE_BY_MODE.get(session.get("debate_mode"), REPLY_ROUNDS_PER_CYCLE)

    def _persona_selection_text(self, auto_agents: list[dict[str, Any]]) -> str:
        return persona_selection_text(auto_agents)

    async def _set_session_status(self, session_id: str, status: str) -> None:
        self.storage.update_session_status(session_id, status)
        await self.broker.publish(session_id, {"type": "status", "status": status})

    async def _publish_trace_event(
        self,
        session_id: str,
        *,
        event_type: str,
        round_type: str | None = None,
        round_index: int | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.trace_publisher.publish(
            session_id,
            event_type=event_type,
            round_type=round_type,
            round_index=round_index,
            agent_id=agent_id,
            payload=payload,
        )

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
