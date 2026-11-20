# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""Loopback UI, file-backed settings and one serial polling worker."""
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlencode
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from jinja2 import Environment, FileSystemLoader, select_autoescape
from billing import official_endpoint, lookup_balance, money_value
from configuration import account_input, SECRETS, masked, validate
from monetary_topic import original_link
from topic_engine import revision
from __version__ import PROJECT_NAME, AUTHOR, REPO_URL, VERSION
from review import article_view, export_list, export_markdown
from history_coverage import projections
from collection import run_job, date_bound, amount
from platform_store import CN
from decimal import Decimal, InvalidOperation
import re
from platform_store import Store

def account_edits(config, fields):
    """Apply the compact form to the current config, including unchecked accounts."""
    accounts = {a["id"]: a for a in config["accounts"]}
    if "remove_id" in fields:
        raise ValueError("请刷新设置页，确认后再移除。")
    rows = []
    for field, aid in fields.items():
        if not re.fullmatch(r"id_[0-9]+", field):
            continue
        if aid not in accounts:
            raise ValueError("公众号列表已变化，请刷新后再保存。")
        rows.append(accounts[aid] | {"enabled": bool(fields.get("enabled_" + field[3:]))})
    if {a["id"] for a in rows} != accounts.keys() or len(rows) != len(accounts):
        raise ValueError("公众号列表已变化，请刷新后再保存。")
    for field, value in fields.items():
        if re.fullmatch(r"name_[0-9]+", field) and value.strip():
            rows.append(account_input(value))
    changes = {k: fields[k] for k in ("key", "verifycode") if fields.get(k, "").strip()}
    if "api_url" in fields:
        changes["api_url"] = fields["api_url"].strip()
    return changes | {"accounts": rows}


def keyword_fields(rules, accounts):
    visible = {k: rules[k] for k in ("name", "include", "maybe", "exclude")}
    visible["accounts"] = {a["id"]: {k: rules["accounts"].get(a["id"], {}).get(k, [])
                                     for k in ("include", "exclude")} for a in accounts}
    return visible


def apply_keywords(rules, accounts, draft):
    visible = keyword_fields(rules, accounts)
    if not isinstance(draft, dict) or draft.keys() != visible.keys():
        raise ValueError("关键词表单不完整，请刷新后再保存。")
    submitted = draft["accounts"]
    if not isinstance(submitted, dict) or submitted.keys() != visible["accounts"].keys():
        raise ValueError("公众号列表已变化，请刷新后再保存。")
    local = dict(rules["accounts"])
    for aid, edits in submitted.items():
        if not isinstance(edits, dict) or edits.keys() != {"include", "exclude"}:
            raise ValueError("请核对公众号专属关键词。")
        local[aid] = local.get(aid, {}) | edits
    return rules | draft | {"accounts": local}


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass

def timestamp(value):
    return datetime.fromtimestamp(value,CN).strftime("%Y-%m-%d %H:%M:%S") if value else "尚无记录"

def money(value):
    return "未返回" if value is None else f"{value} 元"

def options(kind,c,fields):
    floor=str(fields.get("floor",c["min_balance"]))
    if amount(floor) is None or amount(floor)<0:
        raise ValueError("最低余额须为非负数。")
    job=dict(kind=kind,floor=floor,account=fields.get("account"))
    if kind=="history":
        cap=int(fields.get("cap",0))
        unlimited=bool(fields.get("unlimited"))
        if cap<0:
            raise ValueError("页数上限须为非负整数。")
        job.update(cap=cap,unlimited=unlimited)
        if not unlimited:
            start,end=fields.get("start","2026-01-01"),fields.get("end","2026-11-30")
            lo,hi=date_bound(start),date_bound(end,True)
            if lo>=hi:
                raise ValueError("请核对起止日期。")
            job.update(start=start,end=end,lo=lo,hi=hi)
    return job

