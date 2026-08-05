# Knowledge Graph Extraction Pipeline

PDF parsing and knowledge-graph preparation live under `unstructured_data`.
Run every command below from the repository root in PowerShell.

## Output And Cleanup

All parser-generated files are written to:

```text
unstructured_data/output/parsed/
```

Inspect generated files before removing them:

```powershell
Get-ChildItem -LiteralPath .\unstructured_data\output\parsed -Force
```

Remove **all generated parser output** while preserving the tracked `.gitkeep`
file. This does not remove source PDFs in `unstructured_data/Docs`, code, or
your `.env` file:

```powershell
Get-ChildItem -LiteralPath .\unstructured_data\output\parsed -Force | Where-Object { $_.Name -ne '.gitkeep' } | Remove-Item -Recurse -Force
```

Remove only generated files for one document. Replace `12th_Polity_executive`
with the PDF filename without `.pdf`:

```powershell
Get-ChildItem -LiteralPath .\unstructured_data\output\parsed -Force | Where-Object { $_.Name -like '12th_Polity_executive.*' } | Remove-Item -Recurse -Force
```

## Phase 1A - Docling

Use Docling alone for a normal native-text PDF:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_docling_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf
```

Use a small range during development. Page numbers are inclusive and start at
one:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_docling_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --page-range 1 10
```

Enable OCR for a scanned PDF. Add `--force-full-page-ocr` only when every page
should be OCR processed:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_docling_parse.py .\unstructured_data\Docs\document.pdf --ocr
```

## Phase 1A - Hybrid OCR

The Hybrid OCR parser preflights every page, uses native Docling for usable
text pages, and uses OCR only for pages that look scanned or image-dominant.

Preview the routing decision without parsing:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --plan-only
```

Run the complete document:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf
```

Run only the first 30 pages with the same automatic native/OCR routing:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --page-range 1 30
```

## Phase 1A - Hybrid VLM Fallback

The Hybrid-VLM parser first tries native Docling or OCR, checks page output,
then sends only pages needing visual understanding to the provider selected by
`--provider`. It produces Docling Markdown/JSON for native or OCR pages and
`*.vlm_elements.jsonl` for accepted VLM pages.

One exception avoids wasting time in Docling: a page with zero native text, no
embedded raster image, and at least 200 vector drawings is sent directly to
VLM. This identifies vector-only tables and page layouts that Docling cannot
read as native text. Adjust the threshold only for unusual PDFs with
`--direct-vlm-drawing-threshold NUMBER`.

Create a local `.env` from `.env.example` once, then put real values only in
`.env`. Do not commit `.env`:

```powershell
Copy-Item .\.env.example .\.env
```

The local provider reads `VLM_LOCAL_BASE_URL`, `VLM_LOCAL_API_KEY`, and
`VLM_LOCAL_MODEL` (default `qwen3`). Hosted OpenAI uses
`VLM_OPENAI_API_KEY` and `VLM_OPENAI_MODEL`. See `.env.example` for the exact
variable names.

Preview Hybrid-VLM routing without parser or model calls:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --plan-only
```

Check the local gateway and verify that its configured model is available:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --health-check-local
```

List the models exposed by the local gateway:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --list-local-models
```

Run a simple local chat-completions check before a long VLM job:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --local-chat-smoke-test
```

Run the normal Hybrid-VLM pipeline for the whole document with the local model.
It automatically decides native, OCR, VLM, or manual review per page. The
default VLM cap is ten pages per run:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --provider local --progress-mode auto
```

Run the first 30 pages with page-by-page spinner, elapsed time, VLM token/TPS
metrics, and a progress bar. `--max-vlm-pages 0` removes the VLM-page cap; use
it only when you intend to allow every eligible page to call the VLM:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --provider local --page-range 1 30 --max-vlm-pages 0 --progress-mode page
```

Force a particular page through the VLM while retaining the normal hybrid
handling for the other selected pages:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --provider local --page-range 1 10 --force-vlm-page 6 --progress-mode page
```

Test only a selected page with the VLM, bypassing Docling and OCR completely:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --provider local --page-range 6 6 --vlm-only --max-vlm-pages 0 --progress-mode page
```

Test the automatic vector-only direct-VLM route. Unlike `--vlm-only`, this is a
normal hybrid run: the preflight itself selects VLM for page 2:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive_5_6.pdf --provider openai --page-range 2 2 --max-vlm-pages 0 --progress-mode page
```

When local Qwen returns empty or malformed JSON, the parser retries once with a
concise JSON-only prompt and `5000` completion tokens. Raise only that retry
limit for complex pages:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --provider local --page-range 6 6 --vlm-only --local-retry-max-tokens 6000 --progress-mode page
```

Run the Hybrid-VLM pipeline with hosted OpenAI. This uses `VLM_OPENAI_API_KEY`
and can incur API cost:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --provider openai --page-range 1 30 --max-vlm-pages 3 --progress-mode page
```

## Phase 1B - Structural Segmentation

Phase 1B builds `sections.json` and `elements.jsonl` from a Docling JSON
artifact. Run it after a Docling or Hybrid OCR conversion; give it the source
PDF and one generated `*.docling.json` path. The example below assumes the
full Phase 1A Docling command above was run first:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1b_structural_segment.py .\unstructured_data\Docs\12th_Polity_executive.pdf .\unstructured_data\output\parsed\12th_Polity_executive.docling.json
```

## Test The Pipeline Code

Run the Hybrid-VLM unit tests:

```powershell
.\.venv\Scripts\python.exe -B -m unittest unstructured_data.tests.test_stage1a_hybrid_vlm_parse -v
```
