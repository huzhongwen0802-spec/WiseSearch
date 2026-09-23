# -*- coding: utf-8 -*-

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from expertsearch.search_checkpoint import (
    create_search_checkpoint,
    load_search_checkpoint,
    read_search_worker_pid,
)
from expertsearch.search_job_process import launch_search_job


class SearchJobProcessTests(unittest.TestCase):
    def test_launch_failure_persists_explicit_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ), patch(
            "expertsearch.search_job_process.subprocess.Popen",
            side_effect=OSError("[WinError 10013] socket access forbidden"),
        ):
            checkpoint = create_search_checkpoint(
                {"main_domain": "农业", "requested_subdomains": ["育种"]}
            )
            pid, error = launch_search_job(checkpoint["job_id"])

            persisted = load_search_checkpoint(checkpoint["job_id"])
            self.assertIsNone(pid)
            self.assertIn("后台任务进程", error)
            self.assertEqual(persisted["status"], "failed")
            self.assertEqual(persisted["error_source"], "后台任务进程")
            self.assertIn("防火墙", persisted["error_reason"])
            self.assertTrue(persisted["error_action"])

    def test_launch_records_detached_worker_and_prevents_duplicate_launch(self):
        process = Mock(pid=24680)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SEARCH_CHECKPOINT_DIR": directory}
        ), patch(
            "expertsearch.search_job_process.subprocess.Popen",
            return_value=process,
        ) as popen, patch(
            "expertsearch.search_checkpoint.process_is_running",
            return_value=True,
        ):
            checkpoint = create_search_checkpoint(
                {"main_domain": "农业", "requested_subdomains": ["育种"]}
            )
            first_pid, first_error = launch_search_job(checkpoint["job_id"])
            second_pid, second_error = launch_search_job(checkpoint["job_id"])

            self.assertEqual(first_pid, 24680)
            self.assertIsNone(first_error)
            self.assertEqual(second_pid, 24680)
            self.assertIsNone(second_error)
            self.assertEqual(popen.call_count, 1)
            command = popen.call_args.args[0]
            if os.name == "nt":
                self.assertTrue(command[0].lower().endswith("pythonw.exe"))
                self.assertIn("startupinfo", popen.call_args.kwargs)
            self.assertEqual(read_search_worker_pid(checkpoint["job_id"]), 24680)
            self.assertEqual(load_search_checkpoint(checkpoint["job_id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
