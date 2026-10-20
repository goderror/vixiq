-- UP:
-- =============================================================
-- 「应用内回收站」软删除字段。
-- 背景: 旧实现删作品/删单图 = DELETE FROM + 文件 unlink, 误删不可恢复。
--   采用双层保护: ①「删除」只做应用内软删除(标记 + 文件搬进 data_root/trash/);
--   ② 在回收站里「彻底删除」才卸载 DB 记录, 且文件只送 **Windows 电脑回收站**。
-- 语义 (严格, 与 011 的 deleted_at 分离):
--   deleted_at  = 客观事实「pixiv 作品页已 404」, 只针对确认的 pixiv 作品;
--   trashed_at  = 主动删除的标记, 任何来源的作品/页都可能有。
--   两者互不替代, 不得混用。
--   trashed_reason 由系统判定写入 ('user' 用户删除 / 'cascade' 连带 / 'cleanup' 清理),
--   不开放给用户填写。
--   pages.local_path **保持原值不动** —— 它是"恢复时该搬回哪里"的权威答案;
--   pages.trash_rel_path 记录文件当前在 data_root/trash/ 下的相对位置
--   (软删只搬文件不改 local_path → 恢复 = 按 local_path 搬回, 一处 DB 改动都不必做)。
-- 物理隔离: 回收站目录 data_root/trash/ 在 img/ **之外** —— 这样对账/自检/散落图整理
--   的 rglob 扫描天然看不到回收站, 不会被误认为散落图或被当孤儿清掉。
-- =============================================================
ALTER TABLE artworks ADD COLUMN trashed_at TEXT;
ALTER TABLE artworks ADD COLUMN trashed_reason TEXT;
ALTER TABLE pages ADD COLUMN trashed_at TEXT;
ALTER TABLE pages ADD COLUMN trash_rel_path TEXT;

-- 常用判据 (图库列表默认过滤 trashed_at IS NULL) 建索引避免全表扫
CREATE INDEX IF NOT EXISTS idx_artworks_trashed ON artworks(trashed_at);
CREATE INDEX IF NOT EXISTS idx_pages_trashed ON pages(trashed_at);

-- DOWN:
-- 回滚 v016 手工执行:
--   DROP INDEX IF EXISTS idx_pages_trashed;
--   DROP INDEX IF EXISTS idx_artworks_trashed;
--   ALTER TABLE pages DROP COLUMN trash_rel_path;
--   ALTER TABLE pages DROP COLUMN trashed_at;
--   ALTER TABLE artworks DROP COLUMN trashed_reason;
--   ALTER TABLE artworks DROP COLUMN trashed_at;
