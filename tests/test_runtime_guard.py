from types import SimpleNamespace

import pytest

from tests.runtime_guard import validate_test_runtime


def test_validate_test_runtime_accepts_python_311():
    validate_test_runtime(
        version_info=SimpleNamespace(major=3, minor=11),
        executable="/Users/aj/workspace_ai/semaintix/.venv/bin/python",
    )


def test_validate_test_runtime_rejects_other_python_versions():
    with pytest.raises(RuntimeError, match=r"Python 3\.11"):
        validate_test_runtime(
            version_info=SimpleNamespace(major=3, minor=14),
            executable="/usr/local/bin/python3",
        )
