# core/reverse_search.py
"""
通过 saucenao.com 以图搜源,识别后按画师归类移动图片。
从旧 reverse_search.py 迁移,改造点:
- __init__ 改调 make_browser + 补 __enter__/__exit__(原只有 try/finally)
- XPath 从 resources/xpaths.json 的 saucenao 节读
- 临时目录走 paths.user_data('temp_upload')
- attempts/delays/compress_max_size/max_error_count 读 config
- print → reporter
- 修正 execute_script 调用对象(parent.execute_script → driver.execute_script)
"""
import json
import os
import re
import time
import shutil
import random
from pathlib import Path

from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC

from utils import paths
from utils.author_naming import author_folder_name, clean_author_name
from utils.config import load_config
from utils.reporter import Reporter, PrintReporter
from utils.browser import make_browser
from utils.image import compress_image_for_upload
from db.store import pixiv_natural_key


# ========== SauceNAO 结果来源识别 (2026-09-10 扩展: 不止 pixiv/推特) ==========
# 纯函数, 供 detail 反搜与冒烟离线测试复用。识别: pixiv / twitter(x) / artstation /
# deviantart / danbooru / nico静画 / skeb + booru 族 + 域名白名单兜底
# (2026-09-13 精简: 去掉 nijie / pawoo / furaffinity / e621)
# —— 防止把任何 SauceNAO 命中站点的作者误判成盗图 p 站用户。
_PIXIV_LINK = r'https?://www\.pixiv\.net/.*?(?:illust_id|id|artworks|users)(?:=|/)?(\d+)'
_TW_STATUS = r'https?://(?:www\.)?(?:twitter\.com|x\.com)/(?:[^/]+/status/|status/)(\d+)'
_TW_USER = r'https?://(?:www\.)?(?:twitter\.com|x\.com)/([^/?#]+)'
_ARTSTATION = r'https?://(?:www\.)?artstation\.com/(?:artwork/)?([\w-]+)'
_ARTSTATION_USER = r'https?://(?:www\.)?artstation\.com/([\w-]+)/?$'
# ArtStation 作品页的 hash_id (读官方 JSON 接口用, 取当前作者名)
_ARTSTATION_ARTWORK = re.compile(r'artstation\.com/artwork/([\w-]+)', re.I)
_DEVIANTART = r'https?://(?:www\.)?deviantart\.com/([\w-]+)/art/[^/]+-(\d+)'
_DEVIANTART_VIEW = r'https?://(?:www\.)?deviantart\.com/view/(\d+)'
_DANBOORU = r'https?://(?:[a-z0-9-]+\.)*danbooru\.(?:com|donmai\.us)/posts?/(\d+)'
# 常用画师站精确模式 (2026-09-10 三扩; 2026-09-13 精简: 去掉 nijie/pawoo/furaffinity)
_NICO = r'https?://seiga\.nicovideo\.jp/seiga/(im\d+)'
_SKEB = r'https?://skeb\.jp/@([^/]+)(?:/works/(\d+))?'
# 泛化兜底: 其余 SauceNAO 命中站点按域名归类 (booru 族尽力提取作品 ID, 其余只归类)
_SAUCE_HOSTS = [
    # (site, host 片段, 是否尽力提取作品 ID)
    ("yandere", "yande.re", True),
    ("konachan", "konachan", True),
    ("gelbooru", "gelbooru.com", True),
    ("sankaku", "sankakucomplex.com", True),
    ("safebooru", "safebooru", True),
    ("zerochan", "zerochan.net", True),
    ("animepictures", "anime-pictures.net", True),
    ("drawr", "drawr.net", False),
    ("shutterstock", "shutterstock.com", False),
    ("mangadex", "mangadex.org", False),
    ("fakku", "fakku.net", False),
    ("kemono", "kemono.party", False),
    ("nhentai", "nhentai.net", False),
    ("ehentai", "e-hentai.org", False),
    ("bcy", "bcy.net", False),
    ("medibang", "medibang.com", False),
    ("portalgraphics", "portalgraphics.net", False),
    ("madokami", "madokami.com", False),
    ("doujinshi", "doujinshi.org", False),
    ("2dmarket", "2d-market.com", False),
]


def _pick_booru_id(u: str) -> "str | None":
    """booru 族作品 ID 尽力提取: /posts/N · /post/show/N · /view/N · ?id=N。"""
    for pat in (r'/(?:posts?|post|artworks?)(?:/show|/view)?/(\d+)',
                r'[?&](?:id|p)=(\d+)',
                r'/(?:view|show)/(\d+)'):
        m = re.search(pat, u)
        if m:
            return m.group(1)
    return None