def table_estimate(plan):
    b = plan["budget"]
    choice = b["estimates"][plan["default"]]
    if plan["default"] == "skip":
        return "默认跳过，本次 0 元"
    if b["unit"] is None:
        return "待新接口首次拉取后自动按实测单价估算"
    if choice["pages"] is not None:
        return f"约 {choice['pages']} 页 / {choice['cost']} 元"
    if b["year_pages"]:
        return f"每年约 {b['year_pages']} 页 / {b['year_cost']} 元"
    return "试拉后可按发文频率估算" if not plan["has_articles"] else "暂无有效的连续页样本"


def history_options(plan):
    """Only expose distinct choices that the worker can execute."""
    key = plan["default"]
    starts = plan["starts"]
    action = starts[key]
    # A bridge at the head with no cursor/boundary reads the same pages as a restart.
    if key == "bridge" and action["page"] == 1 and not action["offset"] and not action.get("stop_at"):
        key = "force"
    keys = [key]
    if key != "skip":
        keys.append("skip")
    if plan["has_articles"] and "force" not in keys:
        keys.append("force")
    return keys


def history_view(job, plans, balance):
    rows = []
    known = Decimal(0)
    unknown = 0
    for plan in plans:
        budget = plan["budget"]
        overlaps = []
        for interval in budget["overlaps"]:
            start, end = interval["first"] or interval["start"], interval["last"] or interval["end"]
            if start[:4] == end[:4]:
                start, end = start[5:], end[5:]
            overlaps.append(f"{start}～{end} · {interval['count']}篇")
        badge = "已有 " + "、".join(overlaps) if overlaps else ""
        choices = []
        for key in history_options(plan):
            action = plan["starts"][key]
            estimate = budget["estimates"][key]
            cost = estimate["cost"]
            price = f"约 {estimate['pages']} 页 / {cost} 元" if cost is not None else ("待新接口首次拉取后自动按实测单价估算" if budget["unit"] is None else "暂无法估算")
            repeat = key == "force" and plan["has_articles"]
            if key == "skip":
                label = "跳过此号"
                if key == plan["default"]:
                    label = "区间已全覆盖，本次跳过" if job["unlimited"] or budget["missing"] == 0 else "沿用上次进度，本次跳过"
                price = "0 元"
            elif key == "force":
                label = "强制从第 1 页重拉" if repeat else "从第 1 页开始抓取"
            elif action.get("stop_at"):
                label = "补抓新发，遇已有日期即停"
            else:
                label = f"从第 {action['page']} 页续抓"
            choices.append(dict(key=key, label=label, price=price, cost=cost, repeat=repeat))
        default = choices[0]
        if default["cost"] is None:
            unknown += 1
        else:
            known += Decimal(default["cost"])
        metrics = []
        if budget["avg"]:
            metrics.append(f"约 {budget['avg']:g} 天/页")
        if budget["missing"] is not None:
            metrics.append(f"剩余未覆盖约 {budget['missing']} 天")
        elif budget["total"]:
            metrics.append(f"全部历史共 {budget['total']} 页")
        rows.append(dict(name=plan["name"], region=plan["region"], badge=badge,
                         metrics=" · ".join(metrics), choices=choices))
    available = amount(balance)
    return dict(period="全部历史" if job["unlimited"] else f"{job['start']} ～ {job['end']}",
                rows=rows, cost=f"{known:.2f}", unknown=unknown, floor=job["floor"],
                balance=f"{available:.2f}" if available is not None else None,
                short=available is not None and known > 0 and available - Decimal(job["floor"]) < known)


