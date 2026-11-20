# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""Evidence for continuous history traversals. All bounds are inclusive Unix seconds."""
import json
from decimal import Decimal, InvalidOperation
from math import ceil
from billing import official_endpoint
import uuid
import time
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))

def scope_of(job):
    return json.dumps(["all"] if job["unlimited"] else [job["start"],job["end"]])

def migrate(conn, published):
    fields={r[1] for r in conn.execute("PRAGMA table_info(account_state)")}
    columns={"covered_from":"INTEGER","covered_to":"INTEGER","covered_page":"INTEGER DEFAULT 0",
             "covered_offset":"TEXT DEFAULT ''","backfill_chain":"TEXT DEFAULT ''"}
    for name,kind in columns.items():
        if name not in fields:
            conn.execute(f"ALTER TABLE account_state ADD COLUMN {name} {kind}")
    conn.execute("""CREATE TABLE IF NOT EXISTS history_coverage(
        account_id TEXT,chain TEXT,page INTEGER,input_offset TEXT,output_offset TEXT,
        from_ts INTEGER,to_ts INTEGER,terminal INTEGER,PRIMARY KEY(account_id,chain,page))""")
    conn.execute("CREATE INDEX IF NOT EXISTS coverage_bounds ON history_coverage(account_id,from_ts,to_ts DESC,page DESC)")
    if "covered_page" in fields:
        return
    # Older releases committed the forward checkpoint with the page. They did not log
    # the input cursor. Do not infer earlier cursors, or stitch unrelated raw page runs.
    for s in conn.execute("SELECT * FROM account_state WHERE next_backfill_page>1").fetchall():
        n=s["next_backfill_page"]-1
        args=(s["account_id"],s["biz"])
        head=conn.execute("SELECT min(checked_at) FROM history_pages WHERE biz IN (?,?) AND page=1",args).fetchone()[0]
        scope=json.loads(s["backfill_scope"] or "[]")
        if not head or not scope:
            continue
        tails=[]
        for raw, in conn.execute("SELECT response_json FROM history_pages WHERE biz IN (?,?) AND page=?",(*args,n)):
            records=json.loads(raw)
            times=[published(item) for item in records if isinstance(item,dict)]
            # Use the shallowest possible bound when several runs paid for that page.
            tails.append(min(times)+1 if times and min(times)>0 else head+1)
        end=bool(s["backfill_done"] and scope==["all"])
        lower=max(tails,default=head+1)
        if s["backfill_done"] and len(scope)==2:
            lower=min(lower,int(datetime.fromisoformat(scope[0]).replace(tzinfo=CN).timestamp()))
        chain=uuid.uuid4().hex
        save(conn,s["account_id"],chain,n,"" if n==1 else None,s["backfill_offset"],
             0 if end else lower,head,end)
        conn.execute("UPDATE account_state SET backfill_chain=? WHERE account_id=?",(chain,s["account_id"]))

def save(conn,aid,chain,page,entry,following,lower,upper,terminal):
    conn.execute("INSERT OR REPLACE INTO history_coverage VALUES(?,?,?,?,?,?,?,?)",
                 (aid,chain,page,entry,following,lower,upper,int(terminal)))
    best=conn.execute("""SELECT * FROM history_coverage WHERE account_id=?
        ORDER BY from_ts ASC,to_ts DESC,page DESC LIMIT 1""",(aid,)).fetchone()
    conn.execute("""UPDATE account_state SET covered_from=?,covered_to=?,covered_page=?,covered_offset=?
        WHERE account_id=?""",(best["from_ts"],best["to_ts"],best["page"],best["output_offset"],aid))

