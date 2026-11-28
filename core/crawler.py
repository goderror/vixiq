# core/crawler.py
"""
Pixiv 作品下载器。从旧 pixiv_crawler.py 迁移,改造点:
- __init__ 改调 make_browser 工厂(消除重复 EdgeOptions 配置)
- XPath 从 resources/xpaths.json 加载(前端改版改 JSON 即可)
- cookies/save/input 路径全走 paths.user_data(便携)
- delays/retries/min_file_size 读 config
- print → reporter;tqdm → reporter.progress(避免 --windowed 下 fileno 崩溃)
- load_cookies_and_login 复用 utils.browser.login_with_cookies
"""
import json
import os
import re
import shutil
import time
import random
import urllib.request
from pathlib import Path

from selenium.common import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from utils import paths
from utils.author_naming import (
    author_folder_name, clean_author_name, collect_section_dirs,
    pick_default_section, pick_section_parent,
)
from utils.config import load_config
from utils.reporter import Reporter, PrintReporter
from utils.browser import make_browser, login_with_cookies
from db.store import PixivDatabase, PSt, WSt


# 单页原图 URL 形态 (登录原图 / 未登录 master1200 都含 _pN 页码段)
_PAGE_URL_RE = re.compile(r"_p(\d+)(?:_master1200)?\.(?:jpg|png)$", re.IGNORECASE)


def build_missing_page_urls(urls, want_pages) -> set:
    """DOM 漏检多页时, 从已抓到的原图 URL 规律推导缺失页 URL。

    pixiv 同一作品各页的原图 URL **只有页码段不同**且同扩展名:
      .../img/2022/.../100632088_p0_master1200.jpg → ..._p1_master1200.jpg
    两种典型漏检 (2026-09-15 实测):
      - 「查看全部」点击失败 → 只抓到 p0, 2 页作品永远缺 p1;
      - R18 多页作品只抓到 p1 (缺 p0, 实测 109766089) —— 因此锚点**不限于 p0**,
        任一含 _pN 的原图链接都能当模板推导其余页。
    urls: wait_for_images 的 {(url, aid, page_str, ext)} 集合; 返回需新增的同构集合。"""
    try:
        want = int(want_pages)
    except (TypeError, ValueError):
        return set()
    if want <= 1 or not urls:
        return set()
    cands = [u for u in urls if _PAGE_URL_RE.search(u[0])]
    if not cands:
        return set()
    # 锚点优先 p0 (最常见), 否则退回任一页
    base = next((u for u in cands
                 if int(_PAGE_URL_RE.search(u[0]).group(1)) == 0), cands[0])
    b_url, b_aid, _b_page, b_ext = base
    m = _PAGE_URL_RE.search(b_url)
    prefix = b_url[:m.start()]
    suffix = m.group(0)[len(f"_p{m.group(1)}"):]   # 保留 _master1200/.png 等尾巴
    have: set = set()
    for u in urls:
        mm = _PAGE_URL_RE.search(u[0])
        if mm:
            have.add(int(mm.group(1)))
    return {(f"{prefix}_p{pn}{suffix}", str(b_aid), str(pn), b_ext)
            for pn in range(want) if pn not in have}


def convert_to_png(src_path: str) -> "str | None":
    """兼容旧接口: jpg/jpeg → PNG 并删除原文件, 成功返回新路径, 否则 None。

    2026-11-21 起真正的入库转换统一走 `core.image_format.normalize_to_png`
    (含转换来源记录); 这个函数只保留给旧调用/回归用例。"""
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in (".jpg", ".jpeg"):
        return None
    from core.image_format import normalize_to_png
    r = normalize_to_png(src_path, delete_source=True)
    return r["current_path"] if (r.get("ok") and r.get("converted")) else None


# ===== 动图 (ugoira) =====
# pixiv 的动图作品跟普通插画**不是一个通道**: `ajax/illust/{id}/pages` 只给一张静态帧图
# (分辨率还是缩过的), 真正的动图在 `ajax/illust/{id}/ugoira_meta` 里 —— 一个 zip
# (每帧一张 png) + `frames: [{file, delay}]`(每帧延迟毫秒, **可变延迟**)。
# 以前下载器不分动图/静图, 一律走 pages 通道, 于是动图被下成一张低分辨率静态图
# 这里把动图单独走 zip → 合成 gif 的路。


class UgoiraError(RuntimeError):
    """动图专用通道失败。

    **为什么单独定义**: `download_artwork` 里 HTTP 通道失败会回退浏览器通道,
    而浏览器通道同样只有一张静态帧图 —— 回退等于把「动图下成低分辨率单张」这个
    同一个 bug 再演一遍。动图失败必须如实报错, 不能悄悄换通道。
    """


def parse_ugoira_frames(meta_body: dict) -> "list[tuple[str, int]]":
    """ugoira_meta.body → [(帧文件名, 延迟毫秒), ...] (纯函数, 可离线测)。

    延迟下限 10ms: pixiv 偶尔给 0, 而 gif 的 duration=0 会被浏览器当"尽可能快",
    合成出来的动图会快到看不见。
    """
    out: "list[tuple[str, int]]" = []
    for f in (meta_body or {}).get("frames") or []:
        fn = str((f or {}).get("file") or "").strip()
        if not fn:
            continue
        try:
            delay = int((f or {}).get("delay") or 0)
        except (TypeError, ValueError):
            delay = 0
        out.append((fn, max(10, delay)))
    return out


def compose_ugoira_gif(zip_bytes: bytes, frames: "list[tuple[str, int]]",
                       out_path: str) -> "tuple[int, int] | None":
    """pixiv ugoira 的 zip + 每帧延迟 → 一个动图 gif。成功返回 (宽, 高), 失败 None。

    帧一律转 RGBA 再交给 Pillow: ugoira 的帧带透明通道, 转 RGB 会把透明区压成黑块;
    Pillow 存 GIF 时会自己挑调色板并留一个透明索引 (已实测 3 帧可变延迟 + 透明正常)。
    """
    import io
    import zipfile
    from PIL import Image
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            imgs = []
            delays = []
            for fn, delay in frames:
                if fn not in names:
                    continue
                with zf.open(fn) as fh:
                    im = Image.open(io.BytesIO(fh.read()))
                    im.load()
                    imgs.append(im.convert("RGBA"))
                delays.append(delay)
            if not imgs:
                return None
            w, h = imgs[0].size
            imgs[0].save(out_path, "GIF", save_all=True, append_images=imgs[1:],
                         duration=delays, loop=0, optimize=False)
        return w, h
    except Exception:
        return None


class DownloadCancelled(Exception):
    """当前作品的下载被中止。

    中止来源有两个, 都由 `PixivDownloader._should_abort` 判定:
      - 全局「■ 停止任务」→ `reporter.is_cancelled`
      - 购物车里这一条被清空 → 下载引擎注册的逐项回调 (`set_abort_callback`)

    **为什么必须单独定义一个异常**: `download_artwork` 里有一层
    `except Exception` → 「HTTP 直连失败, 改用浏览器兜底」。若用 ValueError 之类
    表示中止, 会被这层吃掉, 结果变成「换个通道接着下」—— 用户点了清空/停止,
    下载却从浏览器又跑一遍。所以中止异常要在那层 except 之前显式 re-raise。
    """


