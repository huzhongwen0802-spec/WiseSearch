# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import shutil
import signal
import tempfile
import threading
import time
import uuid
import weakref
import ctypes
from pathlib import Path
from typing import Any


RECOVERABLE_STATUSES = {"queued", "running", "interrupted", "partial", "failed"}
_ACTIVE_JOB_THREADS: dict[str, weakref.ReferenceType[threading.Thread]] = {}


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def register_search_job(job_id: str) -> None:
    _ACTIVE_JOB_THREADS[str(job_id)] = weakref.ref(threading.current_thread())


def unregister_search_job(job_id: str) -> None:
    _ACTIVE_JOB_THREADS.pop(str(job_id), None)


def search_job_is_running(job_id: str) -> bool:
    thread_ref = _ACTIVE_JOB_THREADS.get(str(job_id))
    thread = thread_ref() if thread_ref else None
    if thread is None or not thread.is_alive():
        _ACTIVE_JOB_THREADS.pop(str(job_id), None)
    else:
        return True
    pid = read_search_worker_pid(job_id)
    return bool(pid and process_is_running(pid))


def checkpoint_root() -> Path:
    configured = os.environ.get("SEARCH_CHECKPOINT_DIR", "tmp/search_checkpoints")
    return Path(configured).expanduser().resolve()


def _manifest_path(job_id: str) -> Path:
    return checkpoint_root() / str(job_id) / "checkpoint.json"


def _worker_pid_path(job_id: str) -> Path:
    return checkpoint_root() / str(job_id) / "worker.pid"


def process_is_running(pid: int) -> bool:
    """Check a process without signalling or terminating it on Windows."""
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(process_id, 0)
        except OSError:
            return False
        return True

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def write_search_worker_pid(job_id: str, pid: int) -> None:
    path = _worker_pid_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid)), encoding="ascii")


def read_search_worker_pid(job_id: str) -> int | None:
    try:
        return int(_worker_pid_path(job_id).read_text(encoding="ascii").strip())
    except (OSError, TypeError, ValueError):
        return None


def clear_search_worker_pid(job_id: str, expected_pid: int | None = None) -> None:
    path = _worker_pid_path(job_id)
    if expected_pid is not None and read_search_worker_pid(job_id) != int(expected_pid):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _terminate_worker_process(pid: int, expected_started_at: float) -> tuple[bool, str]:
    """Stop one verified worker process without risking an unrelated reused PID."""
    if not process_is_running(pid):
        return True, ""
    if expected_started_at <= 0:
        return False, "缺少后台进程启动时间，无法安全确认进程身份。"

    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            return False, f"发送停止信号失败：{error}"
        deadline = time.time() + 5
        while time.time() < deadline and process_is_running(pid):
            time.sleep(0.1)
        return (
            (True, "")
            if not process_is_running(pid)
            else (False, "后台进程未在 5 秒内停止。")
        )

    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_terminate | process_query_limited_information | synchronize,
        False,
        int(pid),
    )
    if not handle:
        return False, "无法打开后台进程，可能已退出或当前用户没有终止权限。"
    try:
        creation = _FileTime()
        exit_time = _FileTime()
        kernel_time = _FileTime()
        user_time = _FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return False, "无法读取后台进程启动时间，已取消终止操作。"
        windows_ticks = (int(creation.high) << 32) + int(creation.low)
        actual_started_at = (windows_ticks - 116444736000000000) / 10_000_000
        if abs(actual_started_at - expected_started_at) > 120:
            return False, "后台 PID 已被其他进程复用，已取消终止操作。"
        if not kernel32.TerminateProcess(handle, 0):
            return False, "Windows 未能终止后台检索进程。"
        kernel32.WaitForSingleObject(handle, 5000)
    finally:
        kernel32.CloseHandle(handle)
    return (
        (True, "")
        if not process_is_running(pid)
        else (False, "后台进程未在 5 秒内停止。")
    )


