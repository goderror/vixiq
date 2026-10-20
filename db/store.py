# db/store.py
"""
Pixiv SQLite 存储层（统一入口）。

P1 数据库完善版 —— 按（统一模型/索引）+ （DDL 迁移/备份恢复/损坏识别不直接删库）改造。

向后兼容：
  - 原 create_tables / get_artwork_info / get_page_info / batch_sync_all 等全部保留签名，
    内部基于新表 artworks/pages（旧 works → artworks 重命名 + 建 works 兼容视图），
    现有 crawler/reverse_search 调用链零改动即可运行。

新增能力：
  - MigrationManager：启动时自动跑 db/migrations/*.sql，升级 DB 结构不丢数据
  - Authors / Tags / Artwork_tags / Reverse_search_history / Download_queue_items 表
  - artworks(title/tag/author 全文索引 FTS5) + md5/status 多列索引
  - 6 维护 API：backup / restore_with_diff / repair / clean_orphans / rebuild_mappings / export_csv
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import traceback
import zipfile
from collections import namedtuple
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from utils import paths
from utils.config import load_config
from utils.reporter import PrintReporter, Reporter

from .migrator import MigrationManager


# =============================================================
# 兼容枚举 —— 保持原名不变
# =============================================================
class PSt:
    """Page 状态:0 占位 / 1 已下载 / -1 失败缺失"""
    PLACEHOLDER = 0
    DONE = 1
    FAILED = -1


class WSt:
    """Work 状态:0 占位 / 1 未完成 / 2 全部完成 / 404 已失效"""
    PLACEHOLDER = 0
    NOT_YET = 1
    DONE = 2
    ERROR = 404


# =============================================================
# 维护 API 返回的数据类（GUI 展示差异/统计）
# =============================================================
@dataclass
class OrphanReport:
    db_missing_on_disk: list[str]      # DB 标 DONE 但实际本地没有的 "artwork_id_pN"
    empty_author_dirs: list[str]       # 空作者子目录 (绝对路径)
    soft_deleted_rows: int             # 已 status=deleted 但仍残留的记录数

    def total_issues(self) -> int:
        return len(self.db_missing_on_disk) + len(self.empty_author_dirs) + self.soft_deleted_rows


@dataclass
class RebuildReport:
    scanned_images: int
    already_mapped: int
    new_mappings: int
    author_dir_count: int


@dataclass
class ReconcileReport:
    """图库与磁盘实时对账结果。"""
    missing_ids: list[str]       # DB 里曾下载、但磁盘文件已全被删的"幽灵记录"
    empty_dirs: list[str]        # 无任何图片文件的子目录 (绝对路径)
    corrupt_files: list[str]     # 疑似损坏/无法打开的图片文件 (绝对路径)
    scanned_images: int          # 本次扫描到的图片文件总数
    new_pages_linked: int        # 磁盘存在但 DB pages 缺失而补写的页数
    db_artwork_count: int        # 扫描时库内作品总数
    pages_relinked: int = 0      # 页面记录已存在但文件被移动/改名, 凭指纹/自然键重新指向新路径的页数
    duplicates: list = field(default_factory=list)  # 同内容(MD5)多份副本: [{md5, artwork_id, page, paths:[rel...]}]
    conflicts: list = field(default_factory=list)   # 同名(同作品ID+页码)但内容不同的冒名副本: [{artwork_id,page,registered,path}]

    def total_issues(self) -> int:
        return (len(self.missing_ids) + len(self.empty_dirs)
                + len(self.corrupt_files) + len(self.duplicates))


@dataclass
class RestoreDiff:
    """备份 vs 当前的差异对比。返回给 GUI 让恢复，避免 的"盲目覆盖丢失数据"。"""
    backup_only_authors: list[str]     # 备份有、当前没有的作者ID
    backup_only_artworks: list[str]    # 备份有、当前没有的作品ID
    current_only_authors: list[str]    # 当前有、备份没有的作者ID (恢复后会丢)
    current_only_artworks: list[str]   # 当前有、备份没有的作品ID (恢复后会丢)

    def will_lose_current(self) -> bool:
        return bool(self.current_only_authors or self.current_only_artworks)


# =============================================================
# 核心类
# =============================================================
def now_stamp() -> str:
    """库内**唯一**的时间戳写法: UTC + 空格分隔, 与 schema 默认值 `datetime('now')` 完全一致。

    千万别用 `datetime.datetime.now().isoformat()` —— 那是**本地时间 + 'T' + 微秒**,
    与库里的 'YYYY-MM-DD HH:MM:SS' 混在一起时, 所有按时间排序的地方(图库「按修改时间」/
    作者「最后更新」)都会错乱: 字符串比较到第 11 个字符时 'T'(0x54) > ' '(0x20),
    于是带 'T' 的行**无条件**排在所有正常时间戳前面 (用户 2026-09-18 报:
    —— 就是 merge_author_into
    等 6 处仍在写 isoformat)。
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def normalize_ts(value: str) -> "str | None":
    """把历史上写歪的时间戳纠正成 `now_stamp()` 的格式; 已经是标准格式的原样返回。

    老数据是**本地时间**的 ISO 串(如 '2026-09-18T05:38:35.586132'), 而库里其它值都是 UTC,
    所以要**按本机时区换算**(不是简单把 'T' 换成空格, 那样会差 8 小时)。认不出来返回 None。
    """
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) == 19 and s[10] == " " and "T" not in s:
        return s                      # 已经是标准格式
    if "T" not in s and "." not in s:
        return None                   # 不是我们写歪的那种, 别乱动
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:             # 老值 = 本机本地时间, 按本机时区换算成 UTC
        dt = dt.astimezone()
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def guess_foreign_source(rel: str) -> str:
    """按相对路径(文件夹名/文件名)启发式判定其他来源图的出处:
    bili@/b站/哔哩 → bilibili; twi@/twitter/推特/x.com → twitter; GGAC@ → ggac;
    artstation/deviantart/niconico/skeb/yandere/konachan/gelbooru/sankaku/
    safebooru 等画师站与 booru 族关键词 → 对应站点
    ;
    32位hex文件名(danbooru 标准命名)/danbooru/donmai → danbooru; 其余 → other。
    用户归档文件夹命名约定: 「来源@作者名」(如 bili@徐尤点、twi@qing0ying0、GGAC@毒瘤)。
    sample(rsk) 文件夹为 danbooru 下载但文件夹名无关键词, 特判为 danbooru。"""
    import re as _re
    p = str(rel).replace("\\", "/")
    low = p.lower()
    stem = low.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if "bili" in low or "哔哩" in p or "b站" in p.lower():
        return "bilibili"
    if "twi" in low or "twitter" in low or "x.com" in low or "推特" in p:
        return "twitter"
    if "ggac" in low:
        return "ggac"
    if "artstation" in low or "artsta" in low:
        return "artstation"
    if "deviantart" in low or "deviant" in low:
        return "deviantart"
    # 常用画师站 (2026-09-10 三扩; 2026-09-13 用户精简: 去掉 nijie/pawoo/furaffinity)
    if "niconico" in low or "nicovideo" in low or "nico静" in p:
        return "niconico"
    if "skeb" in low:
        return "skeb"
    # booru 族 
    if "yande.re" in low or "yandere" in low:
        return "yandere"
    if "konachan" in low:
        return "konachan"
    if "gelbooru" in low:
        return "gelbooru"
    if "sankaku" in low:
        return "sankaku"
    if "safebooru" in low:
        return "safebooru"
    if "zerochan" in low:
        return "zerochan"
    if "anime-pictures" in low or "animepictures" in low:
        return "animepictures"
    # 其余 SauceNAO 常见站点兜底
    if "shutterstock" in low:
        return "shutterstock"
    if "mangadex" in low:
        return "mangadex"
    if "fakku" in low:
        return "fakku"
    if "kemono" in low:
        return "kemono"
    if "nhentai" in low:
        return "nhentai"
    if "ehentai" in low or "e-hentai" in low:
        return "ehentai"
    if "bcy" in low:
        return "bcy"
    if "medibang" in low:
        return "medibang"
    if "portalgraphics" in low:
        return "portalgraphics"
    if "madokami" in low:
        return "madokami"
    if "doujinshi" in low:
        return "doujinshi"
    if "2d-market" in low or "2dmarket" in low:
        return "2dmarket"
    if "drawr" in low:
        return "drawr"
    if (_re.fullmatch(r"[0-9a-f]{32}", stem) or "danbooru" in low
            or "donmai" in low or "sample" in low):
        return "danbooru"
    return "other"


# 非 pixiv 来源溯源: 文件名/文件夹名约定「来源@源站账号」, 如 twi@qing0ying0、
# bili@徐尤点、GGAC@毒瘤。站点键 → source, @ 后账号 → source_author 落库。
_FOREIGN_ACCOUNT_RE = re.compile(
    r'(?P<site>twi|twitter|bilibili|bili|b站|哔哩|ggac|danbooru|donmai|x|'
    r'artstation|artsta|deviantart|deviant|niconico|nicovideo|nico|skeb|'
    r'yandere|yande|konachan|gelbooru|sankaku|'
    r'safebooru|zerochan|shutterstock|mangadex|fakku|kemono|nhentai|ehentai|bcy|'
    r'medibang|portalgraphics|madokami|doujinshi|drawr|2dmarket)\s*@\s*'
    r'(?P<acc>[^/\\（）()：:]+?)(?=[/\\（）()：:]|$)',
    re.IGNORECASE)
_SITE_TO_SOURCE = {
    "twi": "twitter", "twitter": "twitter", "x": "twitter",
    "bili": "bilibili", "bilibili": "bilibili", "b站": "bilibili", "哔哩": "bilibili",
    "ggac": "ggac",
    "artstation": "artstation", "artsta": "artstation",
    "deviantart": "deviantart", "deviant": "deviantart",
    "danbooru": "danbooru", "donmai": "danbooru",
    "niconico": "niconico", "nicovideo": "niconico", "nico": "niconico",
    "skeb": "skeb",
    "yandere": "yandere", "yande": "yandere",
    "konachan": "konachan", "gelbooru": "gelbooru", "sankaku": "sankaku",
    "safebooru": "safebooru", "zerochan": "zerochan",
    "shutterstock": "shutterstock", "mangadex": "mangadex", "fakku": "fakku",
    "kemono": "kemono", "nhentai": "nhentai", "ehentai": "ehentai", "bcy": "bcy",
    "medibang": "medibang", "portalgraphics": "portalgraphics",
    "madokami": "madokami", "doujinshi": "doujinshi", "drawr": "drawr",
    "2dmarket": "2dmarket",
}


def parse_foreign_account(rel: str) -> "tuple[str, str] | None":
    """从相对路径(文件夹/文件名)解析「来源@源站账号」约定 → (source, account)。

    取路径中【最后一个】匹配(通常即作者文件夹/文件名), 账号遇 / \\ () ：: 终止:
      twi@qing0ying0/xx.png      → ('twitter', 'qing0ying0')
      bili@徐尤点/bili@徐尤点：x.png → ('bilibili', '徐尤点')
      GGAC@毒瘤/GGAC@毒瘤：x.png   → ('ggac', '毒瘤')
      X图/D.I.S.H（twi@Dishwasher1910）_13408193/xx.png
                                → ('twitter', 'Dishwasher1910')  (pixiv 文件夹内标注也可解析)
    无法解析(无站点键、或 @ 前不是站点键如 pixiv 昵称 MAIRO@xxx) → None。
    调用方只对【非 pixiv 作品】落 source_author, 并遵守「只增不覆盖」。"""
    ms = list(_FOREIGN_ACCOUNT_RE.finditer(str(rel).replace("\\", "/")))
    if not ms:
        return None
    m = ms[-1]
    acc = m.group("acc").strip().strip("._- ")
    if not acc:
        return None
    src = _SITE_TO_SOURCE.get(m.group("site").lower())
    if src is None:
        return None
    return src, acc


# pixiv 作品自然键: **必须锚定文件名开头**的 {作品ID}_p{页码}。
# 2026-09-15 事故: 旧正则 `(\d+)_p(\d+)` 不锚开头也不排前导零, 外来源顺序号命名
# (`Ehentai_007_p0.png`、`百度贴吧_003_p0.png`) 被解析成作品 007/003, 哈希尾巴
# (`merged_88505ea18757_p0.png` → 18757, `merged_2c9ce92b16f3_p0.png` → 3) 被当成 pid。
# 下载器落盘命名严格为 `{aid}_p{n}.ext` (crawler.download_artwork), pid 永远在开头、
# 永无前导零; waifu2x 等后缀都在页码之后, 不影响锚定。
_PIXIV_PAGE_KEY_RE = re.compile(r"^([1-9]\d*)_p(\d+)(?![0-9])", re.IGNORECASE)

# 垃圾作者名: 纯问号/替换符/空白 (2026-09-16 实测事故: 控制台编码错误把日文昵称写成
# '????????' 后 upsert, 真名被顶进曾用名, 图库作者全显示问号)。这种名字绝不允许入库顶名。
_BAD_AUTHOR_NAME_RE = re.compile(r"^[\s?？\ufffd]+$")

# 「不是名字」的占位词: 文件夹名里出现这些时, 拿它建作者只会造出垃圾作者行
# (真实图库实测撞到过 name='未知作者' 的 local_ 作者)。宁可留空让用户在界面上指定。
_PLACEHOLDER_AUTHOR_NAMES = {"unknown", "none", "null", "n/a", "na", "未署名", "未知作者",
                             "未知", "未命名", "无作者", "未知来源", "其他来源"}


def folder_author_name_usable(name: str) -> bool:
    """文件夹名里剥出来的「作者名」是否值得拿来建作者档案 (单向闭环的首次导入护栏)。
    不可用 = 空值 / 纯符号(无字母数字) / 占位词 —— 这些不是人读信息, 建成作者只会污染库;
    宁可不建 (作品保持无作者 → 界面显示「未知作者」, 由用户在「指定作者」里定)。"""
    s = (name or "").strip()
    if not s or s.lower() in _PLACEHOLDER_AUTHOR_NAMES:
        return False
    return any(ch.isalnum() for ch in s)


def foreign_author_id(source: str, handle: str) -> str:
    """外来源作者的**唯一**稳定键: `local_` + md5('foreign:{站点}:{账号}')[:12]。

    背景 : 原先有**两套**本地作者建键算法 ——
    `create_or_get_local_author()` 用 md5(名字), `backfill_foreign_authors()` 用
    md5('foreign:站点:账号')。同一个人于是被拆成两条作者记录、一键整理摆出两个作者夹
    (实例: `local_d62077f02230`=md5('ying yi') 与 `local_3155a34b4d66`=foreign:twitter:qing0ying0)。
    现在全库统一走这一个函数: 身份认「站点 + 账号」(账号是稳定的), 显示名只是名字。"""
    import hashlib as _hl
    key = f"foreign:{str(source or '').strip()}:{str(handle or '').strip()}"
    return "local_" + _hl.md5(key.encode("utf-8")).hexdigest()[:12]


# 文件名主干是否算「人读信息」(标题回填用: 别把哈希名当标题写进库)
_CJK_CHAR_RE = re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]')
_HASHY_STEM_RE = re.compile(r'^[0-9a-fA-F\-_]{12,}$')


def stem_is_readable(stem: str) -> bool:
    """文件名主干是不是「人读信息」。含中日韩文字 / 有字母单词 → 可读;
    纯哈希串、纯数字、太短 → 无信息 (拿它当标题只会把乱码灌进库)。"""
    s = (stem or "").strip()
    if not s:
        return False
    if _CJK_CHAR_RE.search(s):
        return True
    if len(s) <= 2 or s.isdigit():
        return False
    if _HASHY_STEM_RE.match(s):
        return False
    return any(ch.isalpha() for ch in s)


# 内部键/文件名样式的标题。
# 这类标题是「文件主干/内部主键」被灌进 title 的结果, 不是人读信息 —— 自检与来源确认时
# 必须回填成可读标题。注意 stem_is_readable 认它会返回 True (含字母), 所以单独判一次。
_INTERNAL_KEY_TITLE_RE = re.compile(
    r"^(?:(?:merged|ext)_[0-9a-f]{6,}|[1-9]\d{5,})_p\d+$"      # merged_xxx_p0 / 88007979_p0
    r"|^(?:merged|ext)_[0-9a-f]{6,}$",                          # merged_xxx / ext_xxx
    re.IGNORECASE)


def title_is_internal_key(title: str) -> bool:
    """标题是不是「内部键 / 文件名」样式 (merged_xxx_p0 / ext_xxx / 123456_p0)。"""
    return bool(_INTERNAL_KEY_TITLE_RE.match(str(title or "").strip()))


# ---------- 作者「来源链接」 ----------
# 一条链接 = {"site": "twitter", "url": "https://x.com/foo", "handle": "foo", "label": "X / Twitter"}
#   site/handle 由 web 层用 _classify_url 解析后写入 —— handle 才是"关联"的关键:
#   它等于该作品在该站的 source_author, 也是外来作者唯一身份键 foreign_author_id(site, handle)
#   的原料。界面显示、按链接搜索、账号回填、归档目标都吃这几个字段。
def _norm_author_urls(raw) -> list[dict]:
    """规范化作者链接列表: 接受 [{"url","site","handle","label"}] 或 ["url", ...] 混合,
    只保留 http(s)、按 url 去重且保序 (用户排的顺序即优先级)。"""
    out: list[dict] = []
    seen: set = set()
    for it in (raw or []):
        if isinstance(it, dict):
            url = str(it.get("url") or "").strip()
            site = str(it.get("site") or "").strip().lower()
            handle = str(it.get("handle") or "").strip()
            label = str(it.get("label") or "").strip()
        else:
            url, site, handle, label = str(it or "").strip(), "", "", ""
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        d = {"site": site, "url": url}
        if handle:
            d["handle"] = handle
        if label:
            d["label"] = label
        out.append(d)
    return out


def _author_urls_of(d: dict) -> list[dict]:
    """作者的链接列表: urls_json 为主; 为空时把旧的 pixiv_url / twitter 两列并进来
    (老数据不迁移也能在界面上看到、能用)。json 坏了就当空, 绝不抛。"""
    urls: list[dict] = []
    raw = d.get("urls_json")
    if raw:
        try:
            urls = _norm_author_urls(json.loads(raw))
        except Exception:
            urls = []
    have = {u["url"] for u in urls}
    for site, key in (("pixiv", "pixiv_url"), ("twitter", "twitter")):
        v = str(d.get(key) or "").strip()
        if v.lower().startswith(("http://", "https://")) and v not in have:
            urls.append({"site": site, "url": v})
            have.add(v)
    return urls


# 外来源站点短名 (标题兜底用: 「推特 · @账号」比「未命名作品」有信息量)
_FOREIGN_SOURCE_NAMES = {
    "twitter": "推特", "bili": "bilibili", "ggac": "GGAC", "danbooru": "danbooru",
    "artstation": "ArtStation", "deviantart": "DeviantArt", "niconico": "Nico静画",
    "skeb": "Skeb", "yandere": "yande.re", "ehentai": "Ehentai", "Ehentai": "Ehentai",
    "other": "其他来源",
}


def foreign_title_fallback(source: str, source_author: str, rel: str = "") -> str:
    """外来源作品没有可读标题时的兜底标题: 可读文件主干 > 「站点 · @账号」> ''。

    返回 '' 表示兜底也拿不到信息, 调用方自行决定 (通常写「未命名作品」)。
    """
    stem = Path(str(rel or "").replace("\\", "/")).stem
    if stem and stem_is_readable(stem) and not title_is_internal_key(stem):
        return stem
    src = str(source or "").strip()
    site = _FOREIGN_SOURCE_NAMES.get(src, src)
    who = str(source_author or "").strip()
    if site and site != "pixiv" and who:
        return f"{site} · @{who}"
    if who:
        return f"@{who}"
    return ""


def _connect(path: "str | Path") -> sqlite3.Connection:
    """统一的 SQLite 连接: 长任务并发时"等锁"而不是直接报错。

    背景 : 自检/下载这类后台长任务持续写库时，用户在前端点
    「锁定文件名」拿到 HTTP 500 —— 默认 5s 等不到写锁就抛 `database is locked`。
    这里 ① `timeout`/`busy_timeout` 放宽到 30s（等锁重试而不是立刻失败）；
    ② 打开 WAL 日志模式（读不阻塞写、写不阻塞读，长任务期间照常改图库；
    `:memory:` 库不支持 WAL，会自然回落到 memory 模式，无副作用）。
    """
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
    except sqlite3.DatabaseError:
        pass
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def pixiv_natural_key(name: str) -> "tuple[str, int] | None":
    """文件名里的 pixiv 作品自然键 → (作品ID, 页码); 非 pixiv 命名返回 None。
    - 规范命名 `123456_p0.png` / 大写 `_P0` / 带 `_waifu2x...` 后缀 → 锚定开头命中;
    - 单页作品手动另存常只剩裸 ID `98836835.png` / `119194561.gif` → 7-9 位无前导零。
    排除: 顺序导出号 `00000001_p0`(前导零)、外来源 `站点_序号_p0` 命名(不在开头)、
    毫秒时间戳 `1730345685303`(13位裸ID)、danbooru 32 位 hex(含 a-f 字母)。
    这类文件是 pixiv 图, 即使没下载入库也应登记为正式作品(随后联网补元数据),
    绝不能被「其他来源」扫描误标成 foreign; 反过来外来源顺序号命名也绝不能被误当 pid。"""
    m = _PIXIV_PAGE_KEY_RE.match(str(name))
    if m:
        return m.group(1), int(m.group(2))
    stem = Path(str(name)).stem
    if re.fullmatch(r"[1-9]\d{6,8}", stem):
        return stem, 0
    return None


