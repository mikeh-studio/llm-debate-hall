# LLM Debate Hall

LLM Debate Hall is a for-fun, work-in-progress project for running a circle of local AI debaters. One inspiration for this project was the multi-agent council concept seen in Karpathy's `llm-council`, though this repo takes it in a different, more debate- and game-oriented direction. The current experiment is simple: give multiple agent CLIs a topic, let each one act as a chosen philosopher or persona, and see how the debate unfolds. The broader idea is larger than debate alone; the circle could expand into other multi-agent interactions over time.

## Status

- Local-first and experimental
- Built around local CLIs such as Codex, Claude, and Ollama
- Not polished for hosted deployment
- Best treated as a playground, not a production tool

## What Works Today

- Arena-style setup for 2 to 5 debaters plus a judge
- Philosopher/persona selection per seat
- Single-paragraph turn flow with pause/continue controls
- Persistent per-debater provider threads where supported
- Replay fallback when a provider cannot resume cleanly
- Local SQLite storage for sessions, messages, and scores

The judge is stateless and evaluates from the stored transcript. Supported provider defaults currently map to `codex exec`, `claude -p`, and `ollama run`. Gemini still needs a manual command override in this build.

## Quick Start

```bash
cd llm-debate-hall
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn llm_debate_hall.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Development Checks

```bash
pytest -q
node --check llm_debate_hall/static/app.js
PYTHONPYCACHEPREFIX=/tmp/llm-debate-hall-pyc python3 -m compileall llm_debate_hall tests
```

For UI or orchestration changes, also test the live app in the browser at `http://127.0.0.1:8000`.

## Project Layout

- `llm_debate_hall/` FastAPI app, debate engine, storage, adapters
- `llm_debate_hall/static/` arena UI
- `tests/` API, engine, storage, and adapter tests

## Local-Only Constraints

This project is mainly meant to run on your machine. It depends on locally installed CLIs, local auth state or env vars, and subprocess execution. A remote deployment is possible, but only if the target host also has the required provider tooling installed and authenticated.

## Contributing

Issues and ideas are welcome. Treat the repo as experimental: behavior may change quickly, provider support is uneven, and some flows are still being validated in the live app.
