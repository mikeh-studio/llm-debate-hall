from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from llm_debate_hall.adapters.base import AdapterRequest, PRESET_REGISTRY
from llm_debate_hall.engine import DebateEngine, visible_presets
from llm_debate_hall.events import EventBroker
from llm_debate_hall.models import (
    CreateSessionRequest,
    HumanVoteRequest,
    JudgeDecisionRequest,
    PersonaCreate,
    PersonaUpdate,
    QuestionRequest,
)
from llm_debate_hall.personas import BUILTIN_PERSONAS
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


def create_app(db_path: str | None = None) -> FastAPI:
    base_dir = Path(__file__).resolve().parent
    static_dir = base_dir / "static"
    storage = Storage(db_path or str(base_dir.parent / "llm_debate_hall.db"))
    storage.seed_personas(BUILTIN_PERSONAS)
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

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

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

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict]:
        return storage.list_sessions()

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        try:
            return storage.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/sessions")
    async def create_session(payload: CreateSessionRequest) -> dict:
        if len(payload.agents) < 2 or len(payload.agents) > 5:
            raise HTTPException(status_code=400, detail="Debates must have between 2 and 5 debaters.")
        agent_payloads = []
        for agent in payload.agents:
            preset = PRESET_REGISTRY.get(agent.preset_id)
            if preset is None:
                raise HTTPException(status_code=400, detail=f"Unknown preset: {agent.preset_id}")
            agent_payloads.append(
                {
                    "display_name": agent.display_name,
                    "role": "debater",
                    "side": agent.side or "independent",
                    "persona_id": agent.persona_id if agent.persona_mode != "auto" else None,
                    "preset_id": agent.preset_id,
                    "model_name": agent.model_name,
                    "command": agent.command or preset.command,
                    "args_template": agent.args_template or preset.args_template,
                    "env": agent.env,
                }
            )
        judge_preset = PRESET_REGISTRY.get(payload.judge.preset_id)
        if judge_preset is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {payload.judge.preset_id}")
        session = storage.create_session(
            payload.topic,
            agent_payloads,
            {
                "display_name": payload.judge.display_name,
                "role": "judge",
                "side": "judge",
                "preset_id": payload.judge.preset_id,
                "model_name": payload.judge.model_name,
                "command": payload.judge.command or judge_preset.command,
                "args_template": payload.judge.args_template or judge_preset.args_template,
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
        decision = await engine.decide_winner(
            session_id,
            {
                "id": "judge-override",
                "display_name": payload.judge.display_name,
                "role": "judge",
                "side": "judge",
                "preset_id": payload.judge.preset_id,
                "model_name": payload.judge.model_name,
                "command": payload.judge.command or judge_preset.command,
                "args_template": payload.judge.args_template or judge_preset.args_template,
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
