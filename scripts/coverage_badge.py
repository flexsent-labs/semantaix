"""Generate a repo-hosted coverage badge from pytest-cov XML output."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


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


def _measure_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont
) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _render_badge(coverage: float) -> bytes:
    pct = f"{coverage:.1f}%"
    color = _badge_color(coverage)
    left_label = "coverage"
    font = ImageFont.load_default()
    scratch = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    scratch_draw = ImageDraw.Draw(scratch)
    left_w, left_h = _measure_text(scratch_draw, left_label, font)
    right_w, right_h = _measure_text(scratch_draw, pct, font)
    left_box = max(63, left_w + 16)
    right_box = max(58, right_w + 16)
    total_width = left_box + right_box
    height = 20
    image = Image.new("RGBA", (total_width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, total_width - 1, height - 1), radius=3, fill="#555555")
    draw.rectangle((left_box, 0, total_width - 1, height - 1), fill=color)
    draw.rectangle((0, 0, total_width - 1, height - 1), outline=(255, 255, 255, 40))
    left_x = (left_box - left_w) / 2
    left_y = (height - left_h) / 2 - 1
    right_x = left_box + (right_box - right_w) / 2
    right_y = (height - right_h) / 2 - 1
    draw.text((left_x, left_y), left_label, fill="white", font=font)
    draw.text((right_x, right_y), pct, fill="white", font=font)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a coverage badge from coverage.xml.")
    parser.add_argument("--xml", required=True, type=Path)
    parser.add_argument("--badge", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args(argv)

    coverage = _coverage_percent(args.xml)
    args.badge.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.badge.write_bytes(_render_badge(coverage))
    args.summary.write_text(
        json.dumps({"coverage": round(coverage, 2), "badge": str(args.badge)}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"coverage={coverage:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
