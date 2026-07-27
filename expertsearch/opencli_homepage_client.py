# -*- coding: utf-8 -*-

import ipaddress
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .safe_logging import safe_print


@dataclass
class OpenCLIPageResult:
    ok: bool
    url: str
    content: str = ""
    error: str = ""


_HEALTH_LOCK = threading.Lock()
_HEALTH_CHECKED_AT = 0.0
_HEALTH_AVAILABLE = False
_HEALTH_DETAIL = "尚未检查"
_AUTO_RECOVERY_ATTEMPTED = False
_CALL_LOCK = threading.Lock()
_CALL_COUNT = 0
_BENCHMARK_PROXY_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _node_path() -> Path:
    configured = os.environ.get("OPENCLI_NODE_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(r"C:\Program Files\nodejs\node.exe")


def _opencli_entry_path() -> Path:
    configured = os.environ.get("OPENCLI_ENTRY_PATH", "").strip()
    if configured:
        return Path(configured)
    project_entry = (
        Path(__file__).resolve().parent.parent
        / "tools"
        / "opencli"
        / "runtime"
        / "node_modules"
        / "@jackwener"
        / "opencli"
        / "dist"
        / "src"
        / "main.js"
    )
    if project_entry.is_file():
        return project_entry
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "npm" / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"


def _safe_public_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False, "URL 无法解析"

    if parsed.scheme not in {"http", "https"}:
        return False, "只允许访问公开 HTTP/HTTPS 网页"
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False, "URL 缺少主机名"
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        return False, "禁止访问本机或局域网地址"

    def blocked_address(address: str) -> bool:
        ip = ipaddress.ip_address(address)
        allow_benchmark_proxy = os.environ.get(
            "OPENCLI_ALLOW_BENCHMARK_PROXY_IPS",
            "true",
        ).lower() in {"1", "true", "yes"}
        if allow_benchmark_proxy and ip in _BENCHMARK_PROXY_NETWORK:
            return False
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        )

    try:
        if blocked_address(hostname):
            return False, "禁止访问私有、回环或链路本地 IP"
    except ValueError:
        pass

    if os.environ.get("OPENCLI_VALIDATE_DNS_PUBLIC_IP", "true").lower() in {"1", "true", "yes"}:
        try:
            addresses = {
                entry[4][0]
                for entry in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            }
            for address in addresses:
                if blocked_address(address):
                    return False, "域名解析到了私有、回环或链路本地 IP"
        except (OSError, ValueError):
            return False, "域名解析失败"

    return True, ""


def _run_opencli(arguments: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    node = _node_path()
    entry = _opencli_entry_path()
    if not node.is_file():
        raise FileNotFoundError(f"未找到 Node.js: {node}")
    if not entry.is_file():
        raise FileNotFoundError(f"未找到 OpenCLI 入口: {entry}")

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        [str(node), str(entry), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        creationflags=creationflags,
        check=False,
    )


def _doctor_result() -> tuple[bool, str]:
    result = _run_opencli(["doctor"], timeout=15)
    output = f"{result.stdout}\n{result.stderr}".strip()
    available = (
        "[OK] Extension" in output
        and "[OK] Connectivity" in output
        and "not connected" not in output.lower()
    )
    return available, output


def _start_browser_bridge() -> tuple[bool, str]:
    script = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "windows"
        / "start_opencli_browser.cmd"
    )
    if not script.is_file():
        return False, f"未找到 OpenCLI Browser Bridge 启动脚本: {script}"

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            shell=False,
            creationflags=creationflags,
            check=False,
        )
    except Exception as exc:
        return False, f"自动启动 OpenCLI Browser Bridge 失败: {exc}"
    if result.returncode != 0:
        detail = str(result.stderr or result.stdout or "").strip()
        return False, f"自动启动 OpenCLI Browser Bridge 失败: {detail[:500]}"
    return True, ""


