---
name: merge-mvp-restart
description: Pull the latest mvp branch from origin, merge it into main, and restart the Jarvis stack. Use this whenever the user says "pull mvp and merge with main", "merge mvp into main and restart jarvis", "ship mvp to main", "sync mvp into main then restart", or otherwise wants the mvp branch brought into main and the live assistant cycled to pick it up.
---

# merge-mvp-restart

Bring `origin/mvp` into `main` and restart the live Jarvis stack so it runs the merged code. This repo uses multiple git worktrees, so the branch you need often isn't the one checked out where the session started — find the right worktree before touching git.

## 0. Locate the worktrees

```bash
git worktree list
```

`main` and `mvp` are each expected to be checked out in their own dedicated worktree (not the throwaway `worktree-bridge-cse_*` ones sessions run in). On this machine that's typically:

- `main` → `/Users/jerry/projects/jarvis-mcp`
- `mvp` → `/Users/jerry/projects/jarvis-mvp`

If either branch isn't checked out anywhere, say so and ask how to proceed rather than checking it out over other work.

## 1. Pull mvp

In the mvp worktree:

```bash
cd /Users/jerry/projects/jarvis-mvp
git status --short          # must be clean; if dirty, stop and ask (stash/commit/discard/abort)
git fetch origin mvp
git pull origin mvp
```

Report fast-forward vs merge vs conflict. Never resolve conflicts unilaterally — surface the conflicting hunks (ours vs theirs) and ask, same as `sync-local` does.

## 2. Merge mvp into main

In the main worktree:

```bash
cd /Users/jerry/projects/jarvis-mcp
git rev-parse --abbrev-ref HEAD             # confirm it's main
git status --short -- ':!logs'              # ignore log-file churn from the running server, check everything else is clean
git log main..mvp --oneline                 # preview what's about to land
git merge mvp --no-edit
```

If the merge conflicts, stop and hand the decision back to the user exactly as `sync-local` describes (per-file, ours vs theirs, no unilateral resolution).

**Do not `git push`.** The merge stays local unless the user explicitly asks to push — pushing `main` is a shared-state change that needs separate confirmation.

## 3. Restart Jarvis

Follow the `jarvis-restart` skill in full: capture before-state (`jarvis ps`, `jarvis url`), run `jarvis restart`, then verify (`jarvis ps`, `jarvis health`). Report the public ngrok URL and flag prominently if it changed.

## Report

One short summary covering:
- What landed in main (commit range / short log from `git log main..mvp` before the merge)
- Whether the merge was clean or needed conflict resolution
- Confirmation the merge is local-only (not pushed), unless the user asked to push
- Restart result: before → after per service, health, and public URL (changed or unchanged)

## Guardrails

- Never push or force-push without explicit request.
- Never resolve merge conflicts without showing the user the hunks first.
- Don't restart Jarvis repeatedly hoping a failure clears — if the first restart doesn't come back healthy, read the logs and report verbatim rather than bouncing again.
- Operate on the worktree that actually has the target branch checked out; don't `git checkout` a branch over a worktree that's mid-work on something else.