class PixivDatabase:
    # -------- 生命周期 --------
    def __init__(self, db_path: "Path | str | None" = None, reporter: "Reporter | None" = None):
        if db_path is None:
            cfg = load_config()
            db_path = paths.user_data(cfg["paths"]["db_filename"])
        self.db_path = Path(db_path)
        self.reporter = reporter or PrintReporter()

        # 1) 路径可写区准备（:memory: 不需要父目录/备份目录）
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # 备份目录
            self.backup_dir = paths.user_data('backup')
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        else:
            # 内存库: 随便放个临时路径作为 backup_dir fallback, 不真实写备份
            import os as _os
            self.backup_dir = paths.temp_path(f"pixiv_mem_db_{_os.getpid()}")
            self.backup_dir.mkdir(parents=True, exist_ok=True)

        # 3) 先建连接（:memory: 用字面量字符串, 不要 Path 包装, 否则 sqlite3 不认）
        _p_str = ":memory:" if str(self.db_path) == ":memory:" else str(self.db_path)
        self.conn = _connect(_p_str)
        self.cursor = self.conn.cursor()
        self._trigger = None  # FTS5 同步触发器用

        # 2) 再跑迁移器（必须复用 self.conn: :memory: 时若另开连接建表, 两个连接
        #    就是两个互相隔离的内存库 → 应用连接里查不到 pages/artworks 等表。
        #     记此坑。）
        try:
            migrator = MigrationManager(self.db_path)
            new_versions = migrator.ensure_migrations_applied(conn=self.conn)
            if new_versions:
                self.reporter.log(f"[DB] 已自动应用迁移: v{', v'.join(new_versions)}")
                # 迁移触发的一次性数据整理: v011 把「作品已删除」从标题文案搬进 deleted_at,
                # 遗留在标题里的占位文案顺手换成可读标题 (幂等, 只在本次有迁移时跑)
                try:
                    self.repair_id_titles()
                except Exception:
                    pass
        except Exception as e:  # 迁移失败不阻塞构造，只给明确报错
            self.reporter.error(
                f"[DB] 迁移失败: {e}\n请使用【修复损坏的库】或恢复最近备份。 "
                f"注意: 绝不要直接删除 {self.db_path.name}。"
            )

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ==================================================================
    # 兼容区: 原 store.py 全部 API —— 签名不变, 内部改用新表 / 兼容视图
    # ==================================================================
    def _execute_with_commit(self,
                             executes: "list[str]",
                             params_list: "list[tuple] | None" = None):
        params_list = params_list or [()] * len(executes)
        with self.conn:
            for sql, params in zip(executes, params_list):
                self.cursor.execute(sql, params)

    def reset_pages(self):
        self.cursor.execute('DELETE FROM pages;')
        self.conn.commit()
        self.cursor.execute('VACUUM;')

    def reset_works(self):
        """清空 artworks + pages（老 API 名叫 reset_works，保留名字）。"""
        self.cursor.execute('DELETE FROM artworks;')
        self.conn.commit()
        self.cursor.execute('VACUUM;')

    def clean_works(self):
        self.cursor.execute('DELETE FROM artworks WHERE status=?', (WSt.PLACEHOLDER,))
        self.conn.commit()

    def init_works(self, aid):
        sql = "INSERT OR IGNORE INTO artworks (artwork_id, status) VALUES (?, ?)"
        self._execute_with_commit([sql], [(aid, WSt.PLACEHOLDER)])

    def init_pages(self, aid, total_pages):
        self.change_work_status(aid, WSt.NOT_YET, total_pages=total_pages)
        executes, params_list = [], []
        for page in range(total_pages):
            sql = "INSERT OR IGNORE INTO pages (artwork_id, page_num, status) VALUES (?, ?, ?)"
            executes.append(sql)
            params_list.append((aid, page, PSt.PLACEHOLDER))
        self._execute_with_commit(executes, params_list)

    def mark_page_status(self, aid, page, status, local_path=None,
                         md5=None, width=None, height=None, file_size_bytes=None,
                         origin=None):
        """写/更新一页。origin 给的是这一页的**身份**: 'web'=pixiv 原网页上的页,
        'merged'=用户自己并进来的「附加页」(超出原网页页数的那几张)。

        默认 None = 保持原值 (新行用库默认 'web')。**附加页必须显式传 'merged'** ——
        否则它会被当成原网页页, 之后「拆分」的闸门会以"不让原作品缺页"为由把它锁死在
        作品里 (见 merge_pages / split_pages 的 origin 判断, 2026-09-17)。"""
        self.init_works(aid)
        sql = """
        INSERT INTO pages (artwork_id, page_num, status, local_path, md5, width, height,
                           file_size_bytes, origin)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'web'))
        ON CONFLICT(artwork_id, page_num) DO UPDATE SET
             status=excluded.status,
             local_path=COALESCE(excluded.local_path, pages.local_path),
             md5=COALESCE(excluded.md5, pages.md5),
             width=COALESCE(excluded.width, pages.width),
             height=COALESCE(excluded.height, pages.height),
             file_size_bytes=COALESCE(excluded.file_size_bytes, pages.file_size_bytes),
             origin=COALESCE(?, pages.origin)
        """
        # 页一落库 = 这个作品被改动了 → 顺手刷 updated_at 
        # 「导入图片的操作不会更新最近修改时间」)。此前只有 change_work_status 与
        # 合并/拆分(_refresh_work_progress) 会刷 —— 于是往一个**已有作品**补页
        # (导入散图 / 补缺页)时 updated_at 一动不动, 按「修改时间」排序里它不会提前,
        # 用户看不到刚导入的东西。**新作品不受影响**: init_works 建行时 schema 的
        # DEFAULT (datetime('now')) 已经就是当前时刻, 本来就排最前。
        # 只在真写页时刷: 已在库的文件在 update_pages_from_output / reconcile_output
        # 里就被 `continue` 跳过了, 不会每次对账都把整库刷成「刚刚」。
        self._execute_with_commit(
            [sql, "UPDATE artworks SET updated_at=? WHERE artwork_id=?"],
            [(aid, page, status, local_path, md5, width, height, file_size_bytes,
              origin, origin),
             (now_stamp(), aid)])

    def change_work_status(self, aid, status,
                           total_pages: "object" = 'hold',
                           exist_list: "object" = 'hold',
                           title=None, author_id=None, illust_type=None,
                           tags=None, bookmark_count=None, publish_date=None,
                           is_r18=None, thumb_url=None, ai_type=None):
        # 时间戳必须与 schema 默认值 (datetime('now') = UTC 空格格式) 完全一致 (2026-09-16):
        # 旧代码写的是本地时间 isoformat (UTC+8 + 'T' 分隔), 与其它行混在一起按文本排序时
        # 'T'(0x54) > ' '(0x20) 会让本地格式的行**无条件**排在前面, 且还有 8 小时偏差,
        # 「按修改时间排序」会严重错乱 (生产库实测 1381 行 T 格式 / 70 行空格格式)。
        now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        info = self.get_artwork_info(aid)
        if info and status != WSt.DONE and info.status == WSt.ERROR:
            status = WSt.ERROR

        total_pages = total_pages if total_pages != 'hold' else (info.total_pages if info else None)
        exist_list = exist_list if exist_list != 'hold' else (info.exist_list if info else None)

        if info is None:
            self.init_works(aid)
        tags_json = json.dumps(tags, ensure_ascii=False) if tags is not None else None

        sql = """
        UPDATE artworks
           SET total_pages   = COALESCE(?, total_pages),
               exist_list    = COALESCE(?, exist_list),
               status        = ?,
               last_check    = ?,
               updated_at    = ?,
               title         = COALESCE(?, title),
               author_id     = COALESCE(?, author_id),
               illust_type   = COALESCE(?, illust_type),
               tags_json     = COALESCE(?, tags_json),
               bookmark_count= COALESCE(?, bookmark_count),
               publish_date  = COALESCE(?, publish_date),
               is_r18        = COALESCE(?, is_r18),
               thumb_url     = COALESCE(?, thumb_url),
               ai_type       = COALESCE(?, ai_type)
         WHERE artwork_id = ?
        """
        params = (total_pages, exist_list, status, now, now,
                  title, author_id, illust_type, tags_json,
                  bookmark_count, publish_date,
                  1 if is_r18 else 0 if is_r18 is not None else None,
                  thumb_url,
                  int(ai_type) if ai_type is not None else None,
                  aid)
        self._execute_with_commit([sql], [params])
        # ---- 同步 tags / artwork_tags 关联 (若传入 tags) ----
        if tags is not None:
            self._sync_artwork_tags(aid, tags)

    def update_pages_from_output(self, save_path: Path):
        """同步本地已下载文件到 pages 表。

        身份识别严格收口 (2026-09-15 事故修复): 文件名正则 (\\d+)_p(\\d+) 很宽松 ——
        `Ehentai_007_p0.png` 会被解析成作品「007」, `merged_xxx18757_p0.png` 的哈希尾巴
        会被当成 pid「18757」。当 save_path 就是图库根目录 (购物车下载) 时, 旧逻辑**不查重
        直接 init_works**, 会把已在库文件整批重复登记成假作品 (实测 29 个假作品/78 个重复页)。
        现规则: **该文件路径已被任何 page 记录拥有 → 一律跳过**(不建作品、不改归属、不刷路径
        分隔符); 只有路径不在库中的真·新文件才登记 (那只可能是刚下好的 `{pid}_p{n}`)。"""
        self.reporter.log("正在同步 Output 文件夹状态...")
        real: "dict[tuple[str, int], tuple[str, int]]" = {}
        for p in save_path.rglob('*'):
            if p.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
                continue
            # 统一走 pixiv_natural_key (锚开头+排前导零): 外来源顺序号/哈希尾巴一律不认
            nkey = pixiv_natural_key(p.name)
            if not nkey:
                continue
            aid, page = nkey
            try:
                size = p.stat().st_size
            except OSError:
                size = None
            # 路径一律用正斜杠存 (图库规范; 旧下载器写反斜杠, 这里不再反向污染)
            real[(aid, page)] = (p.relative_to(save_path).as_posix(), size)

        self.cursor.execute("SELECT artwork_id, page_num, status, local_path FROM pages")
        Row = namedtuple('Row', 'status path')
        db: "dict[tuple[str, int], Row]" = {}
        # 已被任意 page 拥有的规范化路径集合 —— 已在库文件绝不允许再登记到另一个作品下
        owned_paths: set[str] = set()
        for r in self.cursor.fetchall():
            db[(r['artwork_id'], r['page_num'])] = Row(r['status'], r['local_path'])
            if r['local_path']:
                owned_paths.add(str(r['local_path']).replace('\\', '/'))

        newly: set[str] = set()   # 本次新登记 DONE 页的作品 → 末尾补齐作品状态
        for key in real.keys() | db.keys():
            aid, page = key
            try:
                if key in real:
                    local_path, sz = real[key]
                    if local_path in owned_paths:
                        continue  # 文件已在库 (可能归属另一个作品) —— 不重复登记, 不做任何改动
                    # 下载完成 + 回填文件大小
                    self.mark_page_status(aid, page, PSt.DONE, local_path=local_path,
                                          file_size_bytes=sz)
                    newly.add(aid)
                else:
                    if db[key].status == PSt.DONE:
                        self.mark_page_status(aid, page, PSt.FAILED, None)
            except sqlite3.Error as e:
                self.reporter.error(f"DB error: {e}\n{traceback.format_exc()}")
        # 登记页后补齐作品状态 (2026-09-15): 旧实现只写 pages 不同步 artworks,
        # 手动/外部放入的图会永远停在 NOT_YET 且 exist_list 为空 (实测 128028374/130720548)
        if newly:
            n = self.promote_completed_works(newly)
            if n:
                self.reporter.log(f"已补齐 {n} 个作品状态 (页齐全但作品状态漏刷)")
        self.reporter.log("Output数据已完成同步。")

    def promote_completed_works(self, aids: "Iterable") -> int:
        """把「页数与声明一致且全部 DONE, 但作品状态未刷成 DONE」的作品补齐。

        根因: update_pages_from_output 登记磁盘文件为 DONE 页后从不调用
        sync_work_and_pages, 导致外部/手动入库的作品 status 停在 NOT_YET、
        exist_list 为空, UI 误显示「补下缺失页」(2026-09-15 实测两个单页作品)。
        只做「提升」: 不降级状态、不删页、不清 total_pages; 页不齐的作品保持原样。
        返回补齐的作品数。"""
        fixed = 0
        for aid in aids:
            info = self.get_artwork_info(aid)
            if not info or not info.total_pages or info.status == WSt.DONE:
                continue
            rows = self.cursor.execute(
                "SELECT page_num, status FROM pages "
                "WHERE artwork_id=? AND trashed_at IS NULL", (aid,)
            ).fetchall()
            done = sorted(r['page_num'] for r in rows if r['status'] == PSt.DONE)
            # 判据 = 原网页那 tp 页全在 (允许带附加页: 页行数可能 > total_pages, 2026-09-17)
            if all(i in set(done) for i in range(int(info.total_pages))):
                self.change_work_status(aid, WSt.DONE, exist_list=json.dumps(done))
                fixed += 1
        return fixed

    def add_works_from_input(self, input_path: Path):
        self.reporter.log(f"正在扫描 {input_path} 并登记新任务...")
        input_ids: set[str] = set()
        for p in input_path.rglob('*'):
            if p.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
                # 统一身份识别 (2026-09-15): 旧正则 re.search(r'(\d+)') 会把文件名里
                # 任意第一段数字 (毫秒时间戳/顺序号/哈希片段) 当 pid, 建出垃圾下载任务。
                nkey = pixiv_natural_key(p.name)
                if nkey:
                    input_ids.add(nkey[0])
        self.cursor.execute("SELECT artwork_id FROM artworks")
        db_ids: set[str] = {r['artwork_id'] for r in self.cursor.fetchall()}
        new_ids = input_ids - db_ids
        for aid in new_ids:
            try:
                self.init_works(aid)
            except sqlite3.Error as e:
                self.reporter.error(f"DB error: {e}\n{traceback.format_exc()}")
        self.reporter.log("新任务添加完成。")
        return input_ids, new_ids

    def get_pending_tasks(self):
        try:
            self.cursor.execute(
                "SELECT artwork_id FROM artworks WHERE status NOT IN (?, ?)",
                (WSt.DONE, WSt.ERROR),
            )
            return [r['artwork_id'] for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            self.reporter.error(f"DB error: {e}\n{traceback.format_exc()}")
            return []

    def bump_created_at(self, aid: str) -> None:
        """把作品的「入库时间」刷成当前时刻 —— 有新图片真正落盘时调用。

        动机 : created_at 原本只在 `init_works`(加入清单)时写入,
        于是「按入库排序」反映的是加清单时刻而非入库时刻; R18 作品常只下到部分页
        (状态 NOT_YET), 若只在「完整下载完成」时刷新, 它们永远排不到前面。

        格式与 schema 的 DEFAULT CURRENT_TIMESTAMP 一致 —— **必须是 UTC 且空格分隔**:
        SQLite 的 CURRENT_TIMESTAMP 是 UTC, 若这里写本地时间会与世界标准时间差 8 小时,
        与其它作品混排时排序直接错乱 (2026-09-16 回填取证时发现)。"""
        try:
            with self.conn:
                self.cursor.execute(
                    "UPDATE artworks SET created_at=? WHERE artwork_id=?",
                    (datetime.datetime.now(datetime.timezone.utc)
                     .strftime('%Y-%m-%d %H:%M:%S'), aid))
        except sqlite3.Error as e:
            self.reporter.error(f"DB error: {e}\n{traceback.format_exc()}")

    def sync_work_and_pages(self, aid):
        try:
            info = self.get_artwork_info(aid)
            total_pages = info.total_pages if info else None
            artwork_status = info.status if info else None

            done_pages, failed_pages, placeholder = [], [], []
            self.cursor.execute(
                "SELECT page_num, status FROM pages "
                "WHERE artwork_id = ? AND trashed_at IS NULL ORDER BY page_num",
                (aid,),
            )
            all_pages = self.cursor.fetchall()
            for row in all_pages:
                if row['status'] == PSt.DONE:
                    done_pages.append(row['page_num'])
                elif row['status'] == PSt.FAILED:
                    failed_pages.append(row['page_num'])
                else:
                    placeholder.append(row['page_num'])
            all_pages_count = len(all_pages)
            done_list = json.dumps(done_pages) if done_pages else None

            if total_pages:
                # 已知原网页页数: 完整性只看 0..tp-1 这 tp 页; **超出 tp 的行是用户手动
                # 并入的「附加页」(origin='merged') 或陈旧占位行** —— 前者必须保留
                # (2026-09-17: 旧逻辑见 all_pages_count != tp 就把 total_pages 置 None,
                # 附加页会让「原 N 页」元数据永久丢失、作品被误降级), 后者才清理。
                tp = int(total_pages)
                done_set = set(done_pages)
                stale = [pn for pn in placeholder if pn >= tp]
                if stale:
                    with self.conn:
                        for pn in stale:
                            self.cursor.execute(
                                "DELETE FROM pages WHERE artwork_id=? AND page_num=?",
                                (aid, pn))
                new_status = WSt.DONE if all(i in done_set for i in range(tp)) \
                    else WSt.NOT_YET
                self.change_work_status(aid, new_status, exist_list=done_list)
                # 首次完整下载完成 → 把「入库时间」刷成此刻 (created_at 原本记的是
                # 「加入清单」时刻, 用户下载完按「入库排序」在最前面找不到该作品)
                if new_status == WSt.DONE and artwork_status != WSt.DONE:
                    self.bump_created_at(aid)
            else:
                total_pages = None
                with self.conn:
                    for page_num in placeholder:
                        self.cursor.execute(
                            "DELETE FROM pages WHERE artwork_id = ? AND page_num = ?",
                            (aid, page_num),
                        )
                if done_pages or artwork_status == WSt.ERROR or failed_pages:
                    self.change_work_status(aid, WSt.NOT_YET, total_pages, done_list)
                else:
                    self.cursor.execute('DELETE FROM artworks WHERE artwork_id=?', (aid,))
        except sqlite3.Error as e:
            self.reporter.error(f"DB error: {e}\n{traceback.format_exc()}")

    def batch_sync_all(self, artwork_ids: "list | None" = None):
        if artwork_ids is None:
            self.cursor.execute(
                "SELECT DISTINCT artwork_id FROM pages UNION SELECT artwork_id FROM artworks"
            )
            artwork_ids = [r[0] for r in self.cursor.fetchall()]
        else:
            artwork_ids = list(set(artwork_ids))
        if not artwork_ids:
            self.reporter.log("没有需要同步的作品")
            return
        self.reporter.log(f"开始批量同步 {len(artwork_ids)} 个作品...")

        failed: list[str] = []
        total = len(artwork_ids)
        for i, aid in enumerate(artwork_ids, 1):
            self.reporter.progress(i, total, "同步")
            try:
                self.sync_work_and_pages(aid)
            except Exception as e:
                self.reporter.error(f"❌{aid} 同步失败: {str(e).splitlines()[0]}")
                failed.append(aid)
        success_count = total - len(failed)
        self.reporter.log(f"批量同步完成!成功: {success_count} / {total}")
        if failed:
            self.reporter.warn("失败的作品:")
            for aid in failed:
                self.reporter.warn(f"  {aid}")

    def get_page_info(self, aid, page):
        Info = namedtuple('ArtworkInfo', ['status', 'local_path',
                                           'md5', 'width', 'height', 'file_size_bytes'])
        try:
            self.cursor.execute(
                "SELECT status, local_path, md5, width, height, file_size_bytes "
                "  FROM pages WHERE artwork_id=? AND page_num=?",
                (aid, page),
            )
            row = self.cursor.fetchone()
            return Info(*row) if row else None
        except sqlite3.Error as e:
            self.reporter.error(f"DB error: {e}\n{traceback.format_exc()}")
            return None

    def get_artwork_info(self, aid):
        """兼容旧调用: 返回 namedtuple(status, total_pages, exist_list)。
        新代码请用 get_artwork_metadata() 取完整元数据(title/tags 等)。"""
        Info = namedtuple('ArtworkInfo', ['status', 'total_pages', 'exist_list'])
        try:
            self.cursor.execute(
                "SELECT status, total_pages, exist_list FROM artworks WHERE artwork_id=?",
                (aid,),
            )
            row = self.cursor.fetchone()
            return Info(*row) if row else None
        except sqlite3.Error as e:
            self.reporter.error(f"DB error: {e}\n{traceback.format_exc()}")
            return None

    # ==================================================================
    # 元数据读写(新 API: 作者 / 完整作品信息 / 下载清单 / 反搜历史)
    # ==================================================================
    @staticmethod
    def _parse_aliases(raw) -> "list[str]":
        try:
            arr = json.loads(raw) if raw else []
        except Exception:
            return []
        if not isinstance(arr, list):
            return []
        out = []
        for a in arr:
            s = str(a).strip()
            if s and s not in out:
                out.append(s)
        return out

    def upsert_author(self, author_id: str, name: str, **extra) -> None:
        now = now_stamp()
        name = (str(name).strip() if name is not None else "")
        # 垃圾名防护 (2026-09-16 '????????' 事故): 纯 ?/�/空白 的名字绝不能顶掉库里
        # 已有的真名 —— 已存在作者时保留旧名 (只更新 extra 字段), 新作者落「未知画师」。
        bad_name = (not name) or bool(_BAD_AUTHOR_NAME_RE.match(name))
        # 曾用名留存: 改名时把旧昵称「追加」进 aliases_json (只增不删、去重、
        # 不含当前名), 绝不覆盖历史。改名自检/下载元数据更新都走这里, 旧名不丢。
        row = self.conn.execute(
            "SELECT name, aliases_json FROM authors WHERE author_id=?",
            (author_id,)).fetchone()
        extra = dict(extra)
        if bad_name:
            name = (str(row[0]).strip() if row and row[0]
                    and not _BAD_AUTHOR_NAME_RE.match(str(row[0]).strip() or "x")
                    else "未知画师")
        if row:
            old_name = (str(row[0]).strip() if row[0] is not None else "")
            aliases = self._parse_aliases(row[1])
            if name and old_name and old_name != name and old_name not in aliases:
                aliases.append(old_name)
            aliases = [a for a in aliases if a != name]  # 当前名不进曾用名
            extra["aliases_json"] = json.dumps(aliases, ensure_ascii=False)
        fields = dict(extra, name=name, last_updated=now)
        # 存在则更新(保留首次 first_seen), 不存在则插入
        if row:
            sets = ', '.join(f"{k}=?" for k in fields.keys())
            self.conn.execute(f"UPDATE authors SET {sets} WHERE author_id=?",
                              list(fields.values()) + [author_id])
        else:
            cols = ['author_id'] + list(fields.keys())
            qs = ','.join(['?'] * len(cols))
            self.conn.execute(
                f"INSERT INTO authors ({','.join(cols)}) VALUES ({qs})",
                [author_id] + list(fields.values()),
            )
        self.conn.commit()

    def dismiss_issue(self, kind: str, ref: str, note: str = "") -> None:
        """把一条体检条目记为「已忽略」(立即提交), 之后不再出现在报告里。

        用途 : 「缺作者名」这类待办项要等用户逐条处理, 不处理就
        每轮自检重新列一遍, 看起来"清单永远不减少"。忽略机制让用户能主动清掉不关心的条目。
        """
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO dismissed_issues (kind, ref, note, created_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (str(kind), str(ref), str(note or "")))

    def restore_issue(self, kind: str, ref: str) -> int:
        """取消忽略 (恢复显示), 返回删除行数。"""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM dismissed_issues WHERE kind=? AND ref=?",
                (str(kind), str(ref)))
            return cur.rowcount or 0

    def dismissed_keys(self) -> "set[tuple[str, str]]":
        """已忽略的 (kind, ref) 集合, 供报告过滤。"""
        try:
            rows = self.conn.execute(
                "SELECT kind, ref FROM dismissed_issues").fetchall()
        except sqlite3.Error:
            return set()
        return {(str(r[0]), str(r[1])) for r in rows}

    def dismissed_issues(self) -> "list[dict]":
        """已忽略条目清单 (按忽略时间倒序), 供「已忽略」面板查看/恢复。"""
        try:
            rows = self.conn.execute(
                "SELECT kind, ref, note, created_at FROM dismissed_issues "
                "ORDER BY created_at DESC").fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    def rewrite_page_path_prefix(self, old_dir: str, new_dir: str) -> int:
        """文件夹改名/搬迁后, 把 pages.local_path 的 `old_dir/` 前缀换成 `new_dir/`
        (**立即提交**), 返回受影响行数。

        为什么单独成方法 (2026-09-16 锁库事故): 以前这两处由 web 层直接
        `db.conn.execute(...)` + 手工 commit —— 一旦漏掉 commit, 隐式写事务就把整库
        写锁握住 (参考 set_author_status 的教训), 而且改动还会在连接关闭时被回滚。
        写库统一走 store 方法, 提交责任只有一处。
        """
        old_dir = str(old_dir or "").strip("/")
        new_dir = str(new_dir or "").strip("/")
        if not old_dir or not new_dir or old_dir == new_dir:
            return 0
        with self.conn:
            cur = self.conn.execute(
                "UPDATE pages SET local_path = REPLACE(local_path, ?, ?) "
                "WHERE local_path LIKE ?",
                (old_dir + "/", new_dir + "/", old_dir + "/%"))
            return cur.rowcount or 0

    def set_author_status(self, author_id: str, status: str) -> None:
        """更新作者存续状态 ('alive' 活跃中 / 'gone' 已注销), **立即提交**。

        为什么单独开一个方法 (2026-09-16 事故): 自检「作者改名同步」原来直接写
        `db.conn.execute("UPDATE authors SET status=...")` 且**从不 commit** ——
        Python sqlite3 的这条 UPDATE 会隐式开启写事务并一直持有写锁, 而那个循环里有
        逐个作者的 HTTP 请求 + 0.5s 睡眠, 于是写锁被握着好几分钟, 期间用户在图库里
        点保存就报 `database is locked`。而且连接关闭时事务被回滚, 状态**根本没存进去**。
        写库一律走这样带提交的方法, 杜绝同类问题。
        """
        with self.conn:
            self.conn.execute("UPDATE authors SET status=? WHERE author_id=?",
                              (str(status), str(author_id)))

    def get_author(self, author_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM authors WHERE author_id=?", (author_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["aliases"] = self._parse_aliases(d.get("aliases_json"))
        d["urls"] = _author_urls_of(d)        # 来源链接(可多条), 含 site/handle
        return d

    def upsert_artwork_metadata(self, aid, *, title=None, author_id=None,
                                author_name=None, illust_type=None, tags=None,
                                publish_date=None, is_r18=None, thumb_url=None,
                                page_count=None, bookmark_count=None,
                                ai_type=None,
                                work_status=WSt.NOT_YET) -> None:
        """下载/反搜归档时随文件一起落库的权威元数据 (标题/标签/作者/封面/R18/总页数)。
        原则: 下载动作本身就取自 pixiv 最权威数据源, 元数据必须当场记录,
        不依赖事后自检/补全 (那些只是保险)。None 字段不覆盖已有值。
        page_count: 作品原网页总页数 (作品视图「原 N 页」展示用)。
        bookmark_count: 收藏数 (统计/排序用)。
        ai_type: pixiv 官方 AI 判定 (1=非AI, 2=AI); 取不到(0/None)时**不写**,
                 免得把已经标记好的行覆盖回「未知」(2026-09-20)。
        work_status 默认 NOT_YET: 作品行不存在时建壳, 下载/对账流程随后收敛状态。"""
        self.change_work_status(str(aid), work_status,
                                title=title,
                                author_id=str(author_id) if author_id else None,
                                illust_type=illust_type,
                                tags=list(tags) if tags is not None else None,
                                publish_date=publish_date,
                                is_r18=is_r18, thumb_url=thumb_url,
                                total_pages=page_count,
                                bookmark_count=bookmark_count,
                                ai_type=ai_type if ai_type in (1, 2) else None)
        if author_id and author_name:
            self.upsert_author(str(author_id), str(author_name))
        # 能拿到权威元数据 = 作品页确实存在 → 撤销可能存在的「已失效」标记
        # (客观事实会变化: 之前 404 后来又恢复, 或我们判错过)
        self.mark_artwork_deleted(str(aid), False)

    def get_work_info(self, aid: str) -> "Optional[object]":
        """下载引擎 ONLY_NEW 跳过判断用 (download_engine._run_item 调)。

        返回 namedtuple(status, total_pages, done):
          - status:  int, WSt 状态值 (2=全部完成)
          - total_pages: int|None
          - done:    bool, 是否全部下载完成 (status == WSt.DONE)
        以前 download_engine 里直接调 db.get_work_info() 但 store.py 根本没这个方法
        → 每次下载 artwork 清单项必 AttributeError 失败。这里补上。
        """
        d = self.get_artwork_metadata(aid)
        if not d:
            return None
        WorkInfo = namedtuple('WorkInfo', ['status', 'total_pages', 'done'])
        st = d.get('status')
        return WorkInfo(st, d.get('total_pages'), st == WSt.DONE)

    def get_artwork_metadata(self, artwork_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM artworks WHERE artwork_id=?", (artwork_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        # tags_json → list
        if d.get('tags_json'):
            try:
                d['tags'] = json.loads(d['tags_json'])
            except json.JSONDecodeError:
                d['tags'] = []
            d.pop('tags_json', None)
        return d

    def search_artworks(self, keyword: str, limit: int = 200) -> list[dict]:
        """全文搜索 + 作者名模糊匹配(返回完整行 dict)。"""
        if not keyword.strip():
            return []
        kw = f"%{keyword.strip()}%"
        rows = self.conn.execute(
            """
            SELECT DISTINCT a.*, au.name AS author_name
              FROM artworks a
         LEFT JOIN authors au ON a.author_id = au.author_id
         LEFT JOIN artwork_tags atg ON atg.artwork_id = a.artwork_id
         LEFT JOIN tags tg          ON tg.tag_id = atg.tag_id
             WHERE a.title LIKE ?
                OR au.name LIKE ?
                OR a.artwork_id LIKE ?
                OR a.author_id LIKE ?
                OR tg.tag_name LIKE ?
          ORDER BY COALESCE(a.publish_date, a.created_at) DESC
             LIMIT ?
            """,
            (kw, kw, kw, kw, kw, limit),
        ).fetchall()
        result: list[dict] = []
        for r in rows:
            d = dict(r)
            if d.get('tags_json'):
                try:
                    d['tags'] = json.loads(d['tags_json'])
                except json.JSONDecodeError:
                    d['tags'] = []
                d.pop('tags_json', None)
            result.append(d)
        return result

    def list_recent_artworks(self, limit: int = 200) -> list[dict]:
        """图库管理: 列出全部作品(按日期倒序), 返回带 tags/作者名的完整行。"""
        rows = self.conn.execute(
            """
            SELECT DISTINCT a.*, au.name AS author_name
              FROM artworks a
         LEFT JOIN authors au ON a.author_id = au.author_id
          ORDER BY COALESCE(a.publish_date, a.created_at) DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result: list[dict] = []
        for r in rows:
            d = dict(r)
            if d.get('tags_json'):
                try:
                    d['tags'] = json.loads(d['tags_json'])
                except json.JSONDecodeError:
                    d['tags'] = []
                d.pop('tags_json', None)
            result.append(d)
        return result

    def artwork_briefs(self, ids: "list[str]") -> dict:
        """批量取作品的「人读标签」素材: {artwork_id: {title, source, source_author, author_name}}。

        用途: 操作审计 / 自检可疑项这类面向用户的清单不该只印 ext_xxx 这种内部主键,
        需要「标题 · 作者 · 来源」才认得出是谁。
        """
        out: dict = {}
        want = [str(x) for x in (ids or []) if str(x)]
        if not want:
            return out
        ph = ",".join("?" * len(want))
        for r in self.conn.execute(
                f"SELECT a.artwork_id, a.title, a.source, a.source_author, a.deleted_at, "
                f"       au.name AS author_name "
                f"  FROM artworks a LEFT JOIN authors au ON a.author_id = au.author_id "
                f" WHERE a.artwork_id IN ({ph})", want).fetchall():
            out[str(r["artwork_id"])] = dict(r)
        return out

    def mark_artwork_deleted(self, artwork_id: str, deleted: bool = True) -> int:
        """记录/撤销「该作品在 pixiv 的作品页已确认不存在」这一客观事实。

        - 只在**确认是 pixiv 作品**(纯数字作品 ID)时允许标记 —— 非 pixiv 作品(ext_/merged_)
          的 ID 不是 pixiv 作品页地址, 谈「已删除」没有意义, 这里直接拒绝(返回 0)。
        - 只写字段, 不碰标题/标签等其他数据(旧实现把事实写进标题占位文案, 已被本字段取代)。
        """
        aid = str(artwork_id or "").strip()
        if not aid or not aid.isdigit():
            return 0
        if deleted:
            set_sql, val = "deleted_at=?", datetime.datetime.now().isoformat(timespec="seconds")
            cond = ""
        else:
            # 撤销时只碰确实被标记过的行 (避免无谓写库)
            set_sql, val, cond = "deleted_at=NULL", None, " AND deleted_at IS NOT NULL"
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE artworks SET {set_sql}, updated_at=datetime('now') "
                f"WHERE artwork_id=?{cond}", ((val, aid) if deleted else (aid,)))
        return cur.rowcount

    def artwork_is_deleted(self, artwork_id: str) -> bool:
        try:
            r = self.conn.execute("SELECT deleted_at FROM artworks WHERE artwork_id=?",
                                  (str(artwork_id),)).fetchone()
        except sqlite3.Error:
            return False
        return bool(r and r[0])

    def repair_id_titles(self, limit: int = 500) -> int:
        """把「谁也认不出」的标题回填成可读标题。返回修复数。

        只改**标题**，绝不碰磁盘文件名（图片名归用户/来源）；取名优先级：
        首张本地文件名（去扩展名，且**必须是可读名**）>「未命名作品」。处理两类：

        1. `title == artwork_id`（早期合并等把内部 ID 写成了标题）；
        2. 旧版「（作品已删除 xxx）」占位文案 —— 「已删除」这个事实自 v011 起存在
           `deleted_at` 字段里, 标题不必再当占位仓库, 换成可读标题；
        3. **内部键/文件名样式的标题**（`merged_86126bd0e0de_p0` / `ext_xxx` / `88007979_p0`）
           —— 合并/拆分会把文件主干直接当标题写进库, 2026-09-16 用户实测。

        护栏 (2026-09-14 单向闭环审计): 只在文件主干**是人读信息**时才拿它当标题 ——
        否则会把 `-7a93b54c836a829` 这类哈希名当标题灌进库 (比原来的作品 ID 更难认)。
        作品 ID 本身留在 `artwork_id` 字段, 不会因为不用它当标题而丢失。
        外来源作品没有可读文件名时退回「站点 · @账号」(foreign_title_fallback)。
        """
        rows = self.conn.execute(
            "SELECT artwork_id, title, source, source_author FROM artworks "
            "WHERE title IS NOT NULL AND (title = artwork_id "
            "   OR title LIKE '（作品已删除%' "
            "   OR title LIKE 'merged\\_%' ESCAPE '\\' "
            "   OR title LIKE 'ext\\_%' ESCAPE '\\') LIMIT ?",
            (int(limit),)).fetchall()
        n = 0
        for r in rows:
            aid = str(r["artwork_id"])
            title = str(r["title"] or "").strip()
            # LIKE 只是粗筛 (会捞到「merged 后的可读名」), 精判交给 title_is_internal_key
            _is_placeholder = (title == aid or title.startswith("（作品已删除"))
            if not _is_placeholder and not title_is_internal_key(title):
                continue
            lp = self.conn.execute(
                "SELECT local_path FROM pages WHERE artwork_id=? "
                "AND local_path IS NOT NULL AND local_path != '' "
                "ORDER BY page_num LIMIT 1", (aid,)).fetchone()
            new_title = "未命名作品"
            if lp and lp[0]:
                _stem = Path(str(lp[0])).stem
                # 占位标题(==作品ID/已删除)时保留旧行为: 可读文件名直接当标题
                # (T59 契约: `900031_p0.jpg` → 标题 `900031_p0`)。
                # 但**内部键标题**要换掉时不能再拿内部键样式的文件名顶上 (否则等于没修),
                # 改用「站点 · @账号」兜底。
                if stem_is_readable(_stem) and not (_is_placeholder is False
                                                    and title_is_internal_key(_stem)):
                    new_title = _stem
                elif not _is_placeholder:
                    new_title = (foreign_title_fallback(r["source"], r["source_author"],
                                                        str(lp[0])) or new_title)
            with self.conn:
                self.conn.execute(
                    "UPDATE artworks SET title=?, updated_at=datetime('now') "
                    "WHERE artwork_id=?", (new_title, aid))
            n += 1
        return n

    def reidentify_merged_external(self, limit: int = 300,
                                   only_aid: "str | None" = None) -> "list[tuple[str, str]]":
        """把「已确认是外部来源」的合并产物从 `merged_` 改成 `ext_` 身份, 并修掉内部键标题。

        动机 : 合并/拆分会生成 `merged_<hash>` 主键, 之后即使用户给它
        填了真实来源 (如推特链接), 主键与标题仍停在 `merged_xxx` —— 语义上它已经是
        「其他来源作品」, 与 `ext_` 是同一类东西, 不该永远背着「合并产物」的身份。
        触发条件: `artwork_id` 以 merged_ 开头 **且** 已确认外部来源
        (source 非空非 pixiv, 或已有 source_url)。纯 pixiv 页的合并产物不动
        (那属于「认领散落页 → 转正回 #pid」的范畴)。

        新主键 = `ext_` + md5('ext|rekey|旧ID')[:16] —— 由旧 ID 决定, 幂等、不会与
        别的作品撞键; 万一撞了退回随机 ext_ 键。pages 由外键 ON UPDATE CASCADE 自动跟随,
        artwork_tags / download_queue_items / reverse_search_history 手动跟随。
        返回 [(旧ID, 新ID)]。
        """
        import hashlib as _hl
        import uuid as _uuid
        params: list = []
        sql = ("SELECT artwork_id, title, source, source_author FROM artworks "
               "WHERE artwork_id LIKE 'merged\\_%' ESCAPE '\\' "
               "  AND ((source IS NOT NULL AND source != '' AND source != 'pixiv') "
               "       OR (source_url IS NOT NULL AND source_url != ''))")
        if only_aid:
            sql += " AND artwork_id = ?"
            params.append(str(only_aid))
        sql += " ORDER BY artwork_id LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        out: "list[tuple[str, str]]" = []
        for r in rows:
            old = str(r["artwork_id"])
            new = "ext_" + _hl.md5(f"ext|rekey|{old}".encode("utf-8")).hexdigest()[:16]
            if self.conn.execute("SELECT 1 FROM artworks WHERE artwork_id=?",
                                 (new,)).fetchone():
                new = "ext_" + _uuid.uuid4().hex[:16]
            title = str(r["title"] or "").strip()
            new_title = title
            if (not title) or title == old or title_is_internal_key(title):
                lp = self.conn.execute(
                    "SELECT local_path FROM pages WHERE artwork_id=? "
                    "AND local_path IS NOT NULL AND local_path != '' "
                    "ORDER BY page_num LIMIT 1", (old,)).fetchone()
                cand = foreign_title_fallback(r["source"], r["source_author"],
                                              (lp[0] if lp else "") or "")
                new_title = cand or (title if not title_is_internal_key(title) else "")
                new_title = new_title or "未命名作品"
            with self.conn:
                # pages 有 ON UPDATE CASCADE, 改主键会自动跟随
                self.conn.execute(
                    "UPDATE artworks SET artwork_id=?, title=?, updated_at=datetime('now') "
                    "WHERE artwork_id=?", (new, new_title, old))
                for tbl, col in (("artwork_tags", "artwork_id"),
                                 ("download_queue_items", "artwork_id"),
                                 ("reverse_search_history", "hit_artwork_id")):
                    try:
                        self.conn.execute(
                            f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new, old))
                    except sqlite3.Error:
                        continue   # 老库可能没有这张表/列, 不影响身份纠正
            out.append((old, new))
        return out

    def works_missing_metadata(self, limit: int = 1000) -> list[str]:
        """图库补全: 返回缺标签/标题/缩略图的作品 ID (优先旧的先补)。

        **只返回 pixiv 正式作品(纯数字 ID)** :
        这个队列的用途是「联网问 pixiv 要元数据」, 而 ext_/merged_ 这类非 pixiv 作品
        pixiv 上根本没有, 去问必然 404 —— 旧实现把它们也塞进来, 于是在自检报告里
        永远挂着「元数据补全失败」, 用户以为是自己没修好的 bug。非 pixiv 作品的标题/
        作者由本地文件夹名与人工归类负责, 不该进这个队列。

        已删除的作品(deleted_at 非空)同样排除 —— 作品页都没了, 联网也补不回来。

        **ai_type IS NULL 也进这个队列** :
        图库要按 AI 筛选, 就得先把全库刷上 pixiv 的 aiType。而"缺标题/标签/封面"
        这个老口径对老库是空的 (用户 1514 个作品元数据都齐) → 光加解析不补数据
        等于没做。纳入后用户点一次现有「补全元数据」即可一次性回填。
        注意 count_missing_metadata() **不**纳入这一条 —— 否则「缺元数据」筛选
        会把全库都标成缺, 属于误导 (两口径刻意分离)。
        """
        rows = self.conn.execute(
            """
            SELECT artwork_id FROM artworks
             WHERE deleted_at IS NULL AND trashed_at IS NULL
               AND artwork_id GLOB '[0-9]*' AND artwork_id NOT GLOB '*[^0-9]*'
               AND (tags_json IS NULL OR title IS NULL OR title = ''
                    OR thumb_url IS NULL OR ai_type IS NULL)
             ORDER BY created_at ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [r['artwork_id'] for r in rows]

    def count_missing_metadata(self) -> int:
        """缺元数据(标签/标题/缩略图)的**pixiv 作品**数量, 供前端提示补全进度
        (与 works_missing_metadata 同口径: 非 pixiv 作品不在此列, 它们补不了)。"""
        return self.conn.execute(
            """
            SELECT COUNT(*) FROM artworks
             WHERE deleted_at IS NULL AND trashed_at IS NULL
               AND artwork_id GLOB '[0-9]*' AND artwork_id NOT GLOB '*[^0-9]*'
               AND (tags_json IS NULL OR title IS NULL OR title = ''
                    OR thumb_url IS NULL)
            """
        ).fetchone()[0]

    def set_artwork_ai_type(self, artwork_id: str, ai_type: int) -> None:
        """写入 pixiv 官方 AI 判定 (1=非AI, 2=AI, 0=接口没给)。

        **与 upsert_artwork_metadata 的区别**: 那边对 0/None 是"不写"(绝不把已标记的
        行降级回未知); 这里是补全循环专用 —— 联网问过一次就必须落一个值, 否则
        `ai_type IS NULL` 的作品永远排在补全队列头部, 每轮都重复请求同一批 (2026-09-20)。

        **刻意不刷 updated_at**: 回填是"给 1500+ 个老作品补一个分类标记", 不是内容
        变了。刷了会把「按修改时间」排好的图库顺序整批打乱 (用户对时间戳很敏感)。
        """
        self.conn.execute(
            "UPDATE artworks SET ai_type=? WHERE artwork_id=?",
            (int(ai_type or 0), str(artwork_id)))
        self.conn.commit()

    # ==================================================================
    # 图库管理增强: 筛选/排序/分页查询 + 删除 + 标签增删改 + 分类数据
    # ==================================================================
    # ==================================================================
    # 其他来源 (danbooru/推特/GGAC/贴吧等无 pixiv ID 文件) 登记
    # ==================================================================
    def register_foreign_file(self, rel: str) -> "str | None":
        """把无 pixiv ID 的图库文件登记为「其他来源」占位作品, 纳入图库统一管理。
        ext ID = md5('ext|'+相对路径) 稳定键; source 由 guess_foreign_source 按
        路径启发式判定 (bilibili/twitter/ggac/danbooru/other)。
        title=「文件夹名 / 文件名」便于辨认; 作者列留空归"未知"(反搜识别后可再归档)。
        已登记过则跳过。返回 ext_id 或 None(已存在)。"""
        import hashlib as _hl
        rel = str(rel).replace("\\", "/")
        # 硬护栏: 文件名带 pixiv 自然键 (123_p0 / 大写_P0 / 单页裸ID) 的是 pixiv 图,
        # 必须由对账登记为正式作品, 绝不能落成「其他来源」占位。
        if pixiv_natural_key(Path(rel).name):
            return None
        ext_id = "ext_" + _hl.md5(f"ext|{rel}".encode("utf-8")).hexdigest()[:16]
        if self.conn.execute(
                "SELECT 1 FROM pages WHERE artwork_id=? AND local_path=?",
                (ext_id, rel)).fetchone():
            return None
        p = Path(rel)
        stem = p.stem
        source = guess_foreign_source(rel)
        parsed = parse_foreign_account(rel)
        source_author = parsed[1] if parsed else None
        folder = p.parent.as_posix()
        title = f"{folder} / {stem}" if folder not in (".", "") else stem
        # 时间戳统一为 UTC + 空格格式, 与 schema 的 DEFAULT datetime('now') 对齐
        # (旧实现用本地时间的 isoformat() → 既差 8 小时又带 'T', 与其它作品混排时
        #  「按入库排序」错乱; 2026-09-16 回填取证时发现 99 条此类历史数据)
        now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self.conn.execute(
            """INSERT OR IGNORE INTO artworks (artwork_id, title, tags_json, thumb_url,
                                             created_at, updated_at, source, source_author)
               VALUES (?, ?, '[]', '', ?, ?, ?, ?)""",
            (ext_id, title, now, now, source, source_author))
        self.conn.execute(
            """INSERT INTO pages (artwork_id, page_num, status, local_path)
               VALUES (?, 0, ?, ?)
               ON CONFLICT(artwork_id, page_num) DO UPDATE SET local_path=excluded.local_path""",
            (ext_id, PSt.DONE, rel))
        # 只增不覆盖: 已有 source_author 值绝不动, 只补空值
        if source_author:
            self.conn.execute(
                "UPDATE artworks SET source_author=? WHERE artwork_id=? "
                "AND (source_author IS NULL OR source_author='')",
                (source_author, ext_id))
        # 必须自己提交 (2026-09-17 踩坑): with _db() 退出只 close 不 commit, 未提交的写会被
        # SQLite 回滚 —— 调用方拿到 ext_id 以为登记成功, 库里其实什么都没有。与项目规则
        # 「写库必须提交」一致 (批量版 register_foreign_files 也在末尾提交)。
        self.conn.commit()
        return ext_id

    def register_foreign_files(self, rels: "list[str]") -> int:
        """批量登记其他来源文件, 返回新登记个数 (供自检/同步把无 pixiv ID 的文件纳入图库)。"""
        n = 0
        for rel in rels:
            try:
                if self.register_foreign_file(rel):
                    n += 1
            except Exception:
                continue
        if n:
            self.conn.commit()
        return n

    def refresh_foreign_sources(self) -> int:
        """把仍是「其他来源」(other/NULL) 的 ext_ 作品按路径启发式细分到具体站点
        (用户新建 bili@/twi@/GGAC@ 文件夹或改名后, 自检/同步时自动归类)。
        只升级 other→具体站点, 绝不覆盖已归类或用户在编辑弹窗手动指定的来源。
        同时顺带解析「来源@账号」补写 source_author (只增不覆盖)。
        返回更新条数。"""
        rows = self.conn.execute(
            """SELECT a.artwork_id, a.source, a.source_author, p.local_path FROM artworks a
               JOIN pages p ON p.artwork_id = a.artwork_id
               WHERE a.artwork_id LIKE 'ext_%' AND p.local_path IS NOT NULL""").fetchall()
        n = 0
        now = now_stamp()
        for aid, old, src_author, lp in rows:
            changed = False
            if old in (None, "", "other"):
                new = guess_foreign_source(lp)
                if new != "other":  # 仅当能确定具体站点时才升级, 拿不准就保持 other
                    self.conn.execute(
                        "UPDATE artworks SET source=?, updated_at=? WHERE artwork_id=?",
                        (new, now, aid))
                    changed = True
            if not (src_author or "").strip():
                parsed = parse_foreign_account(lp)
                if parsed and parsed[1]:
                    self.conn.execute(
                        "UPDATE artworks SET source_author=?, updated_at=? WHERE artwork_id=? "
                        "AND (source_author IS NULL OR source_author='')",
                        (parsed[1], now, aid))
                    changed = True
            if changed:
                n += 1
        if n:
            self.conn.commit()
        # 顺带把「无归属的外来作品」归类到本地作者 (含主页链接回填), 让作者管理完整
        n2 = self.backfill_foreign_authors()
        return n + n2

    # 需要统一格式的时间戳列 (表, 列)。凡是参与排序/显示的都要在这里。
    _TS_COLS = (("artworks", "created_at"), ("artworks", "updated_at"),
                ("artworks", "publish_date"), ("artworks", "deleted_at"),
                ("artworks", "trashed_at"), ("authors", "first_seen"),
                ("authors", "last_updated"), ("download_queue_items", "added_at"),
                ("dismissed_issues", "created_at"))

    def normalize_all_timestamps(self) -> int:
        """把历史上写歪的 ISO 时间戳纠正成库内统一格式 (幂等)。

        症状 (用户 2026-09-18 报): 图库「按修改时间」排序时, 那几条写歪的记录
        (如 '2026-09-18T05:38:35.586132') **一直顶在最前面**, 之后怎么改别的图都排不到
        前面 —— 因为排序是**字符串比较**, 第 11 个字符 'T'(0x54) > ' '(0x20),
        同一天内带 'T' 的无条件排第一。
        老值写的是本地时间, 所以按本机时区换算成 UTC (不是把 'T' 换成空格那么简单)。
        返回纠正的单元格数。"""
        fixed = 0
        for table, col in self._TS_COLS:
            try:
                rows = self.conn.execute(
                    f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL "
                    f"AND (instr({col}, 'T') > 0 OR instr({col}, '.') > 0)"
                ).fetchall()
            except sqlite3.Error:
                continue
            for rid, val in rows:
                new = normalize_ts(val)
                if new and new != val:
                    self.conn.execute(
                        f"UPDATE {table} SET {col}=? WHERE rowid=?", (new, rid))
                    fixed += 1
        if fixed:
            self.conn.commit()
        return fixed

    def backfill_foreign_authors(self) -> int:
        """给「无归属外来作品」(source 非空 且 author_id IS NULL) 建本地作者并归属。

        设计对齐 danbooru 的作者维度: 每位外来作者一个作者记录, 名字用它的
        @源站账号 (如 qing0ying0), 并据站点回填主页链接 (X→x.com/<acc>,
        bilibili→空间搜索)。**未带账号的匿名作品不建作者** (保持 author_id 为空,
        界面显示「未知作者」, 不再记成字面量「未署名」)。
        作者 id = `foreign_author_id(站点, 账号)` 稳定键 (与手动建档同一套算法),
        重复执行幂等, 绝不改动已有归属。返回本次归属的作品数。"""
        from urllib.parse import quote as _q
        rows = self.conn.execute(
            """SELECT a.artwork_id, a.source, a.source_author FROM artworks a
               WHERE a.author_id IS NULL AND COALESCE(a.source,'') NOT IN ('')""").fetchall()
        if not rows:
            return 0
        now = now_stamp()
        # 站点名 → (作者名缺省, 主页 URL 模板)
        def home_url(src, acc):
            acc = acc or ""
            if src == "twitter" and acc:
                return "https://x.com/" + acc
            if src == "bilibili" and acc:
                return "https://space.bilibili.com/search?keyword=" + _q(acc)
            if src in ("danbooru",) and acc:
                return "https://danbooru.donmai.us/users/" + acc
            return None

        changed = 0
        by_aid: "set[tuple[str, str, str, str, str]]" = set()  # (aid, kid, name, src, url)
        for r in rows:
            src = r["source"] or "other"
            acc = (r["source_author"] or "").strip()
            if not acc:
                # 无账号的匿名作品: 无法定位作者, 保持无作者 (显示「未知作者」),
                # 绝不造一个名字叫「未署名」的假作者
                continue
            name = acc
            kid = foreign_author_id(src, name)   # 统一稳定键 (与手动建档同一套算法)
            by_aid.add((r["artwork_id"], kid, name, src, home_url(src, acc) or ""))
        for aid, kid, name, src, url in by_aid:
            # upsert 作者 (幂等): 只补不覆盖已有主页/名字
            self.conn.execute(
                """INSERT INTO authors (author_id, name, first_seen, last_updated, pixiv_url)
                   VALUES (?,?,?,?,?) ON CONFLICT(author_id) DO UPDATE SET
                     last_updated=excluded.last_updated,
                     pixiv_url=CASE WHEN authors.pixiv_url IS NULL OR authors.pixiv_url=''
                                    THEN excluded.pixiv_url ELSE authors.pixiv_url END""",
                (kid, name, now, now, url))
            # 归属作品
            self.conn.execute(
                "UPDATE artworks SET author_id=?, updated_at=? WHERE artwork_id=? AND author_id IS NULL",
                (kid, now, aid))
            changed += 1
        self.conn.commit()
        return changed

    def backfill_clean_author_names(self) -> int:
        """剥离作者现用名里混入的「(来源@账号)」污染 → 净名, 括号内片段并入曾用名。

        现象如「kieed（twi@kieed23232）」「SanMuYYB（twi@sanmuyyb）」——这些是从下载
        路径/标题带进来的源站账号, 不是作者真名, 导致现用名与 pixiv 网页对不上。
        只处理**含 '@' 的括号片段** (twi@/GGAC@ 等), 不含 @ 的正常日文括号(如
        「MUK（むっく）」)绝不动。剥离后原括号内容整体进曾用名(去重), 支持多曾用名。
        幂等, 只在 author 名含 '@' 的行处理。返回更新的作者数。"""
        import re as _re
        pat = _re.compile(r'[（(]([^（）()]*@[^（）()]*)[）)]')
        rows = self.conn.execute(
            "SELECT author_id, name, aliases_json FROM authors WHERE name LIKE '%@%'").fetchall()
        if not rows:
            return 0
        now = now_stamp()
        n = 0
        for aid, name, aliases_json in rows:
            if not name:
                continue
            ms = list(pat.finditer(name))
            if not ms:
                continue
            # 「主名（twi@acc）」→ 主名 + 原括号内容进曾用名
            clean = pat.sub('', name).replace('（', '').replace('）', '').replace('(', '').replace(')', '').strip()
            if not clean:
                continue
            extras = []
            for m in ms:
                inside = m.group(1).strip()
                extras.append(inside)
                for part in inside.replace('@', ' @').split():
                    if part.startswith('@'):
                        extras.append(part[1:])  # 只取账号本体
                    elif '@' in part:
                        extras.append(part.split('@')[1])
            aliases = self._parse_aliases(aliases_json)
            for e in extras:
                if e and e != clean and e not in aliases:
                    aliases.append(e)
            # 曾用名卫生: 剔除与现用名相同项; 若已有裸账号「acc」, 则去掉
            # 「site@acc」冗余变体 (如 twi@kieed23232 vs kieed23232, 留裸账号)。
            kept: "list[str]" = []
            for a in aliases:
                if not a or a == clean or a in kept:
                    continue
                if "@" in a and any(o == a.split("@")[-1] for o in aliases if "@" not in o):
                    continue  # site@acc 但已有裸 acc 别名 → 冗余, 丢弃
                kept.append(a)
            self.conn.execute(
                "UPDATE authors SET name=?, aliases_json=?, last_updated=? WHERE author_id=?",
                (clean, json.dumps(kept, ensure_ascii=False), now, aid))
            n += 1
        if n:
            self.conn.commit()
        return n

    def clean_author_alias_hygiene(self) -> int:
        """曾用名全局卫生 (幂等): 对【所有】作者做一遍曾用名清洗——剔除与现用名
        相同项、空项、去重; 若有裸账号「acc」则去掉「site@acc」冗余变体。
        在 backfill_clean_author_names 剥离污染后再跑, 清理历史遗留的重复别名。
        返回更新的作者数。"""
        rows = self.conn.execute(
            "SELECT author_id, name, aliases_json FROM authors "
            "WHERE aliases_json IS NOT NULL AND aliases_json != '[]'").fetchall()
        if not rows:
            return 0
        now = now_stamp()
        n = 0
        for aid, name, aliases_json in rows:
            cur = (str(name).strip() if name is not None else "")
            aliases = self._parse_aliases(aliases_json)
            kept: "list[str]" = []
            for a in aliases:
                if not a or a == cur or a in kept:
                    continue
                if "@" in a and any(o == a.split("@")[-1] for o in aliases if "@" not in o):
                    continue  # site@acc 但已有裸 acc 别名 → 冗余, 丢弃
                kept.append(a)
            if kept == aliases:
                continue  # 无变化, 不写库
            self.conn.execute(
                "UPDATE authors SET aliases_json=?, last_updated=? WHERE author_id=?",
                (json.dumps(kept, ensure_ascii=False), now, aid))
            n += 1
        if n:
            self.conn.commit()
        return n

    def foreign_unregistered_files(self, disk_rels: "list[str]") -> "list[str]":
        """磁盘有但 pages 表完全没登记的相对路径 (即无 pixiv 自然键、对账也不管的文件)。
        disk_rels: 磁盘扫出的相对路径 posix 列表。
        注意: 文件名带 pixiv 自然键 (_p页码/裸作品ID) 的不算外来——那是没下载入库的
        pixiv 图, 归对账登记为正式作品, 故这里直接排除, 防止误标「其他来源」。"""
        registered = {r[0].replace("\\", "/") for r in self.conn.execute(
            "SELECT local_path FROM pages WHERE local_path IS NOT NULL AND local_path != ''")}
        out = []
        for d in disk_rels:
            if d in registered:
                continue
            if pixiv_natural_key(Path(d).name):
                continue  # pixiv 命名, 走对账补登记, 不进外来扫描
            out.append(d)
        return out

    def backfill_author_aliases(self, extra: "dict | None" = None) -> int:
        """一次性回填作者曾用名: 汇总【下载队列历史名】+ 调用方提供的【磁盘作者
        文件夹名 / 日志改名记录】, 与现有 aliases 取并集 (去重、剔除当前名、
        只增不删, 绝不覆盖)。extra: {author_id: [曾用名, ...]}。返回更新作者数。"""
        names: dict = {}

        def add(aid, nm):
            aid = str(aid).strip()
            nm = str(nm or "").strip()
            if not aid or not nm:
                return
            names.setdefault(aid, [])
            if nm not in names[aid]:
                names[aid].append(nm)

        for r in self.conn.execute(
                "SELECT author_id, author_name FROM download_queue_items "
                "WHERE author_id IS NOT NULL AND author_name IS NOT NULL AND author_name != ''"):
            add(r["author_id"], r["author_name"])
        for aid, nms in (extra or {}).items():
            for nm in nms:
                add(aid, nm)
        updated = 0
        for aid, hist in names.items():
            row = self.conn.execute(
                "SELECT name, aliases_json FROM authors WHERE author_id=?", (aid,)).fetchone()
            if not row:
                continue
            cur = str(row[0]).strip() if row[0] is not None else ""
            merged = self._parse_aliases(row[1])
            before = len(merged)
            for nm in hist:
                if nm != cur and nm not in merged:
                    merged.append(nm)
            if len(merged) != before:
                self.conn.execute(
                    "UPDATE authors SET aliases_json=? WHERE author_id=?",
                    (json.dumps(merged, ensure_ascii=False), aid))
                updated += 1
        self.conn.commit()
        return updated

    def promote_pixiv_named_foreign(self, img_root: "Path | str | None" = None) -> dict:
        """把历史上被误标成「其他来源」(ext_) 但文件名其实是 pixiv 命名
        (_p页码 / 大写_P0 / 单页裸作品ID) 的占位作品转正:
          - 该 (作品ID,页码) 页尚不存在 → mark_page_status 建正式作品页 + 认作者;
          - 已存在 (该作品早已正常下载登记) → 仅删 ext_ 占位壳, 磁盘文件留给对账
            按 MD5 指纹认回 (绝不顶掉已有登记的路径);
        最后删除 ext_ 壳行 (仅删 DB 行 + 级联清理, 不删任何磁盘文件)。
        返回 {promoted, duplicate_ext_removed, deleted_ext, promoted_detail}。"""
        from utils.hashutil import md5_of
        root = Path(img_root) if img_root is not None else None
        rows = self.conn.execute(
            """SELECT a.artwork_id, p.local_path
                 FROM artworks a JOIN pages p ON p.artwork_id = a.artwork_id
                WHERE a.artwork_id LIKE 'ext_%' AND p.local_path IS NOT NULL
                  AND p.local_path != ''""").fetchall()
        promoted, dup = [], []
        delete_ids: "list[str]" = []
        for ext_id, rel in rows:
            rel = str(rel).replace("\\", "/")
            key = pixiv_natural_key(Path(rel).name)
            if not key:
                continue  # 真外来, 保留 ext_
            pid, page = str(key[0]), int(key[1])
            exists = self.conn.execute(
                "SELECT 1 FROM pages WHERE artwork_id=? AND page_num=?",
                (pid, page)).fetchone()
            if not exists:
                h = sz = None
                if root is not None:
                    fp = root / rel
                    if fp.is_file():
                        try:
                            sz = fp.stat().st_size
                            h = md5_of(fp)
                        except OSError:
                            h = sz = None
                self.mark_page_status(pid, page, PSt.DONE,
                                      local_path=rel, md5=h, file_size_bytes=sz)
                self._adopt_author_from_rel(pid, rel)
                promoted.append((ext_id, pid, rel))
            else:
                dup.append((ext_id, pid, rel))
            delete_ids.append(str(ext_id))
        uniq = list(dict.fromkeys(delete_ids))
        if uniq:
            self.conn.commit()
            self.delete_artworks(uniq)  # 只删 DB 行, 不动磁盘文件
        return {"promoted": len(promoted), "duplicate_ext_removed": len(dup),
                "deleted_ext": len(uniq), "promoted_detail": promoted}

    # ==================================================================
    # 手动维护: 本地作者 (非 pixiv 作者, 如 danbooru 画师/已注销查无 ID)
    # ==================================================================
    def _find_author_by_name_or_alias(self, name: str) -> "str | None":
        """按现用名 / 曾用名找同一个人 (不区分大小写)。作者改名后仍能找回原记录,
        不会因为"显示名变了"再建一条新作者。"""
        n = str(name or "").strip().lower()
        if not n:
            return None
        for r in self.conn.execute(
                "SELECT author_id, name, aliases_json FROM authors"):
            if str(r["name"] or "").strip().lower() == n:
                return str(r["author_id"])
            try:
                al = json.loads(r["aliases_json"] or "[]") or []
            except Exception:
                al = []
            if any(str(x).strip().lower() == n for x in al):
                return str(r["author_id"])
        return None

    def create_or_get_local_author(self, name: str, status: "str | None" = None,
                                   source: "str | None" = None,
                                   handle: "str | None" = None) -> str:
        """找/建一条**外来源作者**记录 (danbooru/推特/artstation 等, 或 pixiv 已注销查无 ID)。

        身份定序 (2026-09-14 修: 同一人被拆成两条记录):
          ① 给了「站点 + 账号」→ 先看已有作品是否已用同一 (站点,账号) 归属过作者 → 复用那条;
             否则用**统一稳定键** `foreign_author_id(站点, 账号)` 建/取 (与
             `backfill_foreign_authors` 同一套算法, 不再各算一份 md5);
          ② 只给名字 → 先按**现用名/曾用名**找已存在作者 → 复用 (改名后不再分叉);
          ③ 都没有 → 按名字建 (`local_` + md5(名字)[:12], 原行为)。
        有账号而显示名不可辨认时用账号当名字 (账号至少可读可搜)。
        返回 author_id。"""
        nm = str(name or "").strip()
        src = str(source or "").strip()
        hd = str(handle or "").strip()

        def _ensure(aid: str, disp: str) -> str:
            row = self.conn.execute(
                "SELECT author_id FROM authors WHERE author_id=?", (aid,)).fetchone()
            if not row:
                kw = {"first_seen": now_stamp()}
                if status:
                    kw["status"] = status
                self.upsert_author(aid, disp, **kw)
            elif status:
                self.conn.execute(
                    "UPDATE authors SET status=? WHERE author_id=?", (status, aid))
                self.conn.commit()
            return aid

        if src and hd:
            hit = self.conn.execute(
                "SELECT author_id FROM artworks WHERE source=? AND source_author=? "
                "AND COALESCE(author_id,'') != '' LIMIT 1", (src, hd)).fetchone()
            if hit and self.get_author(str(hit[0])):
                return str(hit[0])
            return _ensure(foreign_author_id(src, hd),
                           nm if folder_author_name_usable(nm) else hd)

        hit = self._find_author_by_name_or_alias(nm)
        if hit:
            if status:
                self.conn.execute(
                    "UPDATE authors SET status=? WHERE author_id=?", (status, hit))
                self.conn.commit()
            return hit

        import hashlib as _hl
        return _ensure("local_" + _hl.md5(nm.encode("utf-8")).hexdigest()[:12], nm)

    def set_artworks_author(self, artwork_ids: "list[str]", author_id: str) -> int:
        """批量把作品的作者改成指定 author_id (手动设置作者功能)。返回更新条数。"""
        if not artwork_ids or not author_id:
            return 0
        ids = [str(x) for x in artwork_ids]
        ph = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"""UPDATE artworks SET author_id=?,
                 updated_at=? WHERE artwork_id IN ({ph})""",
            [author_id, now_stamp()] + ids)
        self.conn.commit()
        return cur.rowcount or 0

    def merge_author_into(self, src_id: str, dst_id: str) -> dict:
        """把作者 src 整个并入 dst (作品/队列跟着走, 作者行合并后删除 src)。

        场景 : 作者记了 pixiv 主页链接 → 明确他其实是 pixiv 作者
        (ID 就在链接里) → 一键把「本地作者 local_xxx」的身份换成真实 pixiv ID,
        作品不再挂在一个假 ID 上, 之后体检的作者改名同步/存续状态也认得他。
        合并规则: 名字以 dst 为准(dst 无名则用 src 的); 曾用名/来源链接合并去重。
        返回 {"artworks": n, "queue": n, "deleted": bool, "artwork_ids": [...]}。
        `artwork_ids` 供调用方做「合并后收尾」(如把文件归拢到作者文件夹)。"""
        src_id, dst_id = str(src_id or "").strip(), str(dst_id or "").strip()
        if not src_id or not dst_id or src_id == dst_id:
            return {"artworks": 0, "queue": 0, "deleted": False, "artwork_ids": []}
        src = self.get_author(src_id)
        dst = self.get_author(dst_id)
        if src is None:
            raise KeyError(f"作者不存在: {src_id}")
        now = now_stamp()
        moved_ids = [str(r[0]) for r in self.conn.execute(
            "SELECT artwork_id FROM artworks WHERE author_id=?", (src_id,)).fetchall()]
        cur = self.conn.execute(
            "UPDATE artworks SET author_id=?, updated_at=? WHERE author_id=?",
            (dst_id, now, src_id))
        n_art = cur.rowcount or 0
        # 队列里的作者任务也要跟着换, 否则作者批量下载会指向已删除的本地作者
        try:
            cur_q = self.conn.execute(
                "UPDATE download_queue_items SET author_id=? WHERE author_id=?",
                (dst_id, src_id))
            n_q = cur_q.rowcount or 0
        except sqlite3.Error:
            n_q = 0
        if dst is not None:
            # 曾用名 + 链接合并 (去重保序, dst 在前; src 的现用名也进曾用名, 绝不丢)
            names = [str(x) for x in (dst.get("aliases") or [])]
            for x in [str(src.get("name") or "")] + [str(y) for y in (src.get("aliases") or [])]:
                if x and x != str(dst.get("name") or "") and x not in names:
                    names.append(x)
            urls = _norm_author_urls(list(dst.get("urls") or []) + list(src.get("urls") or []))
            fields = {
                "name": str(dst.get("name") or "").strip() or str(src.get("name") or ""),
                "aliases_json": json.dumps(names, ensure_ascii=False),
                "urls_json": json.dumps(urls, ensure_ascii=False),
                "last_updated": now,
            }
            for site, col in (("pixiv", "pixiv_url"), ("twitter", "twitter")):
                hit = next((u["url"] for u in urls if u.get("site") == site), "")
                fields[col] = hit or None
            sets = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE authors SET {sets} WHERE author_id=?",
                              list(fields.values()) + [dst_id])
        else:
            # 目标不存在 → 把源行改挂到目标 ID 上 (保住名字/别名/链接), 而不是丢弃
            self.conn.execute(
                "UPDATE authors SET author_id=?, last_updated=? WHERE author_id=?",
                (dst_id, now, src_id))
            src = None
        if src is not None:
            self.conn.execute("DELETE FROM authors WHERE author_id=?", (src_id,))
        self.conn.commit()
        return {"artworks": n_art, "queue": n_q, "deleted": True,
                "artwork_ids": moved_ids}

    def fill_source_author_from_links(self, author_id: str,
                                      links: "list[dict]") -> int:
        """按作者记的站点链接回填作品的 source_author (链接产生"关联"的动作之一)。

        条件保守, 只在**能确定**时写: 作品属于该作者 且 作品来源 == 链接站点
        且 source_author 还空着 → 填成链接里的账号 handle。
        这样「作品来源细分/显示 @账号/归档子夹 twitter@账号」都跟着正确,
        且绝不会覆盖已有的 source_author。返回回填条数。"""
        pairs = [(str(l.get("site") or "").lower(), str(l.get("handle") or "").strip())
                 for l in (links or [])]
        pairs = [(s, h) for s, h in pairs if s and h]
        if not pairs:
            return 0
        n = 0
        for site, handle in pairs:
            cur = self.conn.execute(
                """UPDATE artworks SET source_author=?, updated_at=?
                    WHERE author_id=? AND COALESCE(source,'') = ? COLLATE NOCASE
                      AND (source_author IS NULL OR source_author = '')""",
                (handle, now_stamp(), str(author_id), site))
            n += cur.rowcount or 0
        self.conn.commit()
        return n

    def get_artwork_tag_names(self, artwork_id: str) -> "list[str]":
        """取某作品现有标签名列表 (批量追加标签时用)。"""
        rows = self.conn.execute(
            """SELECT tg.tag_name FROM artwork_tags atg
               JOIN tags tg ON tg.tag_id = atg.tag_id
               WHERE atg.artwork_id=? ORDER BY tg.tag_name""", (str(artwork_id),)).fetchall()
        return [r[0] for r in rows]

    def batch_update_artworks(self, artwork_ids: "list[str]", **fields) -> int:
        """批量编辑: 对每个作品套用相同字段 (title/author_id/illust_type/is_r18/
        source/tags 等, 字段语义同 update_artwork)。返回成功条数。"""
        n = 0
        for aid in artwork_ids:
            try:
                self.update_artwork(str(aid), **fields)
                n += 1
            except Exception:
                continue
        self.conn.commit()
        return n

    def relink_page_path(self, old_rel: str, new_rel: str) -> bool:
        """文件改名后同步 pages.local_path (手动规范化文件名用)。"""
        old_rel, new_rel = old_rel.replace("\\", "/"), new_rel.replace("\\", "/")
        cur = self.conn.execute(
            "UPDATE pages SET local_path=? WHERE local_path=?", (new_rel, old_rel))
        self.conn.commit()
        if (cur.rowcount or 0) > 0:
            self.refresh_ext_titles()
        return (cur.rowcount or 0) > 0

    def refresh_ext_titles(self) -> int:
        """把 ext_ 外来作品的标题从「注册时快照」刷新为当前实际文件夹/文件名
        (文件被一键整理/同步/反搜归档移动后, 标题里的路径不再过期)。
        只在标题仍是自动格式 (末尾 = 当前文件名) 时刷新, 用户手动改过的不动。
        返回更新条数。"""
        n = 0
        rows = self.conn.execute(
            "SELECT artwork_id, title FROM artworks "
            "WHERE artwork_id LIKE 'ext_%'").fetchall()
        for aid, old_title in rows:
            row = self.conn.execute(
                "SELECT local_path FROM pages WHERE artwork_id=? "
                "ORDER BY page_num LIMIT 1", (aid,)).fetchone()
            if not row:
                continue
            rel = str(row[0] or "").replace("\\", "/")
            if not rel:
                continue
            p = Path(rel)
            folder = p.parent.as_posix()
            new_title = f"{folder} / {p.stem}" if folder not in (".", "") else p.stem
            old = str(old_title or "")
            # 只刷新自动格式: 标题为空, 或末尾是「/ 当前文件名」, 或就是纯文件名(注册时在根目录)
            # —— 文件夹搬走但文件没改名; 用户手动改过的不动
            if old != new_title and (not old or old.endswith(f"/ {p.stem}")
                                     or old == p.stem):
                self.conn.execute(
                    "UPDATE artworks SET title=? WHERE artwork_id=?", (new_title, aid))
                n += 1
        if n:
            self.conn.commit()
        return n

    def list_artworks(self, *, q: str = '', illust_type=None, is_r18=None,
                      ai: "str | None" = None,
                      author_id: "str | None" = None, tag: "str | None" = None,
                      missing: bool = False, sort: str = 'date',
                      order: str = 'desc', limit: int = 200,
                      offset: int = 0,
                      file_missing_ids: "set | list | None" = None,
                      deleted: bool = False,
                      trashed: bool = False,
                      author_status: "str | None" = None,
                      source: "str | None" = None,
                      only_aid: "str | None" = None) -> list[dict]:
        """图库通用查询: 关键词(q, 匹配标题/作者名/ID/标签) + 类型 + R18 + AI + 作者 + 标签 + 缺元数据 多条件筛选,
        支持排序(sort: date/title/author/id/bookmark)与分页。返回带 tags/作者名的完整行。
        file_missing_ids: 给定则只返回这些 artwork_id (磁盘文件缺失筛选, 与其他条件 AND)。
        deleted: 只显示已确认失效的 pixiv 作品 (deleted_at 字段非空, 元数据补全时 404 落的)。
        trashed: False(默认) 只显示图库里的(排除回收站); True 只显示回收站里的 (2026-09-17)。
        author_status: 'alive' 作者活跃中 / 'gone' 作者已注销 / 'unknown' 未知作者(作品无 author_id)。
        ai: '2' 只看 AI 作品 / '0' 只看非 AI (未标记的按非 AI 算) / 空 = 全部 (2026-09-20)。
        source: 'pixiv' / 'danbooru' / 'other' 等来源筛选 (artworks.source, 空值视为 pixiv)。"""
        where, params = [], []
        # 回收站是「软件内的软删除」: 图库任何查询默认都必须排除它, 否则删掉的东西还在列表里。
        # 这条过滤放在最前面、无条件生效 —— 漏掉任何一条查询路径都会让回收站内容"复活"。
        where.append("a.trashed_at IS NOT NULL" if trashed else "a.trashed_at IS NULL")
        if only_aid:
            # 「仅查看该作品」: 精确 ID 硬过滤 (2026-09-16), 不能用 q 的 LIKE 误伤
            where.append("a.artwork_id = ?")
            params.append(str(only_aid))
        if file_missing_ids is not None:
            ids = [str(x) for x in file_missing_ids]
            if ids:
                where.append(f"a.artwork_id IN ({','.join('?' * len(ids))})")
                params += ids
            else:
                where.append("1=0")   # 显式筛「缺失文件」但没有缺失 → 空结果
        if q and q.strip():
            kw = f"%{q.strip()}%"
            where.append("""(a.title LIKE ? OR au.name LIKE ? OR a.artwork_id LIKE ?
                         OR a.author_id LIKE ? OR tg.tag_name LIKE ?
                         OR EXISTS (SELECT 1 FROM pages p WHERE p.artwork_id = a.artwork_id
                                    AND p.local_path LIKE ?))""")
            params += [kw, kw, kw, kw, kw, kw]
        if illust_type is not None and illust_type != '':
            where.append("a.illust_type = ?")
            params.append(int(illust_type))
        if is_r18 is not None and is_r18 != '':
            where.append("a.is_r18 = ?")
            params.append(1 if str(is_r18) in ('1', 'true', 'r18') else 0)
        if author_id:
            where.append("a.author_id = ?")
            params.append(str(author_id))
        if tag:
            where.append("tg.tag_name = ?")
            params.append(str(tag))
        if missing:
            # 「缺元数据」= 还没补全且**补得回来**。已删除作品(deleted_at 非空)排除,
            # 否则每轮补全都去请求 404 空转 (v011 前靠标题占位文案挡住, 现在靠字段)。
            # 只算 pixiv 正式作品(纯数字 ID) —— 与 count_missing_metadata 同口径。
            # 外来源(ext_*)作品本来就没有 pixiv 封面(thumb_url 恒为 NULL), 算进来会变成
            # 两张永远补不掉的「缺元数据」。
            where.append("a.deleted_at IS NULL "
                         "AND a.artwork_id GLOB '[0-9]*' AND a.artwork_id NOT GLOB '*[^0-9]*' "
                         "AND (a.tags_json IS NULL "
                         "OR a.title IS NULL OR a.title = '' OR a.thumb_url IS NULL)")
        if deleted:
            where.append("a.deleted_at IS NOT NULL")
        if author_status == 'alive':
            where.append("a.author_id IS NOT NULL AND a.author_id != '' AND au.status = 'alive'")
        elif author_status == 'gone':
            where.append("a.author_id IS NOT NULL AND a.author_id != '' AND au.status = 'gone'")
        elif author_status == 'unknown':
            # 「未知作者」= 作品本身没有作者 (其他来源无作者文件等);
            # 注意不能用 au.status IS NULL: 未联网核对的已知作者会被误捞
            where.append("(a.author_id IS NULL OR a.author_id = '')")
        if source:
            # 来源已细分 (pixiv/danbooru/twitter/bilibili/ggac/other), 精确匹配;
            # NULL 视为 pixiv, 'other' 只匹配真正未归类的外来作品
            where.append("COALESCE(a.source, 'pixiv') = ?")
            params.append(str(source))
        if ai == '2':
            where.append("a.ai_type = 2")
        elif ai == '0':
            # 「非 AI」把还没补过 aiType 的 (NULL) 也算进来 —— 藏起来会让人以为图丢了
            where.append("COALESCE(a.ai_type, 0) <> 2")

        sort_map = {
            'date': "COALESCE(a.publish_date, a.created_at)",
            'created': "a.created_at",
            # 修改时间: 缺失时退回入库时间, 避免 NULL 排到最后/最前造成顺序怪异
            'updated': "COALESCE(a.updated_at, a.created_at)",
            'title': "a.title",
            'author': "COALESCE(au.name, a.author_id)",
            'id': "a.artwork_id",
            'bookmark': "a.bookmark_count",
        }
        order_by = sort_map.get(sort, sort_map['date'])
        direction = "ASC" if str(order).lower() == 'asc' else "DESC"
        # 收藏/日期等大量同值时加 ID 兜底排序, 避免翻页顺序不稳定 (切换升降序有可感知变化)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT a.*, au.name AS author_name, au.status AS author_status
              FROM artworks a
         LEFT JOIN authors au ON a.author_id = au.author_id
         LEFT JOIN artwork_tags atg ON atg.artwork_id = a.artwork_id
         LEFT JOIN tags tg          ON tg.tag_id = atg.tag_id
             {where_sql}
          ORDER BY {order_by} {direction}, a.artwork_id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def count_artworks_filtered(self, *, q: str = '', illust_type=None, is_r18=None,
                                ai: "str | None" = None,
                                author_id: "str | None" = None, tag: "str | None" = None,
                                missing: bool = False,
                                file_missing_ids: "set | list | None" = None,
                                deleted: bool = False,
                                trashed: bool = False,
                                author_status: "str | None" = None,
                                source: "str | None" = None,
                                only_aid: "str | None" = None) -> int:
        """list_artworks 同条件的结果总数 (分页/统计用)。"""
        where, params = [], []
        # 与 list_artworks 同一条回收站过滤 (两处必须一致, 否则页码/总数对不上)
        where.append("a.trashed_at IS NOT NULL" if trashed else "a.trashed_at IS NULL")
        if only_aid:
            # 「仅查看该作品」: 精确 ID 硬过滤 (2026-09-16), 不能用 q 的 LIKE 误伤
            where.append("a.artwork_id = ?")
            params.append(str(only_aid))
        if file_missing_ids is not None:
            ids = [str(x) for x in file_missing_ids]
            if ids:
                where.append(f"a.artwork_id IN ({','.join('?' * len(ids))})")
                params += ids
            else:
                where.append("1=0")
        if q and q.strip():
            kw = f"%{q.strip()}%"
            where.append("""(a.title LIKE ? OR au.name LIKE ? OR a.artwork_id LIKE ?
                         OR a.author_id LIKE ? OR tg.tag_name LIKE ?
                         OR EXISTS (SELECT 1 FROM pages p WHERE p.artwork_id = a.artwork_id
                                    AND p.local_path LIKE ?))""")
            params += [kw, kw, kw, kw, kw, kw]
        if illust_type is not None and illust_type != '':
            where.append("a.illust_type = ?")
            params.append(int(illust_type))
        if is_r18 is not None and is_r18 != '':
            where.append("a.is_r18 = ?")
            params.append(1 if str(is_r18) in ('1', 'true', 'r18') else 0)
        if author_id:
            where.append("a.author_id = ?")
            params.append(str(author_id))
        if tag:
            where.append("tg.tag_name = ?")
            params.append(str(tag))
        if missing:
            # 「缺元数据」= 还没补全且**补得回来**。已删除作品(deleted_at 非空)排除,
            # 否则每轮补全都去请求 404 空转 (v011 前靠标题占位文案挡住, 现在靠字段)。
            # 只算 pixiv 正式作品(纯数字 ID) —— 与 count_missing_metadata 同口径。
            # 外来源(ext_*)作品本来就没有 pixiv 封面(thumb_url 恒为 NULL), 算进来会变成
            # 两张永远补不掉的「缺元数据」。
            where.append("a.deleted_at IS NULL "
                         "AND a.artwork_id GLOB '[0-9]*' AND a.artwork_id NOT GLOB '*[^0-9]*' "
                         "AND (a.tags_json IS NULL "
                         "OR a.title IS NULL OR a.title = '' OR a.thumb_url IS NULL)")
        if deleted:
            where.append("a.deleted_at IS NOT NULL")
        if author_status == 'alive':
            where.append("a.author_id IS NOT NULL AND a.author_id != '' AND au.status = 'alive'")
        elif author_status == 'gone':
            where.append("a.author_id IS NOT NULL AND a.author_id != '' AND au.status = 'gone'")
        elif author_status == 'unknown':
            # 「未知作者」= 作品本身没有作者 (其他来源无作者文件等)
            where.append("(a.author_id IS NULL OR a.author_id = '')")
        if source:
            # 来源已细分 (pixiv/danbooru/twitter/bilibili/ggac/other), 精确匹配;
            # NULL 视为 pixiv, 'other' 只匹配真正未归类的外来作品
            where.append("COALESCE(a.source, 'pixiv') = ?")
            params.append(str(source))
        if ai == '2':
            where.append("a.ai_type = 2")
        elif ai == '0':
            # 「非 AI」把还没补过 aiType 的 (NULL) 也算进来 —— 藏起来会让人以为图丢了
            where.append("COALESCE(a.ai_type, 0) <> 2")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        return self.conn.execute(
            f"""
            SELECT COUNT(DISTINCT a.artwork_id)
              FROM artworks a
         LEFT JOIN authors au ON a.author_id = au.author_id
         LEFT JOIN artwork_tags atg ON atg.artwork_id = a.artwork_id
         LEFT JOIN tags tg          ON tg.tag_id = atg.tag_id
             {where_sql}
            """,
            params,
        ).fetchone()[0]

    def get_artwork_page_list(self, artwork_ids: "list[str]") -> "dict[str, list[dict]]":
        """每作品的库内页清单: {artwork_id: [{page_num, local_path, file_size_bytes, width, height}]}。

        作品视图用——卡片显示「原网页 N 页 / 库内哪几张」, Lightbox 翻页也用这份清单。
        只收有 local_path 的页 (库内实际存在的图片), 按页码升序。
        """
        if not artwork_ids:
            return {}
        ids = [str(x) for x in artwork_ids]
        ph = ','.join('?' * len(ids))
        rows = self.conn.execute(
            f"SELECT artwork_id, page_num, local_path, file_size_bytes, width, height, origin "
            f"FROM pages WHERE artwork_id IN ({ph}) "
            f"  AND local_path IS NOT NULL AND local_path != '' "
            f"  AND trashed_at IS NULL "
            f"ORDER BY artwork_id, page_num",
            ids).fetchall()
        out: "dict[str, list[dict]]" = {}
        for r in rows:
            out.setdefault(str(r["artwork_id"]), []).append(dict(r))
        return out

    def list_images(self, *, q: str = '', illust_type=None, is_r18=None,
                    ai: "str | None" = None,
                    author_id: "str | None" = None, tag: "str | None" = None,
                    missing: bool = False, sort: str = 'date', order: str = 'desc',
                    limit: int = 200, offset: int = 0,
                    deleted: bool = False, author_status: "str | None" = None,
                    source: "str | None" = None,
                    only_aid: "str | None" = None) -> list[dict]:
        """图片视图: 以「页」为单位列出图库里所有图片 (pages 有 local_path 的页)。

        每行 = 一张图片, 附带所属作品/作者信息; 筛选条件与作品视图 list_artworks 一致
        (关键词也直接匹配本地文件名 local_path)。排序按作品维度 (发布/入库/标题/作者/ID)。
        """
        where, params = [], []
        # 图片视图同样必须排除回收站: ① 已软删的页 (单图软删) 不能还显示在图库;
        # ② 整个作品被软删时它的页也不能显示。两条缺一条, 删掉的图就会"复活"(2026-09-17)。
        where.append("p.trashed_at IS NULL AND a.trashed_at IS NULL")
        if only_aid:
            # 「仅查看该作品」: 精确 ID 硬过滤 (2026-09-16)
            where.append("a.artwork_id = ?")
            params.append(str(only_aid))
        if q and q.strip():
            kw = f"%{q.strip()}%"
            where.append("""(a.title LIKE ? OR au.name LIKE ? OR a.artwork_id LIKE ?
                         OR a.author_id LIKE ? OR tg.tag_name LIKE ?
                         OR p.local_path LIKE ?)""")
            params += [kw, kw, kw, kw, kw, kw]
        if illust_type is not None and illust_type != '':
            where.append("a.illust_type = ?")
            params.append(int(illust_type))
        if is_r18 is not None and is_r18 != '':
            where.append("a.is_r18 = ?")
            params.append(1 if str(is_r18) in ('1', 'true', 'r18') else 0)
        if author_id:
            where.append("a.author_id = ?")
            params.append(str(author_id))
        if tag:
            where.append("tg.tag_name = ?")
            params.append(str(tag))
        if missing:
            # 「缺元数据」= 还没补全且**补得回来**。已删除作品(deleted_at 非空)排除,
            # 否则每轮补全都去请求 404 空转 (v011 前靠标题占位文案挡住, 现在靠字段)。
            # 只算 pixiv 正式作品(纯数字 ID) —— 与 count_missing_metadata 同口径。
            # 外来源(ext_*)作品本来就没有 pixiv 封面(thumb_url 恒为 NULL), 算进来会变成
            # 两张永远补不掉的「缺元数据」。
            where.append("a.deleted_at IS NULL "
                         "AND a.artwork_id GLOB '[0-9]*' AND a.artwork_id NOT GLOB '*[^0-9]*' "
                         "AND (a.tags_json IS NULL "
                         "OR a.title IS NULL OR a.title = '' OR a.thumb_url IS NULL)")
        if deleted:
            where.append("a.deleted_at IS NOT NULL")
        if author_status == 'alive':
            where.append("a.author_id IS NOT NULL AND a.author_id != '' AND au.status = 'alive'")
        elif author_status == 'gone':
            where.append("a.author_id IS NOT NULL AND a.author_id != '' AND au.status = 'gone'")
        elif author_status == 'unknown':
            where.append("(a.author_id IS NULL OR a.author_id = '')")
        if source:
            where.append("COALESCE(a.source, 'pixiv') = ?")
            params.append(str(source))
        if ai == '2':
            where.append("a.ai_type = 2")
        elif ai == '0':
            # 「非 AI」把还没补过 aiType 的 (NULL) 也算进来 —— 藏起来会让人以为图丢了
            where.append("COALESCE(a.ai_type, 0) <> 2")
        sort_map = {
            'date': "COALESCE(a.publish_date, a.created_at)",
            'created': "a.created_at",
            # 修改时间: 缺失时退回入库时间, 避免 NULL 排到最后/最前造成顺序怪异
            'updated': "COALESCE(a.updated_at, a.created_at)",
            'title': "a.title",
            'author': "COALESCE(au.name, a.author_id)",
            'id': "a.artwork_id",
            'bookmark': "a.bookmark_count",
        }
        order_by = sort_map.get(sort, sort_map['date'])
        direction = "ASC" if str(order).lower() == 'asc' else "DESC"
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT p.artwork_id, p.page_num, p.local_path,
                            p.file_size_bytes, p.width, p.height, p.origin,
                            a.title, a.illust_type, a.is_r18, a.source,
                            a.source_url, a.source_author, a.thumb_url, a.deleted_at,
                            a.total_pages AS page_count, a.author_id,
                            au.name AS author_name, au.status AS author_status
              FROM pages p
              JOIN artworks a ON a.artwork_id = p.artwork_id
         LEFT JOIN authors au ON a.author_id = au.author_id
         LEFT JOIN artwork_tags atg ON atg.artwork_id = a.artwork_id
         LEFT JOIN tags tg          ON tg.tag_id = atg.tag_id
             {where_sql}
          ORDER BY {order_by} {direction}, p.artwork_id DESC, p.page_num
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def count_images_filtered(self, *, q: str = '', illust_type=None, is_r18=None,
                              ai: "str | None" = None,
                              author_id: "str | None" = None, tag: "str | None" = None,
                              missing: bool = False, deleted: bool = False,
                              author_status: "str | None" = None,
                              source: "str | None" = None,
                              only_aid: "str | None" = None) -> int:
        """list_images 同条件的图片总数 (分页用, 按页去重计数)。"""
        where, params = [], []
        # 图片视图同样必须排除回收站: ① 已软删的页 (单图软删) 不能还显示在图库;
        # ② 整个作品被软删时它的页也不能显示。两条缺一条, 删掉的图就会"复活"(2026-09-17)。
        where.append("p.trashed_at IS NULL AND a.trashed_at IS NULL")
        if only_aid:
            # 「仅查看该作品」: 精确 ID 硬过滤 (2026-09-16)
            where.append("a.artwork_id = ?")
            params.append(str(only_aid))
        if q and q.strip():
            kw = f"%{q.strip()}%"
            where.append("""(a.title LIKE ? OR au.name LIKE ? OR a.artwork_id LIKE ?
                         OR a.author_id LIKE ? OR tg.tag_name LIKE ?
                         OR p.local_path LIKE ?)""")
            params += [kw, kw, kw, kw, kw, kw]
        if illust_type is not None and illust_type != '':
            where.append("a.illust_type = ?")
            params.append(int(illust_type))
        if is_r18 is not None and is_r18 != '':
            where.append("a.is_r18 = ?")
            params.append(1 if str(is_r18) in ('1', 'true', 'r18') else 0)
        if author_id:
            where.append("a.author_id = ?")
            params.append(str(author_id))
        if tag:
            where.append("tg.tag_name = ?")
            params.append(str(tag))
        if missing:
            # 「缺元数据」= 还没补全且**补得回来**。已删除作品(deleted_at 非空)排除,
            # 否则每轮补全都去请求 404 空转 (v011 前靠标题占位文案挡住, 现在靠字段)。
            # 只算 pixiv 正式作品(纯数字 ID) —— 与 count_missing_metadata 同口径。
            # 外来源(ext_*)作品本来就没有 pixiv 封面(thumb_url 恒为 NULL), 算进来会变成
            # 两张永远补不掉的「缺元数据」。
            where.append("a.deleted_at IS NULL "
                         "AND a.artwork_id GLOB '[0-9]*' AND a.artwork_id NOT GLOB '*[^0-9]*' "
                         "AND (a.tags_json IS NULL "
                         "OR a.title IS NULL OR a.title = '' OR a.thumb_url IS NULL)")
        if deleted:
            where.append("a.deleted_at IS NOT NULL")
        if author_status == 'alive':
            where.append("a.author_id IS NOT NULL AND a.author_id != '' AND au.status = 'alive'")
        elif author_status == 'gone':
            where.append("a.author_id IS NOT NULL AND a.author_id != '' AND au.status = 'gone'")
        elif author_status == 'unknown':
            where.append("(a.author_id IS NULL OR a.author_id = '')")
        if source:
            where.append("COALESCE(a.source, 'pixiv') = ?")
            params.append(str(source))
        if ai == '2':
            where.append("a.ai_type = 2")
        elif ai == '0':
            # 「非 AI」把还没补过 aiType 的 (NULL) 也算进来 —— 藏起来会让人以为图丢了
            where.append("COALESCE(a.ai_type, 0) <> 2")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        return self.conn.execute(
            f"""
            SELECT COUNT(*) FROM (
              SELECT DISTINCT p.artwork_id, p.page_num
                FROM pages p
                JOIN artworks a ON a.artwork_id = p.artwork_id
           LEFT JOIN authors au ON a.author_id = au.author_id
           LEFT JOIN artwork_tags atg ON atg.artwork_id = a.artwork_id
           LEFT JOIN tags tg          ON tg.tag_id = atg.tag_id
                   {where_sql}
            )
            """,
            params,
        ).fetchone()[0]

    def get_artwork_local_paths(self, artwork_ids: "list[str]") -> "list[str]":
        """返回这些作品在 pages 表记录的所有本地文件相对路径(去空去重)。

        供 Web 层删除时同步删磁盘文件, 保证「数据库记录 ↔ 磁盘图片」一致,
        不再只删 DB 留下孤儿文件。
        """
        if not artwork_ids:
            return []
        placeholders = ','.join('?' * len(artwork_ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT local_path FROM pages "
            f"WHERE artwork_id IN ({placeholders}) "
            f"  AND local_path IS NOT NULL AND local_path != ''",
            artwork_ids,
        ).fetchall()
        return [r[0] for r in rows]

    def get_first_local_paths(self, artwork_ids: "list[str]") -> "dict[str, str]":
        """每个作品在 pages 里记录的第一个本地文件相对路径(用于图库本地缩略图)。
        返回 {artwork_id: local_path}。"""
        if not artwork_ids:
            return {}
        placeholders = ','.join('?' * len(artwork_ids))
        rows = self.conn.execute(
            f"SELECT artwork_id, local_path FROM pages "
            f"WHERE artwork_id IN ({placeholders}) "
            f"  AND local_path IS NOT NULL AND local_path != '' "
            f"  AND trashed_at IS NULL "
            f"ORDER BY artwork_id, page_num",
            artwork_ids,
        ).fetchall()
        out: "dict[str, str]" = {}
        for r in rows:
            out.setdefault(r['artwork_id'], r['local_path'])
        return out

    def get_first_page_info(self, artwork_ids: "list[str]") -> "dict[str, dict]":
        """每个作品的首图页记录 (page_num 最小): {artwork_id: {page_num, local_path,
        file_size_bytes, width, height}}。图库卡片显示文件大小/原始长宽用。"""
        if not artwork_ids:
            return {}
        placeholders = ','.join('?' * len(artwork_ids))
        rows = self.conn.execute(
            f"SELECT artwork_id, page_num, local_path, file_size_bytes, width, height "
            f"FROM pages WHERE artwork_id IN ({placeholders}) "
            f"  AND trashed_at IS NULL "
            f"ORDER BY artwork_id, page_num",
            artwork_ids,
        ).fetchall()
        out: "dict[str, dict]" = {}
        for r in rows:
            out.setdefault(r['artwork_id'], {
                'page_num': r['page_num'],
                'local_path': r['local_path'],
                'file_size_bytes': r['file_size_bytes'],
                'width': r['width'],
                'height': r['height'],
            })
        return out

    def fill_page_dimensions(self, aid_page_wh: "list[tuple[str, int, int, int]]") -> None:
        """批量回填 pages 的原始宽高 (COALESCE 只补空不覆盖)。
        aid_page_wh: [(artwork_id, page_num, width, height), ...]"""
        if not aid_page_wh:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE pages SET width=COALESCE(?, width), height=COALESCE(?, height) "
                "WHERE artwork_id=? AND page_num=?",
                [(w, h, str(aid), int(page)) for aid, page, w, h in aid_page_wh])

    def _relink_page(self, aid: str, page: int, rel: str,
                     md5: "str | None", size: "int | None") -> None:
        """文件被移动/改名: 把已有页记录重指到新路径。
        mark_page_status 的 ON CONFLICT 用 COALESCE 不会顶掉旧路径, 必须显式 UPDATE。"""
        with self.conn:
            self.conn.execute(
                "UPDATE pages SET local_path=?, status=?, "
                "md5=COALESCE(?, md5), file_size_bytes=COALESCE(?, file_size_bytes) "
                "WHERE artwork_id=? AND page_num=?",
                (rel, PSt.DONE, md5, size, str(aid), int(page)))

    def _backfill_page_fingerprint(self, aid: str, page: int,
                                   md5: "str | None", size: "int | None") -> None:
        """路径命中的老记录 md5 为空时, 首次对账顺手回填指纹/大小。"""
        with self.conn:
            self.conn.execute(
                "UPDATE pages SET md5=COALESCE(?, md5), "
                "file_size_bytes=COALESCE(?, file_size_bytes) "
                "WHERE artwork_id=? AND page_num=?",
                (md5, size, str(aid), int(page)))

    def _adopt_author_from_rel(self, aid: str, rel: str) -> None:
        """外置图片导入: 相对路径形如「作者名_作者ID/xxx_pn.jpg」时, 把文件夹名
        里的作者信息记入 authors 并回填 artworks.author_id (COALESCE, 不覆盖
        DB 已知作者)。纯本地信息, 不联网。
        注意: 文件夹名是下载/拖入时的旧昵称, 可能已过时, 故仅在作者「不存在」时
        用它建档; 已存在的作者绝不覆盖其当前名 (权威名以 pixiv 自检/下载为准),
        也不把权威名误记成曾用名。"""
        try:
            # 作者文件夹可能嵌套在分类目录下 (X图/作者名_作者ID/file), 扫描所有
            # 路径段找「名字_纯数字ID」那一段, 而不是只看顶层。
            name = folder_aid = None
            for seg in str(rel).replace("\\", "/").split("/"):
                n, _, a = seg.rpartition("_")
                n = (n or "").strip()
                if a.isdigit() and n:
                    name, folder_aid = n, a
                    break
            # 单向闭环护栏 (2026-09-14 审计): 夹名里那截不是可辨认的名字 (unknown / 未署名 /
            # 纯符号) 时不拿它建档, 也不回填 —— 宁可留空由用户在「指定作者」里定,
            # 也不要造一个叫「未知作者」的垃圾作者行。
            if not folder_aid or not folder_author_name_usable(name):
                return
            if self.get_author(folder_aid) is None:
                self.upsert_author(folder_aid, name)
            self.conn.execute(
                "UPDATE artworks SET author_id=? WHERE artwork_id=? "
                "AND (author_id IS NULL OR author_id='')",
                (folder_aid, str(aid)))
            self.conn.commit()
        except Exception:
            pass

    def reconcile_output(self, img_root: "Path", *, verify_corrupt: bool = False) -> ReconcileReport:
        """图库 ↔ 磁盘实时对账 (磁盘为路径真源, 双重身份识别)。

        身份识别:
          - 文件名自然键 {作品ID}_p{页码}: pixiv 下载自带, 第一身份;
          - MD5 内容指纹 (pages.md5): 文件被移动/改名后凭指纹认回。
        一次扫描完成:
          ① 路径命中且大小一致 → 不动, md5 空则顺手回填 (首次对账一次性成本);
          ② 路径不在 DB (移动/改名/新增) → 算 MD5:
             指纹命中已有页且旧路径已消失 → 重指路径(自愈);
             自然键命中已有页且旧路径已消失 → 重指路径;
             自然键是全新页 → 补写 pages;
          ③ 同一 MD5 磁盘上多份 → 重复副本报告;
          ④ DONE 作品自然键+指纹都找不到 → 缺失;
          ⑤ 空目录收集 / (可选) PIL 损坏校验。
        不删任何 DB 记录、不删磁盘文件——只对账与报告。
        """
        from utils.hashutil import md5_of
        img_root = Path(img_root)
        IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
        # ① 扫磁盘
        scan_files: "list[tuple[Path, str, int]]" = []
        disk_rels: "set[str]" = set()
        disk_by_aid: "dict[str, set[int]]" = {}
        for p in img_root.rglob('*'):
            if not p.is_file() or p.suffix.lower() not in IMG_EXT:
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            rel = str(p.relative_to(img_root)).replace("\\", "/")
            scan_files.append((p, rel, sz))
            disk_rels.add(rel)
            key = pixiv_natural_key(p.name)
            if key:
                disk_by_aid.setdefault(key[0], set()).add(key[1])
        # ② 空目录
        empty_dirs: "list[str]" = []
        for d in img_root.iterdir():
            if d.is_dir() and not any(f.is_file() and f.suffix.lower() in IMG_EXT for f in d.rglob('*')):
                empty_dirs.append(str(d))
        # ③ DB pages 快照 + 索引
        rows = self.conn.execute(
            "SELECT artwork_id, page_num, status, local_path, md5, file_size_bytes "
            "FROM pages WHERE trashed_at IS NULL"
        ).fetchall()
        db_by_rel: "dict[str, sqlite3.Row]" = {}
        db_by_md5: "dict[str, list[sqlite3.Row]]" = {}
        page_by_key: "dict[tuple[str, int], sqlite3.Row]" = {}
        for r in rows:
            page_by_key[(str(r['artwork_id']), int(r['page_num']))] = r
            if r['local_path']:
                db_by_rel[str(r['local_path']).replace("\\", "/")] = r
            if r['md5']:
                db_by_md5.setdefault(r['md5'], []).append(r)

        new_pages = 0
        relinked = 0
        conflicts: "list[dict]" = []
        relinked_keys: "set[tuple[str, int]]" = set()
        md5_of_disk: "dict[str, str]" = {}   # rel -> md5 (本次算过或从 DB 复用)
        for (p, rel, sz) in scan_files:
            if rel in db_by_rel:
                r = db_by_rel[rel]
                if not r['md5']:
                    h = md5_of(p)
                    if h:
                        self._backfill_page_fingerprint(r['artwork_id'], r['page_num'], h, sz)
                        md5_of_disk[rel] = h
                else:
                    md5_of_disk[rel] = r['md5']
                continue
            # 路径不在 DB: 移动/改名/新增 → 算指纹
            h = md5_of(p)
            if h:
                md5_of_disk[rel] = h
            target_row = None
            if h and h in db_by_md5:
                # 指纹命中: 优先旧路径已不在磁盘的那一页 (移动场景);
                # 旧路径还在 = 这份是副本, 不重指, 留给重复检测
                target_row = next(
                    (c for c in db_by_md5[h]
                     if (str(c['artwork_id']), int(c['page_num'])) not in relinked_keys
                     and (not c['local_path']
                          or str(c['local_path']).replace("\\", "/") not in disk_rels)),
                    None)
            nkey = pixiv_natural_key(p.name)
            if target_row is None and nkey:
                cand = page_by_key.get(nkey)
                if cand is not None and nkey not in relinked_keys:
                    old_rel = (str(cand['local_path']) or "").replace("\\", "/")
                    if not old_rel or old_rel not in disk_rels:
                        target_row = cand
            if target_row is not None:
                ta, tp = str(target_row['artwork_id']), int(target_row['page_num'])
                self._relink_page(ta, tp, rel, h, sz)
                relinked += 1
                relinked_keys.add((ta, tp))
                if h:
                    db_by_md5.setdefault(h, []).append(target_row)
            elif nkey:
                cand = page_by_key.get(nkey)
                if cand is None:
                    # 真正的全新页补登记 (含单页裸ID/大写_P0: pixiv 图, 建正式作品壳)
                    self.mark_page_status(nkey[0], nkey[1], PSt.DONE,
                                          local_path=rel, md5=h, file_size_bytes=sz)
                    new_pages += 1
                    # 外置图片导入: 文件在「作者名_作者ID/」文件夹里 → 本地已有信息,
                    # 当场回填作者 (不联网); 不覆盖 DB 已知作者
                    self._adopt_author_from_rel(nkey[0], rel)
                else:
                    # 同自然键页面已登记且旧文件仍在磁盘上, 这份是内容不同的冒名副本
                    # (如截图改个名/张冠李戴的导入)。绝不能顶掉已有登记, 仅上报冲突
                    conflicts.append({
                        "artwork_id": nkey[0], "page": nkey[1],
                        "registered": (str(cand['local_path']) or "").replace("\\", "/"),
                        "path": rel,
                    })
            # 无指纹无自然键: 外来散落图, 由自检/反搜处理
        # ④ 重复检测: 同一 MD5 磁盘上有多份
        md5_groups: "dict[str, list[str]]" = {}
        for rel, h in md5_of_disk.items():
            md5_groups.setdefault(h, []).append(rel)
        duplicates: "list[dict]" = []
        for h, rels in md5_groups.items():
            if len(rels) < 2:
                continue
            aid = page = None
            for rel in rels:
                mkey = pixiv_natural_key(Path(rel).name)
                if mkey:
                    aid, page = mkey[0], mkey[1]
                    break
            if not aid and db_by_md5.get(h):
                r0 = db_by_md5[h][0]
                aid, page = str(r0['artwork_id']), int(r0['page_num'])
            duplicates.append({"md5": h, "artwork_id": aid, "page": page,
                               "paths": sorted(rels)})
        # ⑤ DONE 作品自然键+指纹都找不到 → 缺失
        downloaded_ids = [str(r['artwork_id']) for r in self.conn.execute(
            "SELECT DISTINCT artwork_id FROM pages WHERE status=? AND trashed_at IS NULL "
            "AND ((local_path IS NOT NULL AND local_path != '') "
            "     OR (md5 IS NOT NULL AND md5 != ''))",
            (PSt.DONE,)).fetchall()]
        present_md5 = set(md5_groups.keys())
        missing_ids: "list[str]" = []
        for aid in downloaded_ids:
            if aid in disk_by_aid:
                continue
            page_md5s = [r['md5'] for r in self.conn.execute(
                "SELECT DISTINCT md5 FROM pages WHERE artwork_id=? AND trashed_at IS NULL "
                "AND md5 IS NOT NULL AND md5 != ''", (aid,)).fetchall()]
            if any(h in present_md5 for h in page_md5s):
                continue  # 改名了但指纹还在
            missing_ids.append(aid)
        # ⑥ 损坏文件(可选, 需 PIL)
        #   判据只有两条: ① 空/极小文件(一定打不开); ② PIL 开图/校验**真的抛异常**。
        #   2026-09-19 修: 原先还有一条「>60MB 一律判损坏」——本意是"太大就不开图, 防撑爆内存",
        #   但它把"没校验"直接说成了"损坏": 用户那张 63.5MB / 1.27 亿像素的**正常**大图
        #   (128028374_p0.png) 被报成损坏图, 而且报告里查不到是谁 (① 只显示一个数字)。
        #   Image.open() 只读文件头、verify() 只校验结构, 都不解码像素 → 大图也不会撑爆内存;
        #   真·图片炸弹由 PIL 自己的 MAX_IMAGE_PIXELS 兜底, 那是"策略限制"不是"文件坏了",
        #   同样不能算损坏 (否则又变成一条查不出原因的假报)。
        corrupt_files: "list[str]" = []
        if verify_corrupt:
            try:
                import warnings as _warnings
                from PIL import Image
            except Exception:
                verify_corrupt = False
            if verify_corrupt:
                _bomb = getattr(Image, "DecompressionBombError", None)
                for p, _rel, _sz in scan_files:
                    try:
                        if p.stat().st_size < 128:  # 空/极小文件直接判坏
                            corrupt_files.append(str(p))
                            continue
                        with _warnings.catch_warnings():
                            # 超大图的 DecompressionBombWarning、坏 EXIF 的 UserWarning
                            # 都只是**警告**: 既别拿去判坏, 也别刷进日志
                            _warnings.simplefilter("ignore")
                            with Image.open(p) as im:
                                im.verify()
                    except Exception as _e:
                        if _bomb is not None and isinstance(_e, _bomb):
                            continue      # 像素数超 PIL 上限 → 跳过校验, 不报损坏
                        corrupt_files.append(str(p))
        db_count = self.conn.execute(
            "SELECT COUNT(*) FROM artworks WHERE trashed_at IS NULL").fetchone()[0] or 0
        # 对账日志只在「本次扫描实际做了事」时记 (补页/自愈), 避免 15s 轮询 + 自检把
        # 同一行全零结果刷成日志洪流; 缺失/损坏/空目录/冲突等持久状态由健康面板/对账响应展示。
        if new_pages or relinked:
            self.reporter.log(
                f"[DB] 对账完成: 扫描 {len(scan_files)} 图, 缺失作品 {len(missing_ids)}, "
                f"空目录 {len(empty_dirs)}, 损坏 {len(corrupt_files)}, 补页 {new_pages}, "
                f"移动自愈 {relinked}, 重复 {len(duplicates)}"
            )
        return ReconcileReport(
            missing_ids=missing_ids,
            empty_dirs=empty_dirs,
            corrupt_files=corrupt_files,
            scanned_images=len(scan_files),
            new_pages_linked=new_pages,
            db_artwork_count=db_count,
            pages_relinked=relinked,
            duplicates=duplicates,
            conflicts=conflicts,
        )

    def delete_artworks(self, artwork_ids: "list[str]") -> int:
        """批量删除作品(级联删 pages/artwork_tags), 返回删除条数。"""
        if not artwork_ids:
            return 0
        placeholders = ','.join('?' * len(artwork_ids))
        with self.conn:
            cur = self.conn.execute(
                f"DELETE FROM artworks WHERE artwork_id IN ({placeholders})",
                artwork_ids,
            )
        # 清理孤儿标签 / 孤儿作者 / 孤儿 pages (不再被任何作品引用的行)
        self._cleanup_orphan_tags()
        self._cleanup_orphan_authors()
        self._cleanup_orphan_pages()
        return cur.rowcount

    def delete_pages(self, items: "list[dict]") -> "tuple[list[str], list[str]]":
        """按「页」删除单张图片 (图片视图的删除粒度): 删 pages 行。

        items: [{"artwork_id": "123", "page_num": 0}, ...]
        返回 (被删页的本地文件相对路径列表, 页被删空而整作品级联移除的 artwork_id 列表)。
        某作品的页全部删完时, 该作品 (元数据/标签/作者关联) 一并删除 —— 与「删整个作品」等价,
        保证不会留下 0 页的幽灵作品; 仍有剩余页的作品只删指定页, 作品本身保留。
        """
        if not items:
            return [], []
        rel_paths: "list[str]" = []
        affected: "set[str]" = set()
        with self.conn:
            for it in items:
                aid = str(it.get("artwork_id") or "").strip()
                if not aid:
                    continue
                try:
                    pn = int(it.get("page_num") or 0)
                except (TypeError, ValueError):
                    pn = 0
                row = self.conn.execute(
                    "SELECT local_path FROM pages WHERE artwork_id=? AND page_num=?",
                    (aid, pn),
                ).fetchone()
                if not row:
                    continue   # 页不存在 (重复提交/已被删), 跳过
                if row[0]:
                    rel_paths.append(row[0])
                affected.add(aid)
                self.conn.execute(
                    "DELETE FROM pages WHERE artwork_id=? AND page_num=?",
                    (aid, pn),
                )
        # 页被删空的作品 → 整作品级联删除 (delete_artworks 内部清孤儿标签/作者/pages)
        emptied = [
            aid for aid in sorted(affected)
            if self.conn.execute(
                "SELECT COUNT(*) FROM pages WHERE artwork_id=?", (aid,)
            ).fetchone()[0] == 0
        ]
        if emptied:
            self.delete_artworks(emptied)
        # 仍有剩余页的作品 → 重算状态 
        # 作品还挂着 DONE, 搜索页按钮仍显示「✓ 已入库」而不是「⬇ 补下缺失页」)。
        # 与拆分/合并走同一个 _refresh_work_progress: 保留 total_pages, 缺页即 NOT_YET。
        for aid in sorted(affected):
            if aid not in emptied:
                self._refresh_work_progress(aid)
        return rel_paths, emptied

    # ==================================================================
    # 应用内回收站 (软删除) —— 2026-09-17 需求
    # ==================================================================
    # 双层保护:
    #   ①「删除」只做**软删除** —— 打标记 + 把文件搬进 data_root/trash/, 一条记录都不删,
    #      随时可一键恢复;
    #   ② 只有在回收站里点「彻底删除」才卸载 DB 记录, 且文件只送 **Windows 电脑回收站**
    #      (永远可还原), 并写操作日志留痕。
    # 两条设计铁律 (踩过坑, 别改):
    #   · 磁盘隔离: 回收站放 `trash/` 于 img/ **之外** → 对账/自检/散落图整理等所有
    #     `img_root.rglob()` 扫描天然看不到它, 不会被当散落图搬走或被当孤儿清掉。
    #   · 路径不变: `local_path` 始终保持「原图库相对路径」不变 —— 它是"恢复时该搬回
    #     哪里"的权威答案, 软删只搬文件、绝不改它 → 恢复时 DB 路径一处都不用动。
    #   · 两套相对路径别搞混: `local_path` 相对 img_root; `trash_rel_path` 相对
    #     **回收站根目录** (data_root/trash), 所以它**不带** trash/ 前缀。
    TRASH_DIRNAME = "trash"

    def _trash_rel(self, artwork_id: str, page_num: int, local_path: str) -> str:
        """该页在**回收站根目录内**的相对路径: <作品ID>/p<页码>__<原文件名>。

        不含 "trash/" 前缀 —— 回收站根目录由 server 层 `_trash_root()`(= data_root/trash)
        决定, 这里只给它在内部的相对位置 (2026-09-17 踩过坑: 带前缀会和根目录叠成
        data_root/trash/trash/...)。
        """
        base = Path(str(local_path or "").replace("\\", "/")).name
        return f"{artwork_id}/p{int(page_num)}__{base}"

    def _trash_move_pair(self, aid: str, row) -> "tuple[str | None, str | None]":
        """恢复时的一次搬移: (回收站相对路径 → 图库相对路径)。任一为空则视为无可搬。"""
        dst = str(row["local_path"] or "").replace("\\", "/")
        src = str(row["trash_rel_path"] or "").replace("\\", "/")
        if not src and dst:
            src = self._trash_rel(aid, int(row["page_num"]), dst)
        return (src, dst) if (src and dst) else (None, None)

    def soft_delete_artworks(self, artwork_ids: "list[str]", reason: str = "user") -> dict:
        """把若干作品**软删除**(进应用内回收站)。返回磁盘搬移计划。

        与旧 delete_artworks 的根本区别: **一条 DB 记录都不删**, 只打 trashed_at 标记。
        文件搬移只算不动 (沿用 merge_pages 的约定), 由调用方按 rel_moves 执行 ——
        这样 DB 与磁盘要么一起成功, 要么都不动。
        返回 {"count", "ids", "rel_moves": [(src_rel, dst_rel), ...]}。
        """
        ids = [str(x) for x in (artwork_ids or []) if str(x).strip()]
        if not ids:
            return {"count": 0, "ids": [], "rel_moves": []}
        ph = ','.join('?' * len(ids))
        rows = self.conn.execute(
            f"SELECT artwork_id, page_num, local_path FROM pages "
            f"WHERE artwork_id IN ({ph}) AND trashed_at IS NULL "
            f"  AND local_path IS NOT NULL AND local_path != ''", ids).fetchall()
        rel_moves = []
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE artworks SET trashed_at=datetime('now'), trashed_reason=?, "
                f"updated_at=datetime('now') "
                f"WHERE artwork_id IN ({ph}) AND trashed_at IS NULL", (reason, *ids))
            for r in rows:
                aid = str(r["artwork_id"])
                pn = int(r["page_num"])
                src = str(r["local_path"]).replace("\\", "/")
                dst = self._trash_rel(aid, pn, src)
                self.conn.execute(
                    "UPDATE pages SET trashed_at=datetime('now'), trash_rel_path=? "
                    "WHERE artwork_id=? AND page_num=?", (dst, aid, pn))
                rel_moves.append((src, dst))
        return {"count": int(cur.rowcount or 0), "ids": ids, "rel_moves": rel_moves}

    def soft_delete_pages(self, items: "list[dict]", reason: str = "user") -> dict:
        """按「页」软删除单张图片 (图片视图的删除粒度)。

        items: [{"artwork_id","page_num"}, ...]
        关键 : **不级联把整个作品也标记进回收站** ——
        删一张图就只是少一张图。若某作品因此变成 0 个有效页, 把它列进 would_empty,
        由调用方弹窗让用户选「保留作品(0页)」还是「一并进回收站」。
        返回 {"rel_moves", "would_empty", "count"}。
        """
        rel_moves = []
        affected, n = set(), 0
        with self.conn:
            for it in (items or []):
                if not isinstance(it, dict):
                    continue
                aid = str(it.get("artwork_id") or "").strip()
                if not aid:
                    continue
                try:
                    pn = int(it.get("page_num") or 0)
                except (TypeError, ValueError):
                    continue
                row = self.conn.execute(
                    "SELECT local_path FROM pages "
                    "WHERE artwork_id=? AND page_num=? AND trashed_at IS NULL",
                    (aid, pn)).fetchone()
                if not row:
                    continue   # 不存在 / 已在回收站 → 跳过
                src = str(row[0] or "").replace("\\", "/")
                if src:
                    dst = self._trash_rel(aid, pn, src)
                    self.conn.execute(
                        "UPDATE pages SET trashed_at=datetime('now'), trash_rel_path=? "
                        "WHERE artwork_id=? AND page_num=?", (dst, aid, pn))
                    rel_moves.append((src, dst))
                else:
                    self.conn.execute(
                        "UPDATE pages SET trashed_at=datetime('now') "
                        "WHERE artwork_id=? AND page_num=?", (aid, pn))
                affected.add(aid)
                n += 1
        # 哪些作品被删空了 (0 个未软删页) → 交前端弹窗决定
        would_empty = []
        for aid in sorted(affected):
            left = self.conn.execute(
                "SELECT COUNT(*) FROM pages WHERE artwork_id=? AND trashed_at IS NULL",
                (aid,)).fetchone()[0]
            if left == 0:
                would_empty.append(aid)
        # 所有受影响作品都要重算状态 —— 包括被删空的那个:
        # 软删不级联删作品, 作品还留在图库, 必须让它从「已入库」变成「缺页」才不骗人
        # (_refresh_work_progress 自己处理"0 个有效页"的情况)。
        for aid in sorted(affected):
            self._refresh_work_progress(aid)
        return {"rel_moves": rel_moves, "would_empty": would_empty, "count": n}

    def pages_would_empty(self, items: "list[dict]") -> "list[str]":
        """预检(只读): 若删掉这些页, 哪些作品会变成 **0 个有效页**。

        供「删除图片会导致作品全空」时弹窗让用户选「保留作品(0页)/一并进回收站」
。不改任何数据。
        """
        by: "dict[str, set]" = {}
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            aid = str(it.get("artwork_id") or "").strip()
            if not aid:
                continue
            try:
                pn = int(it.get("page_num"))
            except (TypeError, ValueError):
                continue
            by.setdefault(aid, set()).add(pn)
        out = []
        for aid, pns in by.items():
            alive = {int(r[0]) for r in self.conn.execute(
                "SELECT page_num FROM pages WHERE artwork_id=? AND trashed_at IS NULL",
                (aid,)).fetchall()}
            # alive 为空 = 本来就 0 有效页 (不是"这次删成空"); 只有"删完变空"才需要设计约定
            if alive and alive <= pns:
                out.append(aid)
        return sorted(out)

    def list_trashed(self, kind: str = "artwork") -> dict:
        """回收站清单。kind='artwork'(作品粒度) / 'page'(单图粒度)。

        两种粒度**互不重复**: 作品粒度只列"作品本身被删"的; 单图粒度只列
        "作品还在、但其中若干页被删"的那些页 —— 用户一眼能分清删的是整个作品还是一张图。
        """
        if kind == "page":
            rows = self.conn.execute(
                """SELECT p.artwork_id, p.page_num, p.local_path, p.trash_rel_path,
                          p.trashed_at, p.origin, p.file_size_bytes,
                          a.title, a.author_id, a.is_r18, au.name AS author_name
                     FROM pages p
                     JOIN artworks a ON a.artwork_id = p.artwork_id
                LEFT JOIN authors au ON au.author_id = a.author_id
                    WHERE p.trashed_at IS NOT NULL AND a.trashed_at IS NULL
                 ORDER BY p.trashed_at DESC, p.artwork_id, p.page_num""").fetchall()
            items = [dict(r) for r in rows]
            return {"kind": "page", "items": items, "total": len(items)}
        rows = self.conn.execute(
            """SELECT a.artwork_id, a.title, a.author_id, a.trashed_at, a.trashed_reason,
                      a.thumb_url, a.source, a.total_pages, a.is_r18, a.status,
                      au.name AS author_name
                 FROM artworks a
            LEFT JOIN authors au ON au.author_id = a.author_id
                WHERE a.trashed_at IS NOT NULL
             ORDER BY a.trashed_at DESC, a.artwork_id""").fetchall()
        ids = [str(r["artwork_id"]) for r in rows]
        counts, covers = {}, {}
        if ids:
            ph = ','.join('?' * len(ids))
            for pr in self.conn.execute(
                    f"SELECT artwork_id, page_num, trash_rel_path FROM pages "
                    f"WHERE artwork_id IN ({ph}) AND trashed_at IS NOT NULL "
                    f"ORDER BY artwork_id, page_num", ids).fetchall():
                k = str(pr["artwork_id"])
                counts[k] = counts.get(k, 0) + 1
                covers.setdefault(k, pr["trash_rel_path"])
        items = []
        for r in rows:
            d = dict(r)
            d["page_count"] = counts.get(str(r["artwork_id"]), 0)
            d["cover_trash"] = covers.get(str(r["artwork_id"]))
            items.append(d)
        return {"kind": "artwork", "items": items, "total": len(items)}

    def _page_conflicts(self, aid: str, img_root: "Path | None",
                        only_pns: "set | None" = None,
                        trash_root: "Path | None" = None) -> "list[dict]":
        """检查某作品待恢复的页有没有冲突 (页号被占 / 原磁盘路径被不同内容占用)。

        trash_root: 回收站根目录 (data_root/trash)。**注意它不在 img_root 里面** ——
        判断"回收站里到底有没有这份文件"必须去它下面找, 不能在 img_root 下找
        (2026-09-17 踩过: 在 img_root 下找永远找不到 → 冲突检测形同虚设)。
        """
        from utils.hashutil import md5_of
        out = []
        rows = self.conn.execute(
            "SELECT page_num, local_path, md5, trash_rel_path FROM pages "
            "WHERE artwork_id=? AND trashed_at IS NOT NULL", (str(aid),)).fetchall()
        troot = Path(trash_root) if trash_root else None
        for r in rows:
            pn = int(r["page_num"])
            if only_pns is not None and pn not in only_pns:
                continue
            if self.conn.execute(
                    "SELECT 1 FROM pages WHERE artwork_id=? AND page_num=? "
                    "AND trashed_at IS NULL", (str(aid), pn)).fetchone():
                out.append({"artwork_id": str(aid), "page_num": pn,
                            "type": "page_taken",
                            "detail": f"p{pn} 页号已被图库中现有内容占用"})
                continue
            rel = str(r["local_path"] or "").replace("\\", "/")
            src_rel = str(r["trash_rel_path"] or "").replace("\\", "/")
            if img_root is None or not rel:
                continue
            # 回收站里根本没有这份文件 → 没有"搬回"这一步, 也就谈不上冲突
            # (文件可能压根没搬成功/被外部处理过)。恢复时只清标记即可, 不能卡住用户。
            if troot is not None and (not src_rel or not (troot / src_rel).is_file()):
                continue
            root = Path(img_root)
            tgt = root / rel
            if not tgt.is_file():
                continue
            # 原路径已有文件: 内容相同 → 同一张图, 不算冲突 (直接清标记);
            # 内容不同(或都算不出指纹) → 才判"被占用", 绝不能覆盖。
            h_src = r["md5"] or (troot and src_rel and md5_of(troot / src_rel)) or None
            h_dst = md5_of(tgt)
            if h_src and h_dst and h_src == h_dst:
                continue
            out.append({"artwork_id": str(aid), "page_num": pn,
                        "type": "path_taken",
                        "detail": f"原路径已被另一份不同内容的文件占用：{rel}"})
        return out

    def preview_restore(self, artwork_ids: "list[str] | None" = None,
                        items: "list[dict] | None" = None,
                        img_root: "Path | None" = None,
                        trash_root: "Path | None" = None) -> dict:
        """恢复**之前**的冲突预检 (只读, 不改任何东西)。

        为什么必须有 : 软删除只是标记, 从"删"到"想恢复"
        之间数据库/磁盘可能已经变了 —— 原页号被补下的页占了、原磁盘路径被新文件占了。
        恢复**绝不能覆盖库中现有内容**, 所以先体检、把冲突项列出来, 恢复时只**跳过**它们
        (用户明确只接受这一种策略)。
        返回 {"ok": [...], "conflicts": [...]}。
        """
        ok, conflicts = [], []
        for aid in (artwork_ids or []):
            aid = str(aid)
            a = self.conn.execute(
                "SELECT trashed_at FROM artworks WHERE artwork_id=?", (aid,)).fetchone()
            if not a or not a[0]:
                conflicts.append({"item": {"artwork_id": aid}, "type": "not_trashed",
                                  "detail": "该作品不在回收站里（可能已被恢复或彻底删除）"})
                continue
            c = self._page_conflicts(aid, img_root, trash_root=trash_root)
            if c:
                detail = "; ".join(x["detail"] for x in c[:3])
                if len(c) > 3:
                    detail += f" 等 {len(c)} 处"
                conflicts.append({"item": {"artwork_id": aid}, "type": c[0]["type"],
                                  "detail": detail})
            else:
                ok.append({"artwork_id": aid})
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            aid = str(it.get("artwork_id") or "").strip()
            try:
                pn = int(it.get("page_num"))
            except (TypeError, ValueError):
                continue
            if not aid:
                continue
            c = self._page_conflicts(aid, img_root, only_pns={pn}, trash_root=trash_root)
            if c:
                conflicts.append({"item": {"artwork_id": aid, "page_num": pn},
                                  "type": c[0]["type"], "detail": c[0]["detail"]})
            else:
                ok.append({"artwork_id": aid, "page_num": pn})
        return {"ok": ok, "conflicts": conflicts}

    def restore_trashed(self, artwork_ids: "list[str] | None" = None,
                        items: "list[dict] | None" = None,
                        img_root: "Path | None" = None,
                        trash_root: "Path | None" = None) -> dict:
        """从回收站恢复: 无冲突的搬回原位并清标记; 冲突项**跳过**并回报原因。

        只跳过、绝不覆盖。
        作品粒度采用"整作品一起恢复": 只要有一页冲突就整作品跳过 —— 避免出现
        "恢复了一半、作品半空"的中间态, 用户也更容易理解。
        返回 {"restored_artworks", "restored_pages", "skipped",
              "rel_moves": [(回收站相对路径, 图库相对路径), ...]}。
        """
        pre = self.preview_restore(artwork_ids, items, img_root, trash_root)
        skipped = list(pre["conflicts"])
        blocked_art = {str(c["item"].get("artwork_id")) for c in skipped
                       if "page_num" not in c["item"]}
        blocked_pages = {(str(c["item"].get("artwork_id")), int(c["item"]["page_num"]))
                         for c in skipped if "page_num" in c["item"]}
        restored_artworks, restored_pages, rel_moves, refreshed = [], [], [], set()
        with self.conn:
            # ① 作品粒度
            for aid in (artwork_ids or []):
                aid = str(aid)
                if aid in blocked_art:
                    continue
                for r in self.conn.execute(
                        "SELECT page_num, local_path, trash_rel_path FROM pages "
                        "WHERE artwork_id=? AND trashed_at IS NOT NULL", (aid,)).fetchall():
                    src, dst = self._trash_move_pair(aid, r)
                    if src and dst:
                        rel_moves.append((src, dst))
                self.conn.execute(
                    "UPDATE pages SET trashed_at=NULL, trash_rel_path=NULL "
                    "WHERE artwork_id=? AND trashed_at IS NOT NULL", (aid,))
                self.conn.execute(
                    "UPDATE artworks SET trashed_at=NULL, trashed_reason=NULL, "
                    "updated_at=datetime('now') WHERE artwork_id=?", (aid,))
                restored_artworks.append(aid)
                refreshed.add(aid)
            # ② 单图粒度
            for it in (items or []):
                if not isinstance(it, dict):
                    continue
                aid = str(it.get("artwork_id") or "").strip()
                try:
                    pn = int(it.get("page_num"))
                except (TypeError, ValueError):
                    continue
                if not aid or (aid, pn) in blocked_pages:
                    continue
                r = self.conn.execute(
                    "SELECT page_num, local_path, trash_rel_path FROM pages "
                    "WHERE artwork_id=? AND page_num=? AND trashed_at IS NOT NULL",
                    (aid, pn)).fetchone()
                if not r:
                    continue
                src, dst = self._trash_move_pair(aid, r)
                if src and dst:
                    rel_moves.append((src, dst))
                self.conn.execute(
                    "UPDATE pages SET trashed_at=NULL, trash_rel_path=NULL "
                    "WHERE artwork_id=? AND page_num=?", (aid, pn))
                restored_pages.append({"artwork_id": aid, "page_num": pn})
                refreshed.add(aid)
        for aid in sorted(refreshed):
            self._refresh_work_progress(aid)
        return {"restored_artworks": restored_artworks, "restored_pages": restored_pages,
                "skipped": skipped, "rel_moves": rel_moves}

    def purge_trashed_artworks(self, artwork_ids: "list[str]") -> dict:
        """**彻底删除**(作品粒度): 卸载 DB 记录。文件已在回收站, 由调用方送电脑回收站。

        返回 {"ids", "rel_paths", "deleted"}; rel_paths 必须在删 DB **之前**取
        (删完就查不到路径了 —— server 层为这个顺序踩过一次坑)。
        """
        ids = [str(x) for x in (artwork_ids or []) if str(x).strip()]
        if not ids:
            return {"ids": [], "rel_paths": [], "deleted": 0}
        ph = ','.join('?' * len(ids))
        rows = self.conn.execute(
            f"SELECT trash_rel_path FROM pages "
            f"WHERE artwork_id IN ({ph}) AND trashed_at IS NOT NULL", ids).fetchall()
        rel_paths = [str(r[0]).replace("\\", "/") for r in rows if r[0]]
        n = self.delete_artworks(ids)   # 物理删 (pages 级联 + 清孤儿)
        return {"ids": ids, "rel_paths": rel_paths, "deleted": n}

    def purge_trashed_pages(self, items: "list[dict]") -> dict:
        """**彻底删除**(单图粒度): 卸载 pages 记录, 文件由调用方送电脑回收站。"""
        rel_paths, done, refreshed = [], [], set()
        with self.conn:
            for it in (items or []):
                if not isinstance(it, dict):
                    continue
                aid = str(it.get("artwork_id") or "").strip()
                try:
                    pn = int(it.get("page_num"))
                except (TypeError, ValueError):
                    continue
                if not aid:
                    continue
                r = self.conn.execute(
                    "SELECT trash_rel_path FROM pages "
                    "WHERE artwork_id=? AND page_num=? AND trashed_at IS NOT NULL",
                    (aid, pn)).fetchone()
                if not r:
                    continue
                if r[0]:
                    rel_paths.append(str(r[0]).replace("\\", "/"))
                self.conn.execute(
                    "DELETE FROM pages WHERE artwork_id=? AND page_num=?", (aid, pn))
                done.append({"artwork_id": aid, "page_num": pn})
                refreshed.add(aid)
        # 作品还在的话重算它的进度 (少了一张图 → 可能变缺页)
        for aid in sorted(refreshed):
            if self.conn.execute("SELECT 1 FROM artworks WHERE artwork_id=?",
                                 (aid,)).fetchone():
                self._refresh_work_progress(aid)
        return {"items": done, "rel_paths": rel_paths, "deleted": len(done)}

    def count_trashed(self) -> dict:
        """回收站数量 (导航角标用): 作品粒度 N 个 + 单图粒度 M 张。"""
        a = self.conn.execute(
            "SELECT COUNT(*) FROM artworks WHERE trashed_at IS NOT NULL").fetchone()[0]
        p = self.conn.execute(
            "SELECT COUNT(*) FROM pages p WHERE p.trashed_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM artworks a WHERE a.artwork_id=p.artwork_id "
            "            AND a.trashed_at IS NULL)").fetchone()[0]
        return {"artworks": int(a or 0), "pages": int(p or 0)}

    # ==================================================================
    # 壁纸库 
    # ==================================================================
    # 「壁纸」= 从图库某一页**派生**到壁纸夹的成品, 一行 = 一张壁纸。
    # 与 pages/artworks 的关系是普通列 + 索引, **刻意没建外键** —— 理由见
    # db/migrations/018_wallpapers.sql: 原图被彻底删除时壁纸记录要留下 (设计口径
    # 「只把对应壁纸标记」), 而 ON DELETE CASCADE 会悄悄删记录、NO ACTION 又会让
    # purge_trashed_artworks 因为「还有壁纸引用」而删不掉原图。
    # 代价: artwork_id/page_num 可能悬空 → 下面查列表一律 LEFT JOIN, 取不到标题
    # 时由调用方退回文件名显示。
    #
    # 与 pages 的**关键区别**: 壁纸没有「文件搬进回收站」这一步 —— 文件始终留在
    # 壁纸夹里 (桌面幻灯片还要接着播它), 所以这几个方法都只动 DB, 不产生 rel_moves。

    def list_wallpapers(self, include_trashed: bool = False,
                        artwork_id: "str | None" = None) -> "list[dict]":
        """列出壁纸 (默认只看未标记的), 带原图的标题/作者。

        一律 LEFT JOIN pages/artworks/authors: 血缘可能悬空 (原图已被彻底删除),
        这时 title/author_name 是 None —— 调用方退回文件名显示即可, 不要当错误。
        """
        sql = (
            "SELECT w.*, "
            "       p.local_path AS page_rel, p.md5 AS page_md5, "
            "       a.title AS title, a.author_id AS author_id, au.name AS author_name "
            "FROM wallpapers w "
            "LEFT JOIN pages p    ON p.artwork_id=w.artwork_id AND p.page_num=w.page_num "
            "LEFT JOIN artworks a ON a.artwork_id=w.artwork_id "
            "LEFT JOIN authors au ON au.author_id=a.author_id "
        )
        where, args = [], []
        if not include_trashed:
            where.append("w.trashed_at IS NULL")
        if artwork_id:
            where.append("w.artwork_id=?")
            args.append(str(artwork_id))
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY w.created_at DESC, w.id DESC"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def wallpaper_file_names(self, include_trashed: bool = False) -> "set[str]":
        """壁纸夹里**已被登记**的文件名集合 (生成时用它跳过已存在的, 实现幂等)。

        只比文件名, 不查磁盘 —— 磁盘上那份在不在、内容和记录对不对, 是对账该管的事。
        """
        sql = "SELECT file_name FROM wallpapers"
        if not include_trashed:
            sql += " WHERE trashed_at IS NULL"
        return {str(r[0]) for r in self.conn.execute(sql).fetchall()}

    def add_wallpapers(self, items: "list[dict]") -> dict:
        """批量登记壁纸。**幂等**: 文件名已登记 (且未标记) 的跳过, 不重复插入。

        items: [{"artwork_id", "page_num", "file_name", 可选 width/height/file_md5/
                 file_size_bytes/src_md5/crop_mode/crop_x0..y1}, ...]
        返回 {"added": n, "skipped": [file_name, ...]}。

        为什么不用 `INSERT OR IGNORE` 让唯一索引挡: 它会把 NOT NULL 之类的错误
        一起吞掉, 出问题什么都看不见。先查一次已登记的文件名、只插新的, 干净。
        """
        rows = [it for it in (items or []) if isinstance(it, dict)]
        if not rows:
            return {"added": 0, "skipped": []}
        seen = self.wallpaper_file_names()      # 未标记的那些才占名字
        add, skipped = [], []
        for it in rows:
            aid = str(it.get("artwork_id") or "").strip()
            fn = str(it.get("file_name") or "").strip()
            if not aid or not fn:
                continue
            if fn in seen:                      # 已登记 / 本批内重复 → 跳过
                skipped.append(fn)
                continue
            try:
                pn = int(it.get("page_num") or 0)
            except (TypeError, ValueError):
                pn = 0
            seen.add(fn)
            add.append((aid, pn, fn, it.get("file_md5"), it.get("file_size_bytes"),
                        it.get("width"), it.get("height"), it.get("crop_mode"),
                        it.get("crop_x0"), it.get("crop_y0"), it.get("crop_x1"),
                        it.get("crop_y1"), it.get("src_md5")))
        if not add:
            return {"added": 0, "skipped": skipped}
        with self.conn:
            self.conn.executemany(
                "INSERT INTO wallpapers(artwork_id, page_num, file_name, file_md5, "
                " file_size_bytes, width, height, crop_mode, crop_x0, crop_y0, "
                " crop_x1, crop_y1, src_md5) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", add)
        return {"added": len(add), "skipped": skipped}

    def soft_delete_wallpapers(self, ids: "list[int] | None" = None,
                               artwork_ids: "list[str] | None" = None,
                               reason: str = "user") -> dict:
        """标记壁纸 (软删)。**不碰文件** —— 文件留在壁纸夹里。

        两个入口 (可同时给, 取并集):
          ids         用户主动删某几张 (reason='user')
          artwork_ids 原图被删 → 连带标记 (reason='cascade'), 传原图ID即可
        返回 {"count", "ids"}。文件要不要送电脑回收站由调用方决定: 口径是
        「删除图库只标记壁纸」, 桌面幻灯片还在播它, 清了会直接变纯色。
        """
        want = []
        for x in (ids or []):
            try:
                want.append(int(x))
            except (TypeError, ValueError):
                continue
        aids = [str(x) for x in (artwork_ids or []) if str(x).strip()]
        if not want and not aids:
            return {"count": 0, "ids": []}
        ors, args = [], []
        if want:
            ors.append(f"id IN ({','.join('?' * len(want))})")
            args += want
        if aids:
            ors.append(f"artwork_id IN ({','.join('?' * len(aids))})")
            args += aids
        # 先选出待标记的行 —— UPDATE 之后再查会混进「以前就用同一个 reason 标记过的」
        rows = [int(r[0]) for r in self.conn.execute(
            f"SELECT id FROM wallpapers WHERE trashed_at IS NULL "
            f"AND ({' OR '.join(ors)})", args).fetchall()]
        if not rows:
            return {"count": 0, "ids": []}
        with self.conn:
            self.conn.execute(
                f"UPDATE wallpapers SET trashed_at=datetime('now'), trashed_reason=? "
                f"WHERE id IN ({','.join('?' * len(rows))})", (reason, *rows))
        return {"count": len(rows), "ids": rows}

    def restore_wallpapers(self, ids: "list[int] | None" = None,
                           artwork_ids: "list[str] | None" = None) -> dict:
        """撤销壁纸的标记 (与 soft_delete_wallpapers 对称)。

        原图**恢复**时必须调这个: 否则连带标记的壁纸会永久从列表里消失 ——
        原图回来了, 它派生出去的壁纸却没有回来的路。
        返回 {"count", "ids"}。
        """
        want = []
        for x in (ids or []):
            try:
                want.append(int(x))
            except (TypeError, ValueError):
                continue
        aids = [str(x) for x in (artwork_ids or []) if str(x).strip()]
        if not want and not aids:
            return {"count": 0, "ids": []}
        ors, args = [], []
        if want:
            ors.append(f"id IN ({','.join('?' * len(want))})")
            args += want
        if aids:
            ors.append(f"artwork_id IN ({','.join('?' * len(aids))})")
            args += aids
        rows = [int(r[0]) for r in self.conn.execute(
            f"SELECT id FROM wallpapers WHERE trashed_at IS NOT NULL "
            f"AND ({' OR '.join(ors)})", args).fetchall()]
        if not rows:
            return {"count": 0, "ids": []}
        with self.conn:
            self.conn.execute(
                f"UPDATE wallpapers SET trashed_at=NULL, trashed_reason=NULL "
                f"WHERE id IN ({','.join('?' * len(rows))})", rows)
        return {"count": len(rows), "ids": rows}

    def purge_wallpapers(self, ids: "list[int]") -> dict:
        """**彻底删除**壁纸: 卸载记录, 返回 {"ids", "file_names", "deleted"}。

        file_names 必须在删 DB **之前**取 —— 删完就查不到该清哪个文件了
        (purge_trashed_artworks 为这个顺序踩过一次坑)。
        与那边不同的是**这里不筛 trashed_at**: 壁纸文件一直躺在壁纸夹里, 不是
        「已在回收站」, 所以任何被删掉的行都得把文件名交出来, 否则就是孤儿文件。
        """
        want = []
        for x in (ids or []):
            try:
                want.append(int(x))
            except (TypeError, ValueError):
                continue
        if not want:
            return {"ids": [], "file_names": [], "deleted": 0}
        ph = ','.join('?' * len(want))
        rows = self.conn.execute(
            f"SELECT file_name FROM wallpapers WHERE id IN ({ph})", want).fetchall()
        names = [str(r[0]) for r in rows if r[0]]
        with self.conn:
            cur = self.conn.execute(f"DELETE FROM wallpapers WHERE id IN ({ph})", want)
        return {"ids": want, "file_names": names, "deleted": int(cur.rowcount or 0)}

    def merge_pages(self, items: "list[dict]", target_id: "str | None" = None,
                    title: "str | None" = None,
                    img_root: "Path | None" = None) -> dict:
        """批量把选中的图片页合并为同一个作品（图片视图的批量操作）。

        items: [{"artwork_id": "123", "page_num": 0}, ...]（至少 2 张）。
        - target_id 给定 → 并入已有作品，页码追加到该作品现有页末尾；
        - target_id 为空 → 新建「合并作品」（ID=merged_<uuid>，非 pixiv 数字命名，
          避免文件名自然键被对账误认回源作品），页码从 0 重排。
        文件同步移入目标作品目录（**保留原文件名**，同名才加 (2)），保持 md5 指纹不变；
        被搬空的源作品自动级联删除。img_root 给定则连目标目录的磁盘现状一起算名字冲突。
        返回 {"target_id", "pages_moved", "artworks_removed",
         "rel_moves": [(旧相对路径, 新相对路径), ...]}，文件移动由调用方执行。
        """
        items = [it for it in (items or []) if it]
        if len(items) < 2:
            raise ValueError("至少选择 2 张图片才能合并")
        src_rows = []
        for it in items:
            aid = str(it.get("artwork_id") or "").strip()
            if not aid:
                continue
            try:
                pn = int(it.get("page_num") or 0)
            except (TypeError, ValueError):
                continue
            r = self.conn.execute(
                "SELECT page_num, status, local_path, md5, width, height, file_size_bytes "
                "FROM pages WHERE artwork_id=? AND page_num=?", (aid, pn)).fetchone()
            if r:
                src_rows.append({"aid": aid, "pn": pn, "status": r["status"],
                                 "local_path": r["local_path"], "md5": r["md5"],
                                 "width": r["width"], "height": r["height"],
                                 "file_size_bytes": r["file_size_bytes"]})
        if len(src_rows) < 2:
            raise ValueError("选中的页中至少 2 张存在于图库")
        src_aids = list(dict.fromkeys(r["aid"] for r in src_rows))

        # 保护 (2026-09-16, 与 split_pages **同一条规则**): pixiv 作品的
        # **原网页页**(origin='web')不允许被搬走 —— 会让原作品缺页, 且无法自动归属回
        # 原作品; 而**合并附加页**(origin='merged', 用户自己并进来的)可以再搬走,
        # 这正是「反悔合并」。其他来源/未知来源的作品不受限, 可自由合并。
        # 注: 判断必须落到"页"而不是"作品" —— 否则会把 pixiv 作品的附加页也一并锁死,
        # 与拆分规则不对称 (拆得走却合不走), 反而制造混乱。
        blocked = []
        for r in src_rows:
            if not str(r["aid"]).strip().isdigit():
                continue
            pg = self.conn.execute(
                "SELECT origin FROM pages WHERE artwork_id=? AND page_num=?",
                (r["aid"], r["pn"])).fetchone()
            if pg and (pg["origin"] or "web") != "merged":
                blocked.append(f"{r['aid']}_p{r['pn']}")
        if blocked:
            _shown = "、".join(blocked[:3]) + ("…" if len(blocked) > 3 else "")
            raise ValueError(
                f"选中的图片里有 pixiv 作品的原网页页（{_shown}），不能合并到别处："
                "这会让原作品缺页。合并进来的附加页可以搬（那是「反悔合并」）")

        is_new = False
        if target_id:
            tid = str(target_id).strip()
            if not self.conn.execute("SELECT 1 FROM artworks WHERE artwork_id=?",
                                     (tid,)).fetchone():
                raise ValueError(f"目标作品 {tid} 不存在")
        else:
            import uuid as _uuid
            tid = "merged_" + _uuid.uuid4().hex[:12]
            is_new = True

        # 目标目录:
        #  - 并入已有作品 → 用该作品首页所在目录;
        #  - 新建合并作品 → 用**首张来源图所在目录**(新作品继承首张来源的作者, 与其同夹最自然):
        #    已有作者的作品合并后直接落回其作者夹, 包括作者夹被人工分类到 `X图/`、`涩图/`
        #    等子目录下的情况, 不必事后再点「一键整理」把 merged_xxx 夹搬回作者夹。
        # 两处都定位不到 (无本地文件 / 文件就在 img 根目录) → 兜底新建「merged_<id>」夹。
        def _dir_of(rel) -> "str | None":
            """相对路径的所在目录; 无目录(就在根)或空 → None。
            绝不返回 "" —— 拼 `{dir}/{name}` 时 "" 会产生前导斜杠, Path 拼接会跳到盘根。"""
            rel = str(rel or "").replace("\\", "/").strip("/")
            return rel.rsplit("/", 1)[0] if "/" in rel else None

        target_dir = None
        if not is_new:
            r = self.conn.execute(
                "SELECT local_path FROM pages WHERE artwork_id=? "
                "AND local_path IS NOT NULL AND local_path != '' ORDER BY page_num LIMIT 1",
                (tid,)).fetchone()
            if r and r[0]:
                target_dir = _dir_of(r[0])
        else:
            for r in src_rows:
                target_dir = _dir_of(r["local_path"])
                if target_dir:
                    break
        if not target_dir:
            target_dir = tid

        rel_moves = []
        with self.conn:
            if is_new:
                # 新作品建档, 元数据默认取第一张所属作品的; 标题默认「合并作品 · 首张标题」,
                # 别只写「合并作品」也别留成作品ID (那是内部主键, 不是人读名字)。
                first = self.conn.execute(
                    "SELECT title, illust_type, is_r18, source, author_id, deleted_at "
                    "FROM artworks WHERE artwork_id=?", (src_rows[0]["aid"],)).fetchone()
                _ft = str((first["title"] if first else "") or "").strip()
                if (_ft == str(src_rows[0]["aid"])
                        or (first and first["deleted_at"])   # 源作品已删除 → 标题不可信
                        or not _ft):
                    _ft = ""
                self.conn.execute(
                    "INSERT OR IGNORE INTO artworks "
                    "(artwork_id, total_pages, status, title, illust_type, is_r18, "
                    "source, author_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                    (tid, len(src_rows), WSt.DONE,
                     (title or (f"合并作品 · {_ft}" if _ft else "合并作品")),
                     (first["illust_type"] if first else 0),
                     (first["is_r18"] if first else 0),
                     (first["source"] if first else None),
                     (first["author_id"] if first else None)))
            # 先删源页 (目标若是源之一, 删后再追加可避免主键冲突), 再算目标末尾页码
            for r in src_rows:
                self.conn.execute("DELETE FROM pages WHERE artwork_id=? AND page_num=?",
                                  (r["aid"], r["pn"]))
            mx = self.conn.execute(
                "SELECT COALESCE(MAX(page_num), -1) FROM pages WHERE artwork_id=?",
                (tid,)).fetchone()[0]
            # 图片名归用户/来源, 系统只用 DB 管 ID (2026-09-13 定案):
            # 合并**只把图挪到目标目录, 保留原文件名** —— 不再改写成 {merged_xxx}_p{n},
            # 否则「miku with flower.png」这类可读名字会被冲成谁也认不出的内部主键。
            # 同名才加 (2)(3) 防覆盖; 名字冲突判定同时看 DB 现有页 + 目标目录磁盘现状。
            used_names: "set[str]" = set()
            for er in self.conn.execute(
                    "SELECT local_path FROM pages WHERE artwork_id=? "
                    "AND local_path IS NOT NULL AND local_path != ''", (tid,)).fetchall():
                if _dir_of(er[0]) == target_dir:
                    used_names.add(Path(str(er[0]).replace("\\", "/")).name.lower())
            # 源页此刻已从 DB 删除, 但**磁盘文件还没动** (移动由调用方在提交后执行)。
            # 这些文件是本次要重新安放的, 不是"别人的占用" —— 不能当冲突, 否则
            # 「把作品并入它自己」时每张图都会被加 (2) (2026-09-16 实测: 88007979
            # 手动合并后 p0/p1 全变 "xxx (2).png")。
            own_on_disk: "set[str]" = set()
            for r in src_rows:
                if r["local_path"]:
                    o = str(r["local_path"]).replace("\\", "/").strip("/")
                    if _dir_of(o) == target_dir:
                        own_on_disk.add(Path(o).name.lower())
            if img_root is not None:
                try:
                    tdir = Path(img_root) / target_dir
                    for f in tdir.iterdir():
                        if f.is_file() and f.name.lower() not in own_on_disk:
                            used_names.add(f.name.lower())
                except OSError:
                    pass
            for i, r in enumerate(src_rows):
                npn = mx + 1 + i
                new_rel = None
                if r["local_path"]:
                    old_rel = str(r["local_path"]).replace("\\", "/")
                    name = Path(old_rel).name
                    if name.lower() in used_names:
                        stem, suf = Path(name).stem, Path(name).suffix
                        k = 2
                        while f"{stem} ({k}){suf}".lower() in used_names:
                            k += 1
                        name = f"{stem} ({k}){suf}"
                    used_names.add(name.lower())
                    new_rel = f"{target_dir}/{name}"
                    # 原地不动(已在目标目录且同名) → 不需要移动
                    if new_rel != old_rel:
                        rel_moves.append((old_rel, new_rel))
                self.conn.execute(
                    "INSERT INTO pages (artwork_id, page_num, status, local_path, md5, "
                    "width, height, file_size_bytes, origin) VALUES (?,?,?,?,?,?,?,?, 'merged')",
                    (tid, npn, r["status"] if r["status"] is not None else PSt.DONE,
                     new_rel, r["md5"], r["width"], r["height"], r["file_size_bytes"]))
            if not is_new:
                cnt = self.conn.execute(
                    "SELECT COUNT(*) FROM pages WHERE artwork_id=?", (tid,)).fetchone()[0]
                # 关键: COALESCE 保留已有 total_pages (pixiv 原网页页数), 绝不覆盖成 ▓本地加了
                # 附加页后的实际页数 —— 否则「原 N 页」元数据永久丢失, OVERWRITE 整作品重下
                # 判断与统计口径全乱 (2026-09-10 审计隐患修复)。仅当原本为空(NULL)时才回填。
                self.conn.execute(
                    "UPDATE artworks SET total_pages = COALESCE(total_pages, ?), "
                    "updated_at=datetime('now') WHERE artwork_id=?", (cnt, tid))
        # 被搬空的源作品(排除目标自身) → 级联删除
        emptied = [aid for aid in src_aids if aid != tid and self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE artwork_id=?", (aid,)).fetchone()[0] == 0]
        if emptied:
            self.delete_artworks(emptied)
        # 还有剩余页的源作品 → 重算状态 (2026-09-16 修): 旧实现漏了这步, 源作品被
        # 拿走页后状态仍是 DONE, 界面显示「✓ 已入库」却其实缺页, 也点不到「补下缺失页」。
        for a in src_aids:
            if a != tid and a not in emptied:
                self._refresh_work_progress(a)
        return {"target_id": tid, "pages_moved": len(src_rows),
                "artworks_removed": emptied, "rel_moves": rel_moves}

    def claim_pixiv_work(self, old_id: str, pixiv_id: str, *, info,
                         img_root: "Path | None" = None) -> dict:
        """把一个本地占位作品 (merged_xxx / ext_xxx) **认领转正**为真正的 pixiv 作品。

        场景 : 两张图被拆成 merged_ 作品, 反搜/文件名都
        指向 pixiv #88007979, 但「确认来源」只写 source_url, 作品 ID/标题/作者/页数都
        没变。用户要的是"合并回来": 占位作品直接变成 #pixiv_id 本体。

        身份依据: 人工在界面点「认领转正」+ 文件名自然键 ({pid}_pN) 双重确认;
        info 是当场从 pixiv ajax 拉的权威元数据 (标题/作者/页数/标签/R18)。

        - 每个本地页文件名必须自带 **同一个** pixiv_id, 页码按文件名 pN 重排
          (修得回合并时页码挂反/" (2)" 改名一类脏状态);
        - 文件规范重命名为 {pixiv_id}_pN.<原后缀>, 保留 md5/尺寸/大小;
        - 页 origin 置 'web' (人工确认的原网页页, 之后禁止再合并/拆分);
        - 目标 #pid 在库已存在: 占位页可补位; 同学号页已 DONE → 字节相同删重,
          不同则拒绝 (绝不覆盖);
        - 旧作品记录删除 (其页已全部转走), 孤儿作者/标签顺手清理。
        文件移动/删重由调用方执行 (与 merge_pages 同一模式)。
        返回 {target_id, title, author_id, author_name, pages, rel_moves, drop_files,
              artworks_removed}。"""
        old_id = str(old_id or "").strip()
        pid = str(pixiv_id or "").strip()
        if not old_id:
            raise ValueError("缺少本地作品 ID")
        if not pid.isdigit():
            raise ValueError("pixiv 作品 ID 必须是纯数字")
        if old_id == pid:
            raise ValueError("该作品已经是正式 pixiv 作品，无需认领")
        wrow = self.conn.execute(
            "SELECT 1 FROM artworks WHERE artwork_id=?", (old_id,)).fetchone()
        if not wrow:
            raise ValueError(f"本地作品 {old_id} 不存在")
        rows = self.conn.execute(
            "SELECT page_num, status, local_path, md5, width, height, file_size_bytes "
            "FROM pages WHERE artwork_id=? ORDER BY page_num", (old_id,)).fetchall()
        if not rows:
            raise ValueError("该作品下没有本地页，无法认领")

        def _dir_of(rel) -> "str | None":
            rel = str(rel or "").replace("\\", "/").strip("/")
            return rel.rsplit("/", 1)[0] if "/" in rel else None

        # ---- ① 文件名身份核对: 全部页必须声明同一个 pid ----
        bad_names, seen_new_pages, plan = [], {}, []
        target_dir = None
        for r in rows:
            old_rel = str(r["local_path"] or "").replace("\\", "/")
            if not old_rel:
                bad_names.append(f"(第 {r['page_num']} 页无文件路径)")
                continue
            nkey = pixiv_natural_key(Path(old_rel).name)
            if not nkey or nkey[0] != pid:
                bad_names.append(Path(old_rel).name)
                continue
            new_pn = int(nkey[1])
            if new_pn >= int(getattr(info, "page_count", 0) or 0):
                raise ValueError(
                    f"文件 {Path(old_rel).name} 声称 p{new_pn}，但 pixiv 网页显示该作品"
                    f"只有 {info.page_count} 页，身份对不上，已中止")
            if new_pn in seen_new_pages:
                raise ValueError(
                    f"有两个文件都声称是 p{new_pn}（{seen_new_pages[new_pn]} 与 "
                    f"{Path(old_rel).name}），请先人工核对页码")
            d = _dir_of(old_rel)
            if target_dir is None:
                target_dir = d
            elif d != target_dir:
                raise ValueError("各页不在同一目录，无法直接认领，请先用一键整理归拢")
            seen_new_pages[new_pn] = Path(old_rel).name
            plan.append({"old_pn": r["page_num"], "new_pn": new_pn,
                         "old_rel": old_rel, "status": r["status"],
                         "md5": r["md5"], "width": r["width"], "height": r["height"],
                         "size": r["file_size_bytes"]})
        if bad_names:
            raise ValueError(
                "以下文件名里找不到与 #%s 一致的作品 ID，不能认领：%s"
                % (pid, "、".join(bad_names[:5])))
        target_dir = target_dir or pid

        # ---- ② 与在库目标作品的同学号页对账 (字节相同删重, 不同拒绝) ----
        old_rels_set = {p["old_rel"].lower() for p in plan}
        for p in plan:
            suffix = Path(p["old_rel"]).suffix.lower() or ".png"
            p["new_rel"] = f"{target_dir}/{pid}_p{p['new_pn']}{suffix}"
            er = self.conn.execute(
                "SELECT status, local_path, md5 FROM pages "
                "WHERE artwork_id=? AND page_num=?", (pid, p["new_pn"])).fetchone()
            if er and er["status"] == PSt.DONE:
                same_file = str(er["local_path"] or "").replace("\\", "/").lower() \
                    == p["new_rel"].lower()
                same_md5 = bool(er["md5"]) and er["md5"] == p["md5"]
                if same_file:
                    p["drop_file"] = True      # 目标已登记同一文件 → 只删旧记录
                elif same_md5:
                    p["drop_file"] = True      # 字节一致的重复页 → 删旧文件, 留目标
                else:
                    raise ValueError(
                        f"作品 #{pid} 已在库且 p{p['new_pn']} 是另一张图"
                        f"（{er['local_path']}），不能覆盖；如确需替换请先在图库删除该页")
            # 磁盘上有未登记的同名文件: 同大小判重删, 不同拒绝防覆盖
            if img_root is not None and not p.get("drop_file"):
                dp = Path(img_root) / p["new_rel"]
                if dp.is_file() and p["new_rel"].lower() not in old_rels_set:
                    try:
                        same_sz = p["size"] and dp.stat().st_size == p["size"]
                    except OSError:
                        same_sz = False
                    if same_sz:
                        p["drop_file"] = True
                    else:
                        raise ValueError(
                            f"目标文件已存在且大小不同: {p['new_rel']}，已中止请人工核对")

        rel_moves, drop_files, page_rows = [], [], []
        for p in plan:
            if p.get("drop_file"):
                if p["old_rel"].lower() != p["new_rel"].lower():
                    drop_files.append(p["old_rel"])
                continue
            if p["old_rel"] != p["new_rel"]:
                rel_moves.append((p["old_rel"], p["new_rel"]))
            page_rows.append(p)

        with self.conn:
            # 权威元数据建档 (含作者建库; work_status 先 NOT_YET, 末尾 sync 收敛)
            self.upsert_artwork_metadata(
                pid,
                title=getattr(info, "title", None),
                author_id=getattr(info, "author_id", None),
                author_name=getattr(info, "author_name", None),
                illust_type=getattr(getattr(info, "illust_type", None), "value", None),
                tags=list(getattr(info, "tags", []) or []),
                publish_date=getattr(info, "publish_date", None),
                is_r18=getattr(info, "is_r18", None),
                thumb_url=(getattr(info, "image_urls", None) or [None])[0],
                page_count=getattr(info, "page_count", None),
                work_status=WSt.NOT_YET)
            self.conn.execute(
                "UPDATE artworks SET source='pixiv', source_url=?, "
                "source_author=NULL, updated_at=datetime('now') WHERE artwork_id=?",
                (f"https://www.pixiv.net/artworks/{pid}", pid))
            # 旧页全部移出 (含 drop_file 的: 目标页已存在, 旧记录作废)
            self.conn.execute("DELETE FROM pages WHERE artwork_id=?", (old_id,))
            # 目标同学号若是占位/失败页 → 用认领页替换
            if page_rows:
                self.conn.executemany(
                    "DELETE FROM pages WHERE artwork_id=? AND page_num=?",
                    [(pid, p["new_pn"]) for p in page_rows])
                for p in page_rows:
                    self.conn.execute(
                        "INSERT INTO pages (artwork_id, page_num, status, local_path, "
                        "md5, width, height, file_size_bytes, origin) "
                        "VALUES (?,?,?,?,?,?,?,?, 'web')",
                        (pid, p["new_pn"],
                         p["status"] if p["status"] is not None else PSt.DONE,
                         p["new_rel"], p["md5"], p["width"], p["height"], p["size"]))
        # 旧作品搬空 → 删除 (顺手清孤儿作者/标签; 作者已被新作品引用, 不会误删)
        self.delete_artworks([old_id])
        self.sync_work_and_pages(pid)
        return {
            "target_id": pid,
            "title": getattr(info, "title", None),
            "author_id": getattr(info, "author_id", None),
            "author_name": getattr(info, "author_name", None),
            "pages": [{"page_num": p["new_pn"], "local_path": p["new_rel"]}
                      for p in page_rows],
            "rel_moves": rel_moves,
            "drop_files": drop_files,
            "artworks_removed": [old_id],
        }

    def scan_claimable_pixiv_works(self) -> dict:
        """扫描**散落的 pixiv 页**: 所有非 pixiv 正式 ID 的本地作品 (merged_/ext_/其他),
        按页文件名 `{pid}_pN` 自然键判断它们本该属于哪个 pixiv 作品。

        纯离线、不改任何东西, 供「🪄 认领散落页」一键预览:
          - ready: 该作品全部有文件名的页都指向**同一个** pid → 可一键认领转正;
          - mixed: 页指向两个及以上不同 pid (历史上多次错误合并) → 不能自动转,
            需用户先拆分再分别认领;
          - no_id: 文件名里没有 pixiv 自然键 → 真外来源/手工命名, 不动。
        页没有 local_path 的不计入 (无身份依据)。"""
        ready, mixed, no_id = [], [], 0
        rows = self.conn.execute(
            """SELECT a.artwork_id AS aid, COALESCE(a.title,'') AS title,
                      p.page_num AS pn, p.local_path AS lp
                 FROM artworks a JOIN pages p ON p.artwork_id = a.artwork_id
                WHERE p.local_path IS NOT NULL AND p.local_path != ''
                ORDER BY a.artwork_id, p.page_num""").fetchall()
        grouped: "dict[str, dict]" = {}
        for r in rows:
            aid = str(r["aid"])
            if aid.isdigit():
                continue   # 已是 pixiv 正式作品
            g = grouped.setdefault(aid, {"title": r["title"], "pages": [], "pids": {}})
            nkey = pixiv_natural_key(Path(str(r["lp"]).replace("\\", "/")).name)
            if not nkey:
                g["pages"].append({"page_num": r["pn"], "file": r["lp"],
                                   "pid": None, "declared_page": None})
                continue
            pid, pg = str(nkey[0]), int(nkey[1])
            g["pids"].setdefault(pid, []).append(r["pn"])
            g["pages"].append({"page_num": r["pn"], "file": r["lp"],
                               "pid": pid, "declared_page": pg})
        for aid, g in grouped.items():
            pids = sorted(g["pids"].keys())
            if not pids:
                no_id += 1
                continue
            entry = {"old_id": aid, "title": g["title"], "pids": pids,
                     "page_count": len(g["pages"]),
                     "pages": [{"page_num": p["page_num"], "file": p["file"],
                                "pid": p["pid"], "declared_page": p["declared_page"]}
                               for p in g["pages"]]}
            if len(pids) == 1:
                entry["pixiv_id"] = pids[0]
                ready.append(entry)
            else:
                mixed.append(entry)
        ready.sort(key=lambda e: e["old_id"])
        mixed.sort(key=lambda e: e["old_id"])
        return {"ready": ready, "mixed": mixed, "no_id": no_id}

    def split_pages(self, items: "list[dict]", img_root: "Path | None" = None) -> dict:
        """把选中的图片页从各自作品里**拆出**, 每张独立成一个新作品。

        与 merge_pages 相反的方向。
        注意: 合并时源页记录已被搬走, **原归属无法还原**, 所以拆出只能新建作品,
        不会回到合并前的那个作品。

        items: [{"artwork_id", "page_num"}, ...]（至少 1 张, 单张也能拆）。
        - 新作品 ID = merged_<uuid>（非 pixiv 命名, 不会被对账误认回源作品）;
        - 标题取原文件名(去扩展名); **继承源作品的作者/来源/来源作者/R18 标记**
          ;
        - **文件不移动**(新作品目录 = 原文件所在目录), 只改数据库归属;
        - pixiv 作品的**原网页页**(origin='web')不允许被拆走 —— 会让原作品缺页;
          合并附加页(origin='merged')可以拆, 那正是「反悔合并」的正常用法;
        - 原作品被搬空 → 级联删除; 还有剩余页 → 重算 exist_list/状态, 但
          **保留 total_pages**(原网页页数声明不能因拆页而丢失, 否则「原 N 页」永久失真)。
        返回 {"created": [...], "pages_moved": n, "artworks_removed": [...]}。"""
        items = [it for it in (items or []) if it]
        if not items:
            raise ValueError("请先选择要拆分的图片")

        # 保护 (与合并闸门对称, 2026-09-16): pixiv 作品的**原网页页**(origin='web')
        # 不允许被拆走 —— 那会让原作品缺页; 而合并附加页(origin='merged')本就是用户
        # 自己并进去的, 拆出来正是「反悔合并」的正常用法, 放行。
        # 其他来源/未知来源的作品不受此限, 可自由拆分。
        blocked = []
        for it in items:
            _aid = str(it.get("artwork_id") or "").strip()
            if not _aid or not _aid.isdigit():
                continue
            try:
                _pn = int(it.get("page_num") or 0)
            except (TypeError, ValueError):
                continue
            _r = self.conn.execute(
                "SELECT origin FROM pages WHERE artwork_id=? AND page_num=?",
                (_aid, _pn)).fetchone()
            if _r and (_r["origin"] or "web") != "merged":
                blocked.append(f"{_aid}_p{_pn}")
        if blocked:
            _shown = "、".join(blocked[:3]) + ("…" if len(blocked) > 3 else "")
            raise ValueError(
                f"选中的图片里有 pixiv 作品的原网页页（{_shown}），不能拆出："
                "这会让原作品缺页。合并进来的附加页可以拆（那是「反悔合并」）")

        created, src_aids = [], []
        with self.conn:
            for it in items:
                aid = str(it.get("artwork_id") or "").strip()
                if not aid:
                    continue
                try:
                    pn = int(it.get("page_num") or 0)
                except (TypeError, ValueError):
                    continue
                r = self.conn.execute(
                    "SELECT status, local_path, md5, width, height, file_size_bytes "
                    "FROM pages WHERE artwork_id=? AND page_num=?", (aid, pn)).fetchone()
                if not r:
                    continue
                # 继承源作品的身份信息 
                meta = self.conn.execute(
                    "SELECT author_id, source, source_author, is_r18 FROM artworks "
                    "WHERE artwork_id=?", (aid,)).fetchone()
                # 1) 从原作品摘掉这一页
                self.conn.execute("DELETE FROM pages WHERE artwork_id=? AND page_num=?",
                                  (aid, pn))
                src_aids.append(aid)
                # 2) 建独立单页作品; 文件留在原地, 只改归属
                # 身份前缀按**真实来源**给 
                # 外部来源, 却一直背着 merged_ 合并产物前缀): 外来源 = ext_ 稳定键; 其余
                # (pixiv 页/未确认) = merged_ 合并产物, 等「认领散落页」转正。
                import hashlib as _hl
                import uuid as _uuid
                old_rel = str(r["local_path"]).replace("\\", "/") if r["local_path"] else None
                _src = str((meta["source"] if meta else "") or "").strip()
                _sau = str((meta["source_author"] if meta else "") or "").strip()
                _is_ext = bool(_src) and _src != "pixiv"
                if _is_ext:
                    nid = "ext_" + _hl.md5(
                        f"ext|split|{old_rel or (aid + '|' + str(pn))}".encode("utf-8")
                    ).hexdigest()[:16]
                    if self.conn.execute("SELECT 1 FROM artworks WHERE artwork_id=?",
                                         (nid,)).fetchone():
                        nid = "ext_" + _uuid.uuid4().hex[:16]
                else:
                    nid = "merged_" + _uuid.uuid4().hex[:12]
                title = ""
                if old_rel:
                    _st = Path(old_rel).stem
                    if stem_is_readable(_st) and not title_is_internal_key(_st):
                        title = _st
                if not title and _is_ext:
                    title = foreign_title_fallback(_src, _sau, old_rel or "")
                title = title or f"拆出 · {aid}_p{pn}"
                pst = r["status"] if r["status"] is not None else PSt.DONE
                self.conn.execute(
                    "INSERT INTO artworks (artwork_id, title, status, total_pages, "
                    "exist_list, author_id, source, source_author, is_r18, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, '[0]', ?, ?, ?, ?, datetime('now'), datetime('now'))",
                    (nid, title, WSt.DONE if pst == PSt.DONE else WSt.NOT_YET,
                     (meta["author_id"] if meta else None),
                     (meta["source"] if meta else None),
                     (meta["source_author"] if meta else None),
                     (meta["is_r18"] if meta else 0)))
                self.conn.execute(
                    "INSERT INTO pages (artwork_id, page_num, status, local_path, md5, "
                    "width, height, file_size_bytes, origin) "
                    "VALUES (?, 0, ?, ?, ?, ?, ?, ?, 'merged')",
                    (nid, pst, old_rel, r["md5"], r["width"], r["height"],
                     r["file_size_bytes"]))
                created.append(nid)

        src_aids = list(dict.fromkeys(src_aids))
        # 3) 原作品: 搬空 → 删; 否则保留 total_pages, 只重算 exist_list 与状态
        for a in src_aids:
            self._refresh_work_progress(a)
        emptied = [a for a in src_aids if self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE artwork_id=?", (a,)).fetchone()[0] == 0]
        if emptied:
            self.delete_artworks(emptied)
        return {"created": created, "pages_moved": len(created),
                "artworks_removed": emptied, "rel_moves": []}

    def reorder_pages(self, aid: str, new_order: "list | None" = None,
                      img_root: "Path | None" = None,
                      assign: "list | None" = None) -> dict:
        """调整一个作品内部的**页序** : 手动认领/自动溯源可能把
        页挂反, 用户要能事后调。两种玩法 (二选一):

        ① new_order (排列): 现有页码的完整排列, 如 [2,0,1] = 旧 p2/p0/p1 依次成为
           新 p0/p1/p2 → 页号被**压实成 0..n-1**。要求页号原本就连续 (没洞)。
        ② assign (点对点改号): [{"old": 旧页码, "new": 新页码}, ...], 只动被点名的页,
           **未提及的页连同页码空洞原样保留**。两条必需的能力都靠它 :
             - 有空洞的作品 (删过页/认领后留洞) 也该能"移动一页" —— 用
               [{old:A,new:B},{old:B,new:A}] 表达"交换两页", 洞不被抹掉;
             - 把「附加页」(origin=merged, 用户自己并进来、页号超出原网页的那几张)
               **填进空位**: [{old:5,new:2}] = 让原 p5 变成 p2。
        语义与护栏 (两种玩法共用):
          - 只改行的 page_num(以及规范命名文件的文件名), md5/origin/尺寸等**跟着文件走**,
            原网页页与附加页的身份不丢; total_pages(原网页页数)绝不动;
          - 目标页号不许重复、不许为负; 未提及的页保持原号;
          - 文件名规范 (`{aid}_pN{ext}`) 且同目录、磁盘无外人占用 → 连文件一起两阶段
            重命名(先全部改成临时名再落最终名, 规避交换时的同名冲突); 否则**只调库内顺序**,
            用户自己的命名一个字符都不碰 (与 merge_pages 同一条原则)。
        rel_moves 已按「先全部→临时名, 再全部→最终名」排好序, 由调用方顺序执行。
        返回 {artwork_id, n, mapping:[{old,new}], rel_moves, files_renamed}。"""
        aid = str(aid or "").strip()
        if not self.conn.execute("SELECT 1 FROM artworks WHERE artwork_id=?",
                                 (aid,)).fetchone():
            raise ValueError(f"作品 {aid} 不存在")
        rows = self.conn.execute(
            "SELECT page_num, status, local_path FROM pages "
            "WHERE artwork_id=? ORDER BY page_num", (aid,)).fetchall()
        if len(rows) < 2:
            raise ValueError("至少要有 2 页才能调整页序")
        old_pns = [int(r["page_num"]) for r in rows]
        bad = [r["page_num"] for r in rows if r["status"] != PSt.DONE
               or not r["local_path"]]
        if bad:
            raise ValueError(f"第 {'、'.join('p'+str(x) for x in bad[:5])} 页还没有本地图片，"
                             "不能参与排序")
        if assign:
            new_of = {pn: pn for pn in old_pns}
            try:
                pairs = [(int(x["old"]), int(x["new"])) for x in assign]
            except (TypeError, ValueError, KeyError, IndexError):
                raise ValueError('assign 必须是 [{"old": 旧页码, "new": 新页码}] 数组')
            for o, n in pairs:
                if o not in new_of:
                    raise ValueError(f"作品里没有第 p{o} 页")
                if n < 0:
                    raise ValueError("页码不能为负数")
                new_of[o] = n
            if len(set(new_of.values())) != len(new_of):
                raise ValueError("目标页码有重复：两页不能占用同一个页号")
        else:
            if set(old_pns) != set(range(len(rows))):
                raise ValueError("该作品页号有空缺（有未下载/已删除的洞）；"
                                 "可直接用「填入空位」把附加页补进洞里，"
                                 "或先补齐缺失页再整体调序")
            try:
                order = [int(x) for x in (new_order or [])]
            except (TypeError, ValueError):
                raise ValueError("页序参数必须是页码数组")
            if len(order) != len(rows) or set(order) != set(old_pns) \
                    or len(set(order)) != len(order):
                raise ValueError("页序必须是现有全部页码的一个排列（不能漏页、不能重复）")
            new_of = {old_pn: np_ for np_, old_pn in enumerate(order)}
        row_by_old = {int(r["page_num"]): r for r in rows}

        # ---- 规划文件改名 ----
        def _dir_of(rel) -> "str | None":
            rel = str(rel or "").replace("\\", "/").strip("/")
            return rel.rsplit("/", 1)[0] if "/" in rel else None

        plans = []   # (old_pn, new_pn, old_rel, new_rel|None)
        dirs: "set[str]" = set()
        canonical = True
        for old_pn in old_pns:
            new_pn = new_of[old_pn]
            r = row_by_old[old_pn]
            old_rel = str(r["local_path"]).replace("\\", "/")
            nkey = pixiv_natural_key(Path(old_rel).name)
            d = _dir_of(old_rel)
            if d:
                dirs.add(d)
            if not nkey or nkey != (aid, old_pn) or not Path(old_rel).suffix:
                canonical = False
            plans.append((old_pn, new_pn, old_rel, d))
        rename = canonical and len(dirs) == 1
        # 磁盘上任何一个"最终名"已被非本次移动文件占用 → 不改名 (防覆盖外人文件)
        if rename and img_root is not None:
            old_rels_lower = {
                str(row_by_old[op]["local_path"]).replace("\\", "/").lower()
                for op, np_, _, _ in plans if op != np_}
            for old_pn, new_pn, _, d in plans:
                if old_pn == new_pn:
                    continue
                suf = Path(str(row_by_old[old_pn]["local_path"])).suffix
                cand = Path(img_root) / d / f"{aid}_p{new_pn}{suf}"
                if cand.is_file() and cand.relative_to(img_root).as_posix().lower() \
                        not in old_rels_lower:
                    rename = False
                    break
        # 源文件缺失 → 只调库, 不动磁盘
        new_of = {op: np_ for op, np_, _, _ in plans}
        if rename and img_root is not None:
            for old_pn in new_of:
                if old_pn == new_of[old_pn]:
                    continue
                if not (Path(img_root) /
                        str(row_by_old[old_pn]["local_path"]).replace("\\", "/")).is_file():
                    rename = False
                    break

        rel_moves: "list[tuple[str, str]]" = []
        finals: "dict[int, str]" = {}
        if rename:
            tmp_pairs, fin_pairs = [], []
            for i, (old_pn, new_pn, old_rel, d) in enumerate(plans):
                if old_pn == new_pn:
                    finals[old_pn] = old_rel
                    continue
                suf = Path(old_rel).suffix
                tmp_rel = f"{d}/.__reorder_{aid}_{i}_{old_pn}_to_{new_pn}{suf}"
                new_rel = f"{d}/{aid}_p{new_pn}{suf}"
                tmp_pairs.append((old_rel, tmp_rel))
                fin_pairs.append((tmp_rel, new_rel))
                finals[old_pn] = new_rel
            # 先全部挪到临时名 (腾出全部最终名), 再统一落位 —— 交换/循环都不会撞名
            rel_moves = tmp_pairs + fin_pairs

        # 按"新位置"排序输出 (与旧实现一致): 日志读起来就是新顺序 p?→p0, p?→p1, ...
        mapping = [{"old": op, "new": new_of[op]}
                   for op in sorted(old_pns, key=lambda o: new_of[o])]
        # 先动磁盘、后写库: 两阶段改名(全部→临时名, 再统一落最终名, 交换/循环都不撞名)。
        # 顺序很关键 —— 以前是"库先提交、磁盘改名放到 HTTP 端点里且异常被吞", 一旦改名失败
        # 就成了「库里说 p0 是 A, 磁盘上 p0 还是 B」的分裂状态, 界面全乱。
        # 现在要么两边都成, 要么一个字节都不动(改名失败 → 倒序回滚已移动的文件 → 抛错)。
        renamed = 0
        done_moves: "list[tuple[str, str]]" = []
        if rename and img_root is not None:
            try:
                for src_rel, dst_rel in rel_moves:
                    s_f = Path(img_root) / src_rel
                    d_f = Path(img_root) / dst_rel
                    if s_f == d_f or not s_f.is_file():
                        continue
                    d_f.parent.mkdir(parents=True, exist_ok=True)
                    s_f.replace(d_f)
                    done_moves.append((src_rel, dst_rel))
            except OSError as e:
                for src_rel, dst_rel in reversed(done_moves):
                    try:
                        (Path(img_root) / dst_rel).replace(Path(img_root) / src_rel)
                    except OSError:
                        pass      # 回滚也失败: 只能保留临时名, 至少不覆盖别人的文件
                raise ValueError(f"同步重命名文件失败（已回滚，库未改动）：{e}") from e
            renamed = sum(1 for _, dst_rel in done_moves if "/.__reorder_" not in dst_rel)
        # 两阶段改号: 先全部挪到"高位临时槽"(必须 > 0, pages.page_num 有 CHECK >= 0),
        # 再落最终槽 —— 交换/循环换序都不会撞主键
        tmp_base = 1000000 + max(old_pns)
        with self.conn:
            for old_pn, new_pn, _, _ in plans:
                self.conn.execute(
                    "UPDATE pages SET page_num=? WHERE artwork_id=? AND page_num=?",
                    (tmp_base + new_pn, aid, old_pn))
            for old_pn, new_pn, _, _ in plans:
                self.conn.execute(
                    "UPDATE pages SET page_num=?, local_path=COALESCE(?, local_path) "
                    "WHERE artwork_id=? AND page_num=?",
                    (new_pn, finals.get(old_pn), aid, tmp_base + new_pn))
        # 状态/exist_list: 行集未变(全 DONE 且连续), 完整性与排序前等价; total_pages 保留。
        # tp 未知(ext_/merged_ 作品)且原本就 DONE 时不许被误降级 —— _refresh_work_progress
        # 的 bool(tp) 判据会把它们打成 NOT_YET, 这里按"原本 DONE 就维持 DONE"兜住。
        info = self.get_artwork_info(aid)
        tp = int(info.total_pages) if info and info.total_pages else 0
        full = bool(tp) and len(rows) >= tp
        if assign:
            # 改号(可能是纯交换, 也可能是把附加页填进洞): 老实按现有行重算完整性/状态 ——
            # 不能沿用下面"行数 >= 原页数就算完整"的捷径, 有洞时那个判据会骗人。
            # 填对洞后 0..tp-1 齐全 → 自动回到 DONE; 只是交换则状态不变 (2026-09-17)。
            self._refresh_work_progress(aid)
        elif full:
            self.change_work_status(aid, WSt.DONE,
                                    exist_list=json.dumps(list(range(len(rows)))))
        elif info and info.status == WSt.DONE and not tp:
            self.change_work_status(aid, WSt.DONE,
                                    exist_list=json.dumps(list(range(len(rows)))))
        else:
            self._refresh_work_progress(aid)
        return {"artwork_id": aid, "n": len(rows), "mapping": mapping,
                "rel_moves": rel_moves, "files_renamed": renamed}

    def _refresh_work_progress(self, aid: str) -> None:
        """页数变动后重算作品的 exist_list 与完成状态, **但保留 total_pages**。

        用于合并/拆分这类「把页从一个作品挪到另一个」的操作: 页被搬走后源作品可能
        实际缺页, 若不同步状态, 界面会拿旧的 DONE 显示「✓ 已入库」骗人, 用户也点不到
        「补下缺失页」。而 total_pages(原网页页数声明) 绝不能因此被清 —— 否则
        「原 N 页」永久失真 (与 sync_work_and_pages 的区别就在这)。"""
        # 回收站里的页不参与进度计算 —— 否则软删掉一张图后, 作品仍显示"已入库/完整"骗人
        # (2026-09-17 回收站上线时同步改)。
        rows = self.conn.execute(
            "SELECT page_num, status FROM pages WHERE artwork_id=? AND trashed_at IS NULL "
            "ORDER BY page_num",
            (aid,)).fetchall()
        if not rows:
            # 页全进了回收站 → 作品事实上有 0 个有效页, 状态改为「缺页」而不是保留旧的 DONE;
            # 但**从未有过页**的作品(仅元数据的壳)保持原状, 不在此处乱改状态。
            if not self.conn.execute(
                    "SELECT 1 FROM pages WHERE artwork_id=? LIMIT 1", (aid,)).fetchone():
                return
            with self.conn:
                self.conn.execute(
                    "UPDATE artworks SET status=?, exist_list=NULL, updated_at=datetime('now') "
                    "WHERE artwork_id=?", (WSt.NOT_YET, aid))
            return
        done = [x["page_num"] for x in rows if x["status"] == PSt.DONE]
        tp_row = self.conn.execute(
            "SELECT total_pages FROM artworks WHERE artwork_id=?", (aid,)).fetchone()
        tp = tp_row[0] if tp_row else None
        # 完整 = 「原网页那 tp 页 (0..tp-1) 全 DONE」。**不能用 len(rows)==tp** ——
        # 作品可以带「附加页」(origin='merged', 用户自己并进来的、超出原网页页数的那几张),
        # 那时页行数 > tp, 旧判据会让一个其实已下全的作品永远停在「缺页」(2026-09-17)。
        full = bool(tp) and all(i in done for i in range(int(tp)))
        if not tp:
            # 连"原网页有几页"都不知道时 (ext_/merged_ 作品, 或元数据还没补到页数),
            # 压根没有"缺页"这回事 → 现有页全 DONE 就算完整。
            # 旧实现这里一律 NOT_YET, 于是**往这类作品里塞一张图会把它从「已入库」悄悄
            # 降级成「缺页」** —— 对账台认领就会触发 (2026-09-17 离线复现确认)。
            full = bool(rows) and len(done) == len(rows)
        with self.conn:
            self.conn.execute(
                "UPDATE artworks SET status=?, exist_list=?, updated_at=datetime('now') "
                "WHERE artwork_id=?",
                (WSt.DONE if full else WSt.NOT_YET,
                 json.dumps(done) if done else None, aid))

    def _cleanup_orphan_authors(self) -> int:
        """删除不再被任何作品引用的孤儿 author 行, 返回删除行数。

        典型场景: 手动批量删除完某作者下全部作品后,
        authors 表残留一行让顶部「作者」统计多出一个人, 造成"明明没文件
        了还显示有 N 个作者"的幻觉。
        """
        before = self.conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        with self.conn:
            self.conn.execute(
                "DELETE FROM authors WHERE author_id NOT IN "
                "(SELECT DISTINCT author_id FROM artworks WHERE author_id IS NOT NULL)"
            )
        after = self.conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        return max(0, before - after)

    def _cleanup_orphan_pages(self) -> int:
        """删除 pages 表中 artwork_id 不在 artworks 里的孤儿行 (常见于测试残留), 返回删除行数。"""
        before = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        with self.conn:
            self.conn.execute(
                "DELETE FROM pages WHERE artwork_id NOT IN "
                "(SELECT artwork_id FROM artworks)"
            )
        after = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        return max(0, before - after)

    def cleanup_orphans(self) -> dict:
        """一次性清理全部孤儿对象 (tags / authors / pages)。

        供图库页「同步/健康检查」完成后调用, 让统计口径立即与磁盘/作品表一致。
        返回 dict: {tags_removed, authors_removed, pages_removed, unsigned_cleared}。
        """
        u = self._cleanup_unsigned_authors()   # 先迁移「未署名」占位作者, 其作者行随后成为孤儿
        t = self._cleanup_orphan_tags()
        a = self._cleanup_orphan_authors()
        p = self._cleanup_orphan_pages()
        return {"tags_removed": t, "authors_removed": a, "pages_removed": p,
                "unsigned_cleared": u}

    def _cleanup_unsigned_authors(self) -> int:
        """迁移旧数据: 早期 backfill_foreign_authors 曾把「无账号外来作品」记成名字为
        「未署名」的 local_ 本地作者并归属。现在这类匿名作品应保持「无作者」
        (界面显示「未知作者」, 状态筛选「未知作者」也能捞到)。
        2026-09-14 扩展: 占位词不止「未署名」—— 夹名里剥出的 unknown/未知作者/未知/未命名
        等一律清掉 (只动 local_ 自动生成的作者行, 绝不碰 pixiv 数字作者)。
        返回解除归属的作品数 (作者行随之删除)。"""
        names = sorted(_PLACEHOLDER_AUTHOR_NAMES)
        ph_names = ",".join("?" * len(names))
        ids = [r[0] for r in self.conn.execute(
            f"SELECT author_id FROM authors WHERE author_id LIKE 'local_%' "
            f"AND LOWER(TRIM(COALESCE(name,''))) IN ({ph_names})", names).fetchall()]
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"UPDATE artworks SET author_id=NULL, updated_at=? WHERE author_id IN ({ph})",
            [now_stamp()] + ids)
        n = cur.rowcount or 0
        self.conn.execute(f"DELETE FROM authors WHERE author_id IN ({ph})", ids)
        self.conn.commit()
        return n

    def set_artwork_tags(self, artwork_id: str, tags: "Iterable[str]") -> None:
        """替换某作品的全部标签 (增/删/改统一入口), 并清孤儿标签。"""
        self._sync_artwork_tags(artwork_id, tags)
        # 同步 tags_json 保持与关联表一致
        now = now_stamp()
        with self.conn:
            self.conn.execute(
                "UPDATE artworks SET tags_json=?, updated_at=? WHERE artwork_id=?",
                (json.dumps(list(tags), ensure_ascii=False), now, artwork_id),
            )
        self._cleanup_orphan_tags()

    def update_artwork(self, artwork_id: str, **fields) -> None:
        """编辑作品元数据: 可改 title/author_id/illust_type/is_r18/publish_date/bookmark_count/thumb_url/tags。
        tags 传 list 时走 set_artwork_tags 同步关联表。"""
        tags = fields.pop('tags', None)
        if tags is not None:
            self.set_artwork_tags(artwork_id, tags)
        if not fields:
            return
        allowed = {'title', 'author_id', 'illust_type', 'is_r18', 'source',
                   'source_url', 'source_author', 'publish_date', 'bookmark_count', 'thumb_url'}
        fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not fields:
            return
        sets = ', '.join(f"{k} = ?" for k in fields)
        params = list(fields.values())
        with self.conn:
            self.conn.execute(
                f"UPDATE artworks SET {sets}, updated_at=? WHERE artwork_id=?",
                (*params, now_stamp(), artwork_id),
            )

    def list_all_tags(self) -> list[dict]:
        """全部标签 + 各标签作品数 (分类浏览/标签下拉用)。"""
        rows = self.conn.execute(
            """
            SELECT tg.tag_name, COUNT(atg.artwork_id) AS count
              FROM tags tg
         LEFT JOIN artwork_tags atg ON atg.tag_id = tg.tag_id
          GROUP BY tg.tag_id
          ORDER BY count DESC, tg.tag_name ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sources(self) -> list[dict]:
        """图库实际用到的全部来源 + 各来源作品数 (筛选下拉/统计用, 含自定义来源)。"""
        rows = self.conn.execute(
            """
            SELECT COALESCE(NULLIF(source, ''), 'pixiv') AS source, COUNT(*) AS count
              FROM artworks
             GROUP BY COALESCE(NULLIF(source, ''), 'pixiv')
             ORDER BY count DESC, source ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # 作者管理/下拉共用: 组装 WHERE (q 匹配 名/曾用名/ID; status/source 精确筛)
    def _author_where(self, q: str = "", status: str = "", source: str = "") -> tuple:
        conds, params = [], []
        q = (q or "").strip()
        if q:
            kw = f"%{q}%"
            # 顺带匹配「来源链接/站点账号」: 记过的链接要能当作搜索入口用
            # (如直接搜 x.com/foo 或 @foo 找到作者) —— 链接不只是展示
            conds.append("(au.name LIKE ? OR au.author_id LIKE ? OR au.aliases_json LIKE ?"
                         " OR au.urls_json LIKE ? OR au.pixiv_url LIKE ? OR au.twitter LIKE ?)")
            params += [kw, kw, kw, kw, kw, kw]
        if status:
            conds.append("au.status = ?")
            params.append(status)
        if source:
            src = str(source).strip().lower()
            # urls_json 是 json.dumps 产物 (键值间有空格) → 比对前先把空格去掉, 免得靠猜格式匹配
            _urls_compact = "REPLACE(COALESCE(au.urls_json,''), ' ', '')"
            if src == 'pixiv':
                # pixiv 作品 source 列存空字符串 ''(见 stats_breakdown 口径), 需特判;
                # 另外: 作者**记过 pixiv 主页链接**也算他有 pixiv 来源 (2026-09-17 需求
                # "记了链接来源列要有反应" —— 作品来源与主页链接取并集)
                conds.append("(EXISTS (SELECT 1 FROM artworks x WHERE x.author_id = au.author_id "
                             "AND COALESCE(x.source,'') = '')"
                             f" OR {_urls_compact} LIKE ?"
                             " OR au.pixiv_url LIKE '%pixiv.net%')")
                params.append('%"site":"pixiv"%')
            else:
                # 其余站点值都是小写 (danbooru/twitter/bilibili/ggac/other), 用 NOCASE 兼容大小写;
                # 同样把"作者主页链接记了该站"算进来
                conds.append("(EXISTS (SELECT 1 FROM artworks x WHERE x.author_id = au.author_id "
                             "AND COALESCE(x.source,'') = ? COLLATE NOCASE)"
                             f" OR {_urls_compact} LIKE ?)")
                params += [src, f'%"site":"{src}"%']
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return where, params

    _AUTHOR_SORT_COLS = {
        "count": "COUNT(a.artwork_id)",
        "name": "au.name",
        "id": "au.author_id",
        "last_updated": "au.last_updated",
    }

    def list_authors(self, *, q: str = "", status: str = "", source: str = "",
                     sort: str = "count", order: str = "desc",
                     limit: "int | None" = None, offset: int = 0) -> list[dict]:
        """作者列表 (作者管理页/作者下拉/统计用, 只含有作品作者的)。

        支持筛选(q 匹配名字/曾用名/ID、status、source)、排序(sort=count/name/id/
        last_updated, order=asc/desc)与分页(limit/offset)。limit=None 返回全量
        (作者下拉等场合复用)。附带各作者作品数、来源分布、状态/曾用名。"""
        where, params = self._author_where(q, status, source)
        sort_col = self._AUTHOR_SORT_COLS.get(sort, "COUNT(a.artwork_id)")
        order_kw = "ASC" if str(order).lower() == "asc" else "DESC"
        sql = f"""
            SELECT au.author_id, au.name, au.aliases_json,
                   au.status, au.last_updated,
                   au.urls_json, au.pixiv_url, au.twitter,
                   COUNT(a.artwork_id) AS count,
                   GROUP_CONCAT(DISTINCT COALESCE(NULLIF(a.source,''), 'pixiv')) AS source_keys
              FROM authors au
              JOIN artworks a ON a.author_id = au.author_id
              {where}
          GROUP BY au.author_id
          ORDER BY {sort_col} {order_kw}, au.name ASC"""
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = params + [max(0, int(limit)), max(0, int(offset))]
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["aliases"] = self._parse_aliases(d.pop("aliases_json", None))
            d["urls"] = _author_urls_of(d)     # 来源链接(可多条): 详情/🌐 按钮/按链接搜索用
            keys = [k for k in (d.pop("source_keys") or "").split(",") if k]
            d["sources"] = keys
            out.append(d)
        return out

    def same_name_pixiv_authors(self, names: "list[str]") -> dict:
        """给一批作者名 → 返回 {名字: {"author_id","name","count"}}：**同名且是真实 pixiv 作者**。

        场景 : 外来作者 (bilibili/X 账号, local_ 键)
        与真实 pixiv 作者同名, 会显示成两行 —— 这是**对的**, 站点账号与 pixiv ID 本就是两种
        身份。但界面要看得出来、并给一键绑定。这里只做"同名 + 纯数字 pixiv ID"的匹配,
        **绝不自动合并**(同名不等于同一人)。count = 名下在库作品数, 供用户判断是不是同一人。
        """
        nm = [str(x).strip() for x in (names or []) if str(x or "").strip()]
        if not nm:
            return {}
        ph = ",".join("?" * len(nm))
        rows = self.conn.execute(
            f"""SELECT au.author_id, au.name, COUNT(a.artwork_id) AS cnt
                  FROM authors au
                  LEFT JOIN artworks a ON a.author_id = au.author_id
                       AND a.trashed_at IS NULL
                 WHERE au.name COLLATE NOCASE IN ({ph})
              GROUP BY au.author_id
              ORDER BY cnt DESC""", nm).fetchall()
        idx: dict = {}
        for r in rows:
            aid = str(r[0])
            if not aid.isdigit():
                continue    # 只认真实 pixiv 作者 (本地作者之间不提示"同名")
            idx.setdefault(str(r[1]).lower(),
                           {"author_id": aid, "name": str(r[1]), "count": int(r[2] or 0)})
        return {n: idx[n.lower()] for n in nm if n.lower() in idx}

    def count_authors_filtered(self, q: str = "", status: str = "",
                               source: str = "") -> int:
        """作者管理页分页总数 (与 list_authors 相同筛选口径)。"""
        where, params = self._author_where(q, status, source)
        return self.conn.execute(
            f"""SELECT COUNT(DISTINCT au.author_id) FROM authors au
                JOIN artworks a ON a.author_id = au.author_id {where}""",
            params).fetchone()[0]

    def update_author(self, author_id: str, *, name: "str | None" = None,
                      status: "str | None" = None,
                      aliases: "list[str] | None" = None,
                      urls: "list | None" = None) -> dict:
        """作者管理页编辑: 改名(旧名自动进曾用名)/状态/曾用名/来源链接手动维护。
        name/status 传 None 表示不改; aliases 传列表则【整体替换】曾用名
        (用户显式编辑, 去重、剔除当前名; 若同时改名, 旧名也会并入)。
        urls 传列表则【整体替换】来源链接 (可多条, 每条可带 site/handle/label):
        同时把旧的 pixiv_url / twitter 两列按站点取第一条回填, 老读者照旧可用。
        返回更新后的作者 dict。"""
        row = self.conn.execute(
            "SELECT name, status, aliases_json FROM authors WHERE author_id=?",
            (author_id,)).fetchone()
        if not row:
            raise KeyError(f"作者不存在: {author_id}")
        cur_name = str(row[0]).strip() if row[0] is not None else ""
        new_name = None
        # 用户手动维护曾用名(整体替换) → 以用户给的为准; 未给则沿用现有
        aliases = ([str(a).strip() for a in (aliases or []) if str(a).strip()]
                   if aliases is not None else list(self._parse_aliases(row[2])))
        if name is not None:
            new_name = str(name).strip()
            if new_name != cur_name and cur_name and cur_name not in aliases:
                aliases.append(cur_name)   # 旧名留存, 绝不丢
        # 去重、剔除当前名 (改名后新名不得进曾用名)
        final: "list[str]" = []
        for a in aliases:
            if a and a != (new_name or cur_name) and a not in final:
                final.append(a)
        now = now_stamp()
        fields = {"last_updated": now}
        if new_name is not None:
            fields["name"] = new_name
        if status is not None:
            fields["status"] = status
        fields["aliases_json"] = json.dumps(final, ensure_ascii=False)
        if urls is not None:
            norm = _norm_author_urls(urls)
            fields["urls_json"] = json.dumps(norm, ensure_ascii=False)
            # 旧列同步 (「🌐 作者主页」按钮等既有读者依赖它们): 每站取第一条, 没有则清空
            for site, col in (("pixiv", "pixiv_url"), ("twitter", "twitter")):
                hit = next((u["url"] for u in norm if u.get("site") == site), "")
                fields[col] = hit or None
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE authors SET {sets} WHERE author_id=?",
            list(fields.values()) + [author_id])
        self.conn.commit()
        return self.get_author(author_id)

    def stats_breakdown(self) -> dict:
        """统计页辅助: 作品来源分布 + 作者存续状态分布 + 作品类型分布 + R18 分布
        (口径: 只含有作品的作者, 与 stats_overview 一致, 避免孤儿作者行)。"""
        src_rows = self.conn.execute(
            """SELECT COALESCE(a.source,'') AS src, COUNT(*) AS cnt
                 FROM artworks a
             GROUP BY src ORDER BY cnt DESC""").fetchall()
        source_dist = [{"source": (r["src"] or "pixiv"), "count": r["cnt"]} for r in src_rows]
        st_rows = self.conn.execute(
            """SELECT COALESCE(au.status,'unknown') AS st, COUNT(DISTINCT au.author_id) AS cnt
                 FROM authors au
                 JOIN artworks a ON a.author_id = au.author_id
             GROUP BY st ORDER BY cnt DESC""").fetchall()
        status_dist = [{"status": (r["st"] or "unknown"), "count": r["cnt"]} for r in st_rows]
        ty_rows = self.conn.execute(
            """SELECT COALESCE(illust_type,'') AS ty, COUNT(*) AS cnt
                 FROM artworks GROUP BY ty ORDER BY cnt DESC""").fetchall()
        _ty_key = {"illust": 0, "manga": 1, "ugoira": 2}

        def _norm_type(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                return _ty_key.get(str(v or "").lower(), 0)

        type_dist = [{"type": _norm_type(r["ty"]), "count": r["cnt"]} for r in ty_rows]
        r18_rows = self.conn.execute(
            """SELECT COALESCE(is_r18,0) AS r18, COUNT(*) AS cnt
                 FROM artworks GROUP BY r18 ORDER BY cnt DESC""").fetchall()
        r18_dist = [{"r18": int(r["r18"]), "count": r["cnt"]} for r in r18_rows]
        # 标签 Top: 统计每张作品 tags_json 里的标签出现次数
        tag_rows = self.conn.execute(
            "SELECT tags_json FROM artworks WHERE tags_json IS NOT NULL AND tags_json != '[]'").fetchall()
        tag_cnt: "dict[str, int]" = {}
        for (tj,) in tag_rows:
            try:
                arr = json.loads(tj)
            except (ValueError, TypeError):
                continue
            if isinstance(arr, list):
                for t in arr:
                    t = str(t).strip()
                    if t:
                        tag_cnt[t] = tag_cnt.get(t, 0) + 1
        tag_dist = [{"tag": k, "count": v} for k, v in sorted(
            tag_cnt.items(), key=lambda kv: (-kv[1], kv[0]))][:60]
        # 作者作品数分布: 每位作者的持有量分桶
        a_rows = self.conn.execute(
            """SELECT COUNT(*) AS cnt FROM artworks a WHERE a.author_id IS NOT NULL
                 GROUP BY a.author_id""").fetchall()
        buckets = [0, 0, 0, 0, 0]  # 1, 2-5, 6-20, 21-100, >100
        for (c,) in a_rows:
            if c == 1:
                buckets[0] += 1
            elif c <= 5:
                buckets[1] += 1
            elif c <= 20:
                buckets[2] += 1
            elif c <= 100:
                buckets[3] += 1
            else:
                buckets[4] += 1
        author_works_dist = [
            {"label": "1 张", "count": buckets[0]},
            {"label": "2-5 张", "count": buckets[1]},
            {"label": "6-20 张", "count": buckets[2]},
            {"label": "21-100 张", "count": buckets[3]},
            {"label": ">100 张", "count": buckets[4]},
        ]
        return {"source_dist": source_dist, "status_dist": status_dist,
                "type_dist": type_dist, "r18_dist": r18_dist,
                "tag_dist": tag_dist, "author_works_dist": author_works_dist}

    def _cleanup_orphan_tags(self) -> int:
        """删除不再被任何作品引用的孤儿 tag (防止标签列表越积越多), 返回删除行数。"""
        before = self.conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        with self.conn:
            self.conn.execute(
                """DELETE FROM tags WHERE tag_id NOT IN
                   (SELECT DISTINCT tag_id FROM artwork_tags)"""
            )
        after = self.conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        return max(0, before - after)

    def _rows_to_dicts(self, rows) -> list[dict]:
        result: list[dict] = []
        for r in rows:
            d = dict(r)
            if d.get('tags_json'):
                try:
                    d['tags'] = json.loads(d['tags_json'])
                except json.JSONDecodeError:
                    d['tags'] = []
                d.pop('tags_json', None)
            result.append(d)
        return result

    # --- 下载清单(🛒购物车) ---
    def queue_add(self, *, item_type: str, status: str = 'pending',
                  title: Optional[str] = None, author_id=None, author_name=None,
                  artwork_id=None, page_count=None, strategy='only_new',
                  source_note=None) -> int:
        cur = self.conn.execute(
            """INSERT INTO download_queue_items
                  (item_type, title, author_id, author_name, artwork_id,
                   page_count, strategy, status, source_note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (item_type, title, author_id, author_name, artwork_id,
             page_count, strategy, status, source_note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def queue_list(self, status: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM download_queue_items"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        # 先入先跑 : 引擎按本顺序取 pending 项执行, 最旧的最先下。
        # 此前是 DESC (最新入队的先跑), 那是「加入清单即自动下载」的配套 —— 刚点的那条
        # 不被积压挡住。但反直觉、看不出谁先谁后, 改为 FIFO。
        # added_at 只到秒, 同秒入队用 id ASC 兜底 (先加的 id 小)。
        # 前端列表不走这条 SQL (自己在 renderTaskTable 里按 added_at 排序), 故无界面副作用。
        sql += " ORDER BY added_at ASC, id ASC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def queue_find_active(self, item_type: str, *,
                          artwork_id=None, author_id=None) -> "Optional[dict]":
        """同目标是否已有**未完成**队列项 (pending/running)。

        2026-09-15: pixez 式去重 —— 「加入清单」前必查, 同一作品/作者绝不重复入队;
        status='done'/'failed' 的历史项不算 (done 已完成可再补下, failed 允许重试)。"""
        sql = ("SELECT * FROM download_queue_items "
               "WHERE item_type=? AND status IN ('pending','running')")
        params: list = [item_type]
        if artwork_id is not None:
            sql += " AND artwork_id=?"
            params.append(str(artwork_id))
        if author_id is not None:
            sql += " AND author_id=?"
            params.append(str(author_id))
        sql += " LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def artwork_download_states(self, aids: Iterable) -> dict:
        """批量查作品下载态 (前端按钮回显, 一条 SQL 避免 N+1):
        {aid: 'downloaded'(页数齐) | 'expired'(原站已失效) | 'in_queue'(有未完成队列项)
              | 'partial'(在库但缺页, 可补下) | 'none'(无记录)}。
        expired 优先级最高, 其次是 in_queue (即使作品已 DONE, 只要队列还在跑就如实显示)。

        完整性**只看 pages**(原网页那 tp 页是否都 DONE), 不再拿 artworks.status 当闸门 ——
        后者是可能陈旧的缓存, 两处实测都会被它卡住 :
          - 87881705: 本地补进缺失的那页后, reconcile_output 只会把 page 刷成 DONE,
            从不去提升 artworks.status(它停在 NOT_YET) → 卡片永远显示「补全页数」,
            点「刷新 / 同步」都不变, 非得点一次「补全页数」联网下载才收敛;
          - 64163186: 作品确已在 pixiv 失效(status=404), 但本地 p0+p1 都完整,
            仍被判 partial → 一直挂着一个点了必然失败的「补全页数」。
        口径与 promote_completed_works 保持一致 (页数与声明一致且全部 DONE)。
        `trashed_at IS NULL` 不能省: 软删掉一页后若仍算 DONE 数, 又会谎报「已入库」。
        total_pages 为空的外来作品没有「完整」概念, 退回只看 status。

        **已失效(404)单独成一态** : 这类作品在 pixiv 上再也拿不到,
        既不能说「已入库」(来源不可靠), 也不能说「补全页数」(点了必然失败) ——
        前端据此改显示「🔎 搜索来源」, 引导用户去别处找来源。"""
        ids = [str(a) for a in aids if a is not None and str(a) != ""]
        out: dict = {a: "none" for a in ids}
        if not ids:
            return out
        marks = ",".join("?" * len(ids))
        for r in self.conn.execute(
                f"""SELECT a.artwork_id AS aid, a.status AS status,
                           a.total_pages AS tp,
                           (SELECT COUNT(*) FROM pages p
                             WHERE p.artwork_id = a.artwork_id
                               AND p.status = {int(PSt.DONE)}
                               AND p.trashed_at IS NULL
                               AND p.page_num < a.total_pages) AS done_in_range
                      FROM artworks a WHERE a.artwork_id IN ({marks})""",
                ids):
            aid = str(r["aid"])
            if r["status"] == WSt.ERROR:
                out[aid] = "expired"
                continue
            # 只数「原网页那 tp 页」(page_num < tp): 超出 tp 的是用户并入的附加页,
            # 不该拿来凑数 —— 否则 tp=2 却只有 p0+p5 时会被误判成完整。
            tp = r["tp"]
            full = (r["done_in_range"] >= int(tp)) if tp else (r["status"] == WSt.DONE)
            out[aid] = "downloaded" if full else "partial"
        for r in self.conn.execute(
                f"SELECT artwork_id FROM download_queue_items "
                f"WHERE item_type='artwork' AND status IN ('pending','running') "
                f"AND artwork_id IN ({marks})", ids):
            aid = str(r["artwork_id"])
            # 已失效的作品入队也必然失败, 别用「已在下载列表」盖掉「搜索来源」的提示
            if out.get(aid) != "expired":
                out[aid] = "in_queue"
        return out

    def author_queue_state(self, author_ids: Iterable) -> dict:
        """作者「全部下载」队列态: {uid: 'in_queue'|'none'} (作者无「已下载完」概念)。"""
        ids = [str(a) for a in author_ids if a is not None and str(a) != ""]
        out: dict = {a: "none" for a in ids}
        if not ids:
            return out
        marks = ",".join("?" * len(ids))
        for r in self.conn.execute(
                f"SELECT author_id FROM download_queue_items "
                f"WHERE item_type='author_all' AND status IN ('pending','running') "
                f"AND author_id IN ({marks})", ids):
            out[str(r["author_id"])] = "in_queue"
        return out

    def queue_author_names(self) -> dict:
        """{author_id(str): author_name} 兜底作者名表:
        反搜归档/孤儿作品在 authors 表可能无名, 但下载入队时记过作者名。"""
        out: dict = {}
        for r in self.conn.execute(
                "SELECT author_id, author_name FROM download_queue_items "
                "WHERE author_id IS NOT NULL AND author_name IS NOT NULL AND author_name != ''"):
            aid = str(r["author_id"])
            if aid not in out:          # 先出现的(最新任务)优先
                out[aid] = r["author_name"]
        return out

    def queue_remove(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM download_queue_items WHERE id=?", (item_id,))
        self.conn.commit()

    def queue_clear(self) -> int:
        return self.queue_clear_scope("all")

    def queue_clear_scope(self, scope: str = "all") -> int:
        """按分组清空下载队列, 返回删除条数。

        scope 与「下载任务」页的四个筛选栏一一对应 :
          all(全部) / running(未完成 = pending+running) / done(已完成) / failed(失败)。
        未知 scope 一律当 all —— 用户点了按钮却静默什么都不做, 比多清更糟。
        """
        cond = {
            "running": "status IN ('pending','running')",
            "done": "status='done'",
            "failed": "status='failed'",
        }.get(str(scope or "all"), "1=1")
        cur = self.conn.execute(
            f"DELETE FROM download_queue_items WHERE {cond}")
        self.conn.commit()
        return cur.rowcount

    def queue_item_alive(self, item_id: int) -> bool:
        """这条队列项是否仍处于「进行中」(行还在 + status='running')。

        2026-09-20 
        。所以下载引擎把它注册成 PixivDownloader 的 abort 回调,
        **逐页**回查: 用户清空队列 → 行没了 → 当前这一项立刻中止, 而不是照旧下完。
        (以前只有全局 _cancel_event, 且只在作品之间检查 → 单作品多页时点停止也傻下完。)
        """
        row = self.conn.execute(
            "SELECT 1 FROM download_queue_items WHERE id=? AND status='running'",
            (int(item_id),)).fetchone()
        return row is not None

    def queue_update_status(self, item_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE download_queue_items SET status=? WHERE id=?",
            (status, item_id),
        )
        self.conn.commit()

    def queue_claim_item(self, item_id: int) -> int:
        """下载引擎「认领」一条任务 (pending → running), 返回受影响行数。

        **返回 0 就是「这条不该再下」的信号** (2026-09-20): 引擎开跑时拿的是
        `queue_list(status='pending')` 快照, 用户清空队列后快照里还留着这些项 ——
        所以每项开跑前先认领一次, 行已被清掉/已被别人改过 → 命中 0 行 → 直接跳过,
        绝不下载。这就是「清空」对用户承诺的。
        (本表 id 是 AUTOINCREMENT, 不会复用, 所以不存在"认领打到新任务上"的问题;
        这道闸门管的是「只改自己认领过、且仍在自己手里的那一行」。)
        """
        cur = self.conn.execute(
            "UPDATE download_queue_items SET status='running' "
            "WHERE id=? AND status='pending'", (item_id,))
        self.conn.commit()
        return cur.rowcount

    def queue_finish_item(self, item_id: int, status: str) -> int:
        """下载引擎收尾写回 (done/failed): 只在**本行仍是 running**时才改, 返回受影响行数。
        理由同 queue_claim_item —— 被清掉的行不该被收尾写回「复活」,
        也不该覆盖别人刚写进去的状态。"""
        cur = self.conn.execute(
            "UPDATE download_queue_items SET status=? "
            "WHERE id=? AND status='running'", (status, item_id))
        self.conn.commit()
        return cur.rowcount

    def queue_release_item(self, item_id: int) -> int:
        """中止后把「进行中」放回「待下载」, 返回受影响行数。

        为什么需要它: 中止有两个来源 ——
          - 用户清空了这一条 → 行没了, 本方法命中 0 行, 什么也不做 (正确);
          - 用户按了「■ 停止任务」→ 行还在且是 running, 若不管它就会**永远卡在
            「进行中」**, 既跑不动也清不掉。放回 pending 才能下次「开始所有未完成」接着下。
        带 `AND status='running'` 条件: 不去动已被清空或被别的操作改过的行。
        """
        cur = self.conn.execute(
            "UPDATE download_queue_items SET status='pending' "
            "WHERE id=? AND status='running'", (int(item_id),))
        self.conn.commit()
        return cur.rowcount

    def queue_clear_done(self) -> int:
        """删除全部已完成(done)任务, 返回删除数。失败任务保留以便重试。"""
        cur = self.conn.execute(
            "DELETE FROM download_queue_items WHERE status='done'")
        self.conn.commit()
        return cur.rowcount

    def queue_reset_failed(self) -> int:
        """把全部失败(failed)任务重置为待下载(pending), 返回重置数。"""
        cur = self.conn.execute(
            "UPDATE download_queue_items SET status='pending' WHERE status='failed'")
        self.conn.commit()
        return cur.rowcount

    def artwork_thumbs(self, artwork_ids: Iterable[str]) -> dict:
        """批量取缩略图直链: {artwork_id: thumb_url} (给下载任务列表展示缩略图)。"""
        ids = [str(x) for x in artwork_ids if str(x)]
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT artwork_id, thumb_url FROM artworks WHERE artwork_id IN ({ph})",
            tuple(ids),
        ).fetchall()
        return {r[0]: r[1] for r in rows if r[1]}

    # --- 反搜历史 ---
    def find_page_by_md5(self, md5: str) -> Optional[dict]:
        """按内容指纹反查图库页 (本地字节级比对, 信任度最高):
        反搜的图若和已下载页 MD5 一致 → 同一张图, 身份确定, 无需信 SauceNAO。"""
        if not md5:
            return None
        row = self.conn.execute(
            "SELECT artwork_id, page_num, local_path, md5 FROM pages "
            "WHERE md5=? ORDER BY page_num LIMIT 1", (md5,)
        ).fetchone()
        return dict(row) if row else None

    def reverse_search_get(self, input_path: str, source: str = 'SauceNAO') -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM reverse_search_history "
            " WHERE input_image_path=? AND search_source=? "
            " ORDER BY searched_at DESC LIMIT 1",
            (input_path, source),
        ).fetchone()
        return dict(row) if row else None

    def reverse_search_put(self, input_path: str, input_md5: Optional[str],
                           confidence: float, source: str,
                           hit_author_id=None, hit_artwork_id=None,
                           hit_page_num=None, corrected=False,
                           raw_json: Optional[str] = None) -> None:
        self.conn.execute(
            """
            INSERT INTO reverse_search_history
                (input_image_path, input_md5, confidence, search_source,
                 hit_author_id, hit_artwork_id, hit_page_num, manually_corrected,
                 raw_response_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(input_image_path, search_source) DO UPDATE SET
                input_md5=excluded.input_md5,
                confidence=excluded.confidence,
                hit_author_id=excluded.hit_author_id,
                hit_artwork_id=excluded.hit_artwork_id,
                hit_page_num=excluded.hit_page_num,
                manually_corrected=excluded.manually_corrected,
                raw_response_json=excluded.raw_response_json,
                searched_at=datetime('now')
            """,
            (input_path, input_md5, confidence, source,
             hit_author_id, hit_artwork_id, hit_page_num,
             1 if corrected else 0, raw_json),
        )
        self.conn.commit()

    # ==================================================================
    # 图库统计面板 (功能 3.1 🏷 图库总览)
    # ==================================================================
    def stats_overview(self, img_root: Optional[Path] = None) -> dict:
        """4 大数字 + 饼图数据,返回 GUI 直接可渲染的 dict。

        作者统计口径: 只计「有作品的作者」, 避免孤儿作者行 (手动删完作品、
        authors 表残留) 让用户看到 「明明没文件了还显示 3 个作者」 的幻觉。
        回收站口径 (2026-09-17): 四大数字都**排除回收站内容**, 与图库列表一致;
        回收站自己的数量单独用 trashed_artworks 提供 (导航角标用)。
        """
        total_artworks = self.conn.execute(
            "SELECT COUNT(*) FROM artworks WHERE trashed_at IS NULL").fetchone()[0]
        trashed_artworks = self.conn.execute(
            "SELECT COUNT(*) FROM artworks WHERE trashed_at IS NOT NULL").fetchone()[0]
        total_pages = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status=? AND trashed_at IS NULL", (PSt.DONE,)
        ).fetchone()[0]
        total_authors = self.conn.execute(
            "SELECT COUNT(DISTINCT author_id) FROM artworks "
            "WHERE author_id IS NOT NULL AND trashed_at IS NULL"
        ).fetchone()[0]

        # 磁盘占用: 优先加总 pages.file_size_bytes, 否则 fallback 扫 img_root
        size_sum = self.conn.execute(
            "SELECT COALESCE(SUM(file_size_bytes), 0) FROM pages "
            "WHERE file_size_bytes IS NOT NULL AND trashed_at IS NULL"
        ).fetchone()[0]
        if size_sum == 0 and img_root and img_root.exists():
            size_sum = 0
            for p in img_root.rglob('*'):
                if p.is_file() and p.suffix.lower() in {'.jpg','.jpeg','.png','.webp','.gif'}:
                    try:
                        size_sum += p.stat().st_size
                    except OSError:
                        pass

        # Top10 作者(按作品数)饼图数据
        rows = self.conn.execute(
            """SELECT COALESCE(au.name, a.author_id) AS author_name,
                      COUNT(*) AS cnt
                 FROM artworks a
            LEFT JOIN authors au ON au.author_id = a.author_id
               WHERE a.trashed_at IS NULL
                GROUP BY 1 ORDER BY cnt DESC LIMIT 10"""
        ).fetchall()
        top_authors = [dict(name=r['author_name'], count=r['cnt']) for r in rows]

        # 按插画/漫画/动图分布
        type_rows = self.conn.execute(
            """SELECT illust_type, COUNT(*) FROM artworks
                WHERE trashed_at IS NULL GROUP BY illust_type"""
        ).fetchall()
        type_map = {0: '插画', 1: '漫画', 2: '动图'}
        type_dist = [dict(name=type_map.get(r[0], f'类型{r[0]}'), count=r[1]) for r in type_rows]

        # 按入库年份
        year_rows = self.conn.execute(
            """SELECT strftime('%Y', COALESCE(publish_date, created_at)) AS y, COUNT(*)
                 FROM artworks WHERE trashed_at IS NULL GROUP BY y ORDER BY y DESC LIMIT 10"""
        ).fetchall()
        year_dist = [dict(year=r[0] or '未知', count=r[1]) for r in year_rows]

        return dict(
            total_artworks=total_artworks,
            trashed_artworks=trashed_artworks,
            total_pages=total_pages,
            total_authors=total_authors,
            disk_size_bytes=size_sum,
            top_authors=top_authors,
            type_distribution=type_dist,
            year_distribution=year_dist,
        )

    # ==================================================================
    # 6 维护 API (对应图库维护面板傻瓜按钮)
    # ==================================================================

    # --- 1. 一键备份 ---
    def backup_to_zip(self, custom_path: Optional[Path] = None) -> Path:
        """备份 pixiv.db + feature_db/ 到一个 zip, 返回 zip 路径。"""
        ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        zip_path = custom_path or (self.backup_dir / f"pixiv_backup_{ts}.zip")
        # 先 VACUUM INTO 到一个临时 db, 避免备份被锁定
        tmp_db = self.backup_dir / f".tmp_backup_{os.getpid()}.db"
        tmp_db.unlink(missing_ok=True)
        try:
            self.conn.execute(f"VACUUM INTO '{self._escape_path(str(tmp_db))}'")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp_db, arcname='pixiv.db')
                # feature_db 目录 (若存在)
                feat_dir = paths.user_data_dir() / 'feature_db'
                if feat_dir.exists():
                    for fp in feat_dir.rglob('*'):
                        if fp.is_file():
                            zf.write(fp, arcname=f"feature_db/{fp.relative_to(feat_dir)}")
                # 备份清单: 记录作品数/页数,给恢复时的对照
                summary = {
                    'created_at': now_stamp(),
                    'artworks': self.conn.execute(
                        "SELECT COUNT(*) FROM artworks").fetchone()[0],
                    'pages_done': self.conn.execute(
                        "SELECT COUNT(*) FROM pages WHERE status=?", (PSt.DONE,)).fetchone()[0],
                    'authors': self.conn.execute(
                        "SELECT COUNT(*) FROM authors").fetchone()[0],
                    'db_path': str(self.db_path),
                }
                zf.writestr('backup_manifest.json',
                            json.dumps(summary, ensure_ascii=False, indent=2))
            self.reporter.log(f"[备份] 完成 → {zip_path} ({zip_path.stat().st_size/1024:.0f} KB)")
            return zip_path
        finally:
            tmp_db.unlink(missing_ok=True)

    # --- 2. 从备份恢复 (对比差异后再确认,  不盲目覆盖) ---
    def compare_backup(self, zip_path: Path) -> RestoreDiff:
        """对比备份 vs 当前 DB, 返回 RestoreDiff。不会执行恢复操作。"""
        if not zipfile.is_zipfile(zip_path):
            raise ValueError(f"{zip_path} 不是合法的 zip 备份文件")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if 'pixiv.db' not in zf.namelist():
                raise ValueError(f"{zip_path} 中未找到 pixiv.db, 不是合法备份")
            tmp = self.backup_dir / f".compare_{os.getpid()}.db"
            tmp.unlink(missing_ok=True)
            zf.extract('pixiv.db', path=tmp.parent)
            # extract 会建 pixiv.db 在 parent 下
            bak_db_path = tmp.parent / 'pixiv.db'
            try:
                bak = sqlite3.connect(str(bak_db_path))
                bak.row_factory = sqlite3.Row
                bak_authors = {r[0] for r in bak.execute("SELECT author_id FROM authors").fetchall()}
                bak_artworks = {r[0] for r in bak.execute("SELECT artwork_id FROM artworks").fetchall()}
                bak.close()
            finally:
                bak_db_path.unlink(missing_ok=True)
        cur_authors = {r[0] for r in self.conn.execute(
            "SELECT author_id FROM authors").fetchall()}
        cur_artworks = {r[0] for r in self.conn.execute(
            "SELECT artwork_id FROM artworks").fetchall()}
        return RestoreDiff(
            backup_only_authors=sorted(bak_authors - cur_authors),
            backup_only_artworks=sorted(bak_artworks - cur_artworks),
            current_only_authors=sorted(cur_authors - bak_authors),
            current_only_artworks=sorted(cur_artworks - bak_artworks),
        )

    def restore_from_zip(self, zip_path: Path, confirm_accept_loss: bool = False) -> RestoreDiff:
        """
        从 zip 备份恢复当前 DB。操作:
          1. 先生成对比 RestoreDiff
          2. 如果当前有作品会丢失(will_lose_current=True):
             - 未传 confirm_accept_loss=True → 抛错, 提示用户
          3. 关闭连接 → 覆盖 pixiv.db → 重建连接 → 重跑迁移器
        返回对比结果, 供 GUI 展示"恢复了多少条"。
        """
        diff = self.compare_backup(zip_path)
        if diff.will_lose_current() and not confirm_accept_loss:
            raise RuntimeError(
                f"拒绝盲目恢复: 当前比备份多出 {len(diff.current_only_authors)} 作者 / "
                f"{len(diff.current_only_artworks)} 作品, 恢复会永久丢失。\n"
                f"确需接受丢失时, 请勾选【接受丢失并恢复】后重试。"
            )
        # 先备份一份当前 DB, 留一条反悔的路
        self.backup_to_zip(self.backup_dir / "pre_restore_safety.zip")
        # 执行恢复
        self.close()
        tmp_dir = self.backup_dir / f".restore_{os.getpid()}"
        tmp_dir.mkdir(exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extract('pixiv.db', path=tmp_dir)
            restored = tmp_dir / 'pixiv.db'
            shutil.copy2(restored, self.db_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        # 重建连接 + 重跑迁移器(备份可能是旧版本 schema)
        self.conn = _connect(self.db_path)
        self.cursor = self.conn.cursor()
        migrator = MigrationManager(self.db_path)
        migrator.ensure_migrations_applied()
        self.reporter.log(
            f"[恢复] 完成: 备份新增 {len(diff.backup_only_artworks)} 作品 / "
            f"丢失 {len(diff.current_only_artworks)} 作品(若 accept_loss=True)"
        )
        return diff

    # --- 3. 修复损坏的库 : 绝不直接删库) ---
    def repair_corrupt(self) -> dict:
        """
        三步修复:
          A. 先 PRAGMA integrity_check, 如果 OK 仅重建索引即可
          B. integrity 失败 → 用 sqlite3_shell 的 .recover (或 Python dump 回退)
          C. 备份 → 新建库 → 导可恢复的数据回来
        返回 dict(ok/recovered_rows/failed, report=str)
        """
        # A) integrity check
        try:
            integ = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as e:
            integ = f"connect_error: {e}"
        if integ == 'ok':
            # 重建所有索引 + VACUUM
            self.conn.execute("REINDEX")
            self.conn.commit()
            self.conn.execute("VACUUM")
            return dict(ok=True, integrity='ok', recovered='reindexed', report='完整性检查通过, 已重建所有索引 + VACUUM。')

        # B) 损坏: 尝试 dump 方式
        self.reporter.warn(f"[修复] integrity_check 失败: {integ}。尝试 dump 恢复...")
        self.backup_to_zip(self.backup_dir / "pre_repair_safety.zip")
        self.close()
        recover_script = io.StringIO()
        recovered: dict[str, int] = {}
        failed: list[str] = []
        try:
            corrupt = sqlite3.connect(str(self.db_path))
            corrupt.row_factory = sqlite3.Row
            # 逐表 dump (跳过损坏行)
            tables = [r[0] for r in corrupt.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
            for tbl in tables:
                try:
                    rows = corrupt.execute(f"SELECT * FROM {tbl}").fetchall()
                    if not rows:
                        recover_script.write(f"DELETE FROM {tbl};\n")
                        continue
                    cols = [d[0] for d in corrupt.execute(f"SELECT * FROM {tbl} LIMIT 1").description]
                    for r in rows:
                        vals = []
                        for c in cols:
                            v = r[c]
                            if v is None:
                                vals.append('NULL')
                            elif isinstance(v, (int, float)):
                                vals.append(str(v))
                            else:
                                s = str(v).replace("'", "''")
                                vals.append(f"'{s}'")
                        recover_script.write(
                            f"INSERT OR IGNORE INTO {tbl} ({','.join(cols)}) "
                            f"VALUES ({','.join(vals)});\n"
                        )
                    recovered[tbl] = len(rows)
                except sqlite3.Error as e:
                    failed.append(f"{tbl}: {e}")
            corrupt.close()
        except sqlite3.Error as e:
            failed.append(f"无法读取损坏的库: {e}")

        # 写新库
        new_path = self.db_path.with_suffix(self.db_path.suffix + '.recovered')
        if new_path.exists():
            new_path.unlink()
        try:
            # 新库先走迁移器得到干净 schema
            new_mgr = MigrationManager(new_path)
            new_mgr.ensure_migrations_applied()
            new_conn = sqlite3.connect(str(new_path))
            try:
                new_conn.executescript(recover_script.getvalue())
                new_conn.commit()
                new_conn.execute("REINDEX")
                new_conn.commit()
                new_conn.execute("VACUUM")
            finally:
                new_conn.close()
            # 替换
            old_bak = self.db_path.with_suffix(self.db_path.suffix + '.before_repair')
            shutil.move(str(self.db_path), str(old_bak))
            shutil.move(str(new_path), str(self.db_path))
        except Exception as e:
            raise RuntimeError(f"[修复] 写新库失败: {e}。损坏库仍在 {self.db_path}, 已备份 {old_bak if 'old_bak' in locals() else None}") from e
        finally:
            pass

        # 重新连上
        self.conn = _connect(self.db_path)
        self.cursor = self.conn.cursor()
        total_recovered = sum(recovered.values())
        report_lines = [f"integrity: {integ}",
                        f"已恢复表 {len(recovered)} 张, 共 {total_recovered} 行:"]
        for t, n in recovered.items():
            report_lines.append(f"  · {t}: {n} 行")
        if failed:
            report_lines.append(f"失败 {len(failed)} 项:")
            for f in failed:
                report_lines.append(f"  ✗ {f}")
        return dict(ok=total_recovered > 0, integrity=integ,
                    recovered=total_recovered, failed_tables=failed,
                    report='\n'.join(report_lines))

    # --- 4. 清理孤儿记录 ---
    def clean_orphan_records(self, img_root: Path, dry_run: bool = True) -> OrphanReport:
        """
        扫描 img_root 下的图片, 对比 pages 表:
          a) DB 标 status=DONE, 但磁盘上没有 local_path → 改成 FAILED (missing)
          b) 遍历保存目录: 空的作者子目录 → 删掉
          c) 软删除清理: artworks/pages.status 不存在此列, 但会检查 pages 中
             artwork_id 在 artworks 里不存在的孤儿行 (外键没开的情况下)
        返回 OrphanReport。dry_run=True 时只算不改。
        """
        missing: list[str] = []
        # a) 所有 status=DONE 的页 → 本地查 local_path
        rows = self.conn.execute(
            "SELECT p.artwork_id, p.page_num, p.local_path FROM pages p "
            " WHERE p.status=? AND p.local_path IS NOT NULL",
            (PSt.DONE,),
        ).fetchall()
        for r in rows:
            full = img_root / r['local_path'] if r['local_path'] and not Path(r['local_path']).is_absolute() else Path(r['local_path'])
            if not full.exists():
                missing.append(f"{r['artwork_id']}_p{r['page_num']}")

        # b) 空目录 (仅 img_root 下两层: <作者目录>)
        empty_dirs: list[str] = []
        if img_root.exists():
            for d in img_root.iterdir():
                if d.is_dir():
                    pics_inside = list(d.rglob('*'))
                    # 只含空/配置/缩略图缓存不算, 真有图片文件才保留
                    real_pics = [p for p in pics_inside
                                 if p.is_file() and p.suffix.lower() in
                                 {'.jpg','.jpeg','.png','.webp','.gif','.mp4','.zip'}]
                    if not real_pics:
                        empty_dirs.append(str(d))

        # c) 外键未开启情况下的 pages 孤儿行
        self.cursor.execute(
            "DELETE FROM pages WHERE artwork_id NOT IN (SELECT artwork_id FROM artworks)"
        )
        soft_deleted = self.cursor.rowcount if self.cursor.rowcount > 0 else 0
        if not dry_run:
            self.conn.commit()
        else:
            self.conn.rollback()

        report = OrphanReport(
            db_missing_on_disk=missing,
            empty_author_dirs=empty_dirs,
            soft_deleted_rows=soft_deleted,
        )
        # a) 和 b) 的改动需要 DML/文件系统, 仅 dry_run=False 时执行
        if not dry_run:
            # a) 改成 FAILED
            if missing:
                with self.conn:
                    for key in missing:
                        m = re.match(r'(\d+)_p(\d+)', key)
                        if m:
                            aid, pn = m.group(1), int(m.group(2))
                            self.conn.execute(
                                "UPDATE pages SET status=?, local_path=NULL "
                                " WHERE artwork_id=? AND page_num=?",
                                (PSt.FAILED, aid, pn),
                            )
            # b) 真删空目录
            for d in empty_dirs:
                try:
                    Path(d).rmdir()
                except OSError:
                    pass
        return report

    # --- 5. 重建图库映射 ---
    def rebuild_image_mappings(self, img_root: Path) -> RebuildReport:
        """用户手动搬移图片目录/盘符后, 扫一遍文件名重建 pages.local_path。"""
        real: "dict[tuple[str, int], Path]" = {}
        for p in img_root.rglob('*'):
            if not p.is_file() or p.suffix.lower() not in {'.jpg','.jpeg','.png','.webp','.gif'}:
                continue
            # 2026-09-15: 统一严格识别器; 旧宽松 re.search 会让外来源文件名里的数字尾巴
            # 顶替真作品页的 local_path (全库路径重写, 污染面最大的一处)。
            _nkey = pixiv_natural_key(p.name)
            if not _nkey:
                continue
            real[(_nkey[0], int(_nkey[1]))] = p

        already, new = 0, 0
        with self.conn:
            for (aid, pn), p in real.items():
                existing = self.conn.execute(
                    "SELECT local_path, trashed_at FROM pages WHERE artwork_id=? AND page_num=?",
                    (aid, pn),
                ).fetchone()
                if existing and existing[1]:
                    # 该页在回收站里: local_path 是"恢复时要搬回哪里"的权威答案, 绝不能
                    # 被重建映射覆盖 (否则恢复就搬错地方了)。直接跳过这一页 (2026-09-17)。
                    continue
                rel = str(p.relative_to(img_root))
                if existing and existing[0] == rel:
                    already += 1
                else:
                    self.conn.execute(
                        "INSERT INTO pages (artwork_id, page_num, status, local_path) "
                        "VALUES (?,?,?,?) "
                        "ON CONFLICT(artwork_id, page_num) DO UPDATE SET "
                        "  status=?, local_path=excluded.local_path",
                        (aid, pn, PSt.DONE, rel, PSt.DONE),
                    )
                    # 顺便写 artworks 占位行
                    self.conn.execute(
                        "INSERT OR IGNORE INTO artworks(artwork_id, status, last_check) "
                        "VALUES (?,?,datetime('now'))",
                        (aid, WSt.PLACEHOLDER),
                    )
                    new += 1
        author_dirs = sum(1 for d in img_root.iterdir() if d.is_dir()) if img_root.exists() else 0
        return RebuildReport(
            scanned_images=len(real),
            already_mapped=already,
            new_mappings=new,
            author_dir_count=author_dirs,
        )

    # --- 6. 导出作品清单 CSV ---
    def export_artworks_csv(self, out_csv: Path) -> int:
        sql = """
        SELECT a.artwork_id,
               a.title,
               au.name           AS author_name,
               a.author_id,
               CASE a.illust_type WHEN 0 THEN '插画' WHEN 1 THEN '漫画' WHEN 2 THEN '动图'
                    ELSE '未知' END AS illust_type,
               a.total_pages,
               a.bookmark_count,
               a.publish_date,
               CASE a.is_r18 WHEN 1 THEN '是' ELSE '否' END AS r18,
               a.tags_json       AS tags,
               a.created_at      AS added_to_library,
               GROUP_CONCAT(p.local_path, '; ') AS local_files
          FROM artworks a
     LEFT JOIN authors au ON au.author_id = a.author_id
     LEFT JOIN pages   p  ON p.artwork_id = a.artwork_id AND p.status=1
      GROUP BY a.artwork_id
      ORDER BY COALESCE(a.publish_date, a.created_at) DESC
        """
        rows = self.conn.execute(sql).fetchall()
        columns = ['作品ID','标题','作者名','作者ID','类型','页数','收藏数','发布日期',
                   'R18','标签','入库时间','本地文件路径']
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(out_csv, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(columns)
            for r in rows:
                w.writerow(list(r))
        return len(rows)

    # ==================================================================
    # internal helpers
    # ==================================================================
    def _sync_artwork_tags(self, artwork_id: str, tags: Iterable[str]) -> None:
        """把一组标签名同步到 tags + artwork_tags 两张表 (idempotent)。
        注意: tags 为空列表 = 清空该作品的标签。"""
        tags = [t for t in tags if t]
        for name in tags:
            self.conn.execute(
                "INSERT OR IGNORE INTO tags(tag_name) VALUES (?)", (name,)
            )
        self.conn.commit()
        if tags:
            tag_ids: dict[str, int] = {
                r['tag_name']: r['tag_id']
                for r in self.conn.execute(
                    "SELECT tag_id, tag_name FROM tags WHERE tag_name IN (%s)"
                    % ','.join('?' * len(tags)),
                    tuple(tags),
                ).fetchall()
            }
        else:
            tag_ids = {}
        with self.conn:
            self.conn.execute(
                "DELETE FROM artwork_tags WHERE artwork_id=?", (artwork_id,)
            )
            for name in tags:
                tid = tag_ids.get(name)
                if tid is None:
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO artwork_tags(artwork_id, tag_id) VALUES (?, ?)",
                    (artwork_id, tid),
                )

    @staticmethod
    def _escape_path(p: str) -> str:
        """SQL VACUUM INTO 用 —— 只保留安全字符。"""
        return p.replace("'", "''")
        return p.replace("'", "''")
