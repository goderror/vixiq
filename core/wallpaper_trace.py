# core/wallpaper_trace.py
"""🖼 桌面壁纸 (Windows 幻灯片) 溯源 —— 2026-09-24

「现在桌面播的是哪张」不需要猜: Windows 幻灯片每切一张就把**这一张的原始文件路径**
写进注册表 HKCU\\Control Panel\\Desktop\\WallPaper (2026-09-24 实测)。于是「桌面上那张」
↔「图库里哪个作品」能对上 —— 轮播时看到中意的图, 当场就知道作者是谁、库内有没有
更高清的版本, 不用再回图库一张张翻。

溯源四档 (可信度由高到低): ① 文件本身就在图库内 ② MD5 与库内页字节一致
③ 文件名解析出的作品 ID 在库内 ④ ORB+颜色融合 (复用 /api/library/similar 的检索器)。
全程**只读**: 不动壁纸夹、不动图库、不动数据库。

**为什么独立成模块**: 网页面板 (web/server.py 的 /api/wallpaper/current) 与系统托盘
(utils/tray.py) 要用**同一份**规则 —— 复制第二份迟早会漂。所以本模块**不 import Flask**,
也不持有 DB 连接与 ORB 检索器这类重资源, 它们一律由调用方注入:

  - ``db_factory``: 返回数据库连接的可调用对象。**必须每次调用都新建** ——
    db/store.py 的 ``sqlite3.connect`` 没开 ``check_same_thread=False``,
    SQLite 连接绑定创建它的线程, 跨线程复用会直接抛异常。
  - ``orb_fn``: 跑 ORB 兜底溯源的可调用对象 (慢路, 默认不跑)。注入而不是搬进来,
    是因为整库特征检索器首次构建要几十秒、占几百 MB, 搬进来会加载两份。
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import threading
from pathlib import Path
from urllib.parse import quote

from db.store import PixivDatabase, pixiv_natural_key
from utils import paths
from utils.reporter import PrintReporter

# 壁纸尺寸缓存: 按 mtime+字节数缓存, 定时刷新不重复解码大图
WALL_SIZE_CACHE: "dict[str, tuple]" = {}
WALL_SIZE_LOCK = threading.Lock()
# ORB 溯源结果按「文件+mtime+大小」缓存: 前端 8 秒轮询一次, 不缓存的话每个轮询都会
# 因为这次没带 deep=1 又把结论退回「未溯源」—— 界面自己来回跳。
WALL_DEEP_CACHE: "dict[str, list]" = {}
WALL_DEEP_LOCK = threading.Lock()
WALL_ORB_MIN = 0.30      # ORB 融合分门槛 (与 2026-09-24 一次性对账脚本同一口径)
WALL_ORB_TOPK = 5        # ORB 候选取前几名 (托盘/面板都只用第一名)

# 「没传 wall_path」与「明确传了 None(没有壁纸)」是两回事, 用哨兵区分
_UNSET = object()


def wallpaper_file() -> "Path | None":
    """当前桌面壁纸的原始文件路径 (幻灯片运行时同样写这里); 取不到/文件已不在 → None。"""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as k:
            val, _t = winreg.QueryValueEx(k, "WallPaper")
    except OSError:
        return None
    p = str(val or "").strip()
    if not p:
        return None
    f = Path(p)
    return f if f.is_file() else None


# Windows 幻灯片认的图片扩展名 (webp 不吃, 别列进来)
WALL_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff")
_SPI_SETDESKWALLPAPER = 0x0014
_SPIF_UPDATEINIFILE, _SPIF_SENDCHANGE = 0x01, 0x02


def _nat_key(name: str) -> str:
    """文件名自然排序键 (数字段左补零, 纯字符串比较, 不会 int/str 混比炸掉)。

    直接 `sorted` 会把 `109395314_p0` 排到 `99999999_p0` 前面, 挑「下一张」时看着乱跳。
    """
    return re.sub(r"\d+", lambda m: m.group(0).rjust(16, "0"), str(name).lower())


def wallpaper_folder_images(folder: Path) -> "list[Path]":
    """壁纸夹里的候选图, 按文件名自然排序 (挑「下一张」用)。读不到目录 → 空。"""
    try:
        names = [n for n in os.listdir(folder)
                 if os.path.splitext(n)[1].lower() in WALL_EXTS]
    except OSError:
        return []
    names.sort(key=_nat_key)
    return [folder / n for n in names]


def wallpaper_dir() -> "Path | None":
    """**配置指定的壁纸夹** (``paths.wallpaper_dir``) —— 没配 / 配的目录读不到 → None。

    为什么要它: 桌面幻灯片用的壁纸夹是用户在**系统设置**里选的, 应用这边
    没有任何地方能看到/改到它 —— 于是「上一张/下一张/幻灯片」只能猜「当前这张所在的文件夹」。
    用户想在应用里指定一个夹子时就该听他的, 而不是继续猜。
    """
    try:
        from utils.config import load_config
        raw = str((load_config().get("paths") or {}).get("wallpaper_dir") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        p = Path(raw)
    except Exception:
        return None
    return p if p.is_dir() else None


def candidates_dir(cur: "Path | None" = None) -> "Path | None":
    """挑「上一张 / 下一张 / 幻灯片」时到底在哪个夹子里挑。

    优先**用户指定的**壁纸夹 (见:func:`wallpaper_dir`), 没指定才回退到「当前壁纸所在文件夹」
    —— 回退这条是 2026-09-24 起就在用的口径, 对没配过的用户行为完全不变。
    """
    d = wallpaper_dir()
    if d is not None:
        return d
    cur = wallpaper_file() if cur is None else cur
    return cur.parent if cur is not None else None


def _step_wallpaper(cur: "Path | None", step: int, shuffle: bool = False) -> "Path | None":
    """候选夹里当前壁纸往前/往后跳 ``step`` 张 (环形); 取不到/只有一张 → None。

    「往前走」= 文件名自然序的下一个, 所以 ``step=-1`` 就是上一张。「小控件三键」的
    上一张/下一张共用这一份, 免得两边各写一遍取模顺序。

    ``shuffle=True`` 时**不看顺序**, 从候选里随机挑一张 (排除当前这张) —— 对应小控件上
    那个「顺序 / 随机」切换键。随机只影响**本应用**发起的换图 (三键 + 幻灯片定时器),
    不动 Windows 幻灯片自己的乱序设置。

    ``cur is None`` (注册表里那张已经被删/移走) 时**不算死路**: 只要配了壁纸夹就还能起步 ——
    注册表只写「现在播的是哪张」, 那张文件被删之后这个值不会自己更新, 于是整个换图功能
    全哑掉 (删掉壁纸后既不自动换图, 点「下一张」也无反应)。起步取自然序第一张 (往前步进时取最后一张), 随机档则任意一张。
    """
    cur = wallpaper_file() if cur is None else cur
    folder = candidates_dir(cur)
    if folder is None:
        return None
    files = wallpaper_folder_images(folder)
    if not files:
        return None
    if cur is None:                    # 没有「当前这张」可参照 → 从夹子的一端起头
        if shuffle:
            return random.choice(files)
        return files[0] if step >= 0 else files[-1]
    if len(files) < 2:
        return None
    if shuffle:
        others = [p for p in files if p != cur]
        return random.choice(others) if others else None
    try:
        i = files.index(cur)
    except ValueError:                 # 当前这张不在列出的候选里 (扩展名不吃/已被删/换了夹子)
        return files[0] if step >= 0 else files[-1]
    return files[(i + step) % len(files)]


def next_wallpaper(cur: "Path | None" = None, shuffle: bool = False) -> "Path | None":
    """候选夹里当前壁纸的下一张, 到底循环回第一张; 取不到/只有一张 → None。"""
    return _step_wallpaper(cur, 1, shuffle)


def prev_wallpaper(cur: "Path | None" = None, shuffle: bool = False) -> "Path | None":
    """候选夹里当前壁纸的上一张, 到顶循环回最后一张; 取不到/只有一张 → None。"""
    return _step_wallpaper(cur, -1, shuffle)


def wallpaper_position(cur: "Path | None" = None) -> "tuple[int, int]":
    """当前这张在候选夹里的位置 → ``(第几张, 共几张)``, 都是 1 起。

    注册表只写「现在播的是哪张」, 没有位置信息; 小控件底部那条进度线要靠这个算
    (见 widget_window.folder_frac)。取不到 → ``(0, 0)``, 界面就画空槽, 不做假进度。
    当前这张不在候选夹里 (用户刚改了壁纸夹) → ``(0, 共几张)``。
    """
    cur = wallpaper_file() if cur is None else cur
    if cur is None:
        return 0, 0
    folder = candidates_dir(cur)
    if folder is None:
        return 0, 0
    files = wallpaper_folder_images(folder)
    try:
        i = files.index(cur)
    except ValueError:
        return 0, len(files)
    return i + 1, len(files)


def apply_wallpaper(p: Path) -> bool:
    """把 p 设成桌面壁纸 —— **只改「现在显示哪一张」**。

    实测确认不会把幻灯片关掉: 换完之后 `Explorer\\Wallpapers` 的 `BackgroundType` 仍是 2,
    幻灯片的间隔/乱序/目录全不受影响。所以这个按钮只是「跳一张看看」, 不是退出轮播。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.SystemParametersInfoW(
            _SPI_SETDESKWALLPAPER, 0, str(p),
            _SPIF_UPDATEINIFILE | _SPIF_SENDCHANGE))
    except Exception:
        return False


