# core/search_client.py
"""
P3 拆分: Pixiv 查询客户端 (纯查询, 不下载图)

解决的问题:
  - 原来 PixivDownloader 一创建就开浏览器 + 跑下载, 顶栏搜索点一下就下载太重
  - 现在 SearchClient: 只查信息 (AuthorInfo / ArtworkInfo), 返回给 GUI 画卡片
  -  + 点➕加清单后, 才由 DownloadEngine 真正去下载
  - 同一 Selenium driver 复用, 连续查询不需要每次重启 Edge

输入类型识别 parse_input(s):
  artwork_id  : 纯数字 (7-10 位, 默认当作作品, 作品不存在 fallback 查作者)
  author_id   : 纯数字 (查询作品没命中时 fallback)
  artwork_url : https://www.pixiv.net/artworks/12345678
  author_url  : https://www.pixiv.net/users/1234567 或 /member_illust.php?id=...
  keyword     : 其它文本 (P4 接关键字搜索, P3 阶段抛 NotImplementedError 友好提示)
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from utils import paths
from utils.config import load_config
from utils.reporter import Reporter, PrintReporter
from utils.browser import make_browser, login_with_cookies

from core.models import AuthorInfo, ArtworkInfo, IllustType


# =============================================================
# 输入识别
# =============================================================
@dataclass(slots=True, frozen=True)
class ParsedQuery:
    kind: str  # 'artwork_id' | 'author_id' | 'artwork_url' | 'author_url' | 'keyword'
    value: str
    raw: str

    def is_id_based(self) -> bool:
        return self.kind in {'artwork_id', 'author_id', 'artwork_url', 'author_url'}


_RE_ARTWORK_URL = re.compile(
    r'pixiv\.net(?:/[a-z]{2})?/artworks/(\d{5,})', re.I)
_RE_USER_URL = re.compile(
    r'pixiv\.net(?:/[a-z]{2})?/users/(\d{3,})', re.I)
_RE_MEMBER_URL = re.compile(
    r'member_illust\.php\?.*(?:id|member_id)=(\d{3,})', re.I)
_RE_PURE_NUM = re.compile(r'^\s*(\d{3,20})\s*$')

# P4b+: pixiv 官方 ajax JSON 接口 —— 浏览器自带 cookies/请求头, 结构稳定,
# 0.3~0.5s 出全量数据。以前爬 DOM (xpaths.json 全是 __next/div/div[2] 绝对路径)
# pixiv 一改版就全失效 → 「搜索只出 1 条/作者爬成空」。
_AJAX_BASE = "https://www.pixiv.net/ajax"

# 作者搜索结果里「作品列表」每批条数 
# 与图库分页/搜索一次 60 条的口径一致。
AUTHOR_WORKS_PAGE = 60

# 关键字搜索结果每批条数。
# 一直是 24 (沿用原显示条数, 不改变用户习惯), 只是现在能往后翻了。
KEYWORD_WORKS_PAGE = 24

# pixiv 搜索接口每页固定返回 60 条 (实测)。「换一批」按 24 条一步走,
# 所以一页刚好够翻 2 次多 —— 跨页时自动续取下一页, 不重复也不跳过。
_PIXIV_SEARCH_PER_PAGE = 60

# 「没搜索时显示一批推荐」的两个来源。
#   ranking   = pixiv 日榜 (ranking.php?format=json), 50 条/页, 有排名, **不返回 aiType**
#   discovery = pixiv 个性化推荐 (/ajax/illust/discovery), 10 条, **自带 aiType**
# 两者都要能套「隐藏 AI 作品」—— discovery 直接就能判, ranking 得靠调用方联网补。
FEED_KINDS = ('ranking', 'discovery')
RANKING_PER_PAGE = 50          # ranking.php 一页固定 50 条 (实测)


def _int_or(v, default: int = 0) -> int:
    """宽松取整: pixiv 的 ranking.php 会把数字全写成字符串 ("4"、"3240")。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _illust_type_of(v) -> IllustType:
    return {0: IllustType.ILLUSTRATION,
            1: IllustType.MANGA,
            2: IllustType.UGOIRA}.get(_int_or(v, 0), IllustType.ILLUSTRATION)


def _rank_item_to_info(it: dict) -> "ArtworkInfo | None":
    """ranking.php 的单条 → ArtworkInfo (纯映射, 无浏览器依赖)。

    ⚠ **排行榜接口不返回 aiType** (逐字段实测确认, `illust_content_type` 里只有
    sexual/lo/grotesque/original/furry 这些旗标) → 这里 ai_type 恒为 0=未知。
    要按 AI 过滤就必须由调用方联网补 (见 web/server.py 的 /api/search/feed)。

    字段名与 ajax/illust 完全不同 (illust_id / user_id / illust_page_count / 字符串数字),
    所以只能单写一份映射, 复用不了 `_illust_body_to_info`。
    """
    if not isinstance(it, dict) or not it.get("illust_id"):
        return None
    aid = str(it["illust_id"])
    ct = it.get("illust_content_type") or {}
    pub = None
    ts = _int_or(it.get("illust_upload_timestamp"), 0)
    if ts > 0:
        try:
            pub = datetime.fromtimestamp(ts).isoformat(timespec='seconds')
        except (OSError, OverflowError, ValueError):
            pub = None
    return ArtworkInfo(
        artwork_id=aid,
        author_id=str(it.get("user_id") or ''),
        title=(it.get("title") or '').strip() or f"#{aid}",
        illust_type=_illust_type_of(it.get("illust_type")),
        page_count=max(1, _int_or(it.get("illust_page_count"), 1)),
        tags=[str(t) for t in (it.get("tags") or [])],
        image_urls=[it.get("url")] if it.get("url") else [],
        publish_date=pub,
        is_r18=_int_or(ct.get("sexual"), 0) >= 1,
        ai_type=0,
        author_name=(it.get("user_name") or '').strip() or None,
    )


def parse_input(s: str) -> ParsedQuery:
    """把顶栏搜索框/粘贴的字符串分类。永远返回 ParsedQuery, 不抛异常。"""
    raw = (s or '').strip()
    if not raw:
        return ParsedQuery('keyword', '', raw)

    if 'pixiv.net' in raw or 'member_illust.php' in raw:
        m = _RE_ARTWORK_URL.search(raw)
        if m:
            return ParsedQuery('artwork_url', m.group(1), raw)
        m = _RE_USER_URL.search(raw)
        if m:
            return ParsedQuery('author_url', m.group(1), raw)
        m = _RE_MEMBER_URL.search(raw)
        if m:
            return ParsedQuery('author_url', m.group(1), raw)
        return ParsedQuery('keyword', raw, raw)

    m = _RE_PURE_NUM.match(raw)
    if m:
        digits = m.group(1)
        # 纯数字: 8 位以上大概率作品, 但我们先当作 artwork 查, 查不到 fallback 作者
        return ParsedQuery('artwork_id', digits, raw)

    return ParsedQuery('keyword', raw, raw)


