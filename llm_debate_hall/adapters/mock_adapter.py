from __future__ import annotations

import asyncio
import json

from llm_debate_hall.adapters.base import AdapterRequest, AdapterResponse, ChunkCallback, DebateAdapter


class MockDebateAdapter(DebateAdapter):
    async def generate(self, request: AdapterRequest, on_chunk: ChunkCallback) -> AdapterResponse:
        if request.output_mode == "persona":
            persona_id = "stoic_rationalist" if request.agent_name.lower().startswith("a") else "pragmatic_engineer"
            raw_text = json.dumps(
                {
                    "persona_id": persona_id,
                    "justification": f"{request.agent_name} selects {persona_id} for the topic.",
                }
            )
        elif request.output_mode == "question_validation":
            question = self._line_value(request.prompt, "QUESTION:")
            accepted = question.endswith("?") and len(question.split()) >= 4
            raw_text = json.dumps(
                {
                    "accepted": accepted,
                    "reason": (
                        "This works as a debate question."
                        if accepted
                        else "This needs to be a clearer arguable question."
                    ),
                    "suggestions": self._suggestions(question),
                }
            )
        elif request.output_mode == "question_suggestions":
            seed = self._line_value(request.prompt, "SEED:")
            raw_text = json.dumps({"suggestions": self._suggestions(seed)})
        elif request.output_mode == "judge":
            winner = self._winner_from_prompt(request.prompt)
            raw_text = json.dumps(
                {
                    "winner_agent_id": winner,
                    "rationale": "The winning agent was more responsive and internally coherent.",
                    "criteria": {
                        "coherence": {"winner": winner, "notes": "Clearer structure."},
                        "responsiveness": {"winner": winner, "notes": "Addressed the rebuttals directly."},
                    },
                }
            )
        else:
            phase = request.output_mode.replace("_", " ")
            raw_text = json.dumps(
                {
                    "display_text": (
                        f"{request.agent_name} delivers a focused {phase} paragraph that presses one clear claim, "
                        "answers the prior exchange directly, and closes on a concrete line of attack."
                    ),
                    "claim": f"{request.agent_name} makes a focused claim about the topic.",
                    "reasoning": [
                        "The argument defines terms before taking a stance.",
                        "The argument responds directly to the latest opposing point.",
                    ],
                    "attack": "The opponent relies on an under-specified assumption.",
                    "question": "What evidence would make you revise your position?",
                    "confidence": 0.72,
                }
            )

        display_text = json.loads(raw_text).get("display_text", raw_text)
        for start in range(0, len(display_text), 24):
            await on_chunk(display_text[start : start + 24])
            await asyncio.sleep(0.005)
        return AdapterResponse(raw_text=raw_text, stream_status="simulated")

    def _winner_from_prompt(self, prompt: str) -> str:
        for line in prompt.splitlines():
            if line.startswith("CANDIDATES:"):
                parts = [item.strip() for item in line.split(":", 1)[1].split(",") if item.strip()]
                return parts[0] if parts else "unknown"
        return "unknown"

    def _line_value(self, prompt: str, prefix: str) -> str:
        for line in prompt.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip()
        return ""

    def _suggestions(self, seed: str) -> list[str]:
        base = seed.rstrip("?") or "autonomous agents"
        return [
            f"Should {base} be given more autonomy?",
            f"What is the strongest case against {base}?",
            f"How should teams govern decisions involving {base}?",
        ]
