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

    def test_ranking_uses_openalex_and_limited_enrichment(self):
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
        openalex_result = df.assign(
            OpenAlex匹配姓名="Test Expert",
            OpenAlex作者ID="https://openalex.org/A1",
            OpenAlex主题="Artificial Intelligence",
            i10指数=40,
            总被引次数=5000,
        )
        with patch.dict(os.environ, {"EXPERTSEARCH_DEMO_FAST_MODE": "true"}):
            with patch.object(
                utils,
                "enrich_openalex_metrics",
                return_value=openalex_result,
            ) as openalex_mock:
                with patch.object(
                    utils,
                    "enrich_expert_details",
                    side_effect=lambda value, query: value,
                ) as enrichment_mock:
                    with patch.object(
                        utils,
                        "verify_survival_status",
                        side_effect=lambda value, query: value,
                    ) as survival_mock:
                        with patch.object(
                            utils,
                            "enrich_semantic_scholar_metrics",
                            side_effect=AssertionError(
                                "demo quality mode must not run Semantic Scholar"
                            ),
                        ):
                            result = utils.add_ranking_metrics(
                                df,
                                "计算机科学领域下的人工智能方向顶级专家信息",
                                include_chinese_experts=True,
                            )

        openalex_mock.assert_called_once()
        enrichment_mock.assert_called_once()
        survival_mock.assert_called_once()
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["评价_H指数"], 50.0)
        self.assertEqual(result.iloc[0]["评价_总被引次数"], 5000)

    def test_citation_parser_does_not_treat_year_as_citations(self):
        row = pd.Series(
            {"主要成果": "代表作《Example》(2009, 引用超2万次)"}
        )

        self.assertEqual(utils.estimate_citations(row), 20000)


if __name__ == "__main__":
    unittest.main()