def opencli_health(force: bool = False) -> tuple[bool, str]:
    global _HEALTH_AVAILABLE, _HEALTH_CHECKED_AT, _HEALTH_DETAIL, _AUTO_RECOVERY_ATTEMPTED

    cache_seconds = float(os.environ.get("OPENCLI_HEALTH_CACHE_SECONDS", "120"))
    with _HEALTH_LOCK:
        now = time.monotonic()
        if not force and now - _HEALTH_CHECKED_AT < cache_seconds:
            return _HEALTH_AVAILABLE, _HEALTH_DETAIL

        try:
            _HEALTH_AVAILABLE, output = _doctor_result()
            auto_recover = os.environ.get(
                "OPENCLI_AUTO_RECOVER_BROWSER_BRIDGE",
                "true",
            ).lower() in {"1", "true", "yes"}
            if not _HEALTH_AVAILABLE and auto_recover and not _AUTO_RECOVERY_ATTEMPTED:
                _AUTO_RECOVERY_ATTEMPTED = True
                started, start_error = _start_browser_bridge()
                if started:
                    wait_seconds = float(os.environ.get("OPENCLI_AUTO_RECOVER_WAIT_SECONDS", "5"))
                    time.sleep(wait_seconds)
                    _HEALTH_AVAILABLE, output = _doctor_result()
                elif start_error:
                    output = f"{output}\n{start_error}"
            _HEALTH_DETAIL = (
                "OpenCLI Browser Bridge 已连接"
                if _HEALTH_AVAILABLE
                else f"OpenCLI Browser Bridge 未连接: {output[:500]}"
            )
        except Exception as exc:
            _HEALTH_AVAILABLE = False
            _HEALTH_DETAIL = f"OpenCLI 健康检查失败: {exc}"
        _HEALTH_CHECKED_AT = now
        return _HEALTH_AVAILABLE, _HEALTH_DETAIL


def _reserve_call() -> tuple[bool, str]:
    global _CALL_COUNT
    max_calls = int(os.environ.get("OPENCLI_HOMEPAGE_MAX_CALLS_PER_PROCESS", "25"))
    if max_calls <= 0:
        return False, "OpenCLI 主页回退已关闭"
    with _CALL_LOCK:
        if _CALL_COUNT >= max_calls:
            return False, f"OpenCLI 本进程调用已达到上限 {max_calls}"
        _CALL_COUNT += 1
        return True, ""


def reset_opencli_call_budget() -> None:
    """为新一批专家补全任务重置 OpenCLI 调用预算。"""
    global _CALL_COUNT
    with _CALL_LOCK:
        _CALL_COUNT = 0


def _clean_opencli_output(output: str) -> str:
    text = str(output or "").strip()
    text = re.sub(r"(?s)^ok:\s*true\s*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_homepage_with_opencli(url: str) -> OpenCLIPageResult:
    """
    使用真实浏览器读取公开专家主页。仅在 Browser Bridge 健康时执行。
    """
    clean_url = str(url or "").strip()
    safe, reason = _safe_public_url(clean_url)
    if not safe:
        return OpenCLIPageResult(False, clean_url, error=reason)

    available, health_detail = opencli_health()
    if not available:
        return OpenCLIPageResult(False, clean_url, error=health_detail)

    reserved, reason = _reserve_call()
    if not reserved:
        return OpenCLIPageResult(False, clean_url, error=reason)

    timeout = float(os.environ.get("OPENCLI_HOMEPAGE_TIMEOUT_SECONDS", "45"))
    wait_seconds = os.environ.get("OPENCLI_HOMEPAGE_WAIT_SECONDS", "3")
    try:
        result = _run_opencli(
            [
                "web",
                "read",
                "--url",
                clean_url,
                "--stdout",
                "true",
                "--download-images",
                "false",
                "--wait",
                str(wait_seconds),
                "--wait-until",
                "domstable",
                "--frames",
                "same-origin",
                "--window",
                "background",
                "--site-session",
                "ephemeral",
                "--keep-tab",
                "false",
                "-f",
                "plain",
            ],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return OpenCLIPageResult(False, clean_url, error=f"OpenCLI 读取超时，超过 {timeout:.0f} 秒")
    except Exception as exc:
        return OpenCLIPageResult(False, clean_url, error=f"OpenCLI 调用失败: {exc}")

    output = _clean_opencli_output(result.stdout)
    error_output = str(result.stderr or "").strip()
    if result.returncode != 0 or not output:
        detail = error_output or output or f"退出码 {result.returncode}"
        safe_print(f"[OpenCLI主页访问警告] {clean_url} | {detail[:500]}")
        return OpenCLIPageResult(False, clean_url, error=detail[:1000])

    max_chars = int(os.environ.get("OPENCLI_HOMEPAGE_MAX_CHARS", "16000"))
    return OpenCLIPageResult(True, clean_url, content=output[:max_chars])
