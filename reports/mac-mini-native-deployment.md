# Mac Mini Native Deployment — Provisioning, Root-Cause Analysis & Remote Control

**Date:** 2026-07-15
**Host:** `jerry@100.74.184.78` (Tailscale) — Mac mini Late 2014, Intel i5-4278U @ 2.60GHz (dual-core), 8 GB RAM, macOS 12.7.6 Monterey (21H1320)
**Repo:** `/Users/jerry/Desktop/jarvis-mcp` — branch `mvp` @ `bcca14c`
**Scope:** Diagnosing a failed `./scripts/start_servers.sh`, provisioning the mini from scratch, evaluating Docker, and building CLI remote control from the laptop.
**Outcome:** Stack runs natively on the mini and is fully drivable from the laptop via `jarvis`. Docker deferred (see §7).

---

## 1. Presenting symptom

```
==> Starting ngrok
==> Waiting for ngrok tunnel
Failed to get ngrok tunnel URL. Check /tmp/ngrok.log
```

The error was misleading. `/tmp/ngrok.log` contained exactly one line:

```
./scripts/start_servers.sh: line 45: ngrok: command not found
```

**Mechanism.** `start_servers.sh:45` backgrounds ngrok and redirects to the log:

```bash
ngrok http 3000 --log=stdout > /tmp/ngrok.log 2>&1 &
```

The shell's "command not found" lands in the log rather than the terminal. The script then polls `127.0.0.1:4040/api/tunnels` for 20s, times out, and reports a generic tunnel failure. A missing binary is indistinguishable from a tunnel that failed to open.

## 2. Root cause — the repo was **copied**, not cloned

ngrok was only the first of three failures. The underlying cause: this repo was copied from the Apple Silicon Mac (`/Users/Spare/Desktop/jarvis-mcp`) onto the Intel mini, bringing a foreign `venv` with it.

| # | Failure | Evidence | Real cause |
|---|---|---|---|
| 1 | `ngrok: command not found` | `/tmp/ngrok.log` | ngrok never installed |
| 2 | `nohup: uvicorn: No such file or directory` | `/tmp/jarvis-agent.log` | **Not** a missing uvicorn. `venv/bin/uvicorn` existed, but its shebang read `#!/Users/Spare/Desktop/jarvis-mcp/venv/bin/python3` — a path that does not exist on the mini. The *interpreter* was missing. |
| 3 | `npm: command not found` | `/tmp/jarvis-dev.log` | node/npm not installed at all; no nvm; `node_modules` absent |

Additional confirmation:

```
$ file venv/bin/python3
venv/bin/python3: Mach-O 64-bit executable arm64      # mini is x86_64
$ venv/bin/python --version
zsh: bad CPU type in executable
```

**Why it stayed hidden.** `start_servers.sh` guards venv creation on directory existence:

```bash
if [ ! -d "$REPO_DIR/venv" ]; then
  python3 -m venv venv && pip install -r requirements.txt
else
  source venv/bin/activate      # <-- sources the broken arm64 venv
fi
```

A present-but-unusable `venv` is treated as valid, so the script never rebuilds and the arch mismatch surfaces later as confusing "file not found" errors.

The script also prints `All set:` after emitting `WARNING:` lines for both services — a successful-looking summary over a stack that is actually down.

## 3. Environment constraints discovered

These shaped every decision that followed and are worth keeping on record:

- **Homebrew lives at `/usr/local`**, not `/opt/homebrew` — this is an Intel Mac.
- **macOS 12 is EOL → Homebrew Tier 3 → no bottles.** Every formula builds from source. `brew install node` began compiling `fmt` via cmake with `llvm` and `swig` still queued behind it. Estimated hours, with a meaningful chance of failure.
- **`sudo` requires a password** — no passwordless sudo, so `.pkg` installers and `pmset` are out of reach for automation.
- **`/usr/local` is not writable, but `/usr/local/bin` is.** Installs must target `$HOME` and symlink into `/usr/local/bin`.
- **Non-interactive SSH has a bare PATH.** `ssh host 'cmd'` runs with `/usr/bin:/bin:/usr/sbin:/sbin` — no `node`, `npm`, or `ngrok`. The login shell (`zsh -lic`) has `/usr/local/bin` first. This produced several false "command not found" readings during diagnosis and is now handled explicitly in tooling.

### 3.1 A blocked Homebrew lock (and what it turned out to be)

`brew install node` failed with:

```
Error: A `brew install node` process has already locked /usr/local/Cellar/pkgconf.
```

A live `brew install qemu` (pid 16064, elapsed **7h45m**) held the lock, actively compiling gettext. It was terminated with the owner's approval.

