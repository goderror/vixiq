# core/wallpaper_lib.py
"""🖼 壁纸库 (2026-09-27): 把图库里**够格当桌面**的那些页, 原样复制进壁纸夹并登记到 wallpapers 表。

背景: 桌面幻灯片读的是**壁纸夹** (配置 ``paths.wallpaper_dir``), 而图库里上千张图绝大多数不适合
当桌面 —— 竖图、超宽图、小图铺上去要么留两条黑边、要么被 Windows 拉变形。手工挑是不可能挑的,
所以按固定规则筛一遍、复制过去。

规则 (用户 2026-09-27 定案):

- **尺寸门槛** 宽 ≥ 1920 且 高 ≥ 1080。本机物理分辨率是 1920×1080, 这里写**显式常量** ——
  ``Screen.Bounds`` 在非 DPI 感知进程里遇到 125% 缩放会给 1536×864, 门槛会静默放松
  (项目里踩过: 验证脚本拿到虚化坐标导致点击落空)。
- **比例门槛** ``|图宽高比 ÷ (16/9) − 1| ≤ 0.10``。只判尺寸的话 2560×1080 这种超宽图也会进来,
  铺上去两侧黑边比竖图还难看。实测 1180 张通过 510 张。
- **原样复制**: 不缩放、不裁剪、不转码 —— 桌面上那张就是图库里那一张, 字节一致。
  ``crop_mode`` / ``crop_x0..y1`` 留给以后「手动框选再裁一张」用 (那时同一页能派生多张,
  正是 018 表里那组「一对多」的用途), 本模块一律留空。
- **文件名** ``{artwork_id}_p{page_num}.{ext}``: 名字里带血缘, 看一眼就知道这张从哪来,
  也天然幂等 —— 同一页重复扫, 名字不变, 不会越跑越多 (见 :meth:`store.add_wallpapers` 的幂等)。

**为什么是手动按钮而不是入库钩子**: 510 张得先跑一遍才知道规则挑得对不对; 钩子挂上去就不好收了。
入口在设置页「桌面与壁纸 → 壁纸库」, 接口见 ``web/server.py`` 的 ``/api/wallpaper/lib/scan``。

与 ``core/wallpaper_trace.py`` 的分工: 那个模块**只读** (桌面这张是图库里的谁), 本模块**写盘 + 写库**。
两边取壁纸夹的口径是同一个 (``wallpaper_trace.wallpaper_dir``), 不在这里另写一份。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from core.wallpaper_trace import WALL_EXTS

# 屏幕门槛 (显式常量, 不用 Screen.Bounds 推导 —— 理由见模块头)
MIN_W, MIN_H = 1920, 1080
# 与 16:9 的比例偏差门槛
TARGET_AR = 16 / 9
AR_TOL = 0.10

# 出错条目最多回报这么多条 (剩下的只进计数, 免得异常信息把响应体刷爆)
MAX_ERRORS = 20


class WallpaperLibError(RuntimeError):
    """生成壁纸失败 (没配壁纸夹 / 目录读不到) —— 只带一句给人看的话。"""


def wallpaper_gate(width, height) -> str:
    """这一页过不过门槛 → ``"ok"`` / ``"size"`` / ``"ratio"``。

    两道门槛**分开回话**是为了让调用方能分项计数: 用户看到「尺寸刷掉 600、比例刷掉 70」,
    才知道门槛定得合不合适; 只回一个 False 就只剩一个总数, 什么都调不了。
    宽高缺失 / 读出来是 0 → 当 ``"size"`` 刷掉 (除零也算在内)。
    """
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return "size"
    if w < MIN_W or h < MIN_H or not h:
        return "size"
    return "ok" if abs((w / h) / TARGET_AR - 1.0) <= AR_TOL else "ratio"


def wallpaper_worthy(width, height) -> bool:
    """够格当桌面 (两道门槛都过)。判定口径只有 :func:`wallpaper_gate` 一份。"""
    return wallpaper_gate(width, height) == "ok"


def wallpaper_file_name(artwork_id, page_num, ext: str) -> str:
    """壁纸夹里的文件名 ``{artwork_id}_p{page_num}{ext}`` (ext 带点, 小写)。"""
    e = str(ext or "").lower()
    if e and not e.startswith("."):
        e = "." + e
    try:
        pn = int(page_num or 0)
    except (TypeError, ValueError):
        pn = 0
    return f"{str(artwork_id or '').strip()}_p{pn}{e}"


def library_candidates(db) -> "list[dict]":
    """参与判定的页: 作品与页都**未标记**, 且带 md5 / 宽高 / 相对路径。

    只承担「把明显的废行滤掉」这一层 —— 文件在不在盘上、过不过两道门槛, 一律在下面按行判。
    SQL 里塞不下比例公式, 门槛也不该存在两份 (改一处忘一处最要命)。
    要求 md5 是因为它要写进 ``file_md5``/``src_md5``: 壁纸夹是用户会手动动的地方, 没有指纹就
    发现不了「文件被换过」, 以后重裁也判不了原图有没有变。
    """
    rows = db.conn.execute(
        "SELECT p.artwork_id AS artwork_id, p.page_num AS page_num, "
        "       p.local_path AS rel, p.md5 AS md5, p.width AS width, p.height AS height "
        "FROM pages p JOIN artworks a ON a.artwork_id = p.artwork_id "
        "WHERE p.trashed_at IS NULL AND a.trashed_at IS NULL "
        "  AND p.local_path IS NOT NULL AND p.local_path <> '' "
        "  AND p.md5 IS NOT NULL AND p.md5 <> '' "
        "  AND p.width IS NOT NULL AND p.height IS NOT NULL "
        "ORDER BY p.artwork_id, p.page_num").fetchall()
    return [dict(r) for r in rows]


def generate(db, wall_dir, img_root, progress_cb=None) -> dict:
    """扫图库 → 按规则把够格的页**原样复制**进 ``wall_dir`` → 登记到 wallpapers 表。

    **幂等**: 已登记 (未标记) 的文件名直接跳过, 同一页重复跑不会越跑越多文件。壁纸夹里
    有个同名文件但没登记 (上次复制完登记失败) 会被覆盖 —— 名字是按血缘定的, 内容同一份,
    覆盖等于把上次没收尾的事做完。

    ``progress_cb(done, total, name)`` 每张图前调一次 —— 上千行 + 几百 MB 的复制要跑一阵,
    没进度的话界面上看着就像卡死。

    返回统计 (给界面显示, 也是「门槛定得对不对」的唯一依据)::

        {"scanned", "passed", "added", "skipped_exist", "skipped_size",
         "skipped_ratio", "skipped_ext", "missing", "errors", "wall_dir", "elapsed"}
    """
    if not wall_dir:
        raise WallpaperLibError("还没指定壁纸夹（设置 → 桌面与壁纸 → 壁纸夹）")
    wall = Path(wall_dir)
    if not wall.is_dir():
        raise WallpaperLibError(f"壁纸夹读不到: {wall}")
    if not img_root:
        raise WallpaperLibError("图库目录读不到")
    root = Path(img_root)
    if not root.is_dir():
        raise WallpaperLibError(f"图库目录读不到: {root}")

    rows = library_candidates(db)
    total = len(rows)
    registered = db.wallpaper_file_names()      # 未标记的才占名字 (标记过的可复用同名)
    items, errors = [], []
    stat = {"scanned": total, "passed": 0, "added": 0, "skipped_exist": 0,
            "skipped_size": 0, "skipped_ratio": 0, "skipped_ext": 0, "missing": 0}
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        if progress_cb:
            try:
                progress_cb(i, total, f"{r['artwork_id']}_p{r['page_num']}")
            except Exception:
                pass                        # 进度回调是界面的事, 坏了不能连累生成
        gate = wallpaper_gate(r["width"], r["height"])
        if gate != "ok":
            stat["skipped_size" if gate == "size" else "skipped_ratio"] += 1
            continue
        stat["passed"] += 1
        rel = str(r["rel"] or "")
        src = root / rel
        if not src.is_file():
            stat["missing"] += 1            # 记录里那一页的文件已不在盘上 (对账/自检该管的活)
            continue
        ext = Path(rel).suffix.lower()
        if ext not in WALL_EXTS:
            # 壁纸夹是给 Windows 幻灯片读的, webp/avif 放进去也播不出来, 白占地方
            stat["skipped_ext"] += 1
            continue
        name = wallpaper_file_name(r["artwork_id"], r["page_num"], ext)
        if name in registered:
            stat["skipped_exist"] += 1
            continue
        try:
            shutil.copy2(str(src), str(wall / name))
            size = (wall / name).stat().st_size
        except Exception as e:
            if len(errors) < MAX_ERRORS:
                errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        registered.add(name)
        items.append({"artwork_id": r["artwork_id"], "page_num": r["page_num"],
                      "file_name": name, "file_md5": r["md5"], "src_md5": r["md5"],
                      "file_size_bytes": size,
                      "width": int(r["width"]), "height": int(r["height"])})

    res = db.add_wallpapers(items)
    stat["added"] = int(res.get("added") or 0)
    stat["wall_dir"] = str(wall)
    stat["elapsed"] = round(time.time() - t0, 1)
    stat["errors"] = errors
    return stat