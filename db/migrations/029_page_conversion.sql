-- UP:
-- 2026-11-21 4B-1: 入库静态图片统一转 PNG, 记录转换来源
-- 页标识 = (artwork_id, page_num), 记录随页保存; 整理/改名只更新 local_path, 不改这些列。
ALTER TABLE pages ADD COLUMN source_format TEXT;        -- 原格式 (jpg/jpeg/webp/...)
ALTER TABLE pages ADD COLUMN source_file_size INTEGER;  -- 转换前文件大小
ALTER TABLE pages ADD COLUMN source_md5 TEXT;           -- 转换前原始指纹 (重复导入识别用)
ALTER TABLE pages ADD COLUMN converted_at TEXT;         -- 转换时间 (本地时间)
ALTER TABLE pages ADD COLUMN current_format TEXT;       -- 当前文件格式 (png/gif/webp/...)
ALTER TABLE pages ADD COLUMN convert_note TEXT;         -- 转换失败/保持原格式的原因

CREATE INDEX IF NOT EXISTS idx_pages_source_md5 ON pages(source_md5);

-- DOWN:
-- 转换记录属于入库结果, 不自动回滚; 如需回退请用数据库备份。
