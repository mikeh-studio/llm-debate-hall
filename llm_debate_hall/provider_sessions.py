from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from llm_debate_hall.adapters.base import AdapterRequest, AdapterResponse, ChunkCallback, DebateAdapter
from llm_debate_hall.adapters.subprocess_adapter import SubprocessAdapterError
from llm_debate_hall.events import EventBroker
from llm_debate_hall.observability import TracePublisher
from llm_debate_hall.prompts import build_persistent_turn_prompt, build_turn_prompt
from llm_debate_hall.storage import Storage


@dataclass(slots=True)
class TurnGenerationResult:
    response: AdapterResponse
    prompt: str


class ProviderSessionService:
    def __init__(
        self,
        *,
        storage: Storage,
        broker: EventBroker,
        trace_publisher: TracePublisher,
    ) -> None:
        self.storage = storage
        self.broker = broker
        self.trace_publisher = trace_publisher

    async def generate_turn(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        topic: str,
        round_type: str,
        round_index: int,
        agent: dict[str, Any],
        adapter: DebateAdapter,
        personas: list[dict[str, Any]],
        on_chunk: ChunkCallback,
    ) -> TurnGenerationResult:
        provider_session = self.storage.get_provider_session(agent["id"])
        active_provider_session = (
            provider_session
            if provider_session and provider_session["mode"] == "persistent" and provider_session["status"] == "active"
            else None
        )
        prompt = build_turn_prompt(session, topic, agent, round_type, personas)
        request = self._adapter_request(
            session_id=session_id,
            topic=topic,
            round_type=round_type,
            agent=agent,
            prompt=prompt,
        )
        should_try_persistent = adapter.supports_persistent_sessions(request) and (
            provider_session is None or active_provider_session is not None
        )

        if not should_try_persistent:
            return TurnGenerationResult(response=await adapter.generate(request, on_chunk), prompt=request.prompt)

        await self.trace_publisher.publish(
            session_id,
            event_type="provider_session_attempt",
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            payload={
                "summary": (
                    f"{agent['display_name']} attempted a persistent provider session resume."
                    if active_provider_session
                    else f"{agent['display_name']} started a persistent provider session."
                ),
                "provider_session_mode": active_provider_session["mode"] if active_provider_session else "persistent",
                "provider_session_status": active_provider_session["status"] if active_provider_session else "new",
            },
        )
        request.prompt = build_persistent_turn_prompt(
            session=session,
            topic=topic,
            agent=agent,
            round_type=round_type,
            provider_session=active_provider_session,
            personas=personas,
        )

        try:
            persistent_result = await adapter.generate_persistent(
                request,
                active_provider_session["provider_session_id"] if active_provider_session else None,
                on_chunk,
            )
            persisted_session = self.storage.upsert_provider_session(
                session_id=session_id,
                agent_id=agent["id"],
                preset_id=agent["preset_id"],
                provider_session_id=persistent_result.provider_session_id,
                mode="persistent",
                status="active",
            )
            await self._publish_provider_session_state(session_id, agent, persisted_session)
            await self.trace_publisher.publish(
                session_id,
                event_type="provider_session_state",
                round_type=round_type,
                round_index=round_index,
                agent_id=agent["id"],
                payload={
                    "summary": f"{agent['display_name']} is using a persistent provider session.",
                    "provider_session_mode": persisted_session["mode"],
                    "provider_session_status": persisted_session["status"],
                },
            )
            return TurnGenerationResult(response=persistent_result.response, prompt=request.prompt)
        except SubprocessAdapterError as exc:
            if not exc.allow_stateless_fallback:
                raise
            response = await self._fallback_to_stateless(
                session_id=session_id,
                round_type=round_type,
                round_index=round_index,
                agent=agent,
                active_provider_session=active_provider_session,
                request=request,
                stateless_prompt=prompt,
                adapter=adapter,
                on_chunk=on_chunk,
                error=exc,
            )
            return TurnGenerationResult(response=response, prompt=request.prompt)
        except Exception as exc:
            response = await self._fallback_to_stateless(
                session_id=session_id,
                round_type=round_type,
                round_index=round_index,
                agent=agent,
                active_provider_session=active_provider_session,
                request=request,
                stateless_prompt=prompt,
                adapter=adapter,
                on_chunk=on_chunk,
                error=exc,
            )
            return TurnGenerationResult(response=response, prompt=request.prompt)

    async def _fallback_to_stateless(
        self,
        *,
        session_id: str,
        round_type: str,
        round_index: int,
        agent: dict[str, Any],
        active_provider_session: dict[str, Any] | None,
        request: AdapterRequest,
        stateless_prompt: str,
        adapter: DebateAdapter,
        on_chunk: ChunkCallback,
        error: Exception,
    ) -> AdapterResponse:
        fallback_session = self.storage.upsert_provider_session(
            session_id=session_id,
            agent_id=agent["id"],
            preset_id=agent["preset_id"],
            provider_session_id=active_provider_session["provider_session_id"] if active_provider_session else None,
            mode="replay_fallback",
            status="fallback",
            last_error=str(error),
        )
        await self._publish_provider_session_state(session_id, agent, fallback_session)
        await self.trace_publisher.publish(
            session_id,
            event_type="provider_session_fallback",
            round_type=round_type,
            round_index=round_index,
            agent_id=agent["id"],
            payload={
                "summary": f"{agent['display_name']} fell back to stateless replay.",
                "provider_session_mode": fallback_session["mode"],
                "provider_session_status": fallback_session["status"],
                "last_error": str(error),
            },
        )
        request.prompt = stateless_prompt
        return await adapter.generate(request, on_chunk)

    async def _publish_provider_session_state(
        self,
        session_id: str,
        agent: dict[str, Any],
        provider_session: dict[str, Any],
    ) -> None:
        await self.broker.publish(
            session_id,
            {
                "type": "provider_session_state",
                "agent_id": agent["id"],
                "agent_name": agent["display_name"],
                "provider_session": provider_session,
            },
        )

    def _adapter_request(
        self,
        *,
        session_id: str,
        topic: str,
        round_type: str,
        agent: dict[str, Any],
        prompt: str,
    ) -> AdapterRequest:
        return AdapterRequest(
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
