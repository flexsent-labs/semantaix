"""Generate a repo-hosted coverage badge from pytest-cov XML output."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
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
        return "#4c1"
    if coverage >= 75.0:
        return "#fe7d37"
    return "#e05d44"


@dataclass(frozen=True)
class BadgeMetrics:
    left_width: int
    right_width: int
    total_width: int


def _metrics(coverage: float) -> BadgeMetrics:
    pct = f"{coverage:.1f}%"
    left_width = 63
    right_width = max(58, 10 + len(pct) * 8)
    return BadgeMetrics(
        left_width=left_width,
        right_width=right_width,
        total_width=left_width + right_width,
    )


def _render_badge(coverage: float) -> str:
    pct = f"{coverage:.1f}%"
    color = _badge_color(coverage)
    metrics = _metrics(coverage)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{metrics.total_width}" height="20" '
        f'role="img" aria-label="coverage {pct}">\n'
        f'  <title>coverage {pct}</title>\n'
        '  <linearGradient id="s" x2="0" y2="100%">\n'
        '    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>\n'
        '    <stop offset="1" stop-opacity=".1"/>\n'
        '  </linearGradient>\n'
        '  <clipPath id="r">\n'
        f'    <rect width="{metrics.total_width}" height="20" rx="3" fill="#fff"/>\n'
        '  </clipPath>\n'
        '  <g clip-path="url(#r)">\n'
        f'    <rect width="{metrics.left_width}" height="20" fill="#555"/>\n'
        f'    <rect x="{metrics.left_width}" width="{metrics.right_width}" '
        f'height="20" fill="{color}"/>\n'
        f'    <rect width="{metrics.total_width}" height="20" fill="url(#s)"/>\n'
        '  </g>\n'
        '  <g fill="#fff" text-anchor="middle" '
        'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">\n'
        f'    <text x="{metrics.left_width / 2}" y="14">coverage</text>\n'
        f'    <text x="{metrics.left_width + (metrics.right_width / 2)}" y="14">{pct}</text>\n'
        '</svg>\n'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a coverage badge from coverage.xml.")
    parser.add_argument("--xml", required=True, type=Path)
    parser.add_argument("--badge", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args(argv)

    coverage = _coverage_percent(args.xml)
    args.badge.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.badge.write_text(_render_badge(coverage), encoding="utf-8")
    args.summary.write_text(
        json.dumps({"coverage": round(coverage, 2), "badge": str(args.badge)}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"coverage={coverage:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
