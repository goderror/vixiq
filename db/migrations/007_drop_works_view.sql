-- UP:
-- =============================================================
-- 移除兼容视图 works (旧 Tkinter 版遗留, 浏览器版重构后全项目无引用)。
-- 背景 (2026-09-10 审计): 001 建库时创建了只读视图 works, 但代码从不查询
-- (所有 SQL 都 FROM artworks)。留着会让每个新数据库都重复建一个死对象。
-- 双头治理: 001 不再创建(新库干净) + 本迁移对既有库 DROP(旧库清理)。
-- 001 已应用的新库走到这里时视图本来就不存在, DROP IF EXISTS 无副作用。
-- =============================================================
DROP VIEW IF EXISTS works;

-- DOWN:
-- 回滚 v007 手工执行(仅旧库需要重建视图时):
--   CREATE VIEW IF NOT EXISTS works AS
--     SELECT artwork_id, total_pages, exist_list, status, last_check FROM artworks;