# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""公众号历史接口扩展。自定义适配器实现 HistoryAdapter，并在启动时调用
register_adapter("provider_name", adapter)。在配置中填写 provider 和 api_url。

fetch_page 每次至多请求一次，禁用自动重试和重定向。返回字典中的 data 为
文章列表，支持 title、digest、url、sn、post_time（Unix 秒）、original 等字段。
可选 nickname 用于核对账号，offset 为下一页游标，total_page 和 is_end 表示
页数或终点。result 字典携带 code、http、ms 以及本次 cost、剩余 balance；
无法可靠取得的价格或余额填 None。失败应给出脱敏的 error 并停止。
"""
import re
import time
from typing import Protocol
from urllib.parse import urlsplit

from billing import page_charge
import requests

DAJIALA_URL = "https://www.dajiala.com/fbmain/monitor/v3/post_history"


def article_url(value):
    if not isinstance(value, str):
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme in ("http", "https") and url.netloc == "mp.weixin.qq.com"
                and (url.path == "/s" or url.path.startswith("/s/"))):
            return url._replace(scheme="https", fragment="").geturl()
    except ValueError:
        pass
    return ""


class HistoryAdapter(Protocol):
    def validate_endpoint(self, url: str) -> None: ...

    def fetch_page(self, config: dict, result: dict, page: int, offset: str) -> dict: ...


class DajialaAdapter:
    errors = {
        10002: "请核对 API Key 和附加码。",
        20001: "账户余额不足，请在接口服务商后台核对。",
        -1: "接口请求过于频繁，请稍后手动再试。",
    }

    def validate_endpoint(self, url):
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment or any(ch.isspace() for ch in url)):
            raise ValueError("请填写兼容历史发文接口的 HTTPS 地址。")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError("接口地址端口无效。") from exc

    def fetch_page(self, config, result, page, offset):
        self.validate_endpoint(config["api_url"])
        if not config.get("key"):
            raise ValueError("请先设置接口密钥。")
        name = config["name"].strip()
        payload = {"key": config["key"], "page": page}
        if config["biz"] and not (name.startswith("待识别公众号 · ") and config["sample_url"]):
            payload.update(biz=config["biz"], name=name, nickname=name, url="",
                           offset=offset, verifycode=config["verifycode"])
        else:
            sample = article_url(config.get("sample_url"))
            if sample:
                payload["url"] = sample
            elif name:
                payload.update(name=name, nickname=name)
            else:
                raise ValueError("请填写公众号准确名称或文章链接。")
            if config["verifycode"]:
                payload["verifycode"] = config["verifycode"]
            if offset:
                payload["offset"] = offset

        start = time.monotonic()
        try:
            with requests.post(config["api_url"], json=payload, timeout=(6, 30),
                               allow_redirects=False) as response:
                result["http"] = response.status_code
                doc = response.json()
        finally:
            result["ms"] = round((time.monotonic() - start) * 1000)
        if not isinstance(doc, dict):
            raise ValueError("接口响应格式异常。")
        try:
            code = int(doc["code"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("接口未返回有效的状态码。") from None
        result.update(code=code, cost=page_charge(doc), balance=doc.get("remain_money"))
        if code:
            result["error"] = self.errors.get(code, f"接口返回错误码 {code}，请核对服务商后台。")
            return {"data": []}
        if not 200 <= result["http"] < 300 or not isinstance(doc.get("data"), list):
            raise ValueError("列表或 HTTP 状态异常。")
        return doc


_adapters = {"dajiala": DajialaAdapter()}


def register_adapter(name: str, adapter: HistoryAdapter):
    """注册受信任的 Python 适配器；配置文件仅选择预先注册的名称。"""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name) or name in _adapters:
        raise ValueError("适配器名称无效或已注册。")
    if not all(callable(getattr(adapter, key, None)) for key in ("validate_endpoint", "fetch_page")):
        raise TypeError("适配器须提供 validate_endpoint 和 fetch_page。")
    _adapters[name] = adapter


def get_adapter(name: str) -> HistoryAdapter:
    try:
        return _adapters[name]
    except KeyError:
        raise ValueError("找不到已注册的采集适配器。") from None
