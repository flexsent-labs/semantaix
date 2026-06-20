import json
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


def test_coverage_color_thresholds():
    from scripts.coverage_badge import _badge_color

    assert _badge_color(90.0) == "brightgreen"
    assert _badge_color(75.0) == "orange"
    assert _badge_color(74.9) == "red"


def test_main_writes_badge_and_summary(tmp_path, capsys):
    xml = _write(
        tmp_path / "coverage.xml",
        '<coverage line-rate="1.0" branch-rate="0.0"></coverage>',
    )
    summary = tmp_path / ".badges" / "coverage-summary.json"

    assert main(
        [
            "--xml",
            str(xml),
            "--summary",
            str(summary),
        ]
    ) == 0
    assert "coverage=100.00%" in capsys.readouterr().out
    assert summary.exists()
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload == {
        "schemaVersion": 1,
        "label": "coverage",
        "message": "100.0%",
        "color": "brightgreen",
    }
