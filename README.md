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

## Phase 1A - Hybrid OCR

Use the hybrid parser when a PDF may contain both native-text pages and scanned
pages. It preflights the PDF, uses OCR only for low-text image pages, and uses
native Docling parsing for the rest.

Preview the hybrid plan without running Docling:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --plan-only
```

Run the full hybrid parse:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf
```

For faster development, run hybrid mode on a small range:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --page-range 1 20
```

## Phase 1A - Hybrid VLM Fallback

The Hybrid-VLM parser first uses native Docling or OCR, verifies page-level
output, and sends only rejected visual pages to the provider selected by the
user. It never stores credentials in output artifacts.

Configure credentials as process environment variables, or place the rotated
values in the gitignored root `.env` file using the variable names in
`.env.example`. `VLM_LOCAL_*` takes priority, followed by compatible
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` values. The default local
model is `qwen3` and the endpoint is checked through `/models` before a local
parse starts.

When both providers are configured, set the hosted OpenAI credential as
`VLM_OPENAI_API_KEY`; this avoids overloading the local gateway's standard
`OPENAI_API_KEY` compatibility alias.

The configured `qwen3` model is accepted for local chat and gateway checks. For
VLM page fallback, the gateway must also accept image messages; an image-unsupported
model is recorded as a manual-review failure rather than yielding invented data.

Preview the complete routing plan without parser or model calls:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --plan-only
```

List models offered by the local OpenAI-compatible endpoint:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --list-local-models
```

Check local gateway connectivity and the selected `qwen3` model:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --health-check-local
```

Run a basic local chat-completions smoke test:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --local-chat-smoke-test
```

Run using the local gateway:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --provider local --page-range 1 3
```

For a first-30-page local test with page-by-page status messages, a live
spinner, elapsed times, VLM token/TPS metrics, and a visual progress bar:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --provider local --page-range 1 30 --max-vlm-pages 3 --progress-mode page
```

`--progress-mode auto` is the default and uses page mode for 30 pages or fewer.
For larger runs it uses range mode to retain efficient Docling conversion while
still displaying live range status and an overall progress bar.

To bypass native Docling and OCR and run only the selected VLM on a specific
page, use `--vlm-only`. For example, test page 6 directly with the local model:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\12th_Polity_executive.pdf --provider local --page-range 6 6 --vlm-only --max-vlm-pages 0 --progress-mode page
```

If the local model spends its first completion budget on reasoning and returns
empty or malformed JSON, the parser automatically retries that page once with
a concise JSON-only prompt and `5000` completion tokens. Override that retry
limit with `--local-retry-max-tokens 6000` when a complex page still needs more
room.

Run using OpenAI `gpt-5-mini`:

```powershell
.\.venv\Scripts\python.exe .\unstructured_data\pipeline\stage1a_hybrid_vlm_parse.py .\unstructured_data\Docs\LAKSHMIKANT.pdf --provider openai --page-range 1 3
```

The default `--max-vlm-pages 10` prevents an unexpected large number of paid
or remote VLM calls. Use `--force-vlm-page 55` to test a specific page.
