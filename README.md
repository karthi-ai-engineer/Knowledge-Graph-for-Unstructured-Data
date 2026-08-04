# Knowledge Graph Extraction Pipeline

This workspace contains the PDF parsing and knowledge graph extraction pipeline
under `unstructured_data`.

## Phase 1A - Docling Parse

Run Phase 1A from the repository root:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_docling_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf
```

For faster development, parse a small page range:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_docling_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --page-range 4 20
```

For scanned pages, enable OCR:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_docling_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --page-range 1 3 --ocr
```

Phase 1A writes generated parser artifacts to:

```text
unstructured_data/output/parsed/
```