# =============================================================
# 查询 Client
# =============================================================
class SearchClient:
    """
    Selenium 驱动的轻量查询器。推荐用 with:
        with SearchClient() as sc:
            author, works = sc.fetch_author('123456')
            info = sc.fetch_artwork('98765432')
    """
    def __init__(self, reporter: "Reporter | None" = None, config: "dict | None" = None,
                 headless: "bool | None" = None):
        """
        headless=None (默认) 时读 config['browser']['headless'] (现在默认 True 全程无头);
        只有手动登录/用户显式要求时才传 headless=False 开可见浏览器。

        惰性起浏览器: 构造时不开 Edge。查询优先 HTTP 直连 (cookies 打 ajax 接口,
        0.3~0.5s, 第三方工具同款做法), 只有 DOM 兜底/登录探测才真正需要浏览器,
        第一次用到时按需创建 (见 driver 属性)。
        """
        self.config = config or load_config()
        self.reporter = reporter or PrintReporter()
        self._headless = headless
        self._explicit_wait = self.config["browser"]["explicit_wait_crawler"]
        self._driver = None
        self._wait = None
        with open(paths.resource("xpaths.json"), encoding="utf-8") as f:
            xp = json.load(f)
        self.xp = xp["pixiv"]
        # cookies 路径: 和 crawler.py / reverse_source.py 一致, 统一从 paths 取
        pcfg = self.config["paths"]
        self.cookies_file = paths.user_data(pcfg["cookies_filename"])
        self.login_ok = False
        self._closed = False
        # HTTP 直连会话 (惰性): None=未尝试, dict=可用, False=已尝试但 cookies 无效/过期
        self._http = None
        self._http_cookie_mtime = 0.0
        # P4a+: 客户端内部操作锁 —— 同一个 Selenium driver 不允许两个线程同时驱动。
        # RLock: execute() 内部再调 ensure_login() 同线程可重入。
        # 注意: app 的 _search_client_lock 只保护「创建/替换客户端」这个短暂动作,
        #       网络操作期间绝不再持有 app 锁 (否则自动登录卡网络会拖死所有查询)。
        self.op_lock = threading.RLock()

    @property
    def driver(self):
        """惰性 Edge 驱动: 第一次被真正需要时才创建; 创建时自动带 cookies 登录
        (login_with_cookies), 保证任何 DOM 兜底路径都有登录态。"""
        if self._driver is None and not self._closed:
            self._driver, self._wait = make_browser(
                headless=self._headless, explicit_wait=self._explicit_wait,
                reporter=self.reporter, config=self.config,
            )
            try:
                self.login_ok = login_with_cookies(self._driver, self.cookies_file, self.reporter)
            except Exception:
                self.login_ok = False
        return self._driver

    @driver.setter
    def driver(self, value):
        """允许外部注入 (冒烟测试 FakeDriver / 调试)。"""
        self._driver = value
        self._wait = None

    @property
    def wait(self):
        if self._wait is None:
            _ = self.driver
        return self._wait

    def _http_user_id(self) -> str:
        """account.json 里的 user_id (x-user-id 请求头, 部分接口依赖)。"""
        try:
            acc = json.loads((paths.data_root() / "account.json").read_text(encoding="utf-8"))
            return str(acc.get("user_id") or "")
        except Exception:
            return ""

    def _http_session(self) -> "dict | None":
        """惰性构建 HTTP 直连会话 (cookies → 请求头)。None=不可用。

        cookies 文件 mtime 变化 (重新登录) 时自动重建, 登录失效后可自愈。
        """
        cf = getattr(self, "cookies_file", None)
        if not cf:
            return None   # 测试桩/未初始化对象 → 走浏览器兜底
        try:
            import os as _os
            mtime = _os.path.getmtime(cf)
        except OSError:
            mtime = 0.0
        if self._http is None or (self._http is False and mtime != self._http_cookie_mtime):
            self._http = False
            self._http_cookie_mtime = mtime
            try:
                from utils.sec import load_cookies_smart
                cookies = load_cookies_smart(str(cf), upgrade=False)
                if cookies:
                    sess = build_pixiv_http_session(cookies, self._http_user_id())
                    if sess:
                        self._http = sess
            except Exception:
                pass
        return self._http or None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ---------- public ----------
    def ensure_login(self) -> bool:
        with self.op_lock:
            if self.login_ok:
                return True
            # HTTP 直连会话可用 → cookies 已就绪, 无需启动浏览器登录
            if self._http_session() is not None:
                self.login_ok = True
                return True
            # 无 cookies/HTTP 失效 → 惰性创建浏览器 (driver 属性内部自动带 cookies 登录)
            _ = self.driver
            return self.login_ok

    def fetch_logged_in_user(self) -> "Optional[dict]":
        """探测当前登录账号 {'user_id','name','avatar'}, 未登录/失败返回 None。(P4a+)"""
        with self.op_lock:
            try:
                from utils.browser import fetch_pixiv_user
                return fetch_pixiv_user(self.driver)
            except Exception:
                return None

    def execute(self, q: ParsedQuery):
        """统一入口: GUI 顶栏搜完调它。返回 dict 结构见下。

        P4a+: op_lock 串行化 —— 若后台自动登录/上一查询正在用浏览器,
        这里排队等待并在日志里给出提示, 用户能看懂为什么慢。"""
        if not self.op_lock.acquire(blocking=False):
            self.reporter.log("⏳ 共享浏览器正忙(后台自动登录/上一查询), 本次查询排队等待…")
            self.op_lock.acquire()
        try:
            return self._execute_locked(q)
        finally:
            self.op_lock.release()

    def execute_scoped(self, q: ParsedQuery, scope: str,
                       offset: int = 0, limit: int = AUTHOR_WORKS_PAGE):
        """按用户选定的类型搜索, 不做自动猜测/二次 fallback, 减少查询成本。

        scope: 'artwork' 只搜作品 | 'author' 只搜作者 | 其它/None 走原自动逻辑 execute()。
        offset/limit (2026-09-18): 「作者作品换一批」—— 只影响作者结果里的作品列表。
        返回 dict 结构与 execute() 一致的子集。
        """
        offset, limit = max(0, int(offset or 0)), max(1, int(limit or AUTHOR_WORKS_PAGE))
        if not self.op_lock.acquire(blocking=False):
            self.reporter.log("⏳ 共享浏览器正忙(后台自动登录/上一查询), 本次查询排队等待…")
            self.op_lock.acquire()
        try:
            self.ensure_login()
            if scope == 'artwork':
                return self._execute_artwork_scope(q, offset=offset, limit=limit)
            if scope == 'author':
                return self._execute_author_scope(q, offset=offset, limit=limit)
            return self._execute_locked(q, offset=offset, limit=limit)
        finally:
            self.op_lock.release()

    def _execute_artwork_scope(self, q: ParsedQuery, *, offset: int, limit: int):
        """只查作品。作者ID/URL 则返回该作者的作品列表; 关键字只返回作品(不扫作者, 省一次 DOM/请求)。"""
        if q.kind in ('artwork_id', 'artwork_url'):
            art = self.fetch_artwork(q.value)
            if not art:
                raise ArtworkNotFoundError(q.value)
            author = self.fetch_author_info_only(art.author_id) if art.author_id else None
            return {'type': 'artwork', 'artwork': art, 'author': author,
                    'related_works': []}
        if q.kind in ('author_id', 'author_url'):
            author, works = self.fetch_author(q.value, limit=limit, offset=offset)
            return {'type': 'author', 'author': author, 'works': works}
        res = self.search_keyword(q.value, offset=offset)
        return {'type': 'keyword_works', 'keyword': res.get('keyword', ''),
                'works': res.get('works', [])}

    def _execute_author_scope(self, q: ParsedQuery, *, offset: int, limit: int):
        """只查作者。作品ID 当作者搜(通常无结果, 明确报错引导); 关键字只返回相关作者(不扫作品)。"""
        if q.kind in ('author_id', 'author_url'):
            author, works = self.fetch_author(q.value, limit=limit, offset=offset)
            return {'type': 'author', 'author': author, 'works': works}
        if q.kind in ('artwork_id', 'artwork_url'):
            try:
                author, works = self.fetch_author(q.value, limit=limit, offset=offset)
                return {'type': 'author', 'author': author, 'works': works}
            except AuthorNotFoundError:
                raise AuthorNotFoundError(
                    f"{q.value} 不是有效的作者 ID (当前是作品 ID)。请切到「搜索作品」页重试。")
        # 关键字 → 真正按作者名搜 (走搜用户接口, 而不是从作品接口顺带提取作者)
        authors = self.search_authors(q.value)
        return {'type': 'keyword_authors', 'keyword': q.value, 'authors': authors}

    def _execute_locked(self, q: ParsedQuery, *, offset: int = 0,
                        limit: int = AUTHOR_WORKS_PAGE):
        self.ensure_login()
        if q.kind in ('artwork_id', 'artwork_url'):
            try:
                art = self.fetch_artwork(q.value)
                author = None
                if art and art.author_id:
                    author = self.fetch_author_info_only(art.author_id)
                return {'type': 'artwork', 'artwork': art, 'author': author,
                        'related_works': []}
            except ArtworkNotFoundError as e:
                # fallback 作者
                try:
                    auth, works = self.fetch_author(q.value, limit=limit, offset=offset)
                    return {'type': 'author', 'author': auth, 'works': works}
                except Exception:
                    raise e
        if q.kind in ('author_id', 'author_url'):
            author, works = self.fetch_author(q.value, limit=limit, offset=offset)
            return {'type': 'author', 'author': author, 'works': works}
        # keyword (P4a)
        return self.search_keyword(q.value, offset=offset)

    def _search_page(self, keyword: str, s_mode: str, p: int) -> list:
        """取 pixiv 关键字搜索第 p 页的原始 60 条 (带页级小缓存)。

        缓存是给「换一批」用的: 按 24 条一步走, 而 pixiv 一页 60 条 ——
        offset 0/24/48 全落在第 1 页, 没缓存就要把同一页反复请求三次
        (用户明确担心被网站风控, 能省一次是一次)。缓存只在本次进程内有效,
        够服务「连点换一批」这个场景就行, 所以满了直接清空, 不做 LRU。
        """
        cache = getattr(self, '_kw_page_cache', None)
        if cache is None:
            cache = self._kw_page_cache = {}
        key = (keyword, s_mode, p)
        if key in cache:
            return cache[key]
        encoded = urllib.parse.quote(keyword, safe='')
        url = (f"{_AJAX_BASE}/search/artworks/{encoded}"
               f"?word={encoded}&order=date_d&mode=all&p={p}"
               f"&s_mode={s_mode}&type=all&lang=zh")
        self.reporter.log(f"[查询] 关键字搜索: {keyword} (s_mode={s_mode}, p={p})")
        body = self._ajax_get(url)
        data = (body.get("illustManga") or {}).get("data") or []
        if len(cache) >= 16:
            cache.clear()
        cache[key] = data
        return data

    def _search_raw(self, keyword: str, s_mode: str, *, offset: int, limit: int) -> list:
        """按 offset 连续取 limit 条原始搜索结果, 跨 pixiv 分页自动续取。

        为什么不能直接把 p+1: 「换一批」一次只走 24 条, 而 pixiv 一页 60 条 ——
        跳到下一页会把这一页里没用到的 36 条白白丢掉。这里从 offset 开始一条不落地
        往后取 (offset=48 时先吃第 1 页剩的 12 条, 再续第 2 页的前 12 条),
        用户按 24 一步步走能把整站结果翻完。
        """
        offset, limit = max(0, int(offset or 0)), max(1, int(limit or 1))
        out: list = []
        p = offset // _PIXIV_SEARCH_PER_PAGE + 1
        start = offset % _PIXIV_SEARCH_PER_PAGE
        while len(out) < limit:
            try:
                page = self._search_page(keyword, s_mode, p)
            except Exception:
                if not out:
                    raise      # 第一页就挂了 → 交给上层走 DOM 兜底
                break          # 续页挂了 → 有多少给多少, 别把已取到的结果也丢了
            if not page:
                break          # 后面没有结果了 (翻到底 / 该词无匹配)
            out.extend(page[start:])
            p += 1
            start = 0
        return out[:limit]

    def search_ranking(self, *, limit: int = KEYWORD_WORKS_PAGE) -> list:
        """pixiv 日榜 (没搜索时显示的推荐之一, 2026-09-20)。

        走老接口 `ranking.php?mode=daily&format=json` —— **不是** `/ajax/` 那套
        (实测 `/ajax/illust/ranking*` 各种 mode 全 404), 所以要用 unwrap=False。
        一页 50 条。**返回体没有 aiType**, 想按 AI 过滤得由调用方补
        (见 web/server.py 的 /api/search/feed)。
        """
        body = self._ajax_get(
            "https://www.pixiv.net/ranking.php?mode=daily&format=json&p=1",
            unwrap=False) or {}
        items = body.get("contents") or []
        self.reporter.log(f"[查询] 排行榜: 取到 {len(items)} 条 (date={body.get('date')})")
        out = []
        for it in items:
            info = _rank_item_to_info(it)
            if info is not None:
                out.append(info)
            if len(out) >= limit:
                break
        return out

    def search_discovery(self, *, limit: int = KEYWORD_WORKS_PAGE) -> list:
        """pixiv 个性化推荐 (没搜索时显示的推荐之二, 2026-09-20)。

        走 `/ajax/illust/discovery?mode=all`。字段与 `profile/illusts` **同构**
        (id/title/userId/url/aiType/pageCount/tags…), 所以直接复用
        `_profile_work_to_info`, 不再写第二份映射。**自带 aiType**, AI 过滤零额外请求。

        接口一次只给 10 条 (limit 参数实测不起作用), 够填一屏推荐了。
        未登录时可能拿不到个性化结果 → 抛异常, 由调用方降级。
        """
        body = self._ajax_get(f"{_AJAX_BASE}/illust/discovery?mode=all&lang=zh") or {}
        items = body.get("illusts") or []
        self.reporter.log(f"[查询] 个性化推荐: 取到 {len(items)} 条")
        out = []
        for it in items:
            info = _profile_work_to_info(it)
            if info is not None:
                out.append(info)
            if len(out) >= limit:
                break
        return out

    def search_keyword(self, keyword: str, *, limit: int = KEYWORD_WORKS_PAGE,
                       offset: int = 0):
        """P4a · 关键字/标签/作者名搜索。

        P4b+ 重写: ajax/search/artworks 主通道 —— 一次 0.5s 返回 60 条结构化数据
        (标题/作者/页数/缩略图全齐)。旧 DOM 扫描只等第 1 个锚点出现就开爬
        (SPA 约 2s 才渲染满) → 用户实测「搜索只有 1 条结果」; 且卡片作者
        链接在锚点外, styled-components 类名无法定位 → 作者全是空。

        s_mode=s_tag (标签完全匹配) 无结果时自动降级 s_tc (部分匹配)。
        DOM 扫描保留为最后兜底 (_search_keyword_dom)。

        offset : 以前 URL 里写死 `p=1` → 同一个词
        每次搜到的永远是同一批固定结果。现在按 offset 连续切片, 跨 pixiv 分页
        (每页 60 条) 自动续取, 不重复也不跳过 (见 _search_raw)。

        统一返回:
            {'type': 'keyword',
             'keyword': str,
             'related_authors': list[AuthorInfo] (最多 3 位去重作者),
             'works': list[ArtworkInfo]}
        """
        if not keyword or not keyword.strip():
            return {'type': 'keyword', 'keyword': '',
                    'related_authors': [], 'works': []}
        if not getattr(self, 'login_ok', False):
            self.reporter.log("ℹ️ 未登录: 搜索结果不含 R-18/部分受限内容,"
                              " 登录后更全 (点右上角账户标签登录)")

        works: list[ArtworkInfo] = []
        author_map: dict[str, AuthorInfo] = {}
        for s_mode in ('s_tag', 's_tc'):
            try:
                data = self._search_raw(keyword, s_mode, offset=offset, limit=limit)
            except Exception as e:
                self.reporter.warn(f"ajax 搜索接口失败, 走 DOM 兜底: {str(e).splitlines()[0]}")
                if not works:
                    return self._search_keyword_dom(keyword, limit=limit, offset=offset)
                break
            for it in data:
                aid = str(it.get("id") or '')
                if not aid:
                    continue
                aw = ArtworkInfo(
                    artwork_id=aid,
                    author_id=str(it.get("userId") or ''),
                    title=(it.get("title") or '').strip() or f"#{aid}",
                    page_count=max(1, int(it.get("pageCount") or 1)),
                    image_urls=[it.get("url")] if it.get("url") else [],
                    is_r18=int(it.get("xRestrict") or 0) >= 1,
                    # aiType (2026-09-20): 关键字结果也要带 AI 判定, 否则查询页
                    # 「隐藏 AI 作品」对关键字搜索完全失效 (全是未知=0)
                    ai_type=int(it.get("aiType") or 0),
                )
                works.append(aw)
                uid = aw.author_id
                if uid and uid not in author_map:
                    author_map[uid] = AuthorInfo(
                        author_id=uid,
                        name=(it.get("userName") or '').strip() or f"作者 {uid}",
                        pixiv_url=f"https://www.pixiv.net/users/{uid}",
                    )
                if len(works) >= limit:
                    break
            if works:
                break  # s_tag 已有结果就不必再跑 s_tc

        if not works:
            # ajax 正常但 0 结果 → DOM 兜底再确认一次 (防接口字段变更)
            dom_res = self._search_keyword_dom(keyword, limit=limit, offset=offset)
            if dom_res['works']:
                return dom_res
        return {
            'type': 'keyword',
            'keyword': keyword,
            'related_authors': list(author_map.values())[:3],
            'works': works,
        }

    def search_authors(self, keyword: str, *, limit: int = 30) -> list[AuthorInfo]:
        """P4a · 按作者名/Id 搜作者。

        实测 (2026-09-02): pixiv 已下架 ajax/search/users/* 独立作者搜索接口
        (路径版 404), 现代作者搜索页在 /search/users?nick=xxx。
        核心难点: 页面 DOM 里 /users/{id} 链接混有导航/推荐关注等噪声, 且锚点内
        often 不含作者名 → 直接扫 a.text 得到一堆「作者 {id}」或无关作者。

        修正策略 (2026-09-03):
          ① 任选一个作者搜索页 (先 /search/users, 老 search_user.php 兜底);
          ② 收集页面上所有 /users/{id} 候选 id (宁多勿漏, 靠名字过滤兜底);
          ③ 逐个用 ajax/user/{id} 取「真实名字+头像」(该接口稳定, 也是全站通用);
          ④ 只保留作者名与关键字匹配的 → 噪声/导航/推荐全部被滤掉,
            名字也拿正确, 不再出现「作者 12345」占位。
        优先 ajax 试探 → 失败再 DOM 兜底, 与 _search_keyword_dom 同一套思路。
        """
        if not keyword or not keyword.strip():
            return []
        if not getattr(self, 'login_ok', False):
            self.reporter.log("ℹ️ 未登录: 作者搜索结果可能不全, 登录后更全")
        kw = keyword.strip()
        self.reporter.log(f"[查询] 作者搜索: {kw}")
        # ① ajax 主通道: GET /ajax/search/users?nick=<kw>  (2026-09-18 修正)
        #    老代码写的是 /ajax/search/users/<kw> —— 那个**路径版 404** 是 pixiv 早就下架的,
        #    于是一直落到 DOM 爬页面那条慢路: 2 次页面加载 + 最长 4.8s 等 SPA 渲染收敛
        #    + 逐个 ajax 补名字, 关键词作者搜索实测 2.5s+ (作品搜索只要 0.3s)。
        #    改成查询参数版后一次拿到全部 (实测 ~0.2s), 也不用再逐个人查。
        t0 = time.time()
        try:
            authors, ok = self._search_users_ajax(kw, limit=limit)
            if ok:
                self.reporter.log(
                    f"[查询] 作者搜索(ajax) 命中 {len(authors)} 位 "
                    f"({(time.time() - t0) * 1000:.0f}ms)")
                return authors          # ok=True: 空列表也是"确实没搜到", 不必再爬页面
        except _AjaxNotFound:
            pass
        except Exception as e:
            self.reporter.warn(f"作者搜索 ajax 失败, 走 DOM 兜底: {str(e).splitlines()[0]}")
        # ② DOM 兜底: 现代页优先, 老页兜底 (只在 ajax 拿不到结构时才会走到)
        return self._search_authors_dom(kw, limit=limit)

    @staticmethod
    def _author_name_matches(kw: str, name: str) -> bool:
        """作者名与关键字是否匹配(大小写不敏感, 双向包含)。空名不匹配。"""
        if not name:
            return False
        a = kw.lower()
        b = name.lower()
        return a in b or b in a

    def _parse_search_users_ajax(self, body, kw: str, *, limit: int):
        """解析作者搜索 ajax 结果, 只保留作者名与关键字匹配的。

        2026-09-18 实测的现代形状 (一次性带齐名字/头像/简介/关注态):
            {"data": [], "page": {"userIds": [...]},
             "users": [{"userId": "102593990", "name": "示例画师",
                        "image": "...", "comment": "...", "isFollowed": true}, ...]}
        老形状 (裸数组 / {"thumbnails":{"data":[...]}} / {"body":{...}}) 也照旧能吃,
        免得 pixiv 换个壳就整条作者搜索挂掉。"""
        root = body
        if isinstance(root, dict) and isinstance(root.get("body"), dict):
            root = root["body"]          # 兼容老外套
        users: list = []
        if isinstance(root, dict):
            for key in ("users", "data"):
                v = root.get(key)
                if isinstance(v, list):
                    users += [x for x in v if isinstance(x, dict)]
                elif isinstance(v, dict) and key == "users":
                    users += [x for x in v.values() if isinstance(x, dict)]
            if not users:
                th = root.get("thumbnails") or {}
                if isinstance(th, dict) and isinstance(th.get("data"), list):
                    users = [x for x in th["data"] if isinstance(x, dict)]
        elif isinstance(root, list):
            users = [x for x in root if isinstance(x, dict)]

        authors: list[AuthorInfo] = []
        seen: set[str] = set()
        for it in users:
            uid = str(it.get("userId") or it.get("id") or '')
            if not uid or uid in seen:
                continue
            name = str(it.get("name") or it.get("userName") or '').strip()
            if not self._author_name_matches(kw, name):
                continue  # 名字不匹配的丢掉, 防接口退化返回推荐/无关
            seen.add(uid)
            authors.append(AuthorInfo(
                author_id=uid,
                name=name or f"作者 {uid}",
                avatar_url=it.get("image") or it.get("imageBig") or None,
                bio=str(it.get("comment") or '').strip() or None,
                following=int(it.get("following") or 0),
                pixiv_url=f"https://www.pixiv.net/users/{uid}",
            ))
            if len(authors) >= limit:
                break
        return authors

    def _search_users_ajax(self, keyword: str, *, limit: int = 30):
        """作者搜索 ajax 主通道 → (作者列表, 接口是否可用)。

        2026-09-18 定案: 端点必须是 **`/ajax/search/users?nick=<kw>`**（查询参数），
        写成 `/ajax/search/users/<kw>` 会 404 —— 老代码就是按路径版写的, 于是每次都掉进
        DOM 那条慢路（2 次页面加载 + 最长 4.8s 等渲染 + 逐个 ajax 补名字, 实测 2.5s+,
        而作品搜索只要 0.3s）。
        这个接口一次就把 userIds + 名字 + 头像 + 简介 + 是否已关注全给了 (HTTP 直连 ~0.2s)。
        返回 (authors, ok): ok=False 表示"接口没给可用结构" → 调用方走 DOM 兜底;
        ok=True 但列表为空 = 确实没搜到, 不必再爬页面。
        """
        encoded = urllib.parse.quote(keyword.strip(), safe='')
        body = self._ajax_get(f"{_AJAX_BASE}/search/users?nick={encoded}&blt=0&lang=zh")
        authors = self._parse_search_users_ajax(body, keyword, limit=limit)
        if authors:
            return authors, True
        # users 为空但给了 userIds (pixiv 偶尔不塞 users 明细) → 用稳定的 ajax/user 逐个补名字
        root = body if isinstance(body, dict) else {}
        ids = (((root.get("page") or {}).get("userIds")) or []) if isinstance(root, dict) else []
        if not ids:
            return [], isinstance(root, dict) and ("page" in root or "users" in root)
        for uid in ids[:max(limit * 2, 10)]:
            if len(authors) >= limit:
                break
            try:
                prof = self._ajax_get(f"{_AJAX_BASE}/user/{uid}?full=1&lang=zh") or {}
            except Exception:
                continue
            nm = str(prof.get("name") or '').strip()
            if not self._author_name_matches(keyword, nm):
                continue
            authors.append(AuthorInfo(
                author_id=str(uid), name=nm or f"作者 {uid}",
                avatar_url=prof.get("image") or None,
                following=int(prof.get("following") or 0),
                pixiv_url=f"https://www.pixiv.net/users/{uid}",
            ))
        return authors, True

    def _search_authors_dom(self, keyword: str, *, limit: int = 30) -> list[AuthorInfo]:
        """作者搜索页 DOM 收集候选 + ajax/user 补全名字 + 关键字过滤。

        自 2026-09 起 pixiv 作者搜索在 /search/users?nick= 页面, 老
        search_user.php 仍可用(做第二候选)。两者初始 DOM 都可能混入大量
        /users/ 导航链接, 因此在页面里取「所有」候选 id, 再由 ajax/user
        补全名字后按关键字过滤 → 只返回真正匹配的作者。
        """
        from selenium.webdriver.common.by import By
        encoded = urllib.parse.quote(keyword, safe='')
        urls = [
            f"https://www.pixiv.net/search/users?nick={encoded}",
            f"https://www.pixiv.net/search_user.php?nick={encoded}",
        ]
        candidate_ids: list[str] = []
        # anchor 文本/ title 里直接给的作者名 (结果卡通常自带名字, 不用再逐个 ajax)
        direct_names: dict[str, str] = {}
        seen_ids: set[str] = set()
        for url in urls:
            self.reporter.log(f"[查询] 作者搜索(DOM): {keyword} → {url}")
            try:
                self.driver.set_page_load_timeout(15)
            except Exception:
                pass
            try:
                self.driver.get(url)
            except Exception as e:
                self.reporter.warn(f"作者搜索页打开失败: {str(e).splitlines()[0]}")
                continue
            # 等结果锚点渲染收敛 (SPA 异步填充); Selenium 异常则用当前已有锚点
            anchors: list = []
            try:
                from selenium.common.exceptions import WebDriverException
                last = -1
                for _ in range(6):
                    try:
                        anchors = self.driver.find_elements(
                            By.XPATH, "//a[contains(@href,'/users/')]")
                    except WebDriverException:
                        break
                    if len(anchors) == last and len(anchors) > 0:
                        break
                    last = len(anchors)
                    time.sleep(0.8)
            except Exception:
                pass
            for a in anchors:
                try:
                    href = (a.get_attribute("href") or '').strip()
                    m = _RE_USER_URL.search(href)
                    if not m:
                        continue
                    uid = m.group(1)
                    if not uid:
                        continue
                    # 结果卡 a 链接的文本/title 常直接就是作者名
                    txt = (a.get_attribute("title") or '').strip() or (a.text or '').strip()
                    name = txt.rstrip('/') if txt else ''
                    if uid in seen_ids:
                        if self._author_name_matches(keyword, name) and name:
                            direct_names[uid] = name
                        continue
                    seen_ids.add(uid)
                    candidate_ids.append(uid)
                    if name and self._author_name_matches(keyword, name):
                        direct_names[uid] = name
                except Exception:
                    continue
            # 已拿到候选就停, 不必再开第二个页面
            if candidate_ids:
                break

        if not candidate_ids:
            self.reporter.warn(f"作者搜索未在页面找到任何候选用户: {keyword}")
            return []

        # ③ 组装结果: 名字在 anchor 里直接给了的 → 直接出(快, 免 ajax);
        #    名字缺/不匹配的 → 用稳定 ajax/user 补全后再过滤
        authors: list[AuthorInfo] = []
        matched_ids: set[str] = set()
        for uid in candidate_ids:
            if len(authors) >= limit:
                break
            name = direct_names.get(uid)
            info = None
            if name:
                # 名字已命中关键字, 头像/following 顺带补一下(单个用户, 成本低)
                try:
                    info = self.fetch_author_info_only(uid)
                except Exception:
                    pass
                if info is not None:
                    name = info.name or name
            else:
                try:
                    info = self.fetch_author_info_only(uid)
                except Exception:
                    continue
                if info is None or not self._author_name_matches(keyword, info.name):
                    continue  # 名字与关键字不匹配(导航/推荐/无关作者) → 丢弃
                name = info.name
            if not name or not self._author_name_matches(keyword, name):
                continue
            if uid in matched_ids:
                continue
            matched_ids.add(uid)
            authors.append(AuthorInfo(
                author_id=uid,
                name=name[:60],
                avatar_url=info.avatar_url if info else None,
                following=info.following if info else 0,
                pixiv_url=f"https://www.pixiv.net/users/{uid}",
            ))
        if not authors:
            self.reporter.warn(
                f"[查询] 作者搜索: 页面有候选但作者名均与「{keyword}」不匹配, "
                f"已过滤为 0 位 (避免返回无关作者)")
        else:
            self.reporter.log(f"[查询] 作者搜索(DOM+过滤) 命中 {len(authors)} 位")
        return authors

    def _search_keyword_dom(self, keyword: str, *, limit: int = KEYWORD_WORKS_PAGE,
                            offset: int = 0):
        """旧 DOM 扫描 (ajax 全挂时的最后兜底)。

        offset: 兜底路径也认「换一批」—— 就地转成 pixiv 的页码 `p`, 否则
        在 ajax 挂掉时点「换一批」会一直拿到同一页 (页内偏移没法用 DOM 表达)。
        """
        encoded = urllib.parse.quote(keyword)
        p = max(0, int(offset or 0)) // _PIXIV_SEARCH_PER_PAGE + 1
        # Pixiv 标签/关键字搜索主 URL (s_mode=s_tag 是含标签完全匹配的搜索, 结果最准)
        url = f"https://www.pixiv.net/search.php?s_mode=s_tag&word={encoded}&p={p}"
        self.reporter.log(f"[查询] 关键字搜索(DOM): {keyword} → {url}")
        self.driver.get(url)
        # P4b+ 修复「只出 1 条」: 不能等第 1 个锚点出现就开爬 ——
        # SPA 约 2s 才把结果网格渲染满, 这里轮询到锚点数收敛(连续两次相同)再扫。
        last = -1
        for _ in range(10):
            n = len(self.driver.find_elements(By.XPATH, "//a[contains(@href,'/artworks/')]"))
            if n == last and n > 1:
                break
            last = n
            time.sleep(1.0)

        works: list[ArtworkInfo] = []
        seen_ids: set[str] = set()
        author_map: dict[str, AuthorInfo] = {}  # 去重 + 收 top3 相关作者

        # 通用 DOM 兜底: 扫所有 artwork 链接的 a 标签
        try:
            anchors = self.driver.find_elements(By.XPATH,
                "//a[contains(@href,'/artworks/')]")
            for a in anchors:
                href = a.get_attribute("href") or ''
                m = re.search(r'/artworks/(\d+)', href)
                if not m:
                    continue
                aid = m.group(1)
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)

                # 缩略图
                img_src = ''
                try:
                    img = a.find_element(By.TAG_NAME, "img")
                    img_src = img.get_attribute("src") or ''
                except Exception:
                    pass

                title = (a.get_attribute("title") or '').strip()

                # 作者信息: 尝试从卡片上下文拿作者 a 链接
                author_id = ''
                author_name = ''
                try:
                    # 在卡片父级范围找 /users/ 的作者链接
                    parent = a.find_element(By.XPATH, "./ancestor::*[contains(@class,'illust') or contains(@class,'item') or contains(@class,'card')][1]")
                    auth_el = parent.find_element(By.XPATH,
                        ".//a[contains(@href,'/users/') and not(contains(@href,'/artworks/'))]")
                    ahref = auth_el.get_attribute("href") or ''
                    am = re.search(r'/users/(\d+)', ahref)
                    if am:
                        author_id = am.group(1)
                        author_name = (auth_el.text or '').strip() or f"作者 {author_id}"
                except Exception:
                    # fallback: 从卡片 aria-label / data 属性试试, 拿不到就留空
                    pass

                # 构造 ArtworkInfo
                aw = ArtworkInfo(
                    artwork_id=aid,
                    author_id=author_id,
                    title=title or f"#{aid}",
                    page_count=1,
                    image_urls=[img_src] if img_src else [],
                )
                works.append(aw)

                # 收集相关作者 (去重, 最多 3 位)
                if author_id and author_id not in author_map:
                    author_map[author_id] = AuthorInfo(
                        author_id=author_id,
                        name=author_name or f"作者 {author_id}",
                        pixiv_url=f"https://www.pixiv.net/users/{author_id}",
                    )

                if len(works) >= limit:
                    break
        except Exception as e:
            self.reporter.warn(f"解析关键字搜索结果出错:{str(e).splitlines()[0]}")

        related_authors = list(author_map.values())[:3]
        return {
            'type': 'keyword',
            'keyword': keyword,
            'related_authors': related_authors,
            'works': works,
        }

    def fetch_artwork(self, artwork_id: str, *, allow_dom_fallback: bool = True) -> ArtworkInfo:
        """P4b+: ajax/illust/{id} 主通道 (0.4s, 字段全且稳), DOM 爬取只做兜底。
        allow_dom_fallback=False: ajax 失败直接抛错 (批量自检场景, DOM 兜底太慢)。"""
        # 与 execute/fetch_logged_in_user 串行, 避免多线程同时驱动同一浏览器
        with self.op_lock:
            return self._fetch_artwork_locked(artwork_id, allow_dom_fallback)

    def _fetch_artwork_locked(self, artwork_id: str, allow_dom_fallback: bool) -> ArtworkInfo:
        url = f"{_AJAX_BASE}/illust/{artwork_id}?lang=zh"
        self.reporter.log(f"[查询] 作品详情 (ajax) {artwork_id}")
        try:
            body = self._ajax_get(url)
        except _AjaxNotFound:
            raise ArtworkNotFoundError(f"作品 {artwork_id} 不存在或被删除")
        except SearchError:
            raise
        except Exception as e:
            # ajax 通道整体失败 (网络/会话损坏) → DOM 兜底
            if not allow_dom_fallback:
                raise
            self.reporter.warn(f"ajax 作品接口失败, 走 DOM 兜底: {str(e).splitlines()[0]}")
            return self._fetch_artwork_dom(artwork_id)
        info = self._illust_body_to_info(body, default_author_id='')
        if info is None:
            raise ArtworkNotFoundError(f"作品 {artwork_id} 返回数据为空")
        return info

    @staticmethod
    def _illust_body_to_info(b: dict, default_author_id: str = '') -> Optional[ArtworkInfo]:
        """ajax/illust body → ArtworkInfo (纯映射, 无浏览器依赖, 冒烟可直接测)。"""
        if not isinstance(b, dict) or not b.get("illustId"):
            return None
        tags = [t.get("tag", "") for t in (b.get("tags") or {}).get("tags", [])]
        tags = [t for t in tags if t]
        itype = {0: IllustType.ILLUSTRATION,
                 1: IllustType.MANGA,
                 2: IllustType.UGOIRA}.get(int(b.get("illustType") or 0),
                                           IllustType.ILLUSTRATION)
        thumb = (b.get("urls") or {}).get("thumb") or ''
        x_restrict = int(b.get("xRestrict") or 0)
        # aiType: pixiv 官方 AI 判定 (1=非AI, 2=AI); 取不到就是 0=未知
        try:
            ai_type = int(b.get("aiType") or 0)
        except (TypeError, ValueError):
            ai_type = 0
        return ArtworkInfo(
            artwork_id=str(b["illustId"]),
            author_id=str(b.get("userId") or default_author_id or ''),
            title=(b.get("illustTitle") or '').strip() or f"#{b['illustId']}",
            illust_type=itype,
            page_count=max(1, int(b.get("pageCount") or 1)),
            tags=tags,
            image_urls=[thumb] if thumb else [],
            publish_date=b.get("uploadDate") or None,
            is_r18=x_restrict >= 1 or 'R-18' in ' '.join(tags),
            ai_type=ai_type,
            author_name=(b.get("userName") or '').strip() or None,
            bookmark_count=int(b.get("bookmarkCount") or b.get("totalBookmarks") or 0),
        )

    def _fetch_artwork_dom(self, artwork_id: str) -> ArtworkInfo:
        """旧 DOM 爬取 (ajax 不可用时的兜底)。"""
        url = f"https://www.pixiv.net/artworks/{artwork_id}"
        self.reporter.log(f"[查询] 打开作品页 {url}")
        self.driver.get(url)
        time.sleep(0.5)
        try:
            self.wait.until(EC.presence_of_element_located(
                (By.XPATH, self.xp["pages_count"] or "//h1")))
        except Exception:
            pass
        # 是否 404
        page_src = self.driver.page_source
        if any(k in page_src for k in ["此页面不存在", "尚无此页", "コンテンツが見つかりません",
                                        "No content found"]):
            raise ArtworkNotFoundError(f"作品 {artwork_id} 不存在或被删除")

        title = self._try_get_text(self.xp.get("artwork_title", "//h1")) or f"作品 {artwork_id}"
        try:
            author_name = self.driver.find_element(By.XPATH, self.xp["author_name"]).text.strip()
        except Exception:
            author_name = "未知作者"
        try:
            auth_a = self.driver.find_element(By.XPATH, self.xp["author_id"])
            author_link = auth_a.get_attribute("href") or ""
            m = re.search(r'/users/(\d+)', author_link)
            author_id = m.group(1) if m else ''
        except Exception:
            author_id = ''
        # 页数
        pages = 1
        try:
            pc = self.driver.find_element(By.XPATH, self.xp["pages_count"])
            txt = pc.text or ''
            m2 = re.search(r'/(\d+)', txt)
            if m2: pages = int(m2.group(1))
        except Exception:
            pass
        # 类型
        itype = IllustType.MANGA if pages > 1 else IllustType.ILLUSTRATION
        # 缩略图(第一张主图): 用 og:image
        thumb = self._og_image() or ''
        # 标签
        tags = self._try_tags()
        return ArtworkInfo(
            artwork_id=str(artwork_id),
            author_id=author_id,
            title=title,
            illust_type=itype,
            page_count=pages,
            tags=tags,
            image_urls=[thumb] if thumb else [],
            is_r18=('R-18' in ' '.join(tags)),
            author_name=author_name if author_name != "未知作者" else None,
        )

    def fetch_author_info_only(self, author_id: str) -> Optional[AuthorInfo]:
        """只查作者头像/名字(轻量), 不进作品页。失败返回 None。

        P4b+: ajax/user/{id} 一次 0.4s 拿到 name+avatar, 不再走作者主页 DOM。"""
        try:
            prof = self._ajax_get(f"{_AJAX_BASE}/user/{author_id}?full=1&lang=zh")
        except Exception:
            prof = None
        if isinstance(prof, dict) and prof.get("name"):
            return AuthorInfo(
                author_id=str(author_id),
                name=str(prof["name"]),
                avatar_url=prof.get("image") or None,
                following=int(prof.get("following") or 0),
                pixiv_url=f"https://www.pixiv.net/users/{author_id}",
            )
        # 兜底: 走完整 fetch_author (ajax 作品列表/DOM)
        try:
            a, _ = self.fetch_author(author_id, limit=0)
            return a
        except Exception:
            return AuthorInfo(author_id=str(author_id), name="作者 " + author_id)

    def fetch_author(self, author_id: str, *, limit: int = 60, offset: int = 0
                     ) -> tuple[AuthorInfo, list[ArtworkInfo]]:
        """作者信息 + 作品列表 (默认最新 60 个)。

        P4b+: ajax 主通道 —— 旧 DOM 版的作者名/头像 xpath 是
        __next/div/div[2]/div[5]/... 绝对路径, pixiv 改版即失效
        (用户实测 author 爬成空)。DOM 版保留为兜底。
        offset (2026-09-18): 「换一批」用 —— pixiv 的 profile/all 一次返回全部作品 ID,
        所以翻页只是换个切片, 不用重新分页请求。命中总数的字段见 works_total。
        """
        try:
            return self._fetch_author_ajax(author_id, limit=limit, offset=offset)
        except _AjaxNotFound:
            raise AuthorNotFoundError(f"作者 {author_id} 不存在/主页不可见")
        except Exception as e:
            self.reporter.warn(f"ajax 作者接口失败, 走 DOM 兜底: {str(e).splitlines()[0]}")
            return self._fetch_author_dom(author_id, limit=limit, offset=offset)

    def _ajax_get(self, url: str, *, unwrap: bool = True):
        """P4b+ 核心: 取 pixiv ajax JSON。unwrap=True 返回 `body` 字段 (ajax 接口的标准壳)。

        unwrap=False (2026-09-20 加): 排行榜走的是**老接口** `ranking.php?format=json`,
        它没有 `{error, body}` 这层壳, 数据直接挂顶层 (`contents`)。硬套 unwrap
        会拿到 None。其余行为 (HTTP 直连优先 / 浏览器兜底 / 404 识别) 两者共用。

        双通道:
        - HTTP 直连优先 (cookies 直接打 ajax, ~0.3~0.5s, 免浏览器导航 ——
          第三方下载器/图库工具同款做法; 元数据回填一直在用, 已证实稳定)。
        - 失败/无 cookies → Selenium driver.get 兜底 (浏览器内自带请求头)。
        - 页面/解析失败           → SearchError (上层走 DOM 兜底)
        - error=true 且消息像 404  → _AjaxNotFound (上层转 Not Found)
        - 其它 error=true          → SearchError(带可读 message)
        """
        sess = self._http_session()
        if sess:
            try:
                from urllib.request import Request, urlopen
                from urllib.error import HTTPError
                try:
                    with urlopen(Request(url, headers=sess["headers"]), timeout=15) as resp:
                        txt = resp.read().decode("utf-8", "replace")
                except HTTPError as e:
                    if e.code in (401, 403):
                        # cookies 已失效 → 停用 HTTP, 后续直接走浏览器 (重新登录后 mtime 变化会自动重建)
                        self._http = False
                        raise SearchError(f"pixiv 登录态失效 (HTTP {e.code})")
                    raise SearchError(f"pixiv HTTP {e.code}")
                data = json.loads(txt)
                if isinstance(data, dict) and data.get("error"):
                    msg = str(data.get("message") or "pixiv 返回错误")
                    if any(k in msg for k in ("无法找到", "找不到", "不存在", "离开了",
                                              "見つかり", "not found")):
                        raise _AjaxNotFound(msg)
                    raise SearchError(f"pixiv 接口错误: {msg}")
                if not unwrap:
                    return data
                return data.get("body") if isinstance(data, dict) else data
            except (_AjaxNotFound, SearchError):
                raise
            except Exception:
                pass   # 网络/解析类临时错误 → 本次回退浏览器, 会话保留下次重试
        # ---- 浏览器兜底 (原逻辑) ----
        try:
            self.driver.get(url)
            txt = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception as e:
            raise SearchError(f"浏览器请求失败: {str(e).splitlines()[0]}")
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            raise SearchError("ajax 返回的不是 JSON (可能被重定向到登录/错误页)")
        if isinstance(data, dict) and data.get("error"):
            msg = str(data.get("message") or "pixiv 返回错误")
            if any(k in msg for k in ("无法找到", "找不到", "不存在", "离开了",
                                      "見つかり", "not found")):
                raise _AjaxNotFound(msg)
            raise SearchError(f"pixiv 接口错误: {msg}")
        if not unwrap:
            return data
        return data.get("body") if isinstance(data, dict) else data

    def _fetch_author_ajax(self, author_id: str, *, limit: int,
                           offset: int = 0
                           ) -> tuple[AuthorInfo, list[ArtworkInfo]]:
        """ajax 主通道: user?full=1 (名字/头像) + profile/all (作品 id 列表)
        + profile/illusts?ids[]= 批量取最新作品完整元数据 (1 次请求替代逐 id 逐个查,
        作者 60 作品从 ~62 次串行 HTTP 降到 3 次)。批量接口失败自动回退逐个 ajax/illust。
        offset: 从最新往旧数, 跳过前 offset 个 («换一批» 用同一次 profile/all 的切片)。
        """
        prof = self._ajax_get(f"{_AJAX_BASE}/user/{author_id}?full=1&lang=zh")
        author = AuthorInfo(
            author_id=str(author_id),
            name=str(prof.get("name") or f"作者 {author_id}"),
            avatar_url=prof.get("image") or None,
            following=int(prof.get("following") or 0),
            pixiv_url=f"https://www.pixiv.net/users/{author_id}",
        )
        works: list[ArtworkInfo] = []
        if limit > 0:
            all_body = self._ajax_get(
                f"{_AJAX_BASE}/user/{author_id}/profile/all?lang=zh") or {}
            ids = list(all_body.get("illusts") or {}) + list(all_body.get("manga") or {})
            # id 数值降序 = 新作品在前 (profile/all 的 JSON 键序不保证)
            ids.sort(key=lambda s: int(s), reverse=True)
            author.works_total = len(ids)          # 换一批要用总数算页数
            ids = ids[max(0, int(offset)):max(0, int(offset)) + limit]
            works = self._batch_illusts(author_id, ids)
            if not works:
                for aid in ids:   # 回退: 逐个查
                    try:
                        b2 = self._ajax_get(f"{_AJAX_BASE}/illust/{aid}?lang=zh")
                    except _AjaxNotFound:
                        continue  # 个别作品被删/不可见, 跳过不炸整个列表
                    info = self._illust_body_to_info(b2, default_author_id=author_id)
                    if info is not None:
                        works.append(info)
        return author, works

    def _batch_illusts(self, author_id: str, ids: "list[str]") -> "list[ArtworkInfo]":
        """profile/illusts?ids[]= 批量取作品元数据 (1 请求 ≈ 50 个, 按传入顺序返回)。
        成功返回作品列表; 失败/空返回空列表 (调用方回退逐个查询)。"""
        if not ids:
            return []
        out: "list[ArtworkInfo]" = []
        try:
            for i in range(0, len(ids), 50):
                chunk = ids[i:i + 50]
                qs = "&".join(f"ids[]={urllib.parse.quote(str(a))}" for a in chunk)
                body = self._ajax_get(
                    f"{_AJAX_BASE}/user/{author_id}/profile/illusts?"
                    f"lang=zh&work_category=illustManga&is_first_page=1&sortBy=date_d&{qs}")
                ws = (body or {}).get("works") or {}
                for aid in chunk:
                    # 映射统一走模块级 _profile_work_to_info (与 HTTP 直连版本共用一份)
                    info = _profile_work_to_info(ws.get(str(aid)) or {}, author_id)
                    if info is not None:
                        out.append(info)
        except Exception as e:
            self.reporter.warn(f"批量作品接口失败, 回退逐个查询: {str(e).splitlines()[0]}")
            return []
        return out

    def _fetch_author_dom(self, author_id: str, *, limit: int, offset: int = 0
                          ) -> tuple[AuthorInfo, list[ArtworkInfo]]:
        """旧 DOM 版作者查询 (ajax 不可用时的兜底)。

        翻页: 作者主页首屏只渲染一页作品, DOM 拿不到总数 → 抓 offset+limit 再切片,
        works_total 留空 (前端据此隐藏页数, 只保留「下一批」)。"""
        url = f"https://www.pixiv.net/users/{author_id}/illustrations"
        self.reporter.log(f"[查询] 打开作者主页 {url}")
        self.driver.get(url)
        try:
            self.wait.until(EC.presence_of_element_located(
                (By.XPATH, self.xp["author_name"] or "//h1")))
        except Exception:
            pass
        page_src = self.driver.page_source
        if any(k in page_src for k in ["此页面不存在", "ユーザーが存在しません",
                                        "No users found"]):
            raise AuthorNotFoundError(f"作者 {author_id} 不存在/主页不可见")
        try:
            name = self.driver.find_element(By.XPATH, self.xp["author_name"]).text.strip()
        except Exception:
            name = f"作者 {author_id}"
        avatar = self._try_get_attr(
            self.xp.get("author_avatar", "//img[@class*='avatar' or contains(@alt,'profile')]"),
            "src")
        bio = self._try_get_text(self.xp.get("author_bio",
            "//div[contains(@class,'bio') or @data-gtm-user-intro]"))
        author = AuthorInfo(
            author_id=str(author_id),
            name=name,
            avatar_url=avatar,
            bio=bio,
            pixiv_url=f"https://www.pixiv.net/users/{author_id}",
        )
        works: list[ArtworkInfo] = []
        if limit > 0:
            off = max(0, int(offset))
            works = self._scrape_author_works(author_id, limit=off + limit)[off:]
        return author, works

    # ---------- internal ----------
    def close(self):
        if self._closed:
            return
        # P4a+: driver.quit() 在页面加载中/会话损坏时可能无限阻塞 —— 
        # 最多等 op_lock 5s, 拿不到也照样 quit (调用方 _on_close 有整体兜底超时)
        acquired = False
        try:
            acquired = self.op_lock.acquire(timeout=5)
        except Exception:
            pass
        if self._driver is not None:   # 惰性浏览器: 从未创建则无需 quit
            try:
                self.driver.quit()
            except Exception:
                pass
        self._closed = True
        if acquired:
            try:
                self.op_lock.release()
            except Exception:
                pass

    def _try_get_text(self, xpath: str) -> Optional[str]:
        if not xpath: return None
        try:
            el = self.driver.find_element(By.XPATH, xpath)
            return (el.text or '').strip()
        except Exception:
            return None

    def _try_get_attr(self, xpath: str, attr: str) -> Optional[str]:
        if not xpath: return None
        try:
            el = self.driver.find_element(By.XPATH, xpath)
            return el.get_attribute(attr) or None
        except Exception:
            return None

    def _og_image(self) -> Optional[str]:
        try:
            el = self.driver.find_element(By.XPATH,
                "//meta[@property='og:image']")
            return el.get_attribute("content") or None
        except Exception:
            return None

    def _try_tags(self) -> list[str]:
        tags = []
        # XPath 兜底: 扫含 tag 的 <a> 文字
        try:
            xps = self.xp.get("artwork_tags")
            if xps:
                els = self.driver.find_elements(By.XPATH, xps)
                for e in els:
                    t = (e.text or '').strip()
                    if t and 0 < len(t) < 40 and len(tags) < 30:
                        tags.append(t)
            if not tags:
                # generic fallback: 任何带"tags"类名的元素里的 a
                els = self.driver.find_elements(By.CSS_SELECTOR,
                    "a[href*='/tags/']")
                for e in els:
                    t = (e.text or '').strip()
                    if t and 0 < len(t) < 40 and t not in tags and len(tags) < 30:
                        tags.append(t)
        except Exception:
            pass
        return tags

    def _scrape_author_works(self, author_id: str, *, limit: int
                             ) -> list[ArtworkInfo]:
        """从作者插画主页抓最近 limit 个作品的 id/标题/缩略/页数。"""
        works: list[ArtworkInfo] = []
        # 抓主页上所有 artwork 卡片链接
        # 优先 JSON bootstrap (Pixiv 常把作品列表放在 window.__INITIAL_STATE__ 里),
        # 这里做一个 DOM 通用兜底: 找 href="/artworks/NNN" 的 a
        try:
            anchors = self.driver.find_elements(By.XPATH,
                "//a[contains(@href,'/artworks/')]")
            seen: set[str] = set()
            for a in anchors:
                href = a.get_attribute("href") or ''
                m = re.search(r'/artworks/(\d+)', href)
                if not m: continue
                aid = m.group(1)
                if aid in seen: continue
                seen.add(aid)
                # 取 img 缩略
                img_src = ''
                try:
                    img = a.find_element(By.TAG_NAME, "img")
                    img_src = img.get_attribute("src") or ''
                except Exception:
                    pass
                title = (a.get_attribute("title") or '').strip()
                works.append(ArtworkInfo(
                    artwork_id=aid, author_id=author_id,
                    title=title or f"#{aid}", page_count=1,
                    image_urls=[img_src] if img_src else [],
                ))
                if len(works) >= limit:
                    break
        except Exception as e:
            self.reporter.warn(f"解析作者作品卡片出错:{str(e).splitlines()[0]}")
        return works


