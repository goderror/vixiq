-- UP:
-- 历史 pages.local_path 有两种分隔符（X图/... 与 X图\...），
-- 023 只按 `/` 回填，导致大量作品 section_id 为空。这里统一成正斜杠再补回填。
UPDATE pages SET local_path = REPLACE(local_path, '\', '/')
 WHERE local_path LIKE '%\%';

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

-- DOWN:
-- 路径分隔符规范化与分类回填都是修复性操作，不自动回滚。
