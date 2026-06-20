from pathlib import Path

from scripts.coverage_badge import _coverage_percent, main


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_coverage_percent_uses_line_rate(tmp_path):
    xml = _write(
        tmp_path / "coverage.xml",
        '<coverage line-rate="0.875" branch-rate="0.0"></coverage>',
    )
    assert _coverage_percent(xml) == 87.5


def test_coverage_percent_falls_back_to_counters(tmp_path):
    xml = _write(
        tmp_path / "coverage.xml",
        """<coverage>
  <packages>
    <package>
      <classes>
        <class>
          <counter type="LINE" missed="2" covered="6" />
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""",
    )
    assert _coverage_percent(xml) == 75.0


def test_main_writes_badge_and_summary(tmp_path, capsys):
    xml = _write(
        tmp_path / "coverage.xml",
        '<coverage line-rate="1.0" branch-rate="0.0"></coverage>',
    )
    badge = tmp_path / ".badges" / "coverage.svg"
    summary = tmp_path / ".badges" / "coverage-summary.json"

    assert main(
        [
            "--xml",
            str(xml),
            "--badge",
            str(badge),
            "--summary",
            str(summary),
        ]
    ) == 0
    assert "coverage=100.00%" in capsys.readouterr().out
    assert badge.exists()
    assert summary.exists()
    assert "100.0" in summary.read_text(encoding="utf-8")
    assert "100.0%" in badge.read_text(encoding="utf-8")