# ── 🧹 「省空间」: 壁纸夹里与库内页**字节一致**的副本 ─────────────────────────
# 壁纸夹 1181 张 / 7.4 GB, 全量算一次 MD5 要几十秒 → 按 mtime+字节数缓存指纹,
# 之后每次扫副本都是秒回 (新增/改动的文件才重算)。
WALL_MD5_CACHE: "dict[str, str]" = {}
WALL_MD5_LOCK = threading.Lock()


def md5_cached(p: Path) -> str:
    """带缓存的:func:`file_md5` (键 = 路径+mtime+字节数, 内容变了必然重算)。"""
    try:
        st = p.stat()
    except OSError:
        return ""
    key = f"{p}|{st.st_mtime_ns}|{st.st_size}"
    with WALL_MD5_LOCK:
        hit = WALL_MD5_CACHE.get(key)
    if hit is not None:
        return hit
    h = file_md5(p)
    if h:
        with WALL_MD5_LOCK:
            if len(WALL_MD5_CACHE) > 4096:
                WALL_MD5_CACHE.clear()
            WALL_MD5_CACHE[key] = h
    return h


def library_md5_index(db) -> dict:
    """库内**已有指纹**的页: ``md5 → {artwork_id, page_num, rel, title, author_name}``。

    一个 md5 只留第一条命中 (同内容多份副本时, 随便挑一条说明"库里有"就够了)。
    """
    out: dict = {}
    try:
        rows = db.conn.execute(
            "SELECT p.md5 AS md5, p.artwork_id AS aid, p.page_num AS pn, "
            "       p.local_path AS rel, a.title AS title, "
            "       COALESCE(au.name, a.author_id, '') AS aname "
            "FROM pages p LEFT JOIN artworks a ON a.artwork_id = p.artwork_id "
            "LEFT JOIN authors au ON au.author_id = a.author_id "
            "WHERE p.md5 IS NOT NULL AND p.md5 <> ''").fetchall()
    except Exception:
        return out
    for r in rows:
        m = str(r["md5"] or "")
        if not m or m in out:
            continue
        out[m] = {
            "artwork_id": str(r["aid"] or ""),
            "page_num": int(r["pn"] or 0),
            "rel": str(r["rel"] or "").replace("\\", "/"),
            "title": r["title"] or "",
            "author_name": r["aname"] or "",
        }
    return out


