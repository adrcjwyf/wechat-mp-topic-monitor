# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""Provider dispatch, public article reading, and user-configured notifications."""
import re
import time
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup, ParserRejectedMarkup

from source_adapters import article_url, get_adapter


def history(c, result, page, offset):
    adapter = get_adapter(c["provider"])
    # Pass only the credentials intended for the history provider.
    source = {key: c[key] for key in ("api_url", "key", "verifycode", "name", "biz", "sample_url")}
    return adapter.fetch_page(source, result, page, offset)


def public_body(link):
    if not article_url(link):
        return ""
    try:
        deadline = time.monotonic()+20
        with requests.get(link, timeout=(4, 8), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                return ""
            data = bytearray()
            for part in response.iter_content(65536):
                data.extend(part)
                if len(data) > 4_000_000 or time.monotonic() > deadline:
                    return ""
        soup = BeautifulSoup(bytes(data), "html.parser", from_encoding="utf-8")
        node = soup.select_one("#js_content")
        if not node or soup.select_one("#js_verify, #verify_form, #js_verify_code"):
            return ""
        for tag in node.select("script,style"):
            tag.decompose()
        body = node.get_text(" ", strip=True)[:300000]
        if len(body) < 500 and re.search(r"环境异常|完成验证后即可继续访问|访问过于频繁|该内容已被发布者删除",body):
            return ""
        return body
    except (requests.RequestException, ValueError, OSError, RecursionError, ParserRejectedMarkup):
        return ""

def notify(bot, text):
    host = urlsplit(bot).netloc
    feishu = host == "open.feishu.cn"
    payload = {"msg_type":"text","content":{"text":text}} if feishu else {"msgtype":"text","text":{"content":text}}
    try:
        with requests.post(bot, json=payload, timeout=(4, 8), allow_redirects=False) as r:
            answer = r.json()
            return r.status_code == 200 and isinstance(answer,dict) and str(answer.get("code",answer.get("StatusCode")) if feishu else answer.get("errcode")) == "0"
    except (ValueError, requests.RequestException, OSError):
        return False
