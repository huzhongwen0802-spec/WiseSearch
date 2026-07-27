# -*- coding: utf-8 -*-

"""
LLM provider false-positive guard.

Some relay providers reject legitimate academic Chinese terms before the model sees
the prompt. This module only rewrites text sent to the LLM; external search,
filenames, UI display, and user-facing task names can keep the original wording.
"""

ACADEMIC_FALSE_POSITIVE_REPLACEMENTS = [
    ("种质资源收集与保存", "germplasm resource collection and conservation"),
    ("种质资源开发与利用", "germplasm resource development and utilization"),
    ("资源收集与保存", "resource collection and conservation"),
    ("资源开发与利用", "resource development and utilization"),
    ("收集与保存", "collection and conservation"),
    ("开发与利用", "development and utilization"),
    ("种质资源", "germplasm resources"),
    ("种质", "germplasm"),
    ("遗传资源", "genetic resources"),
]


def sanitize_for_llm(text: str) -> str:
    safe_text = str(text)
    for source, replacement in ACADEMIC_FALSE_POSITIVE_REPLACEMENTS:
        safe_text = safe_text.replace(source, replacement)
    return safe_text


def is_sensitive_word_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return "sensitive_words" in text or "sensitive words" in text or "local:sensitive" in text
