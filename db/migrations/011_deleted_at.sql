-- UP:
-- =============================================================
-- 「作品已删除」从标题文案升级为数据库字段: artworks.deleted_at。
-- 背景: 旧实现把「pixiv 作品页 404」这个**客观事实**
-- 写进标题占位文案「（作品已删除 xxx）」，再用 `title LIKE '（作品已删除%'` 当筛选条件 ——
-- 结果是"改一下标题就变成已删除 / 就又不是已删除"，语义完全不可靠。
-- 语义 (严格): deleted_at 非空 = 该作品在 pixiv 的作品页已被确认不存在(404/已删除),
--   只可能针对「确认是 pixiv 来源 + 有 pid/作者」的作品; 非 pixiv 作品(ext_/merged_)永不标记。
-- 存量回填: 把旧占位标题里的事实搬到字段 (只认 pixiv 数字作品 ID —— 非 pixiv 的
-- ext_/merged_ 本地壳作品没有 pixiv 作品页, 不该有"已删除"语义);
-- 占位标题本身由 repair_id_titles() 换成可读标题。
ALTER TABLE artworks ADD COLUMN deleted_at TEXT;

UPDATE artworks SET deleted_at = datetime('now')
 WHERE deleted_at IS NULL AND title LIKE '（作品已删除%'
   AND artwork_id NOT GLOB '*[^0-9]*';

-- DOWN:
-- 回滚 v011 手工执行:
--   ALTER TABLE artworks DROP COLUMN deleted_at;
