# Contributing

Debate Hall is a for-fun experimental project. Small fixes, bug reports, and ideas are more useful than large speculative refactors.

## Before Opening A PR

1. Run `pytest -q`.
2. Run `node --check llm_debate_hall/static/app.js`.
3. For UI or orchestration changes, run the live app with `uvicorn llm_debate_hall.main:app --reload` and verify the flow in the browser at `http://127.0.0.1:8000`.
4. Include screenshots for visible arena or layout changes.

## PR Expectations

- Keep changes focused.
- Describe the user-facing behavior change.
- Note any provider-specific assumptions or local CLI requirements.
- Mention whether you tested persistent debater threads, replay fallback, or both.

## Issues

Bug reports and feature ideas are welcome. Please include reproduction steps, provider/preset details, and whether the problem appeared in the live app or only in static review.
