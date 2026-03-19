# Repository Guidelines

## Project Structure & Module Organization
`llm_debate_hall/` contains the FastAPI server and debate runtime. `main.py` serves APIs and static assets, `engine.py` runs the staged debate flow, `storage.py` owns SQLite persistence, and `adapters/` contains provider integrations. Persistent debater thread state is stored separately from transcript history, so keep adapter, engine, and storage changes aligned. Frontend assets live in `llm_debate_hall/static/` as plain `index.html`, `app.js`, and `styles.css`. Tests are in `tests/`. `llm_debate_hall.db` is runtime data, not source.

## Build, Test, and Development Commands
Run locally from the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn llm_debate_hall.main:app --reload
```

Use `pytest -q` to run the full test suite. Use `python -m compileall llm_debate_hall tests` for a quick syntax sweep. For frontend-only edits, use `node --check llm_debate_hall/static/app.js` to catch JavaScript syntax errors.
When `compileall` is blocked by macOS cache permissions, run `PYTHONPYCACHEPREFIX=/tmp/llm-debate-hall-pyc python3 -m compileall llm_debate_hall tests` instead.

## Coding Style & Naming Conventions
Use Python 3.11+ and 4-space indentation. Keep backend code straightforward and typed where practical; existing models use Pydantic and small helper functions over deep class hierarchies. Use `snake_case` for Python names and JSON keys. In frontend code, follow the current vanilla JS style: small rendering helpers, single `state` object, and `camelCase` function names. Keep HTML/CSS changes aligned with the current arena layout rather than introducing frameworks.

## Testing Guidelines
Add tests next to the affected subsystem: API behavior in `tests/test_api.py`, engine flow in `tests/test_engine.py`, persistence in `tests/test_storage.py`, and CLI invocation logic in `tests/test_subprocess_adapter.py`. Name tests `test_<behavior>()`. Prefer deterministic tests using the hidden `mock` preset rather than live external CLIs.

For UI or orchestration changes, also verify the live app manually by running `uvicorn llm_debate_hall.main:app --reload` and testing the affected flow in the browser at `http://127.0.0.1:8000`. Do not rely on static code review alone for arena transitions, streamed dialogue, or provider setup behavior. When provider-session code changes, confirm both persistent-thread behavior and replay fallback in the live app if the required local CLIs are available.

## Commit & Pull Request Guidelines
This workspace does not currently include `.git` history, so no local commit convention can be inferred. Use concise, imperative commit messages such as `Fix Codex preset invocation`. In PRs, include: a short summary, affected user flow, test evidence (`pytest -q`, syntax checks), and screenshots for UI changes.

## Security & Configuration Tips
Provider CLIs may require local auth or env vars. Do not hardcode secrets in source or checked-in JSON overrides. OpenAI uses `codex exec`, Anthropic uses `claude -p`, Ollama uses `ollama run`, and Gemini currently needs a manual command override. Keep provider docs in sync with `llm_debate_hall/adapters/`, `llm_debate_hall/engine.py`, and `llm_debate_hall/storage.py`, especially around persistent debater sessions and the stateless judge path.
