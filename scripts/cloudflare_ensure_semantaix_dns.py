#!/usr/bin/env python3
"""Ensure semantaix.flexsentlabs.com A record points at the production droplet."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
ZONE_NAME = "flexsentlabs.com"
RECORD_NAME = "semantaix"
DEFAULT_DOMAIN = "semantaix.flexsentlabs.com"


def _request(method: str, path: str, *, token: str, body: dict | None = None) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise SystemExit(f"Cloudflare HTTP {exc.code}: {payload}") from exc


def resolve_zone_id(token: str, account_id: str | None) -> str:
    zone_id = os.environ.get("CLOUDFLARE_ZONE_ID", "").strip()
    if zone_id:
        return zone_id

    query = f"/zones?name={ZONE_NAME}"
    if account_id:
        query += f"&account.id={account_id}"
    payload = _request("GET", query, token=token)
    if not payload.get("success"):
        die_errors("zone lookup failed", payload)
    zones = payload.get("result") or []
    if not zones:
        print(
            "ERROR: flexsentlabs.com is not visible in this Cloudflare account/token.\n"
            "Add the site in Cloudflare (account dad0c564…) or set CLOUDFLARE_ZONE_ID\n"
            "from Dashboard → flexsentlabs.com → Overview → Zone ID.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return zones[0]["id"]


def die_errors(prefix: str, payload: dict) -> None:
    errors = payload.get("errors") or payload
    print(f"ERROR: {prefix}: {errors}", file=sys.stderr)
    raise SystemExit(1)


def ensure_a_record(token: str, zone_id: str, ip: str) -> None:
    fqdn = f"{RECORD_NAME}.{ZONE_NAME}"
    listed = _request("GET", f"/zones/{zone_id}/dns_records?name={fqdn}&type=A", token=token)
    if not listed.get("success"):
        die_errors("dns list failed", listed)
    body = {
        "type": "A",
        "name": RECORD_NAME,
        "content": ip,
        "ttl": 1,
        "proxied": False,
    }
    records = listed.get("result") or []
    if records:
        record_id = records[0]["id"]
        updated = _request(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{record_id}",
            token=token,
            body=body,
        )
        if not updated.get("success"):
            die_errors("dns update failed", updated)
        print(f"updated A {fqdn} -> {ip} (dns only)")
        return

    created = _request("POST", f"/zones/{zone_id}/dns_records", token=token, body=body)
    if not created.get("success"):
        die_errors("dns create failed", created)
    print(f"created A {fqdn} -> {ip} (dns only)")


def set_ssl_strict(token: str, zone_id: str) -> None:
    if os.environ.get("CLOUDFLARE_SKIP_SSL", "").lower() in {"1", "true", "yes"}:
        return
    payload = _request(
        "PATCH",
        f"/zones/{zone_id}/settings/ssl",
        token=token,
        body={"value": "strict"},
    )
    if not payload.get("success"):
        print(f"warn: could not set SSL strict: {payload.get('errors')}", file=sys.stderr)
    else:
        print("ssl mode: strict")


def main() -> None:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    ip = os.environ.get("DEPLOY_HOST", "").strip()
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip() or None
    if not token:
        print("ERROR: CLOUDFLARE_API_TOKEN is required", file=sys.stderr)
        raise SystemExit(1)
    if not ip:
        print("ERROR: DEPLOY_HOST is required", file=sys.stderr)
        raise SystemExit(1)

    zone_id = resolve_zone_id(token, account_id)
    ensure_a_record(token, zone_id, ip)
    set_ssl_strict(token, zone_id)
    domain = os.environ.get("DEPLOY_DOMAIN", DEFAULT_DOMAIN)
    print(f"ready: http://{domain}/api/health/live (grey cloud until TLS)")


if __name__ == "__main__":
    main()
