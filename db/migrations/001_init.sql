-- UP:
-- =============================================================
-- P1 初始化完整版: 8 核心表 + 索引 + FTS5 全文索引 + works/pages 兼容视图
-- 旧 works/pages → 新 artworks/pages 的迁移在 MigrationManager._bootstrap_legacy()
-- Python 代码中提前完成 (避免 SQL 层面引用不存在的表)。
-- =============================================================
PRAGMA foreign_keys = ON;

-- 版本表
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===================== artworks (原 works, 扩充字段) =====================
CREATE TABLE IF NOT EXISTS artworks (
    artwork_id       TEXT PRIMARY KEY,
    -- 原 works 字段, 100% 兼容旧调用
    total_pages      INTEGER CHECK (total_pages IS NULL OR total_pages >= 0),
    exist_list       TEXT,
    status           INTEGER DEFAULT 0 CHECK (status IN (0, 1, 2, 404)),
    last_check       TEXT,
    -- 新增: 作品元数据
    author_id        TEXT,
    title            TEXT,
    illust_type      INTEGER DEFAULT 0,
    tags_json        TEXT,
    bookmark_count   INTEGER DEFAULT 0,
    publish_date     TEXT,
    is_r18           INTEGER DEFAULT 0 CHECK (is_r18 IN (0, 1)),
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);

-- 兼容视图 works 已在迁移 007 全部移除: 浏览器版重构后全项目无任何引用,
-- 保留只会让每个新数据库都重复建一个死视图 (2026-09-10 审计定论)。

-- ===================== pages (扩充) =====================
CREATE TABLE IF NOT EXISTS pages (
    artwork_id      TEXT,
    page_num        INTEGER CHECK (page_num >= 0),
    status          INTEGER DEFAULT 0 CHECK (status IN (0, 1, -1)),
    local_path      TEXT,
    md5             TEXT,
    width           INTEGER,
    height          INTEGER,
    file_size_bytes INTEGER,
    PRIMARY KEY (artwork_id, page_num),
    FOREIGN KEY (artwork_id) REFERENCES artworks(artwork_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

-- ===================== authors =====================
CREATE TABLE IF NOT EXISTS authors (
    author_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    avatar_url      TEXT,
    followers       INTEGER DEFAULT 0,
    following       INTEGER DEFAULT 0,
    twitter         TEXT,
    bio             TEXT,
    pixiv_url       TEXT,
    first_seen      TEXT DEFAULT (datetime('now')),
    last_updated    TEXT DEFAULT (datetime('now'))
);

-- ===================== tags / artwork_tags =====================
CREATE TABLE IF NOT EXISTS tags (
    tag_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name    TEXT NOT NULL UNIQUE,
    tag_name_zh TEXT,
    tag_name_en TEXT
);

CREATE TABLE IF NOT EXISTS artwork_tags (
    artwork_id  TEXT NOT NULL,
    tag_id      INTEGER NOT NULL,
    PRIMARY KEY (artwork_id, tag_id),
    FOREIGN KEY (artwork_id) REFERENCES artworks(artwork_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)    REFERENCES tags(tag_id)       ON DELETE CASCADE
);

-- ===================== 反搜历史 =====================
CREATE TABLE IF NOT EXISTS reverse_search_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    input_image_path    TEXT NOT NULL,
    input_md5           TEXT,
    confidence          REAL,
    search_source       TEXT,
    hit_author_id       TEXT,
    hit_artwork_id      TEXT,
    hit_page_num        INTEGER,
    manually_corrected  INTEGER DEFAULT 0 CHECK (manually_corrected IN (0, 1)),
    raw_response_json   TEXT,
    searched_at         TEXT DEFAULT (datetime('now')),
    UNIQUE (input_image_path, search_source)
);

-- ===================== 购物车 / 下载清单 持久化 =====================
CREATE TABLE IF NOT EXISTS download_queue_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type       TEXT NOT NULL,
    title           TEXT,
    author_id       TEXT,
    author_name     TEXT,
    artwork_id      TEXT,
    page_count      INTEGER,
    strategy        TEXT DEFAULT 'only_new',
    status          TEXT DEFAULT 'pending',
    added_at        TEXT DEFAULT (datetime('now')),
    source_note     TEXT
);

-- ===================== 索引 =====================
CREATE INDEX IF NOT EXISTS idx_pages_artwork     ON pages(artwork_id);
CREATE INDEX IF NOT EXISTS idx_pages_md5         ON pages(md5);
CREATE INDEX IF NOT EXISTS idx_artworks_author   ON artworks(author_id, publish_date DESC);
CREATE INDEX IF NOT EXISTS idx_artworks_status   ON artworks(status);
CREATE INDEX IF NOT EXISTS idx_artwork_tags_tag  ON artwork_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_rev_search_md5    ON reverse_search_history(input_md5);
CREATE INDEX IF NOT EXISTS idx_download_status   ON download_queue_items(status);

-- ===================== FTS5 全文索引 =====================
CREATE VIRTUAL TABLE IF NOT EXISTS artwork_fts USING fts5(
    title, tag_list, author_name, content=''
);

-- DOWN:
-- 回滚 v001 手工执行:
--   DROP VIEW IF EXISTS works;
--   DROP TABLE IF EXISTS schema_migrations, download_queue_items, reverse_search_history,
--       artwork_tags, tags, authors, pages, artworks, artwork_fts;
