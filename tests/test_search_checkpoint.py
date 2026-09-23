# -*- coding: utf-8 -*-

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from expertsearch.search_checkpoint import (
    checkpoint_task_result,
    create_search_checkpoint,
    end_and_clear_search_job,
    load_latest_recoverable_checkpoint,
    load_latest_search_checkpoint,
    load_search_checkpoint,
    mark_search_checkpoint,
    read_search_worker_pid,
    record_batch_file,
    register_search_job,
    search_job_is_running,
    stop_search_job_preserving_progress,
    unregister_search_job,
    write_search_worker_pid,
)


class SearchCheckpointTests(unittest.TestCase):
    def test_stop_running_job_preserves_checkpoint_and_batch_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ):
            checkpoint = create_search_checkpoint(
                {
                    "main_domain": "农业",
                    "requested_subdomains": ["育种", "植保"],
                    "subdomain_statuses": [
                        {"细分领域": "育种", "状态": "成功", "实际人数": 10},
                        {"细分领域": "植保", "状态": "待重试", "实际人数": 0},
                    ],
                    "failed_subdomains": ["植保"],
                    "worker_started_at": 123.0,
                }
            )
            batch_path = Path(checkpoint["batch_output_dir"]) / "batch.xlsx"
            batch_path.write_bytes(b"batch")
            checkpoint = record_batch_file(
                checkpoint,
                str(batch_path),
                current_subdomain="植保",
                current_round=1,
            )
            write_search_worker_pid(checkpoint["job_id"], 43210)
            with patch(
                "expertsearch.search_checkpoint.process_is_running",
                return_value=True,
            ), patch(
                "expertsearch.search_checkpoint._terminate_worker_process",
                return_value=(True, ""),
            ) as terminate:
                stopped, message = stop_search_job_preserving_progress(
                    checkpoint["job_id"]
                )

            persisted = load_search_checkpoint(checkpoint["job_id"])
            self.assertTrue(stopped)
            self.assertIn("恢复未完成任务", message)
            terminate.assert_called_once_with(43210, 123.0)
            self.assertEqual(persisted["status"], "interrupted")
            self.assertEqual(persisted["requested_subdomains"], ["育种", "植保"])
            self.assertEqual(persisted["failed_subdomains"], ["植保"])
            self.assertEqual(persisted["batch_files"], [str(batch_path.resolve())])
            self.assertTrue(batch_path.exists())
            self.assertIsNone(read_search_worker_pid(checkpoint["job_id"]))

    def test_stop_failure_keeps_running_checkpoint_untouched(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ):
            checkpoint = create_search_checkpoint(
                {
                    "main_domain": "农业",
                    "requested_subdomains": ["育种"],
                    "worker_started_at": 123.0,
                }
            )
            write_search_worker_pid(checkpoint["job_id"], 43210)
            with patch(
                "expertsearch.search_checkpoint.process_is_running",
                return_value=True,
            ), patch(
                "expertsearch.search_checkpoint._terminate_worker_process",
                return_value=(False, "无法确认进程身份"),
            ):
                stopped, message = stop_search_job_preserving_progress(
                    checkpoint["job_id"]
                )

            self.assertFalse(stopped)
            self.assertIn("未能安全停止", message)
            self.assertEqual(
                load_search_checkpoint(checkpoint["job_id"])["status"],
                "running",
            )

    def test_end_and_clear_removes_temporary_data_but_keeps_final_excel(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": str(Path(directory) / "checkpoints")}
        ):
            final_path = Path(directory) / "results" / "final.xlsx"
            final_path.parent.mkdir(parents=True)
            final_path.write_bytes(b"final")
            checkpoint = create_search_checkpoint(
                {
                    "main_domain": "农业",
                    "requested_subdomains": ["育种"],
                    "subdomain_statuses": [{"细分领域": "育种", "状态": "失败"}],
                    "supplemental_document_context": "补充材料",
                    "final_file_path": str(final_path),
                }
            )
            batch_dir = Path(checkpoint["batch_output_dir"])
            batch_file = batch_dir / "batch.xlsx"
            batch_file.write_bytes(b"batch")
            checkpoint = record_batch_file(
                checkpoint,
                str(batch_file),
                current_subdomain="育种",
                current_round=1,
            )
            checkpoint = mark_search_checkpoint(
                checkpoint,
                "partial",
                final_file_path=str(final_path),
            )

            cleared, message = end_and_clear_search_job(checkpoint["job_id"])

            persisted = load_search_checkpoint(checkpoint["job_id"])
            self.assertTrue(cleared)
            self.assertIn("可以开始新一轮", message)
            self.assertEqual(persisted["status"], "cleared")
            self.assertEqual(persisted["batch_files"], [])
            self.assertEqual(persisted["requested_subdomains"], [])
            self.assertFalse(batch_dir.exists())
            self.assertTrue(final_path.exists())
            self.assertIsNone(load_latest_recoverable_checkpoint())

    def test_end_and_clear_ignores_an_already_missing_batch_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ):
            checkpoint = create_search_checkpoint(
                {
                    "main_domain": "农业",
                    "requested_subdomains": ["育种"],
                }
            )
            batch_dir = Path(checkpoint["batch_output_dir"])
            shutil.rmtree(batch_dir)

            cleared, message = end_and_clear_search_job(checkpoint["job_id"])

            self.assertTrue(cleared)
            self.assertIn("可以开始新一轮", message)
            self.assertNotIn("临时批次目录未完全删除", message)

    def test_end_and_clear_stops_a_running_worker_first(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ):
            checkpoint = create_search_checkpoint(
                {
                    "main_domain": "农业",
                    "requested_subdomains": ["育种"],
                    "worker_started_at": 123.0,
                }
            )
            write_search_worker_pid(checkpoint["job_id"], 43210)
            with patch(
                "expertsearch.search_checkpoint.process_is_running",
                return_value=True,
            ), patch(
                "expertsearch.search_checkpoint._terminate_worker_process",
                return_value=(True, ""),
            ) as terminate:
                cleared, _ = end_and_clear_search_job(checkpoint["job_id"])

            self.assertTrue(cleared)
            terminate.assert_called_once_with(43210, 123.0)
            self.assertEqual(
                load_search_checkpoint(checkpoint["job_id"])["status"],
                "cleared",
            )

    def test_failed_worker_stop_does_not_clear_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ):
            checkpoint = create_search_checkpoint(
                {
                    "main_domain": "农业",
                    "requested_subdomains": ["育种"],
                    "worker_started_at": 123.0,
                }
            )
            write_search_worker_pid(checkpoint["job_id"], 43210)
            with patch(
                "expertsearch.search_checkpoint.process_is_running",
                return_value=True,
            ), patch(
                "expertsearch.search_checkpoint._terminate_worker_process",
                return_value=(False, "没有终止权限"),
            ):
                cleared, message = end_and_clear_search_job(checkpoint["job_id"])

            self.assertFalse(cleared)
            self.assertIn("未执行清空", message)
            self.assertNotEqual(
                load_search_checkpoint(checkpoint["job_id"])["status"],
                "cleared",
            )

    def test_active_job_registry_prevents_duplicate_resume(self):
        register_search_job("job-1")
        self.assertTrue(search_job_is_running("job-1"))
        unregister_search_job("job-1")
        self.assertFalse(search_job_is_running("job-1"))

    def test_checkpoint_persists_context_and_completed_batches(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ):
            checkpoint = create_search_checkpoint(
                {
                    "main_domain": "农业",
                    "requested_subdomains": ["方向一"],
                    "requested_total_experts": 10,
                    "subdomain_targets": {"方向一": 10},
                    "subdomain_statuses": [
                        {"细分领域": "方向一", "状态": "待重试", "目标人数": 10}
                    ],
                    "failed_subdomains": ["方向一"],
                    "experts_per_round": 10,
                    "supplemental_document_context": "补充材料正文",
                }
            )
            batch_path = Path(checkpoint["batch_output_dir"]) / "batch.xlsx"
            batch_path.write_bytes(b"test")
            checkpoint = record_batch_file(
                checkpoint,
                str(batch_path),
                current_subdomain="方向一",
                current_round=1,
            )

            loaded = load_search_checkpoint(checkpoint["job_id"])
            self.assertEqual(loaded["supplemental_document_context"], "补充材料正文")
            self.assertEqual(loaded["batch_files"], [str(batch_path.resolve())])
            self.assertEqual(load_latest_recoverable_checkpoint()["job_id"], checkpoint["job_id"])
            self.assertEqual(checkpoint_task_result(loaded)["main_domain"], "农业")

    def test_completed_checkpoint_is_not_offered_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ):
            checkpoint = create_search_checkpoint(
                {
                    "main_domain": "物理",
                    "requested_subdomains": ["量子信息"],
                    "subdomain_statuses": [],
                }
            )
            mark_search_checkpoint(checkpoint, "completed")
            self.assertIsNone(load_latest_recoverable_checkpoint())
            self.assertEqual(load_latest_search_checkpoint()["status"], "completed")

    def test_worker_pid_is_visible_across_streamlit_sessions(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ), patch("expertsearch.search_checkpoint.process_is_running", return_value=True):
            checkpoint = create_search_checkpoint(
                {"main_domain": "农业", "requested_subdomains": ["育种"]}
            )
            write_search_worker_pid(checkpoint["job_id"], 43210)

            self.assertEqual(read_search_worker_pid(checkpoint["job_id"]), 43210)
            self.assertTrue(search_job_is_running(checkpoint["job_id"]))


if __name__ == "__main__":
    unittest.main()
