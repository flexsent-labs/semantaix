import json

import pytest

from scripts.count_loc import (
    badge_markdown,
    count_code_lines,
    count_repo,
    iter_py_files,
    main,
    update_readme,
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_count_code_lines_ignores_blanks_and_comments(tmp_path):
    source = (
        "# a comment\n"
        "import os\n"
        "\n"
        "def foo():\n"
        '    """Docstring spanning\n'
        '    two lines."""\n'
        "    x = 1  # inline comment\n"
        "    return x\n"
        "\n"
        "\n"
    )
    path = _write(tmp_path / "sample.py", source)
    # import os, def foo, two docstring lines, x = 1, return x -> 6
    assert count_code_lines(path) == 6


def test_count_code_lines_all_blank_or_comment_is_zero(tmp_path):
    path = _write(tmp_path / "empty.py", "# just a comment\n\n# another\n")
    assert count_code_lines(path) == 0


def test_count_code_lines_falls_back_on_tokenize_error(tmp_path):
    # An unterminated triple-quoted string makes tokenize raise; the fallback
    # counts non-blank, non-comment physical lines instead (here: 2).
    path = _write(tmp_path / "broken.py", "x = 1\ny = '''unterminated\n")
    assert count_code_lines(path) == 2


def test_iter_py_files_skips_pycache(tmp_path):
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "real.cpython-311.py").write_text("x = 1\n", encoding="utf-8")
    assert list(iter_py_files([tmp_path])) == [tmp_path / "real.py"]


def test_iter_py_files_skips_missing_root(tmp_path):
    assert list(iter_py_files([tmp_path / "does-not-exist"])) == []


def test_badge_markdown_embeds_count():
    badge = badge_markdown(1234)
    assert "img.shields.io/badge/app%20source-1234%20lines-blue" in badge


def test_update_readme_replaces_and_is_idempotent(tmp_path):
    readme = _write(
        tmp_path / "README.md",
        "# Title\n\n<!-- LOC:START -->old<!-- LOC:END -->\n\nbody\n",
    )
    assert update_readme(readme, 42) is True
    text = readme.read_text(encoding="utf-8")
    assert badge_markdown(42) in text
    assert text.startswith("# Title")
    assert text.endswith("body\n")
    # Re-running with the same count must not rewrite the file.
    assert update_readme(readme, 42) is False


def test_update_readme_requires_markers(tmp_path):
    readme = _write(tmp_path / "README.md", "# Title\n\nno markers here\n")
    with pytest.raises(SystemExit):
        update_readme(readme, 7)


def test_count_repo_smoke():
    loc, files = count_repo()
    assert loc > 0
    assert files > 0


def test_main_prints_integer(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out.strip()
    assert out.isdigit()
    assert int(out) > 0


def test_main_json(capsys):
    assert main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["loc"] > 0
    assert payload["files"] > 0


def test_main_update_readme(tmp_path, capsys):
    readme = _write(
        tmp_path / "README.md",
        "# Title\n<!-- LOC:START -->x<!-- LOC:END -->\n",
    )
    assert main(["--update-readme", str(readme)]) == 0
    assert capsys.readouterr().out.strip() == "changed"
    assert main(["--update-readme", str(readme)]) == 0
    assert capsys.readouterr().out.strip() == "unchanged"
