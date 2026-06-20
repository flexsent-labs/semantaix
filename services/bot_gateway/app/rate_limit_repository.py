"""Story 12.103 — per-customer inbound rate limiting.

The implementation now lives in :mod:`platform_common.inbound_rate_limit` so the
``user_gateway`` MTProto channel can apply the same policy. This module re-exports
it to preserve the existing ``bot_gateway`` import path.
"""

from __future__ import annotations

from platform_common.inbound_rate_limit import InboundRateLimitRepository

__all__ = ["InboundRateLimitRepository"]