# =============================================================
# Exceptions
# =============================================================
class SearchError(Exception): pass
class ArtworkNotFoundError(SearchError): pass
class AuthorNotFoundError(SearchError): pass
class _AjaxNotFound(SearchError): pass  # ajax 接口 404 类错误 → 上层转具体 NotFound


# =============================================================
# HTTP 直连 (不启浏览器): 用 cookies.bin 里的登录态直接打 /ajax/illust/
# 批量补元数据时比 Selenium 逐页导航快几十倍, 也不吃浏览器内存/CPU。
# 用 stdlib urllib, 不引入第三方依赖。
# =============================================================
def build_pixiv_http_session(cookies: list, user_id: str = ""):
    """cookies (Selenium 格式 list[dict]) → {"headers": {...}} 请求上下文。
    无 pixiv cookie 返回 None。"""
    jar: "dict[str, str]" = {}
    for c in cookies or []:
        dom = str(c.get("domain") or "")
        if "pixiv.net" not in dom:
            continue
        try:
            jar[str(c.get("name"))] = str(c.get("value"))
        except Exception:
            pass
    if not jar:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "Referer": "https://www.pixiv.net/",
        "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
    }
    if user_id:
        headers["x-user-id"] = str(user_id)
    return {"headers": headers}


def http_fetch_user_name(session, user_id: str, timeout: float = 12.0):
    """HTTP 直连查 pixiv 用户最新昵称 (作者改名同步用)。
    返回 None = 用户不存在/已注销; 抛异常 = 网络/登录问题 (调用方自行容错)。"""
    import json as _json
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    req = Request(f"https://www.pixiv.net/ajax/user/{user_id}?lang=zh",
                  headers=session["headers"])
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
    except HTTPError as e:
        if e.code == 404:
            return None
        raise
    if data.get("error"):
        return None
    body = data.get("body") or {}
    name = str(body.get("name") or "").strip()
    return name or None


