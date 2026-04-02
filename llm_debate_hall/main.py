from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from llm_debate_hall.adapters.base import AdapterRequest, PRESET_REGISTRY
from llm_debate_hall.engine import DebateEngine, active_models_for_preset, visible_presets
from llm_debate_hall.events import EventBroker
from llm_debate_hall.models import (
    CreateSessionRequest,
    HumanVoteRequest,
    JudgeDecisionRequest,
    ModeratorNoteRequest,
    PersonaCreate,
    PersonaGenerateRequest,
    PersonaUpdate,
    QuestionRequest,
)
from llm_debate_hall.storage import Storage


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _is_question(text: str) -> bool:
    return text.strip().endswith("?")


def _fallback_suggestions(seed: str) -> list[str]:
    prompt = seed.strip() or "AI agents"
    base = prompt.rstrip("?")
    return [
        f"Should {base} be allowed greater autonomy?",
        f"What is the strongest argument against {base}?",
        f"How should teams govern decisions involving {base}?",
    ]


def _persona_generation_prompt(description: str, name_hint: str | None, family_hint: str | None) -> str:
    hint_lines = []
    if name_hint:
        hint_lines.append(f"NAME_HINT: {name_hint}")
    if family_hint:
        hint_lines.append(f"FAMILY_HINT: {family_hint}")
    hints = "\n".join(hint_lines) or "NO_HINTS"
    return (
        "Generate one structured debate persona from the user's description.\n"
        f"DESCRIPTION: {description}\n"
        f"{hints}\n"
        "Return JSON with keys: "
        '{"name":"...", "philosophy_family":"...", "style":"...", "core_values":["..."], "debate_rules":["..."]}'
    )


def _using_default_invocation(preset_id: str, command: list[str], args_template: list[str]) -> bool:
    preset = PRESET_REGISTRY.get(preset_id)
    if preset is None:
        return False
    return command == preset.command and args_template == preset.args_template


def _assert_active_model(
    *,
    preset_id: str,
    model_name: str,
    command: list[str],
    args_template: list[str],
    env: dict[str, str],
) -> None:
    preset = PRESET_REGISTRY.get(preset_id)
    if preset is None:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {preset_id}")
    if preset.requires_command_override:
        return
    if not _using_default_invocation(preset_id, command, args_template):
        return

    active_models = active_models_for_preset(preset_id, env)
    if active_models:
        if model_name in active_models:
            return
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_name}' is not active for preset '{preset_id}'. "
                f"Use one of: {', '.join(active_models)}."
            ),
        )
    if model_name in preset.models:
        return
    if preset.models:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{model_name}' is not configured for preset '{preset_id}'. "
                f"Use one of: {', '.join(preset.models)}."
            ),
        )
    raise HTTPException(status_code=400, detail=f"No models are configured for preset '{preset_id}'.")


