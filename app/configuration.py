# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
import hashlib
import json
import os
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from source_adapters import DAJIALA_URL, article_url, get_adapter


DEFAULT = dict(provider="dajiala", api_url=DAJIALA_URL, key="", verifycode="", accounts=[],
               interval_hours=0, webhook="", min_balance="0.20",
               ai_base_url="", ai_model="", ai_api_key="")
SECRETS = ("key", "verifycode", "webhook", "ai_api_key")
BOTS = {"open.feishu.cn": "/open-apis/bot/v2/hook/", "qyapi.weixin.qq.com": "/cgi-bin/webhook/send",
        "oapi.dingtalk.com": "/robot/send"}


def account_input(value):
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        return {"name": value, "enabled": True}
    u = urlsplit(value)
    if u.netloc.lower() != "mp.weixin.qq.com" or not (u.path == "/s" or u.path.startswith("/s/")):
        raise ValueError("请填写公众号全称或微信文章链接。")
    token = hashlib.sha256(value.encode()).hexdigest()[:20]
    biz = parse_qs(u.query).get("__biz", [""])[0]
    return {"name": "待识别公众号 · " + token[:8], "id": biz or token,
            "sample_url": value, "biz": biz, "enabled": True}


def validate_account(source):
    if not isinstance(source, dict):
        raise ValueError("请检查公众号配置。")
    fields = ("id", "region", "name", "biz", "sample_url")
    if set(source) - {*fields, "local_keywords", "enabled"}:
        raise ValueError("公众号配置包含未知字段。")
    if not all(isinstance(source.get(k, ""), str) for k in fields):
        raise ValueError("公众号名称、标识和分组须为文本。")
    a = {k: source.get(k, "").strip() for k in fields}
    locator = a["name"] or a["sample_url"]
    if locator.lower().startswith(("http://", "https://")):
        parsed = account_input(locator)
        for field in ("name", "sample_url", "biz", "id"):
            if field == "name" or not a[field]:
                a[field] = parsed[field]
    a["region"] = a["region"] or a["name"]
    if not a["name"]:
        raise ValueError("请填写公众号名称。")
    a["id"] = a["id"] or a["biz"] or hashlib.sha256(a["name"].encode()).hexdigest()[:20]
    if a["sample_url"] and not article_url(a["sample_url"]):
        raise ValueError("示例链接须为微信文章链接。")
    keys = source.get("local_keywords", [])
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise ValueError("账号关键词须为文本列表。")
    a["local_keywords"] = list(dict.fromkeys(k.strip() for k in keys if k.strip()))
    a["enabled"] = source.get("enabled", False)
    if not isinstance(a["enabled"], bool):
        raise ValueError("enabled 请填写 true 或 false。")
    return a


def validate(values):
    if not isinstance(values, dict) or values.keys() - DEFAULT.keys():
        raise ValueError("包含未知配置字段，请对照配置模板。")
    c = DEFAULT | values
    if str(c["interval_hours"]) not in ("0", "12", "24", "168"):
        raise ValueError("检查间隔请选择手动、12、24 或 168 小时。")
    c["interval_hours"] = int(c["interval_hours"])
    for k in DEFAULT.keys() - {"interval_hours", "accounts", "min_balance"}:
        if not isinstance(c[k], str) or len(c[k]) > 3000:
            raise ValueError("配置字段须为文本且长度合理。")
        c[k] = c[k].strip()
    get_adapter(c["provider"]).validate_endpoint(c["api_url"])
    if c["ai_base_url"]:
        u = urlsplit(c["ai_base_url"])
        local_http = u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")
        if ((u.scheme != "https" and not local_http) or not u.hostname or u.username is not None
                or u.password is not None or u.query or u.fragment):
            raise ValueError("AI 服务地址须为 HTTPS，或本机 HTTP 地址。密钥请填入独立字段。")
    try:
        floor = Decimal(str(c["min_balance"]))
        if not floor.is_finite() or floor < 0:
            raise ValueError("最低余额须为非负数。")
        c["min_balance"] = str(floor)
    except InvalidOperation:
        raise ValueError("最低余额须为非负数。") from None
    if not isinstance(c["accounts"], list):
        raise ValueError("accounts 应为列表。")
    c["accounts"] = [validate_account(a) for a in c["accounts"]]
    for field in ("id", "name", "biz"):
        seen = [a[field] for a in c["accounts"] if a[field]]
        if len(seen) != len(set(seen)):
            raise ValueError("公众号名称、标识或 biz 重复。")
    if c["webhook"]:
        u = urlsplit(c["webhook"])
        if u.scheme != "https" or u.netloc not in BOTS or not u.path.startswith(BOTS[u.netloc]):
            raise ValueError("请填写飞书、企业微信或钉钉群机器人的 HTTPS Webhook。")
    return c


class Config:
    def __init__(self, directory):
        self.path = Path(directory) / "config.json"
        self.lock = threading.RLock()
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.write(DEFAULT)

    def read(self):
        with self.lock:
            try:
                return validate(json.loads(self.path.read_text("utf-8-sig")))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("data/config.json 格式或内容有误，请对照 config.example.json 修正后保存。") from exc

    def write(self, values):
        with self.lock:
            c = validate(values)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, self.path)
            return c

    def update(self, fields):
        with self.lock:
            return self.write(self.read() | fields)

    def bind(self, aid, biz, nickname=""):
        with self.lock:
            c = self.read()
            a = next((a for a in c["accounts"] if a["id"] == aid), None)
            if not a or a["biz"] not in ("", biz):
                raise ValueError("采集期间账号配置已改变。")
            if nickname and a["sample_url"] and a["name"].startswith("待识别公众号 · "):
                if a["region"] == a["name"]:
                    a["region"] = nickname
                a["name"] = nickname
            a["biz"] = biz
            self.write(c)


def masked(c):
    return {**c, **{k: "" for k in SECRETS}, "configured": {k: bool(c[k]) for k in SECRETS}}