def _profile_work_to_info(w: dict, default_author_id: str = "") -> "ArtworkInfo | None":
    """作者主页列表 (profile/illusts) 的单条 → ArtworkInfo (纯映射, 无浏览器依赖)。

    字段名与 ajax/illust 不同 (url/title/createDate/tags 简写), 这里做适配后复用
    `_illust_body_to_info` —— 别再各写一份 (2026-09-17 「作者对账台」新增)。"""
    if not isinstance(w, dict) or not w.get("id"):
        return None
    return SearchClient._illust_body_to_info({
        "illustId": w.get("id"),
        "illustTitle": w.get("title") or "",
        "userId": w.get("userId"),
        "userName": w.get("userName"),
        "pageCount": w.get("pageCount"),
        "illustType": w.get("illustType"),
        "urls": {"thumb": w.get("url") or ""},
        "xRestrict": w.get("xRestrict"),
        # aiType (2026-09-20): 作者作品列表 / 作者对账台也带 AI 判定,
        # 否则查询页「隐藏 AI 作品」对作者结果失效
        "aiType": w.get("aiType"),
        "uploadDate": w.get("createDate"),
        "tags": {"tags": [{"tag": t} for t in (w.get("tags") or [])]},
        "bookmarkCount": (w.get("bookmarkData") or {}).get("bookmarkCount"),
    }, default_author_id=default_author_id)


