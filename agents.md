# Agent Git Publishing Guide

Use this as a method, not as a literal command transcript. Adapt file paths, tests, branch names, and commit messages to the change being published.

## 1. Confirm the scope

- Run `git status -sb` and inspect both staged and unstaged diffs.
- Treat existing changes as user-owned. Do not discard or rewrite them.
- If the worktree contains unrelated changes, stage explicit paths instead of using `git add -A`.
- Before pushing directly to `main`, confirm that a direct default-branch push is intended. Otherwise, use a feature branch and pull request.

## 2. Validate the change

- Run the smallest relevant tests plus any build, type-check, or lint command affected by the diff.
- For Telegram TypeScript changes in this repository, `npm run build` and the relevant Jest suites are a sensible minimum.
- Run `git diff --check` before staging and `git diff --cached --check` after staging.
- Record what passed, failed, or was intentionally not run in the handoff.

## 3. Stage and review

```bash
git add <intended-paths>
git diff --cached --stat
git diff --cached
git diff --cached --check
```

Use `git add -A` only when the entire worktree has been confirmed as the intended scope.

## 4. Create a signed commit

Signed commits are the default for this repository. Keep the configured signing policy intact.

In an interactive terminal, make GPG aware of the current terminal before committing:

```bash
export GPG_TTY="$(tty)"
gpg-connect-agent updatestartuptty /bye
git commit -S -m "<concise commit message>"
git log -1 --show-signature
```

If signing fails because PIN entry cannot open, do not silently bypass signing with `commit.gpgsign=false`. Ask the user to unlock the key or run the signed commit from an interactive terminal. Create an unsigned commit only when the user explicitly approves that exception.

## 5. Push

```bash
git push -u origin "$(git branch --show-current)"
```

After pushing, report the branch, commit hash, validation performed, and whether the commit signature was verified. Do not create or update a pull request unless requested.

## Repository notes

- GitHub remote: `origin` (`git@github.com:mcxraider/jarvis-mcp.git`).
- The root project uses npm, TypeScript, and Jest; see `package.json` for current scripts.
