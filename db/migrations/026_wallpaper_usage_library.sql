-- UP:
-- 壁纸库口径修正（2026-11-18）:
-- ① backfilled=1 表示「清理副本/补历史」时写入的壁纸库条目:
--    只参与壁纸库统计, 不出现在「最近设置」里 (那栏只显示真正点过「设为壁纸」的记录)。
-- ② 旧记录 used_at 用的是 SQLite 的 UTC 默认值, 界面显示的比本地时间早 8 小时,
--    这里一次性转成本地时间。新记录由 record_wallpaper_usage 显式写本地时间。
ALTER TABLE wallpaper_usage ADD COLUMN backfilled INTEGER NOT NULL DEFAULT 0;
UPDATE wallpaper_usage SET used_at = datetime(used_at, 'localtime')
 WHERE used_at <= datetime('now');
CREATE INDEX IF NOT EXISTS idx_wallpaper_usage_backfill
    ON wallpaper_usage(backfilled, used_at);

-- DOWN:
-- 使用记录属于用户历史，不自动删除。
