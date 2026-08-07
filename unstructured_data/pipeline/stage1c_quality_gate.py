import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 1C: Run quality gates on parsed documents.")
    parser.add_argument("input_dir", type=Path, help="Directory containing Phase 1B outputs")
    parser.add_argument("pdf_stem", type=str, help="The base name of the PDF (e.g. document.hybrid_vlm)")
    return parser.parse_args()


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    data = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))
    return data


def write_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def check_provenance(elements: List[Dict[str, Any]]) -> List[str]:
    """Check that every element has required provenance fields."""
    failures = []
    for el in elements:
        element_id = el.get("element_id")
        if not element_id:
            failures.append("Element found without an element_id.")
            continue
        
        if not el.get("page_num"):
            failures.append(f"Element {element_id} is missing page_num.")
        if not el.get("section_id"):
            failures.append(f"Element {element_id} is missing section_id.")
        if not el.get("source_parser"):
            failures.append(f"Element {element_id} is missing source_parser metadata.")
            
    return failures


def check_text_reconstruction(elements: List[Dict[str, Any]]) -> List[str]:
    """Check for obvious word-join artifacts (e.g. CamelCase errors)."""
    failures = []
    # Match camel case words that are suspiciously long and likely word joins
    # e.g., "MainExamination", "beintroduced"
    word_join_pattern = re.compile(r'[a-z]+[A-Z][a-z]+')
    
    for el in elements:
        text = el.get("text", "")
        if not text:
            continue
            
        # Simplified heuristic: look for camel case in non-code, non-URL text
        words = text.split()
        for word in words:
            if word_join_pattern.search(word) and len(word) > 12:
                # We skip things that look like URLs or file paths
                if "http" not in word and "/" not in word and "\\" not in word:
                    failures.append(f"Element {el.get('element_id')} contains potential word-join artifact: '{word}'")
                    # Limit to 5 warnings to avoid flooding
                    if len(failures) >= 5:
                        return failures
                        
    return failures


def check_section_tree(sections: List[Dict[str, Any]]) -> List[str]:
    """Ensure sections have stable IDs and page ranges."""
    failures = []
    for sec in sections:
        sec_id = sec.get("section_id")
        if not sec_id:
            failures.append("Section found without a section_id.")
            continue
            
        if sec.get("start_page") is None or sec.get("end_page") is None:
            failures.append(f"Section {sec_id} is missing a page range.")
        elif sec["start_page"] > sec["end_page"]:
            failures.append(f"Section {sec_id} has invalid page range ({sec['start_page']} to {sec['end_page']}).")
            
        if not sec.get("title"):
            failures.append(f"Section {sec_id} is missing a title.")
            
    return failures


def check_table_duplication(elements: List[Dict[str, Any]]) -> List[str]:
    """Check if tables are extracted structurally but also duplicated as loose body text nearby."""
    failures = []
    tables = [e for e in elements if e.get("type") == "table"]
    texts = [e for e in elements if e.get("type") in ["paragraph", "text"]]
    
    for table in tables:
        table_text = table.get("text", "")
        if not table_text or "rows" not in table:
            continue
            
        # Flatten table rows to text
        table_content = " ".join([" ".join(row) for row in table["rows"]])
        
        # Check nearby text elements (same page)
        page_num = table.get("page_num")
        nearby_texts = [t for t in texts if t.get("page_num") == page_num]
        
        for nt in nearby_texts:
            text = nt.get("text", "")
            if len(text) > 20 and text in table_content:
                failures.append(f"Potential table content duplicated in text element {nt.get('element_id')} near table {table.get('element_id')}")
                
    return failures


def check_reading_order(elements: List[Dict[str, Any]]) -> List[str]:
    """Check that elements are ordered chronologically by page number."""
    failures = []
    current_page = -1
    
    for el in elements:
        page_num = el.get("page_num")
        if page_num:
            if page_num < current_page:
                failures.append(f"Reading order violation: Element {el.get('element_id')} is on page {page_num} but previous element was on page {current_page}.")
            current_page = max(current_page, page_num)
            
    return failures


def run_quality_gates(input_dir: Path, pdf_stem: str) -> None:
    elements_path = input_dir / f"{pdf_stem}.document.elements.jsonl"
    sections_path = input_dir / f"{pdf_stem}.document.sections.json"
    
    if not elements_path.exists():
        print(f"ERROR: Missing {elements_path}")
        return
    if not sections_path.exists():
        print(f"ERROR: Missing {sections_path}")
        return
        
    elements = read_jsonl(elements_path)
    sections = read_json(sections_path)
    
    print(f"Running Quality Gates on {pdf_stem}...")
    print(f"Total Elements: {len(elements)}")
    print(f"Total Sections: {len(sections)}\n")
    
    results = {
        "provenance_issues": check_provenance(elements),
        "text_reconstruction_issues": check_text_reconstruction(elements),
        "section_tree_issues": check_section_tree(sections),
        "table_duplication_issues": check_table_duplication(elements),
        "reading_order_issues": check_reading_order(elements),
    }
    
    all_passed = True
    for check_name, issues in results.items():
        if issues:
            all_passed = False
            print(f"❌ {check_name.replace('_', ' ').title()} ({len(issues)} issues found):")
            for issue in issues[:5]:  # Limit output to first 5 per category
                print(f"  - {issue}")
            if len(issues) > 5:
                print(f"  ... and {len(issues) - 5} more.")
        else:
            print(f"✅ {check_name.replace('_', ' ').title()}: PASSED")
            
    print("\n--- Final Result ---")
    if all_passed:
        print("✅ ALL QUALITY GATES PASSED. Ready for Phase 2 (Semantic Chunking).")
    else:
        print("❌ QUALITY GATES FAILED. Manual review and parser tuning required before Phase 2.")
        
    # Write report
    report_path = input_dir / f"{pdf_stem}.quality_gate_report.json"
    write_json(report_path, {
        "pdf_stem": pdf_stem,
        "passed": all_passed,
        "results": results,
    })
    print(f"\nReport saved to: {report_path}")


def main() -> None:
    args = parse_args()
    if not args.input_dir.exists():
        raise FileNotFoundError(args.input_dir)
        
    run_quality_gates(args.input_dir, args.pdf_stem)


if __name__ == "__main__":
    main()
