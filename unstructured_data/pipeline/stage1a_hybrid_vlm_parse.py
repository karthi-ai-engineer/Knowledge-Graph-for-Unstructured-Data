"""Phase 1A - Hybrid native/OCR/VLM document parsing.

Native Docling and OCR remain the default parsing routes. A vision-language
model is invoked for pages rejected by deterministic verification checks, pages
explicitly selected with --force-vlm-page, and vector-only pages that cannot
provide native text. Provider credentials are read only from environment
variables and are never written to output artifacts.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fitz  # PyMuPDF

from stage1a_docling_parse import (
    build_page_index,
    build_quality_report as build_docling_quality_report,
    make_converter,
    resolve_output_dir,
    write_json,
)
from stage1a_hybrid_parse import (
    DEFAULT_IMAGE_COVERAGE_THRESHOLD,
    DEFAULT_LOW_IMAGE_COVERAGE_THRESHOLD,
    DEFAULT_LOW_TEXT_THRESHOLD,
    DEFAULT_MIXED_TEXT_THRESHOLD,
    classify_page,
    get_requested_page_range,
    pages_to_ranges,
)


DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_LOCAL_MODEL = "qwen3"
DEFAULT_VLM_MAX_TOKENS = 2600
DEFAULT_LOCAL_RETRY_MAX_TOKENS = 5000
DEFAULT_DIRECT_VLM_DRAWING_THRESHOLD = 200
ELEMENT_TYPES = {
    "heading",
    "paragraph",
    "list_item",
    "caption",
    "table",
    "picture_description",
    "chart_description",
    "unresolved_visual",
}


class ProviderError(RuntimeError):
    """A provider or model returned no usable structured result."""


class ActivitySpinner:
    """Small terminal spinner for long synchronous parser/provider calls."""

    FRAMES = ("|", "/", "-", "\\")

    def __init__(self, message: str) -> None:
        self.message = message
        self.started_at = 0.0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.enabled = sys.stdout.isatty()

    def __enter__(self) -> "ActivitySpinner":
        self.started_at = time.perf_counter()
        if self.enabled:
            self.thread = threading.Thread(target=self._render, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        if self.enabled:
            sys.stdout.write("\r" + " " * 140 + "\r")
            sys.stdout.flush()

    def _render(self) -> None:
        frame_index = 0
        while not self.stop_event.is_set():
            elapsed = time.perf_counter() - self.started_at
            sys.stdout.write(
                f"\r{self.FRAMES[frame_index % len(self.FRAMES)]} {self.message} "
                f"{elapsed:0.1f}s"
            )
            sys.stdout.flush()
            frame_index += 1
            self.stop_event.wait(0.12)


def progress_bar(completed: int, total: int, detail: str) -> str:
    width = 28
    ratio = completed / total if total else 1.0
    filled = round(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {ratio * 100:5.1f}% ({completed}/{total}) {detail}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse a PDF with native Docling, OCR, and verified VLM fallback."
    )
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("output_dir", nargs="?", type=Path)
    parser.add_argument("--page-range", nargs=2, metavar=("START", "END"), type=int)
    parser.add_argument("--low-text-threshold", type=int, default=DEFAULT_LOW_TEXT_THRESHOLD)
    parser.add_argument(
        "--low-image-coverage-threshold",
        type=float,
        default=DEFAULT_LOW_IMAGE_COVERAGE_THRESHOLD,
    )
    parser.add_argument("--mixed-text-threshold", type=int, default=DEFAULT_MIXED_TEXT_THRESHOLD)
    parser.add_argument(
        "--image-coverage-threshold",
        type=float,
        default=DEFAULT_IMAGE_COVERAGE_THRESHOLD,
    )
    parser.add_argument("--force-full-page-ocr", action="store_true")
    parser.add_argument("--no-table-structure", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--vlm-only",
        action="store_true",
        help="Skip native Docling and OCR; send every selected page directly to the chosen VLM.",
    )
    parser.add_argument(
        "--progress-mode",
        choices=("auto", "page", "range"),
        default="auto",
        help="Progress granularity. Auto uses page mode for runs up to 30 pages.",
    )
    parser.add_argument(
        "--page-progress-limit",
        type=int,
        default=30,
        help="Maximum page count for auto page-by-page conversion. Default: 30.",
    )
    parser.add_argument(
        "--provider",
        choices=("none", "local", "openai"),
        default="none",
        help="VLM provider for rejected pages. 'none' writes a review queue without calls.",
    )
    parser.add_argument(
        "--openai-model",
        default=None,
        help=f"OpenAI vision model. Default: {DEFAULT_OPENAI_MODEL}.",
    )
    parser.add_argument(
        "--local-base-url",
        default=None,
        help="OpenAI-compatible local API base URL, for example http://host:port/v1.",
    )
    parser.add_argument(
        "--local-model",
        default=None,
        help=f"Local gateway model ID. Default: {DEFAULT_LOCAL_MODEL}.",
    )
    parser.add_argument(
        "--list-local-models",
        action="store_true",
        help="List local endpoint models and exit; requires VLM_LOCAL_API_KEY.",
    )
    parser.add_argument(
        "--health-check-local",
        action="store_true",
        help="Call the local gateway /models endpoint, validate the selected model, and exit.",
    )
    parser.add_argument(
        "--local-chat-smoke-test",
        action="store_true",
        help="Run a minimal non-streaming local chat request after the /models check, then exit.",
    )
    parser.add_argument(
        "--stream-local",
        action="store_true",
        help="Use server-sent-event streaming for local VLM requests when the gateway supports it.",
    )
    parser.add_argument(
        "--force-vlm-page",
        action="append",
        type=int,
        default=[],
        metavar="PAGE",
        help="Force a page through VLM after rendering; repeatable.",
    )
    parser.add_argument(
        "--max-vlm-pages",
        type=int,
        default=10,
        help="Safety limit for VLM calls; 0 disables the limit. Default: 10.",
    )
    parser.add_argument(
        "--vlm-image-dpi",
        type=int,
        default=160,
        help="Rendered page DPI sent to the VLM. Default: 160.",
    )
    parser.add_argument(
        "--vlm-image-coverage-threshold",
        type=float,
        default=0.50,
        help="Visual-risk threshold used by the verifier. Default: 0.50.",
    )
    parser.add_argument(
        "--direct-vlm-drawing-threshold",
        type=int,
        default=DEFAULT_DIRECT_VLM_DRAWING_THRESHOLD,
        help=(
            "Send zero-text, vector-only pages directly to VLM when their drawing count reaches "
            f"this threshold. Default: {DEFAULT_DIRECT_VLM_DRAWING_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--verifier-min-visual-text-chars",
        type=int,
        default=80,
        help="Minimum extracted characters expected from visual-risk pages. Default: 80.",
    )
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument(
        "--vlm-max-tokens",
        type=int,
        default=DEFAULT_VLM_MAX_TOKENS,
        help=f"Maximum VLM completion tokens. Default: {DEFAULT_VLM_MAX_TOKENS}.",
    )
    parser.add_argument(
        "--local-retry-max-tokens",
        type=int,
        default=DEFAULT_LOCAL_RETRY_MAX_TOKENS,
        help=(
            "Maximum completion tokens for the one compact local-VLM retry. "
            f"Default: {DEFAULT_LOCAL_RETRY_MAX_TOKENS}."
        ),
    )
    parser.add_argument(
        "--local-vlm-retries",
        type=int,
        default=1,
        help="Compact JSON retries after an invalid local VLM response. Default: 1.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional dotenv-style file. Defaults to the repository .env when present.",
    )
    return parser.parse_args()


def image_coverage(page: fitz.Page) -> float:
    area = max(1.0, float(page.rect.width * page.rect.height))
    total = 0.0
    for info in page.get_image_info():
        bbox = info.get("bbox")
        if not bbox:
            continue
        rect = fitz.Rect(bbox) & page.rect
        if not rect.is_empty:
            total += float(rect.width * rect.height)
    return round(min(1.0, total / area), 4)


def classify_vlm_page(page: fitz.Page, args: argparse.Namespace) -> dict[str, Any]:
    item = classify_page(page, args)
    coverage = image_coverage(page) if item["has_image"] else 0.0
    try:
        drawing_count = len(page.get_drawings())
    except Exception:
        drawing_count = 0
    reasons = []
    if (
        item["char_count"] == 0
        and not item["has_image"]
        and drawing_count >= args.direct_vlm_drawing_threshold
    ):
        item["mode"] = "vlm"
        item["reason"] = "vector_only_no_native_text"
        reasons.append("vector_only_no_native_text")
    if coverage >= args.vlm_image_coverage_threshold:
        reasons.append("high_image_coverage")
    if drawing_count >= 20 and item["char_count"] < args.mixed_text_threshold:
        reasons.append("drawing_dominant_low_text")
    item.update(
        {
            "initial_route": item["mode"],
            "image_coverage": coverage,
            "drawing_count": drawing_count,
            "vlm_candidate": bool(reasons),
            "vlm_candidate_reasons": reasons,
        }
    )
    return item


def build_plan(pdf_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    with fitz.open(pdf_path) as document:
        start, end = get_requested_page_range(args, document.page_count)
        pages = [classify_vlm_page(document[p - 1], args) for p in range(start, end + 1)]
        outline_count = len(document.get_toc(simple=True))
        page_count = document.page_count
    return {
        "source_file": str(pdf_path),
        "parser": "docling_hybrid_vlm",
        "pdf_page_count": page_count,
        "requested_page_range": [start, end],
        "pdf_outline_entry_count": outline_count,
        "strategy": {
            "native_ocr": {
                "low_text_threshold": args.low_text_threshold,
                "low_image_coverage_threshold": args.low_image_coverage_threshold,
                "mixed_text_threshold": args.mixed_text_threshold,
                "image_coverage_threshold": args.image_coverage_threshold,
            },
            "verifier": {
                "min_visual_text_chars": args.verifier_min_visual_text_chars,
                "image_coverage_threshold": args.vlm_image_coverage_threshold,
            },
            "vlm_provider": args.provider,
            "vlm_model": selected_model_name(args),
            "max_vlm_pages": args.max_vlm_pages,
            "direct_vlm_drawing_threshold": args.direct_vlm_drawing_threshold,
        },
        "page_count": len(pages),
        "native_page_count": sum(item["mode"] == "native" for item in pages),
        "ocr_page_count": sum(item["mode"] == "ocr" for item in pages),
            "vlm_candidate_page_count": sum(item["vlm_candidate"] for item in pages),
        "direct_vlm_page_count": sum(item["mode"] == "vlm" for item in pages),
        "ranges": pages_to_ranges(pages),
        "pages": pages,
    }


def resolve_progress_mode(plan: dict[str, Any], args: argparse.Namespace) -> str:
    if args.progress_mode != "auto":
        return args.progress_mode
    return "page" if plan["page_count"] <= args.page_progress_limit else "range"


def execution_items(plan: dict[str, Any], progress_mode: str) -> list[dict[str, Any]]:
    if progress_mode == "range":
        return [item for item in plan["ranges"] if item["mode"] in {"native", "ocr"}]
    return [
        {
            "mode": page["mode"],
            "start_page": page["page_num"],
            "end_page": page["page_num"],
            "page_count": 1,
        }
        for page in plan["pages"]
        if page["mode"] in {"native", "ocr"}
    ]


def selected_model_name(args: argparse.Namespace) -> str | None:
    if args.provider == "openai":
        return args.openai_model
    if args.provider == "local":
        return args.local_model
    return None


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding process environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_value(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def apply_environment_defaults(args: argparse.Namespace) -> None:
    args.openai_model = args.openai_model or env_value(
        "VLM_OPENAI_MODEL", "OPENAI_MODEL", default=DEFAULT_OPENAI_MODEL
    )
    args.local_base_url = args.local_base_url or env_value(
        "VLM_LOCAL_BASE_URL", "OPENAI_BASE_URL"
    )
    args.local_model = args.local_model or env_value(
        "VLM_LOCAL_MODEL", "OPENAI_MODEL", default=DEFAULT_LOCAL_MODEL
    )
    args.request_timeout = max(120, args.request_timeout)


def converter_args(args: argparse.Namespace, ocr: bool) -> SimpleNamespace:
    return SimpleNamespace(
        ocr=ocr,
        force_full_page_ocr=args.force_full_page_ocr,
        no_table_structure=args.no_table_structure,
    )


def range_stem(pdf_path: Path, item: dict[str, Any]) -> str:
    return (
        f"{pdf_path.stem}.hybrid_vlm.{item['mode']}."
        f"p{item['start_page']:04d}-p{item['end_page']:04d}"
    )


def convert_range(
    pdf_path: Path, output_dir: Path, item: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    range_args = converter_args(args, ocr=item["mode"] == "ocr")
    started_at = time.perf_counter()
    result = make_converter(range_args).convert(
        pdf_path, page_range=(item["start_page"], item["end_page"])
    )
    elapsed = time.perf_counter() - started_at
    docling_dict = result.document.export_to_dict()
    markdown = result.document.export_to_markdown()
    page_index = build_page_index(
        pdf_path, docling_dict, (item["start_page"], item["end_page"])
    )
    report = build_docling_quality_report(
        pdf_path, docling_dict, markdown, page_index,
        getattr(result.status, "name", str(result.status)), elapsed, range_args,
    )
    stem = range_stem(pdf_path, item)
    paths = {
        "docling_json": output_dir / f"{stem}.docling.json",
        "markdown": output_dir / f"{stem}.md",
        "page_index": output_dir / f"{stem}.page_index.json",
        "quality_report": output_dir / f"{stem}.quality_report.json",
    }
    write_json(paths["docling_json"], docling_dict)
    paths["markdown"].write_text(markdown, encoding="utf-8")
    write_json(paths["page_index"], page_index)
    write_json(paths["quality_report"], report)
    return {
        "mode": item["mode"],
        "page_range": [item["start_page"], item["end_page"]],
        "page_count": item["page_count"],
        "docling_status": report["docling_status"],
        "phase1a_passed": report["phase1a_passed"],
        "warnings": report["warnings"],
        "conversion_seconds": report["conversion_seconds"],
        **{name: str(path) for name, path in paths.items()},
    }


def item_pages(item: dict[str, Any]) -> set[int]:
    return {
        int(prov["page_no"])
        for prov in item.get("prov", [])
        if prov.get("page_no") is not None
    }


def docling_stats_by_page(docling_dict: dict[str, Any], page_nums: list[int]) -> dict[int, dict[str, int]]:
    stats = {
        page_num: {"text_chars": 0, "text_element_count": 0, "table_count": 0, "picture_count": 0}
        for page_num in page_nums
    }
    for item in docling_dict.get("texts", []):
        for page_num in item_pages(item):
            if page_num in stats:
                stats[page_num]["text_chars"] += len((item.get("text") or "").strip())
                stats[page_num]["text_element_count"] += 1
    for collection, key in (("tables", "table_count"), ("pictures", "picture_count")):
        for item in docling_dict.get(collection, []):
            for page_num in item_pages(item):
                if page_num in stats:
                    stats[page_num][key] += 1
    return stats


def verify_page(page: dict[str, Any], stats: dict[str, int], args: argparse.Namespace) -> dict[str, Any]:
    reasons: list[str] = []
    if stats["text_element_count"] == 0 and stats["table_count"] == 0:
        reasons.append("no_docling_content")
    visual_risk = bool(page["vlm_candidate"])
    if visual_risk and stats["text_chars"] < args.verifier_min_visual_text_chars:
        reasons.append("visual_risk_with_sparse_extraction")
    if page["mode"] == "ocr" and page["char_count"] == 0 and stats["text_chars"] == 0:
        reasons.append("ocr_produced_no_text")
    return {
        "page_num": page["page_num"],
        "initial_route": page["initial_route"],
        "final_route": page["initial_route"],
        "decision": "accept" if not reasons else "fallback_to_vlm",
        "accepted": not reasons,
        "confidence": 0.95 if not reasons else 0.0,
        "reasons": reasons,
        "output_stats": stats,
        "manual_review_required": False,
    }


def normalize_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    while normalized.endswith("/v1/v1"):
        normalized = normalized[: -len("/v1")]
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def api_json_request(
    url: str,
    api_key: str,
    payload: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method="POST" if payload is not None else "GET",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
            result["_client_metrics"] = {
                "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                "endpoint": url,
            }
            return result
    except urllib.error.HTTPError as exc:
        messages = {
            401: "authentication failed (HTTP 401); check the configured API key",
            404: "model or endpoint was not found (HTTP 404); check the base URL and model",
            429: "gateway capacity limit reached (HTTP 429); retry after capacity is available",
        }
        message = messages.get(exc.code)
        if message is None and 500 <= exc.code < 600:
            message = f"gateway server error (HTTP {exc.code}); retry later or inspect server logs"
        raise ProviderError(message or f"provider request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = exc.reason if hasattr(exc, "reason") else exc
        raise ProviderError(f"provider connection or timeout failure: {reason}") from exc


def api_stream_chat_request(
    url: str, api_key: str, payload: dict[str, Any], timeout: int
) -> dict[str, Any]:
    """Read OpenAI-compatible server-sent chat chunks without logging secrets."""
    payload = {**payload, "stream": True}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    started_at = time.perf_counter()
    first_token_at: float | None = None
    parts: list[str] = []
    usage: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                text = (choices[0].get("delta") or {}).get("content")
                if isinstance(text, str) and text:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    parts.append(text)
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"streaming provider request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = exc.reason if hasattr(exc, "reason") else exc
        raise ProviderError(f"streaming provider connection or timeout failure: {reason}") from exc
    ended_at = time.perf_counter()
    if not parts:
        raise ProviderError("Streaming provider returned no assistant text.")
    completion_tokens = usage.get("completion_tokens")
    generation_seconds = ended_at - first_token_at if first_token_at else None
    elapsed_seconds = ended_at - started_at
    return {
        "choices": [{"message": {"content": "".join(parts)}}],
        "usage": usage,
        "_client_metrics": {
            "endpoint": url,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "time_to_first_token_seconds": round(first_token_at - started_at, 3) if first_token_at else None,
            "generation_seconds": round(generation_seconds, 3) if generation_seconds else None,
            "decode_tps": (
                round(completion_tokens / generation_seconds, 3)
                if isinstance(completion_tokens, int) and generation_seconds and generation_seconds > 0
                else None
            ),
        },
    }


def local_model_ids(base_url: str, api_key: str, timeout: int) -> list[str]:
    response = api_json_request(f"{normalize_base_url(base_url)}/models", api_key, None, timeout)
    return sorted(str(item["id"]) for item in response.get("data", []) if item.get("id"))


def local_api_key() -> str | None:
    return env_value("VLM_LOCAL_API_KEY", "OPENAI_API_KEY")


def validate_local_gateway(args: argparse.Namespace) -> list[str]:
    api_key = local_api_key()
    if not args.local_base_url or not api_key:
        raise ProviderError(
            "Local gateway requires VLM_LOCAL_BASE_URL and VLM_LOCAL_API_KEY "
            "(or OPENAI_BASE_URL and OPENAI_API_KEY)."
        )
    models = local_model_ids(args.local_base_url, api_key, args.request_timeout)
    if args.local_model not in models:
        raise ProviderError(
            f"Configured local model '{args.local_model}' was not returned by /models. "
            f"Available models: {', '.join(models)}"
        )
    print(
        f"Local gateway healthy: endpoint={normalize_base_url(args.local_base_url)} "
        f"model={args.local_model} models={len(models)}"
    )
    return models


def usage_metrics(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage") or {}
    elapsed = response.get("_client_metrics", {}).get("elapsed_seconds")
    completion_tokens = usage.get("completion_tokens")
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": completion_tokens,
        "total_tokens": usage.get("total_tokens"),
        "elapsed_seconds": elapsed,
        "end_to_end_tps": (
            round(completion_tokens / elapsed, 3)
            if isinstance(completion_tokens, int) and elapsed and elapsed > 0
            else None
        ),
    }


def log_provider_metrics(provider: str, model: str, endpoint: str, metrics: dict[str, Any]) -> None:
    print(
        f"VLM request: provider={provider} model={model} endpoint={endpoint} "
        f"elapsed_seconds={metrics['elapsed_seconds']} prompt_tokens={metrics['prompt_tokens']} "
        f"completion_tokens={metrics['completion_tokens']} total_tokens={metrics['total_tokens']} "
        f"end_to_end_tps={metrics['end_to_end_tps']}"
    )


def local_chat_completion(
    args: argparse.Namespace,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 128,
    stream: bool = False,
) -> dict[str, Any]:
    """Send the full caller-provided conversation history to the local gateway."""
    api_key = local_api_key()
    if not api_key or not args.local_base_url or not args.local_model:
        raise ProviderError("A local base URL, API key, and model are required for local chat.")
    endpoint = f"{normalize_base_url(args.local_base_url)}/chat/completions"
    payload = {"model": args.local_model, "messages": messages, "max_tokens": max_tokens}
    response = (
        api_stream_chat_request(endpoint, api_key, payload, args.request_timeout)
        if stream
        else api_json_request(endpoint, api_key, payload, args.request_timeout)
    )
    metrics = usage_metrics(response)
    log_provider_metrics("local", args.local_model, endpoint, metrics)
    return {
        "content": extract_chat_content(response),
        "model": args.local_model,
        "endpoint": endpoint,
        "metrics": {**metrics, **response.get("_client_metrics", {})},
    }


def page_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": sorted(ELEMENT_TYPES)},
                        "text": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["type", "text", "confidence"],
                },
            },
            "unresolved_visuals": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "elements", "unresolved_visuals"],
    }


def vlm_prompt(page_num: int, compact: bool = False) -> str:
    compact_instruction = (
        " Return exactly one compact JSON object: first character {, last character }. "
        "Do not emit reasoning, <think> blocks, Markdown fences, or explanatory prose. "
        "Use at most 12 elements and combine related visual text into complete elements."
        if compact
        else ""
    )
    return (
        f"Extract page {page_num} of a PDF for a knowledge graph. Return only JSON matching "
        "the supplied schema. Preserve meaningful visible text, table facts, chart/diagram meaning, "
        "and image captions. Do not invent unreadable content; put uncertainty in unresolved_visuals."
        + compact_instruction
    )


def local_retry_prompt(page_num: int) -> str:
    return (
        f"Extract page {page_num} of a PDF. Return exactly one valid JSON object, with {{ as "
        "the first character and } as the last. Do not include reasoning, analysis, <think> "
        "blocks, Markdown, or prose. Be concise: preserve the page heading and table facts in "
        "at most 10 complete elements. Use this JSON shape: "
        '{"summary":"...","elements":[{"type":"paragraph","text":"...","confidence":0.9}],'
        '"unresolved_visuals":[]}.'
    )


def extract_chat_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        raise ProviderError("Provider returned no chat completion choices.")
    if choices[0].get("finish_reason") == "length":
        raise ProviderError("Provider output was truncated at the configured VLM token limit.")
    message = choices[0].get("message") or {}
    if message.get("refusal"):
        raise ProviderError("Provider refused the page extraction request.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("Provider returned empty page extraction content.")
    return content.strip()


def parse_vlm_json(content: str) -> dict[str, Any]:
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    decoder = json.JSONDecoder()
    result: dict[str, Any] | None = None
    for index, char in enumerate(content):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("elements"), list):
            result = candidate
            break
    if result is None:
        raise ProviderError("Provider response was not valid JSON.")
    if not isinstance(result, dict) or not isinstance(result.get("elements"), list):
        raise ProviderError("Provider response did not contain an elements array.")
    for item in result["elements"]:
        if not isinstance(item, dict) or item.get("type") not in ELEMENT_TYPES:
            raise ProviderError("Provider response contained an unsupported element type.")
        if not isinstance(item.get("text"), str):
            raise ProviderError("Provider response contained an element without text.")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ProviderError("Provider response contained an invalid element confidence.")
    return result


def render_page(pdf_path: Path, output_dir: Path, page_num: int, dpi: int) -> Path:
    image_path = output_dir / f"{pdf_path.stem}.hybrid_vlm.p{page_num:04d}.png"
    with fitz.open(pdf_path) as document:
        page = document[page_num - 1]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
        pixmap.save(image_path)
    return image_path


def image_data_url(image_path: Path) -> str:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def run_vlm_page(
    provider: str, args: argparse.Namespace, page_num: int, image_path: Path
) -> dict[str, Any]:
    data_url = image_data_url(image_path)
    if provider == "openai":
        api_key = env_value("VLM_OPENAI_API_KEY", "OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("VLM_OPENAI_API_KEY or OPENAI_API_KEY is required for --provider openai.")
        payload = {
            "model": args.openai_model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": vlm_prompt(page_num, compact=True)},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
            ]}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "page_extraction", "strict": True, "schema": page_schema(),
            }},
            "max_completion_tokens": args.vlm_max_tokens,
        }
        response = api_json_request("https://api.openai.com/v1/chat/completions", api_key, payload, args.request_timeout)
        model = args.openai_model
        endpoint = "https://api.openai.com/v1/chat/completions"
    elif provider == "local":
        api_key = local_api_key()
        if not api_key or not args.local_base_url or not args.local_model:
            raise ProviderError("A local base URL, API key, and model are required for --provider local.")
        payload = {
            "model": args.local_model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": vlm_prompt(page_num, compact=True)},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "max_tokens": args.vlm_max_tokens,
        }
        endpoint = f"{normalize_base_url(args.local_base_url)}/chat/completions"
        response = (
            api_stream_chat_request(endpoint, api_key, payload, args.request_timeout)
            if args.stream_local
            else api_json_request(endpoint, api_key, payload, args.request_timeout)
        )
        model = args.local_model
    else:
        raise ProviderError(f"Unsupported provider: {provider}")
    metrics = usage_metrics(response)
    log_provider_metrics(provider, model, endpoint, metrics)
    try:
        result = parse_vlm_json(extract_chat_content(response))
    except ProviderError as first_error:
        if provider != "local" or args.local_vlm_retries < 1:
            raise
        retry_max_tokens = max(args.vlm_max_tokens, args.local_retry_max_tokens)
        print(
            f"Page {page_num}: local VLM response was incomplete or invalid; retrying once "
            f"with a concise JSON request (max_tokens={retry_max_tokens})."
        )
        retry_payload = dict(payload)
        retry_payload["max_tokens"] = retry_max_tokens
        retry_payload["messages"] = [{"role": "user", "content": [
            {"type": "text", "text": local_retry_prompt(page_num)},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}]
        response = (
            api_stream_chat_request(endpoint, api_key, retry_payload, args.request_timeout)
            if args.stream_local
            else api_json_request(endpoint, api_key, retry_payload, args.request_timeout)
        )
        metrics = usage_metrics(response)
        log_provider_metrics(provider, model, endpoint, metrics)
        try:
            result = parse_vlm_json(extract_chat_content(response))
        except ProviderError as retry_error:
            raise ProviderError(
                f"Local VLM returned invalid JSON after retry: {retry_error}"
            ) from first_error
    return {
        "provider": provider,
        "model": model,
        "endpoint": endpoint,
        "metrics": {**metrics, **response.get("_client_metrics", {})},
        "result": result,
    }


def vlm_elements(page_num: int, extraction: dict[str, Any], image_path: Path) -> list[dict[str, Any]]:
    result = extraction["result"]
    elements = []
    for index, item in enumerate(result["elements"], start=1):
        elements.append({
            "element_id": f"p{page_num:04d}_vlm_e{index:04d}",
            "page_num": page_num,
            "type": item["type"],
            "text": item["text"].strip(),
            "bbox": None,
            "source_parser": "vlm",
            "provider": extraction["provider"],
            "model": extraction["model"],
            "endpoint": extraction["endpoint"],
            "request_metrics": extraction["metrics"],
            "confidence": item.get("confidence"),
            "evidence_image_path": str(image_path),
        })
    return elements


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def verify_conversions(plan: dict[str, Any], conversions: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    stats: dict[int, dict[str, int]] = {}
    for conversion in conversions:
        with open(conversion["docling_json"], encoding="utf-8") as handle:
            docling_dict = json.load(handle)
        start, end = conversion["page_range"]
        stats.update(docling_stats_by_page(docling_dict, list(range(start, end + 1))))
    decisions = []
    forced = set(args.force_vlm_page)
    for page in plan["pages"]:
        if page["mode"] == "vlm":
            decisions.append({
                "page_num": page["page_num"],
                "initial_route": "vlm",
                "final_route": "vlm_direct",
                "decision": "fallback_to_vlm",
                "accepted": False,
                "confidence": 0.0,
                "reasons": page["vlm_candidate_reasons"],
                "output_stats": {},
                "manual_review_required": False,
            })
            continue
        decision = verify_page(page, stats.get(page["page_num"], {
            "text_chars": 0, "text_element_count": 0, "table_count": 0, "picture_count": 0,
        }), args)
        if page["page_num"] in forced:
            decision.update({
                "decision": "fallback_to_vlm", "accepted": False, "confidence": 0.0,
                "reasons": ["forced_vlm_page"],
            })
        decisions.append(decision)
    return decisions


def apply_vlm(
    pdf_path: Path, output_dir: Path, decisions: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    pending = [item for item in decisions if item["decision"] == "fallback_to_vlm"]
    allowed = len(pending) if args.max_vlm_pages == 0 else args.max_vlm_pages
    all_elements: list[dict[str, Any]] = []
    for index, decision in enumerate(pending):
        page_num = decision["page_num"]
        if args.provider == "none":
            decision.update({"decision": "manual_review", "final_route": "manual_review", "manual_review_required": True})
            if decision["initial_route"] == "vlm":
                print(f"Page {page_num}: direct VLM route requires a provider; manual review.")
            else:
                print(f"Page {page_num}: verifier requested VLM, but no provider was selected; manual review.")
            continue
        if index >= allowed:
            decision.update({
                "decision": "manual_review", "final_route": "manual_review", "manual_review_required": True,
                "reasons": decision["reasons"] + ["max_vlm_pages_exceeded"],
            })
            print(f"Page {page_num}: VLM limit reached; manual review.")
            continue
        try:
            print(f"Page {page_num}: VLM ({args.provider}) processing...")
            with ActivitySpinner(f"Page {page_num}: VLM ({args.provider}) processing..."):
                image_path = render_page(pdf_path, output_dir, page_num, args.vlm_image_dpi)
                extraction = run_vlm_page(args.provider, args, page_num, image_path)
                elements = vlm_elements(page_num, extraction, image_path)
            if not elements:
                raise ProviderError("VLM produced no elements.")
            all_elements.extend(elements)
            decision.update({
                "decision": "accept_vlm", "final_route": "vlm", "accepted": True,
                "confidence": min(item["confidence"] for item in elements if item["confidence"] is not None),
                "provider": extraction["provider"], "model": extraction["model"],
                "evidence_image_path": str(image_path), "vlm_element_count": len(elements),
            })
            metrics = extraction["metrics"]
            print(
                f"Page {page_num}: VLM completed in {metrics.get('elapsed_seconds')}s "
                f"(tokens={metrics.get('completion_tokens')}, TPS={metrics.get('end_to_end_tps')})."
            )
            print(progress_bar(index + 1, len(pending), "VLM fallback pages complete"))
        except ProviderError as exc:
            decision.update({
                "decision": "manual_review", "final_route": "manual_review", "manual_review_required": True,
                "provider_error": str(exc),
            })
            print(f"Page {page_num}: VLM failed; manual review required ({exc}).")
    return all_elements


def build_quality_report(
    plan: dict[str, Any], conversions: list[dict[str, Any]], decisions: list[dict[str, Any]]
) -> dict[str, Any]:
    manual = [item["page_num"] for item in decisions if item["manual_review_required"]]
    final_routes = {route: sum(item["final_route"] == route for item in decisions) for route in ("native", "ocr", "vlm", "manual_review")}
    return {
        "source_file": plan["source_file"],
        "parser": "docling_hybrid_vlm",
        "requested_page_range": plan["requested_page_range"],
        "page_count": plan["page_count"],
        "final_route_counts": final_routes,
        "manual_review_pages": manual,
        "conversion_count": len(conversions),
        "total_conversion_seconds": round(sum(item["conversion_seconds"] for item in conversions), 2),
        "phase1a_hybrid_vlm_passed": (
            not manual
            and len(decisions) == plan["page_count"]
            and all(item["final_route"] is not None for item in decisions)
        ),
    }


def main() -> None:
    args = parse_args()
    default_env_file = Path(__file__).resolve().parents[2] / ".env"
    load_env_file(args.env_file or default_env_file)
    apply_environment_defaults(args)
    if not args.pdf_path.exists():
        raise FileNotFoundError(args.pdf_path)
    if args.list_local_models:
        key = local_api_key()
        if not args.local_base_url or not key:
            raise ProviderError("--list-local-models requires a local base URL and API key.")
        for model in local_model_ids(args.local_base_url, key, args.request_timeout):
            print(model)
        return
    if args.health_check_local or args.local_chat_smoke_test:
        validate_local_gateway(args)
        if args.local_chat_smoke_test:
            result = local_chat_completion(
                args,
                [
                    {"role": "system", "content": "You are a concise health-check assistant."},
                    {"role": "user", "content": "Reply with exactly: gateway healthy"},
                ],
            )
            print(f"Local chat response: {result['content']}")
        return
    if (
        args.max_vlm_pages < 0
        or args.vlm_image_dpi < 72
        or args.direct_vlm_drawing_threshold < 0
    ):
        raise ValueError(
            "--max-vlm-pages and --direct-vlm-drawing-threshold must be non-negative, "
            "and --vlm-image-dpi must be at least 72."
        )
    if args.provider == "local" and not args.plan_only:
        validate_local_gateway(args)
    output_dir = resolve_output_dir(args.pdf_path, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = build_plan(args.pdf_path, args)
    progress_mode = resolve_progress_mode(plan, args)
    requested_start, requested_end = plan["requested_page_range"]
    invalid_forced_pages = sorted(
        page
        for page in set(args.force_vlm_page)
        if page < requested_start or page > requested_end
    )
    if invalid_forced_pages:
        raise ValueError(
            f"--force-vlm-page must be within the selected page range "
            f"{requested_start}-{requested_end}; invalid pages: {invalid_forced_pages}"
        )
    if args.vlm_only and args.plan_only:
        raise ValueError("--vlm-only cannot be combined with --plan-only.")
    if args.vlm_only and args.provider == "none":
        raise ValueError("--vlm-only requires --provider local or --provider openai.")
    conversions: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    elements: list[dict[str, Any]] = []
    if not args.plan_only:
        if args.vlm_only:
            print(f"VLM-only mode: sending {plan['page_count']} selected pages directly to {args.provider}.")
            decisions = [
                {
                    "page_num": page["page_num"],
                    "initial_route": "vlm_direct",
                    "final_route": "vlm_direct",
                    "decision": "fallback_to_vlm",
                    "accepted": False,
                    "confidence": 0.0,
                    "reasons": ["vlm_only_requested"],
                    "output_stats": {},
                    "manual_review_required": False,
                }
                for page in plan["pages"]
            ]
        else:
            items = execution_items(plan, progress_mode)
            completed_pages = 0
            print(
                f"Processing {plan['page_count']} pages with {progress_mode}-level progress "
                f"(native={plan['native_page_count']}, OCR={plan['ocr_page_count']}, "
                f"direct VLM={plan['direct_vlm_page_count']})."
            )
            for page in plan["pages"]:
                if page["mode"] == "vlm":
                    print(
                        f"Page {page['page_num']}: VLM selected during preflight "
                        f"({', '.join(page['vlm_candidate_reasons'])})."
                    )
            for item in items:
                page_label = (
                    f"Page {item['start_page']}"
                    if item["page_count"] == 1
                    else f"Pages {item['start_page']}-{item['end_page']}"
                )
                print(f"{page_label}: {item['mode'].upper()} processing...")
                with ActivitySpinner(f"{page_label}: {item['mode'].upper()} processing..."):
                    conversion = convert_range(args.pdf_path, output_dir, item, args)
                conversions.append(conversion)
                completed_pages += item["page_count"]
                print(
                    f"{page_label}: {item['mode'].upper()} completed in "
                    f"{conversion['conversion_seconds']}s."
                )
                print(progress_bar(completed_pages, plan["page_count"], f"{page_label} complete"))
            decisions = verify_conversions(plan, conversions, args)
            print("Verification complete.")
            if progress_mode == "page":
                for decision in decisions:
                    if decision["decision"] == "accept":
                        print(
                            f"Page {decision['page_num']}: {decision['initial_route'].upper()} accepted."
                        )
                    elif decision["initial_route"] == "vlm":
                        print(
                            f"Page {decision['page_num']}: direct VLM route retained "
                            f"({', '.join(decision['reasons'])})."
                        )
                    else:
                        print(
                            f"Page {decision['page_num']}: verifier requested VLM "
                            f"({', '.join(decision['reasons'])})."
                        )
        elements = apply_vlm(args.pdf_path, output_dir, decisions, args)
    else:
        for page in plan["pages"]:
            decisions.append({
                "page_num": page["page_num"], "initial_route": page["initial_route"],
                "final_route": None, "decision": "not_run_plan_only", "accepted": False,
                "confidence": None, "reasons": page["vlm_candidate_reasons"], "manual_review_required": False,
            })
    stem = f"{args.pdf_path.stem}.hybrid_vlm"
    manifest = {
        **plan,
        "plan_only": args.plan_only,
        "vlm_only": args.vlm_only,
        "progress_mode": progress_mode,
        "conversions": conversions,
        "verifier_decisions": decisions,
    }
    write_json(output_dir / f"{stem}.manifest.json", manifest)
    write_json(output_dir / f"{stem}.quality_report.json", build_quality_report(plan, conversions, decisions))
    if elements:
        write_jsonl(output_dir / f"{stem}.vlm_elements.jsonl", elements)
    print(f"Written: {output_dir / f'{stem}.manifest.json'}")
    print(f"Written: {output_dir / f'{stem}.quality_report.json'}")
    if elements:
        print(f"Written: {output_dir / f'{stem}.vlm_elements.jsonl'}")


if __name__ == "__main__":
    main()