def http_fetch_user_works(session, user_id: str, *, limit: int = 800,
                          timeout: float = 25.0) -> "list[ArtworkInfo]":
    """HTTP 直连列出某作者主页的**全部作品** (不起浏览器)。

    用于「作者对账台」: 把画师 pixiv 主页的作品连缩略图一起拿下来, 跟本地那批
    "来源未知/本地 ID" 的图做人工对账 —— 候选集只有这个作者, 比全站以图搜图快得多。
    两步: profile/all 拿 id 列表 → profile/illusts 每 50 个批量取元数据(含缩略图 URL)。
    个别作品被删/不可见自动跳过; 抛异常 = 网络/登录问题 (调用方容错)。"""
    import json as _json
    from urllib.request import Request, urlopen
    hdr = session["headers"]
    base = "https://www.pixiv.net/ajax"

    def _get(url: str) -> dict:
        with urlopen(Request(url, headers=hdr), timeout=timeout) as r:
            d = _json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(d, dict) and d.get("error"):
            raise RuntimeError(f"ajax 错误: {str(d.get('message') or '')[:80]}")
        return (d or {}).get("body") or {}

    all_body = _get(f"{base}/user/{user_id}/profile/all?lang=zh") or {}
    ids = [str(x) for x in (list((all_body.get("illusts") or {}).keys())
                            + list((all_body.get("manga") or {}).keys()))
           if str(x).isdigit()]
    ids.sort(key=lambda s: int(s), reverse=True)      # 数值降序 = 新作品在前
    ids = ids[:max(1, int(limit))]
    out: "list[ArtworkInfo]" = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        qs = "&".join(f"ids[]={urllib.parse.quote(str(a))}" for a in chunk)
        try:
            b = _get(f"{base}/user/{user_id}/profile/illusts?lang=zh"
                     f"&work_category=illustManga&is_first_page=1&sortBy=date_d&{qs}") or {}
        except Exception:
            continue                                  # 某批失败不影响其余
        works = b.get("works") or {}
        for aid in chunk:
            info = _profile_work_to_info(works.get(str(aid)) or {}, str(user_id))
            if info is not None:
                out.append(info)
    return out