**In hindsight this was almost certainly an in-flight colima/Docker setup** — `~/.colima` exists on the mini, and qemu is colima's VM backend on Intel Monterey. No colima/lima binary was ever installed, so the attempt never completed. Noted here because the lost build time was real.

## 4. Provisioning performed

Homebrew was abandoned for anything with heavy dependencies. Prebuilt binaries only.

| Component | Method | Result |
|---|---|---|
| ngrok | `brew install ngrok` (cask, binary artifact — no source build) | 3.39.9 |
| ngrok auth | `ngrok config add-authtoken $NGROK_AUTH_TOKEN` (read from `.env`) | saved to `~/Library/Application Support/ngrok/ngrok.yml` |
| Node | Official `node-v22.23.1-darwin-x64.tar.gz` → `~/.local/node-dist`, symlinked `node`/`npm`/`npx` into `/usr/local/bin` | node v22.23.1, npm 10.9.8 |
| Python | `uv` 0.11.28 → `uv python install 3.10` | CPython 3.10.20 x86_64, installed in 2.19s |
| venv | Rebuilt from a `pip freeze` of the working laptop venv (69 packages) | `venv/bin/python3` now x86_64; uvicorn shebang now `/Users/jerry/...` |
| Node deps | `npm install` | 519 packages |

**On ngrok's authtoken:** `start_servers.sh` never runs `ngrok config add-authtoken`, so any fresh ngrok install fails authentication on the next run even once the binary exists. `NGROK_AUTH_TOKEN` is present in `.env` but unused by the script. Worth wiring in.

### 4.1 `requirements.txt` is unsatisfiable — it was **not** used

The venv was deliberately **not** built from `requirements.txt`, because the file cannot be installed:

1. On Python 3.10: `langgraph-api==0.10.0` requires `Python>=3.11` → rejected.
2. On Python 3.11: `langgraph-api==0.10.0` requires `starlette>=1.0.1`, but the file pins `starlette==0.41.3` (itself required by `fastapi==0.115.6`) → **unsatisfiable at any Python version**.

It also does not describe the environment that actually works. Ground truth from the laptop venv (Python 3.10.0), where `import agents.api` succeeds:

| Package | Working venv | `requirements.txt` |
|---|---|---|
| `langgraph` | **0.6.11** | `1.2.7` |
| `langgraph-api` | **not installed** | `0.10.0` |
| `fastapi` | 0.115.6 | 0.115.6 |
| `starlette` | 0.41.3 | 0.41.3 |
| `uvicorn` | 0.34.0 | — |

The file looks bumped-but-never-installed. Note the agent starts as:

```bash
nohup uvicorn agents.api:app --host 127.0.0.1 --port 8000     # start_servers.sh:120
```

which needs neither `langgraph-api` nor `langgraph-cli` — those serve `langgraph dev` / Studio. `langgraph.json` further declares `"python_version": "3.12"`, contradicting the 3.10 venv.

**Resolution taken:** mirror the known-good environment (69-package freeze, Python 3.10). This is a workaround, not a fix — see §10.

### 4.2 `package-lock.json` churn (reverted)

`npm install` on the mini produced 12 deletions in `package-lock.json`, stripping `"libc": ["glibc"]` / `["musl"]` fields. Cause: the lock was written by **npm 11.2.0** (laptop), while the mini runs **npm 10.9.8**, which does not emit those fields. Reverted via `git checkout -- package-lock.json`. This will recur on every `npm install` from the mini until npm versions are aligned.

## 5. Verification

`./scripts/start_servers.sh` completes clean, with no warnings:

```
==> Starting ngrok
    https://bcf6-116-87-102-52.ngrok-free.app
==> Waiting for webhook registration
    webhook configured and stable for 4s
==> Waiting for agent health check
    agent healthy
```

Verified independently of the script's own reporting (its comments correctly note that a log match alone is not proof of stability):

| Check | Result |
|---|---|
| Processes alive 20s after start (`ngrok`, `ts-node`, `uvicorn`) | all UP |
| `curl localhost:3000/health` | **200** |
| `curl 127.0.0.1:8000/health` | **200** |
| `curl $NGROK_URL/` (end-to-end through tunnel) | 404 — correct; no route at `/`, proves ngrok→web routing |
| `import agents.api` in the mini's venv | OK |

Note `/` returns 404 on both services by design; only `/ping`, `/health`, `/webhook/*` are routed. `/ok` does not exist — an early probe against it produced a misleading 404.

## 6. Git state corrected

Remote: `git@github.com:mcxraider/jarvis.git`