def stop_search_job_preserving_progress(job_id: str) -> tuple[bool, str]:
    """Stop one worker and retain every checkpoint artifact for later resume."""
    checkpoint = load_search_checkpoint(job_id)
    if not checkpoint:
        return False, "未找到需要停止的检索任务。"
    if str(checkpoint.get("status", "") or "") == "completed":
        return False, "该检索已经完成，无需停止。"

    pid = read_search_worker_pid(job_id)
    if pid and process_is_running(pid):
        stopped, stop_error = _terminate_worker_process(
            pid,
            float(checkpoint.get("worker_started_at", 0) or 0),
        )
        if not stopped:
            return False, f"后台任务未能安全停止：{stop_error}"
    clear_search_worker_pid(job_id, expected_pid=pid)

    mark_search_checkpoint(
        checkpoint,
        "interrupted",
        stopped_at=time.time(),
        stopped_by_user=True,
        worker_pid=0,
        progress_message="任务已手动停止；检查点和已完成批次均已保留，可恢复未完成任务",
    )
    return (
        True,
        "后台任务已停止，检查点和已完成专家数据均已保留。现在可以恢复未完成任务。",
    )


def end_and_clear_search_job(job_id: str) -> tuple[bool, str]:
    """End one search, clear temporary progress, and preserve final deliverables."""
    checkpoint = load_search_checkpoint(job_id)
    if not checkpoint:
        return False, "未找到需要结束的检索任务。"

    pid = read_search_worker_pid(job_id)
    if pid and process_is_running(pid):
        stopped, stop_error = _terminate_worker_process(
            pid,
            float(checkpoint.get("worker_started_at", 0) or 0),
        )
        if not stopped:
            return False, f"旧任务仍在运行，未执行清空：{stop_error}"
    clear_search_worker_pid(job_id, expected_pid=pid)

    job_dir = (checkpoint_root() / str(job_id)).resolve()
    cleanup_warnings: list[str] = []
    batch_dir_value = str(checkpoint.get("batch_output_dir", "") or "")
    if batch_dir_value:
        batch_dir = Path(batch_dir_value).resolve()
        if job_dir in batch_dir.parents:
            try:
                if batch_dir.exists():
                    shutil.rmtree(batch_dir)
            except FileNotFoundError:
                # Another cleanup path may have removed it moments earlier.
                pass
            except OSError as error:
                cleanup_warnings.append(f"临时批次目录未完全删除：{error}")
        else:
            cleanup_warnings.append("临时批次目录不在当前任务目录内，已跳过删除。")

    context_path_value = str(
        checkpoint.get("supplemental_document_context_path", "") or ""
    )
    if context_path_value:
        context_path = Path(context_path_value).resolve()
        if job_dir in context_path.parents:
            try:
                context_path.unlink(missing_ok=True)
            except OSError as error:
                cleanup_warnings.append(f"补充材料缓存未删除：{error}")

    checkpoint = mark_search_checkpoint(
        checkpoint,
        "cleared",
        cleared_at=time.time(),
        progress_message="旧检索已结束并清空，可以创建新任务",
        batch_files=[],
        batch_output_dir="",
        run_subdomains=[],
        requested_subdomains=[],
        subdomain_targets={},
        subdomain_statuses=[],
        failed_subdomains=[],
        supplemental_document_context="",
        supplemental_document_context_path="",
        supplemental_document_names=[],
        current_subdomain="",
        current_round=0,
        progress_current=0,
        progress_total=0,
        worker_pid=0,
        last_error="",
        error_source="",
        error_reason="",
        error_action="",
        error_detail="",
    )
    final_path = str(checkpoint.get("final_file_path", "") or "")
    message = "旧检索进度和临时批次已清空，可以开始新一轮检索。"
    if final_path and os.path.exists(final_path):
        message += " 已生成的最终 Excel 已保留。"
    if cleanup_warnings:
        message += " " + "；".join(cleanup_warnings)
    return True, message


