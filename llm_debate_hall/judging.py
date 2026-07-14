from __future__ import annotations

import random
from typing import Any, Callable

from pydantic import ValidationError

from llm_debate_hall.adapters.base import AdapterRequest, DebateAdapter
from llm_debate_hall.events import EventBroker
from llm_debate_hall.observability import TracePublisher
from llm_debate_hall.models import JudgePayload
from llm_debate_hall.payloads import required_json
from llm_debate_hall.prompts import JUDGE_CRITERIA, build_judge_prompt
from llm_debate_hall.storage import Storage


class JudgeDecisionError(RuntimeError):
    pass


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
        shuffled_candidates = list(candidates)
        random.Random(session_id).shuffle(shuffled_candidates)
        labels = [chr(ord("A") + index) for index in range(len(shuffled_candidates))]
        label_by_agent_id = {
            agent["id"]: label for agent, label in zip(shuffled_candidates, labels, strict=True)
        }
        agent_id_by_label = {label: agent_id for agent_id, label in label_by_agent_id.items()}
        prompt = build_judge_prompt(topic, session, label_by_agent_id)
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
        try:
            raw_payload = required_json(
                response.raw_text,
                context=f"{judge['display_name']} judge decision",
                preset_id=judge["preset_id"],
                model_name=judge["model_name"],
            )
        except RuntimeError as exc:
            raise JudgeDecisionError(str(exc)) from exc
        payload = _validated_judge_payload(raw_payload, set(agent_id_by_label))
        winner_agent_id = agent_id_by_label[payload.winner_label]
        criteria = {
            criterion: {
                "scores": {
                    agent_id_by_label[label]: score
                    for label, score in criterion_payload.scores.items()
                },
                "notes": criterion_payload.notes,
            }
            for criterion, criterion_payload in payload.criteria.items()
        }
        score = self.storage.add_judge_score(
            session_id=session_id,
            judge_agent_id=judge["id"],
            winner_agent_id=winner_agent_id,
            rationale=payload.rationale,
            criteria=criteria,
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


def _validated_judge_payload(raw_payload: dict[str, Any], labels: set[str]) -> JudgePayload:
    try:
        payload = JudgePayload.model_validate(raw_payload)
    except ValidationError as exc:
        raise JudgeDecisionError(f"Judge returned an invalid scorecard: {exc}") from exc
    if payload.winner_label not in labels:
        raise JudgeDecisionError(
            f"Judge selected unknown blinded label '{payload.winner_label}'. Expected one of: {', '.join(sorted(labels))}."
        )
    missing_criteria = set(JUDGE_CRITERIA) - set(payload.criteria)
    extra_criteria = set(payload.criteria) - set(JUDGE_CRITERIA)
    if missing_criteria or extra_criteria:
        raise JudgeDecisionError(
            "Judge criteria must be exactly: " + ", ".join(JUDGE_CRITERIA) + "."
        )
    for criterion, scorecard in payload.criteria.items():
        if set(scorecard.scores) != labels:
            raise JudgeDecisionError(
                f"Judge criterion '{criterion}' must score every blinded candidate exactly once."
            )
    return payload
