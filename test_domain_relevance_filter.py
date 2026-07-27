# -*- coding: utf-8 -*-

import unittest

import pandas as pd

from utils import filter_obvious_domain_mismatches


class DomainRelevanceFilterTests(unittest.TestCase):
    def test_removes_obvious_psychiatry_expert_from_agriculture_query(self):
        df = pd.DataFrame(
            [
                {
                    "专家姓名": "Psychiatry Expert",
                    "研究兴趣": "精神分裂症、精神病学和临床精神卫生",
                    "主要成果": "长期研究 schizophrenia 与 psychosis",
                    "学科领域": "农业",
                    "细分领域": "现代农业信息技术",
                }
            ]
        )

        result = filter_obvious_domain_mismatches(
            df,
            "农业领域下的现代农业信息技术方向顶级专家信息",
        )

        self.assertTrue(result.empty)

    def test_keeps_agriculture_expert(self):
        df = pd.DataFrame(
            [
                {
                    "专家姓名": "Agriculture Expert",
                    "研究兴趣": "作物遗传育种与种质资源",
                    "主要成果": "改良水稻品种并开展精准农业研究",
                }
            ]
        )

        result = filter_obvious_domain_mismatches(
            df,
            "农业领域下的作物遗传育种与种质资源方向顶级专家信息",
        )

        self.assertEqual(len(result), 1)

    def test_keeps_cross_disciplinary_agriculture_expert(self):
        df = pd.DataFrame(
            [
                {
                    "专家姓名": "Cross-domain Expert",
                    "研究兴趣": "计算机视觉、作物病害识别与智慧农业",
                    "主要成果": "使用机器学习提高农业生产效率",
                }
            ]
        )

        result = filter_obvious_domain_mismatches(
            df,
            "农业领域下的现代农业信息技术方向顶级专家信息",
        )

        self.assertEqual(len(result), 1)

    def test_unknown_domain_does_not_apply_profile_filter(self):
        df = pd.DataFrame(
            [{"专家姓名": "Expert", "研究兴趣": "精神病学"}]
        )

        result = filter_obvious_domain_mismatches(df, "新兴交叉领域")

        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
