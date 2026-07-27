# -*- coding: utf-8 -*-

import os
import unittest
from unittest.mock import patch

import pandas as pd

import agents
import utils


EXPERT_TABLE = """| 专家姓名 | 国籍 | 个人主页 | 邮箱/电话 | 研究兴趣 | 工作单位 | 职位 | 工作经历 | 教育背景 | H指数 | 主要成果 | 国内合作学者与单位 | 入选依据 | 领域关联依据 | 生存状态 | 信息来源 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Test Expert | 美国 | 暂无公开信息 | 暂无公开信息 | 人工智能 | Test University | 教授 | 暂无公开信息 | 暂无公开信息 | 50 | 机器学习成果 | 暂无公开信息 | 高被引学者 | 人工智能研究 | 在世 | https://example.edu |
"""


class DemoFastModeTests(unittest.TestCase):
    def test_validator_skips_llm(self):
        with patch.dict(os.environ, {"EXPERTSEARCH_DEMO_FAST_MODE": "true"}):
            with patch.object(
                agents,
                "invoke_llm_with_stage",
                side_effect=AssertionError("demo validator must not call LLM"),
            ):
                result = agents.validator_node({"research_data": EXPERT_TABLE})

        self.assertTrue(result["validation_feedback"].startswith("PYTHON_VALIDATION_PASSED"))

    def test_final_enrichment_skips_exhaustive_tools(self):
        df = pd.DataFrame(
            [{"专家姓名": "Test Expert", "工作单位": "Test University"}]
        )
        with patch.dict(os.environ, {"EXPERTSEARCH_DEMO_FAST_MODE": "true"}):
            with patch.object(
                utils,
                "enrich_expert_details",
                side_effect=AssertionError("demo final enrichment must not run"),
            ):
                result = utils.selectively_enrich_final_experts(df, "计算机科学")

        self.assertEqual(len(result), 1)

    def test_ranking_skips_per_expert_network_enrichment(self):
        df = pd.DataFrame(
            [
                {
                    "专家姓名": "Test Expert",
                    "工作单位": "Test University",
                    "职位": "教授",
                    "研究兴趣": "人工智能",
                    "主要成果": "机器学习成果",
                    "H指数": "50",
                    "个人主页": "暂无公开信息",
                }
            ]
        )
        blocked = AssertionError("demo ranking must not run per-expert network tools")
        with patch.dict(os.environ, {"EXPERTSEARCH_DEMO_FAST_MODE": "true"}):
            with patch.object(utils, "enrich_openalex_metrics", side_effect=blocked):
                with patch.object(utils, "enrich_semantic_scholar_metrics", side_effect=blocked):
                    with patch.object(utils, "enrich_expert_details", side_effect=blocked):
                        with patch.object(utils, "verify_survival_status", side_effect=blocked):
                            result = utils.add_ranking_metrics(
                                df,
                                "计算机科学领域下的人工智能方向顶级专家信息",
                                include_chinese_experts=True,
                            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["评价_H指数"], 50.0)


if __name__ == "__main__":
    unittest.main()
