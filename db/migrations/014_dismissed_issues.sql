-- UP:
-- =============================================================
-- 「已忽略的体检项」表。
-- 背景: 自检报告里的「缺作者名」这类条目属于**待办**(等用户指定作者/反搜),
-- 不是系统故障; 但每次自检都重新列一遍, "修了这么多 bug 怎么清单不减少"。
-- 现在允许逐条忽略: 忽略后不再出现在报告里, 需要时可一键恢复。
-- 键 = (kind, ref): kind 是条目类型 (no_author/corrupt/...), ref 是该条目的身份
-- (作品ID / 文件相对路径 / MD5 等)。用 (kind, ref) 而不是行 id, 保证同一条目
-- 反复出现也只忽略一次, 且与报告重算无关。
-- =============================================================
CREATE TABLE IF NOT EXISTS dismissed_issues (
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (kind, ref)
);

-- DOWN:
-- 回滚 v014: 忽略记录属于用户偏好数据, 回滚时保留 (不 DROP) 以避免误伤;
-- 如需彻底清除请手动执行: DROP TABLE dismissed_issues;