class PixivDownloader:
    def __init__(self,
                 save_folder: "Path | str | None" = None,
                 input_folder: "Path | str | None" = None,
                 do_list: bool = False,
                 headless: "bool | None" = None,
                 reporter: "Reporter | None" = None,
                 config: "dict | None" = None):
        self.config = config or load_config()
        self.reporter = reporter or PrintReporter()
        self.db = PixivDatabase(reporter=self.reporter)

        # 路径全部基于程序可写区,默认 data/img、data/input
        pcfg = self.config["paths"]
        self.save_folder = Path(save_folder) if save_folder else paths.user_data(pcfg["img_subdir"])
        self.input_folder = Path(input_folder) if input_folder else paths.user_data(pcfg["input_subdir"])
        self.img_list_file = paths.user_data("img_list.txt")
        self.failed_img_file = paths.user_data("failed_img.txt")
        self.cookies_file = paths.user_data(pcfg["cookies_filename"])

        # XPath 从 resources/xpaths.json 一次性加载
        with open(paths.resource("xpaths.json"), encoding="utf-8") as f:
            xp = json.load(f)
        self.xp = xp["pixiv"]

        # 浏览器**懒加载**: 下载默认走 cookies 直连 pixiv ajax (零浏览器、
        # 不怕 headless 登录态丢失), 只有 HTTP 不可用/失效时才真的起 Edge 兜底。
        # 购物车执行一次以前也会固定启动一次浏览器, 纯浪费 3~5 秒还多一批残留进程。
        self._headless = headless
        self._driver = None
        self._wait = None
        self.login_ok = False
        # HTTP 直连会话: None=未尝试, False=无 cookies/已失效, dict=可用
        self._http = None
        self._http_cookie_mtime = 0.0
        self._author_dir_cache: "dict[str, str]" = {}
        self._section_dirs = None   # 图库顶层的分类目录 (X图/涩图…), 首次落盘时懒加载
        self.download_from_list = do_list
        self.not_found = []
        # 本次作品各页的失败详情 (含回滚状态与备份路径), 供下载队列行最终结果使用。
        self.last_failure_notes: list[str] = []
        # P4b · 购物车 Item 级进度: 外层可注册回调 —— PixivDownloader 内部每一次
        # reporter.progress 调用会额外转成 (scope, cur, total, status) 回调出去。
        # download_engine 绑回调时把 item_id 封装进闭包 → reporter.item_progress 上报。
        # scope: 'artwork_page' / 'artwork_in_list' / 'list_fetch' (新增的上层级)
        self._progress_cb = None
        # 「清空进行中的任务 = 撤销下载」: 引擎注册一个逐项回调,
        # 逐页回查「这条队列项还在不在」→ 行被清掉就立刻中止当前作品。
        # 以前只有全局 _cancel_event, 且只在作品之间检查 → 单作品多页时点了停止也傻下完。
        self._abort_cb = None

    def set_progress_callback(self, cb):
        """P4b: 设置进度回调 cb(scope, cur, total, status)。None 清除。"""
        self._progress_cb = cb

    def set_abort_callback(self, cb):
        """设置「是否应中止当前作品」回调 cb() -> bool。None 清除。
        下载引擎把「这一条队列项是否仍是本进程认领的 running 行」传进来; 逐页检查。"""
        self._abort_cb = cb

    def _should_abort(self) -> bool:
        """是否应中止当前作品: 全局停止 或 引擎的逐项回调说该停。
        回调抛异常一律当「不中止」—— 一次查询失败不该把正常下载打断。"""
        if self.reporter.is_cancelled():
            return True
        if self._abort_cb is None:
            return False
        try:
            return bool(self._abort_cb())
        except Exception:
            return False

    def _abort_now(self, artwork_id: str, done_pages: int, total: int,
                   newly_saved: int):
        """中止当前作品: 先把已下好的页落进 DB 再抛 DownloadCancelled。

        **不删已下好的文件** —— 图库里如实显示成「缺页」, 用户以后还能补下,
        比悄悄删掉用户可能已经想要的东西更安全 (删不删由用户在界面上决定)。
        抛异常而不是 break: 要让 download_artwork 的浏览器兜底与上层循环都看得见。
        """
        self.reporter.warn(
            f"⏹ 已中止: 作品 {artwork_id} 停在第 {done_pages}/{total} 页"
            f"（已下好的页保留在图库, 可随时补下）")
        if newly_saved > 0:
            self.db.bump_created_at(artwork_id)
        self.db.sync_work_and_pages(artwork_id)
        raise DownloadCancelled(f"作品 {artwork_id} 下载已中止")

    def _fire_progress(self, scope: str, cur: int, total: int, status: str):
        if self._progress_cb:
            try: self._progress_cb(scope, int(cur), int(total), status)
            except Exception: pass

    # -------- 浏览器懒加载 (HTTP 直连能搞定就不起 Edge) --------
    @property
    def driver(self):
        if self._driver is None:
            explicit = self.config["browser"]["explicit_wait_crawler"]
            self._driver, self._wait = make_browser(
                headless=self._headless, explicit_wait=explicit,
                reporter=self.reporter, config=self.config)
        return self._driver

    @property
    def wait(self):
        if self._wait is None:
            _ = self.driver    # 触发创建
        return self._wait

    def _http_ctx(self):
        """用 cookies.bin 构建 pixiv HTTP 直连会话 (与 SearchClient 同一套)。
        None=无可用 cookies; cookies 文件 mtime 变化 (重新登录) 后自动重建。"""
        cf = Path(self.cookies_file)
        try:
            mtime = cf.stat().st_mtime
        except OSError:
            mtime = 0.0
        if self._http is None or (self._http is False and mtime != self._http_cookie_mtime):
            self._http_cookie_mtime = mtime
            try:
                from utils.sec import load_cookies_smart
                from core.search_client import build_pixiv_http_session
                cookies = load_cookies_smart(str(cf), upgrade=False)
                uid = ""
                try:
                    acc = json.loads(
                        (paths.data_root() / "account.json").read_text(encoding="utf-8"))
                    uid = str(acc.get("user_id") or "")
                except Exception:
                    pass
                self._http = build_pixiv_http_session(cookies, uid) or False
            except Exception:
                self._http = False
        return self._http or None

    def _http_get_json(self, url: str, timeout: float = 15.0):
        from urllib.request import Request
        from utils.net import urlopen
        with urlopen(Request(url, headers=self._http_ctx()["headers"]),
                     timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    @staticmethod
    def _parse_pages_body(body: list) -> "list[tuple[int, str, str]]":
        """/ajax/illust/{id}/pages body → [(page_num, original_url, ext), ...] (纯函数)。"""
        out = []
        for i, p in enumerate(body or []):
            u = str(((p or {}).get("urls") or {}).get("original") or "")
            if not u:
                continue
            m = re.search(r"_p(\d+)\.(jpg|jpeg|png|gif|webp)(?:\?|$)", u, re.I)
            pn = int(m.group(1)) if m else i
            ext = (m.group(2).lower() if m else "jpg")
            if ext == "jpeg":
                ext = "jpg"
            out.append((pn, u, ext))
        return out

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def load_cookies_and_login(self) -> bool:
        self.login_ok = login_with_cookies(self.driver, self.cookies_file, self.reporter)
        return self.login_ok

    @staticmethod
    def _clean_author_name(name: str) -> str:
        # 只清非法文件名字符 (非法字符→`_`, 与一键整理/反搜/自检同一套规则);
        # pixiv 昵称允许纯数字 (如画师就叫 "310123"), 不能拒绝。
        # 2026-09-21 治本: 以前这里「删掉」非法字符, 一键整理却换成 `_`,
        # 同一位作者 (作者甲/作者乙) 于是被摆成两个夹。
        return clean_author_name(name)

    def _ajax_body(self, url: str):
        """导航到 pixiv ajax URL 取 JSON body, 失败/error 返回 None。
        比爬 DOM 稳, pixiv 改版不受影响。"""
        try:
            self.driver.get(url)
            data = json.loads(self.driver.find_element(By.TAG_NAME, "body").text)
            if data.get("error"):
                return None
            return data.get("body")
        except Exception:
            return None

    def _resolve_author(self, artwork_id: str, author_id=None, author_name=None):
        """解析作品作者, 返回 (author_id, author_name, save_dir, meta_info)。
        优先级: ①传入参数(队列项) ②DB artworks/authors ③ajax/illust 接口 ④旧 DOM XPath。
        全部失败也落占位作者文件夹 (与一键整理/自检同一套命名规则), 绝不散落图库根目录。
        meta_info: ajax/illust 解析出的 ArtworkInfo (标题/标签/封面/R18 全字段),
        下载时随文件当场落库; 拿不到则 None (事后自检兜底)。"""
        from core.search_client import SearchClient
        aid = str(author_id or "").strip()
        aname = ""
        raw_name = ""   # 原始昵称: 算规范夹名要它 (宣传后缀/别名要靠原名才剥得对)
        if author_name and self._clean_author_name(author_name) != "未知画师":
            aname = self._clean_author_name(author_name)
            raw_name = str(author_name).strip()
        # ② DB: artworks.author_id + authors.name
        try:
            meta = self.db.get_artwork_metadata(str(artwork_id)) or {}
            db_aid = str(meta.get("author_id") or "").strip()
            if db_aid:
                aid = aid or db_aid
            if aid:
                arow = self.db.get_author(aid) or {}
                if arow.get("name"):
                    aname = aname or self._clean_author_name(arow["name"])
                    raw_name = raw_name or str(arow["name"]).strip()
        except Exception:
            pass
        # ③ ajax 接口 (body 里标题/标签/页数/封面全有, 一并解析)
        meta_info = None
        if not (aid and aname and self._db_has_title(artwork_id)):
            body = self._ajax_body(f"https://www.pixiv.net/ajax/illust/{artwork_id}?lang=zh")
            if body:
                try:
                    meta_info = SearchClient._illust_body_to_info(body)
                except Exception:
                    meta_info = None
                aj_aid = str(body.get("userId") or "").strip()
                aj_raw = str(body.get("userName") or "").strip()
                aj_name = self._clean_author_name(aj_raw)
                if aj_aid:
                    aid = aid or aj_aid
                if aj_name != "未知画师":
                    aname = aname or aj_name
                    raw_name = raw_name or aj_raw
        # ④ 旧 DOM XPath (最后兜底; ajax 通道会把页面导航走, 先回作品页)
        if not (aid and aname):
            try:
                self.driver.get(f"https://www.pixiv.net/artworks/{artwork_id}")
                dom_raw = self.wait.until(
                    EC.presence_of_element_located((By.XPATH, self.xp["author_name"]))
                ).text.strip()
                dom_link = self.driver.find_element(
                    By.XPATH, self.xp["author_id"]).get_attribute("href")
                dom_aid = re.search(r'/users/(\d+)', dom_link).group(1)
                if dom_aid:
                    aid = aid or dom_aid
                dom_name = self._clean_author_name(dom_raw)
                if dom_name != "未知画师":
                    aname = aname or dom_name
                    raw_name = raw_name or dom_raw
            except Exception as e:
                self.reporter.warn(f"❗ DOM 取作者信息失败:{str(e).splitlines()[0]}")
        aid = aid or "0"
        aname = aname or "未知画师"
        # 夹名走全项目唯一规则 (2026-09-21 治本): 与一键整理的期望夹名一字不差,
        # 否则第一次下载建一个夹、一键整理又想要另一个 (实测 作者甲/作者乙 双夹)。
        # `_pick_author_dir` 仍会优先复用磁盘上已存在的夹 (含被分类进 涩图/X图 的)。
        save_dir = self._pick_author_dir(
            aid, aname, artwork_id, author_folder_name(self.db, aid, raw_name or aname),
            is_r18=(getattr(meta_info, "is_r18", None)
                    if meta_info is not None else None))
        return aid, aname, save_dir, meta_info

    def _db_has_title(self, artwork_id) -> bool:
        try:
            m = self.db.get_artwork_metadata(str(artwork_id)) or {}
            return bool(m.get("title"))
        except Exception:
            return False

    def _record_meta_info(self, artwork_id, info, author_id, author_name):
        """下载/归档当场把权威元数据写库 (标题/标签/作者/封面/R18), 不等自检。"""
        try:
            if info is not None:
                self.db.upsert_artwork_metadata(
                    str(artwork_id),
                    title=info.title or None,
                    author_id=info.author_id or author_id,
                    author_name=info.author_name or author_name,
                    illust_type=info.illust_type.value,
                    tags=list(info.tags or []),
                    publish_date=info.publish_date,
                    is_r18=info.is_r18,
                    thumb_url=(info.image_urls or [None])[0],
                    page_count=info.page_count,
                    ai_type=getattr(info, "ai_type", 0),
                    work_status=WSt.NOT_YET)
            else:
                self.db.upsert_author(str(author_id), str(author_name))
                self.db.change_work_status(str(artwork_id), WSt.NOT_YET,
                                           author_id=str(author_id))
        except Exception as e:
            self.reporter.warn(f"❗ 元数据记录失败: {str(e).splitlines()[0]}")

    def download_artwork(self, artwork_id, author_id=None, author_name=None,
                         force_overwrite: bool = False, pages=None):
        """下载单个作品。**HTTP 直连优先** (cookies 打 ajax, R18 也能拿到原图),
        无 cookies / 登录态失效 / 网络异常时回退 Selenium 浏览器。

        pages (2026-10-20 按页下载): 只下这几页 (页号 0 起); None = 整部。
        **页级任务绝不回退浏览器** —— `_download_browser` 不支持页级, 会把未选页也补下。

        2026-09-16 实测事故: cookies 本身有效, 但购物车链路从不调用
        load_cookies_and_login, headless 浏览器是**未登录**状态, R18 作品页面不渲染
        任何图片 → 三个任务连续报「登录已失效」。直连通道根本不经过浏览器,
        彻底绕开这个坑; 浏览器兜底也会在导航前先注入 cookies。"""
        artwork_id = str(artwork_id)
        only = {int(x) for x in pages} if pages else None
        self.last_failure_notes = []
        self.reporter.log(
            f"正在下载 → https://www.pixiv.net/artworks/{artwork_id}")
        ctx = self._http_ctx()
        if ctx is not None:
            try:
                return self._download_http(
                    artwork_id, ctx, author_id=author_id,
                    author_name=author_name, force_overwrite=force_overwrite,
                    pages=only)
            except PermissionError as e:
                self.reporter.warn(f"HTTP 直连登录态失效 ({str(e).splitlines()[0]})，"
                                   "改用浏览器并重新注入 cookies")
                self._http = False
            except DownloadCancelled:
                # 用户中止: 绝不能落到下面的「改用浏览器兜底」—— 那就成了
                # 「点了清空/停止, 换个通道接着下」
                raise
            except UgoiraError as e:
                # 动图失败也**不能**回退浏览器: 浏览器通道只有一张静态帧图,
                # 下下来就是「动图变低分辨率单张」。如实报错让他重试。
                self.reporter.error(f"动图 {artwork_id} 下载失败: {str(e).splitlines()[0]}")
                raise
            except Exception as e:
                msg = str(e).splitlines()[0]
                # 作品确认不存在 → 不重试不留 running, 直接判失败
                from core.search_client import ArtworkNotFoundError
                if isinstance(e, ArtworkNotFoundError):
                    self.reporter.warn(f"作品已删除或不存在: {msg}")
                    self.db.change_work_status(artwork_id, WSt.ERROR)
                    raise ValueError(f"作品 {artwork_id} 不存在或已删除")
                self.reporter.warn(f"HTTP 直连下载失败，改用浏览器兜底: {msg}")
        if only is not None:
            # 页级任务: 浏览器通道不支持 (见上), 绝不静默改用整部通道。
            raise RuntimeError(
                f"作品 {artwork_id} 的按页下载失败, 且按页下载不支持浏览器兜底")
        return self._download_browser(
            artwork_id, author_id=author_id, author_name=author_name,
            force_overwrite=force_overwrite)

    def _pick_author_dir(self, author_id: str, aname: str, artwork_id: str,
                         folder_name: "str | None" = None,
                         is_r18: "bool | None" = None) -> str:
        """决定作品落盘目录: 优先复用图库里**已存在的该作者夹** (用户可能已把它手工
        分类到 涩图/X图 等子目录), 尤其选已含本作品文件的那个夹 —— 补下缺页时新页必须
        落进同一夹, 不能在图库根另建一个作者夹把作品拆两半; 找不到才用根级标准夹。

        folder_name: 规范夹名 (由 `author_folder_name` 算, 与一键整理的期望夹名一致)。
        不传则退回「清洗后现用名_ID」—— 那条路与一键整理可能对不上, 只在老调用点用。
        is_r18: 作者在多个分类下都有夹时, 按作品 R18 选对应分类 (涩图/R18 vs X图/普通)。"""
        author_id = str(author_id or "0")
        cache_key = f"{author_id}/{artwork_id}"
        if cache_key in self._author_dir_cache:
            return self._author_dir_cache[cache_key]
        suffix = f"_{author_id}"
        cands, with_work= [], []
        try:
            for p in self.save_folder.rglob("*"):
                if p.is_dir() and p.name.endswith(suffix):
                    cands.append(p)
                    if any(p.glob(f"{artwork_id}_p*")):
                        with_work.append(p)
        except OSError:
            pass

        def _parent_of(p: Path) -> str:
            rel = p.relative_to(self.save_folder)
            return "" if str(rel) == p.name else rel.parent.as_posix()

        def _default_section() -> "str | None":
            """优先用图库分类配置（R18 规则 → 分类目录），再退回磁盘关键词识别。"""
            db = getattr(self, "db", None)
            if db is not None and hasattr(db, "section_dir_for_r18"):
                try:
                    sec = db.section_dir_for_r18(is_r18)
                    # 分类目录已存在才跟随；空图库/还没建分类时保持旧行为（根目录）
                    if sec and (Path(self.save_folder) / sec).is_dir():
                        return sec
                except Exception:
                    pass
            if getattr(self, "_section_dirs", None) is None:
                try:
                    ids = {str(r[0]) for r in self.db.conn.execute(
                        "SELECT author_id FROM authors")}
                except Exception:
                    ids = set()
                self._section_dirs = collect_section_dirs(self.save_folder, ids)
            return pick_default_section(self._section_dirs, is_r18)

        if with_work:
            d = str(max(with_work, key=lambda x: len(list(x.glob(f"{artwork_id}_p*")))))
        elif cands:
            # 分类夹优先: 作者已经在 X图/涩图 下建过夹, 新作品跟过去;
            # 只有根目录夹时也直接复用 (不能因为默认分类另建一个)。
            picked = pick_section_parent({_parent_of(p) for p in cands}, is_r18)
            pool = [p for p in cands if _parent_of(p) == picked] if picked is not None else []
            if pool:
                d = str(sorted(pool, key=lambda x: str(x))[0])
            else:
                sec = _default_section()
                if sec:
                    d = os.path.join(self.save_folder, sec,
                                     folder_name or f"{aname}_{author_id}")
                else:
                    d = str(sorted(cands, key=lambda x: str(x))[0])
        else:
            # 新作者: 图库已有分类时按 R18 落分类, 没有分类才落根目录 (保持旧行为)
            sec = _default_section()
            d = os.path.join(self.save_folder, sec or "",
                             folder_name or f"{aname}_{author_id}")
        self._author_dir_cache[cache_key] = d
        return d

    # ---- 下载暂存 + 入库格式统一 (2026-11-21 4B-1 复验返修) ----
    def _staging_path(self, ext: str) -> str:
        """应用拥有的下载暂存文件: 绝不与图库正式文件同名同路径。

        下载失败/重试的清理只会删这个文件, 图库现图在整个下载阶段保持原样。"""
        import uuid
        d = paths.temp_root() / "downloads"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"{uuid.uuid4().hex}{ext or '.img'}")

    def _cleanup_staging(self, *items) -> None:
        for p in items:
            if not p:
                continue
            try:
                os.remove(str(p))
            except OSError:
                pass

    def _replace_file(self, src, dst) -> None:
        """同盘原子替换 (单独成方法, 便于回归注入替换失败)。"""
        os.replace(str(src), str(dst))

    def _old_page_file(self, old_rel: "str | None"):
        if not old_rel:
            return None
        cand = os.path.join(self.save_folder, str(old_rel).replace("/", os.sep))
        return cand if os.path.isfile(cand) else None

    def _record_page_conversion(self, aid: str, pn: int, conv: dict) -> bool:
        """把转换来源记录写到页上 (失败只补原因, 不清空已有来源记录)。"""
        if conv.get("reuse"):
            return True     # 复用已有 PNG: 旧记录仍然可信
        ok = bool(conv.get("ok"))
        try:
            self.db.set_page_conversion(
                aid, pn,
                source_format=conv.get("source_format"),
                source_file_size=conv.get("source_file_size"),
                source_md5=conv.get("source_md5"),
                converted_at=conv.get("converted_at"),
                current_format=conv.get("current_format"),
                convert_note=(conv.get("note") or None),
                replace=ok,
            )
            return True
        except Exception as e:
            self.reporter.warn(
                f"⚠ 转换记录写入失败（文件已保留，可重试）: {str(e).splitlines()[0]}")
            return False

    def _finalize_downloaded_page(self, aid: str, pn: int, staging_path: str,
                                  final_path: str, *,
                                  old_rel: "str | None" = None,
                                  force_overwrite: bool = False) -> "tuple[bool, dict]":
        """下载暂存文件校验/转换后, 再替换图库正式文件并登记。

        两步分离 (4B-1 复验返修):
        1. 新文件只写应用临时目录, 下载失败/重试清理不会碰图库现图;
        2. 候选文件必须可识别且完整解码, 才按覆盖规则切换正式文件。
        失败时旧图字节、路径、状态和转换来源记录一律保持不变。
        """
        import uuid
        from core.image_format import apply_ingest_format, same_pixels, is_readable
        from utils.config import ingest_auto_png
        staging = Path(staging_path)
        dest_path = Path(final_path)

        def _file_sig(path):
            """读文件的 md5/大小; 读不了返回 (None, None)。"""
            try:
                from utils.hashutil import md5_of
                return md5_of(str(path)), os.path.getsize(str(path))
            except Exception:
                return None, None

        def _same_sig(path, md5, size):
            if md5 is None or not os.path.isfile(str(path)):
                return False
            cur_md5, cur_size = _file_sig(path)
            return cur_md5 == md5 and cur_size == size

        def _keep_old(reason: str, note: str, *, conflict: bool = False):
            self._cleanup_staging(staging)
            old_file = self._old_page_file(old_rel)
            old_ok = bool(old_file and is_readable(old_file))
            if not old_ok:
                try:
                    self.db.mark_page_status(aid, pn, PSt.FAILED)
                except Exception as e:
                    self.reporter.warn(
                        f"⚠ 标记失败状态失败: {str(e).splitlines()[0]}")
            self.reporter.warn(
                f"⚠ {note}: {dest_path.name}"
                + ("（已保留旧文件与登记）" if old_ok else ""))
            return False, {"ok": False, "reason": reason, "note": note,
                           "conflict": conflict,
                           "current_path": str(old_file or dest_path)}

        # ① 下载暂存必须可识别且能完整解码
        if not staging.is_file() or not is_readable(staging):
            return _keep_old("unreadable", "新下载文件无法打开，未登记")

        # ② 转换到图库内的候选临时文件 (非图片后缀, 不会被对账扫到)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        candidate = dest_path.parent / f".vixiq-new-{uuid.uuid4().hex}.tmp"
        # 入库格式: 全局设置决定是否转 PNG; 该页明确选过 JPG 时尊重用户选择。
        enabled = ingest_auto_png()
        if enabled:
            try:
                rec = self.db.get_page_conversion(aid, pn)
            except Exception:
                rec = None
            if rec and str(rec.get("format_choice") or "").lower() == "jpg":
                enabled = False
        conv = apply_ingest_format(str(staging), out_path=str(candidate),
                                   enabled=enabled, delete_source=True,
                                   overwrite=True)
        cur_path = Path(conv.get("current_path") or staging)
        if not conv.get("ok") and conv.get("reason") == "unreadable":
            self._cleanup_staging(candidate)
            return _keep_old("unreadable", "新下载文件无法打开，未登记")
        if not cur_path.is_file() or not is_readable(cur_path):
            self._cleanup_staging(staging, candidate)
            return _keep_old("unreadable", "新下载文件无法打开，未登记")
        if conv.get("cleanup_failed"):
            self.reporter.warn("⚠ 临时副本清理失败，可稍后手动清理")
        # 转 PNG 时正式名用 .png; 动图/转换失败回退保持原扩展名
        if conv.get("current_format") == "png":
            dest_path = dest_path.with_suffix(".png")
        elif not conv.get("enabled"):
            # 关闭自动转 PNG: 按实际格式修正扩展名, 但绝不重新编码
            _fmt = str(conv.get("current_format") or "").lower()
            _ext = {"jpg": ".jpg", "jpeg": ".jpg", "webp": ".webp", "gif": ".gif",
                    "bmp": ".bmp", "tif": ".tif", "tiff": ".tif"}.get(_fmt)
            if _ext:
                dest_path = dest_path.with_suffix(_ext)
        single_frame = (not conv.get("animated")
                        and conv.get("current_format") != "gif"
                        and "动图" not in (conv.get("note") or "")
                        and "多帧" not in (conv.get("note") or ""))

        old_file = self._old_page_file(old_rel)
        if old_file is None and dest_path.is_file():
            old_file = str(dest_path)
        # 目标路径已存在且不是当前登记文件 (如旧 JPG 旁边已有 PNG): 必须先核对
        other_target = bool(old_file and Path(old_file) != dest_path
                            and dest_path.is_file())

        # ③ 目标已存在但不是当前登记文件: 内容一致才复用, 否则冲突并保留两份
        if other_target:
            if (is_readable(dest_path) and single_frame
                    and same_pixels(cur_path, dest_path)):
                self._cleanup_staging(staging, cur_path)
                self.reporter.log(f"♻ 目标已有相同图片，复用: {dest_path.name}")
                conv.update(ok=True, reuse=True, current_path=str(dest_path),
                            current_format="png")
                try:
                    from utils.hashutil import md5_of
                    rel = dest_path.relative_to(self.save_folder).as_posix()
                    self.db.store_page_result(
                        aid, pn, rel, md5_of(str(dest_path)),
                        os.path.getsize(str(dest_path)), conversion=None)
                except Exception as e:
                    note = "目标文件一致但登记失败，保留现有文件与登记"
                    self.reporter.warn(f"⚠ {note}: {str(e).splitlines()[0]}")
                    return False, dict(conv, ok=False, reuse=False,
                                       reason="register_failed", note=note,
                                       current_path=str(old_file))
                return True, conv
            self._cleanup_staging(staging, cur_path)
            note = "目标路径已有不同内容的文件，保留两个现有文件"
            self.reporter.warn(f"⚠ {note}: {dest_path.name}")
            return False, dict(conv, ok=False, conflict=True, keep_existing=True,
                               reason="target_conflict", note=note,
                               current_path=str(old_file))

        # ④ 同图复用: 不重复写正式文件; 页登记缺失时补齐现图登记后才算成功
        if (old_file and is_readable(old_file) and single_frame
                and same_pixels(cur_path, old_file)):
            self._cleanup_staging(staging, cur_path)
            self.reporter.log(
                f"♻ 同页已有相同图片，复用: {os.path.basename(old_file)}")
            conv.update(ok=True, reuse=True, current_path=str(old_file),
                        current_format=("png" if str(old_file).lower().endswith(".png")
                                        else conv.get("current_format")))
            rel = None
            try:
                rel = Path(old_file).relative_to(self.save_folder).as_posix()
            except Exception:
                pass
            need_fill = False
            if rel:
                try:
                    info = self.db.get_page_info(aid, pn)
                except Exception:
                    info = None
                need_fill = (info is None
                             or int(info.status or 0) != PSt.DONE
                             or str(info.local_path or "").replace("\\", "/") != rel)
            if need_fill:
                try:
                    from utils.hashutil import md5_of
                    self.db.store_page_result(
                        aid, pn, rel, md5_of(old_file),
                        os.path.getsize(old_file), conversion=None)
                    self.reporter.log(
                        f"🔗 已补齐现图登记: {os.path.basename(old_file)}")
                except Exception as e:
                    note = "复用现图但补齐登记失败，保留现有登记"
                    self.reporter.warn(f"⚠ {note}: {str(e).splitlines()[0]}")
                    return False, dict(conv, ok=False, reuse=False,
                                       reason="register_failed", note=note,
                                       current_path=str(old_file))
            return True, conv

        # ⑤ 不同内容: 普通下载/补页保留现图; 只有强制重下才备份后替换
        if old_file and not force_overwrite:
            self._cleanup_staging(staging, cur_path)
            note = "同页已有不同内容的图片，保留现有文件"
            self.reporter.warn(f"⚠ {note}: {os.path.basename(old_file)}")
            return False, dict(conv, ok=False, conflict=True, keep_existing=True,
                               reason="same_page_conflict", note=note,
                               current_path=str(old_file))

        # ⑥ 强制重下: 用复制保留可恢复副本; 当前图在切换成功前一直留在正式路径
        same_path = bool(old_file and Path(old_file) == dest_path)
        bak = None
        bak_md5 = bak_size = None
        if old_file and force_overwrite:
            bak = old_file + ".bak"
            try:
                if os.path.exists(bak):
                    os.remove(bak)
                shutil.copy2(old_file, bak)
                if not is_readable(bak):
                    raise OSError("备份文件不可读")
                bak_md5, bak_size = _file_sig(bak)
                if bak_md5 is None:
                    raise OSError("备份文件指纹不可用")
                self.reporter.log(f"💾 旧版已备份 → {os.path.basename(bak)}")
            except Exception as e:
                self._cleanup_staging(staging, cur_path)
                note = "覆盖前备份失败，保留现有文件"
                self.reporter.warn(f"⚠ {note}: {str(e).splitlines()[0]}")
                return False, dict(conv, ok=False, conflict=True,
                                   keep_existing=True, reason="backup_failed",
                                   note=note, current_path=str(old_file))
        # 目标原不存在: 切换前再确认一次, 防止被其他操作创建后覆盖
        if dest_path.exists() and not same_path:
            self._cleanup_staging(staging, cur_path)
            note = "切换前目标路径被其他操作创建，保留现有文件"
            self.reporter.warn(f"⚠ {note}: {dest_path.name}")
            return False, dict(conv, ok=False, conflict=True, keep_existing=True,
                               reason="target_conflict", note=note,
                               current_path=str(old_file or dest_path))

        # ⑦ 只做一次原子替换发布候选; 失败时当前图仍在原路径
        try:
            self._replace_file(cur_path, dest_path)
        except Exception as e:
            self._cleanup_staging(staging, cur_path)
            note = "替换正式文件失败，当前图仍保留在原路径"
            self.reporter.warn(f"⚠ {note}: {str(e).splitlines()[0]}")
            return False, dict(conv, ok=False, reason="replace_failed",
                               note=note, current_path=str(old_file or dest_path))
        self._cleanup_staging(staging)

        # ⑧ 文件切换 + 页登记/来源记录必须在一个事务里一起成功
        try:
            rel_path = dest_path.relative_to(self.save_folder).as_posix()
        except Exception as e:
            self.reporter.warn(
                f"⚠ 入库路径无效，未登记: {str(e).splitlines()[0]}")
            return False, dict(conv, ok=False, reason="register_failed",
                               note="入库路径无效", current_path=str(dest_path))
        fmd5 = fsize = None
        try:
            from utils.hashutil import md5_of
            fmd5 = md5_of(str(dest_path))
            fsize = os.path.getsize(str(dest_path))
        except Exception as e:
            self.reporter.warn(
                f"⚠ 读取新文件大小/指纹失败: {str(e).splitlines()[0]}")
        try:
            # 关闭自动转 PNG 时只更新「当前格式」, 不留下任何转换来源字段
            conv_db = conv if conv.get("enabled") else {
                "current_format": conv.get("current_format"), "note": ""}
            self.db.store_page_result(
                aid, pn, rel_path, fmd5, fsize,
                conversion=conv_db, replace_conversion=bool(conv.get("ok")))
        except Exception as e:
            # 事务已回滚。恢复前先确认正式路径仍是本次写入的新图; 不是就保留后来文件。
            old_restored = False
            restore_failed = False
            target_changed = False
            new_removed = False
            remove_failed = False
            backup_path = str(bak) if bak and Path(bak).is_file() else ""
            backup_verified = bool(
                backup_path and bak_md5 is not None
                and _same_sig(backup_path, bak_md5, bak_size)
                and is_readable(backup_path))
            if same_path:
                if not _same_sig(dest_path, fmd5, fsize):
                    target_changed = True
                    note = ("登记失败，回滚前目标已被其他操作更新；"
                            "已保留后来文件与旧图备份，请人工处理")
                    if backup_path:
                        note += f"（可恢复副本：{backup_path}）"
                    self.reporter.warn(f"⚠ {note}: {dest_path.name}")
                    return False, dict(
                        conv, ok=False, reason="register_failed", note=note,
                        current_path=str(dest_path), conflict=True,
                        keep_existing=True, old_restored=False,
                        restore_failed=False, target_changed=True,
                        new_removed=False, remove_failed=False,
                        backup_path=backup_path, backup_verified=backup_verified)
                if not backup_verified:
                    restore_failed = True
                    note = "登记失败，旧图尚未恢复；可恢复副本不可用，请人工处理"
                    self.reporter.warn(f"⚠ {note}: {dest_path.name}")
                    return False, dict(
                        conv, ok=False, reason="register_failed", note=note,
                        current_path=str(dest_path), old_restored=False,
                        restore_failed=True, target_changed=False,
                        new_removed=False, remove_failed=False,
                        backup_path=backup_path, backup_verified=False)
                restore_candidate = dest_path.parent / \
                    f".vixiq-restore-{uuid.uuid4().hex}.tmp"
                last_err = None
                try:
                    for _attempt in range(3):
                        # 先备好并核验恢复候选, 再重新核验目标身份 —— 复制可能耗时,
                        # 期间目标可能被应用内其它操作换成另一张图, 不能拿旧判断覆盖。
                        try:
                            shutil.copy2(bak, restore_candidate)
                            if not _same_sig(restore_candidate, bak_md5, bak_size):
                                raise OSError("恢复候选校验失败")
                        except Exception as e2:
                            last_err = e2
                            time.sleep(0.05)
                            continue
                        if not _same_sig(dest_path, fmd5, fsize):
                            target_changed = True
                            break
                        try:
                            self._replace_file(restore_candidate, dest_path)
                            if _same_sig(dest_path, bak_md5, bak_size):
                                old_restored = True
                                break
                            last_err = OSError("恢复后字节校验失败")
                        except Exception as e2:
                            last_err = e2
                        time.sleep(0.05)
                finally:
                    self._cleanup_staging(restore_candidate)
                if old_restored:
                    self.reporter.log("↩ 登记失败，已从备份恢复旧图")
                    note = "登记失败，已恢复旧图"
                    self.reporter.warn(f"⚠ {note}: {dest_path.name}")
                elif target_changed:
                    note = ("登记失败，回滚前目标已被其他操作更新；"
                            "已保留后来文件与旧图备份，请人工处理")
                    if backup_path:
                        note += f"（可恢复副本：{backup_path}）"
                    self.reporter.warn(f"⚠ {note}: {dest_path.name}")
                else:
                    restore_failed = True
                    note = "登记失败，旧图尚未恢复"
                    note += (f"；可恢复副本：{backup_path}" if backup_path
                             else "；可恢复副本不可用")
                    self.reporter.warn(
                        f"⚠ {note}: {dest_path.name}；"
                        f"{str(last_err).splitlines()[0] if last_err else '恢复失败'}")
                return False, dict(
                    conv, ok=False, reason="register_failed", note=note,
                    current_path=str(dest_path), conflict=target_changed,
                    keep_existing=target_changed, old_restored=old_restored,
                    restore_failed=restore_failed, target_changed=target_changed,
                    new_removed=False, remove_failed=False,
                    backup_path=backup_path, backup_verified=backup_verified)
            # 新旧路径不同: 只撤销本次创建的目标; 目标已被别人更新则保留
            if _same_sig(dest_path, fmd5, fsize):
                remove_err = None
                try:
                    os.remove(str(dest_path))
                except Exception as e2:
                    remove_err = e2
                if remove_err is None:
                    new_removed = True
                    note = ("登记失败，已撤销本次新文件，旧图与登记保持原样"
                            if old_file else "登记失败，已撤销本次新文件")
                    current = str(old_file or dest_path)
                    old_restored = True
                else:
                    # 新图仍被占用: 如实报告保留位置, 不能写成恢复完成。
                    remove_failed = True
                    note = f"登记失败，本次新文件仍被占用未能移除；已保留：{dest_path}"
                    if old_file:
                        note += "；旧图与登记保持原样"
                    if backup_path:
                        note += f"；可恢复副本：{backup_path}"
                    self.reporter.warn(
                        f"⚠ {note}: {str(remove_err).splitlines()[0]}")
                    current = str(dest_path)
            else:
                target_changed = True
                note = "登记失败，目标已被其他操作更新；已保留后来文件与旧图备份"
                if backup_path:
                    note += f"（可恢复副本：{backup_path}）"
                current = str(dest_path)
            if not remove_failed:
                self.reporter.warn(f"⚠ {note}: {dest_path.name}")
            return False, dict(
                conv, ok=False, reason="register_failed", note=note,
                current_path=current, conflict=target_changed,
                keep_existing=target_changed, old_restored=old_restored,
                restore_failed=False, target_changed=target_changed,
                new_removed=new_removed, remove_failed=remove_failed,
                backup_path=backup_path, backup_verified=backup_verified)
        conv["current_path"] = str(dest_path)
        if not conv.get("ok"):
            self.reporter.warn(
                f"⚠ 已保留原格式（{conv.get('note')}），后续可重试转 PNG: "
                f"{dest_path.name}")
        return True, conv

    def _download_http(self, artwork_id: str, ctx, *, author_id=None,
                       author_name=None, force_overwrite: bool = False, pages=None):
        """cookies 直连通道: ajax/illust 取元数据 + ajax/illust/pages 取每页原图直链,
        urllib + Referer 下载 (与浏览器通道同一套落盘/转PNG/入库逻辑)。

        pages (2026-10-20 按页下载): 只下这几页 (页号集合); None = 整部。
        ⚠ `init_pages` 仍传**远端总页数**: 按选中页数写会把整部作品判成已下完
        (sync_work_and_pages 会把 0..tp-1 全标 DONE → 图库谎报入库)。"""
        from core.search_client import http_fetch_illust
        info = http_fetch_illust(ctx, artwork_id)   # 403 → PermissionError
        # 动图 (ugoira) 走单独通道: pages 接口只给一张缩过的静态帧图, 下了也不是动图
        from core.models import IllustType
        if info.illust_type == IllustType.UGOIRA:
            return self._download_ugoira_http(
                artwork_id, ctx, info, force_overwrite=force_overwrite)
        data = self._http_get_json(
            f"https://www.pixiv.net/ajax/illust/{artwork_id}/pages?lang=zh")
        if data.get("error"):
            raise RuntimeError(f"pages 接口错误: {str(data.get('message'))[:80]}")
        all_pages = self._parse_pages_body(data.get("body") or [])
        if not all_pages:
            raise RuntimeError("pages 接口未返回任何图片链接")
        if pages:
            only = {int(x) for x in pages}
            selected = [t for t in all_pages if t[0] in only]
            if not selected:
                raise RuntimeError(
                    f"选中的页 {sorted(only)} 在原网页不存在 (共 {len(all_pages)} 页)")
            pages = selected
        else:
            pages = all_pages

        raw_name = str(info.author_name or author_name or "").strip()
        aname = self._clean_author_name(raw_name or "未知画师")
        aid = str(info.author_id or author_id or "0")
        # 规范夹名 (与一键整理同一套) —— 见 `_resolve_author` 同处注释
        save_dir = self._pick_author_dir(
            aid, aname, artwork_id,
            author_folder_name(self.db, aid, raw_name or aname),
            is_r18=getattr(info, "is_r18", None))
        os.makedirs(save_dir, exist_ok=True)
        self.reporter.log(f"图片将保存至:{save_dir}（HTTP 直连，共 {len(pages)} 页）")
        # 权威元数据当场落库 (标题/标签/作者/R18/封面/页数; 顺带纠正作者旧名/乱码名)
        self._record_meta_info(artwork_id, info, aid, aname)
        self.db.init_pages(artwork_id, info.page_count)

        ccfg = self.config["crawler"]
        success_count = 0
        newly_saved = 0
        total = len(pages)
        for i, (pn, img_url, ext) in enumerate(pages, 1):
            if self._should_abort():
                self._abort_now(artwork_id, i - 1, total, newly_saved)
            filename = f"{artwork_id}_p{pn}.{ext}"
            save_path = os.path.join(save_dir, filename)
            pinfo = self.db.get_page_info(artwork_id, pn)
            if not force_overwrite and pinfo is not None \
                    and int(pinfo.status or 0) == PSt.DONE:
                # DB 说已下完还要看文件在不在 (可能被手工移走/删除)
                existed = None
                if pinfo.local_path:
                    cand = os.path.join(self.save_folder,
                                        str(pinfo.local_path).replace("/", os.sep))
                    if os.path.isfile(cand):
                        existed = cand
                if not existed and os.path.isfile(save_path):
                    existed = save_path
                elif not existed:
                    png_alt = os.path.join(save_dir, f"{artwork_id}_p{pn}.png")
                    if os.path.isfile(png_alt):
                        existed = png_alt
                if existed:
                    self.reporter.progress(i, total, f"{filename} → 已存在")
                    self._fire_progress("artwork_page", i, total, filename[:10])
                    success_count += 1
                    continue
            # 4B-1 复验返修: 新下载只写应用暂存文件, 校验通过后才替换正式文件
            final_path = os.path.join(save_dir, filename)
            staging = self._staging_path("." + ext)
            if self.download_with_retry(img_url, staging):
                ok, conv = self._finalize_downloaded_page(
                    artwork_id, pn, staging, final_path,
                    old_rel=(pinfo.local_path if pinfo is not None else None),
                    force_overwrite=force_overwrite)
                if ok:
                    filename = os.path.basename(conv["current_path"])
                    self.reporter.progress(i, total, f"{filename} → 成功")
                    success_count += 1
                    newly_saved += 1
                else:
                    why = (conv.get("note")
                           or ("同页冲突，保留现有文件" if conv.get("conflict")
                               else "失败"))
                    self.reporter.progress(i, total, f"{filename} → {why}")
                    self.last_failure_notes.append(f"{filename}: {why}")
                    self.not_found.append(filename)
                self._fire_progress("artwork_page", i, total, filename[:10])
            else:
                self._cleanup_staging(staging)
                old_file = self._old_page_file(
                    pinfo.local_path if pinfo is not None else None)
                from core.image_format import is_readable
                if not (old_file and is_readable(old_file)):
                    try:
                        self.db.mark_page_status(artwork_id, pn, PSt.FAILED)
                    except Exception as e:
                        self.reporter.warn(
                            f"⚠ 标记失败状态失败: {str(e).splitlines()[0]}")
                self.reporter.progress(i, total, f"{filename} → 失败")
                self.last_failure_notes.append(f"{filename}: 下载失败")
                self._fire_progress("artwork_page", i, total, filename[:10])
                self.not_found.append(filename)
            time.sleep(random.uniform(
                ccfg["between_pages_delay_min"], ccfg["between_pages_delay_max"]))
        if newly_saved > 0:
            self.db.bump_created_at(artwork_id)
        self.db.sync_work_and_pages(artwork_id)
        self.reporter.log(f"作品 {artwork_id} 下载完成:{success_count}/{total}")
        return success_count, total

    def _download_ugoira_http(self, artwork_id: str, ctx, info,
                              *, force_overwrite: bool = False):
        """动图 (ugoira): `ugoira_meta` → 下 zip → 合成单个 gif → 落库。

        命名用**裸作品 ID** `{pid}.gif`(不是 `{pid}_p0.gif`): 动图是"一张会动的图",
        没有页序; `db.pixiv_natural_key` 认这种裸名(见其 docstring 里的 `119194561.gif`),
        一键整理的「文件名规范化」也会因自然键命中而**原样保留**它, 两边口径一致。
        库内仍是 1 页 (page_num=0), 所以页数/缺页/对账逻辑不用改。
        """
        from urllib.request import Request
        from utils.net import urlopen
        if self._should_abort():
            self._abort_now(artwork_id, 0, 1, 0)
        try:
            meta = self._http_get_json(
                f"https://www.pixiv.net/ajax/illust/{artwork_id}/ugoira_meta?lang=zh",
                timeout=25.0)
        except Exception as e:
            raise UgoiraError(f"ugoira_meta 请求失败: {str(e).splitlines()[0]}")
        body = meta.get("body") if isinstance(meta, dict) else None
        if (isinstance(meta, dict) and meta.get("error")) or not isinstance(body, dict):
            raise UgoiraError(
                f"ugoira_meta 接口错误: {str((meta or {}).get('message'))[:80]}")
        zip_url = str(body.get("originalSrc") or "").strip()
        frames = parse_ugoira_frames(body)
        if not zip_url or not frames:
            raise UgoiraError("ugoira_meta 未返回 zip 地址或帧信息")

        raw_name = str(info.author_name or "").strip()
        aname = self._clean_author_name(raw_name or "未知画师")
        aid = str(info.author_id or "0")
        # 规范夹名 (与一键整理同一套) —— 见 `_resolve_author` 同处注释
        save_dir = self._pick_author_dir(
            aid, aname, artwork_id,
            author_folder_name(self.db, aid, raw_name or aname),
            is_r18=getattr(info, "is_r18", None))
        os.makedirs(save_dir, exist_ok=True)
        self.reporter.log(f"🎞 动图作品, 图片将保存至:{save_dir}（共 {len(frames)} 帧）")
        # 权威元数据当场落库 (illust_type=2 也一并写入 → 图库「类型: 动图」筛得到)
        self._record_meta_info(artwork_id, info, aid, aname)
        self.db.init_pages(artwork_id, 1)

        filename = f"{artwork_id}.gif"
        out_path = os.path.join(save_dir, filename)
        pinfo = self.db.get_page_info(artwork_id, 0)
        if not force_overwrite and pinfo is not None \
                and int(pinfo.status or 0) == PSt.DONE and os.path.isfile(out_path):
            self.reporter.progress(1, 1, f"{filename} → 已存在")
            self._fire_progress("artwork_page", 1, 1, filename)
            return 1, 1
        # 强制重下: 旧 gif 先备份为 .bak (与静图通道同一套, 恢复入口在图库「备份管理」)
        if force_overwrite and os.path.isfile(out_path):
            bak_path = out_path + ".bak"
            try:
                if os.path.exists(bak_path):
                    os.remove(bak_path)
                shutil.copy2(out_path, bak_path)
                self.reporter.log(f"💾 旧版已备份 → {os.path.basename(bak_path)}")
            except Exception as e:
                self.reporter.warn(f"旧版备份失败(跳过备份): {str(e).splitlines()[0]}")

        self.reporter.progress(1, 1, f"下载动图 {len(frames)} 帧…")
        self._fire_progress("artwork_page", 1, 1, filename)
        try:
            with urlopen(Request(zip_url, headers=self._http_ctx()["headers"]),
                         timeout=120.0) as r:
                zip_bytes = r.read()
        except Exception as e:
            self.db.mark_page_status(artwork_id, 0, PSt.FAILED)
            self.not_found.append(filename)
            raise UgoiraError(f"ugoira zip 下载失败: {str(e).splitlines()[0]}")
        if not zip_bytes:
            self.db.mark_page_status(artwork_id, 0, PSt.FAILED)
            self.not_found.append(filename)
            raise UgoiraError("ugoira zip 下载为空")
        if self._should_abort():
            self._abort_now(artwork_id, 0, 1, 0)

        # 合成阶段: 只上报阶段文字 (不估算帧编码百分比), 合成失败仍走原下载失败路径
        self._fire_progress("artwork_page", 0, 1, "正在合成动图")
        wh = compose_ugoira_gif(zip_bytes, frames, out_path)
        if not wh:
            self.db.mark_page_status(artwork_id, 0, PSt.FAILED)
            self.not_found.append(filename)
            raise UgoiraError("ugoira 合成 gif 失败 (帧损坏或 zip 结构异常)")
        w, h = wh
        rel_path = Path(out_path).relative_to(self.save_folder).as_posix()
        fmd5 = fsize = None
        try:
            from utils.hashutil import md5_of
            fmd5 = md5_of(out_path)
            fsize = os.path.getsize(out_path)
        except Exception:
            pass
        self.db.mark_page_status(artwork_id, 0, PSt.DONE, rel_path, md5=fmd5,
                                 width=w, height=h, file_size_bytes=fsize)
        self.db.bump_created_at(artwork_id)
        self.db.sync_work_and_pages(artwork_id)
        self.reporter.log(f"🎞 动图合成完成:{filename} ({len(frames)} 帧, {w}×{h})")
        return 1, 1

    def _download_browser(self, artwork_id, author_id=None, author_name=None,
                          force_overwrite: bool = False):
        # 浏览器兜底: 导航前必须先注入 cookies (购物车链路以前漏掉这步, headless
        # 浏览器全程未登录, R18 作品一张图都拿不到 —— 2026-09-16 三任务连败事故)。
        if not self.login_ok:
            self.login_ok = self.load_cookies_and_login()
        url = f"https://www.pixiv.net/artworks/{artwork_id}"
        self.driver.get(url)

        # 检测作品是否已删除/不存在
        try:
            body = self.wait.until(
                EC.presence_of_element_located((By.TAG_NAME, "h1"))
            )
            body_text = body.text.lower()
            error_keywords = [
                "no content found", "尚无此页", "此页面不存在",
                "作品不存在", "コンテンツが見つかりません",
            ]
            if any(kw in body_text for kw in error_keywords):
                self.reporter.warn("作品已删除或不存在")
                self.db.change_work_status(artwork_id, WSt.ERROR)
                return 0, 0
        except NoSuchElementException:
            pass

        # 尝试点击"查看全部"按钮(多页漫画)
        pages = 1
        try:
            self.reporter.log("开始检测,请稍等...")
            time.sleep(1)
            pages_count = self.wait.until(
                EC.presence_of_element_located((By.XPATH, self.xp["pages_count"]))
            )
            pages = int(self.driver.execute_script(
                'return arguments[0].childNodes[1].textContent.trim();', pages_count
            ).strip('/'))
            button = self.wait.until(
                EC.presence_of_element_located((By.XPATH, self.xp["more_img_button"]))
            )
            button.click()
            self.reporter.log("检测到多页作品,点击查看全部")
            time.sleep(1)
        except Exception as e:
            self.reporter.log(f"未检测到多页作品: {str(e).splitlines()[0]}")

        urls = set(self.wait_for_images(max_wait=5))
        if not urls:
            # 未登录时 pixiv 对 R-18 等受限作品**根本不渲染图片**(页面只给「登录后浏览」),
            # 任何放宽正则的兜底都救不回来 → 直接点破原因, 别让用户以为是作品坏了
            # (2026-09-16 实测: 三个 R-18 作品连续失败, 根因就是登录失效)
            if not self.login_ok:
                self.reporter.warn("❗ 当前未登录或登录已失效：R-18 等受限作品无法浏览。"
                                   "请到「设置」页重新登录后再下载")
            else:
                self.reporter.warn("❗ 未检测到图片链接,跳过此作品")
            return 0, 0
        # 页数以「实际抓到的原图链接」为准: 「查看全部」点击失败/R18 页面
        # 结构差异时, DOM 声明的 pages 可能只有 1, 但 urls 才是真实可下载的页;
        # 反之 urls 少于声明时保留声明值, 交给下面的 URL 规律补齐。
        if len(urls) > pages:
            self.reporter.warn(f"DOM 声明 {pages} 页但抓到 {len(urls)} 个原图链接, 以实际为准")
            pages = len(urls)
        elif len(urls) < pages:
            self.reporter.warn(f"⚠ 仅展开 {len(urls)}/{pages} 页, 稍后按 URL 规律补齐缺页")
        self.db.init_pages(artwork_id, pages)

        # 作者文件夹: 队列项 → DB → ajax → DOM 兜底; 全失败落占位夹, 不散落根目录
        # ajax 通道同时带回标题/标签/封面等权威元数据, 随下载当场落库, 不等自检
        author_id, author_name, save_dir, meta_info = self._resolve_author(
            artwork_id, author_id=author_id, author_name=author_name)
        os.makedirs(save_dir, exist_ok=True)
        self.reporter.log(f"图片将保存至:{save_dir}")
        self._record_meta_info(artwork_id, meta_info, author_id, author_name)
        if meta_info is not None:
            self.reporter.log(f"📝 元数据已记录: 《{meta_info.title}》/ {meta_info.author_name}")

        # 多页漏检兜底: DOM 未完全展开时按 URL 规律补齐缺页。
        # 权威页数取「DOM 声明」与「ajax 元数据」的较大者 —— R18 未登录时 ajax 可能
        # 拿不到元数据, 此时仍需依赖 DOM 的 pages 补齐 (2026-09-15: 109766089 只下到 p1)。
        _want_pages = max(int(pages or 0), int(getattr(meta_info, "page_count", 0) or 0))
        if _want_pages > len(urls):
            _extra = build_missing_page_urls(urls, _want_pages)
            if _extra:
                _had = len(urls)
                urls |= _extra
                # INSERT OR IGNORE: 已存在的占位/完成行不受影响, 只补缺页行+总页数
                self.db.init_pages(artwork_id, _want_pages)
                self.reporter.warn(
                    f"⚠ 页面仅展开 {_had}/{_want_pages} 页，已按 URL 规律补齐 {len(_extra)} 个缺页")

        # 下载
        ccfg = self.config["crawler"]
        success_count = 0
        newly_saved = 0          # 本次真正新落盘的页数 (不含「已存在」跳过)
        url_list = list(urls)
        total = len(url_list)
        for i, img_url in enumerate(url_list, 1):
            if self._should_abort():
                self._abort_now(artwork_id, i - 1, total, newly_saved)
            url, aid, page_str, ext = img_url
            page_num = int(page_str)
            filename = f"{aid}_p{page_num}.{ext}"
            save_path = os.path.join(save_dir, filename)

            pinfo = self.db.get_page_info(aid, page_num)
            if not force_overwrite and pinfo is not None \
                    and int(pinfo.status or 0) == PSt.DONE:
                self.reporter.progress(i, total, f"{filename} → 已存在")
                self._fire_progress("artwork_page", i, total, filename[:10])
                success_count += 1
                continue
            # 4B-1 复验返修: 新下载只写应用暂存文件; 覆盖备份在收尾里按真实旧路径做
            final_path = os.path.join(save_dir, filename)
            staging = self._staging_path("." + ext)
            if self.download_with_retry(url, staging):
                # 入库统一: 静态图转 PNG + 写转换记录 (动图保持原格式)
                ok, conv = self._finalize_downloaded_page(
                    aid, page_num, staging, final_path,
                    old_rel=(pinfo.local_path if pinfo is not None else None),
                    force_overwrite=force_overwrite)
                if ok:
                    filename = os.path.basename(conv["current_path"])
                    self.reporter.progress(i, total, f"{filename} → 成功")
                    success_count += 1
                    newly_saved += 1
                else:
                    why = (conv.get("note")
                           or ("同页冲突，保留现有文件" if conv.get("conflict")
                               else "失败"))
                    self.reporter.progress(i, total, f"{filename} → {why}")
                    self.last_failure_notes.append(f"{filename}: {why}")
                    self.not_found.append(filename)
                self._fire_progress("artwork_page", i, total, filename[:10])
            else:
                self._cleanup_staging(staging)
                old_file = self._old_page_file(
                    pinfo.local_path if pinfo is not None else None)
                from core.image_format import is_readable
                if not (old_file and is_readable(old_file)):
                    try:
                        self.db.mark_page_status(aid, page_num, PSt.FAILED)
                    except Exception as e:
                        self.reporter.warn(
                            f"⚠ 标记失败状态失败: {str(e).splitlines()[0]}")
                self.reporter.progress(i, total, f"{filename} → 失败")
                self.last_failure_notes.append(f"{filename}: 下载失败")
                self._fire_progress("artwork_page", i, total, filename[:10])
                self.not_found.append(filename)

            time.sleep(random.uniform(
                ccfg["between_pages_delay_min"], ccfg["between_pages_delay_max"]
            ))
        # 有新图片真正落盘 → 刷新「入库时间」, 让刚下载的作品按「入库排序」排到最前。
        # 放在 sync 之前: 即使本次只下到部分页(状态仍是 NOT_YET, R18 常见),
        # 也已经"有图入库", 排序必须反映出来。
        if newly_saved > 0:
            self.db.bump_created_at(artwork_id)
        self.db.sync_work_and_pages(artwork_id)
        self.reporter.log(f"作品 {artwork_id} 下载完成:{success_count}/{total}")
        return success_count, total

    def wait_for_images(self, max_wait=5):
        """等待原图链接出现,最多等 max_wait 秒。

        登录态优先抓 img-original(原图); 抓不到时**回退**抓 img-master —— 2026-09-15
        实测「登录 cookie 已失效但 login_ok 仍为 True」时, 只按 original 正则找会一张都
        匹配不到, 最终「未检测到图片链接,跳过此作品」却把任务标成 done。"""
        self.reporter.log("正在等待图片链接加载...")
        pat_original = (r'(https?://i\.pximg\.net/img-original/img/[^\s"]+/'
                        r'(?P<id>\d+)_p(?P<page>\d+)\.(?P<ext>jpg|png))')
        pat_master = (r'(https?://i\.pximg\.net/img-master/img/[^\s"]+/'
                      r'(?P<id>\d+)_p(?P<page>\d+)_master1200\.(?P<ext>jpg|png))')
        for _ in range(max_wait):
            html = self.driver.page_source
            urls = set(re.findall(pat_original, html)) if self.login_ok else set()
            if not urls:                      # 登录失效/未登录: 退回缩略原图, 别空手而归
                urls = set(re.findall(pat_master, html))
            if urls:
                self.reporter.log(f"成功!检测到 {len(urls)} 张图片")
                return urls
            time.sleep(1)
        self.reporter.warn("等待图片链接超时")
        return []

    def download_with_retry(self, url: str, save_path: str) -> bool:
        """带重试的图片下载。返回 True 成功 / False 最终失败。"""
        ccfg = self.config["crawler"]
        retries = ccfg["max_retries"]
        min_size = ccfg["min_file_size"]

        # 下载也走系统代理 (utils.net): 直连 i.pximg.net 在国内会超时。
        # 不用 install_opener —— 那是全局副作用, 会把本机 127.0.0.1 的请求
        # (托盘/小控件/健康检查) 也塞进代理。
        from utils.net import build_opener
        opener = build_opener({"Referer": "https://www.pixiv.net/"})

        for attempt in range(retries + 1):
            try:
                with opener.open(url, timeout=30) as r, open(save_path, "wb") as f:
                    shutil.copyfileobj(r, f)
                # 简单验证文件是否真的下载成功(避免 403 返回 HTML)
                if os.path.getsize(save_path) < min_size:
                    raise Exception("文件太小,可能是 403 页面")
                return True
            except Exception as e:
                fname = os.path.basename(save_path)
                self.reporter.warn(f"{fname} → 下载失败(第 {attempt + 1} 次): {str(e).splitlines()[0]}")
                if attempt < retries:
                    time.sleep(random.uniform(1, 3))
                else:
                    if os.path.exists(save_path):
                        try:
                            os.remove(save_path)
                        except Exception:
                            pass
                    return False
        return False

    def run(self):
        """批量下载"""
        self.db.update_pages_from_output(self.save_folder)  # 先同步现有文件
        input_ids, _ = self.db.add_works_from_input(self.input_folder)
        pending_tasks = self.db.get_pending_tasks()

        self.load_cookies_and_login()

        if self.download_from_list:
            seen = set()
            ids = []
            if self.img_list_file.exists():
                with open(self.img_list_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        match = re.search(r'(\d+)', line)
                        if match:
                            artwork_id = match.group(1)
                            if artwork_id not in seen:
                                seen.add(artwork_id)
                                ids.append(artwork_id)
        else:
            ids = list(pending_tasks)

        self.reporter.log(f"从输入目录扫描到的作品 ID 个数: {len(input_ids)}")
        self.reporter.log(f"待下载的作品个数: {len(ids)}")

        ccfg = self.config["crawler"]
        success = 0
        total = 0
        total_ids = len(ids)
        for idx, aid in enumerate(ids, 1):
            self.reporter.progress(idx, total_ids, f"下载 {aid}")
            self._fire_progress("artwork_in_list", idx, total_ids, aid)
            self.reporter.log(f"{'=' * 20} {aid} {'=' * 20}")
            try:
                success_count, artwork_num = self.download_artwork(aid)
                success += success_count
                total += artwork_num
                if artwork_num == 0:
                    self.not_found.append(aid)
            except DownloadCancelled:
                # 中止要终止整个列表, 不能只把这一项记进 not_found 继续下一项
                raise
            except Exception as e:
                self.reporter.error(f"{aid} → 下载失败:{str(e).splitlines()[0]}")
                self.not_found.append(aid)
            if self.reporter.is_cancelled():
                self.reporter.warn("用户已取消,停止后续下载")
                break
            time.sleep(random.uniform(
                ccfg["between_works_delay_min"], ccfg["between_works_delay_max"]
            ))  # 作品之间稍作间隔,防风控

        self.reporter.log(f"全部下载完成!{success} / {total}")
        if self.not_found:
            self.reporter.warn(f"失败或超时项共 {len(self.not_found)}:")
            for name in self.not_found:
                self.reporter.warn(name)
            with open(self.failed_img_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(self.not_found))

    # ---- P3: 拆分的原子 API (供 download_engine / GUI 查询调用) ----
    def collect_author_artworks(self, user_id: str) -> list[str]:
        """打开作者插画主页, 收集当前可见作品 artwork_id 列表 (去重保序)。"""
        url = f"https://www.pixiv.net/users/{user_id}/illustrations"
        self.reporter.log(f"打开作者主页收集作品列表: {url}")
        self.load_cookies_and_login()
        self.driver.get(url)
        time.sleep(1.5)
        # 扫 artwork 锚点
        seen: set[str] = set()
        ids: list[str] = []
        try:
            anchors = self.driver.find_elements(By.XPATH,
                "//a[contains(@href,'/artworks/')]")
            for a in anchors:
                href = a.get_attribute("href") or ""
                m = re.search(r'/artworks/(\d+)', href)
                if not m: continue
                aid = m.group(1)
                if aid in seen: continue
                seen.add(aid); ids.append(aid)
        except Exception as e:
            self.reporter.warn(f"解析作者作品列表出错:{str(e).splitlines()[0]}")
        self.reporter.log(f"作者 {user_id}: 收集到 {len(ids)} 个作品 ID")
        return ids

    def download_author_all(self, user_id: str, author_name: "str | None" = None):
        """下载指定作者主页的全部作品(当前可见页)。"""
        ids = self.collect_author_artworks(user_id)
        # 作者名: 队列项传入 → DB authors 表 → ajax/user 接口; 一次解析, 逐张透传
        name = (author_name or "").strip()
        if not name or self._clean_author_name(name) == "未知画师":
            try:
                arow = self.db.get_author(str(user_id)) or {}
                if arow.get("name"):
                    name = arow["name"]
            except Exception:
                pass
        if not name or self._clean_author_name(name) == "未知画师":
            body = self._ajax_body(f"https://www.pixiv.net/ajax/user/{user_id}?lang=zh")
            if body and body.get("name"):
                name = str(body["name"])
        self.download_artwork_list(
            ids, scope_label=f"作者 {user_id}",
            author_id=str(user_id), author_name=name or None)

    def download_artwork_list(self, ids: list[str], *, scope_label: str = "",
                              author_id=None, author_name=None):
        """统一循环: 跑一组 artwork_ids, 支持取消, 防风控间隔, 失败落 not_found。"""
        self.load_cookies_and_login()
        self.reporter.log(f"开始下载作品列表: {scope_label or len(ids)} 个 ID")
        ccfg = self.config["crawler"]
        success = 0
        total = 0
        total_ids = len(ids)
        for idx, aid in enumerate(ids, 1):
            if self.reporter.is_cancelled():
                self.reporter.warn("用户已取消,停止后续下载")
                break
            self.reporter.progress(idx, total_ids, f"下载 {aid}")
            self._fire_progress("artwork_in_list", idx, total_ids, aid)
            self.reporter.log(f"{'=' * 16} {aid} ({idx}/{total_ids}) {'=' * 16}")
            try:
                sc, tc = self.download_artwork(
                    aid, author_id=author_id, author_name=author_name)
                success += sc
                total += tc
                if tc == 0:
                    self.not_found.append(aid)
            except DownloadCancelled:
                # 中止要终止整个列表 (同 run()), 不能记进 not_found 继续下一项
                raise
            except Exception as e:
                self.reporter.error(f"{aid} → 下载失败:{str(e).splitlines()[0]}")
                self.not_found.append(aid)
            time.sleep(random.uniform(
                ccfg["between_works_delay_min"], ccfg["between_works_delay_max"]
            ))
        self.reporter.log(f"下载任务{scope_label}完成!{success} / {total}")
        if self.not_found:
            with open(self.failed_img_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(self.not_found))

    def close(self):
        # 2026-09-15 事故: 旧实现在这里对**整个图库**跑 update_pages_from_output +
        # batch_sync_all —— web 购物车每执行一个任务,with 块退出就全库扫 1476 个作品一次,
        # 3 个任务并发 = 3 次全库扫描互踩 (假作品事故的放大器); batch_sync_all 里还带着
        # 「无 done 页即 DELETE 作品」的逻辑, 对无关作品全生效, 是定时炸弹。
        # 现在每个作品下载完在 download_artwork() 末尾已当场 sync_work_and_pages(aid),
        # 页状态也是下载成功即 mark_page_status —— **不需要任何全库收尾**。
        # 走 PixivDatabase.close(): 只关 conn 不会释放图库读锁, 恢复数据库会一直等到超时
        self.db.close()
        if getattr(self, '_driver', None) is not None:
            try:
                self._driver.quit()
                self.reporter.log("浏览器驱动已关闭。")
            except Exception as e:
                self.reporter.error(f"关闭浏览器驱动时出错: {e}")
        self.reporter.log("程序已停止运行。")
