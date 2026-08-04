"""
Phase 1A - Docling document parsing.

Converts a PDF to structured Docling JSON and Markdown, then writes a page index
and a parser quality report. This script intentionally does not build the
chapter/section tree; that belongs to Phase 1B.

Usage:
    .venv/Scripts/python.exe unstructured_data/pipeline/stage1a_docling_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf
    .venv/Scripts/python.exe unstructured_data/pipeline/stage1a_docling_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf --page-range 4 20
    .venv/Scripts/python.exe unstructured_data/pipeline/stage1a_docling_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf --ocr
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption


SCANNED_CHAR_THRESHOLD = 30
WORD_JOIN_PATTERNS = [
    r"\b[A-Za-z]+Examination\b",
    r"\b[A-Za-z]+introduced\b",
    r"\b[A-Z]{2,}OF[A-Z]{2,}\b",
    r"\b[A-Za-z]+Sabha\b",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse a PDF with Docling.")
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
        "--ocr",
        action="store_true",
        help="Enable Docling OCR. Default is off for faster native-text parsing.",
    )
    parser.add_argument(
        "--force-full-page-ocr",
        action="store_true",
        help="Force OCR on every page when --ocr is enabled.",
    )
    parser.add_argument(
        "--no-table-structure",
        action="store_true",
        help="Disable Docling table structure extraction.",
    )
    return parser.parse_args()


def resolve_output_dir(pdf_path: Path, output_dir: Path | None) -> Path:
    if output_dir is not None:
        return output_dir
    project_dir = Path(__file__).resolve().parents[1]
    return project_dir / "output" / "parsed"


def make_converter(args: argparse.Namespace) -> DocumentConverter:
    options = PdfPipelineOptions()
    options.do_ocr = args.ocr
    options.do_table_structure = not args.no_table_structure
    options.heading_hierarchy_options.enabled = True
    options.heading_hierarchy_options.use_bookmarks = True
    options.heading_hierarchy_options.use_numbering = True
    options.heading_hierarchy_options.use_style = True

    if args.ocr:
        options.ocr_options = RapidOcrOptions(
            lang=["english"],
            force_full_page_ocr=args.force_full_page_ocr,
            backend="torch",
        )

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        }
    )


def get_requested_page_range(args: argparse.Namespace) -> tuple[int, int]:
    if args.page_range is None:
        return (1, sys.maxsize)
    start, end = args.page_range
    if start < 1 or end < start:
        raise ValueError("--page-range must be a 1-based inclusive range")
    return (start, end)


def page_is_scanned(page: fitz.Page) -> tuple[bool, int, bool]:
    char_count = len(page.get_text())
    has_image = len(page.get_images()) > 0
    return char_count < SCANNED_CHAR_THRESHOLD and has_image, char_count, has_image


def build_page_index(
    pdf_path: Path,
    docling_dict: dict[str, Any],
    page_range: tuple[int, int],
) -> dict[str, Any]:
    with fitz.open(pdf_path) as doc:
        start = page_range[0]
        end = min(page_range[1], doc.page_count)
        pages = []
        for page_num in range(start, end + 1):
            page = doc[page_num - 1]
            scanned, char_count, has_image = page_is_scanned(page)
            pages.append(
                {
                    "page_num": page_num,
                    "source_type": "scanned_candidate" if scanned else "native_candidate",
                    "char_count": char_count,
                    "has_image": has_image,
                    "docling_page_present": str(page_num) in docling_dict.get("pages", {}),
                }
            )

        toc = doc.get_toc(simple=True)

    scanned_pages = [
        page["page_num"] for page in pages if page["source_type"] == "scanned_candidate"
    ]
    return {
        "source_file": str(pdf_path),
        "total_pages": len(pages),
        "requested_page_range": [start, end],
        "scanned_candidate_count": len(scanned_pages),
        "scanned_candidate_pages": scanned_pages,
        "pdf_outline_entry_count": len(toc),
        "pages": pages,
    }


def collect_text_stats(docling_dict: dict[str, Any], markdown: str) -> dict[str, Any]:
    texts = docling_dict.get("texts", [])
    tables = docling_dict.get("tables", [])
    pictures = docling_dict.get("pictures", [])
    labels = {}
    for item in texts:
        label = item.get("label", "unknown")
        labels[label] = labels.get(label, 0) + 1

    matches = []
    for pattern in WORD_JOIN_PATTERNS:
        for match in re.finditer(pattern, markdown):
            value = match.group(0)
            if value not in matches:
                matches.append(value)
            if len(matches) >= 25:
                break
        if len(matches) >= 25:
            break

    return {
        "text_element_count": len(texts),
        "table_count": len(tables),
        "picture_count": len(pictures),
        "text_label_counts": labels,
        "markdown_char_count": len(markdown),
        "word_join_artifact_samples": matches,
    }


def build_quality_report(
    pdf_path: Path,
    docling_dict: dict[str, Any],
    markdown: str,
    page_index: dict[str, Any],
    conversion_status: str,
    conversion_seconds: float,
    args: argparse.Namespace,
) -> dict[str, Any]:
    requested_pages = {str(page["page_num"]) for page in page_index["pages"]}
    docling_pages = set(docling_dict.get("pages", {}).keys())
    missing_pages = sorted(int(p) for p in requested_pages - docling_pages)
    text_stats = collect_text_stats(docling_dict, markdown)

    warnings = []
    if page_index["scanned_candidate_count"] and not args.ocr:
        warnings.append("Scanned candidate pages detected, but OCR was disabled.")
    if missing_pages:
        warnings.append("Some requested pages are missing from Docling output.")
    if text_stats["word_join_artifact_samples"]:
        warnings.append("Possible word-join artifacts detected in Markdown output.")

    return {
        "source_file": str(pdf_path),
        "parser": "docling",
        "docling_status": conversion_status,
        "conversion_seconds": round(conversion_seconds, 2),
        "ocr_enabled": args.ocr,
        "force_full_page_ocr": args.force_full_page_ocr,
        "table_structure_enabled": not args.no_table_structure,
        "heading_hierarchy_enabled": True,
        "requested_page_range": page_index["requested_page_range"],
        "page_count": page_index["total_pages"],
        "docling_page_count": len(docling_pages),
        "missing_docling_pages": missing_pages,
        "scanned_candidate_pages": page_index["scanned_candidate_pages"],
        "pdf_outline_entry_count": page_index["pdf_outline_entry_count"],
        "text_stats": text_stats,
        "warnings": warnings,
        "phase1a_passed": conversion_status == "SUCCESS" and not missing_pages and not warnings,
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def output_stem(pdf_path: Path, args: argparse.Namespace) -> str:
    if args.page_range is None:
        return pdf_path.stem
    start, end = args.page_range
    return f"{pdf_path.stem}.p{start:04d}-p{end:04d}"


def main() -> None:
    args = parse_args()
    pdf_path = args.pdf_path
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    output_dir = resolve_output_dir(pdf_path, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    page_range = get_requested_page_range(args)
    converter = make_converter(args)

    print(f"Parsing with Docling: {pdf_path}")
    print(f"Page range: {page_range[0]}-{page_range[1] if page_range[1] != sys.maxsize else 'end'}")
    print(f"OCR enabled: {args.ocr}")

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
        args=args,
    )

    stem = output_stem(pdf_path, args)
    docling_json_path = output_dir / f"{stem}.docling.json"
    markdown_path = output_dir / f"{stem}.md"
    page_index_path = output_dir / f"{stem}.page_index.json"
    quality_path = output_dir / f"{stem}.quality_report.json"

    write_json(docling_json_path, docling_dict)
    markdown_path.write_text(markdown, encoding="utf-8")
    write_json(page_index_path, page_index)
    write_json(quality_path, quality_report)

    print(f"Status:              {quality_report['docling_status']}")
    print(f"Conversion seconds:  {quality_report['conversion_seconds']}")
    print(f"Docling pages:       {quality_report['docling_page_count']}")
    print(f"Text elements:       {quality_report['text_stats']['text_element_count']}")
    print(f"Tables:              {quality_report['text_stats']['table_count']}")
    print(f"Scanned candidates:  {quality_report['scanned_candidate_pages'][:20]}")
    print(f"Warnings:            {len(quality_report['warnings'])}")
    print(f"Written:             {docling_json_path}")
    print(f"Written:             {markdown_path}")
    print(f"Written:             {page_index_path}")
    print(f"Written:             {quality_path}")


if __name__ == "__main__":
    main()
