-- UP:
-- 2026-11-21 4B-2: 按需转为 JPG 的格式选择与待回收旧 PNG
ALTER TABLE pages ADD COLUMN format_choice TEXT;    -- 'png' / 'jpg' (用户当前选择的格式)
ALTER TABLE pages ADD COLUMN pending_recycle TEXT;  -- 转 JPG 后待送回收站的旧 PNG 相对路径

-- DOWN:
-- 格式选择属于用户数据, 不自动回滚; 如需回退请用数据库备份。
