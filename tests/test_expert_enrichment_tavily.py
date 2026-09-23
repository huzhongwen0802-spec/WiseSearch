# -*- coding: utf-8 -*-

import unittest
from unittest.mock import patch

from expertsearch.expert_enrichment import _expert_tavily_evidence


class ExpertEnrichmentTavilyTests(unittest.TestCase):
    def test_one_combined_search_is_reused_for_the_same_expert(self):
        row = {
            "专家姓名": "Ada Lovelace",
            "工作单位": "Example University",
            "研究兴趣": "计算机科学",
        }
        cache = {}
        results = [{"title": "Profile", "url": "https://example.com", "content": "bio"}]

        with patch(
            "expertsearch.expert_enrichment._search",
            return_value=results,
        ) as search:
            first = _expert_tavily_evidence(row, "人工智能", object(), cache)
            second = _expert_tavily_evidence(row, "人工智能", object(), cache)

        self.assertEqual(first, results)
        self.assertEqual(second, results)
        search.assert_called_once()
        combined_query = search.call_args.args[1]
        self.assertIn("official homepage", combined_query)
        self.assertIn("education", combined_query)
        self.assertIn("awards", combined_query)
        self.assertIn("China collaboration", combined_query)


if __name__ == "__main__":
    unittest.main()
