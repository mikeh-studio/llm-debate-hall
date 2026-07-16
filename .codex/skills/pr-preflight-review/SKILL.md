---
name: pr-preflight-review
description: Prepare llm-debate-hall changes for PR by reviewing the diff against the base branch, cleaning touched code, checking regression and security risk, running validation and manual smoke tests, updating relevant docs, and ensuring generated artifacts stay out of the PR before handing off to commit-push-pr.
---

# PR Preflight Review

Use this skill before pushing a branch for review.

## Goal

Take the current branch from "implemented" to "reviewer-ready" without widening scope unnecessarily.

This skill does not handle commit, push, or PR creation.
After preflight is clean, use `commit-push-pr`.

## Workflow

1. Inspect branch state and PR scope.
- Run `git status --short --branch`.
- Detect the base branch from `origin/HEAD`, falling back to `main`.
- Review the actual branch diff:
  - `git log origin/<base>..HEAD --oneline`
  - `git diff --stat origin/<base>...HEAD`
  - `git diff origin/<base>...HEAD`
- Identify unrelated, accidental, generated, or stale changes before doing anything else.

2. Run Diff Review.
- Review the diff like a PR reviewer, not the implementer.
- Focus on:
  - user-visible behavior changes
  - hidden coupling between `engine.py`, `storage.py`, `main.py`, adapters, and frontend state
  - missing tests
  - docs drift
  - risky UI state or transcript-flow edits
- Prefer real reviewer findings over style-only comments.

3. Clean touched code.
- Improve readability in already-touched files only.
- Tighten naming, conditionals, small duplication, and comments where useful.
- Do not expand scope into unrelated refactors.
- Preserve unrelated user changes in a dirty worktree.

4. Run Risk Review.
- Look for regressions and edge cases in:
  - debate startup flow
  - live transcript rendering
  - pending/live thread entry handling
  - judge vs debater controls
  - provider-session persistence and fallback
  - exports, replay, and transcript history
  - timeout and reset behavior
- If a risk remains unresolved, surface it clearly instead of burying it.

5. Run Security Check.
- Review the diff for security-sensitive mistakes, especially in:
  - auth or permission checks
  - user or model-controlled input handling
  - prompt/output trust boundaries
  - subprocess execution and shell invocation
  - file writes, exports, and path handling
  - secrets, env vars, and local credential leakage
  - XSS or unsafe HTML rendering in frontend changes
- Call out anything that could expose data, execute the wrong thing, or let unsafe output cross into persistence or execution.
- If a security concern is unresolved, do not mark the branch reviewer-ready.

6. Run validation.
- Start with targeted checks based on changed files.
- Run repo-standard checks when relevant:
  - `pytest -q`
  - `node --check llm_debate_hall/static/app.js`
  - `PYTHONPYCACHEPREFIX=/tmp/llm-debate-hall-pyc python3 -m compileall llm_debate_hall tests`
- Fix in-scope failures when feasible. If not, stop and report the blocker.

7. Run Manual Smoke Test.
- For UI, orchestration, adapter, or runtime changes, run the app locally and test the affected flow in the browser.
- Use the real affected flow, not a static code review substitute.
- Prioritize:
  - setup to debate start
  - arena rendering and live updates
  - transcript behavior
  - judge and debater edit/reset flows
  - pixel stage if touched
  - provider fallback/recovery if touched
- If UI changed, collect screenshot evidence for the PR.

8. Update relevant docs.
- Update only docs that should change because of the shipped behavior:
  - `README.md`
  - `CONTRIBUTING.md`
  - smoke-test or setup instructions
- Keep docs aligned with actual behavior. Do not rewrite broadly without need.

9. Enforce Artifact Hygiene.
- Check that local-only artifacts are not accidentally included:
  - screenshots
  - smoke-test reports
  - `.db` files
  - caches
  - debug output
  - temporary exports
- Update `.gitignore` if needed, or leave the files unstaged.
- Confirm the intended PR contains source changes only.

10. Prepare reviewer context.
- Summarize:
  - what changed
  - why it changed
  - main risks
  - security findings
  - validation performed
  - manual smoke coverage
  - docs updated
  - any caveats or blockers
- If UI changed, mention screenshot evidence.
- If blockers remain, do not hand off as PR-ready.

11. Hand off to final PR flow.
- If the branch is clean and reviewer-ready, use `commit-push-pr`.

## Guardrails

- Do not widen scope into opportunistic refactors.
- Do not ignore failing tests or broken smoke flows.
- Do not stage generated artifacts unless they are intentional source files.
- Do not open a PR with unresolved blockers unless the user explicitly asks for that.
- Prefer reviewer clarity over cleverness.

## Required Output

Before handing off, provide:
- short change summary
- diff-review findings, or explicit note that none were found
- main risks and whether they were addressed
- security findings, or explicit note that none were found
- validation summary
- manual smoke summary
- doc updates made
- artifact hygiene result
- remaining caveats or blockers
- whether the branch is ready for `commit-push-pr`
