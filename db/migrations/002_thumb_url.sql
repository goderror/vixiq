-- UP:
-- =============================================================
-- P4c 图库完善: artworks 表加 thumb_url 列, 存作品缩略图直链,
-- 供图库网格展示图片 (无需重新抓取即可显示)。
-- =============================================================
ALTER TABLE artworks ADD COLUMN thumb_url TEXT;

-- DOWN:
-- 回滚 v002 手工执行:
--   ALTER TABLE artworks DROP COLUMN thumb_url;