def http_fetch_illust(session, artwork_id: str, *, timeout: float = 15.0) -> ArtworkInfo:
    """urllib 直连 ajax 作品接口 → ArtworkInfo。
    作品不存在/删除 → ArtworkNotFoundError; 登录态失效 → PermissionError。"""
    import json as _json
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    url = f"https://www.pixiv.net/ajax/illust/{artwork_id}?lang=zh"
    try:
        with urlopen(Request(url, headers=session["headers"]), timeout=timeout) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
    except HTTPError as e:
        if e.code in (401, 403):
            raise PermissionError(f"pixiv 登录态失效 (HTTP {e.code})")
        if e.code == 404:
            raise ArtworkNotFoundError(f"作品 {artwork_id} 不存在或被删除")
        raise RuntimeError(f"ajax HTTP {e.code}")
    if data.get("error"):
        msg = str(data.get("message") or "")
        low = msg.lower()
        if "not found" in low or "存在" in msg or "見つかり" in msg:
            raise ArtworkNotFoundError(f"作品 {artwork_id} 不存在或被删除")
        raise RuntimeError(f"ajax 错误: {msg[:80]}")
    info = SearchClient._illust_body_to_info(data.get("body") or {})
    if info is None:
        raise RuntimeError("ajax body 无法解析")
    return info


