import asyncio
import json
from pathlib import Path

import pytest

from llm_debate_hall.evaluation import (
    EvaluationQuestionSet,
    EvaluationRunner,
    ProviderChoice,
    build_pairwise_prompt,
    load_question_set,
    parse_pairwise_judgment,
    summarize_records,
    write_report,
)


def test_versioned_question_set_has_broad_unique_coverage() -> None:
    question_set = load_question_set(Path("evals/questions_v1.json"))

    assert question_set.version == "v1"
    assert len(question_set.questions) == 30
    assert len({question.id for question in question_set.questions}) == 30
    assert len({question.category for question in question_set.questions}) >= 5


def test_pairwise_prompt_randomizes_sources_without_revealing_them() -> None:
    prompt, label_sources = build_pairwise_prompt(
        "Which answer is stronger?",
        "The first response makes a careful argument.",
        "The second response reaches a practical conclusion.",
        seed=43,
    )

    assert set(label_sources) == {"A", "B"}
    assert set(label_sources.values()) == {"baseline", "council"}
    assert "ANSWER A:" in prompt
    assert "ANSWER B:" in prompt
    assert "baseline" not in prompt.lower()
    assert "council" not in prompt.lower()


def test_pairwise_judgment_requires_complete_scorecard() -> None:
    raw_text = json.dumps(
        {
            "winner_label": "A",
            "rationale": "A is better.",
            "criteria": {
                "correctness": {"scores": {"A": 8, "B": 6}, "notes": "A is more accurate."}
            },
        }
    )

    with pytest.raises(RuntimeError, match="invalid scorecard"):
        parse_pairwise_judgment(raw_text, ProviderChoice("mock", "mock-model"))


def test_mock_evaluation_runner_writes_reproducible_artifacts(tmp_path: Path) -> None:
    full_set = load_question_set(Path("evals/questions_v1.json"))
    question_set = EvaluationQuestionSet(version=full_set.version, questions=full_set.questions[:1])
    generation = ProviderChoice("mock", "mock-model")
    runner = EvaluationRunner(generation=generation, judge=generation, seed=42)

    records = asyncio.run(runner.run(question_set, repetitions=1))

    assert len(records) == 1
    assert records[0].winner in {"baseline", "council", "tie"}
    assert len(records[0].transcript) == 9
    assert records[0].council_metrics["estimated_total_tokens"] > records[0].baseline_metrics["estimated_total_tokens"]
    summary = summarize_records(records)
    assert summary["runs"] == 1
    assert sum(summary["wins"].values()) == 1

    json_path, markdown_path = write_report(
        output_dir=tmp_path,
        question_set=question_set,
        generation=generation,
        judge=generation,
        seed=42,
        repetitions=1,
        records=records,
    )
    assert json_path.exists()
    assert markdown_path.exists()
    assert json.loads(json_path.read_text())["schema_version"] == "evaluation_report_v1"
    assert "Council win rate" in markdown_path.read_text()
