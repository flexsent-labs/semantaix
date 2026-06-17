#!/usr/bin/env bash
# Verify Google Calendar OAuth is production-ready on a deployed host.
set -euo pipefail

DOMAIN="${DEPLOY_DOMAIN:-semantaix.flexsentlabs.com}"
BASE_URL="${VERIFY_BASE_URL:-https://${DOMAIN}}"

echo "=== calendar oauth health ==="
body="$(curl -fsS "${BASE_URL}/api/health/calendar-oauth")"
echo "$body" | python3 -m json.tool
echo "$body" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)'

echo "=== initiate redirect_uri (optional; needs INTERNAL_SERVICE_TOKEN) ==="
if [ -n "${INTERNAL_SERVICE_TOKEN:-}" ]; then
  consent="$(
    curl -fsS -X POST "${BASE_URL}/api/calendar/connect/initiate" \
      -H "Authorization: Bearer ${INTERNAL_SERVICE_TOKEN}" \
      -H "Content-Type: application/json" \
      -d '{"project_id":1,"operator":"@oauth-probe"}' \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["consent_url"])'
  )"
  python3 - "$consent" "$DOMAIN" <<'PY'
import sys
import urllib.parse as u

consent, domain = sys.argv[1], sys.argv[2]
expected = f"https://{domain}/api/calendar/oauth/callback"
params = u.parse_qs(u.urlparse(consent).query)
redirect = u.unquote(params.get("redirect_uri", [""])[0])
if redirect != expected:
    print(f"ERROR: redirect_uri mismatch: {redirect!r} != {expected!r}", file=sys.stderr)
    sys.exit(1)
print(f"redirect_uri ok: {redirect}")
PY
else
  echo "skip initiate probe (INTERNAL_SERVICE_TOKEN not set)"
fi

echo "calendar oauth production check: OK"
