import os
import sys
from pathlib import Path

import pytest

# Tests run with a single inbound-forward attempt — no real backoff sleeps.
# Set before any service module constructs its Settings singleton. Production
# uses the `pending_forward_retry_delays_seconds` default (2,5,10); the retry
# tests opt back in by monkeypatching the setting explicitly.
os.environ["PENDING_FORWARD_RETRY_DELAYS_SECONDS"] = ""

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_webhook_update_claims(tmp_path, monkeypatch):
    """Story 12.31 — give every test its own webhook-entry dedup store.

    The webhook claims each Telegram ``update_id`` via the module-level
    ``webhook_update_claim_repository`` singleton (bound to its DB path at
    import). The operator-command test helpers reuse fixed ``update_id`` values
    (mostly ``1``) as throwaway placeholders, so without per-test isolation the
    claim would dedup across unrelated tests — and writing to the default
    ``.data/`` file would make the suite non-hermetic across runs. Point the
    singleton at a fresh per-test SQLite file (mirrors how the api tests reset
    ``answer_trace_repository.db_path``). No-op when the bot_gateway is not
    imported (api / platform_common-only test runs)."""
    module = sys.modules.get("services.bot_gateway.app.main")
    if module is not None:
        monkeypatch.setattr(
            module.webhook_update_claim_repository,
            "db_path",
            str(tmp_path / "webhook_dedup.sqlite3"),
        )
    yield
