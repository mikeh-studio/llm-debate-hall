from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from llm_debate_hall.adapters.base import PRESET_REGISTRY, AdapterRequest, DebateAdapter
from llm_debate_hall.adapters.mock_adapter import MockDebateAdapter
from llm_debate_hall.adapters.subprocess_adapter import SubprocessDebateAdapter
from llm_debate_hall.engine import DebateEngine
from llm_debate_hall.events import EventBroker
from llm_debate_hall.observability import estimate_usage
from llm_debate_hall.payloads import required_json, single_paragraph
from llm_debate_hall.storage import Storage


DEFAULT_QUESTIONS_PATH = Path(__file__).resolve().parent.parent / "evals/questions_v1.json"
DEFAULT_OUTPUT_DIR = Path("artifacts/evaluations")
DEBATER_BLUEPRINTS = (
    ("Athena", "stoic_rationalist"),
    ("Byron", "pragmatic_engineer"),
    ("Cicero", "humanist_mediator"),
)
PAIRWISE_CRITERIA = ("correctness", "completeness", "reasoning", "clarity")


class EvaluationQuestion(BaseModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    question: str = Field(min_length=8)


class EvaluationQuestionSet(BaseModel):
    version: str = Field(min_length=1)
    questions: list[EvaluationQuestion] = Field(min_length=1)

    @field_validator("questions")
    @classmethod
    def unique_question_ids(cls, questions: list[EvaluationQuestion]) -> list[EvaluationQuestion]:
        ids = [question.id for question in questions]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation question ids must be unique")
        return questions


class PairwiseCriterion(BaseModel):
    scores: dict[str, float]
    notes: str = Field(min_length=1)

    @field_validator("scores")
    @classmethod
    def valid_scores(cls, scores: dict[str, float]) -> dict[str, float]:
        if set(scores) != {"A", "B"}:
            raise ValueError("pairwise criteria must score A and B exactly once")
        if any(score < 0 or score > 10 for score in scores.values()):
            raise ValueError("pairwise scores must be between 0 and 10")
        return scores


class PairwiseJudgment(BaseModel):
    winner_label: Literal["A", "B", "tie"]
    rationale: str = Field(min_length=1)
    criteria: dict[str, PairwiseCriterion]

    @field_validator("criteria")
    @classmethod
    def exact_criteria(cls, criteria: dict[str, PairwiseCriterion]) -> dict[str, PairwiseCriterion]:
        if set(criteria) != set(PAIRWISE_CRITERIA):
            raise ValueError("pairwise criteria must be exactly: " + ", ".join(PAIRWISE_CRITERIA))
        return criteria


@dataclass(frozen=True, slots=True)
class ProviderChoice:
    preset_id: str
    model_name: str


@dataclass(slots=True)
class GenerationResult:
    text: str
    latency_ms: int
    usage: dict[str, Any]


@dataclass(slots=True)
class EvaluationRecord:
    question_id: str
    category: str
    question: str
    repetition: int
    seed: int
    baseline_answer: str
    council_answer: str
    transcript: list[dict[str, Any]]
    winner: str
    judgment: dict[str, Any]
    label_sources: dict[str, str]
    baseline_metrics: dict[str, Any]
    council_metrics: dict[str, Any]
    judge_metrics: dict[str, Any]


def load_question_set(path: Path) -> EvaluationQuestionSet:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load evaluation questions from {path}: {exc}") from exc
    try:
        return EvaluationQuestionSet.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid evaluation question set {path}: {exc}") from exc


def build_pairwise_prompt(
    question: str,
    baseline_answer: str,
    council_answer: str,
    *,
    seed: int,
) -> tuple[str, dict[str, str]]:
    candidates = [("baseline", baseline_answer), ("council", council_answer)]
    random.Random(seed).shuffle(candidates)
    label_sources = {label: source for label, (source, _) in zip(("A", "B"), candidates, strict=True)}
    answer_by_label = {label: answer for label, (_, answer) in zip(("A", "B"), candidates, strict=True)}
    score_shape = '"A": 0, "B": 0'
    prompt = (
        "Compare two anonymized answers. Do not infer which system produced either answer.\n"
        f"QUESTION: {question}\n"
        "CANDIDATES: A, B\n"
        f"ANSWER A:\n{answer_by_label['A']}\n"
        f"ANSWER B:\n{answer_by_label['B']}\n"
        f"CRITERIA: {', '.join(PAIRWISE_CRITERIA)}\n"
        "Score both answers from 0 to 10 on every criterion. Use winner_label A, B, or tie.\n"
        "Return only JSON with this shape: "
        f'{{"winner_label":"A", "rationale":"...", "criteria":'
        f'{{"correctness":{{"scores":{{{score_shape}}},"notes":"..."}},'
        f'"completeness":{{"scores":{{{score_shape}}},"notes":"..."}},'
        f'"reasoning":{{"scores":{{{score_shape}}},"notes":"..."}},'
        f'"clarity":{{"scores":{{{score_shape}}},"notes":"..."}}}}}}'
    )
    return prompt, label_sources


def parse_pairwise_judgment(raw_text: str, provider: ProviderChoice) -> PairwiseJudgment:
    payload = required_json(
        raw_text,
        context="Pairwise evaluation judge",
        preset_id=provider.preset_id,
        model_name=provider.model_name,
    )
    try:
        return PairwiseJudgment.model_validate(payload)
    except ValidationError as exc:
        raise RuntimeError(f"Pairwise evaluation judge returned an invalid scorecard: {exc}") from exc


class EvaluationRunner:
    def __init__(
        self,
        *,
        generation: ProviderChoice,
        judge: ProviderChoice,
        seed: int,
    ) -> None:
        _validate_provider_choice(generation)
        _validate_provider_choice(judge)
        self.generation = generation
        self.judge = judge
        self.seed = seed

    async def run(
        self,
        question_set: EvaluationQuestionSet,
        *,
        repetitions: int,
        limit: int | None = None,
    ) -> list[EvaluationRecord]:
        if repetitions < 1:
            raise ValueError("repetitions must be at least 1")
        questions = question_set.questions[:limit] if limit else question_set.questions
        records: list[EvaluationRecord] = []
        with TemporaryDirectory(prefix="multi-agent-council-eval-") as temp_dir:
            storage = Storage(
                Path(temp_dir) / "evaluation.db",
                personas_root_path=Path(temp_dir) / "personas",
            )
            engine = DebateEngine(storage=storage, broker=EventBroker())
            for question_index, question in enumerate(questions):
                for repetition in range(1, repetitions + 1):
                    run_seed = self.seed + question_index * 10_000 + repetition
                    records.append(
                        await self._run_one(
                            storage=storage,
                            engine=engine,
                            question=question,
                            repetition=repetition,
                            run_seed=run_seed,
                        )
                    )
        return records

    async def _run_one(
        self,
        *,
        storage: Storage,
        engine: DebateEngine,
        question: EvaluationQuestion,
        repetition: int,
        run_seed: int,
    ) -> EvaluationRecord:
        baseline_prompt = (
            "Answer the question directly and independently. Give a self-contained, accurate answer with explicit "
            "reasoning, important uncertainty, and a practical conclusion. Do not mention debates or other agents.\n"
            f"QUESTION: {question.question}"
        )
        baseline = await self._generate(
            provider=self.generation,
            prompt=baseline_prompt,
            output_mode="evaluation_baseline",
            agent_name="Direct Baseline",
            run_id=f"{question.id}-{repetition}-baseline",
        )

        personas = list(DEBATER_BLUEPRINTS)
        random.Random(run_seed).shuffle(personas)
        preset = PRESET_REGISTRY[self.generation.preset_id]
        agents = [
            {
                "display_name": name,
                "role": "debater",
                "side": "independent",
                "sentiment": "exploratory",
                "persona_id": persona_id,
                "preset_id": self.generation.preset_id,
                "model_name": self.generation.model_name,
                "command": list(preset.command),
                "args_template": list(preset.args_template),
                "env": {},
            }
            for name, persona_id in personas
        ]
        session = storage.create_session(
            question.question,
            agents,
            {
                "display_name": "Evaluation Judge",
                "role": "judge",
                "side": "judge",
                "sentiment": "mediating",
                "preset_id": self.judge.preset_id,
                "model_name": self.judge.model_name,
                "command": list(PRESET_REGISTRY[self.judge.preset_id].command),
                "args_template": list(PRESET_REGISTRY[self.judge.preset_id].args_template),
                "env": {},
            },
            debate_mode="serious",
            topic_type=question.category,
            topic_tags=["evaluation", question_set_tag(question.id)],
        )
        await engine.run_segment(session["id"])
        completed_session = storage.get_session(session["id"])
        transcript = [
            {
                "round_type": message["round_type"],
                "round_index": message["round_index"],
                "agent_name": message["agent_name"],
                "display_text": message["display_text"],
            }
            for message in completed_session["messages"]
        ]
        transcript_text = "\n".join(
            f"{item['round_type']} {item['round_index']} | {item['agent_name']} | {item['display_text']}"
            for item in transcript
        )
        synthesis_prompt = (
            "Synthesize the strongest answer to the question from the council transcript. Resolve disagreements, "
            "retain useful uncertainty, and return one self-contained answer. Do not mention the council or speakers.\n"
            f"QUESTION: {question.question}\nTRANSCRIPT:\n{transcript_text}"
        )
        synthesis = await self._generate(
            provider=self.generation,
            prompt=synthesis_prompt,
            output_mode="evaluation_synthesis",
            agent_name="Council Synthesizer",
            run_id=f"{question.id}-{repetition}-synthesis",
        )

        judge_prompt, label_sources = build_pairwise_prompt(
            question.question,
            baseline.text,
            synthesis.text,
            seed=run_seed,
        )
        judge_result = await self._generate(
            provider=self.judge,
            prompt=judge_prompt,
            output_mode="evaluation_pairwise_judge",
            agent_name="Blinded Pairwise Judge",
            run_id=f"{question.id}-{repetition}-judge",
        )
        judgment = parse_pairwise_judgment(judge_result.text, self.judge)
        winner = "tie" if judgment.winner_label == "tie" else label_sources[judgment.winner_label]
        council_turn_metrics = _council_metrics(completed_session)
        return EvaluationRecord(
            question_id=question.id,
            category=question.category,
            question=question.question,
            repetition=repetition,
            seed=run_seed,
            baseline_answer=baseline.text,
            council_answer=synthesis.text,
            transcript=transcript,
            winner=winner,
            judgment=judgment.model_dump(),
            label_sources=label_sources,
            baseline_metrics={"latency_ms": baseline.latency_ms, **baseline.usage},
            council_metrics=_combine_metrics(council_turn_metrics, synthesis),
            judge_metrics={"latency_ms": judge_result.latency_ms, **judge_result.usage},
        )

    async def _generate(
        self,
        *,
        provider: ProviderChoice,
        prompt: str,
        output_mode: str,
        agent_name: str,
        run_id: str,
    ) -> GenerationResult:
        preset = PRESET_REGISTRY[provider.preset_id]
        adapter: DebateAdapter = MockDebateAdapter() if provider.preset_id == "mock" else SubprocessDebateAdapter()
        started = time.perf_counter()
        response = await adapter.generate(
            AdapterRequest(
                session_id=run_id,
                agent_id=run_id,
                agent_name=agent_name,
                preset_id=provider.preset_id,
                role="evaluation",
                side="independent",
                topic=prompt,
                prompt=prompt,
                output_mode=output_mode,
                model_name=provider.model_name,
                command=list(preset.command),
                args_template=list(preset.args_template),
                env={},
            ),
            _noop,
        )
        return GenerationResult(
            text=single_paragraph(response.raw_text),
            latency_ms=round((time.perf_counter() - started) * 1000),
            usage=estimate_usage(prompt, response.raw_text, provider.preset_id, provider.model_name),
        )


async def _noop(_: str) -> None:
    return None


def question_set_tag(question_id: str) -> str:
    return f"eval:{question_id}"


def _validate_provider_choice(choice: ProviderChoice) -> None:
    preset = PRESET_REGISTRY.get(choice.preset_id)
    if preset is None:
        raise ValueError(f"Unknown provider preset: {choice.preset_id}")
    if not choice.model_name.strip():
        raise ValueError(f"Model name is required for preset {choice.preset_id}")
    if preset.requires_command_override:
        raise ValueError(
            f"Evaluation does not run unsafe command overrides. Preset '{choice.preset_id}' needs a verified built-in invocation first."
        )


def _council_metrics(session: dict[str, Any]) -> dict[str, Any]:
    completed = [event for event in session.get("trace_events", []) if event["event_type"] == "turn_completed"]
    payloads = [event.get("payload", {}) for event in completed]
    costs = [payload.get("estimated_cost_usd") for payload in payloads]
    return {
        "latency_ms": sum(int(payload.get("latency_ms") or 0) for payload in payloads),
        "estimated_prompt_tokens": sum(int(payload.get("estimated_prompt_tokens") or 0) for payload in payloads),
        "estimated_output_tokens": sum(int(payload.get("estimated_output_tokens") or 0) for payload in payloads),
        "estimated_total_tokens": sum(int(payload.get("estimated_total_tokens") or 0) for payload in payloads),
        "estimated_cost_usd": None if any(cost is None for cost in costs) else round(sum(costs), 6),
    }


def _combine_metrics(turn_metrics: dict[str, Any], synthesis: GenerationResult) -> dict[str, Any]:
    combined: dict[str, Any] = {
        "latency_ms": turn_metrics["latency_ms"] + synthesis.latency_ms,
    }
    for key in ("estimated_prompt_tokens", "estimated_output_tokens", "estimated_total_tokens"):
        combined[key] = int(turn_metrics.get(key) or 0) + int(synthesis.usage.get(key) or 0)
    turn_cost = turn_metrics.get("estimated_cost_usd")
    synthesis_cost = synthesis.usage.get("estimated_cost_usd")
    combined["estimated_cost_usd"] = (
        None if turn_cost is None or synthesis_cost is None else round(turn_cost + synthesis_cost, 6)
    )
    return combined


def summarize_records(records: list[EvaluationRecord]) -> dict[str, Any]:
    counts = {source: sum(record.winner == source for record in records) for source in ("baseline", "council", "tie")}
    decisive = counts["baseline"] + counts["council"]
    council_rate = counts["council"] / decisive if decisive else None
    interval = _wilson_interval(counts["council"], decisive) if decisive else (None, None)
    return {
        "runs": len(records),
        "wins": counts,
        "decisive_runs": decisive,
        "council_win_rate": council_rate,
        "council_win_rate_95ci": list(interval),
        "average_estimated_tokens": {
            "baseline": _average_metric(records, "baseline_metrics", "estimated_total_tokens"),
            "council": _average_metric(records, "council_metrics", "estimated_total_tokens"),
        },
        "average_estimated_cost_usd": {
            "baseline": _average_metric(records, "baseline_metrics", "estimated_cost_usd"),
            "council": _average_metric(records, "council_metrics", "estimated_cost_usd"),
        },
    }


def _average_metric(records: list[EvaluationRecord], section: str, metric: str) -> float | None:
    values = [getattr(record, section).get(metric) for record in records]
    present = [float(value) for value in values if value is not None]
    return round(sum(present) / len(present), 6) if present else None


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total) / denominator
    return round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)


