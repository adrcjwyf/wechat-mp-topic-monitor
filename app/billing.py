# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""Billing values and the official read-only balance lookup."""
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests


def official_endpoint(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in {"www.dajiala.com", "dajiala.com"}
            and parsed.port in (None, 443) and parsed.username is None and parsed.password is None)


def money_value(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def page_charge(doc):
    for key in ("cost_money", "cost"):
        value = money_value(doc.get(key))
        if value is not None:
            return str(value)
    return None


def lookup_balance(config):
    if not official_endpoint(config["api_url"]) or not config["key"]:
        raise ValueError("请先填写极致了接口的 API Key。")
    try:
        with requests.post("https://www.dajiala.com/fbmain/monitor/v3/get_remain_money",
                           json={"key": config["key"], "verifycode": config["verifycode"]},
                           timeout=(4, 8), allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError
            doc = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ValueError("免费余额查询失败，请稍后刷新余额。") from exc
    if not isinstance(doc, dict) or str(doc.get("code")) not in {"0", "200"}:
        raise ValueError("免费余额查询未成功，请核对 Key 与附加码。")
    value = money_value(doc.get("remain_money"))
    if value is None and isinstance(doc.get("data"), dict):
        value = money_value(doc["data"].get("remain_money"))
    if value is None:
        raise ValueError("余额接口未返回有效金额，请稍后刷新。")
    return value
