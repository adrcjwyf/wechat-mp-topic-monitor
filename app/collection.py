# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""Serial, checkpointed acquisition. Checkpoints and paid page contents commit together."""
from collections import Counter
from billing import lookup_balance, money_value, official_endpoint
from dataclasses import dataclass
from uuid import uuid4
import history_coverage
import hashlib
import json
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

from topic_engine import clean_visible
from monetary_topic import SELECTED
from platform_store import CN, published
from platform_source import history, public_body, notify, article_url

def date_bound(value, after=False):
    d = date.fromisoformat(value) + timedelta(days=int(after))
    return int(datetime(d.year,d.month,d.day,tzinfo=CN).timestamp())

def amount(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except InvalidOperation:
        return None

def identify(db, a, doc):
    nickname = clean_visible(doc.get("nickname") or doc.get("mp_nickname") or "")
    votes=Counter()
    own=set()
    seen=set()
    for item in doc["data"]:
        if not isinstance(item,dict):
            continue
        link=article_url(item.get("url"))
        biz=parse_qs(urlsplit(link).query).get("__biz",[""])[0]
        identity=(biz,str(item.get("sn") or link))
        if identity in seen:
            continue
        seen.add(identity)
        if not biz:
            continue
        votes[biz]+=1
        if str(item.get("original"))!="2" and str(item.get("item_show_type"))!="11":
            own.add(biz)
    leader,count=votes.most_common(1)[0] if votes else ("",0)
    majority=leader if count*2>len(seen) else ""
    named=bool(nickname and nickname==clean_visible(a["name"]))
    by_biz=bool(a["biz"] and a["biz"]==majority)
    reported_biz = str(doc.get("__biz") or doc.get("biz") or "").strip()
    pending = a["sample_url"] and a["name"].startswith("待识别公众号 · ")
    if pending and nickname:
        if a["biz"] and (reported_biz or majority) and (reported_biz or majority) != a["biz"]:
            raise ValueError("返回文章的公众号标识与示例链接不符。")
        biz = a["biz"] or reported_biz or (majority if majority in own else "")
        db.config.bind(a["id"], biz, nickname)
        a.update(next(item for item in db.config.read()["accounts"] if item["id"] == a["id"]))
        db.sync_accounts(db.config.read())
        return
    if not (named or by_biz) and (nickname or (a["biz"] and votes) or (votes and not majority)):
        raise ValueError(f"接口名称和文章归属无法确认（接口名称：{str(nickname or '未提供')[:80]}）。原始页已保留。")
    if named and reported_biz and not a["biz"]:
        db.config.bind(a["id"], reported_biz)
        a["biz"] = reported_biz
        db.sync_accounts(db.config.read())
    elif not a["biz"] and majority in own:
        db.config.bind(a["id"],majority)
        a["biz"]=majority
        db.sync_accounts(db.config.read())

@dataclass
class BodyReads:
    failures: int = 0
    skipped: int = 0

def finish_bodies(db, a, ids, stop, webhook, reads):
    for rid in ids:
        if stop.is_set():
            raise InterruptedError
        row=db.row(rid)
        if row["coverage"]=="new":
            can_fetch=bool(article_url(row["url"])) and reads.failures<4
            if can_fetch and stop.wait(.8):
                raise InterruptedError
            if db.claim_body(rid):
                if can_fetch:
                    body=public_body(row["url"])
                    if body:
                        reads.failures=0
                        db.save_body(rid,body,a)
                    else:
                        reads.failures+=1
                elif reads.failures>=4 and row["url"]:
                    reads.skipped+=1
                row=db.row(rid)
        if row["decision"]==SELECTED and db.mark_match(rid) and webhook:
            if not notify(webhook,"\n".join((a["name"],row["title"],row["excerpt"] or row["reason"],row["url"]))):
                yield 1

def checkpoint(db, a, kind, scope, selection):
    aid=a["id"]
    with db.transaction() as conn:
        state=dict(conn.execute("SELECT * FROM account_state WHERE account_id=?",(aid,)).fetchone())
        if kind=="history":
            history_coverage.begin(conn,aid,selection,scope)
        if kind=="increment" and state["inc_page"]==1:
            conn.execute("DELETE FROM page_seen WHERE account_id=? AND action=?",(aid,kind))
    return db.account_state(aid)

def run_job(db,c,job,stop,progress):
    summary=dict(action=job["kind"],calls=0,count=0,added=0,hits=0,in_range=0,matched_range=0,total_cost=None,balance=None,
                 code=None,ms=None,cost=None,account="",page=None,results=[],titles=[],push_errors=0)
    reads=BodyReads()
    kind=job["kind"]
    floor=Decimal(job["floor"])
    official = official_endpoint(c["api_url"])
    refreshed = False
    def guard(next_price=None):
        nonlocal balance, refreshed
        if kind=="test":
            return
        def blocked():
            return balance is not None and (balance <= floor or (next_price is not None and balance-next_price < floor))
        if official and not refreshed:
            refreshed = True
            balance = lookup_balance(c)
            db.record_billing(balance)
            summary["balance"] = str(balance)
        if blocked():
            raise ValueError("余额已到安全线，或再抓一页会低于保留金额。已保留断点。")
    try:
        db.sync_accounts(c)
        balance=money_value(db.run_state()["balance"])
        selected=[a.copy() for a in c["accounts"] if (a["id"]==job.get("account") if kind=="test" else a["enabled"])]
        populated = db.populated_accounts()
        if kind == "test" and any(a["id"] in populated for a in selected):
            raise ValueError("这个公众号已有文章，已验通。可用「更新最新文章」或补抓历史。")
        if kind == "increment":
            selected = [a for a in selected if a["id"] in populated]
            if not selected:
                raise ValueError("请先试拉一个公众号，并在设置中启用它。")
        if not selected:
            raise ValueError("请先选择公众号或启用至少一个公众号。")
        for a in selected:
            account_result={"name":a["name"],"pages":0,"count":0,"added":0,"hits":0,"note":""}
            summary["results"].append(account_result)
            choice=None
            scope=""
            if kind=="history":
                choice=job.get("choices",{}).get(a["id"])
                if not choice:
                    raise ValueError("历史补采须先预检并确认。")
                if choice["mode"]=="skip":
                    account_result["note"]="已跳过，0 费用"
                    continue
                scope=history_coverage.scope_of(job)
            next_price = money_value(db.account_state(a["id"])["unit_cost"])
            guard(Decimal("0.16") if next_price is None and official else next_price)
            state=checkpoint(db,a,kind,scope,choice)
            chain=state["backfill_chain"] if kind=="history" else uuid4().hex
            page,offset=1,""
            cutoff=state["latest_post_at"]
            if kind=="history":
                page,offset=state["next_backfill_page"],state["backfill_offset"]
            if kind=="increment" and state["inc_page"]>1:
                page,offset,cutoff=state["inc_page"],state["inc_offset"],state["inc_cutoff"]
            with db.transaction() as conn:
                pending=[r[0] for r in conn.execute("""SELECT review_id FROM articles WHERE book_id=?
                    AND (coverage='new' OR (decision=? AND announced=0))""",("mp:"+a["id"], SELECTED))]
            summary["push_errors"]+=sum(finish_bodies(db,a,pending,stop,c["webhook"],reads))
            if kind=="history" and state["backfill_done"]:
                account_result["note"]="该范围此前已完成"
                continue
            while True:
                if stop.is_set() or (summary["calls"] and stop.wait(.8)):
                    raise InterruptedError
                next_price = money_value(state["unit_cost"])
                guard(Decimal("0.16") if next_price is None and official else next_price)
                probe = not official and db.take_balance_probe()
                summary.update(account=a["name"],page=page)
                progress(summary)
                page_started=int(time.time())
                call={}
                summary["calls"]+=1
                try:
                    doc=history(c|a,call,page,offset)
                finally:
                    charge=money_value(call.get("cost"))
                    balance=money_value(call.get("balance"))
                    db.record_billing(balance,a["id"],charge)
                    summary.update(code=call.get("code"),ms=call.get("ms"),
                                   cost=str(charge) if charge is not None else None,
                                   balance=str(balance) if balance is not None else None)
                if charge is not None:
                    summary["total_cost"]=str((amount(summary["total_cost"]) or Decimal(0))+charge)
                progress(summary)
                if call.get("error"):
                    raise ValueError(call["error"])
                try:
                    identify(db,a,doc)
                except ValueError:
                    with db.transaction() as conn:
                        conn.execute("INSERT INTO history_pages(biz,checked_at,page,response_json) VALUES(?,?,?,?)",
                                     (a["id"],int(time.time()),page,json.dumps(doc["data"],ensure_ascii=False)))
                    raise
                total=doc.get("total_page")
                total=int(total) if str(total).isdigit() and int(total)>0 else None
                dates=[t for item in doc["data"] if isinstance(item,dict) and (t:=published(item))>0]
                next_offset=str(doc.get("offset") or "")
                last=not doc["data"] or str(doc.get("is_end","")).lower() in ("true","1") or bool(total and page>=total)
                confirmed_end=last
                boundary=kind=="history" and not job["unlimited"] and dates and min(dates)<job["lo"]
                if choice and choice.get("stop_at") and dates:
                    boundary = boundary or min(dates) <= choice["stop_at"]
                problem=""
                with db.transaction() as conn:
                    ids,known,added=db.ingest(conn,a,doc,page)
                    db.watermarks(conn,a["id"])
                    conn.execute("UPDATE account_state SET last_sync_at=? WHERE account_id=?",(int(time.time()),a["id"]))
                    if page==1 or total:
                        conn.execute("UPDATE account_state SET total_page=? WHERE account_id=?",(total,a["id"]))
                    if not total and page>1 and state["total_page"]:
                        last=last or page>=state["total_page"]
                    if kind=="increment":
                        boundary=not cutoff or known or any(t<=cutoff for t in dates)
                    complete=bool(last or boundary)
                    if kind!="test" and not complete:
                        signature=hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()
                        inserted=conn.execute("INSERT OR IGNORE INTO page_seen VALUES(?,?,?)",(a["id"],kind,signature)).rowcount
                        cursor_required = "offset" in doc or bool(offset) or ("is_end" in doc and total is None)
                        if not inserted or (cursor_required and (not next_offset or next_offset==offset)):
                            problem="接口未提供有效的下一页，或返回了重复页，已停止继续付费。"
                    if not problem:
                        history_coverage.record(conn,a["id"],chain,page,offset,next_offset,
                            [published(item) if isinstance(item,dict) else 0 for item in doc["data"]],
                            page_started,confirmed_end)
                    if kind=="history" and not problem:
                        conn.execute("""UPDATE account_state SET next_backfill_page=?,backfill_offset=?,backfill_done=?
                            WHERE account_id=?""",(page+1,next_offset,int(complete),a["id"]))
                    if kind=="increment" and not problem:
                        conn.execute("""UPDATE account_state SET inc_page=?,inc_offset=?,inc_cutoff=? WHERE account_id=?""",
                                    (1 if complete else page+1,"" if complete else next_offset,0 if complete else cutoff,a["id"]))
                summary["push_errors"]+=sum(finish_bodies(db,a,ids,stop,c["webhook"],reads))
                summary.update(body_circuit_open=reads.failures>=4,body_skipped=reads.skipped)
                rows=[db.row(rid) for rid in ids]
                matches=[r for r in rows if r["decision"]==SELECTED]
                window=[r for r in rows if job.get("lo",0)<=r["publish_at"]<job.get("hi",10**12)]
                summary["in_range"]+=len(window)
                summary["matched_range"]+=sum(r["decision"]==SELECTED for r in window)
                summary["titles"]=(summary["titles"]+[r["title"] for r in matches])[:20]
                summary["added"]+=added
                account_result["added"]+=added
                summary["count"]+=len(rows)
                summary["hits"]+=len(matches)
                account_result["pages"]+=1
                account_result["count"]+=len(rows)
                account_result["hits"]+=len(matches)
                progress(summary)
                if problem:
                    raise ValueError(problem)
                if (probe or (kind!="test" and floor>0)) and balance is None:
                    raise ValueError("平台未返回有效余额，已保存本页并暂停后续调用。")
                guard()
                if kind=="test" or complete:
                    account_result["note"]="单页测试完成" if kind=="test" else ("到达末页" if last else "到达日期或库内边界")
                    break
                if kind=="history" and job["cap"] and account_result["pages"]>=job["cap"]:
                    account_result["note"]="本次页数达到上限，可继续断点"
                    break
                page,offset=page+1,next_offset
                state=db.account_state(a["id"])
    except InterruptedError:
        summary["error"]="已停止；已提交页及下一页位置已保存。"
    except ValueError as exc:
        summary["error"]=str(exc)
    except Exception as exc:
        summary["error"]="网络或本地处理未完成，请核对网络后手动继续。错误类型 "+type(exc).__name__
    summary.update(body_circuit_open=reads.failures>=4,body_skipped=reads.skipped)
    return summary
