import os
import sys
from pathlib import Path

# Tests run with a single inbound-forward attempt — no real backoff sleeps.
# Set before any service module constructs its Settings singleton. Production
# uses the `pending_forward_retry_delays_seconds` default (2,5,10); the retry
# tests opt back in by monkeypatching the setting explicitly.
os.environ["PENDING_FORWARD_RETRY_DELAYS_SECONDS"] = ""

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
