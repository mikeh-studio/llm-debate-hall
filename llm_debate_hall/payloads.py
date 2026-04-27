from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def required_json(raw_text: str, *, context: str, preset_id: str, model_name: str) -> dict[str, Any]:
    payload = extract_json(raw_text)
    if payload is None:
        raise RuntimeError(f"{context} returned invalid JSON for {preset_id}:{model_name}.")
    return payload


def single_paragraph(text: str) -> str:
    cleaned = " ".join(part.strip() for part in text.replace("\r", "\n").splitlines() if part.strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_turn_payload(raw_text: str, agent_name: str, round_type: str) -> dict[str, Any]:
    payload = extract_json(raw_text)
    if not payload:
        detail = single_paragraph(raw_text)
        suffix = f" {detail}" if detail else ""
        raise RuntimeError(f"{agent_name} produced invalid turn output during {round_type}.{suffix}")
    display = payload.get("display_text") or payload.get("claim") or raw_text
    display = single_paragraph(display)
    return {
        "display_text": display,
        "claim": single_paragraph(payload.get("claim", display)),
        "reasoning": payload.get("reasoning", []),
        "attack": single_paragraph(payload.get("attack", "")),
        "question": single_paragraph(payload.get("question", "")),
        "confidence": float(payload.get("confidence", 0.5)),
        "raw_text": raw_text,
    }
