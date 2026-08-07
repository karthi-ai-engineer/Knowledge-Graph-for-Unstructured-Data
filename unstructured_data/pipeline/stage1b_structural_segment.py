"""
Phase 1B - Structural segmentation.

Builds a section tree from the PDF outline and attaches every Docling-parsed
element to the nearest section. Phase 1A remains the parser source of truth;
this stage adds document hierarchy and stable element IDs for chunking.

Usage:
    .venv/Scripts/python.exe unstructured_data/pipeline/stage1b_structural_segment.py unstructured_data/Docs/LAKSHMIKANT.pdf unstructured_data/output/parsed/LAKSHMIKANT.p0044-p0049.docling.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF


PART_RE = re.compile(r"^PART\s+([IVXLCDM]+)\b", re.IGNORECASE)
CHAPTER_RE = re.compile(r"^(\d+)\.\s+(.+)")
APPENDIX_RE = re.compile(r"^Appendix\s+([IVXLCDM]+)\s*:\s*(.+)", re.IGNORECASE)
NON_ID_CHARS_RE = re.compile(r"[^a-z0-9]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build sections.json and elements.jsonl from Docling output."
    )
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("input_dir", type=Path, help="Directory containing Phase 1A outputs")
    parser.add_argument(
        "output_dir",
        nargs="?",
        type=Path,
        help="Defaults to the input directory.",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    data = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))
    return data


def write_json(path: Path, data: dict[str, Any] | list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def output_stem(docling_json_path: Path) -> str:
    suffix = ".docling.json"
    name = docling_json_path.name
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return docling_json_path.stem


def slugify(value: str, max_len: int = 48) -> str:
    slug = NON_ID_CHARS_RE.sub("_", value.lower()).strip("_")
    return slug[:max_len].strip("_") or "untitled"


def roman_to_int(value: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for char in reversed(value.upper()):
        current = values.get(char)
        if current is None:
            return None
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total


def infer_outline_kind_and_level(title: str, seen_main_part: bool) -> tuple[str, int]:
    if PART_RE.match(title):
        return "part", 1
    if title.strip().lower() == "appendices":
        return "appendices", 1
    if APPENDIX_RE.match(title):
        return "appendix", 2
    if CHAPTER_RE.match(title):
        return "chapter", 2
    if seen_main_part:
        return "section", 2
    return "front_matter", 1


def make_section_id(title: str, kind: str, occurrence: int) -> str:
    if kind == "part":
        match = PART_RE.match(title)
        if match:
            number = roman_to_int(match.group(1))
            if number is not None:
                return f"part_{number:02d}_{slugify(title)}"
    if kind == "chapter":
        match = CHAPTER_RE.match(title)
        if match:
            return f"ch_{int(match.group(1)):03d}_{slugify(match.group(2))}"
    if kind == "appendix":
        match = APPENDIX_RE.match(title)
        if match:
            number = roman_to_int(match.group(1))
            if number is not None:
                return f"app_{number:02d}_{slugify(match.group(2))}"
    if kind == "appendices":
        return "appendices"
    return f"{kind}_{occurrence:03d}_{slugify(title)}"


def load_outline_sections(pdf_path: Path) -> tuple[list[dict[str, Any]], int]:
    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count
        toc = doc.get_toc(simple=True)

    sections: list[dict[str, Any]] = [
        {
            "section_id": "document",
            "title": pdf_path.stem,
            "level": 0,
            "parent_id": None,
            "parent_title": None,
            "start_page": 1,
            "end_page": page_count,
            "source": "synthetic_root",
            "confidence": 1.0,
            "outline_level": 0,
            "order": 0,
            "kind": "document",
        }
    ]
    if not toc:
        return sections, page_count

    first_outline_page = max(1, min(int(toc[0][2]), page_count))
    if first_outline_page > 1:
        sections.append(
            {
                "section_id": "front_matter_pre_outline",
                "title": "Front Matter Before PDF Outline",
                "level": 1,
                "parent_id": "document",
                "parent_title": pdf_path.stem,
                "start_page": 1,
                "end_page": first_outline_page - 1,
                "source": "synthetic_pre_outline",
                "confidence": 1.0,
                "outline_level": 0,
                "order": 1,
                "kind": "front_matter",
            }
        )

    seen_main_part = False
    stack: dict[int, dict[str, Any]] = {0: sections[0]}
    occurrences: dict[str, int] = {}

    outline_order_start = len(sections)
    for order, entry in enumerate(toc, start=outline_order_start):
        outline_level, title, page_num = entry
        title = str(title).strip()
        kind, inferred_level = infer_outline_kind_and_level(title, seen_main_part)
        if kind == "part":
            seen_main_part = True

        occurrences[kind] = occurrences.get(kind, 0) + 1
        parent = stack.get(inferred_level - 1, sections[0])
        section = {
            "section_id": make_section_id(title, kind, occurrences[kind]),
            "title": title,
            "level": inferred_level,
            "parent_id": parent["section_id"] if parent else None,
            "parent_title": parent["title"] if parent else None,
            "start_page": max(1, min(int(page_num), page_count)),
            "end_page": page_count,
            "source": "pdf_outline",
            "confidence": 0.98 if outline_level == inferred_level else 0.9,
            "outline_level": int(outline_level),
            "order": order,
            "kind": kind,
        }
        sections.append(section)
        stack[inferred_level] = section
        for level in [level for level in stack if level > inferred_level]:
            del stack[level]

    for index, section in enumerate(sections[1:], start=1):
        end_page = page_count
        for next_section in sections[index + 1 :]:
            if next_section["level"] <= section["level"]:
                end_page = max(section["start_page"], next_section["start_page"] - 1)
                break
        section["end_page"] = end_page

    return sections, page_count


def build_sections_from_docling_headings(
    elements: list[dict[str, Any]],
    document_title: str,
    page_count: int,
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = [
        {
            "section_id": "document",
            "title": document_title,
            "level": 0,
            "parent_id": None,
            "parent_title": None,
            "start_page": 1,
            "end_page": page_count,
            "source": "synthetic_root",
            "confidence": 1.0,
            "outline_level": 0,
            "order": 0,
            "kind": "document",
        }
    ]

    stack: dict[int, dict[str, Any]] = {0: sections[0]}
    occurrences: dict[str, int] = {}
    
    headings = [e for e in elements if e["type"] == "heading"]
    if not headings:
        return sections
        
    seen_main_part = False
    order = 1
    for heading in headings:
        title = heading.get("text", "").strip()
        if not title:
            continue
            
        docling_level = heading.get("docling_level") or 1
        kind, inferred_level = infer_outline_kind_and_level(title, seen_main_part)
        if kind == "part":
            seen_main_part = True
            
        # Use Docling's explicitly assigned level if available
        level = docling_level
        
        occurrences[kind] = occurrences.get(kind, 0) + 1
        
        parent_level = level - 1
        while parent_level > 0 and parent_level not in stack:
            parent_level -= 1
        parent = stack.get(parent_level, sections[0])
        
        section = {
            "section_id": make_section_id(title, kind, occurrences[kind]),
            "title": title,
            "level": level,
            "parent_id": parent["section_id"],
            "parent_title": parent["title"],
            "start_page": max(1, min(int(heading.get("page_num") or 1), page_count)),
            "end_page": page_count,
            "source": "docling_heading",
            "confidence": 0.8,
            "outline_level": level,
            "order": order,
            "kind": kind,
        }
        sections.append(section)
        stack[level] = section
        
        for l in list(stack.keys()):
            if l > level:
                del stack[l]
        order += 1
        
    for index, section in enumerate(sections):
        end_page = page_count
        for next_section in sections[index + 1 :]:
            if next_section["level"] <= section["level"]:
                end_page = max(section["start_page"], next_section["start_page"] - 1)
                break
        section["end_page"] = end_page
        
    return sections


def ref_id(ref: dict[str, Any] | str | None) -> str | None:
    if ref is None:
        return None
    if isinstance(ref, str):
        return ref
    return ref.get("$ref")


def first_prov(item: dict[str, Any]) -> dict[str, Any]:
    prov = item.get("prov") or []
    if not prov:
        return {}
    return prov[0] or {}


def bbox_to_list(bbox: dict[str, Any] | None) -> list[float] | None:
    if not bbox:
        return None
    return [
        float(bbox["l"]),
        float(bbox["t"]),
        float(bbox["r"]),
        float(bbox["b"]),
    ]


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def text_for_item(item: dict[str, Any]) -> str:
    text = normalize_text(item.get("text"))
    marker = infer_marker(item)
    if marker and not text.startswith(marker):
        return f"{marker} {text}".strip()
    return text


def infer_marker(item: dict[str, Any]) -> str:
    marker = normalize_text(item.get("marker"))
    if marker:
        return marker
    raw_text = normalize_text(item.get("orig") or item.get("text"))
    match = re.match(r"^(\d+\.)\s+", raw_text)
    return match.group(1) if match else ""


def parent_is_list_group(item: dict[str, Any], ref_maps: dict[str, dict[str, Any]]) -> bool:
    parent_ref = ref_id(item.get("parent"))
    if not parent_ref:
        return False
    parent = ref_maps.get(parent_ref, {})
    return parent.get("label") == "list" or parent.get("name") == "list"


def text_element_type(item: dict[str, Any], ref_maps: dict[str, dict[str, Any]]) -> str:
    label = item.get("label")
    if label == "section_header" and infer_marker(item) and parent_is_list_group(item, ref_maps):
        return "list_item"
    return {
        "section_header": "heading",
        "list_item": "list_item",
        "caption": "caption",
        "text": "paragraph",
    }.get(label, "text")


def build_ref_maps(docling_dict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    maps: dict[str, dict[str, Any]] = {}
    for key in ("texts", "tables", "pictures", "groups"):
        for item in docling_dict.get(key, []):
            self_ref = item.get("self_ref")
            if self_ref:
                maps[self_ref] = item
    return maps


def table_to_rows(table: dict[str, Any]) -> list[list[str]]:
    grid = table.get("data", {}).get("grid") or []
    rows: list[list[str]] = []
    for row in grid:
        rows.append([normalize_text(cell.get("text")) for cell in row])
    return rows


def make_base_element(
    item: dict[str, Any],
    source_ref: str,
    element_type: str,
    order: int,
) -> dict[str, Any]:
    prov = first_prov(item)
    bbox = prov.get("bbox")
    page_num = prov.get("page_no")
    return {
        "element_id": f"p{int(page_num or 0):04d}_e{order:05d}",
        "source_ref": source_ref,
        "type": element_type,
        "page_num": page_num,
        "bbox": bbox_to_list(bbox),
        "bbox_coord_origin": bbox.get("coord_origin") if bbox else None,
        "order": order,
        "source_parser": "docling",
        "source_label": item.get("label"),
        "docling_level": item.get("level"),
    }


def flatten_docling_elements(docling_dict: dict[str, Any]) -> list[dict[str, Any]]:
    ref_maps = build_ref_maps(docling_dict)
    body_children = docling_dict.get("body", {}).get("children", [])
    elements: list[dict[str, Any]] = []
    visited: set[str] = set()

    def append_ref(source_ref: str) -> None:
        if source_ref in visited:
            return
        item = ref_maps.get(source_ref)
        if not item:
            return
        visited.add(source_ref)

        if source_ref.startswith("#/groups/"):
            for child in item.get("children", []):
                child_ref = ref_id(child)
                if child_ref:
                    append_ref(child_ref)
            return

        if source_ref.startswith("#/texts/"):
            order = len(elements) + 1
            element_type = text_element_type(item, ref_maps)
            element = make_base_element(item, source_ref, element_type, order)
            element["text"] = text_for_item(item)
            element["raw_text"] = item.get("orig") or item.get("text") or ""
            marker = infer_marker(item)
            if marker:
                element["marker"] = marker
            if item.get("enumerated") is not None:
                element["enumerated"] = item.get("enumerated")
            elements.append(element)
            return

        if source_ref.startswith("#/tables/"):
            caption_refs = [ref_id(ref) for ref in item.get("captions", [])]
            for caption_ref in caption_refs:
                if caption_ref:
                    append_ref(caption_ref)

            order = len(elements) + 1
            element = make_base_element(item, source_ref, "table", order)
            rows = table_to_rows(item)
            captions = [
                text_for_item(ref_maps[caption_ref])
                for caption_ref in caption_refs
                if caption_ref and caption_ref in ref_maps
            ]
            element["text"] = normalize_text(" ".join(captions))
            element["caption_refs"] = [ref for ref in caption_refs if ref]
            element["captions"] = captions
            element["rows"] = rows
            element["row_count"] = len(rows)
            element["col_count"] = max((len(row) for row in rows), default=0)
            elements.append(element)
            return

        if source_ref.startswith("#/pictures/"):
            order = len(elements) + 1
            element = make_base_element(item, source_ref, "picture", order)
            element["text"] = ""
            elements.append(element)

    for child in body_children:
        child_ref = ref_id(child)
        if child_ref:
            append_ref(child_ref)

    # Keep orphaned Docling objects visible, but append them after body order.
    for source_ref in sorted(ref_maps):
        if source_ref.startswith("#/texts/") or source_ref.startswith("#/tables/"):
            append_ref(source_ref)

    return elements


def section_for_page(sections: list[dict[str, Any]], page_num: int | None) -> dict[str, Any]:
    if not page_num:
        return sections[0]
    candidates = [
        section
        for section in sections
        if section["start_page"] <= page_num <= section["end_page"]
    ]
    if not candidates:
        return sections[0]
    return max(candidates, key=lambda section: (section["level"], section["start_page"], section["order"]))


def attach_sections(
    elements: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for element in elements:
        section = section_for_page(sections, element.get("page_num"))
        element["section_id"] = section["section_id"]
        element["section_title"] = section["title"]
        element["section_level"] = section["level"]
        element["parent_section_id"] = section["parent_id"]
        element["parent_section_title"] = section["parent_title"]
    return elements


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_report(
    pdf_path: Path,
    docling_json_path: Path,
    sections: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    page_count: int,
) -> dict[str, Any]:
    section_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    pages = set()
    for element in elements:
        section_counts[element["section_id"]] = section_counts.get(element["section_id"], 0) + 1
        type_counts[element["type"]] = type_counts.get(element["type"], 0) + 1
        if element.get("page_num"):
            pages.add(element["page_num"])

    unassigned = section_counts.get("document", 0)
    warnings = []
    if unassigned:
        warnings.append("Some elements only matched the synthetic document root.")
    if not any(section["source"] == "pdf_outline" for section in sections):
        if not any(section["source"] == "docling_heading" for section in sections):
            warnings.append("No PDF outline sections or Docling headings were found.")

    return {
        "source_file": str(pdf_path),
        "docling_json_file": str(docling_json_path),
        "pdf_page_count": page_count,
        "section_count": len(sections),
        "outline_section_count": sum(1 for section in sections if section["source"] == "pdf_outline"),
        "docling_heading_section_count": sum(1 for section in sections if section["source"] == "docling_heading"),
        "element_count": len(elements),
        "element_page_count": len(pages),
        "element_type_counts": type_counts,
        "root_only_element_count": unassigned,
        "warnings": warnings,
        "phase1b_passed": not warnings,
    }


def main() -> None:
    args = parse_args()
    if not args.pdf_path.exists():
        raise FileNotFoundError(args.pdf_path)
    if not args.input_dir.exists():
        raise FileNotFoundError(args.input_dir)

    output_dir = args.output_dir or args.input_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    pdf_stem = args.pdf_path.stem
    sections, page_count = load_outline_sections(args.pdf_path)
    
    # Load all docling elements for this PDF
    elements = []
    docling_files = list(args.input_dir.glob(f"{pdf_stem}.*.docling.json"))
    for df in docling_files:
        docling_dict = read_json(df)
        elements.extend(flatten_docling_elements(docling_dict))
    
    # Load VLM elements for this PDF (these can be orphans with no docling equivalent)
    vlm_path = args.input_dir / f"{pdf_stem}.hybrid_vlm.vlm_elements.jsonl"
    if vlm_path.exists():
        vlm_elements = read_jsonl(vlm_path)
        elements.extend(vlm_elements)
    
    # Sort elements chronologically by page number and order
    elements.sort(key=lambda e: (e.get("page_num") or 0, e.get("order") or 0))

    # Fallback to Docling headings if PDF outline is absent/sparse
    if len(sections) <= 2 and not any(s["source"] == "pdf_outline" for s in sections):
        sections = build_sections_from_docling_headings(elements, args.pdf_path.stem, page_count)
        
    attach_sections(elements, sections)

    stem = f"{pdf_stem}.hybrid_vlm"
    sections_path = output_dir / f"{stem}.document.sections.json"
    elements_path = output_dir / f"{stem}.document.elements.jsonl"
    report_path = output_dir / f"{stem}.segmentation_report.json"

    write_json(sections_path, sections)
    write_jsonl(elements_path, elements)
    
    report_data = build_report(
        pdf_path=args.pdf_path,
        docling_json_path=docling_files[0] if docling_files else args.input_dir,
        sections=sections,
        elements=elements,
        page_count=page_count,
    )
    report_data["docling_files_processed"] = len(docling_files)
    write_json(
        report_path,
        report_data,
    )

    print(f"Sections:       {len(sections)}")
    print(f"Elements:       {len(elements)}")
    print(f"Written:        {sections_path}")
    print(f"Written:        {elements_path}")
    print(f"Written:        {report_path}")


if __name__ == "__main__":
    main()
