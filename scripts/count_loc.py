"""Count application-source lines of code and stamp the number into the README.

Scope is the application code only — Python under ``services/`` and
``platform_common/``. Top-level ``tests/``, ``data/``, ``docs/`` and the
``_bmad*`` artifacts are excluded simply by not being under those roots.

A line counts as code when it carries at least one real token (anything that is
not a comment, newline, or indentation marker). Blank lines and comment-only
lines are ignored; docstrings and multi-line strings count as code, matching
the conventional "code lines" definition used by tools like tokei.

Usage::

    python scripts/count_loc.py                     # print the integer
    python scripts/count_loc.py --json              # {"loc": N, "files": M}
    python scripts/count_loc.py --update-readme README.md
"""

from __future__ import annotations

import argparse
import json
import re
import tokenize
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOTS: tuple[str, ...] = ("services", "platform_common")

LOC_START = "<!-- LOC:START -->"
LOC_END = "<!-- LOC:END -->"
_MARKER_RE = re.compile(re.escape(LOC_START) + r".*?" + re.escape(LOC_END), re.DOTALL)

_SKIP_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)


def iter_py_files(roots: Iterable[Path]) -> Iterator[Path]:
    """Yield every ``*.py`` file under the given roots, skipping caches."""
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _fallback_count(path: Path) -> int:
    """Count non-blank, non-comment lines without tokenizing (last resort)."""
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def count_code_lines(path: Path) -> int:
    """Return the number of physical lines in ``path`` that carry code."""
    real_lines: set[int] = set()
    try:
        with path.open("rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if token.type in _SKIP_TOKENS:
                    continue
                for row in range(token.start[0], token.end[0] + 1):
                    real_lines.add(row)
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return _fallback_count(path)
    return len(real_lines)


def count_repo(roots: Iterable[str] = ROOTS) -> tuple[int, int]:
    """Return ``(total_code_lines, file_count)`` across the configured roots."""
    total = 0
    files = 0
    for path in iter_py_files(REPO_ROOT / root for root in roots):
        total += count_code_lines(path)
        files += 1
    return total, files


def badge_markdown(loc: int) -> str:
    """Render the shields.io badge whose value we commit ourselves."""
    return f"![App source](https://img.shields.io/badge/app%20source-{loc}%20lines-blue)"


def update_readme(path: Path, loc: int) -> bool:
    """Rewrite the LOC marker block in ``path``; return True if it changed."""
    text = path.read_text(encoding="utf-8")
    replacement = f"{LOC_START}{badge_markdown(loc)}{LOC_END}"
    new_text, hits = _MARKER_RE.subn(lambda _match: replacement, text)
    if hits == 0:
        raise SystemExit(
            f"LOC markers not found in {path}; expected {LOC_START} ... {LOC_END}"
        )
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Count app-source lines of code.")
    parser.add_argument("--json", action="store_true", help="emit JSON: loc + file count")
    parser.add_argument(
        "--update-readme",
        metavar="PATH",
        help="rewrite the LOC marker block in the given README in place",
    )
    args = parser.parse_args(argv)

    loc, files = count_repo()

    if args.update_readme:
        changed = update_readme(Path(args.update_readme), loc)
        print("changed" if changed else "unchanged")
    elif args.json:
        print(json.dumps({"loc": loc, "files": files}))
    else:
        print(loc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
