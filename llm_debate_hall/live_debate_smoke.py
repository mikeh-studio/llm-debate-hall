from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from llm_debate_hall.adapters.base import PRESET_REGISTRY
from llm_debate_hall.adapters.subprocess_adapter import AUTH_ERROR_MARKERS, MODEL_ERROR_MARKERS
from llm_debate_hall.engine import active_models_for_preset, visible_presets
from llm_debate_hall.main import create_app


DEFAULT_TOPIC = (
    "AGI should not be deployed at scale until interpretability and control methods are good enough "
    "to reliably detect dangerous deception"
)
DEFAULT_REPORT_DIR = Path("artifacts/live-debate-smoke")
DEFAULT_MAX_ATTEMPTS = 5
ALLOWED_PRESET_IDS = ("openai", "anthropic", "ollama")
PRESET_PRIORITY = {preset_id: index for index, preset_id in enumerate(ALLOWED_PRESET_IDS)}
DEBATER_BLUEPRINTS = (
    ("Athena", "stoic_rationalist"),
    ("Byron", "pragmatic_engineer"),
    ("Cicero", "humanist_mediator"),
)
JUDGE_NAME = "Minos"


@dataclass(frozen=True, slots=True)
class ModelChoice:
    preset_id: str
    model_name: str
    label: str


@dataclass(frozen=True, slots=True)
class DebaterPlan:
    display_name: str
    persona_id: str
    preset_id: str
    model_name: str


@dataclass(frozen=True, slots=True)
class AttemptPlan:
    debaters: tuple[DebaterPlan, DebaterPlan, DebaterPlan]
    judge: ModelChoice


@dataclass(slots=True)
class AttemptRecord:
    number: int
    plan: AttemptPlan
    success: bool
    session_id: str | None
    status: str
    failure_reason: str | None
    session: dict[str, Any] | None


@dataclass(slots=True)
class SmokeRunOutcome:
    ok: bool
    report_path: Path
    attempts: list[AttemptRecord]
    topic: str
    blocker: str | None = None


def _single_paragraph(text: str) -> str:
    return " ".join(part.strip() for part in text.replace("\r", "\n").splitlines() if part.strip())


def _looks_like_provider_issue(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in MODEL_ERROR_MARKERS) or any(
        marker in lowered for marker in AUTH_ERROR_MARKERS
    )


def discover_real_model_choices(presets: list[dict[str, Any]]) -> list[ModelChoice]:
    ordered_presets = sorted(
        presets,
        key=lambda preset: (
            PRESET_PRIORITY.get(preset["id"], len(PRESET_PRIORITY)),
            preset.get("label", preset["id"]),
        ),
    )
    choices: list[ModelChoice] = []
    seen: set[tuple[str, str]] = set()
    for preset in ordered_presets:
        if preset["id"] not in ALLOWED_PRESET_IDS:
            continue
        if not preset.get("is_available", False):
            continue
        if preset.get("requires_command_override", False):
            continue
        validated_models = list(preset.get("validated_models", []))
        active_models = validated_models or active_models_for_preset(preset["id"])
        for model_name in active_models:
            key = (preset["id"], model_name)
            if key in seen:
                continue
            seen.add(key)
            choices.append(ModelChoice(preset_id=preset["id"], model_name=model_name, label=preset["label"]))
    return choices


def build_attempt_plans(model_choices: list[ModelChoice], max_attempts: int) -> list[AttemptPlan]:
    if not model_choices or max_attempts <= 0:
        return []

    plans: list[AttemptPlan] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    for model_choice in model_choices:
        debaters = tuple(
            DebaterPlan(
                display_name=display_name,
                persona_id=persona_id,
                preset_id=model_choice.preset_id,
                model_name=model_choice.model_name,
            )
            for display_name, persona_id in DEBATER_BLUEPRINTS
        )
        key = tuple((seat.preset_id, seat.model_name) for seat in debaters) + (
            (model_choice.preset_id, model_choice.model_name),
        )
        if key in seen:
            continue
        seen.add(key)
        plans.append(AttemptPlan(debaters=debaters, judge=model_choice))
        if len(plans) >= max_attempts:
            return plans

    max_rotations = max(max_attempts * max(len(model_choices), 1), max_attempts)

    for offset in range(max_rotations):
        debaters = tuple(
            DebaterPlan(
                display_name=display_name,
                persona_id=persona_id,
                preset_id=model_choices[(offset + index) % len(model_choices)].preset_id,
                model_name=model_choices[(offset + index) % len(model_choices)].model_name,
            )
            for index, (display_name, persona_id) in enumerate(DEBATER_BLUEPRINTS)
        )
        judge = model_choices[(offset + len(DEBATER_BLUEPRINTS)) % len(model_choices)]
        key = tuple((seat.preset_id, seat.model_name) for seat in debaters) + ((judge.preset_id, judge.model_name),)
        if key in seen:
            continue
        seen.add(key)
        plans.append(AttemptPlan(debaters=debaters, judge=judge))
        if len(plans) >= max_attempts:
            break
    return plans


