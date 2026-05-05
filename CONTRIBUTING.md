# Contributing

Debate Hall is a for-fun experimental project. Small fixes, bug reports, and ideas are more useful than large speculative refactors.

## Before Opening A PR

1. Run `pytest -q`.
2. Run `node --check llm_debate_hall/static/app.js`.
3. Run `PYTHONPYCACHEPREFIX=/tmp/llm-debate-hall-pyc python3 -m compileall llm_debate_hall tests`.
4. For UI or orchestration changes, run the live app with `uvicorn llm_debate_hall.main:app --reload` and verify the flow in the browser at `http://127.0.0.1:8000`.
5. For debate-start changes, confirm the Chamber shows persona selection, an active speaker, or transcript entries after `Start Debate` instead of remaining blank.
6. For real-provider runtime or subprocess changes, run `python3 scripts/run_live_debate_smoke.py` if the required local CLIs are available.
7. For observability changes, verify the latest-turn metrics, trace timeline, and `Trace JSON` export all match the same session.
8. For persona workflow changes, verify generated drafts, persona intensity controls, and persona icons in the Personas view and Chamber.
9. Check `git status --short` and keep local session databases, SQLite sidecars, generated screenshots, and smoke-test artifacts out of the PR unless they are intentional fixtures.
10. Include screenshots for visible chamber or layout changes.

## PR Expectations

- Keep changes focused.
- Describe the user-facing behavior change.
- Note any provider-specific assumptions or local CLI requirements.
- Mention whether you tested persistent debater threads, replay fallback, or both.

## Issues

Bug reports and feature ideas are welcome. Please include reproduction steps, provider/preset details, and whether the problem appeared in the live app or only in static review.