def record(conn,aid,chain,page,entry,following,times,head_at,terminal):
    if page==1:
        upper=head_at
        lower=upper+1
    else:
        prev=conn.execute("""SELECT * FROM history_coverage WHERE account_id=? AND chain=? AND page=?""",
                          (aid,chain,page-1)).fetchone()
        if not prev or prev["terminal"] or prev["output_offset"]!=entry:
            return
        upper,lower=prev["to_ts"],prev["from_ts"]
    if terminal:
        lower=0
    elif times and all(t>0 for t in times) and all(x>=y for x,y in zip(times,times[1:])):
        # Other articles with the tail timestamp can still be on the next page.
        lower=min(lower,min(times)+1)
    save(conn,aid,chain,page,entry,following,lower,upper,terminal)

def day(ts):
    return datetime.fromtimestamp(ts,CN).strftime("%Y-%m-%d")

def covered_intervals(tails, job):
    """Merge inclusive evidence as half-open intervals, clipped to the requested dates."""
    bounds = []
    for row in tails:
        start, end = row["from_ts"], row["to_ts"] + 1
        if not job["unlimited"]:
            start, end = max(start, job["lo"]), min(end, job["hi"])
        if start < end:
            bounds.append((start, end))
    merged = []
    for start, end in sorted(bounds):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def budget(conn, aid, state, anchor, intervals, job, starts, default, official):
    book = "mp:" + aid
    try:
        price = Decimal(str(state["unit_cost"]))
        if not price.is_finite() or price < 0:
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        price = Decimal("0.16") if official else None
    k = anchor["page"] if anchor else 0
    average = None
    if anchor and anchor["from_ts"] <= anchor["to_ts"]:
        # The upper evidence bound is the time of checking, not the newest post.
        first, last = conn.execute("""SELECT min(publish_at), max(publish_at) FROM articles
            WHERE book_id=? AND publish_at>0 AND publish_at BETWEEN ? AND ?""",
            (book, max(0, anchor["from_ts"] - 1), anchor["to_ts"])).fetchone()
        if first is not None:
            average = max(1, (last - first) / 86400) / k
    overlaps = []
    for low, high in intervals:
        # The tail timestamp itself can straddle two pages. Include stored entries at that
        # timestamp in the displayed count, without claiming the entire timestamp is covered.
        display_low = max(low - 1, job.get("lo", 0), 1)
        count, oldest, latest = conn.execute("""SELECT count(*),min(publish_at),max(publish_at)
            FROM articles WHERE book_id=? AND publish_at>0 AND publish_at>=? AND publish_at<?""",
            (book, display_low, high)).fetchone()
        overlaps.append(dict(start=day(low) if low else "历史起点", end=day(high-1), count=count,
                             first=day(oldest) if count else "", last=day(latest) if count else ""))
    missing = None
    if not job["unlimited"]:
        covered = sum(end-start for start,end in intervals)
        missing = max(0, (job["hi"]-job["lo"]-covered)/86400)
    total = state["total_page"] if state else None
    estimates = {}
    for key, action in starts.items():
        pages = None
        if key == "skip":
            pages = 0
        elif job["unlimited"]:
            endpoint = total or (k if anchor and anchor["terminal"] else None)
            if endpoint:
                pages = max(1, endpoint-action["page"]+1)
        elif average:
            # A paid walk may need to pass pages between the requested end and the
            # cursor. Resume charges only the earlier gap, force also reads old pages.
            tail = anchor["from_ts"]
            if "stop_at" in action:
                head = min(job["hi"], int(datetime.now(CN).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())+86400)
                pages = max(1,ceil(max(0,head-action["stop_at"])/(86400*average))-action["page"]+1)
            elif key in ("resume", "bridge", "after_first", "continue"):
                older = max(0, tail-job["lo"]) / 86400
                pages = max(1, ceil(older/average) + max(0,k-action["page"]+1))
            else:
                pages = max(1, ceil(max(0,state["latest_post_at"]-job["lo"])/(86400*average)))
                if anchor["terminal"] and job["lo"] <= state["oldest_post_at"]:
                    pages = k
            if total:
                pages = min(pages, max(1,total-action["page"]+1))
        if job["cap"] and pages is not None:
            pages = min(pages, job["cap"])
        cost = "0.00" if key == "skip" else (f"{pages*price:.2f}" if price is not None and pages is not None else None)
        estimates[key] = dict(pages=pages, cost=cost)
    base = estimates[default]
    forced = estimates["force"]
    saved = None if None in (forced["pages"], base["pages"]) else max(0, forced["pages"]-base["pages"])
    if saved is None and default != "skip":
        saved = min(k, starts[default]["page"]-1)
    year_pages = ceil(365/average) if average else None
    return dict(avg=float(f"{average:.3g}") if average else None, missing=ceil(missing) if missing is not None else None,
                overlaps=overlaps, estimates=estimates, saved=saved, saved_cost=f"{(saved or 0)*price:.2f}" if price is not None else None,
                year_pages=year_pages, year_cost=f"{year_pages*price:.2f}" if year_pages and price is not None else None,
                total=total, unit=str(price) if price is not None else None, sample_pages=k,
                cap_cost=f"{price*job['cap']:.2f}" if job["cap"] and price is not None else None)