def find_wall_dupes(wall_dir: Path, db_factory, progress_cb=None) -> dict:
    """扫 ``wall_dir`` 里与库内某页 **MD5 字节一致**的副本 —— **只报告, 绝不删**。

    用途是「省空间」: 这些文件和图库里那份一模一样, 删掉壁纸夹里的那份不丢任何东西
    (必须**逐条人工确认 + 走 Windows 回收站**, 不许自动清理)。
    所以本函数是**纯只读**的 —— 删不删、删哪几条, 一律由界面上的勾选决定。

    ``progress_cb(done, total, name)`` 在每张图前调一次 (7.4 GB 要跑几十秒, 得有进度)。
    """
    files = wallpaper_folder_images(wall_dir)
    try:
        cur = wallpaper_file()
    except Exception:
        cur = None
    with db_factory() as db:
        index = library_md5_index(db)
    items: list = []
    total_bytes = 0
    for i, f in enumerate(files, 1):
        if progress_cb:
            progress_cb(i, len(files), f.name)
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        h = md5_cached(f)
        hit = index.get(h) if h else None
        if not hit:
            continue
        total_bytes += sz
        items.append({
            "name": f.name,
            "size": sz,
            "md5": h,
            "current": 1 if (cur is not None and f == cur) else 0,
            "artwork_id": hit["artwork_id"],
            "page_num": hit["page_num"],
            "rel": hit["rel"],
            "title": hit["title"],
            "author_name": hit["author_name"],
        })
    items.sort(key=lambda x: (-int(x["size"]), str(x["name"]).lower()))
    return {"dir": str(wall_dir), "scanned": len(files), "count": len(items),
            "bytes": total_bytes, "index_size": len(index), "items": items}


