"""Idempotent bootstrap for the sales DB (Story 12.01).

Single entry point that creates every table + index used by the sales
persona stack in ``.data/semantaix_sales.db``. Runs each repo's own
``init_schema`` so the table-creation logic stays co-located with the
repository that owns the table.

The bootstrap is idempotent: every statement uses ``IF NOT EXISTS`` so
calling :func:`init_schema` twice is a no-op for both schema and data.

The default-off invariant is preserved by the schema alone — every table
starts empty, and the always-on activation gate reads
:meth:`ServicesRepository.count_active` to keep the sales answerer silent
until an operator adds the first service row.
"""

from __future__ import annotations

from services.api.app.sales.client_materials_repository import (
    init_schema as _init_client_materials_schema,
)
from services.api.app.sales.followup_queue_repository import (
    init_schema as _init_followup_queue_schema,
)
from services.api.app.sales.services_repository import (
    init_schema as _init_services_schema,
)
from services.api.app.sales.state_repository import (
    init_schema as _init_state_schema,
)


def init_schema(db_path: str) -> None:
    """Create every sales table + index in ``db_path``.

    Safe to call multiple times — every CREATE uses ``IF NOT EXISTS``.
    """
    _init_state_schema(db_path)
    _init_services_schema(db_path)
    _init_client_materials_schema(db_path)
    _init_followup_queue_schema(db_path)


__all__ = ["init_schema"]
