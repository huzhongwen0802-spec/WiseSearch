# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

from .error_diagnostics import checkpoint_error_updates, format_error_for_user
from .search_checkpoint import (
    checkpoint_root,
    load_search_checkpoint,
    mark_search_checkpoint,
    read_search_worker_pid,
    search_job_is_running,
    write_search_worker_pid,
)


def launch_search_job(job_id: str) -> tuple[int | None, str | None]:
    """Launch one detached worker so Streamlit reruns cannot cancel the search."""
    job_id = str(job_id or "").strip()
    checkpoint = load_search_checkpoint(job_id)
    if not checkpoint:
        return None, "未找到可执行的检索检查点。"
    if search_job_is_running(job_id):
        return read_search_worker_pid(job_id), None

    job_dir = checkpoint_root() / job_id
    log_path = job_dir / "worker.log"
    checkpoint = mark_search_checkpoint(
        checkpoint,
        "queued",
        worker_log_path=str(log_path.resolve()),
        last_error="",
        error_source="",
        error_reason="",
        error_action="",
        error_detail="",
        progress_message="后台任务正在启动",
    )

    worker_executable = Path(sys.executable)
    if os.name == "nt":
        pythonw = worker_executable.with_name("pythonw.exe")
        if pythonw.exists():
            worker_executable = pythonw
    command = [str(worker_executable), "-m", "expertsearch.search_worker", job_id]
    kwargs: dict[str, object] = {
        "cwd": str(Path(__file__).resolve().parents[1]),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    worker_environment = os.environ.copy()
    project_env_path = Path(__file__).resolve().parents[1] / ".env"
    if project_env_path.is_file():
        for key, value in dotenv_values(project_env_path, encoding="utf-8-sig").items():
            if value is not None:
                worker_environment[str(key)] = str(value)
    kwargs["env"] = worker_environment
    if os.name == "nt":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startup_info
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True

    try:
        with log_path.open("ab", buffering=0) as log_handle:
            process = subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
        write_search_worker_pid(job_id, process.pid)
    except Exception as error:
        mark_search_checkpoint(
            checkpoint,
            "failed",
            progress_message="后台任务启动失败",
            **checkpoint_error_updates(error, stage="后台检索进程启动"),
        )
        return None, format_error_for_user(error, stage="后台检索进程启动")
    return process.pid, None