def save_search_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist a search checkpoint so an interrupted UI can recover it."""
    job_id = str(checkpoint.get("job_id", "") or "").strip()
    if not job_id:
        raise ValueError("checkpoint is missing job_id")

    payload = dict(checkpoint)
    payload["job_id"] = job_id
    payload["updated_at"] = time.time()
    manifest_path = _manifest_path(job_id)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    supplemental_context = str(payload.pop("supplemental_document_context", "") or "")
    context_path_value = str(payload.get("supplemental_document_context_path", "") or "")
    context_path = (
        Path(context_path_value)
        if context_path_value
        else manifest_path.parent / "supplemental_context.txt"
    )
    if supplemental_context:
        context_path.write_text(supplemental_context, encoding="utf-8")
        payload["supplemental_document_context_path"] = str(context_path.resolve())

    fd, temporary_name = tempfile.mkstemp(
        prefix="checkpoint_",
        suffix=".json.tmp",
        dir=str(manifest_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, manifest_path)
    finally:
        if os.path.exists(temporary_name):
            os.remove(temporary_name)
    return payload


def create_search_checkpoint(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    job_dir = checkpoint_root() / job_id
    batch_dir = job_dir / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        **dict(payload),
        "job_id": job_id,
        "status": "running",
        "created_at": time.time(),
        "updated_at": time.time(),
        "batch_output_dir": str(batch_dir.resolve()),
        "batch_files": [],
        "final_file_path": "",
    }
    return save_search_checkpoint(checkpoint)


def load_search_checkpoint(job_id: str) -> dict[str, Any] | None:
    manifest_path = _manifest_path(job_id)
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    context_path = str(payload.get("supplemental_document_context_path", "") or "")
    if context_path:
        try:
            payload["supplemental_document_context"] = Path(context_path).read_text(
                encoding="utf-8"
            )
        except OSError:
            payload["supplemental_document_context"] = ""
    return payload


def load_latest_search_checkpoint() -> dict[str, Any] | None:
    root = checkpoint_root()
    if not root.exists():
        return None
    manifests = sorted(
        root.glob("*/checkpoint.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for manifest_path in manifests:
        checkpoint = load_search_checkpoint(manifest_path.parent.name)
        if checkpoint:
            return checkpoint
    return None


def load_latest_recoverable_checkpoint() -> dict[str, Any] | None:
    checkpoint = load_latest_search_checkpoint()
    if checkpoint and checkpoint.get("status") in RECOVERABLE_STATUSES:
        return checkpoint
    return None


def record_batch_file(
    checkpoint: dict[str, Any],
    excel_path: str,
    *,
    current_subdomain: str,
    current_round: int,
) -> dict[str, Any]:
    batch_files = list(checkpoint.get("batch_files", []) or [])
    absolute_path = str(Path(excel_path).resolve())
    if absolute_path not in batch_files:
        batch_files.append(absolute_path)
    checkpoint.update(
        {
            "status": "running",
            "batch_files": batch_files,
            "current_subdomain": str(current_subdomain or ""),
            "current_round": max(0, int(current_round)),
        }
    )
    return save_search_checkpoint(checkpoint)


def mark_search_checkpoint(
    checkpoint: dict[str, Any],
    status: str,
    **updates: Any,
) -> dict[str, Any]:
    checkpoint.update(updates)
    checkpoint["status"] = status
    return save_search_checkpoint(checkpoint)


def checkpoint_task_result(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """Expose persisted task metadata in the shape used by the Streamlit retry UI."""
    if not checkpoint:
        return {}
    return {
        "elapsed_seconds": int(checkpoint.get("elapsed_seconds", 0) or 0),
        "expert_count": int(checkpoint.get("expert_count", 0) or 0),
        "file_name": str(checkpoint.get("final_file_name", "") or ""),
        "file_path": str(checkpoint.get("final_file_path", "") or ""),
        "main_domain": str(checkpoint.get("main_domain", "") or ""),
        "requested_subdomains": list(checkpoint.get("requested_subdomains", []) or []),
        "subdomain_statuses": list(checkpoint.get("subdomain_statuses", []) or []),
        "failed_subdomains": list(checkpoint.get("failed_subdomains", []) or []),
        "retry_attempt_count": int(checkpoint.get("retry_attempt_count", 0) or 0),
        "requested_total_experts": int(
            checkpoint.get("requested_total_experts", 0) or 0
        ),
        "subdomain_targets": dict(checkpoint.get("subdomain_targets", {}) or {}),
        "experts_per_round": int(checkpoint.get("experts_per_round", 0) or 0),
        "checkpoint_job_id": str(checkpoint.get("job_id", "") or ""),
    }
