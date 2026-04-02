# LLM Debate Hall

![LLM Debate Hall header](llm_debate_hall/static/llm-debate-hall-header_2.png)

LLM Debate Hall is a local-first FastAPI app for running structured multi-agent debates with real model CLIs, persistent transcripts, and persona-driven debaters. Give several agents a question, cast each one as a distinct philosophy or style, and watch the debate unfold as one continuous Arena thread instead of a pile of disconnected model outputs.

It is built for a very specific kind of experiment: using the local tools you already trust, keeping the debate state on your machine, and making the whole exchange inspectable enough to replay, judge, and test. The result sits somewhere between an AI toy, a debate simulator, and a local developer tool for probing how different model backends reason under pressure.

## Why It Stands Out

- Local-first by default: provider CLIs, auth, storage, and transcripts stay on your machine
- Structured debates instead of raw chat panes: opening rounds, replies, pause/continue flow, and judging
- Persona-driven seats: each debater can argue from a distinct philosophical frame or operational style
- Transcript-first design: debates are persisted, recoverable, and inspectable after the live run
- Real-provider smoke testing: the repo can now validate a full 3-debater live debate path end to end

## What It Can Do

- Arena-style setup for 2 to 5 debaters plus a judge
- Per-seat model, provider, and persona configuration
- Persona intensity controls so the same persona can play subtly or aggressively without cloning it
- Persona draft generation from a freeform description, then save-as-custom in the library
- Visible auto-persona selection before opening statements
- Single-paragraph turn flow with pause/continue controls
- Transcript-first Arena updates with persisted session recovery
- Persistent per-debater provider threads where supported
- Replay fallback when a provider cannot resume cleanly
- Arena observability with per-turn latency, provider/model, fallback state, token estimates, and a timeline
- Structured trace export for later analysis
- Local SQLite storage for sessions, messages, and scores

The judge is stateless and evaluates from the stored transcript. Supported provider defaults currently map to `codex exec`, `claude -p`, and `ollama run`. Gemini still needs a manual command override in this build.

## Current UI

### Setup

The setup screen lets you configure a topic, 2 to 5 debaters, per-seat personas, and the judge before the chamber starts.

![Setup screen](llm_debate_hall/static/setup_page.png)

### Pixel Stage Arena

The Arena now includes an alternate Pixel Stage presentation mode that keeps the full transcript as the source of truth while rendering the current speakers as retro RPG-style sprites with a live speech bubble.

![Pixel Stage Arena](llm_debate_hall/static/arena.png)

### Personas And Arena Theme

Personas remain editable in the app, and the Arena supports a dark theme for longer debate sessions.

![Personas in dark mode](docs/screenshots/personas_dark_mode.png)

## Quick Start

```bash
cd llm-debate-hall
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn llm_debate_hall.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Validation

The lightweight local validation set is:

```bash
pytest -q
node --check llm_debate_hall/static/app.js
PYTHONPYCACHEPREFIX=/tmp/llm-debate-hall-pyc python3 -m compileall llm_debate_hall tests
```

For UI or orchestration changes, also test the live app in the browser at `http://127.0.0.1:8000`.
When validating debate startup, confirm the Arena shows the persona-selection phase or the transcript itself instead of staying blank after `Start Debate`.
For observability changes, verify the Arena trace timeline and `Trace JSON` export reflect the same session state.
For persona workflow changes, verify persona generation, icon rendering, and save/edit flows in the Personas view.

## Live Debate Smoke Test

This repo now includes a repo-local Codex skill at `.codex/skills/live-debate-smoke/SKILL.md` plus a runner script that executes a real 3-debater smoke test on a fixed AGI safety topic. The smoke test is meant to answer one question quickly: does the end-to-end live debate path still work with real providers, real turns, and real persisted transcript output?

The runner prefers a single validated real provider/model across all three seats before trying mixed lineups, keeps iterating until it gets a valid debate result or hits a concrete blocker, and writes local Markdown evidence under `artifacts/live-debate-smoke/`.

```bash
python3 scripts/run_live_debate_smoke.py
```

The smoke workflow shipped alongside two runtime fixes that matter for real-provider debates:

- longer generation timeouts for slow live turns, configurable with `LLM_DEBATE_HALL_GENERATION_TIMEOUT_SECONDS`
- process-group cleanup on timeout so orphaned provider subprocesses do not linger after a failed turn

The runner requires at least one validated real provider from the built-in `openai`, `anthropic`, or `ollama` presets. It will not fall back to `mock`, and its generated reports/screenshots are intended as local evidence rather than committed source.

## Project Layout

- `llm_debate_hall/` FastAPI app, debate engine, storage, adapters
- `llm_debate_hall/static/` arena UI
- `tests/` API, engine, storage, and adapter tests

## Status

- Local-first and experimental
- Built around local CLIs such as Codex, Claude, and Ollama
- Strongest on single-machine use, not hosted deployment
- Best treated as a serious prototype rather than a polished SaaS product

## Local-Only Constraints

This project is mainly meant to run on your machine. It depends on locally installed CLIs, local auth state or env vars, and subprocess execution. Real-provider debates require the same provider tooling to be installed and authenticated wherever the app runs.

## Contributing

Issues and ideas are welcome. Treat the repo as experimental: behavior may change quickly, provider support is uneven, and some flows are still being validated in the live app.
