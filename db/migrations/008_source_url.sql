-- UP:
-- =============================================================
-- 来源原始 URL: artworks 加 source_url 列。
-- 背景 : 非 pixiv 来源(其他/未知)治理需要记录来源页原始 URL,
-- 支持手动填充/修改; 「搜索来源」流程 = 先解析已记录 URL 归类(快) → 无 URL 才反搜。
-- pixiv 来源作品本就有作品页 URL(artwork_id 即 ID), 该列主要给外站/未知来源用,
-- 但 pixiv 作品也可记录(如作者主页链接), 不设非空约束。
-- =============================================================
ALTER TABLE artworks ADD COLUMN source_url TEXT;

-- DOWN:
-- 回滚 v008 手工执行:
--   ALTER TABLE artworks DROP COLUMN source_url;