-- UP:
-- 图库分类（2026-11-18 A 阶段）:
--   分类是用户可配置的顶层目录，作品记住自己属于哪个分类；
--   作者夹永远建在分类目录下。初始两条与现有磁盘目录一致（先不改名）。
CREATE TABLE IF NOT EXISTS library_sections (
    section_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    dir_name    TEXT NOT NULL,
    match_json  TEXT NOT NULL DEFAULT '{}',
    is_default  INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO library_sections
    (section_id, name, dir_name, match_json, is_default, sort_order)
VALUES
    ('general', '普通', 'X图', '{"is_r18": 0}', 1, 0),
    ('r18',     'R18',  '涩图', '{"is_r18": 1}', 0, 1);

ALTER TABLE artworks ADD COLUMN section_id TEXT;

-- 按现有顶层目录回填：X图 → 普通，涩图 → R18；其余留空（整理时按默认分类处理）
UPDATE artworks SET section_id = 'general'
 WHERE COALESCE(section_id, '') = ''
   AND EXISTS (SELECT 1 FROM pages p
                WHERE p.artwork_id = artworks.artwork_id
                  AND p.trashed_at IS NULL
                  AND p.local_path LIKE 'X图/%');
UPDATE artworks SET section_id = 'r18'
 WHERE COALESCE(section_id, '') = ''
   AND EXISTS (SELECT 1 FROM pages p
                WHERE p.artwork_id = artworks.artwork_id
                  AND p.trashed_at IS NULL
                  AND p.local_path LIKE '涩图/%');

CREATE INDEX IF NOT EXISTS idx_artworks_section ON artworks(section_id);

-- DOWN:
-- 分类字段与配置属于用户整理结果，不自动回滚或删除。
