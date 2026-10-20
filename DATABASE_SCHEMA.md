# 数据库结构参考（Vixiq）

> 本文件是数据库结构档案，随迁移更新。尚未启用的表、视图与枚举在此标注用途，便于后续启用。
>
> - 引擎：SQLite 单文件，`journal_mode=delete`（非 WAL，本地单进程服务够用）
> - 迁移：`db/migrations/` 下的 SQL 文件，按序号应用
> - 版本：由 `schema_migrations` 表记录

---

## 一、核心业务表（8 张）

### 1. `artworks` — 作品主表（每行一个作品）
| 列 | 类型 | 说明 |
|---|---|---|
| artwork_id | TEXT PK | 作品 ID；外置导入用 `ext_xxx`，合并生成用 `merged_xxx`（非数字，避免被对账误认） |
| total_pages | INTEGER | pixiv 原网页的总页数，用于展示「原 N 页」。部分下载时实际页数少于该值是正常状态 |
| exist_list | TEXT | 兼容旧字段（预留） |
| status | INTEGER | 下载状态：0=未下 / 1=部分 / 2=全部完成 / 404=已删除 |
| last_check | TEXT | 最近核验时间 |
| author_id | TEXT | 作者 ID |
| title | TEXT | 标题 |
| illust_type | INTEGER | 0=插画 / 1=漫画 / 2=动图 |
| tags_json | TEXT | 标签 JSON 数组 |
| bookmark_count | INTEGER | 收藏数（排序用） |
| publish_date | TEXT | 发布日期 |
| is_r18 | INTEGER | 0/1 |
| created_at / updated_at | TEXT | 时间戳，统一为 UTC、格式与 `datetime('now')` 一致。迁移 013 已规范化历史数据（早期写入过本地时间格式，两种格式混排会导致按修改时间排序错乱） |
| thumb_url | TEXT | 缩略图直链（迁移 002） |
| source | TEXT | 来源站点键：pixiv / danbooru / ggac / twi / bili 等（迁移 003） |
| source_author | TEXT | 非 pixiv 来源账号（如 `twi@qing0ying0`），只增不覆盖（迁移 005） |

### 2. `pages` — 图片页表（每行一张图片）
`(artwork_id, page_num)` 复合主键，外键指向 artworks 并级联删除。

列：status（0=未下 / 1=完成 / -1=失败）、local_path（磁盘相对路径）、md5（内容指纹，用于对账）、width / height / file_size_bytes（文件信息显示）、origin（迁移 006：`web` 为原网页下载或存量，`merged` 为合并追加的页；合并页无法从页码判断来源，落库时标记，图库卡片显示为 `pN*`）。

### 3. `authors` — 作者表
author_id PK；name（不可读或纯符号的名称由 `_is_unreadable_name` 拦截并回退为曾用名）；
avatar_url / followers / following / twitter / bio / pixiv_url / first_seen / last_updated；
status（迁移 003：`alive` 活跃 / `gone` 已注销 / NULL 未知）；
aliases_json（迁移 004：曾用名数组，改名时追加，不覆盖）。

### 4. `tags` — 标签字典
tag_id 自增 PK；tag_name 唯一；tag_name_zh / tag_name_en。

### 5. `artwork_tags` — 作品与标签多对多
`(artwork_id, tag_id)` 联合主键，两侧外键级联删除。

### 6. `download_queue_items` — 下载清单
id 自增 PK；item_type（artwork 单作品 / author_all 作者全部 / reverse_sourced 反搜补齐）；
title / author_id / author_name / artwork_id / page_count；strategy（默认 `only_new`）；status（pending / running / done / failed）；added_at；source_note（`reverse_sourced` 项存放 input_path JSON）。

### 7. `reverse_search_history` — 反搜历史
id PK；input_image_path / input_md5 / confidence / search_source / hit_author_id / hit_artwork_id / hit_page_num / manually_corrected / raw_response_json / searched_at；UNIQUE(input_image_path, search_source)。

### 8. `schema_migrations` — 迁移版本表
version PK；applied_at。启动时据此决定执行哪些未应用的迁移。

---

## 二、未启用的对象（保留并标注用途，不要当作冗余删除）

### A. `works` 视图 — 已移除（迁移 007）
旧 Tkinter 版本的兼容层，浏览器版重构后无引用。迁移 001 不再创建该视图（新库干净），迁移 007 用 `DROP VIEW IF EXISTS` 清理旧库。如需回滚，见迁移 007 的 DOWN 注释。

### B. `artwork_fts`（及 `_config` / `_data` / `_docsize` / `_idx` 影子表）— 预留全文索引
用途：数据量增大后，`LIKE '%kw%'` 全表扫描变慢时可改为 FTS5 全文检索。当前搜索走 LIKE（见 `store.search_artworks` / `list_artworks` 的 WHERE 子句）。

启用时需：① 为增删改编写同步触发器；② 全量回填现有数据。注意默认分词器对中日文支持有限，因此此前选择了 LIKE。

### C. `DownloadStrategy` — 策略枚举
- `ONLY_NEW = 'only_new'`：默认策略。实际执行为页级「已存在跳过」，部分下载的作品重下时只补缺失页。
- `OVERWRITE = 'overwrite'`：已接线。下载任务页的「⚠ 强制重下覆盖」经确认后传入 strategy，跳过页级已存在判定并覆盖同路径文件。
- `MISSING_ONLY = 'missing_only'`：已删除，与 ONLY_NEW 在当前实现下行为等价。

强制重下前会把旧文件备份为 `{原名}.bak`（同目录，`.bak` 不被对账识别为图片）。图库的「💾 备份管理」可列出全部 `.bak`，对比新旧后恢复或删除。

### D. `db.store.PixivDatabase._trigger` — 预留属性
初始化时设为 `None`，供将来 FTS5 同步触发器使用，当前未使用。

---

## 三、索引清单（迁移 001 创建）

- idx_pages_artwork：pages(artwork_id)
- idx_pages_md5：pages(md5)
- idx_artworks_author：artworks(author_id, publish_date DESC)
- idx_artworks_status：artworks(status)
- idx_artwork_tags_tag：artwork_tags(tag_id)
- idx_rev_search_md5：reverse_search_history(input_md5)
- idx_download_status：download_queue_items(status)

---

## 四、完整性约束

- `PRAGMA integrity_check` 通过，外键无违规，不存在孤儿 pages / authors / tags，`local_path` 无重复引用。
- `_backfill_page_counts` 只补 `total_pages` 数值，项目中不存在自动补下载缺失页的机制。
