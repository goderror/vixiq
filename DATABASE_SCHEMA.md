# 数据库结构参考（Vixiq）

> 本文件是数据库 (`D:/vixiq-data/pixiv.db`) 的结构档案，随迁移更新。
> 死表/死视图/死枚举在此**标注待命用途**——数据量扩充、需要启用对应能力时据此派上用场。
>
> - 引擎: SQLite（单文件）, `journal_mode=delete`（非 WAL，单进程本地服务可接受）
> - 迁移: `db/migrations/001_init.sql` ~ `005_source_author.sql`，全部已应用
> - 版本: schema_migrations 表记录

---

## 一、核心业务表（8 张）

### 1. `artworks` — 作品主表（每行一个作品）
| 列 | 类型 | 说明 |
|---|---|---|
| artwork_id | TEXT PK | 作品 ID；外置导入用 `ext_xxx`，合并生成用 `merged_xxx`（非数字，防止被对账误认） |
| total_pages | INTEGER | **pixiv 原网页总页数**（作品视图「原 N 页」展示用）。`部分页≠总页数`是正常现象（用户主动只下部分页），非漏洞 |
| exist_list | TEXT | 兼容旧字段（预留） |
| status | INTEGER | 下载状态: 0=未下 / 1=部分 / 2=全部完成 / 404=已删除 |
| last_check | TEXT | 最近核验时间 |
| author_id | TEXT | 作者 ID |
| title | TEXT | 标题 |
| illust_type | INTEGER | 0=插画 / 1=漫画 / 2=动图 |
| tags_json | TEXT | 标签 JSON 数组 |
| bookmark_count | INTEGER | 收藏数（排序用） |
| publish_date | TEXT | 发布日期 |
| is_r18 | INTEGER | 0/1 |
| created_at / updated_at | TEXT | 时间戳。**统一格式 = UTC + 空格**（与 schema 默认 `datetime('now')` 一致）；迁移 013 已把历史上被写成「本地时间 + `T`」的 `updated_at` 规范化 —— 两种格式混排时 `'T' > ' '` 会让本地格式行无条件排在最前，按修改时间排序会错乱。图库默认排序 `sort=updated` |
| thumb_url | TEXT | 缩略图直链（迁移 002） |
| source | TEXT | 来源站点键: pixiv / danbooru / ggac / twi / bili 等（迁移 003） |
| source_author | TEXT | 非 pixiv 来源账号（如 `twi@qing0ying0`），只增不覆盖（迁移 005） |

### 2. `pages` — 图片页表（每行一张图片）
`(artwork_id, page_num)` 复合主键，FK → artworks CASCADE。
列：status(0=未下/1=完成/-1=失败)、local_path（磁盘相对路径）、md5（内容指纹，对账认回身份用）、width/height/file_size_bytes（文件信息显示用）、**origin（迁移 006: 'web' 原网页下载/存量默认 | 'merged' 合并图片附加页** —— 合并追加入末尾的页码无法从页码判断，落库打标，图库卡片附加页显示 `pN*`）。

### 3. `authors` — 作者表
author_id PK；name（不可读/纯符号名会被 `_is_unreadable_name` 拦截并回退曾用名）；
avatar_url/followers/following/twitter/bio/pixiv_url/first_seen/last_updated；
status（迁移 003: 'alive' 活跃中 / 'gone' 已注销 / NULL 未知）；
aliases_json（迁移 004: 曾用名历史数组，改名自动追加、不覆盖）。

### 4. `tags` — 标签字典
tag_id 自增 PK；tag_name 唯一；tag_name_zh / tag_name_en（中英文名）。

### 5. `artwork_tags` — 作品×标签多对多
`(artwork_id, tag_id)` 联合 PK，两侧 FK CASCADE。

### 6. `download_queue_items` — 购物车/下载清单
id 自增 PK；item_type（artwork 单作品 / author_all 作者全部 / reverse_sourced 反搜补齐）；
title/author_id/author_name/artwork_id/page_count；strategy（默认 'only_new'）；status（pending/running/done/failed）；added_at；source_note（reverse_sourced 项存 input_path JSON）。