def wallpaper_size(p: Path) -> "tuple[int, int]":
    """壁纸像素尺寸 (只读文件头); 按 mtime+字节数缓存, 定时轮询不重复解码大图。"""
    try:
        st = p.stat()
    except OSError:
        return 0, 0
    key = f"{p}|{st.st_mtime_ns}|{st.st_size}"
    with WALL_SIZE_LOCK:
        hit = WALL_SIZE_CACHE.get(key)
    if hit is not None:
        return hit
    wh = (0, 0)
    try:
        from PIL import Image
        old_bomb = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None
        try:
            with Image.open(p) as im:
                wh = (int(im.width), int(im.height))
        finally:
            Image.MAX_IMAGE_PIXELS = old_bomb
    except Exception:
        wh = (0, 0)
    with WALL_SIZE_LOCK:
        if len(WALL_SIZE_CACHE) > 64:
            WALL_SIZE_CACHE.clear()
        WALL_SIZE_CACHE[key] = wh
    return wh


def file_md5(p: Path) -> str:
    h = hashlib.md5()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def wall_relation(wall_wh: "tuple[int, int]", page_wh: "tuple[int, int]") -> str:
    """壁纸图 ↔ 库内页的形态差异 (回答"这张是不是从我库里那张裁/缩出来的")。"""
    ww, wh = wall_wh
    pw, ph = page_wh
    if not ww or not wh or not pw or not ph:
        return ""
    if (ww, wh) == (pw, ph):
        return "same"
    if (wh, ww) == (pw, ph):
        return "rotate"          # 竖图转横图之类: 长宽正好对调
    if abs((ww / wh) - (pw / ph)) > 0.01:
        return "crop"            # 比例变了 → 被裁过
    return "rescale"             # 比例一致, 只是缩放过


def img_v(abs_path: Path) -> str:
    """本地图片的「内容版本令牌」= 修改时间(ns)-字节数。

    为什么非有不可: 图片 URL 里的 `p=` 是**文件路径**, 而「路径不变、内容变」在
    页序调整里是必然发生的 —— 调页序就是把同一目录下的文件名互换内容(名字不变、
    内容互换), 好让「第 N 张」直接等于 `p{N}`。URL 要是不变, 浏览器会拿
    `max-age=2592000`(30 天) 的缓存一直显示旧图, 看起来像改动没生效。
    令牌与缩略图磁盘缓存的键同源(mtime+size) → 内容变了令牌必变, 没变就不变。"""
    try:
        st = abs_path.stat()
    except OSError:
        return ""
    return f"{st.st_mtime_ns}-{st.st_size}"


def local_img_url(rel: str, img_root: "Path | None", thumb: bool = True) -> str:
    """本地图片 URL (可选带内容版本令牌 v=; 源文件不在时不留 v, 不影响已缓存语义)。"""
    u = f"/api/local-img?p={quote(rel)}"
    if thumb:
        u += "&thumb=1"
    if img_root is not None and rel:
        v = img_v(Path(img_root) / str(rel).replace("\\", "/"))
        if v:
            u += f"&v={v}"
    return u


