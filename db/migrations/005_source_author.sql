-- UP:
-- =============================================================
-- 非 pixiv 来源溯源: artworks 加 source_author 列。
-- 文件名/文件夹名约定「来源@源站账号」(如 twi@qing0ying0、bili@徐尤点、
-- GGAC@毒瘤), 站点键 → artworks.source, @ 后账号 → source_author 落库。
-- 写入一律「只增不覆盖」: 已有值绝不动, 只补空值 (用户手动指定优先)。
-- =============================================================
ALTER TABLE artworks ADD COLUMN source_author TEXT;

-- DOWN:
-- 回滚 v005 手工执行:
--   ALTER TABLE artworks DROP COLUMN source_author;
