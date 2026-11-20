# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
"""Filtered review pages and streaming exports from the same stored decisions."""
import csv
import io
import json
import re
from math import ceil
from urllib.parse import urlencode, quote
from collection import date_bound
from monetary_topic import PAGE_SIZE, TIERS, SELECTED, POSSIBLE, reading_link, original_link, highlight, BUSINESS_SIGNAL

FIELDS=("region","account_name","biz","sn","title","digest","content_text","url","publish_at","publish_date","original","position")
HEADERS=("区域","公众号名称","biz","sn","title","digest","content_text","url","publish_at","publish_date","original","position")

def filters(query, *, by_tier=True):
    selected={k:query.get(k,[""])[0] for k in ("region","account","start","end")}
    terms,values=[],[]
    for name,col,op in (("account","account_name","="),("region","region","="),("start","publish_at",">="),("end","publish_at","<")):
        if selected[name]:
            terms.append(col+op+"?")
            values.append(selected[name] if name in ("account","region") else date_bound(selected[name],name=="end"))
    if selected["start"] and selected["end"] and selected["start"]>selected["end"]:
        raise ValueError("开始日期应早于或等于结束日期。")
    if by_tier:
        tier=query.get("decision",[""])[0]
        if tier:
            if tier not in TIERS:
                raise ValueError("请选择有效的初筛档位。")
            selected["decision"]=tier
            terms.append("decision=?")
            values.append(tier)
    return selected,terms,values

def article_view(app,respond,query,topic=False):
    selected,terms,values=filters(query,by_tier=not topic)
    params={k:v for k,v in selected.items() if v}
    scope_qs=urlencode({k:v for k,v in params.items() if k!="decision"})
    groups=[]
    with app.db.transaction() as db:
        db.execute("BEGIN")
        regions=[r[0] for r in db.execute("SELECT DISTINCT account_name FROM articles ORDER BY account_name")]
        sections = ((SELECTED, SELECTED, "page"), (POSSIBLE, "疑似相关 / 重点待核", "review_page")) if topic else ((None, "", "page"),)
        for tier, heading, key in sections:
            conditions=terms+["decision=?"] if tier else terms
            args=values+[tier] if tier else values
            where=" WHERE "+" AND ".join(conditions) if conditions else ""
            total=db.execute("SELECT count(*) FROM articles"+where,args).fetchone()[0]
            pages=max(1,ceil(total/PAGE_SIZE))
            page=max(1,min(int(query.get(key,["1"])[0]),pages))
            order = "publish_at DESC,review_id"
            if tier == POSSIBLE:
                order = "(category='重点待核') DESC," + order
            rows=db.execute("SELECT * FROM articles"+where+" ORDER BY "+order+" LIMIT ? OFFSET ?",
                            [*args,PAGE_SIZE,(page-1)*PAGE_SIZE])
            items=[dict(row=dict(r),link=reading_link(r),decision=(r["decision"],r["category"],r["reason"])) for r in rows]
            groups.append(dict(tier=tier,heading=heading,key=key,total=total,page=page,pages=pages,rows=items))
    for g in groups:
        keep={other["key"]:other["page"] for other in groups if other is not g and other["page"]>1}
        g["previous"]=urlencode(params|keep|{g["key"]:g["page"]-1})
        g["next"]=urlencode(params|keep|{g["key"]:g["page"]+1})
    return app._render(respond,"topic.html" if topic else "all.html",groups=groups,tiers=TIERS,topic=topic,
        article_query=urlencode(params | {g["key"]:g["page"] for g in groups if g["page"]>1} | {"from":"topic" if topic else "all"}),
        regions=regions,f=selected,qs=urlencode(params),scope_qs=scope_qs)

def csvline(values):
    out=io.StringIO(newline="")
    safe=[]
    for v in values:
        s="" if v is None else str(v)
        safe.append("'"+s if s.lstrip().startswith(("=","+","-","@")) or s[:1] in ("\t","\r","\n") else s)
    csv.writer(out).writerow(safe)
    return out.getvalue().encode("utf-8")