def write_report(
    *,
    output_dir: Path,
    question_set: EvaluationQuestionSet,
    generation: ProviderChoice,
    judge: ProviderChoice,
    seed: int,
    repetitions: int,
    records: list[EvaluationRecord],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"evaluation-{question_set.version}-{timestamp}"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    summary = summarize_records(records)
    payload = {
        "schema_version": "evaluation_report_v1",
        "created_at": datetime.now(UTC).isoformat(),
        "question_set_version": question_set.version,
        "generation": asdict(generation),
        "judge": asdict(judge),
        "seed": seed,
        "repetitions": repetitions,
        "summary": summary,
        "records": [asdict(record) for record in records],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    ci_low, ci_high = summary["council_win_rate_95ci"]
    rate = summary["council_win_rate"]
    rate_text = "n/a" if rate is None else f"{rate:.1%} ({ci_low:.1%}-{ci_high:.1%})"
    rows = "\n".join(
        f"| {record.question_id} | {record.repetition} | {record.winner} | "
        f"{record.baseline_metrics.get('estimated_total_tokens')} | "
        f"{record.council_metrics.get('estimated_total_tokens')} |"
        for record in records
    )
    markdown_path.write_text(
        "\n".join(
            [
                f"# Multi-Agent Council Evaluation - {question_set.version}",
                "",
                f"- Generation: `{generation.preset_id}:{generation.model_name}`",
                f"- Judge: `{judge.preset_id}:{judge.model_name}`",
                f"- Seed: `{seed}`",
                f"- Runs: `{len(records)}`",
                f"- Council win rate on decisive runs: `{rate_text}`",
                f"- Wins: `{summary['wins']}`",
                "",
                "| Question | Repeat | Winner | Baseline tokens | Council tokens |",
                "|---|---:|---|---:|---:|",
                rows,
                "",
                "The interval is a Wilson 95% confidence interval over decisive pairwise judgments. "
                "Token and cost values are heuristic estimates.",
            ]
        ),
        encoding="utf-8",
    )
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare direct answers with Multi-Agent Council answers.")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--preset", default="mock")
    parser.add_argument("--model", default="mock-model")
    parser.add_argument("--judge-preset")
    parser.add_argument("--judge-model")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    question_set = load_question_set(args.questions)
    generation = ProviderChoice(args.preset, args.model)
    judge = ProviderChoice(args.judge_preset or args.preset, args.judge_model or args.model)
    runner = EvaluationRunner(generation=generation, judge=judge, seed=args.seed)
    records = asyncio.run(runner.run(question_set, repetitions=args.repetitions, limit=args.limit))
    json_path, markdown_path = write_report(
        output_dir=args.output_dir,
        question_set=question_set,
        generation=generation,
        judge=judge,
        seed=args.seed,
        repetitions=args.repetitions,
        records=records,
    )
    print(f"Evaluation JSON: {json_path}")
    print(f"Evaluation report: {markdown_path}")
    return 0