def _clean_dirname(name: str) -> str:
    """清洗为合法文件夹名片段 (非法字符→`_`, 与一键整理/下载器同一套规则, 2026-09-21 治本),
    空则「未知画师」。以前这里「删掉」非法字符, 与一键整理对不上会摆出两个夹。"""
    return clean_author_name(name)


def detect_source_links(hrefs: "list[str]") -> "dict | None":
    """从一组 URL 里识别来源站点, 返回 {site, url, artwork_id, author_id, handle} 或 None。
    site ∈ pixiv / twitter / artstation / deviantart / danbooru / niconico / skeb /
    yandere / konachan / gelbooru / sankaku / safebooru / zerochan / animepictures /
    其余域名白名单 (2026-09-13 精简: 去掉 nijie/pawoo/furaffinity/e621)。
    pixiv 优先, 未知名返回 None。"""
    for h in hrefs:
        u = (h or "").strip()
        if not u or not u.startswith("http"):
            continue
        # pixiv 作品/作者
        m = re.search(_PIXIV_LINK, u)
        if m:
            if "/users/" in u:
                return {"site": "pixiv", "url": u, "artwork_id": None,
                        "author_id": m.group(1), "handle": ""}
            return {"site": "pixiv", "url": u, "artwork_id": m.group(1),
                    "author_id": None, "handle": ""}
        # 推特 / X
        m = re.search(_TW_STATUS, u)
        if m:
            handle = ""
            mu = re.search(_TW_USER, u)
            if mu and mu.group(1) not in ("status", "home", "i", "share"):
                handle = mu.group(1)
            return {"site": "twitter", "url": f"https://twitter.com/status/{m.group(1)}",
                    "artwork_id": None, "author_id": None, "handle": handle}
        # ArtStation (artwork 或作者主页)
        m = re.search(_ARTSTATION, u)
        if m and ("artstation.com" in u):
            handle = ""
            mu = re.search(_ARTSTATION_USER, u)
            if mu and mu.group(1) not in ("artwork", "artworks"):
                handle = mu.group(1)
            return {"site": "artstation", "url": u, "artwork_id": None,
                    "author_id": None, "handle": handle}
        # DeviantArt (作品页 / view)
        m = re.search(_DEVIANTART, u)
        if m:
            return {"site": "deviantart", "url": u, "artwork_id": None,
                    "author_id": None, "handle": m.group(1)}
        m = re.search(_DEVIANTART_VIEW, u)
        if m:
            return {"site": "deviantart", "url": u, "artwork_id": None,
                    "author_id": None, "handle": ""}
        # Danbooru
        m = re.search(_DANBOORU, u)
        if m:
            return {"site": "danbooru", "url": u, "artwork_id": m.group(1),
                    "author_id": None, "handle": ""}
        # 常用画师站精确解析 (2026-09-13 精简: 去掉 nijie/pawoo/furaffinity)
        m = re.search(_NICO, u)
        if m:
            return {"site": "niconico", "url": u, "artwork_id": m.group(1),
                    "author_id": None, "handle": ""}
        m = re.search(_SKEB, u)
        if m:
            return {"site": "skeb", "url": u, "artwork_id": m.group(2),
                    "author_id": None, "handle": m.group(1)}
        # 泛化兜底: booru 族 + 其余 SauceNAO 站点按域名归类
        for site, host, want_id in _SAUCE_HOSTS:
            if host in u:
                return {"site": site, "url": u,
                        "artwork_id": _pick_booru_id(u) if want_id else None,
                        "author_id": None, "handle": ""}
    return None