def projections(db,accounts,job, *, enabled_only=True):
    out=[]
    official = official_endpoint(db.config.read()["api_url"])
    with db.transaction() as conn:
        for a in accounts:
            if enabled_only and not a["enabled"]:
                continue
            aid=a["id"]
            state=conn.execute("SELECT * FROM account_state WHERE account_id=?",(aid,)).fetchone()
            tails=conn.execute("""SELECT h.* FROM history_coverage h JOIN
                (SELECT chain,max(page) AS page FROM history_coverage WHERE account_id=? GROUP BY chain) t
                ON h.chain=t.chain AND h.page=t.page WHERE h.account_id=?""",(aid,aid)).fetchall()
            intervals = covered_intervals(tails, job)
            def full(h):
                return bool(h["terminal"]) if job["unlimited"] else h["from_ts"]<=job["lo"] and job["hi"]-1<=h["to_ts"]
            cover=next((h for h in tails if full(h)),None)
            anchor=cover or min(tails,key=lambda h:(h["from_ts"],-h["to_ts"]),default=None)
            # Repeated single-page tests may see different publication dates; use the current cursor.
            if not cover and anchor and all(h["page"]==1 for h in tails):
                recent=[h for h in tails if not h["terminal"] and (h["output_offset"] or "").strip()
                        and 0<=time.time()-h["to_ts"]<=86400]
                if recent:
                    anchor=max(recent,key=lambda h:h["to_ts"])
            same=state and state["backfill_scope"]==scope_of(job) and not state["backfill_done"] and state["next_backfill_page"]>1
            starts={"force":dict(page=1,offset="",chain="",mode="force")}
            default="force"
            info="尚无可证明的连续覆盖，从第 1 页开始。"
            if anchor:
                low,high=anchor["from_ts"],anchor["to_ts"]
                earliest=day(low) if low else "历史起点"
                info=f"{earliest} ～ {day(high)} 已有连续页记录（已拉至第 {anchor['page']} 页）；核验截至北京时间 {datetime.fromtimestamp(high,CN):%Y-%m-%d %H:%M:%S}。"
                if low>high:
                    info=f"已拉至第 {anchor['page']} 页，时间证据不足以证明完整区间。"
                if cover or (not job["unlimited"] and intervals == [(job["lo"], job["hi"])]):
                    period="全部历史（截至上述核验时间）" if job["unlimited"] else f"{job['start']} ～ {job['end']} 时间段"
                    info+=f" [{a['region']}/{a['name']}] 在 {period}已有完整数据。"
                    default="skip"
                elif not anchor["terminal"] and (job["unlimited"] or job["lo"]<low):
                    n=max(1,anchor["page"]-1)
                    prev=conn.execute("SELECT input_offset FROM history_coverage WHERE account_id=? AND chain=? AND page=?",
                                      (aid,anchor["chain"],n)).fetchone()
                    if n==1 or (prev and prev[0] is not None):
                        starts["bridge"]=dict(page=n,offset=prev[0] if prev else "",chain=anchor["chain"],mode="bridge")
                    elif anchor["output_offset"] or (same and not state["backfill_done"]):
                        starts["bridge"]=dict(page=anchor["page"]+1,offset=anchor["output_offset"],chain=anchor["chain"],mode="bridge")
                        info+=" 旧库仅保留下一页的断点，可从该位置直接续接。"
                    if "bridge" in starts:
                        default="bridge"
                    if anchor["page"]==1 and (anchor["output_offset"] or "").strip():
                        starts["after_first"]=dict(page=2,offset=anchor["output_offset"],chain=anchor["chain"],mode="bridge")
                        if 0<=time.time()-high<=86400:
                            default="after_first"
                        else:
                            info+=" 此游标已超过 24 小时；如平台拒绝旧游标，本轮会暂停。"
                    if anchor["output_offset"] and anchor["page"] > 1:
                        starts["continue"] = dict(page=anchor["page"]+1, offset=anchor["output_offset"],
                                                  chain=anchor["chain"], mode="bridge")
                        default="continue"
                    elif "after_first" in starts:
                        default="after_first"
                    if not job["unlimited"] and job["hi"]-1>high:
                        info+=" 续接仅向更早补采；核验时间之后的发文可用最新增量补齐。"
                else:
                    info+=" 请求含尚未覆盖的较新时段，从第 1 页才能核验。"
            if same and not cover and default != "skip":
                starts["resume"]=dict(page=state["next_backfill_page"],offset=state["backfill_offset"],
                                      chain=state["backfill_chain"],mode="resume")
                default="resume"
            if (not cover and not same and state and state["backfill_done"] and anchor
                    and state["backfill_scope"]==scope_of(job) and not job["unlimited"]
                    and job["lo"]>=anchor["from_ts"] and job["hi"]-1>anchor["to_ts"]):
                default="skip"
                info+=" 上次已补到当时的最新位置；默认跳过，可用增量检查后续新发文。"
            starts["skip"]=dict(mode="skip")
            newer_only=bool(anchor and not cover and not job["unlimited"]
                            and anchor["from_ts"]<=job["lo"]<=anchor["to_ts"]<job["hi"]-1)
            if newer_only and default != "skip":
                # When the missing dates are above the cursor, an older continuation
                # cannot reach them. Walk from the head only to the known boundary.
                starts["newer"] = dict(page=1, offset="", chain="", mode="newer", stop_at=anchor["to_ts"])
                if default == "resume":
                    starts["resume"]["stop_at"] = anchor["to_ts"]
                else:
                    default = "newer"
                info += " 新发部分从最新页向前抓，遇到已覆盖段即停。"
            has_articles = conn.execute("SELECT 1 FROM articles WHERE book_id=? LIMIT 1", ("mp:"+aid,)).fetchone() is not None
            quote = budget(conn, aid, state, anchor, intervals, job, starts, default, official)
            out.append(dict(budget=quote, has_articles=has_articles, id=aid,region=a["region"],name=a["name"],info=info,starts=starts,
                            newer_only=newer_only,default=default))
    return out

def begin(conn,aid,selection,scope):
    mode=selection["mode"]
    if mode=="resume":
        return
    page,old=selection["page"],selection["chain"]
    chain=uuid.uuid4().hex
    if mode=="bridge" and page>1:
        conn.execute("""INSERT INTO history_coverage
            SELECT account_id,?,page,input_offset,output_offset,from_ts,to_ts,terminal FROM history_coverage
            WHERE account_id=? AND chain=? AND page<?""",(chain,aid,old,page))
    conn.execute("DELETE FROM page_seen WHERE account_id=? AND action='history'",(aid,))
    conn.execute("""UPDATE account_state SET backfill_done=0,backfill_scope=?,next_backfill_page=?,
                 backfill_offset=?,backfill_chain=? WHERE account_id=?""",
                 (scope,page,selection["offset"],chain,aid))
