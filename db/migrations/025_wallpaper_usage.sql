-- UP:
-- 壁纸使用记录（2026-11-18）:
--   「设为壁纸」分两种：裁剪版会复制进壁纸夹，原图只引用不复制。
--   wallpaper_files 只记壁纸夹里实际存在的文件，原图不该塞进去（会破坏移出/回收等
--   以「夹内相对名」为前提的操作）。这里单独记「设置过哪些壁纸」，原图与裁剪版都登记。
CREATE TABLE IF NOT EXISTS wallpaper_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    artwork_id  TEXT,
    page_num    INTEGER,
    file_path   TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'library',   -- library=图库原图 / crop=裁剪版 / wall=壁纸夹已有
    used_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wallpaper_usage_used ON wallpaper_usage(used_at);

-- DOWN:
-- 使用记录属于用户历史，不自动删除。
