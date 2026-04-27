from __future__ import annotations

from typing import Any

from llm_debate_hall.events import EventBroker
from llm_debate_hall.storage import Storage

MODEL_PRICING_USD_PER_1K_TOKENS: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "gpt-5"): (0.00125, 0.01),
    ("openai", "gpt-5-mini"): (0.00025, 0.002),
    ("openai", "gpt-4.1"): (0.002, 0.008),
    ("openai", "gpt-4.1-mini"): (0.0004, 0.0016),
    ("anthropic", "sonnet"): (0.003, 0.015),
    ("anthropic", "claude-sonnet-4"): (0.003, 0.015),
    ("anthropic", "claude-3-7-sonnet"): (0.003, 0.015),
    ("anthropic", "opus"): (0.015, 0.075),
    ("anthropic", "claude-opus-4.1"): (0.015, 0.075),
}


def estimate_usage(prompt: str, raw_output: str, preset_id: str, model_name: str) -> dict[str, Any]:
    prompt_tokens = _estimate_tokens(prompt)
    output_tokens = _estimate_tokens(raw_output)
    total_tokens = prompt_tokens + output_tokens
    pricing = MODEL_PRICING_USD_PER_1K_TOKENS.get((preset_id, model_name))
    estimated_cost_usd = None
    if pricing:
        prompt_rate, output_rate = pricing
        estimated_cost_usd = round((prompt_tokens * prompt_rate + output_tokens * output_rate) / 1000, 6)
    return {
        "estimate_source": "heuristic",
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }


class TracePublisher:
    def __init__(self, storage: Storage, broker: EventBroker) -> None:
        self.storage = storage
        self.broker = broker

    async def publish(
        self,
        session_id: str,
        *,
        event_type: str,
        round_type: str | None = None,
        round_index: int | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = self.storage.add_trace_event(
            session_id=session_id,
            event_type=event_type,
            round_type=round_type,
            round_index=round_index,
            agent_id=agent_id,
            payload=payload,
        )
        await self.broker.publish(session_id, {"type": "trace_event_saved", "trace_event": event})
        return event


def _estimate_tokens(text: str) -> int:
    cleaned = (text or "").strip()
    if not cleaned:
        return 0
    return max(1, round(len(cleaned) / 4))
