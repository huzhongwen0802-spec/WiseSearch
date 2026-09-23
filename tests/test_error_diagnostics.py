# -*- coding: utf-8 -*-

import unittest

from expertsearch.error_diagnostics import (
    checkpoint_error_updates,
    diagnose_error,
    format_error_for_user,
)


class ErrorDiagnosticsTests(unittest.TestCase):
    def test_legacy_generic_provider_list_uses_stage_to_identify_llm(self):
        diagnostic = diagnose_error(
            "外部服务连接失败或超时。常见来源包括 LLM/API、Tavily、OpenAlex。"
            "定位信息: 研究员节点第1批：Connection error."
        )

        self.assertEqual(diagnostic["错误来源"], "LLM 中转服务")
        self.assertIn("稳定连接", diagnostic["失败原因"])

    def test_tavily_rate_limit_is_reported_explicitly(self):
        diagnostic = diagnose_error("Tavily request failed: 429 too many requests")

        self.assertEqual(diagnostic["错误来源"], "Tavily 搜索服务")
        self.assertIn("调用频率", diagnostic["失败原因"])

    def test_homepage_forbidden_explains_automatic_fallback(self):
        diagnostic = diagnose_error("HTTP 主页访问失败: 403 Forbidden")

        self.assertEqual(diagnostic["错误来源"], "专家主页网站")
        self.assertIn("反爬虫", diagnostic["失败原因"])
        self.assertIn("OpenCLI", diagnostic["建议操作"])

    def test_missing_key_is_configuration_error(self):
        diagnostic = diagnose_error("TAVILY_API_KEY is not configured")

        self.assertEqual(diagnostic["错误来源"], "Tavily 搜索服务")
        self.assertIn("缺少", diagnostic["失败原因"])

    def test_technical_detail_redacts_secrets(self):
        formatted = format_error_for_user(
            "Authorization: Bearer sk-example-secret-value token=private-token"
        )

        self.assertNotIn("sk-example-secret-value", formatted)
        self.assertNotIn("private-token", formatted)
        self.assertIn("***", formatted)

    def test_checkpoint_fields_are_stable(self):
        updates = checkpoint_error_updates(
            "验证节点：LLM/API 中转服务连接失败或超时: Connection error."
        )

        self.assertEqual(updates["error_source"], "LLM 中转服务")
        self.assertTrue(updates["error_reason"])
        self.assertTrue(updates["error_action"])
        self.assertTrue(updates["error_detail"])


if __name__ == "__main__":
    unittest.main()