class Scheduler:
    def __init__(self,db):
        self.db=db
        self.condition=threading.Condition()
        self.command=None
        self.busy=False
        self.closing=False
        self.cancel=threading.Event()
        self._thread=None
        self.finished=0
        self.preview_plan=None

    def start(self):
        self._thread=threading.Thread(target=self.run,daemon=True)
        self._thread.start()

    def preview(self,fields):
        with self.condition:
            if self.busy or self.command:
                raise ValueError("请等当前任务结束后再核算费用。")
            c=self.db.config.read()
            self.db.sync_accounts(c)
            job=options("history",c,fields)
            plans=projections(self.db,c["accounts"],job)
            if not plans:
                raise ValueError("请先启用至少一个区域。")
            key=secrets.token_urlsafe(24)
            self.preview_plan=(key,c,job,plans)
            return key,job,plans

    def preview_details(self, key):
        with self.condition:
            if not self.preview_plan or self.preview_plan[0] != key:
                raise ValueError("本次估算已失效，请返回设置重新估算。")
            token, _, job, rows = self.preview_plan
            return token, job, rows

    def confirm(self,fields):
        with self.condition:
            plan=self.preview_plan
            if not plan or fields.get("plan")!=plan[0]:
                raise ValueError("核算结果已更新或已使用，请重新核算。")
            if self.busy or self.command:
                raise ValueError("已有任务在运行，请稍后重新核算。")
            _,c,job,plans=plan
            if c!=self.db.config.read() or plans!=projections(self.db,c["accounts"],job):
                self.preview_plan=None
                raise ValueError("账号配置或历史断点已变化，请重新核算。")
            choices={}
            for i,p in enumerate(plans):
                mode=fields.get(f"choice_{i}")
                if mode not in history_options(p):
                    raise ValueError("请核对每个账号的拉取选项。")
                if mode=="force" and p["has_articles"] and fields.get("force_ack")!="yes":
                    raise ValueError("强制重拉会重复付费。请返回费用核算页，确认弹窗后再开始。")
                choices[p["id"]]=p["starts"][mode]
            if all(v["mode"]=="skip" for v in choices.values()):
                self.preview_plan=None
                return "已跳过全部账号，本次 0 次调用、0 费用。"
            if not c["key"]:
                raise ValueError("执行付费拉取前请保存 API Key，再重新核算。")
            self.preview_plan=None
            saved={k:v for k,v in job.items() if k in ("start","end","unlimited","cap","floor")}
            with self.db.transaction() as db:
                db.execute("INSERT OR REPLACE INTO app_meta VALUES('history_form',?)",(json.dumps(saved),))
            self.cancel.clear()
            self.command=(c,job|{"choices":choices})
            self.condition.notify()
            return ""

    def request(self,kind,fields):
        with self.condition:
            if self.busy or self.command or time.monotonic()-self.finished<2:
                return "已有任务或刚刚结束，请查看本轮结果。"
            c=self.db.config.read()
            if not c["key"]:
                raise ValueError("请先填写 API Key 并保存。")
            job=options(kind,c,fields)
            if kind=="test" and not any(a["id"]==job["account"] for a in c["accounts"]):
                raise ValueError("请选择一个测试账号。")
            populated = self.db.populated_accounts()
            if kind=="test" and job["account"] in populated:
                raise ValueError("这个公众号已经验通，请使用更新或补抓。")
            if kind=="increment" and not any(a["enabled"] and a["id"] in populated for a in c["accounts"]):
                raise ValueError("请先试拉并启用至少一个公众号。")
            if kind=="history":
                raise ValueError("请先免费核算日期范围和费用。")
            self.preview_plan=None
            self.cancel.clear()
            self.command=(c,job)
            self.condition.notify()
            return ""

    def update(self,fields,*,simple_accounts=False):
        with self.condition,self.db.config.lock:
            if self.busy or self.command:
                raise ValueError("请等待本轮结束后修改配置。")
            old=self.db.config.read()
            if simple_accounts:
                fields = account_edits(old, fields)
            c=validate(old|fields)
            self.db.sync_accounts(c)
            self.db.config.write(c)
            with self.db.transaction() as db:
                if old["interval_hours"]!=c["interval_hours"]:
                    db.execute("UPDATE run_state SET anchor=? WHERE id=1",(int(time.time()),))
            self.condition.notify()
            return c

    def change_account(self, fields, restore):
        aid = fields.get("account_id", "")
        with self.condition, self.db.config.lock:
            if self.busy or self.command:
                raise ValueError("请等本轮抓取结束后再修改公众号。")
            config = self.db.config.read()
            accounts = config["accounts"]
            account = next((a for a in accounts if a["id"] == aid), None)
            key = "removed_account:" + aid
            if restore:
                with self.db.transaction() as conn:
                    record = conn.execute("SELECT v FROM app_meta WHERE k=?", (key,)).fetchone()
                if account or not record:
                    raise ValueError("该账号的状态已变化，请刷新。")
                updated = accounts + [json.loads(record[0])]
            else:
                if fields.get("ack") != "yes" or not account:
                    raise ValueError("移除未确认或账号已变化，请刷新。")
                with self.db.transaction() as conn:
                    conn.execute("INSERT OR REPLACE INTO app_meta VALUES(?,?)",
                                 (key, json.dumps(account, ensure_ascii=False)))
                updated = [a for a in accounts if a["id"] != aid]
            self.update({"accounts": updated})
            if restore:
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM app_meta WHERE k=?", (key,))

    def refresh_balance(self):
        with self.condition:
            if self.busy or self.command:
                raise ValueError("请等当前抓取结束后再刷新余额。")
            c = self.db.config.read()
            self.db.sync_accounts(c)
            if official_endpoint(c["api_url"]):
                balance = lookup_balance(c)
                self.db.record_billing(balance)
                return "refreshed"
            self.db.arm_balance_probe()
            return "armed"

    def halt(self):
        with self.condition:
            self.command=None
            self.cancel.set()
            self.condition.notify()

    def stop(self):
        with self.condition:
            self.closing=True
            self.halt()

    def join(self):
        if self._thread:
            self._thread.join()

    def run(self):
        while True:
            with self.condition:
                if self.closing:
                    return
                if self.command:
                    (c,job),self.command=self.command,None
                else:
                    try:
                        c=self.db.config.read()
                        s=self.db.run_state()
                        due=c["interval_hours"] and c["key"] and s["status"] in ("ok","ready") and time.time()>=s["anchor"]+c["interval_hours"]*3600
                        if due:
                            populated=self.db.populated_accounts()
                            due=any(a["enabled"] and a["id"] in populated for a in c["accounts"])
                    except ValueError:
                        due=False
                    if not due:
                        self.condition.wait(15)
                        continue
                    self.cancel.clear()
                    job=options("increment",c,{})
                self.preview_plan=None
                self.busy=True
            try:
                progress=lambda r:self.db.report("working",r)
                progress({})
                r=run_job(self.db,c,job,self.cancel,progress)
                self.db.report("paused" if r.get("error") else "ok",r)
            finally:
                with self.condition:
                    self.busy=False
                    self.finished=time.monotonic()