def validate_transcript(session: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if session.get("status") != "awaiting_continue":
        issues.append(f"Session ended in status `{session.get('status', 'unknown')}` instead of `awaiting_continue`.")

    debaters = [agent for agent in session.get("agents", []) if agent.get("role") == "debater"]
    if len(debaters) != 3:
        issues.append(f"Expected 3 debaters, found {len(debaters)}.")

    rounds = session.get("rounds", [])
    if len(rounds) != 3:
        issues.append(f"Expected 3 rounds, found {len(rounds)}.")

    messages = session.get("messages", [])
    if len(messages) != 9:
        issues.append(f"Expected 9 saved debater messages, found {len(messages)}.")

    counts = {agent["id"]: 0 for agent in debaters}
    for message in messages:
        agent_id = message.get("agent_id")
        if agent_id in counts:
            counts[agent_id] += 1
        display_text = _single_paragraph(message.get("display_text", ""))
        if not display_text:
            issues.append("Encountered a saved message with empty display text.")
        if _looks_like_provider_issue(display_text):
            issues.append(f"Saved debate text looks like provider output instead of dialogue: {display_text}")
        raw_text = message.get("normalized_payload", {}).get("raw_text", "")
        if _looks_like_provider_issue(raw_text):
            issues.append(f"Raw model output looks like a provider error: {_single_paragraph(raw_text)}")

    for agent in debaters:
        if counts.get(agent["id"], 0) != 3:
            issues.append(f"{agent['display_name']} spoke {counts.get(agent['id'], 0)} times instead of 3.")
        if not agent.get("persona_id"):
            issues.append(f"{agent['display_name']} has no persona assigned.")

    for entry in session.get("thread_entries", []):
        if entry.get("kind") == "system" and _looks_like_provider_issue(entry.get("display_text", "")):
            issues.append(f"System log shows provider failure text: {_single_paragraph(entry['display_text'])}")

    return issues


def _derive_failure_reason(session: dict[str, Any] | None, exc: Exception | None, validation_issues: list[str]) -> str:
    session_detail = _latest_session_detail(session)
    if validation_issues:
        if session_detail:
            return "; ".join([*validation_issues, f"Last session detail: {session_detail}"])
        return "; ".join(validation_issues)
    if exc is not None:
        return _single_paragraph(str(exc)) or exc.__class__.__name__
    if session_detail:
        return session_detail
    if session:
        return f"Session ended in status `{session.get('status', 'unknown')}` without saved dialogue."
    return "Attempt failed before a session could be stored."


def _latest_session_detail(session: dict[str, Any] | None) -> str | None:
    if not session:
        return None
    for entry in reversed(session.get("thread_entries", [])):
        text = _single_paragraph(entry.get("display_text", ""))
        if text:
            return text
    return None


def _create_attempt_session(app: Any, topic: str, plan: AttemptPlan) -> dict[str, Any]:
    storage = app.state.storage
    debaters = [
        {
            "display_name": seat.display_name,
            "role": "debater",
            "side": "independent",
            "persona_id": seat.persona_id,
            "preset_id": seat.preset_id,
            "model_name": seat.model_name,
            "command": list(PRESET_REGISTRY[seat.preset_id].command),
            "args_template": list(PRESET_REGISTRY[seat.preset_id].args_template),
            "env": {},
        }
        for seat in plan.debaters
    ]
    judge = {
        "display_name": JUDGE_NAME,
        "role": "judge",
        "side": "judge",
        "preset_id": plan.judge.preset_id,
        "model_name": plan.judge.model_name,
        "command": list(PRESET_REGISTRY[plan.judge.preset_id].command),
        "args_template": list(PRESET_REGISTRY[plan.judge.preset_id].args_template),
        "env": {},
    }
    return storage.create_session(topic, debaters, judge)


def _run_attempt(app: Any, topic: str, plan: AttemptPlan, attempt_number: int) -> AttemptRecord:
    session = _create_attempt_session(app, topic, plan)
    engine = app.state.engine
    storage = app.state.storage

    captured_exception: Exception | None = None
    try:
        asyncio.run(engine.run_segment(session["id"]))
    except Exception as exc:  # pragma: no cover - exercised via session inspection in tests
        captured_exception = exc

    latest_session = storage.get_session(session["id"])
    issues = validate_transcript(latest_session)
    success = captured_exception is None and not issues
    return AttemptRecord(
        number=attempt_number,
        plan=plan,
        success=success,
        session_id=session["id"],
        status=latest_session.get("status", "unknown"),
        failure_reason=None if success else _derive_failure_reason(latest_session, captured_exception, issues),
        session=latest_session,
    )


def _persona_name_map(app: Any) -> dict[str, str]:
    return {persona["id"]: persona["name"] for persona in app.state.storage.list_personas()}


def _format_attempt_lineup(plan: AttemptPlan, persona_names: dict[str, str]) -> str:
    debater_parts = [
        f"{seat.display_name} [{persona_names.get(seat.persona_id, seat.persona_id)}] via {seat.preset_id}:{seat.model_name}"
        for seat in plan.debaters
    ]
    return "; ".join(debater_parts) + f"; judge {JUDGE_NAME} via {plan.judge.preset_id}:{plan.judge.model_name}"


def summarize_views(session: dict[str, Any], persona_names: dict[str, str]) -> list[str]:
    messages_by_agent: dict[str, list[dict[str, Any]]] = {}
    for message in session.get("messages", []):
        messages_by_agent.setdefault(message["agent_id"], []).append(message)

    summaries: list[str] = []
    for agent in session.get("agents", []):
        if agent.get("role") != "debater":
            continue
        agent_messages = messages_by_agent.get(agent["id"], [])
        if not agent_messages:
            continue
        opening_payload = agent_messages[0].get("normalized_payload", {})
        final_payload = agent_messages[-1].get("normalized_payload", {})
        opening_claim = _single_paragraph(opening_payload.get("claim") or agent_messages[0].get("display_text", ""))
        final_claim = _single_paragraph(final_payload.get("claim") or agent_messages[-1].get("display_text", ""))
        final_attack = _single_paragraph(final_payload.get("attack", ""))
        summary = (
            f"{agent['display_name']} ({persona_names.get(agent.get('persona_id', ''), agent.get('persona_id', 'unknown'))}, "
            f"{agent['preset_id']}:{agent['model_name']}) opens with: {opening_claim}. "
            f"Final position: {final_claim}."
        )
        if final_attack:
            summary += f" Main criticism: {final_attack}."
        summaries.append(summary)
    return summaries


def _build_report_markdown(
    *,
    topic: str,
    attempts: list[AttemptRecord],
    persona_names: dict[str, str],
    blocker: str | None,
) -> str:
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    success_attempt = next((attempt for attempt in attempts if attempt.success), None)
    lines = [
        "# Live Debate Smoke Report",
        "",
        f"- Generated: {generated_at}",
        f"- Topic: {topic}",
        f"- Result: {'PASS' if success_attempt else 'FAIL'}",
        f"- Attempts: {len(attempts)}",
        "",
        "## Attempt History",
        "",
    ]

    if not attempts:
        lines.extend(
            [
                "- No attempts were run.",
                "",
            ]
        )
    else:
        for attempt in attempts:
            lines.extend(
                [
                    f"### Attempt {attempt.number}",
                    "",
                    f"- Lineup: {_format_attempt_lineup(attempt.plan, persona_names)}",
                    f"- Session ID: {attempt.session_id or 'not created'}",
                    f"- Status: {attempt.status}",
                    f"- Outcome: {'PASS' if attempt.success else 'FAIL'}",
                ]
            )
            if attempt.failure_reason:
                lines.append(f"- Failure: {attempt.failure_reason}")
            lines.append("")

    if success_attempt and success_attempt.session:
        session = success_attempt.session
        lines.extend(
            [
                "## Model And Persona Log",
                "",
            ]
        )
        for agent in session.get("agents", []):
            role = agent["role"]
            if role == "debater":
                lines.append(
                    f"- {agent['display_name']}: {agent['preset_id']}:{agent['model_name']} | "
                    f"{persona_names.get(agent.get('persona_id', ''), agent.get('persona_id', 'unknown'))}"
                )
            else:
                lines.append(f"- {agent['display_name']}: {agent['preset_id']}:{agent['model_name']} | judge")
        lines.extend(
            [
                "",
                "## Debate Transcript",
                "",
            ]
        )
        for message in session.get("messages", []):
            lines.append(
                f"- Round {message['round_index']} {message['round_type']} | {message['agent_name']} | "
                f"{_single_paragraph(message['display_text'])}"
            )
        lines.extend(
            [
                "",
                "## Three Views",
                "",
            ]
        )
        for summary in summarize_views(session, persona_names):
            lines.append(f"- {summary}")
        lines.append("")
    else:
        lines.extend(
            [
                "## Blocker",
                "",
                f"- {blocker or 'No successful real-provider lineup was able to complete the required debate run.'}",
                "",
            ]
        )

    return "\n".join(lines)


def _report_path(report_dir: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return report_dir / f"live-debate-smoke-{timestamp}.md"


def run_live_debate_smoke(
    *,
    topic: str = DEFAULT_TOPIC,
    report_dir: Path = DEFAULT_REPORT_DIR,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> SmokeRunOutcome:
    report_dir.mkdir(parents=True, exist_ok=True)
    presets = visible_presets()
    model_choices = discover_real_model_choices(presets)
    attempts: list[AttemptRecord] = []
    blocker: str | None = None

    if not model_choices:
        blocker = "No validated real providers are available. Expected at least one validated preset from openai, anthropic, or ollama."
        report_path = _report_path(report_dir)
        report_path.write_text(
            _build_report_markdown(topic=topic, attempts=attempts, persona_names={}, blocker=blocker),
            encoding="utf-8",
        )
        return SmokeRunOutcome(ok=False, report_path=report_path, attempts=attempts, topic=topic, blocker=blocker)

    attempt_plans = build_attempt_plans(model_choices, max_attempts=max_attempts)
    if not attempt_plans:
        blocker = "No unique attempt plans could be built from the validated real model choices."
        report_path = _report_path(report_dir)
        report_path.write_text(
            _build_report_markdown(topic=topic, attempts=attempts, persona_names={}, blocker=blocker),
            encoding="utf-8",
        )
        return SmokeRunOutcome(ok=False, report_path=report_path, attempts=attempts, topic=topic, blocker=blocker)

    with TemporaryDirectory(prefix="llm-debate-hall-live-smoke-") as temp_dir:
        app = create_app(
            db_path=str(Path(temp_dir) / "debate.db"),
            personas_root=str(Path(temp_dir) / "personas"),
        )
        persona_names = _persona_name_map(app)
        for attempt_number, plan in enumerate(attempt_plans, start=1):
            attempt = _run_attempt(app, topic, plan, attempt_number)
            attempts.append(attempt)
            if attempt.success:
                report_path = _report_path(report_dir)
                report_path.write_text(
                    _build_report_markdown(
                        topic=topic,
                        attempts=attempts,
                        persona_names=persona_names,
                        blocker=None,
                    ),
                    encoding="utf-8",
                )
                return SmokeRunOutcome(
                    ok=True,
                    report_path=report_path,
                    attempts=attempts,
                    topic=topic,
                )
        blocker = attempts[-1].failure_reason if attempts else "Unknown blocker."
        report_path = _report_path(report_dir)
        report_path.write_text(
            _build_report_markdown(
                topic=topic,
                attempts=attempts,
                persona_names=persona_names,
                blocker=blocker,
            ),
            encoding="utf-8",
        )
        return SmokeRunOutcome(
            ok=False,
            report_path=report_path,
            attempts=attempts,
            topic=topic,
            blocker=blocker,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LLM Debate Hall live 3-debater smoke test.")
    parser.add_argument("--topic", default=DEFAULT_TOPIC, help="Debate topic to use.")
    parser.add_argument(
        "--report-dir",
        default=str(DEFAULT_REPORT_DIR),
        help="Directory where the Markdown report should be written.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="Maximum number of retry attempts with different real-provider lineups.",
    )
    args = parser.parse_args(argv)

    outcome = run_live_debate_smoke(
        topic=args.topic,
        report_dir=Path(args.report_dir),
        max_attempts=args.max_attempts,
    )
    status = "PASS" if outcome.ok else "FAIL"
    print(f"{status}: {outcome.report_path}")
    if outcome.blocker:
        print(outcome.blocker)
    return 0 if outcome.ok else 1


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_REPORT_DIR",
    "DEFAULT_TOPIC",
    "AttemptPlan",
    "AttemptRecord",
    "DebaterPlan",
    "ModelChoice",
    "SmokeRunOutcome",
    "build_attempt_plans",
    "discover_real_model_choices",
    "main",
    "run_live_debate_smoke",
    "summarize_views",
    "validate_transcript",
]
