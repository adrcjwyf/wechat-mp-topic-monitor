# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""Shared decision labels and display helpers for configurable topics."""
import re
from urllib.parse import quote

from source_adapters import article_url


PAGE_SIZE = 30
SELECTED = "入选"
POSSIBLE = "疑似相关（待复核）"
REJECTED = "未入选"
TIERS = (SELECTED, POSSIBLE, REJECTED)
# Compatibility for excerpt renderers. Actual evidence comes from topic_engine.
BUSINESS_SIGNAL = re.compile(r"(?!)")


def original_link(article):
    return article_url(article["url"])


def reading_link(row):
    original = original_link(row)
    return original or "/articles/" + quote(row["review_id"], safe="")


def highlight(summary, reason):
    if not summary:
        return ""
    # Quote the sentence containing the matched term where possible.
    term = re.split(r"[·：:]", reason)[-1].strip()
    pos = summary.find(term) if term else -1
    if pos < 0:
        return summary[:110] + ("…" if len(summary) > 110 else "")
    start = max(summary.rfind(ch, 0, pos) for ch in "。！？；\n") + 1
    start = max(start, pos - 36)
    return ("…" if start > 0 else "") + summary[start:start + 110] + ("…" if len(summary) > start + 110 else "")