class WebApp:
    def __init__(self,db,scheduler):
        self.db = db
        self.scheduler = scheduler
        self.last_visit=time.monotonic()
        self.exit_requested=False
        self.csrf = secrets.token_urlsafe(32)
        self.env = Environment(loader=FileSystemLoader(Path(__file__).with_name("templates")),autoescape=select_autoescape())
        self.env.globals.update(project_name=PROJECT_NAME,author=AUTHOR,repo_url=REPO_URL,version=VERSION,fmt_ts=timestamp,fmt_interval=lambda m:f"{m//60} 小时",money=money)

    def _render(self,respond,template,*,status="200 OK",**ctx):
        if "rules" not in ctx:
            ctx["rules"] = self.db.current_rules()
        page=self.env.get_template(template).render(**ctx).encode()
        respond(status,[("Content-Type","text/html; charset=utf-8"),("Cache-Control","no-store"),
                         ("X-Frame-Options","DENY"),("Referrer-Policy","same-origin"),
                         ("Content-Security-Policy","default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")])
        return [page]

    def _redirect(self,respond,where):
        respond("303 See Other",[("Location",where),("Cache-Control","no-store")])
        return [b""]

    def json(self,respond,status,doc):
        respond(status,[("Content-Type","application/json; charset=utf-8"),("Cache-Control","no-store"),("X-Content-Type-Options","nosniff")])
        return [json.dumps(doc,ensure_ascii=False).encode()]

    def form_denied(self,respond,api,message):
        if api:
            return self.json(respond,"403 Forbidden",{"error":message})
        return self._render(respond,"notice.html",status="403 Forbidden",note=message)

    def rules_page(self, respond, saved):
        rules = self.db.current_rules()
        accounts = self.db.config.read()["accounts"]
        return self._render(respond, "rules_editor.html", rules=keyword_fields(rules, accounts),
                            rules_revision=revision(rules), c={"accounts": accounts}, csrf=self.csrf,
                            note="已保存并重算。" if saved else "")

    def settings(self,respond,note=""):
        c=self.db.config.read()
        self.db.sync_accounts(c)
        state=self.db.run_state()
        stats=self.db.stats()
        with self.db.transaction() as conn:
            row=conn.execute("SELECT v FROM app_meta WHERE k='history_form'").fetchone()
        form=dict(start="2026-01-01",end="2026-11-30",cap=0,floor=c["min_balance"],unlimited=False)
        if row:
            form.update(json.loads(row[0]))
        plans=self.table_estimates(c,form)
        configured={a["id"] for a in c["accounts"]}
        active={a["id"] for a in c["accounts"] if a["enabled"]}
        can_update=any(s["account_id"] in active and s["n"] for s in stats)
        official = official_endpoint(c["api_url"])
        available = money_value(state["balance"])
        highest = max((money_value(a["unit_cost"]) or Decimal(0) for a in stats if a["account_id"] in active), default=Decimal(0))
        low = available is not None and (available <= Decimal(form["floor"]) or available-highest < Decimal(form["floor"]))
        with self.db.transaction() as conn:
            archived = [json.loads(row[0]) for row in conn.execute(
                "SELECT v FROM app_meta WHERE k GLOB 'removed_account:*' ORDER BY k")]
        archived = [a for a in archived if a["id"] not in configured]
        return self._render(respond,"settings.html",archived=archived,official=official,low=low,c=masked(c),state=state,r=state["report"],stats=stats,plans=plans,configured=configured,can_update=can_update,form=form,
                            csrf=self.csrf,busy=self.scheduler.busy or bool(self.scheduler.command),note=note)

    def table_estimates(self,c,fields):
        job=options("history",c,fields)
        plans=projections(self.db,c["accounts"],job,enabled_only=False)
        return {p["id"]:table_estimate(p) for p in plans}

    def article(self,respond,rid,*,back,note="",draft=None,status="200 OK"):
        with self.db.transaction() as db:
            row=db.execute("SELECT * FROM articles WHERE review_id=?",(rid,)).fetchone()
        if row is None:
            return self._render(respond,"notice.html",status="404 Not Found",note="未找到这篇文章。")
        return self._render(respond,"article.html",back=back,status=status,row=row,csrf=self.csrf,note=note,
                            original=original_link(row),draft=draft,version=self.db.body_version(row["content_html"]))

    def idle_exit(self):
        if self.exit_requested:
            return True
        if self.scheduler.busy or self.scheduler.command or time.monotonic()-self.last_visit<150:
            return False
        try:
            return not self.db.config.read()["interval_hours"]
        except ValueError:
            return True

    def __call__(self,env,respond):
        path,method=env["PATH_INFO"],env["REQUEST_METHOD"]
        self.last_visit=time.monotonic()
        query=parse_qs(env.get("QUERY_STRING",""))
        api=path=="/api/config"
        compact = method=="POST" and path=="/history" and "application/json" in env.get("HTTP_ACCEPT","")
        try:
            if method=="GET" and path in ("/history-ui.js","/tags-ui.js"):
                code=Path(__file__).with_name(path[1:]).read_bytes()
                respond("200 OK",[("Content-Type","application/javascript; charset=utf-8"),("Cache-Control","no-store")])
                return [code]
            if method=="GET" and path=="/history":
                plan,job,rows=self.scheduler.preview_details(query.get("plan",[""])[0])
                view=history_view(job,rows,self.db.run_state()["balance"])
                return self._render(respond,"history_preview.html",plan=plan,csrf=self.csrf,view=view)
            if method=="GET" and path=="/history-estimate":
                fields={k:v[0] for k,v in query.items() if k in {"start","end","unlimited"}}
                return self.json(respond,"200 OK",self.table_estimates(self.db.config.read(),fields))
            if method=="GET" and path=="/alive.js":
                respond("200 OK",[("Content-Type","application/javascript"),("Cache-Control","no-store")])
                return [b"setInterval(()=>fetch('/alive',{cache:'no-store'}).catch(()=>{}),15000)"]
            if method=="GET" and path=="/alive":
                return self.json(respond,"200 OK",{})
            if method=="GET" and path in {"/","/target","/topic"}:
                return article_view(self,respond,query,True)
            if method=="GET" and path=="/rules":
                return self.rules_page(respond, query.get("saved")==["1"])
            if method=="GET" and path in {"/onboarding","/settings","/sources/1"}:
                saved=env.get("QUERY_STRING")=="saved"
                note = "已保存专题词库，全库文章已重新筛选，费用 0 元。" if query.get("rules_saved")==["1"] else ""
                if query.get("account")==["removed"]:
                    note="已移除并保存。可在「已移除公众号」中恢复。"
                elif query.get("account")==["restored"]:
                    note="公众号已恢复，原有文章和抓取进度可继续使用。"
                if query.get("balance")==["refreshed"]:
                    note="余额已刷新。"
                elif query.get("balance")==["armed"]:
                    note="旧余额已清空，下次抓取用首请求核对余额。"
                return self.settings(respond,"配置已保存。" if saved else note)
            if method=="GET" and path=="/all":
                return article_view(self,respond,query)
            if method=="GET" and path in ("/ai-review.md","/ai-all.md"):
                return export_markdown(self,respond,query,full=path=="/ai-all.md")
            if method=="GET" and path in ("/all.csv","/all.json"):
                return export_list(self,respond,path.endswith(".csv"),query)
            article_route=re.fullmatch(r"/articles/([^/]+)",path)
            if article_route:
                source = "topic" if query.get("from")==["topic"] else "all"
                filters = {k:v[0] for k,v in query.items() if k in ("account","region","start","end","decision","page","review_page")}
                back = "/" + source + ("?" + urlencode(filters) if filters else "")
            if method=="GET" and article_route:
                note="正文已保存，已按最新内容重新筛选。" if query.get("saved")==["1"] else ""
                return self.article(respond,article_route[1],back=back,note=note)
            if method=="GET" and api:
                return self.json(respond,"200 OK",{**masked(self.db.config.read()),"csrf":self.csrf})
            if method=="POST" and (article_route or path in {"/rules","/settings","/check","/test","/history","/history/confirm","/cancel","/exit","/balance/refresh","/accounts/remove","/accounts/restore","/api/config"}):
                origin=env.get("HTTP_ORIGIN")
                if origin and origin!="http://"+env["HTTP_HOST"]:
                    return self.form_denied(respond,api or compact,"本次提交未执行。请从本机程序重新打开设置页，再提交一次。")
                n=int(env.get("CONTENT_LENGTH") or 0)
                if not 0 < n <= (4*1024*1024 if article_route else 1024*1024):
                    raise ValueError("请求长度异常。")
                raw=env["wsgi.input"].read(n).decode()
                if api:
                    if env.get("CONTENT_TYPE","").split(";")[0]!="application/json":
                        raise ValueError("需要 application/json。")
                    fields=json.loads(raw)
                    if not isinstance(fields,dict):
                        raise ValueError("请提交 JSON 对象。")
                else:
                    fields={k:v[0] for k,v in parse_qs(raw,keep_blank_values=True).items()}
                token=fields.pop("csrf","") or env.get("HTTP_X_CSRF_TOKEN","")
                if not isinstance(token,str) or not hmac.compare_digest(token.encode(),self.csrf.encode()):
                    return self.form_denied(respond,api or compact,"这个页面已过期。请重新打开设置页，再提交一次。")
                if path in ("/accounts/remove","/accounts/restore"):
                    restore = path.endswith("/restore")
                    self.scheduler.change_account(fields, restore)
                    return self._redirect(respond,"/settings?account="+("restored" if restore else "removed"))
                if path=="/balance/refresh":
                    result=self.scheduler.refresh_balance()
                    return self._redirect(respond,"/settings?balance="+result)
                if path=="/rules":
                    with self.scheduler.condition:
                        if self.scheduler.busy or self.scheduler.command:
                            raise ValueError("请等本轮抓取结束后再保存词库。")
                        if "keywords" in fields:
                            draft = apply_keywords(self.db.current_rules(), self.db.config.read()["accounts"],
                                                   json.loads(fields["keywords"]))
                        else:
                            draft = json.loads(fields.get("rules", ""))
                        self.db.save_rules(draft, fields.get("revision",""))
                    return self._redirect(respond,"/rules?saved=1")
                if article_route:
                    body=fields.get("body","")
                    if len(body)>300000:
                        return self.article(respond,article_route[1],back=back,draft=body,status="400 Bad Request",note="正文最多 30 万字。")
                    try:
                        saved=self.db.edit_body(article_route[1],body,fields.get("version",""))
                    except ValueError as exc:
                        return self.article(respond,article_route[1],back=back,draft=body,status="400 Bad Request",note=str(exc))
                    if not saved:
                        return self.article(respond,article_route[1],back=back,draft=body,status="409 Conflict",
                            note="库中的正文已经变化。请核对下方已保存版本，再决定是否保存你的文字。")
                    return self._redirect(respond,path+"?"+urlencode(filters | {"from":source,"saved":"1"}))
                if path in ("/settings","/api/config"):
                    simple = not api and fields.get("form_kind") == "simple_accounts"
                    if not api and not simple:
                        for k in SECRETS:
                            clear=fields.pop("clear_"+k,None)
                            if clear:
                                fields[k]=""
                            elif not fields.get(k):
                                fields.pop(k,None)
                        rows=[]
                        for i in sorted(int(k[7:]) for k in fields if re.fullmatch(r"region_[0-9]+",k)):
                            row={k:fields.pop(f"{k}_{i}","").strip() for k in ("id","region","name","biz","sample_url","local_keywords","enabled")}
                            if not any(row.values()):
                                continue
                            row["local_keywords"]=[k for k in re.split(r"[，,、\s]+",row["local_keywords"]) if k]
                            row["enabled"]=bool(row["enabled"])
                            rows.append(row)
                        fields["accounts"]=rows
                    cfg=self.scheduler.update(fields,simple_accounts=simple)
                    if api:
                        return self.json(respond,"200 OK",masked(cfg))
                    return self._redirect(respond,"/settings?saved")
                if path in ("/exit","/cancel"):
                    self.scheduler.halt()
                    if path=="/exit":
                        self.exit_requested=True
                        return self._render(respond,"notice.html",note="程序正在退出，可以关闭网页。")
                    return self._redirect(respond,"/settings")
                if path=="/history":
                    plan,job,rows=self.scheduler.preview(fields)
                    view=history_view(job,rows,self.db.run_state()["balance"])
                    if compact:
                        return self.json(respond,"200 OK",dict(plan=plan,view=view))
                    return self._render(respond,"history_preview.html",plan=plan,csrf=self.csrf,view=view)
                if path=="/history/confirm":
                    message=self.scheduler.confirm(fields)
                else:
                    message=self.scheduler.request("test" if path=="/test" else "increment",fields)
                if message:
                    return self.settings(respond,message)
                return self._redirect(respond,"/settings")
        except (ValueError, OSError) as exc:
            msg=str(exc) if type(exc) is ValueError else "读取失败，请核对本地配置文件或提交格式。"
            if api or compact:
                return self.json(respond,"400 Bad Request",{"error":msg})
            if path in ("/history","/history/confirm"):
                return self._render(respond,"notice.html",status="400 Bad Request",note=msg)
            return self._render(respond,"notice.html",note=msg)
        return self.json(respond,"404 Not Found",{"error":"页面不存在。"})

def build_runtime():
    db=Store(os.environ["WECHAT_MP_DATA"])
    scheduler=Scheduler(db)
    return WebApp(db,scheduler),scheduler
