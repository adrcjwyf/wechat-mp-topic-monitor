# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""Evaluate a saved topic against article text; all vocabulary is supplied by the caller."""
import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

GROUPS = ("include", "maybe", "exclude")
PRESET_DIR = Path(__file__).resolve().parents[1] / "presets"
ZERO_WIDTH = str.maketrans("", "", "\u200b\u200c\u200d\ufeff")


def clean_visible(value):
    return (value or "").translate(ZERO_WIDTH).strip()


def normal(value):
    return unicodedata.normalize("NFKC", clean_visible(value)).casefold()


def terms(values, *, expressions=False):
    if not isinstance(values, list) or len(values) > 512:
        raise ValueError("每组请填写不超过 512 个词条。")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or len(value) > (512 if expressions else 256):
            raise ValueError("词条过长或格式有误。")
        value = clean_visible(value)
        key = value if expressions else normal(value)
        if not key or key in seen:
            continue
        if expressions:
            try:
                compiled(value)
            except (re.error, OverflowError, RecursionError) as exc:
                raise ValueError(f"正则无效：{value[:60]}（{exc}）") from exc
        seen.add(key)
        result.append(value)
    return result


@lru_cache(maxsize=512)
def compiled(expression):
    return re.compile(expression, re.I)


def validate_rules(value):
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise ValueError("请填写专题名称。")
    name = clean_visible(value["name"])
    prompt = value.get("prompt", "")
    if not name or len(name)>120 or not isinstance(prompt,str) or len(prompt)>16000:
        raise ValueError("专题名称限 1～120 字，AI 复核提示词限 16000 字。")
    regex = value.get("regex", {})
    accounts = value.get("accounts", {})
    if not isinstance(regex, dict) or not isinstance(accounts, dict):
        raise ValueError("正则选项和公众号设置须为 JSON 对象。")
    rules = dict(name=name, prompt=prompt, regex={}, accounts={})
    for group in GROUPS:
        rules[group] = terms(value.get(group, []))
        rules["regex"][group] = terms(regex.get(group, []), expressions=True)
    if isinstance(value.get("profile"),str):
        rules["profile"] = value["profile"]
    for aid, local in accounts.items():
        if not aid or len(aid)>256 or not isinstance(local,dict):
            raise ValueError("公众号专属规则格式有误。")
        rules["accounts"][aid] = {key: terms(local[key]) for key in (*GROUPS,"identity") if key in local}
    return rules


def presets():
    return {p.stem: validate_rules(json.loads(p.read_text("utf-8-sig"))) for p in sorted(PRESET_DIR.glob("*.json"))}


def serialize(rules):
    return json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def revision(rules):
    return hashlib.sha256(serialize(rules).encode()).hexdigest()


def find_hit(fields, words, patterns):
    for field, text in fields:
        for word in words:
            if normal(word) in text:
                return field, word
        for pattern in patterns:
            hit = compiled(pattern).search(text)
            if hit:
                return field, hit.group()[:80] or pattern
    return None


def classify_record(row, rules, identity, tiers, fallback=None):
    selected, possible, rejected = tiers
    local = rules["accounts"].get(row["book_id"].removeprefix("mp:"), {})
    identity = local.get("identity", identity)
    fields = [(label, normal(row[key])) for label,key in (("标题","title"),("摘要","summary"),("正文","content_text"))]
    raw = json.loads(row["raw_json"])
    link_biz = parse_qs(urlsplit(row["url"] or "").query).get("__biz", [""])[0]
    repost = str(raw.get("original"))=="2" or str(raw.get("item_show_type"))=="11"
    if row["source_note"] or (not repost and any(b and row["biz"] and b != row["biz"] for b in (link_biz,raw.get("biz")))):
        return rejected, "来源", row["source_note"] or "原文标识与账号不符", ""
    legacy = fallback(row, identity, rules) if fallback else None
    for group, tier in (("exclude",rejected),("include",selected),("maybe",possible)):
        for origin, words, expressions in (("本号",local.get(group,[]),[]),("统一",rules[group],rules["regex"][group])):
            hit = find_hit(fields, words, expressions)
            if hit:
                if legacy and tier == legacy[0]:
                    return legacy
                label, term = hit
                value = {"标题":row["title"],"摘要":row["summary"],"正文":row["content_text"]}[label] or ""
                pos = max(0, normal(value).find(normal(term)))
                return tier, origin+"规则", label+" · "+term, value[max(0,pos-24):pos+90]
    return legacy or (rejected, "专题", "未命中当前专题词库", "")
