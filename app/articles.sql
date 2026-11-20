-- SPDX-License-Identifier: Apache-2.0
-- Copyright 2026 adrcjwyf

CREATE TABLE IF NOT EXISTS articles (
    review_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    publish_at INTEGER NOT NULL DEFAULT 0,
    original_id TEXT NOT NULL DEFAULT '',
    read_num INTEGER NOT NULL DEFAULT 0,
    like_num INTEGER NOT NULL DEFAULT 0,
    author TEXT NOT NULL DEFAULT '',
    content_html TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    fetched_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_book_publish ON articles(book_id, publish_at DESC);
