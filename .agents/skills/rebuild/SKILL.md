---
name: rebuild
description: Use when asked to restart, redeploy, rebuild, bounce, or bring up the Semantaix services / live Docker stack — typically after merging a change to main.
---

# Rebuild & Restart Services

Run the bundled script — it redeploys the live Semantaix stack from `origin/main`
and restarts it **autonomously (no prompts, no questions)**:

```bash
bash "$(git rev-parse --show-toplevel)/.Codex/skills/rebuild/restart.sh"
```

It is safe to run unattended and exits non-zero (with a clear message) if it
can't proceed safely. Do not fall back to manual `docker compose` / `git`
commands unless the script aborts and tells you why.

## What it does

1. **Locates the deploy dir** = the working_dir of the running `semantaix`
   compose project (falls back to the main worktree root). Works no matter which
   worktree you invoke it from.
2. **Advances that checkout to `origin/main` — fast-forward only.** It does NOT
   `git checkout main` (main is usually checked out/locked in another worktree),
   and it ABORTS rather than clobbering if the deploy dir has uncommitted tracked
   changes or has diverged from `origin/main`.
3. `docker compose build && docker compose up -d`.
4. **Restarts nginx** (required). nginx `proxy_pass` uses literal hostnames with
   no `resolver`, so it caches each upstream IP at startup; `up -d` recreates
   api/bot_gateway with NEW IPs, and without this restart nginx keeps proxying
   the dead old IPs → **502 Bad Gateway** on the Telegram webhook (the customer's
   messages silently stop arriving). This was a real incident.
5. **Verifies through nginx** (not just internal health): `GET /telegram/webhook`
   → **405** (reachable, POST-only; never 502) and `/api/health/live` → **200**.

## If it aborts

- *"uncommitted tracked changes"* or *"not a fast-forward"*: the deploy dir needs
  manual reconciliation — run `git -C <deploy-dir> status` and resolve. The
  script refuses to force state or discard local commits.
- *webhook still 502 after a run*: re-run it (re-restarts nginx); if it persists,
  check `docker compose logs nginx` and backend health.
