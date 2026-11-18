-- UP:
-- 标签维度与分区（2026-11-19）:
--   维度是标签的受控类别; 维度可选绑定一个磁盘目录, 绑定了就形成「分区」。
--   一个标签可以属于多个维度（category_tags 多对多），标签本身全局只存一份。
CREATE TABLE IF NOT EXISTS tag_categories (
    category_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    dir_name    TEXT NOT NULL DEFAULT '',   -- 空 = 纯逻辑维度; 非空 = 分区目录
    sort_order  INTEGER NOT NULL DEFAULT 0, -- 多个分区维度命中时的优先级
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
INSERT OR IGNORE INTO tag_categories(category_id, name, dir_name, sort_order) VALUES
    ('character',     '角色',   '', 0),
    ('style',         '风格',   '', 1),
    ('uncategorized', '未分类', '', 2);

-- 标签质量: normal=正常 / low=低频或单字符 / noise=收藏数等噪音
ALTER TABLE tags ADD COLUMN quality TEXT NOT NULL DEFAULT 'normal';
-- 规范化名字: 合并同义/大小写/全半角时用; 迁移只做简单回填, 唯一性由应用层保证
ALTER TABLE tags ADD COLUMN normalized_name TEXT;
UPDATE tags SET normalized_name = lower(trim(tag_name))
 WHERE normalized_name IS NULL OR normalized_name = '';

CREATE TABLE IF NOT EXISTS category_tags (
    category_id TEXT NOT NULL,
    tag_id      INTEGER NOT NULL,
    PRIMARY KEY (category_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_category_tags_tag ON category_tags(tag_id, category_id);

-- 现有标签先全部归入「未分类」; 用户再逐步分配到角色/风格等维度
INSERT OR IGNORE INTO category_tags(category_id, tag_id)
SELECT 'uncategorized', tag_id FROM tags;

-- DOWN:
-- 维度与关联属于用户整理结果，不自动删除。