def stream_rows(conn,as_csv,query):
    _,terms,values=filters(query)
    where=" WHERE "+" AND ".join(terms) if terms else ""
    yield b"\xef\xbb\xbf"+csvline((*HEADERS,"判定","类别","原因","正文状态","原始记录JSON")) if as_csv else b"["
    for i,r in enumerate(conn.execute("SELECT * FROM articles"+where+" ORDER BY publish_at DESC,review_id",values)):
        coverage="已读取正文" if r["coverage"]=="full" else "待处理" if r["coverage"]=="new" else "仅标题与摘要"
        if as_csv:
            yield csvline([*(r[k] for k in FIELDS),r["decision"],r["category"],r["reason"],coverage,r["raw_json"]])
        else:
            doc={k:r[k] for k in FIELDS}
            doc.update(article=json.loads(r["raw_json"]),decision=r["decision"],category=r["category"],reason=r["reason"],coverage=coverage)
            yield ((",\n" if i else "")+json.dumps(doc,ensure_ascii=False)).encode()
    if not as_csv:
        yield b"]"

def export_list(app,respond,as_csv,query):
    filters(query)
    ext="csv" if as_csv else "json"
    respond("200 OK",[("Content-Type",("text/csv" if as_csv else "application/json")+"; charset=utf-8"),
                      ("Content-Disposition",f'attachment; filename="articles.{ext}"'),("Cache-Control","no-store")])
    def stream():
        with app.db.transaction() as db:
            yield from stream_rows(db,as_csv,query)
    return stream()



def md_cell(text):
    text=" ".join((text or "").split())
    return (text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            .replace("\\","\\\\").replace("|","\\|").replace("[","\\[").replace("]","\\]"))

def clip(text, limit):
    text=" ".join((text or "").split())
    return text if len(text)<=limit else text[:limit-1]+"…"

def ai_excerpt(row):
    digest=clip(row["digest"],120)
    full_text=row["content_text"] or ""
    body=clip(full_text,220)
    excerpt=[]
    if digest:
        excerpt.append("摘要："+digest)
    if body:
        excerpt.append("正文："+body)
        evidence=clip(row["excerpt"],100)
        if evidence and evidence.strip("…") not in body:
            excerpt.append("相关句："+evidence)
    elif digest:
        excerpt.append("缺正文")
    return "；".join(excerpt) or "仅标题，缺摘要及正文"

def export_markdown(app,respond,query,*,full):
    selected,terms,values=filters(query,by_tier=False)
    label="AI 全量防漏稿（全部文章）" if full else "AI 复核稿（入选 + 疑似）"
    if not full:
        terms.append("decision IN (?,?)")
        values.extend((SELECTED,POSSIBLE))
    where=" WHERE "+" AND ".join(terms) if terms else ""
    filename=quote(label+".md",safe="")
    fallback="ai-all.md" if full else "ai-review.md"
    respond("200 OK",[("Content-Type","text/markdown; charset=utf-8"),("Cache-Control","no-store"),
                      ("Content-Disposition",f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{filename}")])
    def stream():
        with app.db.transaction() as conn:
            conn.execute("BEGIN")
            total=conn.execute("SELECT count(*) FROM articles"+where,values).fetchone()[0]
            rules=app.db.read_rules(conn)
            heading=(f"# {md_cell(rules['name'])} · {label}\n\n{rules['prompt']}\n\n"
                     f"范围：{md_cell(selected['account'] or selected['region']) or '全部公众号'}；{selected['start'] or '不限起日'} ～ "
                     f"{selected['end'] or '不限止日'}；共 {total} 篇。\n\n"
                     "| 编号 | 区域 | 日期 | 初筛结论 | 标题 | 摘要/正文节选 | 原文链接 |\n"
                     "|---:|---|---|---|---|---|---|\n")
            yield heading.encode("utf-8")
            for i,r in enumerate(conn.execute("SELECT * FROM articles"+where+" ORDER BY publish_at DESC,review_id",values),1):
                link=original_link(r)
                url="<"+quote(link,safe=":/?#&=%+;@,-._~")+">" if link else "缺原文链接"
                cells=[str(i),md_cell(r["region"]),md_cell((r["publish_date"] or "")[:10]),
                       md_cell(r["decision"]),md_cell(r["title"]),md_cell(ai_excerpt(r)),url]
                yield ("| "+" | ".join(cells)+" |\n").encode("utf-8")
    return stream()
