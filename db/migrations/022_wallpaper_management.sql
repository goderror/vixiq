-- UP:
ALTER TABLE wallpaper_files ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;
ALTER TABLE wallpaper_files ADD COLUMN labels_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE wallpaper_files ADD COLUMN group_id INTEGER;
CREATE TABLE wallpaper_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE wallpaper_trash (
    id TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    original_dir TEXT NOT NULL,
    trash_rel TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL,
    trashed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_wallpaper_files_group ON wallpaper_files(group_id);

-- DOWN:
-- 新列与回收记录包含用户整理结果，不自动回滚或删除。
