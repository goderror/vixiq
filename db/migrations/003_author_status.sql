-- UP:
-- =============================================================
-- 作者存续状态: authors 表加 status 列 ('alive' 作者活跃中 /
-- 'gone' 作者已注销 / NULL 未知未核对)。
-- 数据来源: 自检「作者名刷新」逐个核对 /ajax/user/ 时顺手写入;
-- 一次性回填脚本把已删除占位作品解析出的分类写入。
-- 图库「状态」筛选用: 作者活跃中 / 作者已注销 / 未知或无作者。
-- =============================================================
ALTER TABLE authors ADD COLUMN status TEXT;
ALTER TABLE artworks ADD COLUMN source TEXT;

-- DOWN:
-- 回滚 v003 手工执行:
--   ALTER TABLE authors DROP COLUMN status;
--   ALTER TABLE artworks DROP COLUMN source;
