---
name: live-debate-smoke
description: Run the repo-specific live 3-debater smoke test for LLM Debate Hall. Use when you need to verify that a real debate can complete on the topic "AGI should not be deployed at scale until interpretability and control methods are good enough to reliably detect dangerous deception", with three debaters each speaking three times, and a Markdown report logging transcript output, models, personas, retries, and summarized views. The smoke test should keep iterating through real-provider lineups until it gets a valid debate result or reaches a concrete blocker.
---

# Live Debate Smoke

Run the repo-owned smoke runner:

```bash
python3 scripts/run_live_debate_smoke.py
```

## Workflow

1. Execute the runner exactly once.
- The runner probes real validated providers from the repo's preset registry.
- It uses three fixed built-in personas:
  - `stoic_rationalist`
  - `pragmatic_engineer`
  - `humanist_mediator`
- It runs the required AGI topic with exactly 3 debaters.

2. Trust the runner's validation and retry behavior.
- A passing run must save 9 debate messages, with each debater speaking exactly 3 times.
- Provider errors, invalid turn output, missing transcript entries, or failed session status are treated as failures.
- The runner keeps iterating with different real-provider lineups until it gets a valid debate result or reaches a concrete blocker after exhausting attempts.

3. Read the generated Markdown report.
- Default output directory: `artifacts/live-debate-smoke/`
- The report contains:
  - attempt history
  - model and persona used for each seat
  - debate transcript output
  - concise summaries of all three debater views
  - blocker details if no valid real-provider run succeeds

## Flags

Use flags only when needed:

```bash
python3 scripts/run_live_debate_smoke.py --max-attempts 8 --report-dir artifacts/live-debate-smoke --topic "..."
```
