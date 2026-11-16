# core/wallpaper_index.py
"""🖼 壁纸夹归属索引 —— 2026-11-16

把「壁纸夹里每个文件到底是图库里的哪一页」**正向落库** (表 wallpaper_files),
之后列表 / 统计 / 查重全部读这张表, 秒回, 不再每次实时跑 ORB。

为什么需要它 (用户 2026-11 反馈):
  壁纸夹里很多图是从图库手动裁剪或另存出来的 —— 文件名不带作品 ID、字节也和原图不同。
  只靠「文件名 / MD5」反推会漏, 于是同一作品的壁纸越堆越多还查不出来。
  先扫一遍建立归属 (含 ORB 特征兜底), 之后「设为壁纸」时的重复检测才有准确依据。

扫描分两段, 与用户的耐心匹配:
  ① 快扫 (秒级): 文件名里的作品 ID + MD5 与库内页一致, 命中即归属;
  ② 慢扫 (可选): 剩下的按修改时间倒序跑 ORB 图像特征, 命中写 match_kind='visual'。
     文件没变且有归属的**直接跳过**, 第二次扫描只处理新增/改动的文件。

本模块只读写 DB 与文件元信息, **不删任何文件** —— 删副本一律由界面勾选后走回收站。
ORB 检索器由调用方注入 (server 里有整库单例), 避免这里再加载一份特征库。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from core.wallpaper_trace import (
    WALL_ORB_MIN,
    library_md5_index,
    md5_cached,
    wallpaper_folder_images,
    wallpaper_size,
)

# pixiv 作品 ID: 6~9 位纯数字, 前后不能再连数字 (避免把时间戳/分辨率当成作品 ID)
_ID_RE = re.compile(r"(?<!\d)(\d{6,9})(?!\d)")
# 页号: 项目自己的命名是 {artwork_id}_p{page}.ext, 裁剪版是 ..._p{page}_crop.jpg
_PAGE_RE = re.compile(r"_p(\d{1,4})(?!\d)", re.I)

# match_kind → 界面上的一句话 (前端按 key 取文案, 不自己拼)
MATCH_LABELS = {
    "md5": "内容一致",
    "name": "文件名带 ID",
    "visual": "图像特征",
    "manual": "手动指定",
}


def page_from_name(name: str) -> "int | None":
    """从文件名里取页号 (``12345678_p3.jpg`` → 3); 取不到 → None。"""
    m = _PAGE_RE.search(str(name or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _fmt_sec(n: float) -> float:
    return round(float(n or 0), 1)


def scan_wall_dir(db_factory, wall_dir, img_root: "Path | None" = None,
                  orb_fn=None, progress_cb=None, deep: bool = True) -> dict:
    """扫 ``wall_dir`` 建立归属并写回 ``wallpaper_files``, 返回本次统计。

    - ``db_factory``: 每次调用都新建连接 (SQLite 连接绑线程, 后台线程里不能复用)。
    - ``orb_fn(f, img_root)``: 慢扫用的 ORB 溯源函数, 签名与 server 里那份一致;
      不给就只做快扫。
    - ``progress_cb(stage, done, total, name)``: stage = ``fast`` / ``visual``。
    - ``deep=False``: 跳过 ORB 慢扫 (只处理快扫口径)。

    手动归属（包括解除归属）优先；文件内容改变后才重新自动识别。
    """
    wall_dir = Path(wall_dir)
    files = wallpaper_folder_images(wall_dir)
    total = len(files)
    t0 = time.time()
    stats = {"dir": str(wall_dir), "total": total, "owned": 0, "orphan": 0,
             "by_md5": 0, "by_name": 0, "by_visual": 0, "manual": 0,
             "reused": 0, "removed": 0, "dupes": 0, "seconds": 0.0}

    def prog(stage: str, done: int, name: str, ttl: int = total) -> None:
        if not progress_cb:
            return
        try:
            progress_cb(stage, done, ttl, name)
        except Exception:
            pass

    with db_factory() as db:
        prev = {str(r["file_name"]): dict(r)
                for r in db.conn.execute("SELECT * FROM wallpaper_files").fetchall()}
        md5_index = library_md5_index(db)
        try:
            aid_set = {str(r[0]) for r in
                       db.conn.execute("SELECT artwork_id FROM artworks").fetchall()}
        except Exception:
            aid_set = set()
        try:
            rel_map = {
                str(r[0]).replace("\\", "/"): (str(r[1]), int(r[2] or 0))
                for r in db.conn.execute(
                    "SELECT local_path, artwork_id, page_num FROM pages "
                    "WHERE local_path IS NOT NULL").fetchall()
            }
        except Exception:
            rel_map = {}

        keep: "list[dict]" = []
        pending: "list[tuple[Path, int]]" = []
        now_txt = time.strftime("%Y-%m-%d %H:%M:%S")

        for i, f in enumerate(files, 1):
            prog("fast", i, f.name)
            try:
                st = f.stat()
            except OSError:
                continue
            old = prev.get(f.name)
            unchanged = bool(
                old and Path(old.get("dir") or "").resolve() == wall_dir.resolve()
                and int(old.get("size") or 0) == st.st_size
                and abs(float(old.get("mtime") or 0) - st.st_mtime) < 1.0)
            has_owner = bool(old and str(old.get("artwork_id") or "").strip())
            manual = bool(old and old.get("match_kind") == "manual")
            if unchanged and manual:
                keep.append(dict(old))
                stats["reused"] += 1
                continue
            # 没变 + 已有归属 → 直接复用, 连 MD5 都不重算 (整夹重扫也是秒级)
            needs_page = bool(has_owner and old.get("page_num") is None
                              and deep and orb_fn is not None)
            if unchanged and has_owner and not needs_page:
                keep.append(dict(old))
                stats["reused"] += 1
                continue
            # 没变 + 未归属: 深扫时还要再试一次 ORB, 浅扫就先按旧记录留着
            if unchanged and not deep:
                keep.append(dict(old))
                continue

            h = md5_cached(f)
            aid, pn, kind, conf = "", None, "", 0.0
            hit = md5_index.get(h) if h else None
            if manual and old.get("md5") and old["md5"] == h:
                aid, pn, kind, conf = old.get("artwork_id"), old.get("page_num"), "manual", 1.0
            elif hit:
                aid, pn, kind, conf = hit["artwork_id"], hit["page_num"], "md5", 1.0
            else:
                m = _ID_RE.search(f.name)
                if m and m.group(1) in aid_set:
                    aid, pn, kind, conf = m.group(1), page_from_name(f.name), "name", 0.9
            try:
                wh = wallpaper_size(f)
            except Exception:
                wh = (0, 0)
            rec = {"file_name": f.name, "dir": str(wall_dir), "mtime": st.st_mtime,
                   "size": st.st_size, "md5": h, "width": wh[0], "height": wh[1],
                   "artwork_id": aid or None, "page_num": pn,
                   "match_kind": kind, "confidence": conf, "scanned_at": now_txt}
            keep.append(rec)
            if pn is None and kind != "manual" and deep and orb_fn is not None:
                pending.append((f, len(keep) - 1))

        # 慢扫: 新文件优先 (用户刚裁剪出来的最可能是要找的那张)
        if pending:
            try:
                pending.sort(key=lambda it: it[0].stat().st_mtime, reverse=True)
            except Exception:
                pass
            n = len(pending)
            for i, (f, pos) in enumerate(pending, 1):
                prog("visual", i, f.name, n)
                try:
                    cands = orb_fn(f, img_root) if img_root is not None else orb_fn(f)
                except Exception:
                    continue
                if not cands:
                    continue
                top = cands[0]
                if top.get("error"):
                    continue
                score = float(top.get("orb") or 0)
                if score < WALL_ORB_MIN:
                    continue
                rel = str(top.get("rel") or "").replace("\\", "/")
                hp = rel_map.get(rel)
                if not hp:
                    continue
                rec = keep[pos]
                if rec.get("artwork_id") and str(rec["artwork_id"]) != hp[0]:
                    continue  # 已知作品但未知图片时，不用别的作品的相似图补页号。
                rec["artwork_id"] = hp[0]
                rec["page_num"] = hp[1]
                rec["match_kind"] = "visual"
                rec["confidence"] = round(score, 3)

        # 壁纸夹里已经不存在的文件: 记录一并清掉 (只动 DB, 不碰磁盘)
        alive = {r["file_name"] for r in keep}
        gone = [n for n in prev if n not in alive]
        with db.conn:
            db.conn.executemany(
                "INSERT INTO wallpaper_files(file_name, dir, mtime, size, md5, width, height, "
                "artwork_id, page_num, match_kind, confidence, scanned_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_name) DO UPDATE SET dir=excluded.dir, mtime=excluded.mtime, "
                "size=excluded.size, md5=excluded.md5, width=excluded.width, "
                "height=excluded.height, artwork_id=excluded.artwork_id, "
                "page_num=excluded.page_num, match_kind=excluded.match_kind, "
                "confidence=excluded.confidence, scanned_at=excluded.scanned_at",
                [(r["file_name"], r["dir"], r["mtime"], r["size"], r["md5"],
                  r["width"], r["height"], r["artwork_id"], r["page_num"],
                  r["match_kind"], r["confidence"], r["scanned_at"]) for r in keep])
            if gone:
                db.conn.executemany(
                    "DELETE FROM wallpaper_files WHERE file_name=?",
                    [(n,) for n in gone])

    stats["removed"] = len(gone)
    stats["seconds"] = _fmt_sec(time.time() - t0)
    for r in keep:
        if r.get("artwork_id"):
            stats["owned"] += 1
            k = str(r.get("match_kind") or "")
            if k in ("md5", "name", "visual", "manual"):
                stats["by_" + k] = stats.get("by_" + k, 0) + 1
        else:
            stats["orphan"] += 1
    stats["dupes"] = len(group_dupes(keep))
    return stats


def list_wall_files(db_factory) -> "list[dict]":
    """壁纸夹索引 + 作品标题/作者 (LEFT JOIN: 归属可能悬空, 取不到就留空)。"""
    with db_factory() as db:
        rows = db.conn.execute(
            "SELECT wf.file_name, wf.dir, wf.mtime, wf.size, wf.md5, wf.width, wf.height, "
            "       wf.artwork_id, wf.page_num, wf.match_kind, wf.confidence, wf.scanned_at, "
            "       wf.favorite, wf.labels_json, wf.group_id, g.name AS group_name, a.source, a.source_url, "
            "       a.title AS title, a.is_r18 AS is_r18, "
            "       COALESCE(au.name, a.author_id, '') AS author_name "
            "FROM wallpaper_files wf "
            "LEFT JOIN wallpaper_groups g ON g.id=wf.group_id "
            "LEFT JOIN artworks a  ON a.artwork_id = wf.artwork_id "
            "LEFT JOIN authors  au ON au.author_id = a.author_id "
            "ORDER BY wf.file_name").fetchall()
    return [dict(r) for r in rows]


def _row_name(r: dict) -> str:
    """记录里的文件名: 索引行用 file_name, 接口组装后的条目用 name。"""
    return str(r.get("file_name") or r.get("name") or "")


def _image_keys(row: dict) -> list:
    """只有具体图片归属或非空 MD5 才能证明同图；作品归属本身不能。"""
    keys = []
    aid = str(row.get("artwork_id") or "").strip()
    pn = row.get("page_num")
    try:
        if aid and pn is not None and int(pn) >= 0:
            keys.append(("page", aid, int(pn)))
    except (TypeError, ValueError):
        pass
    md5 = str(row.get("md5") or "").strip().lower()
    if md5:
        keys.append(("md5", md5))
    return keys


def _image_components(rows: "list[dict]") -> list:
    """同图关系取并集，避免原图/裁剪版与跨作品的字节副本分组相互遗漏。"""
    unique = {_row_name(r): r for r in rows if _row_name(r)}
    items = list(unique.values())
    parents = list(range(len(items)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    seen = {}
    for i, row in enumerate(items):
        for key in _image_keys(row):
            if key in seen:
                parents[root(i)] = root(seen[key])
            else:
                seen[key] = i
    grouped = {}
    for i, row in enumerate(items):
        grouped.setdefault(root(i), []).append(row)
    return list(grouped.values())


def matching_wall_files(rows: "list[dict]", artwork_id: str,
                        page_num: "int | None", md5: str = "") -> "list[dict]":
    """返回对应某张原图的全部已有版本，与管理页分组使用完全相同的口径。"""
    keys = set(_image_keys({"artwork_id": artwork_id, "page_num": page_num, "md5": md5}))
    if not keys:
        return []
    return [row for group in _image_components(rows)
            if any(keys.intersection(_image_keys(r)) for r in group) for row in group]


def group_dupes(rows: "list[dict]") -> "list[dict]":
    """同一张图（含裁剪/缩放版本）归组，只报告，不删除或决定保留哪张。"""
    groups = []
    for items in _image_components(rows):
        if len(items) < 2:
            continue
        first = items[0]
        page_keys = {k for r in items for k in _image_keys(r) if k[0] == "page"}
        page_key = next(iter(page_keys)) if len(page_keys) == 1 else None
        groups.append({
            "key": ("p:%s:%s" % page_key[1:]) if page_key else "image:" + min(_row_name(r) for r in items),
            "kind": "page" if page_key else "image",
            "artwork_id": page_key[1] if page_key else "",
            "page_num": page_key[2] if page_key else None,
            "title": first.get("title") or "", "author_name": first.get("author_name") or "",
            "files": sorted(_row_name(r) for r in items),
        })
    groups.sort(key=lambda g: (-len(g["files"]), g["key"]))
    return groups


def upsert_wall_file(db, wall_dir, path: Path, artwork_id: str, page_num: "int | None" = None,
                     match_kind: str = "manual", confidence: float = 1.0) -> None:
    """把「刚设为壁纸的那一张」立即写进索引 —— 不必等下次整夹扫描。

    调用方已经知道归属 (作品 ID + 页号) 时用这个, 比重新识别可靠。
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return
    try:
        wh = wallpaper_size(p)
    except Exception:
        wh = (0, 0)
    now_txt = time.strftime("%Y-%m-%d %H:%M:%S")
    with db.conn:
        db.conn.execute(
            "INSERT INTO wallpaper_files(file_name, dir, mtime, size, md5, width, height, "
            "artwork_id, page_num, match_kind, confidence, scanned_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_name) DO UPDATE SET dir=excluded.dir, mtime=excluded.mtime, "
            "size=excluded.size, md5=excluded.md5, width=excluded.width, "
            "height=excluded.height, artwork_id=excluded.artwork_id, "
            "page_num=excluded.page_num, match_kind=excluded.match_kind, "
            "confidence=excluded.confidence, scanned_at=excluded.scanned_at",
            (p.name, str(wall_dir), st.st_mtime, st.st_size, md5_cached(p),
             wh[0], wh[1], str(artwork_id), page_num, match_kind,
             float(confidence), now_txt))


def forget_wall_file(db, file_name: str) -> None:
    """文件被移出壁纸夹后, 把索引里那条删掉 (只动 DB)。"""
    with db.conn:
        db.conn.execute("DELETE FROM wallpaper_files WHERE file_name=?", (str(file_name),))
