"""Generate a repo-hosted coverage badge from pytest-cov XML output."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def _coverage_percent(xml_path: Path) -> float:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    line_rate = root.attrib.get("line-rate")
    if line_rate is not None:
        return float(line_rate) * 100.0

    covered = 0
    missed = 0
    for counter in root.findall(".//counter"):
        if counter.attrib.get("type") != "LINE":
            continue
        covered += int(counter.attrib["covered"])
        missed += int(counter.attrib["missed"])

    total = covered + missed
    if total == 0:
        return 0.0
    return (covered / total) * 100.0


def _badge_color(coverage: float) -> str:
    if coverage >= 90.0:
        return "brightgreen"
    if coverage >= 75.0:
        return "orange"
    return "red"


def _render_badge(coverage: float) -> dict[str, object]:
    pct = f"{coverage:.1f}%"
    return {
        "schemaVersion": 1,
        "label": "coverage",
        "message": pct,
        "color": _badge_color(coverage),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a coverage badge from coverage.xml.")
    parser.add_argument("--xml", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args(argv)

    coverage = _coverage_percent(args.xml)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(_render_badge(coverage), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"coverage={coverage:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