# =============================================================
# 网页端「写操作」: 收藏(♡ 红心) / 关注 
#
# 「下载后自动收藏该作品（原网页点击红心）」「增加关注该作者的按钮」。
# 契约是从 pixiv 自己的前端 JS 里挖出来并实测打通的 (用**不存在的 ID** 试,
# 对方只会回「找不到/404」, 绝不会误改账号状态):
#   收藏    POST /ajax/illusts/bookmarks/add   JSON {illust_id, restrict:0公开/1非公开,
#                                                   comment:"", tags:[]}
#   取消    GET  /ajax/illust/{id}/bookmarkData → body.bookmarkData.id
#           POST /ajax/illusts/bookmarks/delete 表单 bookmark_id=<id>
#   关注    POST /bookmark_add.php             表单 mode=add&type=user&user_id=<id>
#                                                   &tag=&restrict=0&format=json
#   取关    POST /rpc_group_setting.php        表单 mode=del&type=bookuser&id=<userId>
#   ※ 点赞(いいね)刻意不做: pixiv 官方说明「点赞无法取消、每作品每天仅一次」。
# 写操作必须带 x-csrf-token: 从任意 pixiv 页面的 __NEXT_DATA__ 里
#   props.pageProps.serverSerializedPreloadedState.api.token 取, 用户 ID 在同页
#   props.pageProps.gaUserData.userId。缺 token 时 pixiv 统一回 400「请重新登录」。
# =============================================================
class PixivWebActionError(RuntimeError):
    """网页端写操作失败 (未登录 / 登录态失效 / 目标不存在 / 参数被拒)。"""