### 7. `reverse_search_history` — 反搜历史
id PK；input_image_path/input_md5/confidence/search_source/hit_author_id/hit_artwork_id/hit_page_num/manually_corrected/raw_response_json/searched_at；UNIQUE(input_image_path, search_source)。

### 8. `schema_migrations` — 迁移版本表
version PK；applied_at。启动时据此决定跑哪些未应用迁移。

---

## 二、待命对象（死代码/死表，标注未来用途——勿当漏洞删除）

### A. `works` 视图 — 已移除（迁移 007，2026-09-10）
- 旧 Tkinter 版兼容层，浏览器版重构后全项目无引用。
- **双头治理**：001 不再创建（新库干净）+ 007 `DROP VIEW IF EXISTS works`（旧库清理）。生产库已应用 007，sqlite_master 无 view 残留。
- **恢复（如误删需回滚）**：见 007 的 DOWN 注释。

### B. `artwork_fts` (+`_config/_data/_docsize/_idx` 影子表) — FTS5 全文索引死表
- **用途（未来启用时）**：数据量扩大后，`LIKE '%kw%'` 全表扫描变慢时启用全文检索——倒排索引 + `MATCH`，速度快几个数量级。当前搜索走 LIKE（见 `store.search_artworks`/`list_artworks` 的 WHERE 子句）。
- **待命要点**：表是 `content=''` FTS5 空表，长期 0 行。启用时需①写同步触发器（增删改时同步索引）②全量回填现有 1521 条；注意默认分词器对中日文支持有限（p 站日文标题多，当初因此选了 LIKE 兜底）。

### C. `DownloadStrategy` — 策略枚举现状（2026-09-10 更新）
- `ONLY_NEW = 'only_new'`：默认策略，实际执行=页级"已存在跳过"（crawler 逐页判 PSt.DONE → 跳过），部分下载作品重下只补缺失页。
- `OVERWRITE = 'overwrite'`：**已接线**（2026-09-10）——任务页「⚠ 强制重下覆盖」下拉（前端弹窗确认）→ `/api/cart/download` 传 strategy → download_engine → crawler `force_overwrite`（跳过页级已存在判定，重下同路径覆盖）。
- `MISSING_ONLY = 'missing_only'`：**已删除**（2026-09-10）。与 ONLY_NEW 在现有实现下行为等价（跳过判断在页级做），属冗余。
- **副本/恢复机制**：OVERWRITE 强制重下前旧文件自动备份为 `{原名}.bak`（同目录，.bak 不被对账当图片识别）；图库「💾 备份管理」列出全部 .bak，可**对比新旧（左旧/右新）**后恢复（.bak→原名，删 .bak）或删除。API：`/api/library/backups` / `backup-img` / `backup/restore` / `backup/delete`。页数固定不受影响——副本不产生新页码，恢复只是文件内容替换。

### D. `db.store.PixivDatabase._trigger` 实例属性 — 死属性
初始化时 `self._trigger = None  # FTS5 同步触发器用`，从未使用。FTS5 启用时可复用它存触发器。一行代码，留着无妨。

---

## 三、索引清单（001 迁移建）

- idx_pages_artwork: pages(artwork_id)
- idx_pages_md5: pages(md5)
- idx_artworks_author: artworks(author_id, publish_date DESC)
- idx_artworks_status: artworks(status)
- idx_artwork_tags_tag: artwork_tags(tag_id)
- idx_rev_search_md5: reverse_search_history(input_md5)
- idx_download_status: download_queue_items(status)

---

## 四、健康基线（2026-09-10 审计）

- `PRAGMA integrity_check = ok`；外键 0 违规；无孤儿 pages/authors/tags；local_path 无重复引用。
- 数量：artworks 1521 / pages 1885 / authors 409 / tags 2665 / artwork_tags 9271 / queue 1 / 反搜历史 4。
- 定量结论：「部分页≠总页数」为正常设计（用户主动部分下载），`_backfill_page_counts` 只补 total_pages 数字，**全项目不存在自动补下载缺失页的机制**（详见 DEBUG_HISTORY.md 2026-09-10 条目）。