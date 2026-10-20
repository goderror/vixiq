-- UP:
-- =============================================================
-- 「壁纸库」: 壁纸与图库的关联表。
--
-- 背景: 原本只有一个平铺的壁纸夹 (配置 paths.wallpaper_dir), 壁纸与图库的关系全靠
--   **反推** (core/wallpaper_trace.py: MD5 秒回 → 文件名作品ID → ORB 模糊匹配)。
--   一旦引入「用原图裁出新壁纸」, 裁过的成品字节与原图不同, MD5 反推直接失效,
--   只剩 ORB 那条要加载整库特征的路。所以关系必须**正向落库**。
--
-- 两条口径:
--   ① 壁纸是从原图**派生的成品**: 一张原图 (严格说是 pages 的一页) 可以派生多张
--      —— 不同裁切区域 / 不同屏幕比例, 这就是本表的「一对多」。
--   ② **删除图库只把对应壁纸标记**, 不清文件、不连带删记录。桌面幻灯片仍在那
--      个夹子里播, 把文件清掉会让桌面直接变纯色。
--
-- ⚠ 因此本表**刻意不建外键** (与 001/016 的 pages→artworks 不同):
--   ON DELETE CASCADE 会在「彻底删除」原图时悄悄删掉壁纸记录 —— 记录没了、
--   文件还在夹子里, 变成没人认领的孤儿, 与口径 ② 直接冲突;
--   ON DELETE NO ACTION 又会让 purge_trashed_artworks/pages 因为「还有壁纸引用」
--   而**删不掉原图**。
--   所以血缘用普通列 + 索引维护, 生命周期全部由应用层显式管。
--   代价: artwork_id/page_num 可能指向一个已被彻底删除的页, 展示时必须 LEFT JOIN,
--   取不到标题/作者就退回文件名 (实现时注意)。
--
-- 字段值 (不写 CHECK: 016 的先例就是不带约束, 且 SQLite 改 CHECK 要重建表):
--   crop_mode      : 'center' 居中 / 'subject' 主体优先 / 'manual' 手动框选
--   trashed_reason : 'user' 用户删 / 'cascade' 原图被删连带 / 'cleanup' 清理
-- =============================================================
CREATE TABLE IF NOT EXISTS wallpapers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 血缘: 从 pages 的哪一页派生 (一行 = 一张壁纸成品)
    artwork_id      TEXT NOT NULL,
    page_num        INTEGER NOT NULL,

    -- 成品: 存壁纸夹内的**相对文件名**, 不存绝对路径。
    -- 夹子路径属配置 (换夹子时记录仍能对上), 也与 api_wallpaper_dupes_recycle
    -- 只接受相对名的安全口径一致。
    file_name       TEXT NOT NULL,
    file_md5        TEXT,              -- 壁纸夹是用户会手动动的地方, 靠它发现文件被换/被改
    file_size_bytes INTEGER,
    width           INTEGER,
    height          INTEGER,

    -- 裁切口径: 「用原图重新裁一张」时按这组参数重现
    crop_mode       TEXT,
    crop_x0         INTEGER,
    crop_y0         INTEGER,
    crop_x1         INTEGER,
    crop_y1         INTEGER,
    -- 裁切时原图的指纹。原图被换过就别信旧裁切框 (可能越界), 得重裁。
    -- 有它就不必再存 src_width/src_height —— 内容变了指纹必然变, 尺寸一并覆盖。
    src_md5         TEXT,

    -- 生命周期: 沿用 016 那套「软删可恢复」的口径, 不另造一套
    trashed_at      TEXT,
    trashed_reason  TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- 反查「这张原图派生过哪些壁纸」(一对多的入口)
CREATE INDEX IF NOT EXISTS idx_wallpapers_page    ON wallpapers(artwork_id, page_num);
-- 壁纸列表默认过滤 trashed_at IS NULL, 与 016 的 idx_pages_trashed 同理
CREATE INDEX IF NOT EXISTS idx_wallpapers_trashed ON wallpapers(trashed_at);

-- 夹子里不能有两个同名文件。WHERE 子句是必须的: 被标记的那条还占着这个名字,
-- 不带条件的话「标记掉旧壁纸 → 再裁一张同名」会直接撞唯一约束。
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallpapers_file
    ON wallpapers(file_name) WHERE trashed_at IS NULL;

-- 同参数防重: 同一页 + 同一裁切框 + 同一输出尺寸 = 同一张图, 再生成一次纯属浪费。
-- 与「一对多」不冲突 —— 一对多靠**不同的裁切框**实现。
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallpapers_crop
    ON wallpapers(artwork_id, page_num, crop_x0, crop_y0, crop_x1, crop_y1, width, height)
    WHERE trashed_at IS NULL;

-- DOWN:
-- 回滚 v018 手工执行:
--   DROP INDEX IF EXISTS uq_wallpapers_crop;
--   DROP INDEX IF EXISTS uq_wallpapers_file;
--   DROP INDEX IF EXISTS idx_wallpapers_trashed;
--   DROP INDEX IF EXISTS idx_wallpapers_page;
--   DROP TABLE IF EXISTS wallpapers;