**The mini's `main` branch was mislabeled.** Local `main` sat on `bcca14c` — which is `origin/mvp`'s HEAD, not `origin/main` (`a7a389b`). A `git push` from it would have pushed mvp work onto `main`.

An earlier reading that "36 commits are unpushed" was **wrong**: those commits are fully pushed to `origin/mvp`. They are 36 ahead only of `origin/main`.

Branches now:

```
* mvp                  bcca14c [origin/mvp]                    <- checked out
  main                 a7a389b [origin/main]                   <- repointed to real origin/main
  latency-reduction-p0 6bea483 [origin/latency-reduction-p0]
```

Nothing was lost: local `main`'s commit was identical to `origin/mvp` (0 ahead / 0 behind), now preserved as the `mvp` branch.

**`logs/` is tracked in git — 299 files.** The servers write to `logs/app.log` continuously, so the working tree is dirty the moment the stack runs, and `git pull` will conflict. `.gitignore` correctly excludes `/node_modules`, `venv/`, and `.env*`, so the copied-venv failure cannot recur through git — it came from copying the folder.

## 7. Docker evaluation — deferred, running native

**Verdict: the compose stack targets a cloud VPS, not this mini.** `DEPLOYMENT.md` is explicit: EC2/VPS in `ap-southeast-1` with a static public IP, a DNS `A` record, and inbound 80/443.

Blockers on the mini:

- **Caddy cannot work behind home NAT.** It needs public DNS + inbound 80/443 for ACME certificates. The mini is NAT'd — which is precisely why ngrok exists here.
- **Three required vars are missing from `.env`**: `AGENT_POSTGRES_DSN`, `WEB_POSTGRES_DSN`, `PUBLIC_DOMAIN`. All use `${VAR:?err}`, so `docker compose config` fails interpolation immediately. The compose has never been run.
- **Hardware.** On Intel macOS, Docker runs a Linux VM. A 2014 dual-core i5 with 8 GB hosting Node + Python + Caddy — while building FFmpeg and native Python wheels inside that VM — is impractical.
- **macOS 12** cannot use Virtualization.framework (needs 13+), so colima is forced onto QEMU, which has no bottle → the multi-hour source build seen in §3.1. Recent Docker Desktop releases also drop macOS 12.

**Decision: run native on the mini (option C).** If Docker is revisited, the answer is a cloud VPS, driven from the laptop via `docker context create <name> --docker "host=ssh://..."`. The laptop already has Docker CLI 28.2.1, and a context works against any remote daemon — so the "control it from my CLI" goal is satisfied either way.

## 8. Remote control tooling

Both scripts live **outside the git repo**, deliberately: `logs/` is tracked, so anything added inside the repo dirties the tree and fights `git pull`.

**On the mini — `/usr/local/bin/jarvis-status`**

```
SERVICE STATUS    PID     UPTIME     CPU%   MEM     PORT   HEALTH
ngrok   up        90100   00:44      0.3    24M     4040   -
web     up        90150   00:42      102.1  413M    3000   200
agent   up        90182   00:26      0.1    83M     8000   200

tunnel  https://bcf6-116-87-102-52.ngrok-free.app
branch  mvp @ bcca14c
logs    /tmp/ngrok.log  /tmp/jarvis-dev.log  /tmp/jarvis-agent.log
```

It curls `/health` rather than only checking liveness, so a wedged-but-running process reports `unhealthy` — something plain `ps` would miss. Exit code: `0` all healthy, `1` if anything is down.

**Live mode:** `jarvis ps -w [-n SEC]` (default 2s) refreshes until Ctrl-C. The refresh loop
runs *on the mini* over a single SSH session rather than reconnecting each tick.

**CPU% in live mode is computed, not read.** `ps -o %cpu` on macOS reports a *lifetime
average* — cumulative CPU time over process age — so it barely moves on a long-running
process and is useless for watching load arrive. Live mode instead samples cumulative
CPU-seconds (`ps -o cputime`) each tick and diffs them against the previous tick, giving a
true instantaneous figure. The header shows `CPU%*` once a delta is available; the first
tick, and any tick after a pid change (i.e. a restart), falls back to the lifetime average.

Verified against real load — a `curl` flood at `web:3000/health`:

| Frame | web CPU% | MEM |
|---|---|---|
| baseline | 0.0 | 418M |
| baseline | 0.3 | 418M |
| under load | **14.0** | 421M |
| under load | **13.0** | 425M |
| under load | **17.5** | 430M |
| after | 4.7 | 431M |

The same load under `ps -o %cpu` would have read a flat `0.0`.

