# AGENTS.md

Read `CLAUDE.md` first — it is the map of the codebase (architecture, where files live, logging rules, test layout). `README.md` is the architecture narrative. This file covers only how to **validate and publish** a change.

Treat this as a method, not a command transcript. Adapt paths, tests, branch names, and commit messages to the change being published.

## Logging and diagnostics

Every new logging, trace, diagnostic, or debugger-style output path must use the existing async logger for the layer it touches.

- **TypeScript:** the shared `logger` from `src/utils/logger.ts`, which queues to `src/utils/log-worker.ts`. No `console.log`, no direct Winston transports, no synchronous file writes, no ad-hoc debug files, no request-path logging sinks.
- **Python:** the run logging facilities in `agents/agent_api/app/run_logging.py` (`RunFileLog`, `FileLoggingTracer`, `open_run_log`, `flush_run_logs`, `shutdown_run_logs`). No direct `open()`, `write()`, or `json.dumps()` dump paths in graph/API request execution.
- **Tracing** (LangSmith) goes through `agents/agent_api/app/tracing.py` and the existing `wrap_openai` / `@traceable` boundaries — see the Tracing section in `CLAUDE.md`.
- New diagnostics must be non-blocking, bounded under backpressure, redacted, best-effort on failure, and wired into existing flush/shutdown hooks. Tests inspecting async logs must flush first.

## 1. Confirm the scope

- Run `git status -sb` and inspect both staged and unstaged diffs.
- Treat pre-existing changes as user-owned. Do not discard or rewrite them. This worktree usually carries unrelated in-progress work, so **stage explicit paths** rather than `git add -A` unless the whole worktree is confirmed in scope.
- Never stage Finder/editor duplicates (`* 2.ts`, `* 2.py`, `* 2.md`) or `__pycache__/`.
- Before pushing to `main`, confirm a direct default-branch push is intended. Otherwise use a feature branch and a PR.

## 2. Validate the change

Run the smallest relevant checks for the layers the diff touches:

```bash
# TypeScript
npm run build
npm test -- --runInBand
npm run test:integration -- --runInBand    # only if the diff touches the webhook/gate path
npm run lint

# Python (project root, venv active)
pytest tests/agents/

# SQL / migrations
npm run db:lint
npm run db:migrations
```

Minimum bar: TS changes need `npm run build` plus the affected Jest suites; Python changes need the affected `tests/agents/` files; migrations need `npm run db:lint`.

If `pytest` fails at *collection* with a Starlette version error, the venv has drifted from the pinned version — that is environmental, not your diff.

Run `git diff --check` before staging and `git diff --cached --check` after. Record what passed, what failed, and what you intentionally did not run.

## 3. Stage and review

```bash
git a .                    # only when the entire worktree is confirmed in scope
git diff --cached --stat
git diff --cached
git diff --cached --check
```

`git a` is the user's staging alias. With unrelated work present, use explicit paths instead.

## 4. Create a signed commit

Signed commits are the default here; keep the configured signing policy intact.

```bash
export GPG_TTY="$(tty)"
gpg-connect-agent updatestartuptty /bye
git com "<commit msg>"
git log -1 --show-signature
```

`git com` is the user's commit alias. Verify the resulting commit is signed. If signing fails because PIN entry cannot open, do **not** bypass with `commit.gpgsign=false` — ask the user to unlock the key or run the commit from an interactive terminal. Commit unsigned only on explicit approval.

## 5. Push

```bash
git push origin "$(git branch --show-current)"
```

Then report the branch, commit hash, validation performed, and whether the signature verified. Do not open or update a pull request unless asked.

If a push aborts with `bad object refs/codex/...`, that is a corrupt local ref from another tool, not an auth failure — local git is fine; prune the bad ref before retrying.

## GitHub issues

Every `gh issue create` call must pass `--label` with at least one existing label (`gh label list` to see current set — see CLAUDE.md for the list). Never create an unlabeled issue, and don't invent new labels ad hoc.

## Repository notes

- Remote: `origin` → `git@github.com:mcxraider/jarvis-mcp.git`. Default branch: `main`.
- Node/TypeScript with npm and Jest (`package.json`); Python agent under `agents/agent_api/` with pytest.
- ESLint uses the flat config `eslint.config.js`. Do not reintroduce `.eslintrc.json`.
