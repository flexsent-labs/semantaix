from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeMismatch:
    expected_major: int
    expected_minor: int
    actual_major: int
    actual_minor: int
    executable: str


def validate_test_runtime(*, version_info, executable: str) -> None:
    """Fail fast when tests run under an unsupported Python runtime."""
    expected_major = 3
    expected_minor = 11
    actual_major = version_info.major
    actual_minor = version_info.minor
    if (actual_major, actual_minor) == (expected_major, expected_minor):
        return

    mismatch = RuntimeMismatch(
        expected_major=expected_major,
        expected_minor=expected_minor,
        actual_major=actual_major,
        actual_minor=actual_minor,
        executable=executable,
    )
    raise RuntimeError(
        "Semantaix tests require Python "
        f"{mismatch.expected_major}.{mismatch.expected_minor}; "
        f"got {mismatch.actual_major}.{mismatch.actual_minor} at {mismatch.executable}. "
        "Use `.venv/bin/pytest` or create the venv with `python3.11 -m venv .venv`."
    )