def _wall_artwork(db, aid: str, img_root: Path, img_url_fn=local_img_url) -> "dict | None":
    """作品元数据 + 库内页清单 (含每页尺寸, 供「库内有没有更高清」对比)。"""
    if not aid:
        return None
    row = db.conn.execute("SELECT * FROM artworks WHERE artwork_id=?",
                          (str(aid),)).fetchone()
    if not row:
        return None
    a = dict(row)
    author_id = str(a.get("author_id") or "")
    au = db.conn.execute("SELECT name, avatar_url FROM authors WHERE author_id=?",
                         (author_id,)).fetchone() if author_id else None
    pages = (db.get_artwork_page_list([str(aid)]) or {}).get(str(aid), [])
    out_pages = []
    for p in pages:
        rel = str(p.get("local_path") or "").replace("\\", "/")
        out_pages.append({
            "page_num": p.get("page_num"),
            "rel": rel,
            "width": int(p.get("width") or 0),
            "height": int(p.get("height") or 0),
            "size": int(p.get("file_size_bytes") or 0),
            "origin": p.get("origin") or "",
            "url": img_url_fn(rel, img_root, thumb=False),
            "thumb": img_url_fn(rel, img_root),
        })
    return {
        "artwork_id": str(a.get("artwork_id") or ""),
        "title": a.get("title") or "",
        "author_id": author_id,
        "author_name": (au["name"] if au else "") or author_id,
        "author_avatar": (au["avatar_url"] if au else "") or "",
        "is_r18": int(a.get("is_r18") or 0),
        "illust_type": int(a.get("illust_type") or 0),
        "total_pages": a.get("total_pages"),
        "trashed": 1 if a.get("trashed_at") else 0,
        "pages": out_pages,
    }


def _page_by_rel(db, rel: str):
    """按「图库内相对路径」反查那一页。pages.local_path 在 Windows 上是反斜杠,
    而这里流通的 rel 一律正斜杠 → 用 REPLACE 做斜杠无关的比较, 否则永远查不到
    (2026-09-24 实测: MD5 已命中却拿不到作品, 就是这一处斜杠不一致)。"""
    if not rel:
        return None
    return db.conn.execute(
        r"SELECT artwork_id, page_num FROM pages "
        r"WHERE REPLACE(local_path, '\', '/')=? LIMIT 1",
        (str(rel).replace("\\", "/"),)).fetchone()


def _default_db() -> PixivDatabase:
    """托盘等非 Web 场景的默认连接 (Web 场景由 server 注入带 WebReporter 的工厂)。"""
    return PixivDatabase(reporter=PrintReporter())


def _default_img_root() -> Path:
    from utils.config import load_config
    return Path(paths.user_data(load_config()["paths"]["img_subdir"])).resolve()


_REL_TEXT = {"same": "像素完全一致", "rotate": "旋转过",
             "crop": "裁过", "rescale": "缩放过"}
_KIND_TEXT = {"library": "就在图库内", "md5": "字节一致", "id": "按作品ID对上"}


def build_summary(info: dict) -> dict:
    """把溯结论压成一句话 —— 托盘 tooltip、气泡、桌面小控件三处共用同一份文案。"""
    art = info.get("artwork") or {}
    m = info.get("match") or {}
    f = info.get("file") or {}
    kind = str(m.get("kind") or "none")
    if kind in _KIND_TEXT:
        src = _KIND_TEXT[kind]
    elif kind == "orb":
        src = f"图像匹配 {int(round(float(m.get('confidence') or 0) * 100))}%"
    elif kind == "weak":
        src = "疑似同图(分数偏低)"
    else:
        src = "未溯源"
    bits = [src]
    rel_text = _REL_TEXT.get(str(m.get("relation") or ""))
    if rel_text:
        bits.append(rel_text)
    if info.get("bigger"):
        bits.append("库内有更高清")
    elif art:
        bits.append("库内已是最清晰")
    return {
        "author": str(art.get("author_name") or ""),
        "title": str(art.get("title") or f.get("name") or ""),
        "line": " · ".join(bits),
    }


