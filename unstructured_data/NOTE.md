# Folder note

All project files have been organized under `unstructured_data`.

Current layout:

- `Docs/` - source PDF files.
- `pipeline/` - parsing and processing scripts.
- `output/` - generated parsed output.
- `notes/` - design and planning notes.
- `requirements.txt` - Python package requirements for this project.

The `.venv` folder remains one level above `unstructured_data` because it is a local Python environment, not project data.

Example parser command from the workspace root:

```powershell
.venv/Scripts/python.exe unstructured_data/pipeline/stage1_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf
```

Phase 1A Docling parser command from the workspace root:

```powershell
.venv/Scripts/python.exe unstructured_data/pipeline/stage1a_docling_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf
```

For faster development runs, limit the conversion to a small page range:

```powershell
.venv/Scripts/python.exe unstructured_data/pipeline/stage1a_docling_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf --page-range 4 20
```

Phase 1B structural segmentation command from the workspace root:

```powershell
.venv/Scripts/python.exe unstructured_data/pipeline/stage1b_structural_segment.py unstructured_data/Docs/LAKSHMIKANT.pdf unstructured_data/output/parsed/LAKSHMIKANT.p0044-p0049.docling.json
```

Phase 1A hybrid OCR parser command from the workspace root:

```powershell
.venv/Scripts/python.exe unstructured_data/pipeline/stage1a_hybrid_parse.py unstructured_data/Docs/LAKSHMIKANT.pdf --plan-only
```

Run without `--plan-only` to execute the hybrid parse. The hybrid parser
preflights all pages, selects OCR only for low-text image pages, and uses native
Docling parsing for the remaining pages.
