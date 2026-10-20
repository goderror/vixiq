# core/reverse_source.py
"""
已知 artwork_id 时,直接走 pixiv 找画师并归类移动图片(无需 saucenao)。
从旧 reverse_source.py 迁移,改造点:
- __init__ 改调 make_browser + 补 __enter__/__exit__(原只有 try/finally)
- load_cookies_and_login 复用 utils.browser.login_with_cookies
- XPath 从 resources/xpaths.json 的 pixiv 节读
- search_path 默认 data/img;作品间间隔读 config
- print → reporter
"""
import json
import os
import re
import time
import random
import shutil
from pathlib import Path

from selenium.webdriver.common.by import By

from utils import paths
from utils.author_naming import author_folder_name, clean_author_name
from utils.config import load_config
from utils.reporter import Reporter, PrintReporter
from utils.browser import make_browser, login_with_cookies
from db.store import pixiv_natural_key


class ReverseSource:
    def __init__(self, search_path: "Path | str | None" = None,
                 headless: "bool | None" = None,
                 reporter: "Reporter | None" = None,
                 config: "dict | None" = None,
                 archive_path: "Path | str | None" = None):
        self.config = config or load_config()
        self.reporter = reporter or PrintReporter()
        # search_path = 待反搜图片的输入目录 (独立 input/, 不和图库 img/ 混放);
        # archive_path = 识别成功后归档的图库根目录 (img/, 作者文件夹建在这里)
        self.search_path = Path(search_path) if search_path \
            else paths.user_data(self.config["paths"].get("input_subdir", "input"))
        self.archive_path = Path(archive_path) if archive_path \
            else paths.user_data(self.config["paths"]["img_subdir"])
        self.search_path.mkdir(parents=True, exist_ok=True)
        self.archive_path.mkdir(parents=True, exist_ok=True)

        with open(paths.resource("xpaths.json"), encoding="utf-8") as f:
            xp = json.load(f)
        self.xp_pixiv = xp["pixiv"]

        explicit = self.config["browser"]["explicit_wait_reverse"]
        self.driver, self.wait = make_browser(
            headless=headless, explicit_wait=explicit,
            reporter=self.reporter, config=self.config
        )
        self.cookies_file = paths.user_data(self.config["paths"]["cookies_filename"])
        self.login_ok = False
        self.not_found = []
        self.author_cache = {}  # {artwork_id: author_folder} 避免重复获取
        # 反搜结果清单 (供图库入库/前端结果面板): success=[{file,artwork_id,dest}], failed=[{file,reason}]
        # in_library=[{file,artwork_id}] (本地指纹命中, 图库里已有同一张)
        # skipped=[{file,artwork_id,reason}] (归档时目标已有同名文件, 按「已存在」选项跳过)
        self.results = {"success": [], "failed": [],
                        "in_library": [], "skipped": []}
        # DB 句柄: 归档当场把 ajax 权威元数据 (标题/标签/作者/封面) 落库
        try:
            from db.store import PixivDatabase
            self.db = PixivDatabase(reporter=self.reporter)
        except Exception:
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def load_cookies_and_login(self) -> bool:
        self.login_ok = login_with_cookies(self.driver, self.cookies_file, self.reporter)
        return self.login_ok

    def _ajax_get(self, url: str):
        """driver 已在 pixiv 域时, 直接导航到 ajax URL 取 JSON (比爬页面 DOM 稳,
        pixiv 改版不受影响)。失败返回 None。"""
        import json as _json
        try:
            self.driver.get(url)
            body = self.driver.find_element(By.TAG_NAME, "body").text
            data = _json.loads(body)
            if data.get("error"):
                return None
            return data.get("body")
        except Exception:
            return None

    @staticmethod
    def _clean_author_name(name: str) -> str:
        # 与一键整理/下载器/自检同一套非法字符规则 (非法字符→`_`); 注意 pixiv 昵称允许
        # 纯数字 (如画师就叫 "310123"), 不能把纯数字当无效名错改成「未知画师」。
        return clean_author_name(name)

    def find_source(self, artwork_id):
        # 优先 ajax JSON 取作者 (userId/userName), 页面 DOM 改版也不受影响
        body = self._ajax_get(f"https://www.pixiv.net/ajax/illust/{artwork_id}?lang=zh")
        author_name = author_id = None
        meta_info = None
        if body:
            author_id = str(body.get("userId") or "")
            author_name = (body.get("userName") or "").strip()
            # ajax body 带全量元数据 (标题/标签/封面/R18), 归档当场落库, 不等自检
            try:
                from core.search_client import SearchClient
                meta_info = SearchClient._illust_body_to_info(body)
            except Exception:
                meta_info = None
        if not (author_id and author_name):
            # 回退: 作品页 XPath (旧逻辑, pixiv 改版后可能取错元素)
            self.driver.get(f"https://www.pixiv.net/artworks/{artwork_id}")
            try:
                author_name = self.driver.find_element(By.XPATH, self.xp_pixiv["author_name"]).text.strip()
                author_link = self.driver.find_element(By.XPATH, self.xp_pixiv["author_id"]).get_attribute("href")
                author_id = re.search(r'/users/(\d+)', author_link).group(1)
            except Exception as e:
                self.reporter.warn(f"获取作者信息失败:{str(e).splitlines()[0]}")
                return None
        raw_name = str(author_name or "").strip()   # 原始昵称: 算规范夹名要它 (剥宣传后缀靠原名)
        author_name = self._clean_author_name(author_name)
        # 元数据随归档一起落库 (最权威数据源, 当场记录)
        if self.db is not None:
            try:
                if meta_info is not None:
                    self.db.upsert_artwork_metadata(
                        str(artwork_id),
                        title=meta_info.title or None,
                        author_id=meta_info.author_id or author_id,
                        author_name=meta_info.author_name or author_name,
                        illust_type=meta_info.illust_type.value,
                        tags=list(meta_info.tags or []),
                        publish_date=meta_info.publish_date,
                        is_r18=meta_info.is_r18,
                        thumb_url=(meta_info.image_urls or [None])[0],
                        page_count=meta_info.page_count,
                        ai_type=getattr(meta_info, "ai_type", 0))
                    self.reporter.log(f"📝 元数据已记录: 《{meta_info.title}》/ {author_name}")
                else:
                    self.db.upsert_author(str(author_id), author_name)
            except Exception as e:
                self.reporter.warn(f"元数据记录失败:{str(e).splitlines()[0]}")
        # 作者夹名走全项目唯一规则 (2026-09-21 治本): 与一键整理的期望夹名一致,
        # 不再用「删掉非法字符」的本地拼法, 否则反搜归档会另起一个夹
        author_folder = os.path.join(
            str(self.archive_path),
            author_folder_name(self.db, str(author_id), raw_name or author_name))
        return author_folder

    REV_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    @classmethod
    def _list_images(cls, search_path: Path, files=None):
        """列待反搜图片: files=None 扫目录全部; 否则按白名单 basename 过滤 (防路径穿越)。"""
        if files is None:
            return [p for p in sorted(search_path.iterdir())
                    if p.is_file() and p.suffix.lower() in cls.REV_IMG_EXT]
        out = []
        for name in files or []:
            # 只取文件名, 任何目录/../ 都剥掉
            base = os.path.basename(str(name).replace("\\", "/"))
            if not base:
                continue
            p = search_path / base
            if p.is_file() and p.suffix.lower() in cls.REV_IMG_EXT:
                out.append(p)
        return out

    def run(self, files=None, archive: bool = True, existing: str = "copy",
            on_progress: "object | None" = None,
            batch_size: "int | None" = None,
            batch_break_sec: "int | float | None" = None):
        """按文件名作品 ID 直查归档。
        archive=False 时只识别不归档 (不移动文件)。
        existing: 归档时目标文件夹已有同名文件时的处理
                 'copy'=加副本(默认,绝不覆盖) / 'skip'=跳过(文件留在待反搜区) / 'overwrite'=覆盖已有文件。
        on_progress(idx, total, name, status, rest_sec=None): 进度回调 (idx=已完成组数,
                  status ∈ running/ok/break, break 时 rest_sec=剩余休息秒)。
        batch_size/batch_break_sec: 每 batch_size 组休息 batch_break_sec 秒 (防拉黑);
                  默认读配置 reverse_search.batch_size / batch_break_sec。"""
        if not self.search_path.exists():
            self.reporter.error("存放待搜索图片的文件夹不存在!")
            return self.results

        image_files = self._list_images(self.search_path, files)
        if not image_files:
            self.reporter.log("未发现任何图片文件")
            return self.results
        self.reporter.log(f"发现 {len(image_files)} 张待反搜的图片")
        self.load_cookies_and_login()

        from collections import defaultdict
        groups = defaultdict(list)  # {artwork_id: [Path]}
        for img_path in image_files:
            match = re.search(r'(\d+)', img_path.name)
            if match:
                groups[match.group(1)].append(img_path)
            else:
                self.reporter.warn(f"跳过无效文件名(无ID):{img_path.name}")
                self.not_found.append(img_path.name)
                self.results["failed"].append({"file": img_path.name, "reason": "文件名中无作品ID"})

        ccfg = self.config["crawler"]  # 复用作品间间隔配置
        rcfg = self.config.get("reverse_search", {})
        bs = max(1, int(batch_size if batch_size is not None else rcfg.get("batch_size", 5) or 5))
        bbr = max(1.0, float(batch_break_sec if batch_break_sec is not None
                             else rcfg.get("batch_break_sec", 30) or 30))
        total = len(groups)
        for idx, (artwork_id, img_paths) in enumerate(groups.items(), 1):
            if on_progress is not None:
                on_progress(idx - 1, total, artwork_id, "running")
            self.reporter.progress(idx, total, f"反搜 {artwork_id}({len(img_paths)} 张)")
            self.reporter.log(f"[{idx}/{total}] 正在反搜 artwork_id:{artwork_id}({len(img_paths)} 张图片)")

            if artwork_id in self.author_cache:
                author_folder = self.author_cache[artwork_id]
                self.reporter.log(f"使用缓存的作者信息:{author_folder}")
            else:
                author_folder = self.find_source(artwork_id)
                if author_folder:
                    self.author_cache[artwork_id] = author_folder
            if on_progress is not None:
                on_progress(idx, total, artwork_id, "ok")

            if author_folder:
                target_dir = Path(author_folder)
                target_dir.mkdir(exist_ok=True)
                for img_path in img_paths:
                    # 指纹兜底 (归档模式): 图库已有字节一致的同一张 → 不重复归档
                    if archive and self.db is not None:
                        try:
                            from utils.hashutil import md5_of
                            h = md5_of(str(img_path))
                            if h:
                                db_page = self.db.find_page_by_md5(h)
                                if db_page:
                                    self.results["in_library"].append({
                                        "file": img_path.name,
                                        "artwork_id": str(db_page.get("artwork_id")),
                                        "trust": "hash",
                                        "reason": "本地指纹比对一致，图库已有此图"})
                                    self.reporter.log(
                                        f"✅ {img_path.name} 与图库作品 "
                                        f"{db_page.get('artwork_id')} 字节一致, 已在图库, 不重复归档")
                                    continue
                        except Exception:
                            pass
                    if archive:
                        new_path = target_dir / img_path.name
                        if new_path.exists():
                            if existing == "skip":
                                self.reporter.warn(
                                    f"⏭ {img_path.name} 目标已存在同名, 按选项跳过(文件留在待反搜区)")
                                self.results["skipped"].append({
                                    "file": img_path.name, "artwork_id": artwork_id,
                                    "reason": "目标文件夹已有同名文件，按「已存在」选项跳过"})
                                continue
                            if existing == "copy":
                                new_path = target_dir / f"{img_path.stem}__{int(time.time())}{img_path.suffix}"
                                self.reporter.log(
                                    f"📄 {img_path.name} 目标已存在同名, 加副本: {new_path.name}")
                            # overwrite: 保持同名, shutil.move 在 Windows 上会覆盖目标文件
                        shutil.move(img_path, new_path)
                        self.reporter.log(f"已移动 {img_path.name} → {target_dir.name}/{new_path.name}")
                        dest = str(new_path)
                    else:
                        self.reporter.log(f"已识别(未归档) {img_path.name} → {author_folder}")
                        dest = None
                    self.results["success"].append({
                        "file": img_path.name, "artwork_id": artwork_id,
                        "dest": dest, "archived": archive,
                    })
            else:
                self.reporter.warn(f"未找到图源:{artwork_id}(共 {len(img_paths)} 张图片)")
                for img_path in img_paths:
                    self.not_found.append(img_path.name)
                    self.results["failed"].append({
                        "file": img_path.name, "artwork_id": artwork_id,
                        "reason": "Pixiv 未找到该作品/作者",
                    })
            if self.reporter.is_cancelled():
                self.reporter.warn("用户已取消,停止反搜")
                break
            time.sleep(random.uniform(
                ccfg["between_works_delay_min"], ccfg["between_works_delay_max"]
            ))
            # 批次防拉黑休息: 每 bs 组强制休息 bbr 秒 (批量查 pixiv 防临时限制)
            if idx % bs == 0 and idx < total and on_progress is not None:
                for remain in range(int(bbr), 0, -1):
                    if on_progress is not None:
                        on_progress(idx, total, artwork_id, "break", remain)
                    if self.reporter.is_cancelled():
                        break
                    time.sleep(1)
        self.reporter.log("全部反搜完成!已按画师整理好文件夹")
        return self.results

    # ---- P3: 单张补齐 API (供 🛒 download_engine 调用) ----
    def process_one(self, img_path: Path, *,
                    force_artwork_id: Optional[str] = None,
                    force_author_id: Optional[str] = None) -> bool:
        """
        处理单张反搜补齐图片。
        语义和批处理 run() 一致: 按 [画师名_画师ID] 目录归档移动图片。

        Params:
          force_artwork_id: 加清单时已经反搜查到的 artwork_id, 跳过文件名正则, 直接拿去查作者
          force_author_id:  直接指定作者, 完全跳过 Pixiv 访问 (最快路径)
        Returns: True 成功归档 / False 失败
        """
        self.load_cookies_and_login()
        img_path = Path(img_path)
        if not img_path.exists():
            self.reporter.warn(f"process_one: 文件不存在 {img_path}")
            return False

        author_folder: Optional[str] = None
        # 1. 最快路径: 指定了 author_id → ajax 取昵称后直接拼作者文件夹
        if force_author_id:
            ubody = self._ajax_get(f"https://www.pixiv.net/ajax/user/{force_author_id}")
            an = self._clean_author_name((ubody or {}).get("name") or "") if ubody else None
            if an and an != "未知画师":
                author_folder = os.path.join(str(self.archive_path),
                                             f"{an}_{force_author_id}")
            else:
                self.reporter.warn(f"按 author_id 查作者昵称失败: {force_author_id}")
        if not author_folder and force_artwork_id:
            if force_artwork_id in self.author_cache:
                author_folder = self.author_cache[force_artwork_id]
            else:
                author_folder = self.find_source(force_artwork_id)
                if author_folder:
                    self.author_cache[force_artwork_id] = author_folder
        if not author_folder:
            # 没给强制参数 → 退回到文件名自然键识别 artwork_id
            _nkey = pixiv_natural_key(img_path.name)
            if _nkey:
                aid = _nkey[0]
                author_folder = self.find_source(aid)
                if author_folder:
                    self.author_cache[aid] = author_folder
        if not author_folder:
            self.not_found.append(img_path.name)
            self.reporter.warn(f"process_one: 无法定位图源, 跳过 {img_path.name}")
            return False
        target_dir = Path(author_folder)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / img_path.name
        if target_path.exists():
            # 目标已存在同名, 加后缀避免覆盖
            target_path = target_dir / f"{img_path.stem}__{int(time.time())}{img_path.suffix}"
        shutil.move(str(img_path), str(target_path))
        self.reporter.log(f"[反搜补齐] {img_path.name} → {target_path}")
        return True

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
        if self.not_found:
            self.reporter.warn("失败或超时项:")
            for name in self.not_found:
                self.reporter.warn(name)
