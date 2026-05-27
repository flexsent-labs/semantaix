"""Tiny in-memory OpenRouter stub used by signoff scripts.

Returns a canned chat completion so demos can exercise `/suggest` end-to-end
without hitting the real provider.

The response body comes from one of these environment variables, checked
in order on every request (so a signoff can `export` a new value between
steps to steer the next call):

1. ``OPENROUTER_STUB_RESPONSE_JSON`` — used verbatim as the ``content``
   string when callers expect schema'd JSON (e.g. the SalesPersonaAnswerer
   parsing ``{"extracted_fields": ..., "next_question": ...}``).
2. ``OPENROUTER_STUB_RESPONSE`` — plain text (default: "Use the reset
   link via the email."), used by guardrail / grounding demos.

The listening port comes from ``OPENROUTER_STUB_PORT`` (default 18500);
signoff scripts that set ``OPENROUTER_BASE_URL=http://127.0.0.1:<port>``
must export this to match.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length", "0"))
        if length > 0:
            self.rfile.read(length)
        response_text = os.environ.get(
            "OPENROUTER_STUB_RESPONSE_JSON"
        ) or os.environ.get(
            "OPENROUTER_STUB_RESPONSE", "Use the reset link via the email."
        )
        body = json.dumps(
            {"choices": [{"message": {"content": response_text}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002,D401
        return


def main() -> None:
    port = int(os.environ.get("OPENROUTER_STUB_PORT", "18500"))
    server = HTTPServer(("127.0.0.1", port), _StubHandler)
    sys.stdout.write(f"openrouter stub listening on 127.0.0.1:{port}\n")
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
