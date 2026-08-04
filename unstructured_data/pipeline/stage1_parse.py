"""
Stage 1 - Document Parsing.

Reads a PDF, classifies each page as native-text or scanned, extracts body
text while filtering out headings and margin noise (footers/footnote markers)
into separate fields, and writes one structured JSON file per document.

Heading/noise detection is font-size based:
  - "body size" = the font size with the most total characters across the doc
  - a block whose largest span is >= body_size * HEADING_RATIO and has at
    least MIN_HEADING_CHARS characters -> heading (level = rank by size)
  - a block whose largest span is <= body_size * NOISE_RATIO -> margin noise
    (running footers, footnote markers), excluded from body text
  - everything else -> body text

Usage:
    .venv/Scripts/python.exe unstructured_data/pipeline/stage1_parse.py <pdf_path> [output_dir]
"""

import json
import sys
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

HEADING_RATIO = 1.15
NOISE_RATIO = 0.85
MIN_HEADING_CHARS = 4
SCANNED_CHAR_THRESHOLD = 30


def extract_page_blocks(page):
    """Return list of (bbox, text, max_span_size) for text blocks on a page."""
    blocks = []
    raw = page.get_text("dict")
    for block in raw["blocks"]:
        if block["type"] != 0:
            continue
        text = "".join(
            span["text"] for line in block["lines"] for span in line["spans"]
        )
        if not text.strip():
            continue
        max_size = max(
            span["size"] for line in block["lines"] for span in line["spans"]
        )
        blocks.append((block["bbox"], text, round(max_size, 1)))
    return blocks


def extract_page_tables(page):
    """Detect tables on a page and extract their rows via PyMuPDF's table finder."""
    tables = []
    try:
        finder = page.find_tables()
    except Exception:
        return tables
    for table in finder.tables:
        try:
            rows = table.extract()
        except Exception:
            rows = []
        tables.append({"bbox": list(table.bbox), "rows": rows})
    return tables


def _overlap_ratio(block_bbox, table_bbox):
    """Fraction of block_bbox's area that falls inside table_bbox."""
    bx0, by0, bx1, by1 = block_bbox
    tx0, ty0, tx1, ty1 = table_bbox
    ix0, iy0 = max(bx0, tx0), max(by0, ty0)
    ix1, iy1 = min(bx1, tx1), min(by1, ty1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter_area = iw * ih
    block_area = max(1e-6, (bx1 - bx0) * (by1 - by0))
    return inter_area / block_area


def detect_body_size(all_pages_blocks):
    """Body size = font size with the most total characters across the doc."""
    size_char_counts = Counter()
    for blocks in all_pages_blocks:
        for _, text, size in blocks:
            size_char_counts[size] += len(text)
    if not size_char_counts:
        return None
    return size_char_counts.most_common(1)[0][0]


def rank_heading_levels(all_pages_blocks, body_size):
    """Map each distinct heading font size to a level (1 = largest)."""
    heading_sizes = set()
    for blocks in all_pages_blocks:
        for _, text, size in blocks:
            if size >= body_size * HEADING_RATIO and len(text.strip()) >= MIN_HEADING_CHARS:
                heading_sizes.add(size)
    ordered = sorted(heading_sizes, reverse=True)
    return {size: level + 1 for level, size in enumerate(ordered)}


def classify_page(page, blocks, body_size, level_by_size, tables):
    raw_text_len = len(page.get_text())
    images = page.get_images()
    has_image = len(images) > 0

    # Low text + an embedded image -> a photographed/scanned page needing OCR.
    # Low text + no image -> just a sparse native page (e.g. a near-empty
    # table row or chapter divider), not a scan.
    if raw_text_len < SCANNED_CHAR_THRESHOLD and has_image:
        return {
            "source_type": "scanned",
            "char_count": raw_text_len,
            "has_image": has_image,
            "body_text": "",
            "headings": [],
            "margin_noise": [],
            "tables": [],
        }

    table_bboxes = [t["bbox"] for t in tables]

    body_parts = []
    headings = []
    margin_noise = []

    for bbox, text, size in blocks:
        stripped = text.strip()
        if not stripped:
            continue
        # Skip blocks that are mostly inside a detected table -- that text is
        # already captured structurally in `tables`, don't duplicate it as
        # loose fragments in body_text/headings.
        if any(_overlap_ratio(bbox, tb) > 0.5 for tb in table_bboxes):
            continue
        if size >= body_size * HEADING_RATIO and len(stripped) >= MIN_HEADING_CHARS:
            headings.append(
                {"text": stripped, "size": size, "level": level_by_size.get(size, 1)}
            )
        elif size <= body_size * NOISE_RATIO:
            margin_noise.append(stripped)
        else:
            body_parts.append(stripped)

    return {
        "source_type": "native",
        "char_count": raw_text_len,
        "has_image": has_image,
        "body_text": "\n".join(body_parts),
        "headings": headings,
        "margin_noise": margin_noise,
        "tables": tables,
    }


def parse_pdf(pdf_path: Path):
    doc = fitz.open(pdf_path)

    all_pages_blocks = [extract_page_blocks(doc[i]) for i in range(doc.page_count)]
    body_size = detect_body_size(all_pages_blocks)
    level_by_size = rank_heading_levels(all_pages_blocks, body_size)

    pages = []
    for i in range(doc.page_count):
        tables = extract_page_tables(doc[i])
        page_data = classify_page(doc[i], all_pages_blocks[i], body_size, level_by_size, tables)
        page_data["page_num"] = i + 1
        pages.append(page_data)

    scanned_pages = [p["page_num"] for p in pages if p["source_type"] == "scanned"]
    table_page_count = sum(1 for p in pages if p["tables"])
    total_table_count = sum(len(p["tables"]) for p in pages)

    result = {
        "source_file": str(pdf_path),
        "total_pages": doc.page_count,
        "body_font_size": body_size,
        "heading_levels": {str(size): lvl for size, lvl in level_by_size.items()},
        "native_page_count": doc.page_count - len(scanned_pages),
        "scanned_page_count": len(scanned_pages),
        "scanned_pages": scanned_pages,
        "table_page_count": table_page_count,
        "total_table_count": total_table_count,
        "pages": pages,
    }
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: stage1_parse.py <pdf_path> [output_dir]")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    project_dir = Path(__file__).resolve().parents[1]
    output_dir = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else project_dir / "output" / "parsed"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Parsing {pdf_path} ...")
    result = parse_pdf(pdf_path)

    out_path = output_dir / f"{pdf_path.stem}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Total pages:        {result['total_pages']}")
    print(f"Native pages:       {result['native_page_count']}")
    print(f"Scanned pages:      {result['scanned_page_count']} {result['scanned_pages'][:20]}")
    print(f"Detected body size: {result['body_font_size']}")
    print(f"Heading levels:     {result['heading_levels']}")
    print(f"Pages with tables:  {result['table_page_count']} ({result['total_table_count']} tables total)")
    print(f"Written to:         {out_path}")


if __name__ == "__main__":
    main()
