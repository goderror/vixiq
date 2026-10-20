-- UP:
-- =============================================================
-- 作者曾用名: authors 表加 aliases_json 列 (JSON 数组, 存历次改名前的旧昵称)。
-- pixiv 作者常改名; 自检 _refresh_author_names → upsert_author 检测到昵称
-- 变化时, 把旧名【追加】进该数组 (去重、不含当前名、绝不覆盖), 改名历史永久
-- 保留。前端图库作者筛选下拉据此显示「曾用名」并支持按旧名检索到该作者。
-- =============================================================
ALTER TABLE authors ADD COLUMN aliases_json TEXT;

-- DOWN:
-- 回滚 v004 手工执行:
--   ALTER TABLE authors DROP COLUMN aliases_json;