class ReverseSearch:
    def __init__(self, search_path: "Path | str", headless: "bool | None" = None,
                 reporter: "Reporter | None" = None, config: "dict | None" = None,
                 archive_path: "Path | str | None" = None):
        self.config = config or load_config()
        self.reporter = reporter or PrintReporter()
        # search_path = 待反搜输入目录; archive_path = 识别成功后的图库归档根 (img/)
        self.search_path = Path(search_path)
        self.search_path.mkdir(parents=True, exist_ok=True)
        self.archive_path = Path(archive_path) if archive_path \
            else paths.user_data(self.config["paths"]["img_subdir"])
        self.archive_path.mkdir(parents=True, exist_ok=True)

        with open(paths.resource("xpaths.json"), encoding="utf-8") as f:
            xp = json.load(f)
        self.xp_saucenao = xp["saucenao"]

        explicit = self.config["browser"]["explicit_wait_reverse"]
        self.driver, self.wait = make_browser(
            headless=headless, explicit_wait=explicit,
            reporter=self.reporter, config=self.config
        )
        self.error_count = 0
        # 反搜结果清单 (供图库入库/前端结果面板):
        # success=[{file,artwork_id,dest,trust}] failed=[{file,reason}]
        # conflict=[{file,filename_id,saucenao_id}] (识别与文件名矛盾, 不自动处理)
        # in_library=[{file,artwork_id}] (本地指纹命中, 图库里已有同一张)
        # skipped=[{file,artwork_id,reason}] (目标已有同名文件, 按「已存在」选项跳过)
        self.results = {"success": [], "failed": [],
                        "conflict": [], "in_library": [],
                        "skipped": []}
        try:
            from db.store import PixivDatabase
            self.db = PixivDatabase(reporter=self.reporter)
        except Exception:
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def search_by_image(self, img_path: Path, detail: bool = False):
        """上传一张图到 SauceNAO 反搜来源。

        detail=False (默认, 兼容旧调用): 返回第一个有效 Pixiv 信息 (artwork_id, author_name, author_id)。
        detail=True  (来源治理用): 返回 dict {site:'pixiv'|'twitter', artwork_id, author_name,
                    author_id, tweet_url, handle} —— 无 pixiv 时识别推特来源 (SauceNAO 支持
                    推特图反搜), 全无则 site=None。""
        """
        scfg = self.config["reverse_search"]
        self.driver.get("https://saucenao.com/")
        upload_path = compress_image_for_upload(
            img_path, max_size=scfg["compress_max_size"],
            temp_dir=paths.user_data("temp_upload"),
        )
        upload_input = self.wait.until(
            EC.presence_of_element_located((By.XPATH, self.xp_saucenao["input_file"]))
        )
        upload_input.send_keys(str(upload_path.resolve()))
        time.sleep(1)
        self.reporter.log("文件上传完成。")
        search_button = self.wait.until(
            EC.element_to_be_clickable((By.XPATH, self.xp_saucenao["click_to_search"]))
        )
        search_button.click()
        self.reporter.log("点击检索")

        try:
            max_retries = scfg["max_attempts"]
            retry_interval = scfg["retry_interval_sec"]
            high_results = None
            for attempt in range(max_retries):
                time.sleep(retry_interval)
                try:
                    if self.driver.find_elements(By.XPATH, '//div[@id="result-hidden-notification"]'):
                        high_results = self.driver.find_elements(
                            By.XPATH, '//div[@id="result-hidden-notification"]/preceding::div[@class="result"]'
                        )
                    else:
                        high_results = self.driver.find_elements(By.XPATH, '//*[@class="result"]')

                    if high_results:
                        self.reporter.log(f"第 {attempt + 1} 次尝试:发现 {len(high_results)} 个高匹配结果。")
                        self.driver.execute_script("window.stop();")  # 找到结果,停止加载
                        break
                    else:
                        self.reporter.log(f"第 {attempt + 1} 次尝试:暂无结果,继续等待加载...")

                except Exception as e:
                    self.reporter.warn(f"第 {attempt + 1} 次尝试出错: {str(e).splitlines()[0]},继续等待...")

            if not high_results:
                self.error_count += 1
                self.reporter.error("❌ 所有尝试后仍无高匹配结果")
                return (None, None, None) if not detail else {
                    "site": None, "artwork_id": None, "author_name": None,
                    "author_id": None, "tweet_url": "", "handle": ""}

            for idx, result in enumerate(high_results, 1):
                self.reporter.log(f"检查第 {idx} 栏结果...")
                try:
                    content = result.find_element(By.XPATH, './/div[@class="resultcontentcolumn"]')
                    artwork_strong = content.find_element(By.XPATH, './/strong[1]')
                    artwork_match = self.driver.execute_script(
                        'return arguments[0].nextSibling', artwork_strong
                    )
                    author_strong = content.find_element(By.XPATH, './/strong[2]')
                    author_match = self.driver.execute_script(
                        'return arguments[0].nextSibling', author_strong
                    )

                    # 单栏解析: 识别 pixiv 作品/作者 (原逻辑); 非 pixiv 来源由
                    # detect_source_links 统一识别 (twitter/artstation/deviantart/danbooru, 2026-09-10)
                    pattern = r'https?://www\.pixiv\.net/.*?(?:illust_id|id|artworks|users)(?:=|/)?(\d+)(?:\s|$)'

                    artwork_id = None
                    if artwork_match and isinstance(artwork_match, WebElement):
                        href = artwork_match.get_attribute("href") or ""
                        m = re.search(pattern, href)
                        artwork_id = m.group(1) if m else None
                        self.reporter.log(f"第{idx}栏作品ID:{artwork_match.text}")

                    author_id = None
                    text = None
                    if isinstance(author_match, WebElement):
                        href = author_match.get_attribute("href") or ""
                        text = author_match.text.strip()
                        m = re.search(pattern, href)
                        author_id = m.group(1) if m else None
                    self.reporter.log(f"第{idx}栏作者名称及ID:{text}_{author_id}")
                    # 与一键整理/下载器同一套非法字符规则 (2026-09-21 治本), 空名回退「未知画师」
                    author_name = clean_author_name(text or "未知画师")

                    if detail:
                        if artwork_id or author_id:
                            return {"site": "pixiv", "artwork_id": artwork_id,
                                    "author_name": author_name, "author_id": author_id,
                                    "url": "", "handle": ""}
                        # 非 pixiv 来源: 从结果栏全部链接识别 (twitter/artstation/deviantart/danbooru)
                        try:
                            hrefs = [a.get_attribute("href") or ""
                                     for a in content.find_elements(By.XPATH, './/a[@href]')]
                        except Exception:
                            hrefs = []
                        det = detect_source_links(hrefs)
                        if det:
                            if det.get("site") != "pixiv":
                                # 外来源作者以**源网页当前值**为准 (不再直接采信
                                # SauceNAO 结果卡上的索引名 —— 作者改过名它就是旧名)
                                self._enrich_foreign_author(det, author_name)
                            else:
                                det["author_name"] = author_name
                            return det
                        continue  # 该栏无有效来源, 看下一栏

                    if artwork_id:
                        self.reporter.log(f"✅ 根据作品ID选中第{idx}栏结果")
                        return artwork_id, author_name, author_id
                    elif author_id:
                        self.reporter.log(f"🟡 根据作者ID选中第{idx}栏结果")
                        return artwork_id, author_name, author_id

                except Exception as e:
                    self.reporter.warn(f"第{idx}栏解析出错: {str(e).splitlines()[0]}")
                    continue
            self.reporter.error("❌ 所有高匹配结果均无有效 Pixiv 信息")
            return (None, None, None) if not detail else {
                "site": None, "artwork_id": None, "author_name": None,
                "author_id": None, "tweet_url": "", "handle": "",
                "author_src": None}

        except TimeoutException:
            self.error_count += 1
            self.reporter.error("检索超时")
        except NoSuchElementException:
            self.error_count += 1
            self.reporter.error("检索失败:页面未给出结果")

        return (None, None, None) if not detail else {
            "site": None, "artwork_id": None, "author_name": None,
            "author_id": None, "tweet_url": "", "handle": "",
            "author_src": None}

    REV_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    @classmethod
    def _list_images(cls, search_path: Path, files=None):
        """列待反搜图片: files=None 扫目录全部; 否则按白名单 basename 过滤 (防路径穿越)。"""
        if files is None:
            return [p for p in sorted(search_path.iterdir())
                    if p.is_file() and p.suffix.lower() in cls.REV_IMG_EXT]
        out = []
        for name in files or []:
            base = os.path.basename(str(name).replace("\\", "/"))
            if not base:
                continue
            p = search_path / base
            if p.is_file() and p.suffix.lower() in cls.REV_IMG_EXT:
                out.append(p)
        return out

    def _ajax_body(self, url: str):
        """导航到 pixiv ajax URL 取 JSON body, 失败/error 返回 None。"""
        try:
            self.driver.get(url)
            data = json.loads(self.driver.find_element(By.TAG_NAME, "body").text)
            if data.get("error"):
                return None
            return data.get("body")
        except Exception:
            return None

    def _artstation_author(self, url: str) -> "tuple[str, str] | None":
        """读 ArtStation 官方项目 JSON 拿**当前**作者: 返回 (显示名, 账号), 拿不到 None。

        为什么必须读网页: SauceNAO 结果卡上的作者名是**它自己的索引
        快照** —— 作者改过名, 卡片就永远是旧名。实例: 卡片写「ying yi」, 而作品页
        (artstation.com/artwork/DAJVOE) 当前作者是「qing ying」(账号 qing0ying0),
        连 CDN 图片文件名都还留着 `ying-yi-*` 的旧痕迹。与 pixiv 的 `_ajax_body` 同思路:
        用同一个浏览器会话直读官方数据, 而不是信第三方索引。
        Cloudflare 会拦冷会话 (返回 HTML 挑战页「请稍候…」) → 检测到就等几秒重试一次。"""
        m = _ARTSTATION_ARTWORK.search(url or "")
        if not m:
            return None
        api = f"https://www.artstation.com/projects/{m.group(1)}.json"
        for attempt in range(2):
            try:
                self.driver.get(api)
                body = (self.driver.find_element(By.TAG_NAME, "body").text or "").strip()
                if not body.startswith("{"):
                    if attempt == 0:            # 挑战页 (HTML) → 等放行后重试一次
                        time.sleep(3)
                        continue
                    return None
                u = json.loads(body).get("user") or {}
                nm = str(u.get("full_name") or "").strip()
                un = str(u.get("username") or "").strip()
                if nm or un:
                    return (nm or un, un)
                return None
            except Exception:
                if attempt == 0:
                    time.sleep(1)
                    continue
                return None
        return None

    def _enrich_foreign_author(self, det: dict, card_name: str) -> None:
        """定外来源的作者名 (与 pixiv 一样"严格记录网页上的当前作者"):
          ① 源网页当前作者 —— 读得到就用 (最权威, 能纠正改名);
          ② URL 里的账号 handle —— 当前账号, 稳定标识;
          ③ SauceNAO 结果卡上的名字 —— 第三方索引快照, **可能是旧名**, 明确标成
             `author_src='saucenao_index'`, 由界面提示用户核对; 绝不静悄悄当事实写库。"""
        live = None
        if det.get("site") == "artstation":
            live = self._artstation_author(det.get("url") or "")
        if live:
            det["author_name"] = live[0]
            det["handle"] = live[1] or det.get("handle") or ""
            det["author_src"] = "page"
            return
        hd = (det.get("handle") or "").strip()
        if hd:
            det["author_name"] = hd
            det["author_src"] = "handle"
            return
        det["author_name"] = card_name
        det["author_src"] = "saucenao_index"

    def _record_artwork_meta(self, artwork_id):
        """归档成功后从 pixiv 最权威接口 (ajax/illust) 抓全量元数据当场落库:
        标题/标签/作者/封面/R18。失败不阻断 (自检仍兜底)。"""
        if self.db is None or not artwork_id:
            return
        try:
            body = self._ajax_body(
                f"https://www.pixiv.net/ajax/illust/{artwork_id}?lang=zh")
            if not body:
                return
            from core.search_client import SearchClient
            info = SearchClient._illust_body_to_info(body)
            if info is None:
                return
            self.db.upsert_artwork_metadata(
                str(artwork_id),
                title=info.title or None,
                author_id=info.author_id or None,
                author_name=info.author_name or None,
                illust_type=info.illust_type.value,
                tags=list(info.tags or []),
                publish_date=info.publish_date,
                is_r18=info.is_r18,
                thumb_url=(info.image_urls or [None])[0],
                page_count=info.page_count,
                ai_type=getattr(info, "ai_type", 0))
            self.reporter.log(
                f"📝 元数据已记录: 《{info.title}》/ {info.author_name or '?'}")
        except Exception as e:
            self.reporter.warn(f"元数据记录失败: {str(e).splitlines()[0]}")

    @staticmethod
    def _decide_identity(img_name: str, saucenao_id, db_page):
        """身份信任链 (SauceNAO 识别最不可靠, 绝不让它覆盖既有事实):
          ① db_page: 本地指纹(MD5)与图库某页字节一致 → 同一张图, 身份确定
          ② 文件名自然键 {作品ID}_p{页码}: 既有归属声明
             - 与 SauceNAO 一致   → 按 ID 归档 (双来源互证, 仍属参考级)
             - 与 SauceNAO 矛盾   → 冲突, 不归档不改名, 交人工确认
          ③ 文件名无 ID (截图/随便命名): 只能参考 SauceNAO, 按其 ID 命名但
             标记「未人工确认」, 用户可在结果面板一眼看出。
        返回 dict(action, artwork_id, page, trust, filename_id)。"""
        if db_page:
            return {"action": "in_library", "artwork_id": str(db_page.get("artwork_id")),
                    "page": db_page.get("page_num"), "trust": "hash", "filename_id": None}
        _nkey = pixiv_natural_key(img_name)
        filename_id = _nkey[0] if _nkey else None
        if filename_id:
            if saucenao_id and str(saucenao_id) == filename_id:
                return {"action": "archive", "artwork_id": filename_id,
                        "page": int(_nkey[1]), "trust": "consistent",
                        "filename_id": filename_id}
            return {"action": "conflict", "artwork_id": str(saucenao_id) if saucenao_id else None,
                    "page": None, "trust": "conflict", "filename_id": filename_id}
        # 文件名无任何 ID: SauceNAO 是唯一参考
        return {"action": "archive", "artwork_id": str(saucenao_id) if saucenao_id else None,
                "page": 0, "trust": "unverified", "filename_id": None}

    @staticmethod
    def _plan_archive_path(target_dir: Path, artwork_id, page: int,
                           img_path: Path) -> "tuple[Path, bool]":
        """规范归档命名: {作品ID}_p{页码}{原后缀}; 目标占用 → 页码递增绝不覆盖。
        返回 (新路径, 是否发生改名)。矛盾溯源信息由调用方写 reverse_search_history。"""
        ext = img_path.suffix.lower() or ".jpg"
        page = int(page or 0)
        candidate = target_dir / f"{artwork_id}_p{page}{ext}"
        while candidate.exists():
            page += 1
            candidate = target_dir / f"{artwork_id}_p{page}{ext}"
        renamed = candidate.name != img_path.name
        return candidate, renamed

    def _foreign_target_dir(self, site: str, handle: str) -> Path:
        """外来源归档目标 (对齐图库分层结构 2026-09-12):
        - DB 里已有「该来源@账号」的作品且作者是 pixiv 用户(数字 author_id) →
          其 pixiv 顶层夹/「站点@账号」子夹 (与一键整理的子夹聚合一致, 不再散落);
        - 否则平铺「站点@账号」。
        未知来源(other) / 无账号(未知作者) → 夹名加 `#...#` 包裹 
        未知来源 → `#其他来源#`; 真实来源无账号 → `#站点#` (与一键整理口径一致;
        不用 `_` 前缀: 部分作者名本身含下划线, 区分度不够)。
        返回归档目标目录 (archive_path 下)。"""
        acc = (handle or "").strip()
        site_s = (site or "").strip()
        # 未知来源 (other/其他来源/未知来源) → 基底「其他来源」; 否则用站点名
        base = "其他来源" if site_s.lower() in ("other", "其他来源", "未知来源") \
            else _clean_dirname(site_s)
        fname = _clean_dirname(f"{base}@{acc}") if acc else f"#{_clean_dirname(base)}#"
        top = None
        if acc and self.db is not None:
            try:
                # 优先 pixiv 作者(数字 id) → 聚合到其顶层夹; 没有再看外来作者(local_xxx)
                row = self.db.conn.execute(
                    "SELECT a.author_id FROM artworks a "
                    "WHERE a.source=? AND a.source_author=? AND a.author_id IS NOT NULL "
                    "AND a.author_id != '' AND a.author_id NOT LIKE 'local_%' LIMIT 1",
                    (site, acc)).fetchone()
                if row:
                    top = str(row["author_id"])
                if not top:
                    row = self.db.conn.execute(
                        "SELECT a.author_id FROM artworks a "
                        "WHERE a.source=? AND a.source_author=? AND a.author_id LIKE 'local_%' LIMIT 1",
                        (site, acc)).fetchone()
                    if row:
                        top = str(row["author_id"])
            except Exception:
                top = None
        if top:
            try:
                a = self.db.get_author(top)
                # 顶层夹名走全项目唯一规则 (2026-09-21 治本): 与一键整理的期望夹一字不差,
                # 否则同一位作者会被摆成两个夹 (夹名里的非法字符处理方式必须一致)。
                # 顶层按作者名, 内部按来源分子夹 —— **单来源也是**
                # (单来源若平铺进作者夹, 夹名里就看不出作品来自哪个站,
                #  与一键整理 `_tidy_expected_folder` 同口径)
                nm = author_folder_name(self.db, top, str(a.get("name") or ""))
                return self.archive_path / nm / fname
            except Exception:
                pass
        return self.archive_path / fname

    def run(self, files=None, archive: bool = True, existing: str = "copy",
            on_progress: "object | None" = None,
            batch_size: "int | None" = None,
            batch_break_sec: "int | float | None" = None):
        """批量反搜归档。archive=False 时只识别不归档: 不移动文件、不落库,
        识别结果仍在结果面板展示 (用户自行决定是否归档, 防误动已有高清版)。
        existing: 归档时目标已有同名文件时的处理
                 'copy'=加副本(页码递增, 默认绝不覆盖) / 'skip'=跳过(文件留在待反搜区) / 'overwrite'=覆盖已有文件。
        on_progress(idx, total, name, status, rest_sec=None): 进度回调 (idx=已完成数,
                  status ∈ running/ok/break, break 时 rest_sec=剩余休息秒)。
        batch_size/batch_break_sec: 每 batch_size 张休息 batch_break_sec 秒 (防 SauceNAO 拉黑);
                  默认读配置 reverse_search.batch_size / batch_break_sec。"""
        if not self.search_path.exists():
            self.reporter.error("存放待搜索图片的文件夹不存在!")
            return self.results
        not_found_dir = self.search_path / "not_found"
        not_found_dir.mkdir(exist_ok=True)

        image_files = self._list_images(self.search_path, files)

        if not image_files:
            self.reporter.log("未发现任何图片文件")
            return self.results
        self.reporter.log(f"发现 {len(image_files)} 张待反搜的图片")

        scfg = self.config["reverse_search"]
        bs = max(1, int(batch_size if batch_size is not None else scfg.get("batch_size", 5) or 5))
        bbr = max(1.0, float(batch_break_sec if batch_break_sec is not None
                             else scfg.get("batch_break_sec", 30) or 30))
        total = len(image_files)
        for idx, img_path in enumerate(image_files, 1):
            if on_progress is not None:
                on_progress(idx - 1, total, img_path.name, "running")
            self.reporter.progress(idx, total, f"反搜 {img_path.name}")
            # detail 模式: 同时识别 pixiv 与外来源 (twitter/artstation/danbooru/...),
            # 外来源命中不再算「未找到图源」, 而是归档到新结构位置
            hit = self.search_by_image(img_path, detail=True)
            site = hit.get("site")
            match_artwork_id = hit.get("artwork_id")
            match_author_name = hit.get("author_name")
            match_author_id = hit.get("author_id")
            handle = hit.get("handle") or ""
            if on_progress is not None:
                on_progress(idx, total, img_path.name, "ok")

            if match_artwork_id:
                # 信任链决策: 本地指纹 > 文件名自然键 > SauceNAO
                db_page = None
                if self.db is not None:
                    try:
                        from utils.hashutil import md5_of
                        h = md5_of(str(img_path))
                        if h:
                            db_page = self.db.find_page_by_md5(h)
                    except Exception:
                        db_page = None
                decision = self._decide_identity(
                    img_path.name, match_artwork_id, db_page)

                if decision["action"] == "in_library":
                    # 指纹命中: 图库里已有同一张, 不归档不移动, 仅提示
                    self.reporter.log(
                        f"✅ {img_path.name} 与图库作品 {decision['artwork_id']} "
                        f"字节一致, 已在图库中, 不重复归档")
                    self.results["in_library"].append({
                        "file": img_path.name,
                        "artwork_id": decision["artwork_id"],
                        "trust": "hash",
                        "reason": "本地指纹比对一致，图库已有此图",
                    })
                elif decision["action"] == "conflict":
                    # 文件名 ID 与 SauceNAO 不一致: 谁都不可信, 交人工
                    self.reporter.warn(
                        f"⚠ {img_path.name} 文件名声明作品 "
                        f"{decision['filename_id']}，SauceNAO 识别为 "
                        f"{decision['artwork_id']}，已保留待人工确认")
                    self.results["conflict"].append({
                        "file": img_path.name,
                        "filename_id": decision["filename_id"],
                        "saucenao_id": decision["artwork_id"],
                        "saucenao_author": f"{match_author_name}_{match_author_id}",
                        "reason": "文件名ID与识别结果不一致，需人工确认",
                    })
                else:
                    # archive: 一致(consistent) 或 无ID截图(unverified, 仅参考级)
                    trust = decision["trust"]
                    # 夹名走全项目唯一规则 (2026-09-21 治本): 与一键整理期望夹一致 (含别名),
                    # 不再本地拼「名_ID」, 否则同一位作者会被摆成两个夹。
                    target_dir = self.archive_path / author_folder_name(
                        self.db, str(match_author_id or "0"), match_author_name)
                    target_dir.mkdir(exist_ok=True)
                    original_name = img_path.name
                    if archive:
                        # 目标名占用按「已存在」选项处理:
                        # copy=页码递增加副本(绝不覆盖) / skip=跳过 / overwrite=覆盖
                        ext = img_path.suffix.lower() or ".jpg"
                        page = int(decision["page"] or 0)
                        canonical = target_dir / f"{decision['artwork_id']}_p{page}{ext}"
                        if canonical.exists() and existing == "skip":
                            self.reporter.warn(
                                f"⏭ {original_name} 目标 {canonical.name} 已存在, "
                                f"按选项跳过(文件留在待反搜区)")
                            self.results["skipped"].append({
                                "file": img_path.name,
                                "artwork_id": decision["artwork_id"],
                                "reason": "目标文件夹已有同名文件，按「已存在」选项跳过"})
                            continue
                        if canonical.exists() and existing == "overwrite":
                            new_path = canonical
                            renamed = canonical.name != original_name
                            self.reporter.warn(
                                f"⚠ {original_name} 覆盖已有文件 {canonical.name}")
                        else:
                            new_path, renamed = self._plan_archive_path(
                                target_dir, decision["artwork_id"],
                                decision["page"], img_path)
                            if renamed:
                                self.reporter.log(f"按规范命名: {original_name} → {new_path.name}")
                        shutil.move(str(img_path), str(new_path))
                        tag = "双来源互证" if trust == "consistent" else "仅 SauceNAO 参考, 未人工确认"
                        self.reporter.log(
                            f"识别成功({tag}) {new_path.name} → "
                            f"{match_author_name}_{match_author_id}")
                        # 归档即从 pixiv 权威接口抓元数据当场落库 (标题/标签/作者/封面),
                        # 不等自检; 注意 ajax 返回的作者才是官方信息, 可能纠正 SauceNAO 的作者
                        self._record_artwork_meta(decision["artwork_id"])
                        dest = str(new_path)
                    else:
                        # 仅识别不归档: 文件留在 input/, 结果面板给出识别结论由用户决定
                        self.reporter.log(
                            f"识别成功(未归档) {img_path.name} → "
                            f"{match_author_name}_{match_author_id}")
                        dest = None
                    self.results["success"].append({
                        "file": new_path.name if archive else original_name,
                        "original_file": original_name if (archive and renamed) else None,
                        "renamed": bool(archive and renamed),
                        "artwork_id": decision["artwork_id"],
                        "author": f"{match_author_name}_{match_author_id}",
                        "dest": dest,
                        "archived": archive,
                        "trust": trust,
                    })
            elif site and site != "pixiv":
                # 外来源命中 (twitter/artstation/danbooru/...): 归档到新结构位置
                # (其 pixiv 顶层/「站点@账号」子夹, 或平铺「站点@账号」)。无作品ID,
                # 只记来源, 不落库 (来源治理/反搜来源页面再人工确认)。
                try:
                    dest_dir = self._foreign_target_dir(site, handle)
                    dest_dir.mkdir(exist_ok=True)
                    original_name = img_path.name
                    if archive:
                        dest = dest_dir / original_name
                        if dest.exists():
                            self.reporter.warn(
                                f"⏭ {original_name} 已存在于 {dest_dir.name}, 跳过")
                            self.results["skipped"].append({
                                "file": img_path.name, "artwork_id": None,
                                "reason": f"目标文件夹 {dest_dir.name} 已有同名文件"})
                        else:
                            shutil.move(str(img_path), str(dest))
                            self.reporter.log(
                                f"来源识别(外网) {original_name} → {dest_dir.name}")
                        self.results["success"].append({
                            "file": original_name, "artwork_id": None,
                            "author": f"{site}@{handle}" if handle else site,
                            "dest": str(dest) if dest.exists() else None,
                            "archived": True, "trust": "saucenao", "site": site})
                    else:
                        self.reporter.log(
                            f"来源识别(未归档) {original_name} → {dest_dir.name}")
                        self.results["success"].append({
                            "file": original_name, "artwork_id": None,
                            "author": f"{site}@{handle}" if handle else site,
                            "dest": None, "archived": False,
                            "trust": "saucenao", "site": site})
                except Exception as e:
                    self.reporter.warn(f"外来源归档失败: {str(e).splitlines()[0]}")
                    self.results["failed"].append({
                        "file": img_path.name,
                        "reason": f"外来源归档失败: {str(e).splitlines()[0]}"})
            else:
                if archive:
                    shutil.move(img_path, not_found_dir / img_path.name)
                self.reporter.warn(f"未找到图源:{img_path.name}")
                self.results["failed"].append({"file": img_path.name, "reason": "SauceNAO 未匹配到 Pixiv 来源"})
            if self.reporter.is_cancelled():
                self.reporter.warn("用户已取消,停止反搜")
                break
            if self.error_count > scfg["max_error_count"]:
                self.reporter.error("错误次数过多,停止反搜")
                return self.results
            time.sleep(random.uniform(
                scfg["between_images_delay_min"], scfg["between_images_delay_max"]
            ))
            # 批次防拉黑休息: 每 bs 张强制休息 bbr 秒 (SauceNAO 频繁上传会被临时限制)
            if idx % bs == 0 and idx < total and on_progress is not None:
                for remain in range(int(bbr), 0, -1):
                    if on_progress is not None:
                        on_progress(idx, total, img_path.name, "break", remain)
                    if self.reporter.is_cancelled():
                        break
                    time.sleep(1)
        self.reporter.log("全部反搜完成!已按画师整理好文件夹")
        return self.results

    def close(self):
        try:
            if getattr(self, "db", None) is not None:
                self.db.close()
        except Exception:
            pass
        try:
            self.driver.quit()
        except Exception:
            pass
        temp_dir = paths.user_data("temp_upload")
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.reporter.log("已清理临时压缩图片文件夹")