def create_app(db_path: str | None = None, personas_root: str | None = None) -> FastAPI:
    base_dir = Path(__file__).resolve().parent
    static_dir = base_dir / "static"
    resolved_db_path = db_path or os.environ.get("LLM_DEBATE_HALL_DB_PATH") or str(base_dir.parent / "llm_debate_hall.db")
    resolved_personas_root = personas_root or os.environ.get("LLM_DEBATE_HALL_PERSONAS_ROOT") or str(base_dir / "personas")
    storage = Storage(
        resolved_db_path,
        personas_root_path=resolved_personas_root,
    )
    broker = EventBroker()
    engine = DebateEngine(storage=storage, broker=broker)

    app = FastAPI(title="LLM Debate Hall")
    app.state.storage = storage
    app.state.engine = engine
    app.state.broker = broker

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount("/persona-icons", StaticFiles(directory=storage.persona_icons_dir), name="persona-icons")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    async def publish_trace_event(
        session_id: str,
        *,
        event_type: str,
        round_type: str | None = None,
        round_index: int | None = None,
        agent_id: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        event = storage.add_trace_event(
            session_id=session_id,
            event_type=event_type,
            round_type=round_type,
            round_index=round_index,
            agent_id=agent_id,
            payload=payload,
        )
        await broker.publish(session_id, {"type": "trace_event_saved", "trace_event": event})
        return event

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "app": "llm-debate-hall"}

    @app.get("/api/health")
    async def api_health() -> dict:
        return {"status": "ok", "app": "llm-debate-hall"}

    @app.get("/api/presets")
    async def list_presets() -> list[dict]:
        return visible_presets()

    @app.get("/api/personas")
    async def list_personas() -> list[dict]:
        return storage.list_personas()

    @app.post("/api/questions/validate")
    async def validate_question(payload: QuestionRequest) -> dict:
        question = payload.question.strip()
        if not _is_question(question):
            return {
                "accepted": False,
                "reason": "The debate prompt must be written as a question.",
                "suggestions": _fallback_suggestions(question),
            }

        judge_preset = PRESET_REGISTRY.get(payload.judge.preset_id)
        if judge_preset is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {payload.judge.preset_id}")
        judge_agent = {
            "id": "question-validator",
            "display_name": payload.judge.display_name,
            "role": "judge",
            "side": "judge",
            "preset_id": payload.judge.preset_id,
            "model_name": payload.judge.model_name,
            "command": payload.judge.command or judge_preset.command,
            "args_template": payload.judge.args_template or judge_preset.args_template,
            "env": payload.judge.env,
        }
        _assert_active_model(
            preset_id=payload.judge.preset_id,
            model_name=payload.judge.model_name,
            command=judge_agent["command"],
            args_template=judge_agent["args_template"],
            env=judge_agent["env"],
        )
        adapter = engine.adapter_factory(judge_agent)
        prompt = (
            "Decide whether this is a strong debate question.\n"
            f"QUESTION: {question}\n"
            'Return JSON: {"accepted": true|false, "reason": "...", "suggestions": ["...", "...", "..."]}'
        )
        response = await adapter.generate(
            AdapterRequest(
                session_id="question-validation",
                agent_id="question-validator",
                agent_name=payload.judge.display_name,
                preset_id=payload.judge.preset_id,
                role="judge",
                side="judge",
                topic=question,
                prompt=prompt,
                output_mode="question_validation",
                model_name=payload.judge.model_name,
                command=judge_agent["command"],
                args_template=judge_agent["args_template"],
                env=judge_agent["env"],
            ),
            lambda chunk: asyncio.sleep(0),
        )
        parsed = _extract_json(response.raw_text) or {}
        accepted = bool(parsed.get("accepted")) if "accepted" in parsed else len(question.split()) >= 4
        return {
            "accepted": accepted,
            "reason": parsed.get("reason", "The judge accepted the question.") if accepted else parsed.get(
                "reason", "The judge could not validate this as a debate question."
            ),
            "suggestions": parsed.get("suggestions", _fallback_suggestions(question)),
        }

    @app.post("/api/questions/suggestions")
    async def suggest_questions(payload: QuestionRequest) -> dict:
        seed = payload.question.strip()
        judge_preset = PRESET_REGISTRY.get(payload.judge.preset_id)
        if judge_preset is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {payload.judge.preset_id}")
        judge_agent = {
            "id": "question-suggester",
            "display_name": payload.judge.display_name,
            "role": "judge",
            "side": "judge",
            "preset_id": payload.judge.preset_id,
            "model_name": payload.judge.model_name,
            "command": payload.judge.command or judge_preset.command,
            "args_template": payload.judge.args_template or judge_preset.args_template,
            "env": payload.judge.env,
        }
        _assert_active_model(
            preset_id=payload.judge.preset_id,
            model_name=payload.judge.model_name,
            command=judge_agent["command"],
            args_template=judge_agent["args_template"],
            env=judge_agent["env"],
        )
        adapter = engine.adapter_factory(judge_agent)
        prompt = (
            "Suggest exactly three debate questions.\n"
            f"SEED: {seed or 'AI agents and governance'}\n"
            'Return JSON: {"suggestions": ["...", "...", "..."]}'
        )
        response = await adapter.generate(
            AdapterRequest(
                session_id="question-suggestions",
                agent_id="question-suggester",
                agent_name=payload.judge.display_name,
                preset_id=payload.judge.preset_id,
                role="judge",
                side="judge",
                topic=seed or "AI agents and governance",
                prompt=prompt,
                output_mode="question_suggestions",
                model_name=payload.judge.model_name,
                command=judge_agent["command"],
                args_template=judge_agent["args_template"],
                env=judge_agent["env"],
            ),
            lambda chunk: asyncio.sleep(0),
        )
        parsed = _extract_json(response.raw_text) or {}
        return {"suggestions": parsed.get("suggestions", _fallback_suggestions(seed))}

    @app.post("/api/personas")
    async def create_persona(payload: PersonaCreate) -> dict:
        return storage.create_persona(payload)

    @app.put("/api/personas/{persona_id}")
    async def update_persona(persona_id: str, payload: PersonaUpdate) -> dict:
        try:
            return storage.update_persona(persona_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/personas/generate")
    async def generate_persona(payload: PersonaGenerateRequest) -> dict:
        description = payload.description.strip()
        if not description:
            raise HTTPException(status_code=400, detail="Persona description cannot be empty.")

        preset = PRESET_REGISTRY.get(payload.generator.preset_id)
        if preset is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {payload.generator.preset_id}")
        command = payload.generator.command or preset.command
        args_template = payload.generator.args_template or preset.args_template
        _assert_active_model(
            preset_id=payload.generator.preset_id,
            model_name=payload.generator.model_name,
            command=command,
            args_template=args_template,
            env=payload.generator.env,
        )
        generator_agent = {
            "id": "persona-generator",
            "display_name": payload.generator.display_name,
            "role": "judge",
            "side": "system",
            "preset_id": payload.generator.preset_id,
            "model_name": payload.generator.model_name,
            "command": command,
            "args_template": args_template,
            "env": payload.generator.env,
        }
        adapter = engine.adapter_factory(generator_agent)
        response = await adapter.generate(
            AdapterRequest(
                session_id="persona-generation",
                agent_id="persona-generator",
                agent_name=payload.generator.display_name,
                preset_id=payload.generator.preset_id,
                role="judge",
                side="system",
                topic=description,
                prompt=_persona_generation_prompt(description, payload.name_hint, payload.philosophy_family_hint),
                output_mode="persona_generation",
                model_name=payload.generator.model_name,
                command=command,
                args_template=args_template,
                env=payload.generator.env,
            ),
            lambda chunk: asyncio.sleep(0),
        )
        parsed = _extract_json(response.raw_text)
        if parsed is None:
            raise HTTPException(status_code=400, detail="Persona generator returned invalid JSON.")
        try:
            return PersonaCreate(
                name=str(parsed.get("name", "")).strip(),
                philosophy_family=str(parsed.get("philosophy_family", "")).strip(),
                style=str(parsed.get("style", "")).strip(),
                core_values=[str(item).strip() for item in parsed.get("core_values", []) if str(item).strip()],
                debate_rules=[str(item).strip() for item in parsed.get("debate_rules", []) if str(item).strip()],
                is_selectable=True,
            ).model_dump()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Persona generator returned malformed fields: {exc}") from exc

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict]:
        return storage.list_sessions()

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        try:
            return storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict:
        try:
            storage.delete_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/sessions")
    async def create_session(payload: CreateSessionRequest) -> dict:
        if len(payload.agents) < 2 or len(payload.agents) > 5:
            raise HTTPException(status_code=400, detail="Debates must have between 2 and 5 debaters.")
        agent_payloads = []
        for agent in payload.agents:
            preset = PRESET_REGISTRY.get(agent.preset_id)
            if preset is None:
                raise HTTPException(status_code=400, detail=f"Unknown preset: {agent.preset_id}")
            command = agent.command or preset.command
            args_template = agent.args_template or preset.args_template
            _assert_active_model(
                preset_id=agent.preset_id,
                model_name=agent.model_name,
                command=command,
                args_template=args_template,
                env=agent.env,
            )
            agent_payloads.append(
                {
                    "display_name": agent.display_name,
                    "role": "debater",
                    "side": agent.side or "independent",
                    "persona_id": agent.persona_id if agent.persona_mode != "auto" else None,
                    "persona_intensity": agent.persona_intensity,
                    "preset_id": agent.preset_id,
                    "model_name": agent.model_name,
                    "command": command,
                    "args_template": args_template,
                    "env": agent.env,
                }
            )
        judge_preset = PRESET_REGISTRY.get(payload.judge.preset_id)
        if judge_preset is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {payload.judge.preset_id}")
        judge_command = payload.judge.command or judge_preset.command
        judge_args_template = payload.judge.args_template or judge_preset.args_template
        _assert_active_model(
            preset_id=payload.judge.preset_id,
            model_name=payload.judge.model_name,
            command=judge_command,
            args_template=judge_args_template,
            env=payload.judge.env,
        )
        session = storage.create_session(
            payload.topic,
            agent_payloads,
            {
                "display_name": payload.judge.display_name,
                "role": "judge",
                "side": "judge",
                "preset_id": payload.judge.preset_id,
                "model_name": payload.judge.model_name,
                "command": judge_command,
                "args_template": judge_args_template,
                "env": payload.judge.env,
            },
        )
        return session

    @app.post("/api/sessions/{session_id}/start")
    async def start_session(session_id: str) -> dict:
        try:
            storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        engine.start_session(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/continue")
    async def continue_session(session_id: str) -> dict:
        try:
            session = storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session["status"] != "awaiting_continue":
            raise HTTPException(status_code=400, detail="Session is not waiting for a continue decision.")
        engine.continue_session(session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/agents/{agent_id}/reset-session")
    async def reset_agent_session(session_id: str, agent_id: str) -> dict:
        try:
            session = storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        agent = next((item for item in session["agents"] if item["id"] == agent_id), None)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent does not belong to this session.")
        if agent["role"] != "debater":
            raise HTTPException(status_code=400, detail="Only debater sessions can be reset.")

        had_provider_session = storage.reset_provider_session(agent_id)
        detail = (
            f"Reset native session for {agent['display_name']}. Next turn will start fresh."
            if had_provider_session
            else f"{agent['display_name']} had no stored native session. Next turn will start fresh."
        )
        entry = storage.add_thread_entry(
            session_id=session_id,
            kind="system",
            display_name="Debate Hall",
            display_text=detail,
            agent_id=agent_id,
            payload={"event": "provider_session_reset", "agent_id": agent_id},
        )
        await broker.publish(session_id, {"type": "thread_entry_saved", "entry": entry})
        await publish_trace_event(
            session_id,
            event_type="provider_session_reset",
            agent_id=agent_id,
            payload={
                "summary": detail,
                "provider_session_mode": None,
                "provider_session_status": "reset",
            },
        )
        await broker.publish(
            session_id,
            {
                "type": "provider_session_state",
                "agent_id": agent_id,
                "agent_name": agent["display_name"],
                "provider_session": None,
            },
        )
        return storage.get_session(session_id)

    @app.post("/api/sessions/{session_id}/moderator-note")
    async def add_moderator_note(session_id: str, payload: ModeratorNoteRequest) -> dict:
        try:
            session = storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        note = payload.text.strip()
        if not note:
            raise HTTPException(status_code=400, detail="Moderator note cannot be empty.")
        if session["status"] != "awaiting_continue":
            raise HTTPException(
                status_code=400,
                detail="Moderator notes can only be sent while the session is waiting to continue.",
            )
        entry = storage.add_thread_entry(
            session_id=session_id,
            kind="moderator",
            display_name="Moderator",
            display_text=note,
            payload={"source": "user"},
        )
        await broker.publish(session_id, {"type": "thread_entry_saved", "entry": entry})
        engine.continue_session(session_id)
        return {"ok": True, "entry": entry}

    @app.post("/api/sessions/{session_id}/end")
    async def end_session(session_id: str) -> dict:
        try:
            session = storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session["status"] not in {"awaiting_continue", "running"}:
            raise HTTPException(status_code=400, detail="Session cannot be ended right now.")
        await engine.end_session(session_id)
        return storage.get_session(session_id)

    @app.post("/api/sessions/{session_id}/judge-decision")
    async def judge_decision(session_id: str, payload: JudgeDecisionRequest) -> dict:
        try:
            session = storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session["status"] not in {"awaiting_winner", "completed"}:
            raise HTTPException(status_code=400, detail="Session is not waiting for a winner decision.")

        judge_preset = PRESET_REGISTRY.get(payload.judge.preset_id)
        if judge_preset is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {payload.judge.preset_id}")
        judge_command = payload.judge.command or judge_preset.command
        judge_args_template = payload.judge.args_template or judge_preset.args_template
        _assert_active_model(
            preset_id=payload.judge.preset_id,
            model_name=payload.judge.model_name,
            command=judge_command,
            args_template=judge_args_template,
            env=payload.judge.env,
        )
        decision = await engine.decide_winner(
            session_id,
            {
                "id": "judge-override",
                "display_name": payload.judge.display_name,
                "role": "judge",
                "side": "judge",
                "preset_id": payload.judge.preset_id,
                "model_name": payload.judge.model_name,
                "command": judge_command,
                "args_template": judge_args_template,
                "env": payload.judge.env,
            },
        )
        return decision

    @app.post("/api/sessions/{session_id}/vote")
    async def set_vote(session_id: str, payload: HumanVoteRequest) -> dict:
        try:
            session = storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        candidate_ids = {agent["id"] for agent in session["agents"] if agent["role"] == "debater"}
        if payload.winner_agent_id not in candidate_ids:
            raise HTTPException(status_code=400, detail="Winner must be one of the debate agents.")
        storage.set_human_vote(session_id, payload.winner_agent_id)
        storage.update_session_status(session_id, "completed")
        return storage.get_session(session_id)

    @app.get("/api/sessions/{session_id}/export")
    async def export_session(session_id: str) -> JSONResponse:
        try:
            payload = storage.export_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(payload)

    @app.get("/api/sessions/{session_id}/trace")
    async def get_trace(session_id: str) -> dict:
        try:
            session = storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"session_id": session_id, "trace_events": session.get("trace_events", [])}

    @app.get("/api/sessions/{session_id}/trace/export")
    async def export_trace(session_id: str) -> JSONResponse:
        try:
            session = storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(
            {
                "session_id": session["id"],
                "topic": session["topic"],
                "status": session["status"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
                "trace_events": session.get("trace_events", []),
            }
        )

    @app.websocket("/ws/sessions/{session_id}")
    async def session_ws(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        queue = await broker.subscribe(session_id)
        try:
            while True:
                event = await asyncio.to_thread(queue.get)
                await websocket.send_json(event)
        except WebSocketDisconnect:
            await broker.unsubscribe(session_id, queue)

    return app


app = create_app()
