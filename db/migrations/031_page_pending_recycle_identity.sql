-- UP:
-- 2026-11-22 4B-2 返修: 待回收旧 PNG 必须带内容指纹, 重试只处理已确认的那一份
ALTER TABLE pages ADD COLUMN pending_recycle_md5 TEXT;
ALTER TABLE pages ADD COLUMN pending_recycle_size INTEGER;

-- DOWN:
-- 回收身份属于用户数据, 不自动回滚; 如需回退请用数据库备份。
