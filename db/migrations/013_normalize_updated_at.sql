-- UP:
-- =============================================================
-- 统一 artworks.updated_at 的时间格式。
-- 背景: schema 默认值是 datetime('now') —— UTC + 空格分隔; 但 db/store.py 的
-- change_work_status() 旧代码写的是 datetime.now().isoformat —— **本地时间 + 'T' 分隔**。
-- 两种格式混在一列里按文本排序时: 'T'(0x54) > ' '(0x20), 于是所有本地格式的行会
-- 无条件排在任何 UTC 空格格式行之前 (与真实时间无关), 再加上 8 小时时区偏差,
-- 一旦「按修改时间」排序就会严重错乱 (生产库实测 1381 行 'T' / 70 行空格)。
-- 本迁移把历史的本地 'T' 值转成 UTC 空格格式; 代码侧已同步改为只写 UTC 空格格式。
-- datetime(x, 'utc') = 把左侧视为本地时间并转成 UTC (跟随系统时区, 不写死 +8)。
-- =============================================================
UPDATE artworks
   SET updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime(updated_at, 'utc'))
 WHERE updated_at IS NOT NULL
   AND instr(updated_at, 'T') > 0;

-- DOWN:
-- 回滚 v013 无需操作 (格式统一是有损但正确的规范化, 无法还原原本地时间字符串)
