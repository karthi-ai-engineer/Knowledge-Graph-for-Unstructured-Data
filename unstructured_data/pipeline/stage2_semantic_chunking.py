import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List
import tiktoken

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 2: Semantic Chunking")
    parser.add_argument("input_dir", type=Path, help="Directory containing Phase 1 outputs")
    parser.add_argument("pdf_stem", type=str, help="The base name of the PDF (e.g., document.hybrid_vlm)")
    parser.add_argument("--chunk-size", type=int, default=1000, help="Target token limit per chunk")
    parser.add_argument("--overlap-size", type=int, default=150, help="Token overlap from previous chunk")
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


def write_jsonl(path: Path, data: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def get_breadcrumbs(section_id: str, section_dict: Dict[str, Dict[str, Any]]) -> str:
    """Reconstruct the chapter/section hierarchy path."""
    path = []
    curr = section_id
    while curr and curr in section_dict:
        title = section_dict[curr].get('title', '').strip()
        if title:
            path.insert(0, title)
        curr = section_dict[curr].get('parent_id')
    return " > ".join(path)


def truncate_to_tail(text: str, token_limit: int, tokenizer) -> str:
    """Return the last `token_limit` tokens of the text."""
    if not text:
        return ""
    tokens = tokenizer.encode(text)
    if len(tokens) <= token_limit:
        return text
    return tokenizer.decode(tokens[-token_limit:])


def chunk_elements(
    elements: List[Dict[str, Any]], 
    sections: List[Dict[str, Any]], 
    chunk_size: int, 
    overlap_size: int
) -> List[Dict[str, Any]]:
    
    tokenizer = tiktoken.get_encoding("cl100k_base")
    section_dict = {s["section_id"]: s for s in sections}
    
    chunks = []
    
    current_chunk_text = []
    current_tokens = 0
    current_pages = set()
    current_section = None
    
    prev_tail = ""
    chunk_index = 1
    
    def finalize_chunk():
        nonlocal current_chunk_text, current_tokens, current_pages, current_section, prev_tail, chunk_index
        
        if not current_chunk_text:
            return
            
        full_text = "\n\n".join(current_chunk_text)
        breadcrumbs = get_breadcrumbs(current_section, section_dict) if current_section else ""
        
        chunk = {
            "chunk_id": f"chunk_{chunk_index:04d}",
            "page_range": sorted(list(current_pages)),
            "section_id": current_section,
            "hierarchy": breadcrumbs,
            "text": full_text,
            "prev_chunk_tail": prev_tail,
            "token_count": current_tokens
        }
        chunks.append(chunk)
        
        # Prepare for next chunk
        prev_tail = truncate_to_tail(full_text, overlap_size, tokenizer)
        chunk_index += 1
        current_chunk_text = []
        current_tokens = 0
        current_pages = set()
        # Note: current_section carries over until a new element changes it

    for el in elements:
        text = el.get("text", "").strip()
        
        # If it's a table, serialize the rows into a readable format
        if el.get("type") == "table" and "rows" in el:
            table_text = []
            if text:
                table_text.append(f"Table Caption: {text}")
            for row in el.get("rows", []):
                # Clean up empty cells, remove newlines, escape pipes, and join with pipes
                cleaned_row = [str(cell).strip().replace("\n", " ").replace("\r", "").replace("|", "\\|") if cell else "" for cell in row]
                table_text.append(" | ".join(cleaned_row))
            text = "\n".join(table_text)
            
        if not text.strip():
            continue
            
        el_tokens = len(tokenizer.encode(text))
        sec_id = el.get("section_id")
        page_num = el.get("page_num")
        
        # If element is huge (e.g. giant table), we have to force split it
        # (Simplified for now: we just allow it to slightly exceed or we would sentence-split)
        
        # Check if adding this element exceeds the chunk size
        # We also prefer to break if the section_id changes at a high level, 
        # but for Phase 2 we mainly chunk by size to maintain context density.
        if current_tokens + el_tokens > chunk_size and current_tokens > 0:
            finalize_chunk()
            
        # Update current state
        current_chunk_text.append(text)
        current_tokens += el_tokens
        if page_num:
            current_pages.add(page_num)
        if sec_id:
            current_section = sec_id
            
    # Finalize the last chunk
    finalize_chunk()
    return chunks


def main():
    args = parse_args()
    
    elements_path = args.input_dir / f"{args.pdf_stem}.document.elements.jsonl"
    sections_path = args.input_dir / f"{args.pdf_stem}.document.sections.json"
    
    if not elements_path.exists() or not sections_path.exists():
        logging.error(f"Missing input files. Please ensure Phase 1 outputs exist in {args.input_dir}")
        return
        
    logging.info(f"Loading elements from {elements_path.name}")
    elements = read_jsonl(elements_path)
    sections = read_json(sections_path)
    
    logging.info(f"Loaded {len(elements)} elements and {len(sections)} sections.")
    
    chunks = chunk_elements(elements, sections, args.chunk_size, args.overlap_size)
    
    output_path = args.input_dir / f"{args.pdf_stem}.document.chunks.jsonl"
    write_jsonl(output_path, chunks)
    
    logging.info(f"Successfully created {len(chunks)} semantic chunks.")
    logging.info(f"Written to: {output_path}")

if __name__ == "__main__":
    main()
