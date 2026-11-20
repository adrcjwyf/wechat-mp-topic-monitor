# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 adrcjwyf
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from topic_engine import clean_visible
from __version__ import PROJECT_NAME, AUTHOR, REPO_URL, SCHEMA_VERSION
from monetary_topic import TIERS
from topic_engine import classify_record, presets, revision, serialize, validate_rules
from datetime import timezone, timedelta
from decimal import Decimal

CN = timezone(timedelta(hours=8))
from urllib.parse import urlsplit, parse_qs

import history_coverage
from configuration import Config
from platform_source import article_url

def published(item):
    try:
        n = int(item.get("post_time") or 0)
        if n > 10**12:
            n //= 1000
        if 0 < n < 32503680000:
            return n
        return int(datetime.fromisoformat(str(item.get("post_time_str"))).replace(tzinfo=CN).timestamp())
    except (ValueError, OSError, OverflowError, TypeError):
        return 0

class Store:
    def __init__(self, data):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.config = Config(self.data)
        self.presets = presets()
        self.default_rules = self.presets['general_blank']
        with self.transaction() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(Path(__file__).with_name("articles.sql").read_text("utf-8"))
            fields = {r[1] for r in db.execute("pragma table_info(articles)")}
            if "source_note" not in fields:
                db.execute("ALTER TABLE articles ADD COLUMN source_note TEXT NOT NULL DEFAULT ''")
            if "coverage" not in fields:
                db.execute("ALTER TABLE articles ADD COLUMN coverage TEXT NOT NULL DEFAULT 'new'")
                db.execute("UPDATE articles SET coverage='full' WHERE content_html<>''")
            if "announced" not in fields:
                db.execute("ALTER TABLE articles ADD COLUMN announced INTEGER NOT NULL DEFAULT 0")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS history_pages(id INTEGER PRIMARY KEY, biz TEXT NOT NULL,
                    checked_at INTEGER NOT NULL, page INTEGER NOT NULL, response_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS articles_source_url ON articles(book_id,url);
                CREATE INDEX IF NOT EXISTS articles_source_sn ON articles(book_id,original_id);
                CREATE TABLE IF NOT EXISTS checks(biz TEXT PRIMARY KEY,status TEXT DEFAULT 'ready',
                    success INTEGER DEFAULT 0, anchor INTEGER DEFAULT 0, report TEXT DEFAULT '{}',
                    cost TEXT,balance TEXT);
                UPDATE checks SET status='interrupted' WHERE status='working';
            """)

            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_meta(k TEXT PRIMARY KEY,v TEXT);
                CREATE TABLE IF NOT EXISTS account_state(account_id TEXT PRIMARY KEY,region TEXT NOT NULL,
                  account_name TEXT NOT NULL,biz TEXT DEFAULT '', keywords TEXT DEFAULT '[]',
                  latest_post_at INTEGER DEFAULT 0, oldest_post_at INTEGER DEFAULT 0,last_sync_at INTEGER DEFAULT 0,
                  next_backfill_page INTEGER DEFAULT 1, backfill_offset TEXT DEFAULT '', backfill_scope TEXT DEFAULT '',
                  backfill_done INTEGER DEFAULT 0, total_page INTEGER, unit_cost TEXT,
                  inc_page INTEGER DEFAULT 1, inc_offset TEXT DEFAULT '', inc_cutoff INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS page_seen(account_id TEXT, action TEXT, fingerprint TEXT,
                  PRIMARY KEY(account_id,action,fingerprint));
                CREATE TABLE IF NOT EXISTS run_state(id INTEGER PRIMARY KEY CHECK(id=1), status TEXT DEFAULT 'ready',
                  report TEXT DEFAULT '{}', balance TEXT, anchor INTEGER DEFAULT 0);
                INSERT OR IGNORE INTO run_state(id) VALUES(1);
                UPDATE run_state SET status='paused' WHERE status='working';
            """)
            db.execute("BEGIN")
            history_coverage.migrate(db,published)
            additions = dict(region="TEXT",account_name="TEXT",biz="TEXT",sn="TEXT",digest="TEXT",
                content_text="TEXT",publish_date="TEXT",original="INTEGER",position="INTEGER",decision="TEXT",
                category="TEXT",reason="TEXT",excerpt="TEXT")
            for col, typ in additions.items():
                if col not in fields:
                    db.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
            db.execute("CREATE INDEX IF NOT EXISTS article_region_date ON articles(region,publish_at)")
        c=self.config.read()
        for a in c["accounts"]:
            with self.transaction() as db:
                stored=db.execute("SELECT biz FROM account_state WHERE account_id=?",(a["id"],)).fetchone()
            if stored and stored[0] and not a["biz"]:
                self.config.bind(a["id"],stored[0])
        self.sync_accounts(self.config.read())
        with self.transaction() as db:
            for r in db.execute("""SELECT book_id,max(author) FROM articles WHERE book_id NOT IN
                    (SELECT 'mp:'||account_id FROM account_state) GROUP BY book_id""").fetchall():
                aid = r[0].removeprefix("mp:")
                name = r[1] or aid
                region = "历史账号"
                db.execute("INSERT INTO account_state(account_id,region,account_name,biz,keywords) VALUES(?,?,?,?,?)",
                           (aid,region,name,aid,json.dumps([region],ensure_ascii=False)))
                self.watermarks(db,aid)
            db.executemany("INSERT OR REPLACE INTO app_meta(k,v) VALUES(?,?)",
                           (("generator",PROJECT_NAME),("author",AUTHOR),("repo_url",REPO_URL),("schema_version",str(SCHEMA_VERSION))))
            rules = self.read_rules(db)
            signature = self.engine_signature(rules)
            old = db.execute("SELECT v FROM app_meta WHERE k='rules'").fetchone()
            if not old or old[0] != signature:
                self.reclassify(db, rules)
                db.execute("INSERT OR REPLACE INTO app_meta VALUES('rules',?)", (signature,))

    def read_rules(self, db):
        record = db.execute("SELECT v FROM app_meta WHERE k='topic_config'").fetchone()
        return json.loads(record[0]) if record else self.default_rules

    def current_rules(self):
        with self.transaction() as db:
            return self.read_rules(db)

    def engine_signature(self, rules):
        digest = hashlib.sha256(serialize(rules).encode())
        for name in ("topic_engine.py","monetary_topic.py"):
            digest.update(Path(__file__).with_name(name).read_bytes())
        return digest.hexdigest()

    def reclassify(self, db, rules):
        cursor = db.execute("""SELECT a.*,s.keywords FROM articles a JOIN account_state s
            ON a.book_id='mp:'||s.account_id""")
        for row in cursor:
            self.classify(db, row, rules, json.loads(row["keywords"]))

    def save_rules(self, draft, previous):
        rules = validate_rules(draft)
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            if previous != revision(self.read_rules(db)):
                raise ValueError("词库已在另一页面更新，请刷新页面后修改。")
            db.execute("INSERT OR REPLACE INTO app_meta VALUES('topic_config',?)", (serialize(rules),))
            self.reclassify(db, rules)
            db.execute("INSERT OR REPLACE INTO app_meta VALUES('rules',?)", (self.engine_signature(rules),))
            return db.execute("SELECT count(*) FROM articles").fetchone()[0]

    def classify(self, db, row, rules, identity):
        decision = classify_record(row, rules, identity, TIERS)
        db.execute("UPDATE articles SET decision=?,category=?,reason=?,excerpt=? WHERE review_id=?",
                   (*decision, row["review_id"]))

    def sync_accounts(self,c):
        with self.transaction() as db:
            signature = hashlib.sha256(json.dumps([c["api_url"], c["key"]]).encode()).hexdigest()
            previous = db.execute("SELECT v FROM app_meta WHERE k='billing_scope'").fetchone()
            if previous is None or previous[0] != signature:
                if previous is not None:
                    db.execute("UPDATE run_state SET balance=NULL WHERE id=1")
                    db.execute("UPDATE account_state SET unit_cost=NULL,total_page=NULL")
                    db.execute("DELETE FROM app_meta WHERE k='balance_probe'")
                db.execute("INSERT OR REPLACE INTO app_meta VALUES('billing_scope',?)", (signature,))
            for a in c["accounts"]:
                aid=a["id"]
                keys=json.dumps(a["local_keywords"],ensure_ascii=False)
                prev=db.execute("SELECT * FROM account_state WHERE account_id=?",(aid,)).fetchone()
                if prev and prev["biz"] and not a["biz"]:
                    a["biz"]=prev["biz"]
                if prev and prev["biz"] and prev["biz"]!=a["biz"] and db.execute(
                        "SELECT 1 FROM articles WHERE book_id=? LIMIT 1",("mp:"+aid,)).fetchone():
                    raise ValueError("已有历史的账号不可替换为另一个 biz，请新增账号。")
                db.execute("""INSERT INTO account_state(account_id,region,account_name,biz,keywords) VALUES(?,?,?,?,?)
                  ON CONFLICT(account_id) DO UPDATE SET region=excluded.region, account_name=excluded.account_name,
                  biz=coalesce(nullif(excluded.biz,''),account_state.biz),keywords=excluded.keywords""",
                  (aid,a["region"],a["name"],a["biz"],keys))
                if not prev or (prev["region"],prev["account_name"],prev["biz"],prev["keywords"])!=(a["region"],a["name"],a["biz"],keys):
                    for row in db.execute("SELECT review_id FROM articles WHERE book_id=?",("mp:"+aid,)):
                        self.describe(db,row[0],a)
                    self.watermarks(db,aid)
                    if not prev:
                        db.execute("""UPDATE account_state SET last_sync_at=coalesce((SELECT max(success) FROM checks WHERE biz=?),0)
                                   WHERE account_id=?""",(a["biz"],aid))

    def describe(self, db, rid, a):
        db.execute("""UPDATE articles SET region=?,account_name=?,biz=?,sn=original_id,digest=summary,
          content_html=CASE WHEN coverage='full' AND coalesce(content_html,'')='' THEN coalesce(content_text,'') ELSE content_html END,
          content_text=CASE WHEN coverage='full' AND coalesce(content_text,'')<>'' THEN content_text ELSE content_html END,
          publish_date=CASE WHEN publish_at>0 THEN datetime(publish_at,'unixepoch','+8 hours') ELSE '' END,
          original=json_extract(raw_json,'$.original'),position=json_extract(raw_json,'$.position') WHERE review_id=?""",
          (a["region"],a["name"],a["biz"],rid))
        row=db.execute("SELECT * FROM articles WHERE review_id=?",(rid,)).fetchone()
        self.classify(db, row, self.read_rules(db), a["local_keywords"])

    def watermarks(self,db,aid):
        db.execute("""UPDATE account_state SET (oldest_post_at,latest_post_at)=(SELECT coalesce(min(publish_at),0),
          coalesce(max(publish_at),0) FROM articles WHERE book_id=? AND publish_at>0) WHERE account_id=?""",("mp:"+aid,aid))

    def account_state(self,aid):
        with self.transaction() as db:
            return dict(db.execute("SELECT * FROM account_state WHERE account_id=?",(aid,)).fetchone())

    def stats(self):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("""SELECT s.*,count(a.review_id) AS n,
              count(CASE WHEN a.decision='入选' THEN 1 END) AS hits FROM account_state s
              LEFT JOIN articles a ON a.book_id='mp:'||s.account_id GROUP BY s.account_id ORDER BY s.rowid""")]

    def populated_accounts(self):
        with self.transaction() as db:
            return {row[0] for row in db.execute("""SELECT s.account_id FROM account_state s
                WHERE EXISTS (SELECT 1 FROM articles a WHERE a.book_id='mp:'||s.account_id)""")}

    def run_state(self):
        with self.transaction() as db:
            s=dict(db.execute("SELECT * FROM run_state WHERE id=1").fetchone())
            s["report"]=json.loads(s["report"])
            return s

    def record_billing(self, balance, aid=None, charge=None):
        with self.transaction() as db:
            if balance is not None:
                db.execute("UPDATE run_state SET balance=? WHERE id=1", (str(balance),))
                db.execute("DELETE FROM app_meta WHERE k='balance_probe'")
            if aid is not None and charge is not None:
                db.execute("UPDATE account_state SET unit_cost=? WHERE account_id=?", (str(charge), aid))

    def arm_balance_probe(self):
        with self.transaction() as db:
            db.execute("UPDATE run_state SET balance=NULL WHERE id=1")
            db.execute("INSERT OR REPLACE INTO app_meta VALUES('balance_probe','armed')")

    def take_balance_probe(self):
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            marker = db.execute("SELECT v FROM app_meta WHERE k='balance_probe'").fetchone()
            if not marker:
                return False
            if marker[0] != "armed":
                raise ValueError("首请求未返回有效余额，请核对充值后再手动放行。")
            db.execute("UPDATE app_meta SET v='spent' WHERE k='balance_probe'")
            return True

    def report(self,status,r):
        with self.transaction() as db:
            db.execute("UPDATE run_state SET status=?,report=?,anchor=? WHERE id=1",
                (status,json.dumps(r,ensure_ascii=False),int(time.time())))

    def connect(self):
        conn = sqlite3.connect(self.data / "wechat_mp.db", timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as conn, conn:
            yield conn

    def ingest(self, db, c, doc, page):
        """Commit the complete page before starting any public article request."""
        book = "mp:" + c["id"]
        source_note = ""  # Page identity has been verified. verdict checks individual links.
        ids = {}
        skipped = 0
        known = False
        added = 0
        db.execute("INSERT INTO history_pages(biz,checked_at,page,response_json) VALUES(?,?,?,?)",
                   (c["biz"] or c["id"], int(time.time()), page, json.dumps(doc["data"], ensure_ascii=False)))
        for item in doc["data"]:
            if not isinstance(item, dict):
                skipped += 1
                continue
            link = article_url(item.get("url"))
            query = parse_qs(urlsplit(link).query)
            sn = str(item.get("sn") or query.get("sn", [""])[0]).strip()
            title = str(item.get("title") or "").strip()
            raw = json.dumps(item, ensure_ascii=False)
            fallback = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            prior = db.execute("""SELECT review_id FROM articles WHERE book_id=?
                AND ((original_id=? AND ?<>'') OR (url=? AND ?<>'')) LIMIT 1""", (book,sn,sn,link,link)).fetchone()

            rid = prior[0] if prior else hashlib.sha256((book+"|"+(sn or link or fallback)).encode()).hexdigest()
            exists = bool(prior) or db.execute("SELECT 1 FROM articles WHERE review_id=?",(rid,)).fetchone() is not None
            known |= exists and rid not in ids
            added += int(not exists)
            db.execute("""INSERT INTO articles(review_id,book_id,title,summary,url,publish_at,original_id,author,raw_json,fetched_at,source_note)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(review_id) DO UPDATE SET title=excluded.title,
                summary=excluded.summary,
                fetched_at=excluded.fetched_at, author=excluded.author,
                url=coalesce(nullif(excluded.url,''),articles.url),original_id=coalesce(nullif(excluded.original_id,''),articles.original_id),
                publish_at=coalesce(nullif(excluded.publish_at,0),articles.publish_at),raw_json=excluded.raw_json,source_note=excluded.source_note""",
                (rid,book,title or "（接口未提供标题）",clean_visible(str(item.get("digest") or "")),link,published(item),sn,c["name"],
                 raw,int(time.time()),source_note))
            self.describe(db,rid,c)
            ids[rid] = None
        return list(ids), known, added

    def row(self, rid):
        with self.transaction() as db:
            return dict(db.execute("SELECT * FROM articles WHERE review_id=?",(rid,)).fetchone())

    def claim_body(self, rid):
        with self.transaction() as db:
            return db.execute("UPDATE articles SET coverage='summary' WHERE review_id=? AND coverage='new'",(rid,)).rowcount

    def save_body(self, rid, body, a):
        with self.transaction() as db:
            changed=db.execute("UPDATE articles SET content_html=?,content_text=?,coverage='full' WHERE review_id=? AND coverage<>'full'",(body,body,rid)).rowcount
            if not changed:
                return
            self.describe(db,rid,a)

    @staticmethod
    def body_version(body):
        return hashlib.sha256((body or "").encode("utf-8")).hexdigest()

    def edit_body(self,rid,body,version):
        if not clean_visible(body):
            raise ValueError("请先粘贴正文。")
        with self.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("""SELECT a.content_html,s.* FROM articles a JOIN account_state s
                ON a.book_id='mp:'||s.account_id WHERE a.review_id=?""",(rid,)).fetchone()
            if row is None:
                raise ValueError("未找到这篇文章。")
            if self.body_version(row["content_html"])!=version:
                return False
            db.execute("UPDATE articles SET content_html=?,content_text=?,coverage='full' WHERE review_id=?",(body.strip(),body.strip(),rid))
            self.describe(db,rid,dict(region=row["region"],name=row["account_name"],biz=row["biz"],
                                      local_keywords=json.loads(row["keywords"])))
        return True

    def mark_match(self, rid):
        with self.transaction() as db:
            return db.execute("UPDATE articles SET announced=1 WHERE review_id=? AND announced=0",(rid,)).rowcount
