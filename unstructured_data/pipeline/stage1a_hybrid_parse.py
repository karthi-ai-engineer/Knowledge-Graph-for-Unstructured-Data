"""
Phase 1A - Hybrid Docling document parsing.

Preflights the PDF with PyMuPDF, sends only image-dominant low-text pages
through OCR, and sends the remaining pages through native Docling parsing.

This script writes per-range Docling artifacts plus hybrid manifest/report files.
It intentionally does not merge Docling JSON refs into one synthetic document;
later stages should read the manifest and process range artifacts in page order.

Usage:
    .venv/Scripts/python.exe unstructured_data/pipeline/stage1a_hybrid_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf --plan-only
    .venv/Scripts/python.exe unstructured_data/pipeline/stage1a_hybrid_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf
    .venv/Scripts/python.exe unstructured_data/pipeline/stage1a_hybrid_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf --page-range 1 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fitz  # PyMuPDF

from stage1a_docling_parse import (
    build_page_index,
    build_quality_report,
    collect_text_stats,
    make_converter,
    resolve_output_dir,
    write_json,
)


DEFAULT_LOW_TEXT_THRESHOLD = 30
DEFAULT_LOW_IMAGE_COVERAGE_THRESHOLD = 0.03
DEFAULT_MIXED_TEXT_THRESHOLD = 150
DEFAULT_IMAGE_COVERAGE_THRESHOLD = 0.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse a PDF with hybrid OCR.")
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument(
        "output_dir",
        nargs="?",
        type=Path,
        help="Defaults to unstructured_data/output/parsed",
    )
    parser.add_argument(
        "--page-range",
        nargs=2,
        metavar=("START", "END"),
        type=int,
        help="1-based inclusive page range for development/testing.",
    )
    parser.add_argument(
        "--low-text-threshold",
        type=int,
        default=DEFAULT_LOW_TEXT_THRESHOLD,
        help="OCR pages with fewer native characters than this and at least one image.",
    )
    parser.add_argument(
        "--low-image-coverage-threshold",
        type=float,
        default=DEFAULT_LOW_IMAGE_COVERAGE_THRESHOLD,
        help="Minimum image coverage for low-text pages that still have some native text.",
    )
    parser.add_argument(
        "--mixed-text-threshold",
        type=int,
        default=DEFAULT_MIXED_TEXT_THRESHOLD,
        help="OCR image-dominant pages with fewer native characters than this.",
    )
    parser.add_argument(
        "--image-coverage-threshold",
        type=float,
        default=DEFAULT_IMAGE_COVERAGE_THRESHOLD,
        help="Minimum approximate image coverage for mixed low-text OCR pages.",
    )
    parser.add_argument(
        "--force-full-page-ocr",
        action="store_true",
        help="Force OCR on every OCR-selected page region.",
    )
    parser.add_argument(
        "--no-table-structure",
        action="store_true",
        help="Disable Docling table structure extraction.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only write the hybrid plan; do not run Docling conversions.",
    )
    return parser.parse_args()


def get_requested_page_range(args: argparse.Namespace, page_count: int) -> tuple[int, int]:
    if args.page_range is None:
        return (1, page_count)
    start, end = args.page_range
    if start < 1 or end < start:
        raise ValueError("--page-range must be a 1-based inclusive range")
    return (start, min(end, page_count))


def image_coverage(page: fitz.Page) -> float:
    page_area = max(1.0, float(page.rect.width * page.rect.height))
    try:
        image_infos = page.get_image_info()
    except Exception:
        return 0.0

    image_area = 0.0
    for image_info in image_infos:
        bbox = image_info.get("bbox")
        if not bbox:
            continue
        rect = fitz.Rect(bbox) & page.rect
        if not rect.is_empty:
            image_area += float(rect.width * rect.height)
    return round(min(1.0, image_area / page_area), 4)


def classify_page(page: fitz.Page, args: argparse.Namespace) -> dict[str, Any]:
    text = page.get_text()
    char_count = len(text.strip())
    image_count = len(page.get_images())
    has_image = image_count > 0
    coverage = image_coverage(page) if has_image else 0.0

    if char_count == 0 and has_image:
        mode = "ocr"
        reason = "no_native_text_with_image"
    elif (
        char_count < args.low_text_threshold
        and has_image
        and coverage >= args.low_image_coverage_threshold
    ):
        mode = "ocr"
        reason = "low_text_with_image"
    elif (
        char_count < args.mixed_text_threshold
        and has_image
        and coverage >= args.image_coverage_threshold
    ):
        mode = "ocr"
        reason = "image_dominant_low_text"
    else:
        mode = "native"
        reason = "native_text_available"

    return {
        "page_num": page.number + 1,
        "mode": mode,
        "reason": reason,
        "char_count": char_count,
        "image_count": image_count,
        "has_image": has_image,
        "image_coverage": coverage,
    }


def build_hybrid_plan(pdf_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        start, end = get_requested_page_range(args, page_count)
        toc_count = len(doc.get_toc(simple=True))
        pages = [classify_page(doc[page_num - 1], args) for page_num in range(start, end + 1)]

    ranges = pages_to_ranges(pages)
    return {
        "source_file": str(pdf_path),
        "pdf_page_count": page_count,
        "requested_page_range": [start, end],
        "pdf_outline_entry_count": toc_count,
        "strategy": {
            "low_text_threshold": args.low_text_threshold,
            "low_image_coverage_threshold": args.low_image_coverage_threshold,
            "mixed_text_threshold": args.mixed_text_threshold,
            "image_coverage_threshold": args.image_coverage_threshold,
            "force_full_page_ocr": args.force_full_page_ocr,
            "table_structure_enabled": not args.no_table_structure,
        },
        "page_count": len(pages),
        "native_page_count": sum(1 for page in pages if page["mode"] == "native"),
        "ocr_page_count": sum(1 for page in pages if page["mode"] == "ocr"),
        "ocr_pages": [page["page_num"] for page in pages if page["mode"] == "ocr"],
        "native_pages": [page["page_num"] for page in pages if page["mode"] == "native"],
        "ranges": ranges,
        "pages": pages,
    }


def pages_to_ranges(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not pages:
        return []
    ranges = []
    current = {
        "mode": pages[0]["mode"],
        "start_page": pages[0]["page_num"],
        "end_page": pages[0]["page_num"],
        "page_count": 1,
    }
    for page in pages[1:]:
        if page["mode"] == current["mode"] and page["page_num"] == current["end_page"] + 1:
            current["end_page"] = page["page_num"]
            current["page_count"] += 1
        else:
            ranges.append(current)
            current = {
                "mode": page["mode"],
                "start_page": page["page_num"],
                "end_page": page["page_num"],
                "page_count": 1,
            }
    ranges.append(current)
    return ranges


def converter_args(args: argparse.Namespace, ocr_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        ocr=ocr_enabled,
        force_full_page_ocr=args.force_full_page_ocr,
        no_table_structure=args.no_table_structure,
    )


def range_stem(pdf_path: Path, item: dict[str, Any]) -> str:
    return (
        f"{pdf_path.stem}.hybrid.{item['mode']}."
        f"p{item['start_page']:04d}-p{item['end_page']:04d}"
    )


def convert_range(
    pdf_path: Path,
    output_dir: Path,
    item: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    ocr_enabled = item["mode"] == "ocr"
    range_args = converter_args(args, ocr_enabled=ocr_enabled)
    page_range = (item["start_page"], item["end_page"])
    converter = make_converter(range_args)

    print(
        f"Parsing {item['mode']}: pages {item['start_page']}-{item['end_page']} "
        f"({item['page_count']} pages)"
    )
    started_at = time.perf_counter()
    result = converter.convert(pdf_path, page_range=page_range)
    conversion_seconds = time.perf_counter() - started_at

    doc = result.document
    docling_dict = doc.export_to_dict()
    markdown = doc.export_to_markdown()
    page_index = build_page_index(pdf_path, docling_dict, page_range)
    quality_report = build_quality_report(
        pdf_path=pdf_path,
        docling_dict=docling_dict,
        markdown=markdown,
        page_index=page_index,
        conversion_status=getattr(result.status, "name", str(result.status)),
        conversion_seconds=conversion_seconds,
        args=range_args,
    )

    stem = range_stem(pdf_path, item)
    docling_json_path = output_dir / f"{stem}.docling.json"
    markdown_path = output_dir / f"{stem}.md"
    page_index_path = output_dir / f"{stem}.page_index.json"
    quality_path = output_dir / f"{stem}.quality_report.json"

    write_json(docling_json_path, docling_dict)
    markdown_path.write_text(markdown, encoding="utf-8")
    write_json(page_index_path, page_index)
    write_json(quality_path, quality_report)

    return {
        "mode": item["mode"],
        "page_range": [item["start_page"], item["end_page"]],
        "page_count": item["page_count"],
        "ocr_enabled": ocr_enabled,
        "docling_status": quality_report["docling_status"],
        "phase1a_passed": quality_report["phase1a_passed"],
        "warnings": quality_report["warnings"],
        "conversion_seconds": quality_report["conversion_seconds"],
        "text_element_count": quality_report["text_stats"]["text_element_count"],
        "table_count": quality_report["text_stats"]["table_count"],
        "docling_json": str(docling_json_path),
        "markdown": str(markdown_path),
        "page_index": str(page_index_path),
        "quality_report": str(quality_path),
    }


def build_hybrid_quality_report(
    plan: dict[str, Any],
    conversions: list[dict[str, Any]],
    plan_only: bool,
) -> dict[str, Any]:
    warnings = []
    failed = [item for item in conversions if not item["phase1a_passed"]]
    if failed:
        warnings.append("One or more hybrid range conversions did not pass Phase 1A checks.")
    if plan_only:
        warnings.append("Plan-only mode did not run Docling conversions.")

    return {
        "source_file": plan["source_file"],
        "parser": "docling_hybrid",
        "requested_page_range": plan["requested_page_range"],
        "page_count": plan["page_count"],
        "native_page_count": plan["native_page_count"],
        "ocr_page_count": plan["ocr_page_count"],
        "ocr_pages": plan["ocr_pages"],
        "range_count": len(plan["ranges"]),
        "conversions": conversions,
        "total_conversion_seconds": round(
            sum(item["conversion_seconds"] for item in conversions), 2
        ),
        "text_stats": {
            "text_element_count": sum(item["text_element_count"] for item in conversions),
            "table_count": sum(item["table_count"] for item in conversions),
        },
        "warnings": warnings,
        "phase1a_hybrid_passed": not warnings,
    }


def write_hybrid_outputs(
    output_dir: Path,
    pdf_path: Path,
    plan: dict[str, Any],
    conversions: list[dict[str, Any]],
    plan_only: bool,
) -> None:
    stem = f"{pdf_path.stem}.hybrid"
    manifest = dict(plan)
    manifest["conversions"] = conversions
    manifest["plan_only"] = plan_only

    write_json(output_dir / f"{stem}.manifest.json", manifest)
    write_json(
        output_dir / f"{stem}.quality_report.json",
        build_hybrid_quality_report(plan, conversions, plan_only=plan_only),
    )

    if not plan_only:
        markdown_parts = []
        for conversion in conversions:
            markdown_parts.append(Path(conversion["markdown"]).read_text(encoding="utf-8"))
        (output_dir / f"{stem}.md").write_text(
            "\n\n".join(markdown_parts),
            encoding="utf-8",
        )


def main() -> None:
    args = parse_args()
    pdf_path = args.pdf_path
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    output_dir = resolve_output_dir(pdf_path, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    plan = build_hybrid_plan(pdf_path, args)

    print(f"Hybrid preflight: {pdf_path}")
    print(f"Page range:       {plan['requested_page_range'][0]}-{plan['requested_page_range'][1]}")
    print(f"Native pages:     {plan['native_page_count']}")
    print(f"OCR pages:        {plan['ocr_page_count']} {plan['ocr_pages'][:25]}")
    print(f"Ranges:           {plan['ranges']}")

    conversions: list[dict[str, Any]] = []
    if not args.plan_only:
        for item in plan["ranges"]:
            conversions.append(convert_range(pdf_path, output_dir, item, args))

    write_hybrid_outputs(
        output_dir=output_dir,
        pdf_path=pdf_path,
        plan=plan,
        conversions=conversions,
        plan_only=args.plan_only,
    )

    stem = f"{pdf_path.stem}.hybrid"
    print(f"Written:          {output_dir / f'{stem}.manifest.json'}")
    print(f"Written:          {output_dir / f'{stem}.quality_report.json'}")
    if not args.plan_only:
        print(f"Written:          {output_dir / f'{stem}.md'}")


if __name__ == "__main__":
    main()
