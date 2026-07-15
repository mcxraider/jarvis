---
name: sync-local
description: Sync a local checkout with a remote branch via git pull, surfacing any merge conflicts to the user for a decision instead of resolving them silently. Use this whenever the user says "sync-local", "sync from remote", "pull main", "update my local from origin", "get the latest from a branch", or otherwise wants their local working copy (e.g. the local Jarvis server) brought up to date with origin. Trigger even if they just name a branch and say "sync it".
---

# sync-local

Bring the local checkout up to date with `origin/<branch>` safely. The user's local copy is a running server (Jarvis), so the priority is: never lose local work, never guess at a conflict resolution, and always leave the repo in a state the user understands.

## Inputs

- **branch** — taken from the user's prompt (e.g. "sync-local main"). If no branch is given, use the current branch and say so; if the current branch is detached or ambiguous, ask.

## Workflow

### 1. Pre-flight

Run these and report anything notable before touching the remote:

```bash
git rev-parse --abbrev-ref HEAD   # current branch
git status --short                # uncommitted changes
git stash list                    # pre-existing stashes
```

If the working tree is dirty, stop and ask the user which they want:
- **stash** — `git stash push -u -m "sync-local auto-stash"`, pull, then `git stash pop` (and surface any conflicts from the pop the same way as below)
- **commit first**
- **discard** — only if they explicitly say so
- **abort the sync**

Never discard or hard-reset on your own initiative. Uncommitted work on a live server is often the only copy.

### 2. Pull

```bash
git fetch origin <branch>
git pull origin <branch>
```

Report the outcome plainly: already up to date, fast-forwarded (with a short `git log --oneline HEAD@{1}..HEAD`), merged, or conflicted.

### 3. If there are conflicts

Do **not** resolve them. Gather the evidence and hand the decision back:

```bash
git status --short | grep '^UU\|^AA\|^DU\|^UD'   # conflicted paths
git diff                                          # conflict hunks
```

Then present, per conflicted file:
- the file path
- the conflicting hunks, with "ours" (local) and "theirs" (remote) clearly labelled
- a one-line read on what each side seems to be doing, and a recommendation if there's an obvious one

Then ask what to do. Offer the realistic options: take ours (`git checkout --ours <file>`), take theirs (`git checkout --theirs <file>`), a hand-written merge, or abort the whole pull (`git merge --abort`) and leave things exactly as they were. Different files can go different ways.

After the user decides, apply it, `git add` the resolved paths, and commit the merge. Show `git status` to confirm the tree is clean.

### 4. Post-sync notes (report, don't act)

The pull may have landed changes that need a follow-up on a running server. Check what changed and mention only what's relevant:

```bash
git diff --stat HEAD@{1} HEAD
```

Flag if any of these moved: dependency manifests (`requirements.txt`, `pyproject.toml`, `uv.lock`, `package.json`), `.env.example` or config schemas, database/Supabase migrations, or the process entrypoint. Suggest the follow-up (reinstall deps, run migrations, restart the service) — but let the user run it unless they ask you to.

## Guardrails

- No `git push`, `git reset --hard`, `git clean -fd`, or force operations as part of a sync. If the situation seems to call for one, say why and ask.
- If the pull fails for a non-conflict reason (no upstream, detached HEAD, auth failure, diverged history requiring rebase), report the actual git error verbatim and ask before improvising.
- End every run with a one-paragraph summary: branch, what came in, conflicts and how they were resolved, anything the user still needs to do.
