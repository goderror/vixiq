-- UP:
-- =============================================================
-- 「壁纸夹归属索引」: 壁纸夹里**每一个文件**对应图库哪一页。
--
-- 背景 (2026-11 用户反馈): 壁纸夹里很多图是从图库手动裁剪/另存出来的, 既没有作品 ID,
-- 字节也和原图不同 —— 只靠 MD5 或文件名反推, 同一作品会堆好几份而查不出来。
-- 做法: 扫一次壁纸夹, 把归属**正向落库**, 之后查重、列表、统计全部读这张表 (秒回),
-- 不再每次实时跑 ORB。
--
-- 与 018 的 wallpapers 表分工:
--   wallpapers     = 从图库**生成**的壁纸 (有裁切参数、能重现), 只登记程序产出的那些;
--   wallpaper_files = 壁纸夹里**实际存在的文件** (含用户手动放进来的历史文件),
--                     一行 = 一个文件, 归属是"识别出来的"而不是"生成时记下的"。
--   两张表不建外键, 理由同 018: 原图被彻底删除时这里的归属可以悬空, 展示时 LEFT JOIN
--   取不到标题就退回文件名, 绝不能因为"还有引用"而删不掉原图。
--
-- 值:
--   match_kind: 'md5' 内容一致 / 'name' 文件名带作品ID / 'visual' 图像特征 / 'manual' 手动指定
--   confidence: 0~1, 仅 visual 有意义 (ORB 融合分)
--
-- 主键用**夹内相对文件名** (夹子内平铺, 与回收站接口只收相对名的安全口径一致);
-- dir 记录扫描时的夹子路径, 换了壁纸夹后旧记录由下一次扫描清掉。
-- =============================================================
CREATE TABLE IF NOT EXISTS wallpaper_files (
    file_name   TEXT PRIMARY KEY,
    dir         TEXT,
    mtime       REAL,
    size        INTEGER,
    md5         TEXT,
    width       INTEGER,
    height      INTEGER,
    artwork_id  TEXT,
    page_num    INTEGER,
    match_kind  TEXT,
    confidence  REAL,
    scanned_at  TEXT
);

-- 查重入口: 按作品/页反查「夹子里已有几张」
CREATE INDEX IF NOT EXISTS idx_wallpaper_files_artwork
    ON wallpaper_files(artwork_id, page_num);
-- 同内容副本 (不同文件名、字节一致) 的入口
CREATE INDEX IF NOT EXISTS idx_wallpaper_files_md5
    ON wallpaper_files(md5);

-- DOWN:
-- 回滚 v021 手工执行:
--   DROP INDEX IF EXISTS idx_wallpaper_files_md5;
--   DROP INDEX IF EXISTS idx_wallpaper_files_artwork;
--   DROP TABLE IF EXISTS wallpaper_files;
