-- UP:
-- 标签状态（2026-11-19）: active=正常 / disabled=已停用。
-- 停用只停止出现在主列表和默认筛选推荐，不改 artwork_tags 关联，可在「已停用」恢复。
ALTER TABLE tags ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
CREATE INDEX IF NOT EXISTS idx_tags_status ON tags(status);

-- DOWN:
-- 标签状态属于用户整理结果，不自动回滚。
