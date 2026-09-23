# -*- coding: utf-8 -*-

import unittest

from expertsearch.batch_execution import (
    allocate_subdomain_workload,
    is_llm_connection_failure,
    merge_subdomain_statuses,
    retry_attempt_count,
    retryable_subdomains,
)


class BatchExecutionTests(unittest.TestCase):
    def test_workload_distributes_total_exactly_and_rounds_up(self):
        workload = allocate_subdomain_workload(["方向一", "方向二", "方向三"], 31, 10)
        self.assertEqual([item["目标人数"] for item in workload], [11, 10, 10])
        self.assertEqual([item["查询轮次"] for item in workload], [2, 1, 1])
        self.assertEqual(sum(item["目标人数"] for item in workload), 31)

    def test_workload_requires_at_least_one_expert_per_subdomain(self):
        with self.assertRaises(ValueError):
            allocate_subdomain_workload(["方向一", "方向二", "方向三"], 2, 10)

    def test_workload_deduplicates_subdomains_without_changing_order(self):
        workload = allocate_subdomain_workload(["方向一", "方向一", "方向二"], 20, 10)
        self.assertEqual([item["细分领域"] for item in workload], ["方向一", "方向二"])
        self.assertEqual([item["目标人数"] for item in workload], [10, 10])

    def test_llm_connection_failure_requires_llm_and_connection_markers(self):
        self.assertTrue(
            is_llm_connection_failure(
                "研究员节点：LLM/API 中转服务连接失败或超时: Connection error."
            )
        )
        self.assertFalse(is_llm_connection_failure("Tavily 检索全部失败"))

    def test_retryable_subdomains_preserve_original_order(self):
        statuses = [
            {"细分领域": "方向一", "状态": "成功"},
            {"细分领域": "方向二", "状态": "失败"},
            {"细分领域": "方向三", "状态": "部分成功"},
            {"细分领域": "方向四", "状态": "待重试"},
        ]
        self.assertEqual(retryable_subdomains(statuses), ["方向二", "方向三", "方向四"])

    def test_current_retry_status_overrides_previous_status(self):
        merged = merge_subdomain_statuses(
            ["方向一", "方向二"],
            [
                {"细分领域": "方向一", "状态": "成功"},
                {"细分领域": "方向二", "状态": "失败"},
            ],
            [{"细分领域": "方向二", "状态": "成功", "实际人数": 15}],
        )
        self.assertEqual([item["状态"] for item in merged], ["成功", "成功"])
        self.assertEqual(merged[1]["实际人数"], 15)

    def test_retry_attempt_count_supports_at_least_three_consecutive_retries(self):
        task_result = {}
        observed = []
        for _ in range(4):
            next_count = retry_attempt_count(task_result) + 1
            task_result["retry_attempt_count"] = next_count
            observed.append(retry_attempt_count(task_result))
        self.assertEqual(observed, [1, 2, 3, 4])

    def test_retry_attempt_count_handles_legacy_or_invalid_state(self):
        self.assertEqual(retry_attempt_count(None), 0)
        self.assertEqual(retry_attempt_count({"retry_attempt_count": "2"}), 2)
        self.assertEqual(retry_attempt_count({"retry_attempt_count": "invalid"}), 0)


if __name__ == "__main__":
    unittest.main()
