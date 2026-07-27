# -*- coding: utf-8 -*-

"""Console/log helpers that tolerate non-UTF Windows terminals."""

from __future__ import annotations

import sys
from typing import Any


def safe_text(value: Any) -> str:
    """Return text that can be written to the current stdout encoding."""
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, errors="backslashreplace").decode(encoding)


def safe_print(*values: Any, sep: str = " ", end: str = "\n") -> None:
    """Print without letting console encoding errors break the task."""
    try:
        print(*values, sep=sep, end=end)
    except UnicodeEncodeError:
        safe_values = [safe_text(value) for value in values]
        safe_sep = safe_text(sep)
        safe_end = safe_text(end)
        print(*safe_values, sep=safe_sep, end=safe_end)
