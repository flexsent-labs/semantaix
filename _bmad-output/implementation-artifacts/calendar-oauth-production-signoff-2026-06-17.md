# Calendar OAuth — Production Signoff

**Date:** 2026-06-17  
**Epic:** 11 (Calendar / availability)  
**Status:** Prod-ready (code + deploy pipeline)  
**Production host:** `semantaix.flexsentlabs.com`

## Goal

Google Calendar OAuth must use the stable production callback URL so operators are not blocked by ngrok/dev redirect limits when connecting calendars via `/connect_calendar`.

## Production configuration

| Setting | Value |
|---------|-------|
| `APP_ENV` | `production` |
| `WEB_UI_BASE_URL` | `https://semantaix.flexsentlabs.com/admin` |
| `GOOGLE_OAUTH_REDIRECT_URI` | `https://semantaix.flexsentlabs.com/api/calendar/oauth/callback` |
| Rendered by | `scripts/render_production_env.py` from `DEPLOY_DOMAIN` |
| Bootstrap | `scripts/digitalocean_bootstrap.sh` patches redirect on first install |

## Code safeguards

1. **`services/api/app/calendar/oauth_production.py`** — rejects ngrok/localhost/trycloudflare redirects when `APP_ENV=production`; requires redirect host to match `WEB_UI_BASE_URL`.
2. **`GET /api/health/calendar-oauth`** — returns `200` when prod-ready, `503` when misconfigured (used by deploy workflow).
3. **Startup** — logs `calendar_oauth_production_ready` or raises critical incident `calendar_oauth_production_misconfigured` in production.
4. **`scripts/verify_production_calendar_oauth.sh`** — post-deploy probe in `.github/workflows/deploy.yml`.

## Google Cloud Console (manual — required once)

1. Open [Google Cloud Console](https://console.cloud.google.com/) → project with Calendar OAuth client.
2. **APIs & Services → Credentials** → OAuth 2.0 Client ID used by Semantaix.
3. Under **Authorized redirect URIs**, add:
   ```
   https://semantaix.flexsentlabs.com/api/calendar/oauth/callback
   ```
4. Remove or keep dev/ngrok URIs only for local development (not on production droplet).
5. Save. No app redeploy needed for Console-only changes.

## Verification checklist

- [ ] `curl -fsS https://semantaix.flexsentlabs.com/api/health/calendar-oauth` → `"ok": true`, `"prod_ready": true`
- [ ] Production `.env` on droplet shows production redirect (not ngrok)
- [ ] Google Console lists production redirect URI
- [ ] Operator `/connect_calendar` completes OAuth and returns to admin UI
- [ ] GHA deploy step **Verify calendar OAuth production readiness** passes

## Local development

Local `.env` may keep an ngrok or tunnel redirect URI. Production deploy always overwrites via `render_production_env.py`. Dev tunnels are intentionally allowed when `APP_ENV != production`.

## Related tests

- `tests/test_calendar_oauth_production.py`
- `tests/test_api_health_calendar_oauth.py`
- `tests/test_render_production_env_calendar.py`
