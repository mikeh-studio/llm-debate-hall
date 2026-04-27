from __future__ import annotations

from typing import Any, Callable

from llm_debate_hall.adapters.base import AdapterRequest, DebateAdapter
from llm_debate_hall.events import EventBroker
from llm_debate_hall.observability import TracePublisher
from llm_debate_hall.payloads import required_json
from llm_debate_hall.prompts import build_judge_prompt
from llm_debate_hall.storage import Storage


class JudgeService:
    def __init__(
        self,
        *,
        storage: Storage,
        broker: EventBroker,
        adapter_factory: Callable[[dict[str, Any]], DebateAdapter],
        trace_publisher: TracePublisher,
    ) -> None:
        self.storage = storage
        self.broker = broker
        self.adapter_factory = adapter_factory
        self.trace_publisher = trace_publisher

    async def judge_session(self, session_id: str, topic: str, judge: dict[str, Any]) -> None:
        session = self.storage.get_session(session_id)
        candidates = [agent for agent in session["agents"] if agent["role"] == "debater"]
        prompt = build_judge_prompt(topic, session, candidates)
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
        response = await adapter.generate(request, _noop)
        payload = required_json(
            response.raw_text,
            context=f"{judge['display_name']} judge decision",
            preset_id=judge["preset_id"],
            model_name=judge["model_name"],
        )
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
        thread_entry = self.storage.add_thread_entry(
            session_id=session_id,
            kind="judge",
            display_name=judge["display_name"],
            display_text=score["rationale"],
            payload={
                "winner_agent_id": score["winner_agent_id"],
                "criteria": score["criteria"],
                "raw_text": response.raw_text,
            },
        )
        await self.trace_publisher.publish(
            session_id,
            event_type="judge_completed",
            agent_id=judge["id"],
            payload={
                "summary": f"{judge['display_name']} selected a winner.",
                "preset_id": judge["preset_id"],
                "model_name": judge["model_name"],
                "winner_agent_id": score["winner_agent_id"],
            },
        )
        await self.broker.publish(session_id, {"type": "judge_result", "judge_score": score})
        await self.broker.publish(session_id, {"type": "thread_entry_saved", "entry": thread_entry})


async def _noop(_: str) -> None:
    return None