def _no_wallpaper(reason: str) -> dict:
    """「现在没有一个可用的壁纸文件」时的统一返回。

    额外带上 ``folder``: 壁纸夹配好了没有。桌面上的小控件在**没有当前壁纸**时靠它决定
    画出哪些键 —— 配了夹子就还能「下一张 / 播放 / 顺序·随机」, 一个都没配就真的无处可点
    (那张壁纸被删之后卡片整个**空了**, 一个按钮都没有)。
    """
    d = candidates_dir(None)
    return {"status": "ok", "active": False, "reason": reason,
            "folder": {"ready": bool(d), "dir": str(d or "")}}


def trace_current_wallpaper(*, deep: bool = False, wall_path=_UNSET, db_factory=None,
                            img_root=None, img_url_fn=None, orb_fn=None) -> dict:
    """当前壁纸 → 图库里是哪个作品 (只读)。返回结构与 /api/wallpaper/current 的 JSON 一致。

    ``wall_path``: 不传 = 自己读注册表; 传 Path = 用这张; 传 None = 没有壁纸。
    ``db_factory``: 返回数据库连接的可调用对象 (默认 PrintReporter 版); 每次调用都会
    新建连接 → 天然满足「SQLite 连接绑定线程」的约束。
    ``orb_fn``: ORB 兜底溯源 (慢路)。不传则 deep 只会标出 deep_error, 绝不偷偷跑重活。
    """
    f = wallpaper_file() if wall_path is _UNSET else (Path(wall_path) if wall_path else None)
    if f is None:
        return _no_wallpaper("注册表里没有可用的壁纸文件（没开幻灯片，或壁纸文件已被移走）")
    # f 必须和 img_root 一样做 resolve 规范化 (2026-09-24 实测): 注册表给的壁纸路径
    # 可能是 8.3 短名 (ADMINI~1) 或经由 junction, 不规范化会让 in_lib 误判成「库外」——
    # 后果不止结论降级 (library 1.0 → id 0.9), 更严重的是「移出壁纸」的只读护栏
    # 会失效: 明明在图库内的文件被允许删除, 绕过数据库软删除。
    try:
        f = f.resolve()
    except OSError:
        pass
    if img_root is None:
        img_root = _default_img_root()
    else:
        img_root = Path(img_root).resolve()
    img_url_fn = img_url_fn or local_img_url
    factory = db_factory or _default_db

    try:
        st = f.stat()
    except OSError:
        return _no_wallpaper("壁纸文件已读不到")
    fkey = f"{f}|{st.st_mtime_ns}-{st.st_size}"
    with WALL_DEEP_LOCK:
        cached_deep = WALL_DEEP_CACHE.get(fkey)
    deep = deep or cached_deep is not None      # 算过一次就一直沿用, 界面不来回跳
    ww, wh = wallpaper_size(f)
    in_lib = (f == img_root) or (img_root in f.parents)
    info = {
        "status": "ok", "active": True, "deep": deep,
        "file": {
            "name": f.name, "dir": str(f.parent), "path": str(f),
            "size": st.st_size, "mtime": int(st.st_mtime), "ext": f.suffix.lower(),
            "width": ww, "height": wh, "in_library": 1 if in_lib else 0,
            "thumb": (f"/api/local-img?p={quote(f.name)}"
                      f"&root={'img' if in_lib else 'wall'}&thumb=1"
                      f"&v={st.st_mtime_ns}-{st.st_size}"),
        },
        "match": {"kind": "none", "confidence": 0.0, "rel": "",
                  "page_num": None, "exact": False, "relation": ""},
        "artwork": None, "pages": [], "candidates": [], "bigger": 0,
    }
    # 壁纸夹里的位置 (第几张/共几张) —— 小控件底部那条进度线要用 (widget_window.folder_frac)
    _idx, _tot = wallpaper_position(f)
    info["folder"] = {"index": _idx, "total": _tot}
    rel = aid = orb_rel = ""
    if in_lib:
        rel = str(f.relative_to(img_root)).replace("\\", "/")
        info["match"].update(kind="library", confidence=1.0, rel=rel, exact=True)

    # ── 第一段 (需要 DB): 三条秒回的路 —— 文件在库内 / MD5 字节一致 / 文件名作品ID ──
    with factory() as db:
        if not rel:
            md5 = file_md5(f)
            row = db.find_page_by_md5(md5) if md5 else None
            if row:
                rel = str(row.get("local_path") or "").replace("\\", "/")
                info["match"].update(kind="md5", confidence=1.0, rel=rel,
                                     page_num=row.get("page_num"), exact=True)
        if not rel:
            nk = pixiv_natural_key(f.name)
            if nk and db.conn.execute("SELECT 1 FROM artworks WHERE artwork_id=?",
                                      (str(nk[0]),)).fetchone():
                aid = str(nk[0])
                info["match"].update(kind="id", confidence=0.9)
                # 文件名里的页码指向的那一张 = 这条线索对应的库内页
                for r in (db.get_artwork_page_list([aid]) or {}).get(aid, []):
                    if int(r.get("page_num") or 0) == int(nk[1]):
                        rel = str(r.get("local_path") or "").replace("\\", "/")
                        info["match"]["rel"] = rel
                        info["match"]["page_num"] = r.get("page_num")
                        break
        if not aid and rel:
            row = _page_by_rel(db, rel)
            aid = str(row["artwork_id"]) if row else ""

    # ── 第二段 (不占 DB 连接): ORB 兜底慢路 —— 首次要加载整库特征, 可能几秒~几十秒 ──
    if not aid and deep:
        cands = cached_deep
        if cands is None:
            if orb_fn is None:
                info["deep_error"] = "图像检索器未接入"
                cands = []
            else:
                cands = orb_fn(f, img_root)
            # 报错不缓存: 一次瞬时失败 (抢 _ls_lock / 内存不足) 若被钉住, 用户再点
            # 「图像溯源」也只会反复看到同一个错误, 永远等不到重试。
            if not any(c.get("error") for c in cands):
                with WALL_DEEP_LOCK:
                    if len(WALL_DEEP_CACHE) > 8:
                        WALL_DEEP_CACHE.clear()
                    WALL_DEEP_CACHE[fkey] = cands
        err = next((c.get("error") for c in cands if c.get("error")), "")
        if err:
            info["deep_error"] = err
        cands = [c for c in cands if c.get("rel")]
        info["candidates"] = cands
        if cands and cands[0]["orb"] >= WALL_ORB_MIN:
            orb_rel = cands[0]["rel"]
            info["match"].update(kind="orb", confidence=cands[0]["fusion"], rel=orb_rel)
        elif cands:
            # 有候选但分数太低: 只说「疑似」, 绝不硬认 (宁可让用户自己看)
            info["match"].update(kind="weak", confidence=cands[0]["fusion"],
                                 rel=cands[0]["rel"])

    # ── 第三段 (需要 DB): 补全命中页 + 取作品详情 ──
    if aid or orb_rel:
        with factory() as db:
            if not aid and orb_rel:
                row = _page_by_rel(db, orb_rel)
                if row:
                    aid = str(row["artwork_id"])
                    info["match"]["page_num"] = row["page_num"]
            if aid:
                info["artwork"] = _wall_artwork(db, aid, img_root, img_url_fn)

    # ── 收尾 (纯算术): 形态差异 + 库内有没有更高清 ──
    art = info["artwork"]
    if art:
        info["pages"] = art.pop("pages")
        page = next((p for p in info["pages"]
                     if info["match"].get("page_num") is not None
                     and p["page_num"] == info["match"]["page_num"]), None)
        if page:
            info["match"]["relation"] = wall_relation(
                (ww, wh), (page["width"], page["height"]))
        best = None
        for p in info["pages"]:
            if not (p["width"] and p["height"]):
                continue
            if best is None or p["width"] * p["height"] > best["width"] * best["height"]:
                best = p
        if best:
            best["biggest"] = 1
            info["bigger"] = 1 if best["width"] * best["height"] > ww * wh else 0
    info["summary"] = build_summary(info)
    return info