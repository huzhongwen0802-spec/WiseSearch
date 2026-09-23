# -*- coding: utf-8 -*-

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from expertsearch.agents import tavily_search_context
from expertsearch.tavily_client import (
    TavilyBudgetExceeded,
    configure_tavily_budget,
    create_tavily_search,
    current_tavily_configuration,
    estimate_tavily_credit_plan,
    invoke_tavily_search,
    tavily_budget_status,
)


class TavilyClientTests(unittest.TestCase):
    def setUp(self):
        configure_tavily_budget(1000)

    def test_search_does_not_add_a_hard_timeout(self):
        response = Mock(status_code=200)
        response.json.return_value = {"results": []}
        with patch.dict(
            os.environ,
            {
                "TAVILY_API_KEY": "test-key",
                "EXPERTSEARCH_PREFER_PROCESS_TAVILY_KEY": "true",
            },
        ), patch("langchain_tavily._utilities.requests.post", return_value=response) as post:
            search = create_tavily_search(max_results=2, search_depth="advanced")
            search.invoke({"query": "test query"})

        self.assertNotIn("timeout", post.call_args.kwargs)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_project_dotenv_key_wins_by_default(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "stale-process-key"}, clear=False), patch(
            "expertsearch.tavily_client._project_tavily_values",
            return_value={"TAVILY_API_KEY": "fresh-dotenv-key"},
        ):
            os.environ.pop("EXPERTSEARCH_PREFER_PROCESS_TAVILY_KEY", None)
            api_key, _, source = current_tavily_configuration()

        self.assertEqual(api_key, "fresh-dotenv-key")
        self.assertEqual(source, "项目.env")

    def test_invocation_requests_usage_metadata(self):
        response = Mock(
            status_code=200,
        )
        response.json.return_value = {
            "request_id": "request-123",
            "results": [{"title": "Example", "url": "https://example.com"}],
            "usage": {"credits": 1},
        }
        with patch.dict(
            os.environ,
            {
                "TAVILY_API_KEY": "test-key",
                "EXPERTSEARCH_PREFER_PROCESS_TAVILY_KEY": "true",
            },
        ), patch("langchain_tavily._utilities.requests.post", return_value=response) as post:
            search = create_tavily_search(max_results=2, search_depth="advanced")
            result = invoke_tavily_search(search, "test query", stage="测试")

        self.assertEqual(result["request_id"], "request-123")
        self.assertTrue(post.call_args.kwargs["json"]["include_usage"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_api_error_dict_is_not_reported_as_success(self):
        search = Mock()
        search.api_wrapper.tavily_api_key.get_secret_value.return_value = "test-key"
        search.invoke.return_value = {"error": ValueError("Error 401: invalid api key")}

        with self.assertRaisesRegex(RuntimeError, "Tavily API 调用失败"):
            invoke_tavily_search(search, "test query", stage="测试")

    def test_two_hundred_experts_receive_one_thousand_credit_budget(self):
        with patch.dict(os.environ, {"TAVILY_CREDITS_PER_EXPERT": "5"}, clear=False):
            status = configure_tavily_budget(200)

        self.assertEqual(status["limit_credits"], 1000)
        self.assertEqual(status["used_credits"], 0)

    def test_two_hundred_expert_normal_path_is_below_budget(self):
        estimate = estimate_tavily_credit_plan(200)

        self.assertEqual(estimate["candidate_batches"], 20)
        self.assertEqual(estimate["candidate_requests"], 40)
        self.assertEqual(estimate["enrichment_requests"], 200)
        self.assertEqual(estimate["survival_requests"], 200)
        self.assertEqual(estimate["expected_requests"], 440)
        self.assertEqual(estimate["candidate_credits"], 80)
        self.assertEqual(estimate["enrichment_credits"], 400)
        self.assertEqual(estimate["survival_credits"], 400)
        self.assertEqual(estimate["expected_credits"], 880)

    def test_budget_blocks_requests_before_exceeding_limit(self):
        response = Mock(status_code=200)
        response.json.side_effect = [
            {
                "request_id": "request-1",
                "results": [{"title": "One", "url": "https://example.com/1"}],
                "usage": {"credits": 2},
            },
            {
                "request_id": "request-2",
                "results": [{"title": "Two", "url": "https://example.com/2"}],
                "usage": {"credits": 2},
            },
        ]
        with patch.dict(
            os.environ,
            {
                "TAVILY_API_KEY": "test-key",
                "EXPERTSEARCH_PREFER_PROCESS_TAVILY_KEY": "true",
                "TAVILY_CREDITS_PER_EXPERT": "2",
            },
        ), patch("langchain_tavily._utilities.requests.post", return_value=response) as post:
            configure_tavily_budget(1)
            search = create_tavily_search(max_results=2, search_depth="advanced")
            invoke_tavily_search(search, "first query", stage="测试")
            with self.assertRaises(TavilyBudgetExceeded):
                invoke_tavily_search(search, "second query", stage="测试")

        self.assertEqual(post.call_count, 1)
        self.assertEqual(tavily_budget_status()["used_credits"], 2)

    def test_identical_query_uses_cached_result_without_new_credit(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "request_id": "request-cache",
            "results": [{"title": "One", "url": "https://example.com/1"}],
            "usage": {"credits": 2},
        }
        with patch.dict(
            os.environ,
            {
                "TAVILY_API_KEY": "test-key",
                "EXPERTSEARCH_PREFER_PROCESS_TAVILY_KEY": "true",
            },
        ), patch("langchain_tavily._utilities.requests.post", return_value=response) as post:
            configure_tavily_budget(10)
            search = create_tavily_search(max_results=2, search_depth="advanced")
            invoke_tavily_search(search, "same query", stage="测试")
            invoke_tavily_search(search, "same query", stage="测试")

        self.assertEqual(post.call_count, 1)
        self.assertEqual(tavily_budget_status()["used_credits"], 2)

    def test_failed_request_keeps_conservative_budget_reservation(self):
        search = Mock()
        search.search_depth = "advanced"
        search.max_results = 2
        search.topic = "general"
        search.api_wrapper.tavily_api_key.get_secret_value.return_value = "test-key"
        search.invoke.side_effect = ConnectionError("temporary connection failure")

        with patch.dict(os.environ, {"TAVILY_CREDITS_PER_EXPERT": "2"}, clear=False):
            configure_tavily_budget(1)
            with self.assertRaises(ConnectionError):
                invoke_tavily_search(search, "failed query", stage="测试")

        status = tavily_budget_status()
        self.assertEqual(status["used_credits"], 2)
        self.assertEqual(status["requests"], 1)

    def test_persisted_budget_is_restored_after_worker_restart(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "request_id": "request-resume",
            "results": [{"title": "One", "url": "https://example.com/1"}],
            "usage": {"credits": 2},
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "TAVILY_API_KEY": "test-key",
                "EXPERTSEARCH_PREFER_PROCESS_TAVILY_KEY": "true",
            },
        ), patch("langchain_tavily._utilities.requests.post", return_value=response):
            usage_path = Path(temp_dir) / "tavily_usage.json"
            configure_tavily_budget(200, usage_path=usage_path)
            search = create_tavily_search(max_results=2, search_depth="advanced")
            invoke_tavily_search(search, "durable query", stage="测试")
            restored = configure_tavily_budget(200, usage_path=usage_path)

        self.assertEqual(restored["used_credits"], 2)
        self.assertEqual(restored["requests"], 1)

    def test_researcher_degrades_when_tavily_is_unavailable(self):
        with patch(
            "expertsearch.agents.create_tavily_search",
            side_effect=RuntimeError("temporary Tavily failure"),
        ):
            context = tavily_search_context("人工智能")

        self.assertEqual(context, "")


if __name__ == "__main__":
    unittest.main()