Resolution note: `ps -o cputime` has centisecond granularity, so at `-n 1` the smallest
non-zero reading is 1.0%. The 2s default halves that to 0.5%.

**On load average:** the status line shows it for trend, but macOS counts I/O-blocked threads
in load — not just runnable ones — so it is *not* a clean CPU-saturation signal. The mini
idles around 1.9–2.0 with no meaningful CPU consumer. Trust the per-process CPU% column.

**`jarvis` — installed on BOTH machines** (`~/.local/bin/jarvis` on the laptop, `/usr/local/bin/jarvis` on the mini). It is one script: it detects whether the repo exists locally and either acts directly (on the mini) or drives it over SSH (from the laptop). Mirrors docker-compose verbs:

```bash
jarvis ps                 # the table above
jarvis logs [web|agent|ngrok] [-f]
jarvis up | down | restart
jarvis url                # current public ngrok URL
jarvis health             # health codes only; exit 1 if unhealthy
jarvis ssh                # shell on the mini, cd'd into the repo
jarvis git <args...>      # git command executed in the repo on the mini
```

Configurable via `JARVIS_HOST` / `JARVIS_REPO`. Colour is suppressed when stdout is not a TTY, so `jarvis ps | grep agent` stays clean. `jarvis up` wraps commands in `zsh -lic` to avoid the bare-PATH trap from §3.

Both scripts are installed to `/usr/local/bin` on the mini, which is on its login PATH and is writable (`/usr/local` itself is not).

Two bugs were found and fixed after first delivery, both instances of the §3 PATH trap:

1. `jarvis` was installed **only on the laptop**, so running `jarvis ps` while SSH'd into the mini failed with `command not found`. It is a wrapper that SSHes *to* the mini, so it did not exist once you were on it. Fixed by making the script location-aware and installing it on both machines.
2. The unified script then called `jarvis-status` by **bare name** through non-interactive SSH, whose PATH excludes `/usr/local/bin`. `jarvis health` masked this by only using `curl` (in `/usr/bin`). Fixed by calling `/usr/local/bin/jarvis-status` via absolute path.

Verified from both machines across all verbs, including `restart` in local mode.

Verified end-to-end: `down` → all three report down (exit 1); `up` → all three healthy (exit 0) with the webhook re-registered against the rotated ngrok URL.

## 9. Power settings

`pmset` originally showed `sleep 1` — the mini slept after **one minute** idle, which would silently drop the tunnel whenever nobody was logged in. Now set (by the owner, requires sudo):

```bash
sudo pmset -a sleep 0 disksleep 0 autorestart 1
```

`sleep 0` confirmed. `womp 1` (wake-on-LAN) was already set.

## 10. Outstanding items

| # | Item | Notes |
|---|---|---|
| 1 | **`requirements.txt` is unsatisfiable** | Needs reconciliation: decide whether langgraph is 0.6.x or 1.2.x, whether `langgraph-api`/`langgraph-cli` are actually wanted, then regenerate. Until then the mini's venv is a freeze mirror and `pip install -r requirements.txt` **must not** be run there. |
| 2 | `langgraph.json` declares `python_version: 3.12` | Contradicts the 3.10 venv. |
| 3 | **`logs/` tracked in git (299 files)** | Guarantees a dirty tree and pull conflicts. Fix: `git rm -r --cached logs/ && echo "logs/" >> .gitignore`. Not done — rewrites tracked state; owner's call. |
| 4 | **No auto-start on reboot** | `autorestart 1` reboots the machine but nothing restarts the stack. A user-level LaunchAgent (`~/Library/LaunchAgents`, no sudo needed) would close this. |
| 5 | npm version mismatch | laptop 11.2.0 vs mini 10.9.8 → `package-lock.json` churn on every mini `npm install`. |
| 6 | node version mismatch | laptop v20.17.0 vs mini v22.23.1. Both satisfy `engines: node >=16`. |
| 7 | ngrok URL rotates | Free tier gives a random URL per restart. `start_servers.sh` rewrites `.env` and re-registers the webhook, so it self-heals — but any hardcoded URL breaks. A reserved domain (paid) or Tailscale Funnel would stabilise it. |
| 8 | `.env` is untracked (`.gitignore: .env*`) | Correct, but means env changes do not propagate between laptop and mini — sync manually. |

## 11. Working loop from the laptop

```bash
jarvis ps                    # check state
jarvis git pull              # (once logs/ is untracked — see item 3)
jarvis restart               # apply changes
jarvis logs agent -f         # watch
```

Run `npm install` on the mini only when `package.json` changes. Do **not** run `pip install -r requirements.txt` there until item 1 is resolved.