_WEB_CRED: dict = {}          # {"key": 登录态指纹, "token": str, "user_id": str, "ts": float}
_WEB_CRED_TTL = 1800.0        # 页面里的 token 会过期, 半小时重取一次
_PIXIV_WEB_BASE = "https://www.pixiv.net"


def _cred_key(session) -> str:
    """按**登录态**（Cookie 串）做缓存键 —— server 每次都是新建的 session dict,
    按对象身份做键会永远不命中, 于是每收藏一张都要多打一次 pixiv 首页。"""
    import hashlib as _h
    ck = str((session.get("headers") or {}).get("Cookie") or "")
    return _h.md5(ck.encode("utf-8")).hexdigest()


def _web_http(session, url: str, *, data: "bytes | None" = None,
              content_type: "str | None" = None, timeout: float = 20.0,
              method: "str | None" = None):
    """网页端请求底层 (单独抽出来, 便于离线测试替换)。返回 (status, text)。
    HTTPError 不抛出, 统一把状态码与响应体交回调用方判断。"""
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    hdr = dict(session["headers"])
    hdr["Accept"] = "application/json, text/html;q=0.9, */*;q=0.8"
    hdr["Referer"] = f"{_PIXIV_WEB_BASE}/"
    hdr["Origin"] = _PIXIV_WEB_BASE
    if content_type:
        hdr["Content-Type"] = content_type
    if method is None:
        method = "POST" if data is not None else "GET"
    req = Request(url, data=data, headers=hdr, method=method)
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        raise PixivWebActionError(f"网络请求失败: {str(e).splitlines()[0]}")


def pixiv_web_credentials(session, *, force: bool = False) -> dict:
    """取网页端写操作需要的 {token, user_id} (带进程内缓存)。

    token 是 pixiv 服务端渲染在页面里的 CSRF 令牌, 随页面下发; 缺它就一律
    400「请重新登录」。未登录 (页面 isLoggedIn=false / 无 token) → 抛错。"""
    import json as _json
    import time as _t
    key = _cred_key(session)
    if ((not force) and _WEB_CRED.get("key") == key and _WEB_CRED.get("token")
            and (_t.time() - float(_WEB_CRED.get("ts") or 0)) < _WEB_CRED_TTL):
        return {"token": _WEB_CRED["token"], "user_id": _WEB_CRED.get("user_id") or ""}
    st, html = _web_http(session, f"{_PIXIV_WEB_BASE}/")
    if st != 200 or not html:
        raise PixivWebActionError(f"打不开 pixiv 首页 (HTTP {st})")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        raise PixivWebActionError("页面结构变了, 取不到登录令牌")
    try:
        data = _json.loads(m.group(1))
        pp = (data.get("props") or {}).get("pageProps") or {}
        ss = pp.get("serverSerializedPreloadedState")
        if isinstance(ss, str):
            ss = _json.loads(ss)
        token = str(((ss or {}).get("api") or {}).get("token") or "")
        uid = str((pp.get("gaUserData") or {}).get("userId") or "")
    except Exception:
        raise PixivWebActionError("页面数据解析失败, 取不到登录令牌")
    if not token:
        raise PixivWebActionError("未登录 pixiv（或登录态已失效），请到设置页重新登录")
    _WEB_CRED.clear()
    _WEB_CRED.update({"key": key, "token": token, "user_id": uid, "ts": _t.time()})
    return {"token": token, "user_id": uid}


def _web_api(session, path: str, *, json_body: dict = None, form_body: dict = None,
             timeout: float = 20.0):
    """带 csrf 头打 pixiv 网页端接口。返回解析后的 JSON (dict 或 list)。

    json_body/form_body 都不给 = GET (如查收藏状态); 给了 = 对应编码的 POST。
    出错统一抛 PixivWebActionError (消息取 pixiv 的 message, 人读)。"""
    import json as _json
    from urllib.parse import urlencode
    cred = pixiv_web_credentials(session)
    sess = dict(session)
    sess["headers"] = dict(session["headers"])
    sess["headers"]["x-csrf-token"] = cred["token"]
    if cred.get("user_id"):
        sess["headers"]["x-user-id"] = cred["user_id"]
    sess["headers"]["x-pixiv-next"] = "1"
    data, ctype = None, None
    if json_body is not None:
        data = _json.dumps(json_body).encode("utf-8")
        ctype = "application/json"
    elif form_body is not None:
        data = urlencode(form_body).encode("utf-8")
        ctype = "application/x-www-form-urlencoded; charset=utf-8"
    st, text = _web_http(sess, f"{_PIXIV_WEB_BASE}{path}", data=data,
                         content_type=ctype, timeout=timeout)
    try:
        body = _json.loads(text)
    except Exception:
        body = None
    if isinstance(body, dict) and body.get("error"):
        msg = str(body.get("message") or "").strip()
        if not msg:
            msg = ("目标不存在或已被删除" if st == 404
                   else f"pixiv 拒绝了这次操作 (HTTP {st})")
        raise PixivWebActionError(msg)
    if st != 200:
        raise PixivWebActionError(f"pixiv 返回 HTTP {st}")
    # pixiv 的 ajax 统一外壳是 {"error":false,"body":{...}} → 拆掉外壳(老接口
    # bookmark_add.php / rpc_group_setting.php 没有这层, 原样返回)。
    if isinstance(body, dict) and "body" in body:
        return body["body"]
    return body if body is not None else {"raw": text}


def web_bookmark_state(session, artwork_id: str) -> dict:
    """查作品收藏状态 → {"bookmarked": bool, "bookmark_id": str|None, "private": bool}。"""
    body = _web_api(session, f"/ajax/illust/{artwork_id}/bookmarkData") or {}
    bd = (body.get("bookmarkData") if isinstance(body, dict) else None) or {}
    bid = str(bd.get("id") or "")
    return {"bookmarked": bool(bid), "bookmark_id": bid or None,
            "private": bool(bd.get("private"))}


def web_bookmark_add(session, artwork_id: str, *, private: bool = False) -> dict:
    """收藏作品 (原网页点 ♡)。private=True = 非公开收藏 (只有自己可见)。"""
    return _web_api(session, "/ajax/illusts/bookmarks/add",
                    json_body={"illust_id": str(artwork_id),
                               "restrict": 1 if private else 0,
                               "comment": "", "tags": []})


def web_bookmark_remove(session, artwork_id: str) -> bool:
    """取消收藏 (先取收藏 id 再删)。返回是否确实删掉。"""
    st = web_bookmark_state(session, artwork_id)
    if not st.get("bookmark_id"):
        return False
    _web_api(session, "/ajax/illusts/bookmarks/delete",
             form_body={"bookmark_id": st["bookmark_id"]})
    return True


def web_follow_state(session, user_id: str) -> bool:
    """查是否已关注该作者 (读接口)。

    走同一套 `_web_api` (而不是自己 urlopen): ① 统一 .body 解包; ② 离线测试能
    替换同一层传输, **绝不许在测试里意外打真接口**。"""
    body = _web_api(session, f"/ajax/user/{user_id}?full=1&lang=zh")
    if not isinstance(body, dict):
        raise PixivWebActionError("关注状态返回格式异常")
    return bool(body.get("isFollowed"))


def web_follow(session, user_id: str, *, private: bool = False) -> bool:
    """关注作者 (private=True = 非公开关注)。返回 True=已关注。"""
    body = _web_api(session, "/bookmark_add.php",
                    form_body={"mode": "add", "type": "user",
                               "user_id": str(user_id), "tag": "",
                               "restrict": 1 if private else 0, "format": "json"})
    # 这个老接口成功时回的是简版数组 (前端的成功判据就是"数组且长度为 0")
    if isinstance(body, list):
        return len(body) == 0
    return True


def web_unfollow(session, user_id: str) -> bool:
    """取消关注作者。返回 True=已取关。"""
    body = _web_api(session, "/rpc_group_setting.php",
                    form_body={"mode": "del", "type": "bookuser",
                               "id": str(user_id)})
    if isinstance(body, dict) and body.get("user_id"):
        return str(body["user_id"]) == str(user_id)
    return True
