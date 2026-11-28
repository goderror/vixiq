-- UP:
-- 2026-11-28 下载队列行保留失败详情 (如「旧图尚未恢复」与可恢复备份路径)
ALTER TABLE download_queue_items ADD COLUMN error_note TEXT;

-- DOWN:
-- 失败详情属于用户可见的任务结果, 不自动回滚; 如需回退请用数据库备份。
