"""Argument parsers for the Story 12.02 sales slash-commands.

Split out from ``services.bot_gateway.app.main`` so the parsing rules can be
covered in isolation:

* ``/service_add <name> [| description]`` — literal ``|`` separator (NOT the
  regex ``\\|``), split on the first pipe, both halves ``strip()``'d.
* ``/service_remove <id>`` — single positive integer.
* ``/sales_state [@customer]`` — optional ``@username`` arg, normalised so a
  bare ``bob`` becomes ``@bob``.

Validation failures raise :class:`SalesCommandUsageError` carrying the exact
one-line Russian usage string the dispatcher will DM back to the operator.
"""

from __future__ import annotations


class SalesCommandUsageError(ValueError):
    """Raised when a slash-command's argument shape is invalid.

    ``str(exc)`` is the operator-facing one-line usage message ready to DM.
    """


SERVICE_ADD_USAGE = "Использование: /service_add <название> [| описание]"
SERVICE_REMOVE_USAGE = "Использование: /service_remove <id>"


def _strip_command_prefix(text: str, command: str) -> str:
    """Return everything after ``command``, both halves stripped.

    The dispatch layer matches the regex before calling us, so ``text``
    always starts with ``command`` (after a leading ``strip()``).
    """
    stripped = text.strip()
    return stripped[len(command):].strip()


def parse_service_add(text: str) -> tuple[str, str | None]:
    """Parse ``/service_add <name> [| description]``.

    Returns ``(name, description | None)``. The first literal ``|`` splits
    the args; subsequent pipes stay inside the description verbatim.
    """
    body = _strip_command_prefix(text, "/service_add")
    if not body:
        raise SalesCommandUsageError(SERVICE_ADD_USAGE)
    name_raw, separator, description_raw = body.partition("|")
    name = name_raw.strip()
    if not name:
        raise SalesCommandUsageError(SERVICE_ADD_USAGE)
    if not separator:
        return name, None
    description = description_raw.strip()
    if not description:
        raise SalesCommandUsageError(SERVICE_ADD_USAGE)
    return name, description


def parse_service_remove(text: str) -> int:
    """Parse ``/service_remove <id>`` into a positive int."""
    body = _strip_command_prefix(text, "/service_remove")
    parts = body.split()
    if len(parts) != 1:
        raise SalesCommandUsageError(SERVICE_REMOVE_USAGE)
    try:
        service_id = int(parts[0])
    except ValueError as exc:
        raise SalesCommandUsageError(SERVICE_REMOVE_USAGE) from exc
    if service_id <= 0:
        raise SalesCommandUsageError(SERVICE_REMOVE_USAGE)
    return service_id


def parse_sales_state(text: str) -> str | None:
    """Parse ``/sales_state [@customer]``.

    Returns ``None`` when no arg is given. Otherwise returns the username
    canonicalised with a leading ``@`` so callers can pass it straight to
    the lookup function.
    """
    body = _strip_command_prefix(text, "/sales_state")
    candidate = body.strip()
    if not candidate:
        return None
    if candidate.startswith("@"):
        return candidate
    return f"@{candidate}"
