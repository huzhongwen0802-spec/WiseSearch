# -*- coding: utf-8 -*-

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from expertsearch.search_checkpoint import create_search_checkpoint, load_search_checkpoint
from expertsearch.search_worker import _status_for, run_search_job


class SearchWorkerTests(unittest.TestCase):
    def test_failed_status_contains_structured_diagnostic(self):
        status = _status_for(
            "人工智能",
            actual=4,
            target=10,
            successful_attempts=1,
            failed_attempts=1,
            errors=["研究员节点：LLM/API 中转服务连接失败或超时: Connection error."],
        )

        self.assertEqual(status["状态"], "部分成功")
        self.assertEqual(status["错误来源"], "LLM 中转服务")
        self.assertIn("稳定连接", status["失败原因"])
        self.assertTrue(status["建议操作"])
        self.assertIn("研究员节点", status["技术详情"])

    def test_worker_completes_from_checkpoint_without_streamlit_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint_dir = root / "checkpoints"
            final_dir = root / "results"
            with patch.dict(
                os.environ,
                {
                    "SEARCH_CHECKPOINT_DIR": str(checkpoint_dir),
                    "SUBDOMAIN_TIER_RECOVERY_ATTEMPTS": "0",
                    "SUBDOMAIN_FINAL_TOPUP_ATTEMPTS": "0",
                },
            ):
                checkpoint = create_search_checkpoint(
                    {
                        "main_domain": "农业",
                        "requested_subdomains": ["育种"],
                        "run_subdomains": ["育种"],
                        "requested_total_experts": 1,
                        "subdomain_targets": {"育种": 1},
                        "subdomain_statuses": [
                            {
                                "细分领域": "育种",
                                "状态": "待重试",
                                "实际人数": 0,
                                "目标人数": 1,
                            }
                        ],
                        "failed_subdomains": ["育种"],
                        "experts_per_round": 10,
                        "final_output_dir": str(final_dir),
                    }
                )

                def fake_agent(*args, **kwargs):
                    batch_path = Path(kwargs["output_dir"]) / "batch.xlsx"
                    pd.DataFrame(
                        {
                            "专家姓名": ["Test Expert"],
                            "细分领域": ["育种"],
                            "评价_TotalScore": [1.0],
                        }
                    ).to_excel(batch_path, index=False)
                    return str(batch_path), None

                def fake_write(frame, path, **kwargs):
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    frame.to_excel(path, index=False)

                with patch(
                    "expertsearch.search_worker.run_agent_task",
                    side_effect=fake_agent,
                ), patch(
                    "expertsearch.search_worker.finalize_merged_experts",
                    side_effect=lambda frame, **kwargs: frame,
                ), patch(
                    "expertsearch.search_worker.write_expert_excel",
                    side_effect=fake_write,
                ):
                    result = run_search_job(checkpoint["job_id"])

                persisted = load_search_checkpoint(checkpoint["job_id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(persisted["expert_count"], 1)
                self.assertEqual(persisted["failed_subdomains"], [])
                self.assertTrue(Path(persisted["final_file_path"]).exists())


if __name__ == "__main__":
    unittest.main()
