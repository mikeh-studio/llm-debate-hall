import asyncio
import json
from pathlib import Path

import pytest

from llm_debate_hall.adapters.base import AdapterResponse
from llm_debate_hall.events import EventBroker
from llm_debate_hall.judging import JudgeDecisionError, JudgeService
from llm_debate_hall.observability import TracePublisher
from llm_debate_hall.storage import Storage


class CapturingJudgeAdapter:
    def __init__(self, *, malformed: bool = False) -> None:
        self.prompt = ""
        self.malformed = malformed

    async def generate(self, request, on_chunk) -> AdapterResponse:
        self.prompt = request.prompt
        labels = [
            item.strip()
            for line in request.prompt.splitlines()
            if line.startswith("CANDIDATES:")
            for item in line.split(":", 1)[1].split(",")
        ]
        if self.malformed:
            payload = {"winner_label": "Z", "rationale": "Invalid.", "criteria": {}}
        else:
            winner = labels[0]
            payload = {
                "winner_label": winner,
                "rationale": f"Candidate {winner} made the strongest case.",
                "criteria": {
                    criterion: {
                        "scores": {label: (8 if label == winner else 6) for label in labels},
                        "notes": f"Candidate {winner} led on {criterion}.",
                    }
                    for criterion in ("coherence", "responsiveness", "evidence", "style")
                },
            }
        return AdapterResponse(raw_text=json.dumps(payload), stream_status="complete")


def _session_with_transcript(storage: Storage) -> tuple[dict, dict]:
    session = storage.create_session(
        "Should judges evaluate anonymized transcripts?",
        [
            {
                "display_name": "Athena",
                "role": "debater",
                "side": "independent",
                "persona_id": "stoic_rationalist",
                "preset_id": "mock",
                "model_name": "mock-model",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
            {
                "display_name": "Burke",
                "role": "debater",
                "side": "independent",
                "persona_id": "pragmatic_engineer",
                "preset_id": "mock",
                "model_name": "mock-model",
                "command": ["mock"],
                "args_template": [],
                "env": {},
            },
        ],
        {
            "display_name": "Solon",
            "role": "judge",
            "side": "judge",
            "preset_id": "mock",
            "model_name": "mock-model",
            "command": ["mock"],
            "args_template": [],
            "env": {},
        },
    )
    for agent in session["agents"][:2]:
        storage.add_thread_entry(
            session_id=session["id"],
            kind="agent",
            display_name=agent["display_name"],
            display_text=f"{agent['display_name']} makes a distinctive argument.",
            round_type="opening",
            round_index=1,
            agent_id=agent["id"],
            payload={},
        )
    judge = session["agents"][-1]
    return session, judge


def test_judge_uses_blinded_full_scorecard_and_maps_winner(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    session, judge = _session_with_transcript(storage)
    adapter = CapturingJudgeAdapter()
    broker = EventBroker()
    service = JudgeService(
        storage=storage,
        broker=broker,
        adapter_factory=lambda _: adapter,
        trace_publisher=TracePublisher(storage, broker),
    )

    asyncio.run(service.judge_session(session["id"], session["topic"], judge))

    assert "Athena" not in adapter.prompt
    assert "Burke" not in adapter.prompt
    assert "Candidate A" in adapter.prompt
    assert "Candidate B" in adapter.prompt
    score = storage.get_session(session["id"])["judge_score"]
    assert score["winner_agent_id"] in {agent["id"] for agent in session["agents"][:2]}
    assert set(score["criteria"]) == {"coherence", "responsiveness", "evidence", "style"}


def test_invalid_judge_output_fails_instead_of_awarding_first_seat(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "debate.db", personas_root_path=tmp_path / "personas")
    session, judge = _session_with_transcript(storage)
    adapter = CapturingJudgeAdapter(malformed=True)
    broker = EventBroker()
    service = JudgeService(
        storage=storage,
        broker=broker,
        adapter_factory=lambda _: adapter,
        trace_publisher=TracePublisher(storage, broker),
    )

    with pytest.raises(JudgeDecisionError, match="scorecard|criteria|unknown"):
        asyncio.run(service.judge_session(session["id"], session["topic"], judge))

    assert storage.get_session(session["id"])["judge_score"] is None
