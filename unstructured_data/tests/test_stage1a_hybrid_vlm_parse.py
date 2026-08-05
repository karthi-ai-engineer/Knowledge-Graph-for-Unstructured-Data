from __future__ import annotations

import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import stage1a_hybrid_vlm_parse as hybrid_vlm


class HybridVlmTests(unittest.TestCase):
    def make_args(self, **overrides):
        values = {
            "low_text_threshold": 30,
            "low_image_coverage_threshold": 0.03,
            "mixed_text_threshold": 150,
            "image_coverage_threshold": 0.5,
            "vlm_image_coverage_threshold": 0.5,
            "direct_vlm_drawing_threshold": 200,
            "verifier_min_visual_text_chars": 80,
            "force_vlm_page": [],
            "provider": "openai",
            "openai_model": "gpt-5-mini",
            "local_base_url": "http://local.example/v1",
            "local_model": "Qwen3-VL-8B-Instruct",
            "request_timeout": 5,
            "vlm_max_tokens": 2600,
            "local_retry_max_tokens": 5000,
            "local_vlm_retries": 1,
            "stream_local": False,
            "progress_mode": "auto",
            "page_progress_limit": 30,
        }
        values.update(overrides)
        return type("Args", (), values)()

    def test_verifier_routes_sparse_visual_page_to_vlm(self):
        page = {
            "page_num": 9,
            "initial_route": "native",
            "mode": "native",
            "char_count": 20,
            "vlm_candidate": True,
        }
        decision = hybrid_vlm.verify_page(
            page,
            {"text_chars": 12, "text_element_count": 1, "table_count": 0, "picture_count": 1},
            self.make_args(),
        )
        self.assertEqual(decision["decision"], "fallback_to_vlm")
        self.assertIn("visual_risk_with_sparse_extraction", decision["reasons"])

    def test_verifier_accepts_adequate_native_page(self):
        page = {
            "page_num": 10,
            "initial_route": "native",
            "mode": "native",
            "char_count": 400,
            "vlm_candidate": False,
        }
        decision = hybrid_vlm.verify_page(
            page,
            {"text_chars": 400, "text_element_count": 8, "table_count": 0, "picture_count": 0},
            self.make_args(),
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["final_route"], "native")

    def test_vector_only_page_bypasses_docling_and_routes_directly_to_vlm(self):
        page = Mock()
        page.number = 6
        page.get_text.return_value = ""
        page.get_images.return_value = []
        page.get_drawings.return_value = [{}] * 250
        classified = hybrid_vlm.classify_vlm_page(page, self.make_args())
        self.assertEqual(classified["mode"], "vlm")
        self.assertEqual(classified["initial_route"], "vlm")
        self.assertIn("vector_only_no_native_text", classified["vlm_candidate_reasons"])

    def test_direct_vlm_pages_are_excluded_from_docling_execution(self):
        plan = {
            "pages": [
                {"page_num": 1, "mode": "ocr"},
                {"page_num": 2, "mode": "vlm"},
                {"page_num": 3, "mode": "native"},
            ],
            "ranges": [
                {"mode": "ocr", "start_page": 1, "end_page": 1, "page_count": 1},
                {"mode": "vlm", "start_page": 2, "end_page": 2, "page_count": 1},
                {"mode": "native", "start_page": 3, "end_page": 3, "page_count": 1},
            ],
        }
        self.assertEqual(
            [item["mode"] for item in hybrid_vlm.execution_items(plan, "range")],
            ["ocr", "native"],
        )

    def test_base_url_has_exactly_one_v1_path(self):
        self.assertEqual(
            hybrid_vlm.normalize_base_url("http://gateway:7080/v1"),
            "http://gateway:7080/v1",
        )

    def test_auto_progress_mode_uses_page_mode_for_short_runs(self):
        args = self.make_args()
        self.assertEqual(
            hybrid_vlm.resolve_progress_mode({"page_count": 30}, args),
            "page",
        )
        self.assertEqual(
            hybrid_vlm.resolve_progress_mode({"page_count": 31}, args),
            "range",
        )
        self.assertEqual(
            hybrid_vlm.progress_bar(15, 30, "Page 15 complete"),
            "[##############--------------]  50.0% (15/30) Page 15 complete",
        )

    def test_vlm_only_execution_uses_every_selected_page(self):
        plan = {
            "pages": [
                {"page_num": 6, "mode": "native"},
                {"page_num": 7, "mode": "ocr"},
            ],
            "ranges": [{"mode": "native", "start_page": 6, "end_page": 7, "page_count": 2}],
        }
        items = hybrid_vlm.execution_items(plan, "page")
        self.assertEqual([(item["start_page"], item["mode"]) for item in items], [(6, "native"), (7, "ocr")])
        self.assertEqual(
            hybrid_vlm.normalize_base_url("http://gateway:7080/v1/v1/"),
            "http://gateway:7080/v1",
        )

    def test_standard_openai_environment_variables_configure_local_gateway(self):
        args = self.make_args(local_base_url=None, local_model=None, openai_model=None)
        with patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": "http://gateway:7080/v1", "OPENAI_MODEL": "qwen3"},
            clear=True,
        ):
            hybrid_vlm.apply_environment_defaults(args)
        self.assertEqual(args.local_base_url, "http://gateway:7080/v1")
        self.assertEqual(args.local_model, "qwen3")
        self.assertGreaterEqual(args.request_timeout, 120)

    def test_local_health_check_validates_configured_model(self):
        args = self.make_args(local_base_url="http://gateway:7080/v1", local_model="qwen3")
        with patch.dict(os.environ, {"VLM_LOCAL_API_KEY": "test-key"}, clear=True):
            with patch.object(hybrid_vlm, "local_model_ids", return_value=["qwen3", "other"]):
                models = hybrid_vlm.validate_local_gateway(args)
        self.assertEqual(models, ["qwen3", "other"])

    def test_local_chat_preserves_messages_and_records_usage(self):
        args = self.make_args(local_base_url="http://gateway:7080/v1", local_model="qwen3")
        response = {
            "choices": [{"message": {"content": "gateway healthy"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            "_client_metrics": {"elapsed_seconds": 0.5},
        }
        messages = [
            {"role": "system", "content": "System context"},
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "Prior response"},
            {"role": "user", "content": "Latest message"},
        ]
        with patch.dict(os.environ, {"VLM_LOCAL_API_KEY": "test-key"}, clear=True):
            with patch.object(hybrid_vlm, "api_json_request", return_value=response) as request:
                result = hybrid_vlm.local_chat_completion(args, messages)
        self.assertEqual(result["content"], "gateway healthy")
        self.assertEqual(result["metrics"]["end_to_end_tps"], 4.0)
        self.assertEqual(request.call_args.args[0], "http://gateway:7080/v1/chat/completions")
        self.assertEqual(request.call_args.args[2]["messages"], messages)

    def test_local_vlm_payload_uses_only_portable_chat_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.png"
            image_path.write_bytes(b"not-a-real-png")
            response = {
                "choices": [{"message": {"content": """
                    {"summary":"Page.","elements":[{"type":"paragraph","text":"Text.","confidence":0.9}],"unresolved_visuals":[]}
                """}}]
            }
            args = self.make_args(local_base_url="http://gateway:7080/v1", local_model="qwen3")
            with patch.dict(os.environ, {"VLM_LOCAL_API_KEY": "test-key"}, clear=True):
                with patch.object(hybrid_vlm, "api_json_request", return_value=response) as request:
                    hybrid_vlm.run_vlm_page("local", args, 5, image_path)
            payload = request.call_args.args[2]
            self.assertEqual(request.call_args.args[0], "http://gateway:7080/v1/chat/completions")
            self.assertNotIn("response_format", payload)
            self.assertEqual(payload["model"], "qwen3")

    def test_local_vlm_retries_invalid_json_once(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.png"
            image_path.write_bytes(b"not-a-real-png")
            invalid_response = {"choices": [{"message": {"content": "<think>unfinished"}}]}
            valid_response = {
                "choices": [{"message": {"content": """
                    {"summary":"Page.","elements":[{"type":"paragraph","text":"Text.","confidence":0.9}],"unresolved_visuals":[]}
                """}}]
            }
            args = self.make_args(local_base_url="http://gateway:7080/v1", local_model="qwen3")
            with patch.dict(os.environ, {"VLM_LOCAL_API_KEY": "test-key"}, clear=True):
                with patch.object(
                    hybrid_vlm,
                    "api_json_request",
                    side_effect=[invalid_response, valid_response],
                ) as request:
                    result = hybrid_vlm.run_vlm_page("local", args, 5, image_path)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(result["result"]["summary"], "Page.")
            self.assertEqual(request.call_args.args[2]["max_tokens"], 5000)

    def test_local_vlm_retries_empty_content_once(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.png"
            image_path.write_bytes(b"not-a-real-png")
            empty_response = {"choices": [{"message": {"content": ""}}]}
            valid_response = {
                "choices": [{"message": {"content": """
                    {"summary":"Page.","elements":[{"type":"paragraph","text":"Text.","confidence":0.9}],"unresolved_visuals":[]}
                """}}]
            }
            args = self.make_args(local_base_url="http://gateway:7080/v1", local_model="qwen3")
            with patch.dict(os.environ, {"VLM_LOCAL_API_KEY": "test-key"}, clear=True):
                with patch.object(
                    hybrid_vlm,
                    "api_json_request",
                    side_effect=[empty_response, valid_response],
                ) as request:
                    result = hybrid_vlm.run_vlm_page("local", args, 5, image_path)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args.args[2]["max_tokens"], 5000)
            self.assertEqual(result["result"]["summary"], "Page.")

    def test_authentication_error_is_sanitized(self):
        error = HTTPError("http://gateway", 401, "Unauthorized", {}, BytesIO(b"secret body"))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(hybrid_vlm.ProviderError, "authentication failed"):
                hybrid_vlm.api_json_request("http://gateway/v1/models", "test-key", None, 120)

    def test_openai_payload_requests_strict_json_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.png"
            image_path.write_bytes(b"not-a-real-png")
            response = {
                "choices": [{"message": {"content": """
                    {"summary":"A diagram.","elements":[{"type":"chart_description","text":"Trend rises.","confidence":0.9}],"unresolved_visuals":[]}
                """}}]
            }
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
                with patch.object(hybrid_vlm, "api_json_request", return_value=response) as request:
                    extraction = hybrid_vlm.run_vlm_page("openai", self.make_args(), 5, image_path)
            payload = request.call_args.args[2]
            self.assertEqual(payload["model"], "gpt-5-mini")
            self.assertEqual(payload["response_format"]["type"], "json_schema")
            self.assertTrue(payload["response_format"]["json_schema"]["strict"])
            self.assertEqual(extraction["result"]["elements"][0]["type"], "chart_description")

    def test_invalid_provider_json_is_rejected(self):
        with self.assertRaisesRegex(hybrid_vlm.ProviderError, "valid JSON"):
            hybrid_vlm.parse_vlm_json("not json")

    def test_truncated_completion_is_reported_clearly(self):
        with self.assertRaisesRegex(hybrid_vlm.ProviderError, "truncated"):
            hybrid_vlm.extract_chat_content(
                {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
            )

    def test_json_is_recovered_after_model_reasoning_text(self):
        result = hybrid_vlm.parse_vlm_json(
            "<think>brief internal reasoning</think>\n"
            '{"summary":"Page.","elements":[{"type":"paragraph","text":"Text.","confidence":0.9}],"unresolved_visuals":[]}'
        )
        self.assertEqual(result["summary"], "Page.")


if __name__ == "__main__":
    unittest.main()
