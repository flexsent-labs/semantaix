# Rebuild & Restart Services
1. cd to the active worktree (verify with `git rev-parse --show-toplevel`)
2. git checkout main && git pull
3. Ensure Docker daemon is running; start Docker Desktop and poll if not
4. docker compose build && docker compose up -d
5. **Restart nginx** so it re-resolves the recreated backends' IPs:
   `docker compose restart nginx`. nginx caches each upstream IP at startup
   (the config uses a literal-hostname `proxy_pass http://api:8000` with no
   `resolver`), and `up -d` recreates api/bot_gateway with NEW container IPs —
   so without this restart nginx keeps proxying to the dead old IPs and returns
   **502 Bad Gateway** to the Telegram webhook (the customer's messages silently
   stop arriving).
6. Verify all containers are healthy AND the proxied routes are reachable —
   not just the internal health checks. The webhook path must answer through
   nginx: `curl -s -o /dev/null -w '%{http_code}' http://localhost/telegram/webhook`
   should be **405** (reachable, POST-only), **never 502**. Report status.
