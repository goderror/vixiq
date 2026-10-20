# web/server.py
"""
Flask 后端: 复用 core/db 全部能力, 通过 SSE 把进度实时推到浏览器。

取代 Tkinter gui/ 的职责:
  - 搜索 (SearchClient.execute)           → GET /api/search
  - 购物车 加入/列表/删除/下载            → /api/cart*
  - 下载进度 (run_cart + item_progress)   → SSE /api/events
  - 反搜来源 (ReverseSource/ReverseSearch) → /api/reverse*
  - 图库管理 (stats/search/backup)        → /api/library*
  - 登录管理 (开可见浏览器手动登录+保存)   → /api/auth*

启动入口见 main.py (启动器)。浏览器打开 http://127.0.0.1:<port>/ 即可用。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from core.models import DownloadStrategy
from core.search_client import (
    AuthorNotFoundError,
    ArtworkNotFoundError,
    PixivWebActionError,
    SearchClient,
    parse_input,
    web_bookmark_add,
    web_bookmark_state,
    web_follow,
    web_follow_state,
    web_unfollow,
)
from core.download_engine import run_cart
# 桌面壁纸溯源唯一规则  与 utils/tray.py 的托盘共用同一份, 不各写一份
from core.wallpaper_trace import (
    wallpaper_file,
    wall_relation,
    img_v,
    local_img_url,
    next_wallpaper,
    prev_wallpaper,
    candidates_dir,
    apply_wallpaper,
    find_wall_dupes,
    WALL_DEEP_CACHE,
    trace_current_wallpaper,
)
from db.store import PixivDatabase, WSt, PSt, pixiv_natural_key, parse_foreign_account, \
    folder_author_name_usable
from utils import paths
from utils import audit_log
# 作者夹命名唯一规则 (2026-09-21 治本): 一键整理/下载器/反搜/自检共用同一套,
# 不再各写一份非法字符处理规则 —— 否则同一位作者会被摆成两个夹
from utils.author_naming import (
    POLLUTE_PAREN as _POLLUTE_PAREN,
    author_folder_name as _author_folder_name,
    folder_base as _folder_base,
    is_unreadable_name as _is_unreadable_name,
    sanitize_dir as _sanitize_dir,
    search_promo as _search_promo,
    strip_promo as _strip_promo,
)
from utils.config import load_config
from utils.logger import build_reporter

from web.event_bus import EventHub, WebReporter

# =============================================================
# 全局单例 (Flask dev server threaded=True, 多线程并发安全)
# =============================================================
_hub = EventHub()
_cancel_event = threading.Event()

# 启动器开关的热应用钩子  main.py 启动时用 set_launcher_apply() 把
# 「起/停托盘、起/停小控件、开关心跳守护」这套进程级动作交进来 —— web 层只负责存盘与回调,
# 不掺和进程生命周期 (serve 里没有 tray/widget 对象)。没接钩子时 (如冒烟测试直接 create_app)
# 只是不热应用, 开关照常存盘。
_launcher_apply = None


def set_launcher_apply(fn) -> None:
    """注册「启动与托盘」开关的热应用回调: 收 {键: 布尔} → 返回 {键: 结果}。"""
    global _launcher_apply
    _launcher_apply = fn

# ---------------------------------------------------------------------------
# 🔔 数据域「脏」登记 + 事件驱动刷新 (一套覆盖全库的机制)
#   问题: 以前每个写接口各自"记得"通知前端刷新, 漏一个就出现"改了但界面没变",
#         用户只能跑全量体检或手动 F5; 也不该让每个页面各写一套刷新逻辑。
#   做法 = 事件驱动的标准三件套 (写入侧登记 → 版本号 → 订阅侧定向重算):
#     ① 任何一次 /api 写请求都在 after_request 里按语义登记它影响的「数据域」(scope)
#        并从请求体里抽出受影响的实体 id —— 零侵入, 新接口自动被覆盖;
#     ② 每个 scope 维护 rev 与最近受影响的 ids, 登记时经 SSE 广播 {"type":"dirty"…};
#     ③ 前端按 scope 只刷新对应视图(图库/作者/统计/任务/体检/反搜/来源/设置), 并用
#        GET /api/dirty?since=<rev> 做断线补偿 —— 推送 + 轮询补偿 = 事件不丢。
#     scope 枚举与前端 SCOPE_REFRESH 一一对应, 前端只在「该视图正打开」时才去拉数据。
#   体检步骤、下载线程等**非 HTTP 触发**的改动在各自代码里显式调 mark_dirty()。
#   注意: 只登记"真的改了数据"的写请求 —— 只读的定时轮询(periodic=1)不登记,
#         否则空闲期也会不断自增版本, 前端就会莫名其妙地反复重拉 (实测噪声)。
# ---------------------------------------------------------------------------
_DIRTY_SCOPES = ("artworks", "pages", "authors", "facets", "stats", "queue",
                 "check", "reverse", "sources", "search",
                 # 非图库数据的域: 设置页(安全模式/缩略图缓存)、登录态、备份、操作记录
                 "settings", "auth", "thumbs", "backups", "ops")
_dirty_state = {"rev": 0, "scopes": {}}   # {scope: {"rev": n, "ids": [...], "at": ts}}
_dirty_lock = threading.Lock()
_DIRTY_ID_CAP = 200                        # 单个 scope 记住的 id 数上限, 超了记 "*"=整体失效


def mark_dirty(*scopes, ids=None) -> None:
    """登记「这些数据域变了」→ 自增版本 + SSE 广播, 让前端只重算那一块。

    ids 给的是"这次具体动了哪些实体"; 合并进该 scope 的待刷新集合 (前端可据此高亮/定位)。
    超过上限就退化成 "*"(整域失效, 全量重拉该视图) —— 避免集合无限膨胀。"""
    names = [s for s in dict.fromkeys(scopes or []) if s in _DIRTY_SCOPES]
    if not names:
        return
    id_list = [str(x) for x in (ids or []) if str(x).strip()][:_DIRTY_ID_CAP]
    with _dirty_lock:
        _dirty_state["rev"] += 1
        rev = _dirty_state["rev"]
        now = time.time()
        out = {}
        for n in names:
            cur = _dirty_state["scopes"].setdefault(n, {"rev": 0, "ids": [], "at": 0.0})
            cur["rev"] = rev
            cur["at"] = now
            if cur["ids"] != ["*"]:
                merged = list(dict.fromkeys(list(cur["ids"]) + id_list))
                cur["ids"] = merged if len(merged) <= _DIRTY_ID_CAP else ["*"]
            out[n] = {"rev": cur["rev"], "ids": list(cur["ids"])}
        payload = {"type": "dirty", "rev": rev, "scopes": out}
    try:
        _hub.publish(payload)
    except Exception:
        pass


def _dirty_snapshot(since: int = 0) -> dict:
    """取 since 之后变脏的数据域 (since=0 = 全部当前状态)。前端断线补偿用。"""
    with _dirty_lock:
        rev = _dirty_state["rev"]
        scopes = {k: {"rev": v["rev"], "ids": list(v["ids"]), "at": v["at"]}
                  for k, v in _dirty_state["scopes"].items() if int(v["rev"]) > int(since)}
    return {"rev": rev, "scopes": scopes}


def _scopes_for_request(path: str, method: str, body: "dict | None",
                        query: "dict | None" = None) -> "tuple[tuple, list]":
    """一次写请求影响哪些数据域 (按 HTTP 语义映射, 不依赖各接口自觉申报)。

    只对写方法生效; 4xx/5xx 由调用方过滤 (失败请求不该让前端白刷一遍)。
    query 用来识别「只读轮询」这类写方法: 例如 /api/library/sync?periodic=1
    是前端 15s 定时调用的对账轮询, 它可能什么都不改 → 不登记, 免得空闲期版本空转。"""
    if str(method).upper() not in ("POST", "PUT", "PATCH", "DELETE"):
        return (), []
    if str((query or {}).get("periodic", "")) == "1":
        return (), []
    p = str(path or "")
    ids: "list[str]" = []
    if isinstance(body, dict):
        for k in ("artwork_id", "local_artwork_id", "target_artwork_id", "author_id",
                  "item_id", "rel", "name"):
            v = body.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                ids.append(str(v))
        for k in ("ids", "artwork_ids", "items"):
            v = body.get(k)
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, dict):
                        if x.get("artwork_id"):
                            ids.append(str(x["artwork_id"]))
                    elif str(x).strip():
                        ids.append(str(x))
    if p.startswith("/api/heartbeat") or p.startswith("/api/bye") or p.startswith("/api/focus"):
        return (), ids
    if p.startswith("/api/safemode"):
        # R18 打码开关: 改的是渲染方式, 图库/搜索的图都要按新状态重画
        return ("settings", "artworks"), ids
    if p.startswith("/api/launcher"):
        # 启动器形态开关 (托盘/自动开浏览器/壁纸小控件): 只影响设置页那张卡
        return ("settings",), ids
    if p.startswith("/api/auth/"):
        return ("auth",), ids
    if p.startswith("/api/thumbs/"):
        return ("thumbs",), ids
    if p.startswith("/api/logs/operations/clear"):
        return ("ops",), ids
    if p.startswith("/api/cart") or p.startswith("/api/tasks"):
        # search: 查询页每张卡上的「已入库/已在下载列表/补缺失页」状态也是库数据, 顺带重算
        return ("queue", "artworks", "stats", "search", "ops"), ids
    if p.startswith("/api/reverse"):
        return ("reverse", "artworks", "facets", "stats"), ids
    if p.startswith("/api/library/check") or p.startswith("/api/library/self-check") \
            or p.startswith("/api/tasks/") or p.startswith("/api/library/complement") \
            or p.startswith("/api/library/metadata-fill"):
        return ("check", "artworks", "authors", "facets", "stats", "search", "ops"), ids
    if p.startswith("/api/library/backup") or p.startswith("/api/library/db-backup"):
        # 备份/恢复/删除只影响备份清单; 恢复是"用旧文件覆盖当前图" → 图库那几张也要重画
        if "/restore" in p:
            return ("backups", "artworks", "pages", "stats", "ops"), ids
        return ("backups", "ops"), ids
    if p.startswith("/api/logs"):
        return (), ids
    if p.startswith("/api/library"):
        # 图库写操作: 作品/页/筛选项/统计一律可能变; 来源类再带上 sources
        scopes = ["artworks", "pages", "facets", "stats", "search"]
        if "/source" in p or "/claim" in p or "/import" in p:
            scopes.append("sources")
        if "/author" in p or "/set-author" in p:
            scopes.append("authors")
        # 改动都会落一条操作记录 (设置页那张表), 打开着就顺手刷新
        scopes.append("ops")
        return tuple(scopes), ids
    return (), ids

# 下载任务单飞: 同一时刻只允许一个 run_cart 后台线程 (2026-09-15 并发下载事故)
_cart_busy = threading.Event()
# 下载进行中又有新任务加入 → 跑完当前这轮后自动补跑一轮
_cart_rerun = threading.Event()
# 设置页「退出程序」防重入: 已在退出流程中则重复请求直接返回
_quitting = False
# 浏览器页面心跳: 页面开着时前端每 10s ping 一次 /api/heartbeat,
# 页面关闭瞬间 sendBeacon POST /api/bye。
# main.py 的守护线程据此退出:
#   - bye 后 12s 内无任何心跳 (无其他标签/无刷新) → 快速退出 (正常关页面路径);
#   - 或收到过心跳后连续 90s 无心跳 → 兜底退出 (浏览器崩溃/休眠, bye 没发出);
#   - ever=False (从未收到心跳, 纯 API/命令行场景) 不自动退出。
_heartbeat = {"last": 0.0, "ever": False, "bye_at": 0.0}
_config = load_config()
_client_lock = threading.Lock()
_search_client: "SearchClient | None" = None
_search_client_headless: bool = True   # 当前共享浏览器是否无头 (False=用户点了「显示浏览器」)
_manual_lock = threading.Lock()
_manual_driver = None  # 登录用的可见浏览器 driver
# 登录流程状态机: idle(无窗口) → waiting(浏览器已开,等用户登录) → success(已自动保存)
_login_state = {"state": "idle", "account": None, "msg": ""}
_login_lock = threading.Lock()
# 一键自检状态 (后台线程跑, 前端轮询) —— 兼容旧入口, 新实现见 _check_state
_self_check_state = {"running": False, "result": None}

# ---------------------------------------------------------------------------
# 🩺 体检 (原「一键自检」) —— 拆成 6 个可独立运行的步骤
#   旧实现把性质完全不同的 6 件事串进同一个后台线程: 深度磁盘对账(最重 I/O) →
#   离线清理 → 作者改名同步(每个作者一次 HTTP) → 联网补元数据(Selenium, 最慢) →
#   文件整理(动磁盘) → 汇总。于是: 耗时长、进度只有"第几步"、某步出错整体白跑、
#   中途关页面被守护线程退出就前功尽弃, 报告里还混着"需人工处理的待办"和
#   "拿非 pixiv 作品去问 pixiv"的必然失败噪声, 用户体感是"修了这么多 bug 清单也不见少"。
#   现在: 每步能单独跑/单独看结果/单独重跑; 报告分「可疑项」与「待办项」; 条目可忽略
#   不再提示; 每次运行的任务记录落盘, 下次启动能发现"上次异常中断"并提示是否继续。
# ---------------------------------------------------------------------------
_check_state = {
    "running": None,     # 正在跑的步骤名 (None = 空闲)
    "steps": {},         # {步骤名: {ok, at, summary, error}} —— 每步最近一次结果
    "progress": {},      # {step, done, total, current, started_at}
    "report": None,      # 最近一次报告 {time, suspicious[], todos[], ignored}
    "last_run": None,    # {steps, at, fixed} 最近一次运行
    "journal_key": None, # 当前任务在任务记录里的键 (check:all / check:<步骤>)
}
_CHECK_STEPS = [
    ("reconcile", "① 磁盘对账",
     "扫描磁盘↔数据库一致性，并校验图片能否正常打开（最重的磁盘 I/O，只读＋路径自愈）"),
    ("offline", "② 离线清理",
     "本地数据整理：补作者名、登记其他来源、清孤儿行、修无意义标题、纠正 merged 身份（毫秒级）"),
    ("authors", "③ 作者改名同步",
     "联网核对作者现用名与存续状态（每位作者一次请求，较慢）"),
    ("metadata", "④ 联网补元数据",
     "补标题/标签/封面/作者（需登录，最慢，被中断后可继续）"),
    ("organize", "⑤ 文件整理",
     "散落图归位、作者文件夹校正、历史文件名规范化（会移动磁盘文件）"),
    ("report", "⑥ 复检报告",
     "把上一版报告逐条重新核对：已解决的清掉、变了的更新、仍存在的保留；"
     "本次没跑到的来源如实标注「未复检」绝不误清（只读，秒级）"),
]
_CHECK_STEP_NAMES = [s[0] for s in _CHECK_STEPS]
_CHECK_TITLE_OF = {s[0]: s[1] for s in _CHECK_STEPS}
_CHECK_DESC_OF = {s[0]: s[2] for s in _CHECK_STEPS}


def _check_title(name: str) -> str:
    return _CHECK_TITLE_OF.get(name, name)


def _check_resumable(name: str) -> bool:
    """被中断后能否"接着跑": 体检的每一步都是幂等的 (重跑只会把已完成的部分跳过),
    所以都算可继续。留着这个判断, 是为了将来出现真正不可续跑的任务时能如实告诉用户
    "任务异常中断", 而不是给一个点了也没用的「继续」按钮。"""
    return name in _CHECK_TITLE_OF


# ---------- 任务运行记录 (落盘): 异常中断检测 + 下次启动提示是否继续 ----------
_TASK_JOURNAL_LOCK = threading.Lock()


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _journal_path() -> Path:
    """长任务的运行记录。进程被守护线程拦腰砍断时, 记录会停在 running ——
    下次启动据此发现"上次异常中断"并提示是否继续。"""
    return Path(paths.data_root()) / "task_journal.json"


def _journal_read() -> dict:
    try:
        p = _journal_path()
        if p.is_file():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _journal_save(data: dict) -> None:
    try:
        p = _journal_path()
        tmp = p.with_name(p.name + ".tmp")   # 先写临时文件再替换, 断电不会留半截 JSON
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _journal_start(key: str, title: str, resumable: bool = True,
                   steps: "list[str] | None" = None) -> None:
    with _TASK_JOURNAL_LOCK:
        d = _journal_read()
        st = list(steps or [])
        d.setdefault("tasks", {})[key] = {
            "key": key, "title": title, "status": "running",
            "resumable": bool(resumable), "steps": st, "done_steps": [],
            "current": (st[0] if st else None),
            "started_at": _now_str(), "updated_at": _now_str(),
            "progress": {}, "note": "",
        }
        _journal_save(d)


def _journal_write(key: str, status: str, **fields) -> None:
    with _TASK_JOURNAL_LOCK:
        d = _journal_read()
        t = (d.get("tasks") or {}).get(key)
        if not isinstance(t, dict):
            return
        t.update(fields)
        t["status"] = status
        t["updated_at"] = _now_str()
        _journal_save(d)


def _journal_step_done(key: str, step: str, next_step: "str | None" = None) -> None:
    with _TASK_JOURNAL_LOCK:
        d = _journal_read()
        t = (d.get("tasks") or {}).get(key)
        if not isinstance(t, dict):
            return
        done = list(t.get("done_steps") or [])
        if step not in done:
            done.append(step)
        t.update(done_steps=done, current=next_step, updated_at=_now_str())
        _journal_save(d)


def _journal_finish(key: str, ok: bool, note: str = "") -> None:
    _journal_write(key, "done" if ok else "failed", note=str(note or ""),
                   finished_at=_now_str(), progress={})


def _journal_mark_interrupted() -> int:
    """服务启动时调用: 把上次残留的 running 记录标成 interrupted (进程被砍断的痕迹)。"""
    n = 0
    with _TASK_JOURNAL_LOCK:
        d = _journal_read()
        for t in (d.get("tasks") or {}).values():
            if isinstance(t, dict) and t.get("status") == "running":
                t.update(status="interrupted", updated_at=_now_str())
                n += 1
        if n:
            _journal_save(d)
    return n


def _journal_pending() -> "list[dict]":
    """需要用户处理的异常中断任务 (还没被"忽略"掉的), 最近中断的排在前面。"""
    d = _journal_read()
    out = []
    for t in (d.get("tasks") or {}).values():
        if not isinstance(t, dict) or t.get("status") != "interrupted":
            continue
        steps = list(t.get("steps") or [])
        done = list(t.get("done_steps") or [])
        remain = [s for s in steps if s not in done]
        if not remain:
            remain = steps[-1:]   # 中断在最后一步中途: 该步没跑完, 重跑它
        out.append({**t, "remain": remain})
    return sorted(out, key=lambda x: str(x.get("updated_at") or ""), reverse=True)


def _journal_ack(key: str) -> None:
    """用户选择"不继续" → 标记已确认, 不再提示。"""
    _journal_write(key, "acknowledged", note="用户已忽略本次中断")
# 反搜任务进度 (后台线程跑, 前端 1s 轮询 /api/reverse/status):
# phase ∈ idle/running/break/done/error; batch=当前批次号; rest_sec=批次休息剩余秒
_REVERSE_PROGRESS = {
    "running": False, "total": 0, "done": 0, "current": "",
    "phase": "idle", "rest_sec": 0, "batch": 0,
    "batch_size": 5, "batch_break_sec": 30,
    "started_at": None, "finished_at": None, "error": "",
}
# 图库内「反搜来源」(单作品逐页 SauceNAO) 的进度, 前端弹窗里显示进度条
_SRC_SEARCH = {
    "running": False, "artwork_id": None, "total": 0, "done": 0, "current": "",
    "phase": "idle", "error": "", "result": None,
    "started_at": None, "finished_at": None,
    # 批量模式 (「🔎 批量搜索来源」): None=单作品; 否则 {total, done, auto_classified,
    # items:[{artwork_id,status,site,label,url,error,...}]} 供前端出一张汇总表
    "batch": None,
}
# 老 cookies.bin 升级后无 account.json 时, 后台惰性探测账号信息 (只跑一次)
_account_probing = False
_account_probe_lock = threading.Lock()


def _maybe_probe_account_background() -> None:
    """有 cookies 但缺 account.json (老版本升级/手动拷入 cookies):
    后台用无头浏览器探测一次登录账号信息并落盘, 让头像/昵称正常显示。"""
    global _account_probing
    if not _cookies_path().exists() or _account_path().exists():
        return
    with _account_probe_lock:
        if _account_probing:
            return
        _account_probing = True

    def _probe():
        global _account_probing
        try:
            sc = _get_search_client(headless=True)
            if sc and sc.ensure_login():
                info = sc.fetch_logged_in_user()
                if info and info.get("user_id"):
                    _save_account(info)
        except Exception:
            pass
        finally:
            with _account_probe_lock:
                _account_probing = False

    threading.Thread(target=_probe, daemon=True).start()


def _account_path() -> Path:
    """登录账号信息 (昵称/头像/uid) 持久化文件, 与 cookies.bin 同目录。"""
    return Path(paths.data_root()) / "account.json"


def _load_account() -> "dict | None":
    try:
        d = json.loads(_account_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("user_id") else None
    except Exception:
        return None


def _save_account(info: dict) -> None:
    try:
        _account_path().write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    except Exception:
        pass


def _clear_account() -> None:
    try:
        p = _account_path()
        if p.is_file():
            p.unlink()
    except Exception:
        pass


def _reporter() -> WebReporter:
    """组合: 落 data/logs/ 日志文件 + WebReporter 广播到 SSE。"""
    return build_reporter(inner=WebReporter(_hub, _cancel_event), config=_config)


def _cookies_path() -> Path:
    return paths.user_data(_config["paths"]["cookies_filename"])


def _db() -> PixivDatabase:
    return PixivDatabase(reporter=_reporter())


def _pixiv_http_session():
    """用本机 cookies 建一个 **HTTP 直连** pixiv 会话 (不起浏览器, 毫秒级)。
    没有 cookies / 建不起来就返回 None —— 调用方据此给出"先去登录"的明确提示。
    (与自检「作者改名同步」用的是同一套 HTTP 直连, 见 core.search_client)"""
    try:
        from utils.sec import load_cookies_smart
        from core.search_client import build_pixiv_http_session
        ck = load_cookies_smart(str(_cookies_path()), upgrade=False)
        if not ck:
            return None
        acc = _load_account()
        return build_pixiv_http_session(ck, str((acc or {}).get("user_id") or ""))
    except Exception:
        return None


# create_app 内部还定义了一个**同名局部函数** `_pixiv_http_session` (读同一套 cookies),
# 而 Python 编译期就把该名字在 create_app 里定成了闭包变量 —— 于是 create_app 内所有同名
# 引用都被它劫持, 改不了、也打不了桩。这个模块级别名供「需要在测试里替换」的接口显式引用。
_pixiv_http_session_module = _pixiv_http_session


def _fill_author_name_by_id(db, author_id: str) -> str:
    """按 pixiv 作者 ID 联网取现用名并入库 —— 用户只填了纯数字作者 ID 时用。

    以前填了 ID 保存后库里作者名是空的, 图库显示裸 ID, 用户会以为"没填上/变成自定义作者"。
    已有可读名 / 没登录 / 网络失败 → 原样返回, 绝不覆盖已有权威名 (体检「作者改名同步」也能补)。"""
    cur = ""
    try:
        row = db.conn.execute("SELECT name FROM authors WHERE author_id=?",
                              (author_id,)).fetchone()
        cur = str(row[0]).strip() if row and row[0] else ""
        if cur and not _is_unreadable_name(cur):
            return cur
        sess = _pixiv_http_session()
        if sess is None:
            return cur
        from core.search_client import http_fetch_user_name
        nm = str(http_fetch_user_name(sess, author_id) or "").strip()
        if nm:
            db.upsert_author(author_id, _readable_author_name(db, author_id, nm))
            return nm
    except Exception:
        pass
    return cur


# ---------- 人读标签: 面向用户的清单别只印 ext_xxx / merged_xxx 这种内部主键 ----------
def _artwork_label(brief: "dict | None", aid: str) -> str:
    """《标题》· 作者 X · 来源 Y —— 缺项自动省略。
    标题无意义时(空值 / 等于作品ID)显示「（无标题）」; 已确认失效的作品(deleted_at 非空)
    显示「（原作品已删除）」—— 这个事实来自数据库字段, 与标题怎么改无关。"""
    t = brief or {}
    title = str(t.get("title") or "").strip()
    if t.get("deleted_at"):
        label = "（原作品已删除）"
    elif title and title != str(aid):
        label = f"《{title}》"
    else:
        label = "（无标题）"
    who = str(t.get("author_name") or "").strip() or str(t.get("source_author") or "").strip()
    src = str(t.get("source") or "").strip() or "pixiv"   # source 空值 = pixiv (与图库来源筛选一致)
    parts = [label]
    if who:
        parts.append(f"作者 {who}")
    parts.append(f"来源 {src}")
    return " · ".join(parts)


def _labels(ids) -> "dict[str, str]":
    """批量 {作品ID: 人读标签}; 查库失败/无记录时退回「作品 <ID>」, 绝不因标签查不到而报错。"""
    want = [str(x) for x in (ids or []) if str(x)]
    if not want:
        return {}
    try:
        with _db() as db:
            briefs = db.artwork_briefs(want)
    except Exception:
        briefs = {}
    return {i: _artwork_label(briefs.get(i) or {}, i) for i in want}


def _labels_inline(ids, maxn: int = 3) -> str:
    """审计文案用简写: 「《A》· 来源 X；《B》· 来源 Y；…等 N 个」"""
    want = [str(x) for x in (ids or []) if str(x)]
    if not want:
        return ""
    m = _labels(want)
    head = "；".join(m[i] for i in want[:maxn])
    if len(want) > maxn:
        head += f"；…等 {len(want)} 个"
    return head


def _auto_bookmark_after_download(queued_ids: "set[int]") -> None:
    """下载成功后自动收藏到 pixiv (原网页点 ♡ 红心)。

    四条边界（都是有意的，别放宽）：
      · 只处理本批**跑成功**的「单个作品」任务 —— 画师批量下载不碰（一次收藏上千张不是想要的效果）；
      · 已收藏的作品**一律跳过**（只读状态判断，绝不覆盖/改动原本的公开-非公开设置）；
      · 没登录/没 cookies → 如实打一行日志后跳过，不报错、不影响下载结果。
      · **R18 作品强制非公开收藏**：公开收藏里挂着成人向作品
        是能被别人看到的，这类一律 private，与设置里的公开/非公开无关。
    """
    try:
        cfg = (load_config().get("pixiv_actions") or {})
    except Exception:
        return
    if not cfg.get("auto_bookmark"):
        return
    targets: "list[tuple[str, bool]]" = []      # (作品ID, 是否 R18)
    try:
        seen: "set[str]" = set()
        with _db() as db:
            for it in db.queue_list():
                if (int(it.get("id") or 0) not in queued_ids
                        or str(it.get("status") or "") != "done"
                        or str(it.get("item_type") or "") != "artwork"):
                    continue
                aid = str(it.get("artwork_id") or "")
                if not aid.isdigit() or aid in seen:
                    continue
                seen.add(aid)
                meta = db.get_artwork_metadata(aid) or {}
                targets.append((aid, bool(meta.get("is_r18"))))
    except Exception as e:
        _reporter().warn(f"自动收藏: 读下载队列失败 {str(e).splitlines()[0]}")
        return
    if not targets:
        return
    sess = _pixiv_http_session()
    if sess is None:
        _reporter().warn(f"自动收藏: 未登录 pixiv, 已跳过 {len(targets)} 个作品"
                         f"（到设置页登录后即可自动收藏）")
        return
    dflt_private = bool(cfg.get("bookmark_private"))
    ok, skipped, failed, r18_private = 0, 0, [], 0
    for aid, is_r18 in targets:
        private = dflt_private or is_r18
        try:
            if web_bookmark_state(sess, aid).get("bookmarked"):
                skipped += 1
                continue
            web_bookmark_add(sess, aid, private=private)
            ok += 1
            if private and is_r18:
                r18_private += 1
        except PixivWebActionError as e:
            failed.append(f"{aid}: {e}")
        except Exception as e:
            failed.append(f"{aid}: {str(e).splitlines()[0]}")
        time.sleep(0.4)      # 连着打写接口容易被 pixiv 限流, 缓一下
    if ok or skipped:
        tail = f"，{skipped} 个早已收藏跳过" if skipped else ""
        r18_tail = f"，其中 {r18_private} 个 R18 作品按非公开收藏" if r18_private else ""
        _reporter().log(f"♡ 已自动收藏 {ok} 个作品到 pixiv"
                        f"（默认{'非公开' if dflt_private else '公开'}）{r18_tail}{tail}")
        if ok:
            audit_log.record("update", "自动收藏到 pixiv",
                             f"{ok} 个作品（默认{'非公开' if dflt_private else '公开'}）"
                             f"{r18_tail}{tail}", ok)
    if failed:
        _reporter().warn(f"自动收藏失败 {len(failed)} 个: " + "；".join(failed[:3]))


def _launch_run_cart(only_ids: "list[int] | None" = None,
                     strategy: "DownloadStrategy | None" = None) -> None:
    """后台线程跑下载任务。only_ids 给定则只跑这些 id(单条重试/重试失败)。

    单飞 (2026-09-15 事故修复): 「加入清单即自动下载」会让用户连续加入多个作品,
    旧代码每次都 new 一个线程 → 实测 3 个下载任务并发, 各自又对整个图库跑 Output 同步,
    互相踩出 29 个假作品。现规则: 已有任务在跑 → 不再开线程, 置「跑完再来一轮」标记,
    由正在跑的线程收尾后自动补跑 (那时队列里新增的 pending 项会被一并捞走)。"""
    if _cancel_event.is_set():
        _cancel_event.clear()
    if _cart_busy.is_set():
        _cart_rerun.set()
        try:
            _reporter().warn("已有下载任务正在执行，已排队，完成后自动继续…")
        except Exception:
            pass
        return

    def _task():
        _cart_busy.set()
        mark_dirty("queue")          # 队列状态立刻变"进行中", 各页面按域刷新
        try:
            while True:
                _cart_rerun.clear()
                try:
                    try:
                        with _db() as db:
                            _before = db.count_artworks_filtered()
                            # 本批「待下载」的队列项 id: 跑完据此判断谁真的下载成功了
                            # (自动收藏只对这批生效, 不会去翻历史的已完成任务)
                            _pending_ids = {int(it["id"])
                                            for it in db.queue_list(status="pending")}
                    except Exception:
                        _before = None
                        _pending_ids = set()
                    if strategy is None:
                        run_cart(_reporter(), config=_config, only_ids=only_ids)
                    else:
                        run_cart(_reporter(), config=_config, only_ids=only_ids,
                                 strategy=strategy)
                    # 下载完成的「新增」审计: 图库作品数净增即记一条 (增删同时发生时不虚报)
                    try:
                        with _db() as db:
                            _after = db.count_artworks_filtered()
                        if _before is not None and _after > _before:
                            audit_log.record("add", "下载入库",
                                             f"下载完成, 图库新增 {_after - _before} 个作品",
                                             _after - _before)
                    except Exception:
                        pass
                except Exception as e:
                    _reporter().error(f"下载任务异常: {str(e).splitlines()[0]}")
                # 本批下载成功的作品 → 按设置自动收藏到 pixiv 
                try:
                    _auto_bookmark_after_download(_pending_ids)
                except Exception as e:
                    _reporter().warn(f"自动收藏异常: {str(e).splitlines()[0]}")
                # 下载线程不是 HTTP 触发的改动 → 显式登记, 让所有页面按数据域自行刷新
                mark_dirty("queue", "artworks", "pages", "stats", "facets")
                if not _cart_rerun.is_set():
                    break
                _reporter().log("排队任务开始补跑…")
        finally:
            _cart_busy.clear()

    threading.Thread(target=_task, daemon=True).start()


# ---------- 共享 SearchClient (连查不重开浏览器; 默认无头, 可切有头供用户围观) ----------
def _get_search_client(headless: "bool | None" = None) -> SearchClient:
    """获取共享查询浏览器。
    headless=None → 沿用当前模式 (默认无头);
    headless=False → 重建为可见窗口 (用户想围观浏览器在干嘛);
    headless=True  → 重建为无头。"""
    global _search_client, _search_client_headless
    with _client_lock:
        want_headless = _search_client_headless if headless is None else headless
        if _search_client is None or want_headless != _search_client_headless:
            if _search_client is not None:
                try:
                    _search_client.close()
                except Exception:
                    pass
            _search_client = SearchClient(reporter=_reporter(), config=_config,
                                          headless=want_headless)
            _search_client_headless = want_headless
        else:
            _search_client.reporter = _reporter()
        return _search_client


def _reset_search_client() -> None:
    global _search_client, _search_client_headless
    with _client_lock:
        if _search_client is not None:
            try:
                _search_client.close()
            except Exception:
                pass
            _search_client = None
        _search_client_headless = True   # 重置后回默认无头


# ---------- 图库 ↔ 磁盘对账 (带 TTL 缓存, 避免每次筛选都全扫磁盘) ----------
# 全盘 reconcile 一次约 300ms (扫描 1700+ 文件), 每次查询热路径都会触发;
# TTL 从 60s 提到 300s, 普通浏览/搜索/翻页不再频繁全扫, 只有「缺失文件」筛选
# (api_library_items force) 和手动同步/导入才强制重扫, 保证那些入口结果即时准确。
_RECONCILE_TTL = 300.0
_reconcile_state = {"ts": 0.0, "rep": None}
# 对账锁: 全盘扫描期间并发调用 (15s 轮询/同步/导入/自检) 可能同时看到 rep=None 各自开扫,
# 造成重复扫描 + 日志重复。加锁后同一时刻只有一个扫描, 其余等待复用结果。
_reconcile_lock = threading.Lock()


def _cached_reconcile(force: bool = False):
    """返回最近一次(≤ TTL)的磁盘对账结果; force=True 强制重扫(前端刷新/同步)。
    结果用于图库的 file_missing 标记与清理缺失。"""
    now = time.time()
    state = _reconcile_state
    if force or state["rep"] is None or (now - state["ts"]) > _RECONCILE_TTL:
        with _reconcile_lock:
            # 锁内二次检查: 等待期间可能已被别的调用方扫好
            now = time.time()
            if not (force or state["rep"] is None
                    or (now - state["ts"]) > _RECONCILE_TTL):
                return state["rep"]
            img_root = paths.user_data(_config["paths"]["img_subdir"])
            with _db() as db:
                state["rep"] = db.reconcile_output(img_root, verify_corrupt=False)
                # 外来作品标题随文件移动刷新 (懒收敛: 整理/同步/归档搬走的 ext_ 标题不再过期)
                try:
                    db.refresh_ext_titles()
                except Exception:
                    pass
            state["ts"] = time.time()
    return state["rep"]


def _reconcile_to_json(rep) -> dict:
    return {
        "missing_ids": rep.missing_ids or [],
        "empty_dirs": rep.empty_dirs or [],
        "corrupt_files": rep.corrupt_files or [],
        "scanned_images": rep.scanned_images,
        "new_pages_linked": rep.new_pages_linked,
        "pages_relinked": int(getattr(rep, "pages_relinked", 0) or 0),
        "db_artwork_count": rep.db_artwork_count,
        "total_issues": rep.total_issues(),
        "duplicates": getattr(rep, "duplicates", []) or [],
        "conflicts": getattr(rep, "conflicts", []) or [],
    }


# 「同步/对账」接口的响应里, 这几个计数全为 0 = 这次一张图都没动 (幂等空转)
_SYNC_CHANGE_KEYS = ("new_pages_linked", "pages_relinked", "authors_adopted",
                     "orphans_removed", "foreign_registered", "sources_refined")


def _sync_changed(resp) -> bool:
    """写后校验: 图库同步到底改没改东西 (True=有改动, 需要通知前端刷新)。

    解析失败一律当作"有改动"——宁可多刷一次, 也不能因为读不懂响应而漏刷新。"""
    try:
        j = resp.get_json(silent=True)
        if not isinstance(j, dict):
            return True
        return any(int(j.get(k) or 0) for k in _SYNC_CHANGE_KEYS)
    except Exception:
        return True


def _adopt_authors_from_folders(db, img_root: Path) -> int:
    """离线补作者名: 图库文件夹规范为「作者名_作者ID」, 扫描后 upsert 到 authors 表。
    反搜归档/手动放图的作品 authors 表常缺行, 导致图库作者列显示裸 ID;
    文件夹名是本地已有信息, 不联网即可修复。返回新补/更新的作者数。
    顺带: 文件夹内的作品若 artworks.author_id 为空 (外置图片导入), 回填之。
    递归所有层级 —— 用户可能用「X图/作者名_ID/」这类分类子目录组织图库,
    只扫第一层会漏掉绝大多数作者文件夹。
    原则 (与 store._adopt_author_from_rel 一致): 文件夹名是下载/拖入时的旧昵称,
    可能已含「(twi@账号)」污染且已过时——只在作者【不存在或名字为空】时用它建档,
    绝不覆盖已有作者的权威名 (权威名以 pixiv 自检/下载元数据为准), 避免同步轮询
    反复把旧文件夹名写回数据库。"""
    import os as _os
    import re as _re
    pat = _re.compile(r'[（(]([^（）()]*@[^（）()]*)[）)]')
    n = 0
    try:
        for dirpath, dirnames, _files in _os.walk(img_root):
            dname = _os.path.basename(dirpath)
            name, _, aid = dname.rpartition("_")
            name = (name or "").strip()
            if not (aid.isdigit() and name):
                continue
            row = db.conn.execute(
                "SELECT name FROM authors WHERE author_id=?", (aid,)).fetchone()
            if row and (row[0] or "").strip():
                continue  # 已有权威名 → 文件夹旧昵称绝不覆盖
            # 新建时剥离「(来源@账号)」污染 (只在新建档时清理, 旧库名不动)
            if "@" in name:
                ms = list(pat.finditer(name))
                if ms:
                    clean = pat.sub('', name).replace('（', '').replace('）', '')\
                        .replace('(', '').replace(')', '').strip()
                    if clean:
                        name = clean
            # 单向闭环护栏 (2026-09-14 审计): 夹名那截不是可辨认的名字 (unknown/未署名/纯符号)
            # 就不建档、也不回填 —— 宁可留空由用户在「指定作者」里定, 别造垃圾作者行
            if not folder_author_name_usable(name):
                continue
            db.upsert_author(aid, name)
            n += 1
            # 该文件夹下 pages 对应的作品, author_id 缺失则回填 (不覆盖已知)
            rel_prefix = Path(dirpath).relative_to(img_root).as_posix()
            db.conn.execute(
                """UPDATE artworks SET author_id=?
                    WHERE (author_id IS NULL OR author_id='')
                      AND artwork_id IN (
                        SELECT DISTINCT artwork_id FROM pages
                         WHERE local_path LIKE ?)""",
                (aid, f"{rel_prefix}/%"))
        db.conn.commit()
    except Exception:
        pass
    return n


# 后台联网补元数据状态 (sync/导入发现新图时触发; 自检是全量保险, 这是增量即时补)
_meta_fill_state = {"running": False}

# 图库图片扩展名 (模块级, 供 _organize_loose_images 等模块级函数使用)
_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# =============================================================
# 缩略图磁盘缓存: 图库卡片绝不直出原图 (单张数 MB ~ 上百 MB, 上千张时
# 浏览器解码和磁盘 IO 都会被拖死)。首次请求用 PIL 等比缩到 ~480px 长边
# 转 JPEG, 缓存到 data/thumbs/, 之后零成本直读。缓存键含 根|相对路径|
# mtime_ns|大小 → 原图替换/移动后自动失效。thumbs/ 在 img/ 之外, 不会被
# 对账扫描当成图库图片。
# =============================================================
_THUMB_MAX_PX = 480
_thumb_dir_cache: "Path | None" = None
_thumb_lock = threading.Lock()


def _thumb_dir() -> Path:
    global _thumb_dir_cache
    if _thumb_dir_cache is None:
        d = Path(paths.data_root()) / "thumbs"
        d.mkdir(parents=True, exist_ok=True)
        _thumb_dir_cache = d
    return _thumb_dir_cache


def _stable_thumb_path(root_key: str, rel: str) -> Path:
    """「稳定」缩略图路径: 键只含 根|相对路径 (不含 mtime/size), 源图删除后仍可显示。
    用于文件缺失时回溯"删掉的图长什么样"。"""
    key = f"stable|{root_key}|{rel.replace(chr(92), '/')}"
    return _thumb_dir() / f"stable_{hashlib.md5(key.encode('utf-8')).hexdigest()}.jpg"


def _ensure_stable(cache: Path, root_key: str, rel: str) -> None:
    """生成一个与路径绑定的稳定缩略图副本 (内容=当前缩略图, 源文件删了也能看)。"""
    try:
        st = _stable_thumb_path(root_key, rel)
        if not st.is_file() and cache.is_file():
            import shutil as _sh
            _sh.copy2(cache, st)
    except Exception:
        pass


def _thumb_cache_path(src: Path, root_key: str, rel: str) -> "Path | None":
    """该源图对应的缩略图缓存路径 (只算路径, 不生成); 源文件读不到属性时返回 None。
    缓存键含 mtime+size → 源图被替换后自动失效重算, 未变则长期命中。"""
    try:
        st = src.stat()
    except OSError:
        return None
    key_src = f"{root_key}|{rel.replace(chr(92), '/')}|{st.st_mtime_ns}|{st.st_size}"
    return _thumb_dir() / f"{hashlib.md5(key_src.encode('utf-8')).hexdigest()}.jpg"


def _thumb_cached(src: Path, root_key: str, rel: str) -> bool:
    """缩略图缓存是否已就绪 (轻量: 只 stat + 查文件, 不做任何解码/生成)。
    启动预热用它快速跳过已缓存的图 —— 否则每次开服务都要全库空转一遍。"""
    p = _thumb_cache_path(src, root_key, rel)
    return bool(p and p.is_file())


def _get_thumb(src: Path, root_key: str, rel: str) -> "Path | None":
    """返回(必要时生成)缩略图 JPEG 路径; PIL 缺失/解码失败返回 None (调用方回退原图)。"""
    try:
        from PIL import Image
    except Exception:
        return None
    cache = _thumb_cache_path(src, root_key, rel)
    if cache is None:
        return None
    if cache.is_file():
        return cache
    with _thumb_lock:
        if cache.is_file():
            return cache
        old_bomb = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None  # 允许 1 亿+ 像素超大 PNG (DecompressionBomb)
        try:
            with Image.open(src) as im:
                try:
                    # JPEG: draft 让解码器直接按目标尺寸采样, 不解全图 (省内存)
                    im.draft("RGB", (_THUMB_MAX_PX, _THUMB_MAX_PX))
                except Exception:
                    pass
                im.thumbnail((_THUMB_MAX_PX, _THUMB_MAX_PX), Image.LANCZOS)
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])  # 透明底铺白
                    out = bg
                else:
                    out = im.convert("RGB")
                out.save(cache, "JPEG", quality=82)
        except Exception:
            try:
                if cache.exists():
                    cache.unlink()
            except Exception:
                pass
            return None
        finally:
            Image.MAX_IMAGE_PIXELS = old_bomb
    _ensure_stable(cache, root_key, rel)  # 同步一份稳定副本 (删源文件后仍能找回缩略图)
    return cache


def _organize_loose_images(img_root: Path) -> int:
    """根目录散落图按 DB 已知作者归进「作者名_作者ID/」文件夹。
    同名同大小判重复删源, 异名保留不覆盖; 作者未知的不动。返回整理张数。

    夹名与落点 (2026-09-21 治本): 一律复用磁盘上**已有的**该作者夹 —— 作者夹可能被
    用户分类到 涩图/X图 等子目录, 也可能名字还是旧名/旧规则生成的; 复用它而不是按现用名
    另起一个, 否则同一位作者会被摆成两个夹。一个都没有时才按规范名建 (与下载器
    `_pick_author_dir` 同一策略)。此前这里用「删掉非法字符」拼夹名, 与一键整理的
    「非法字符→下划线」对不上, 是双夹的成因之一。"""
    import shutil as _shutil
    loose = [p for p in sorted(img_root.iterdir())
             if p.is_file() and p.suffix.lower() in _IMG_EXT]
    if not loose:
        return 0
    # 作者夹索引 {作者ID: [夹路径]} —— 一次扫描供本批散件复用 (没散件时不扫, 零开销)
    existing: "dict[str, list[Path]]" = {}
    try:
        for d in img_root.rglob("*"):
            if d.is_dir():
                m = re.search(r'_(\d{6,9})$', d.name)
                if m:
                    existing.setdefault(m.group(1), []).append(d)
    except OSError:
        pass
    organized = 0
    for p in loose:
        # 统一身份识别  旧宽松正则会把外来源顺序号当 pid 去查作者
        nkey = pixiv_natural_key(p.name)
        if not nkey:
            continue  # 连作品 ID 都没有, 留给反搜
        try:
            with _db() as _db4:
                row = _db4.conn.execute(
                    """SELECT a.author_id AS aid, au.name AS name
                         FROM artworks a
                    LEFT JOIN authors au ON a.author_id=au.author_id
                        WHERE a.artwork_id=? LIMIT 1""",
                    (nkey[0],)).fetchone()
                if not row or not row["aid"] or not row["name"]:
                    continue  # 作者未知, 不动
                aid = str(row["aid"])
                # 规范夹名要查 DB 别名, 必须在连接有效期内算出来 (with 外连接已关)
                fallback = img_root / _author_folder_name(_db4, aid, str(row["name"]))
            cands = existing.get(aid) or []
            # 优先挑已有同名文件的夹 (补缺页场景), 否则第一个; 一个都没有才用规范名新建
            target_dir = next((c for c in cands if (c / p.name).exists()),
                              cands[0] if cands else fallback)
            existing.setdefault(aid, [target_dir])
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / p.name
            if target.exists():
                try:
                    if target.stat().st_size == p.stat().st_size:
                        p.unlink()
                        organized += 1
                except OSError:
                    pass
                continue
            _shutil.move(str(p), str(target))
            organized += 1
        except Exception:
            pass
    return organized


def _ps_dialog_preamble() -> str:
    """弹系统对话框的 PowerShell 公共前缀:
    ① 输出统一 UTF-8 —— 中文路径经 GBK 控制台回传会乱码;
    ② 造一个「父窗口 = 当前前台窗口(也就是浏览器)」的 IWin32Window。系统对话框若没有
       父窗口, Windows 的前台锁定会让它出现在当前前台窗口**后面** —— 用户看到的就是
       「点了导入, 弹窗跑到浏览器后面去了」。给了 owner, 对话框
       必定画在 owner 之上; Add-Type 失败(极端环境)时 $owner 为 $null, 退回无主对话框。"""
    return (
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$owner = $null;"
        "try {"
        " Add-Type -TypeDefinition 'using System;"
        "using System.Runtime.InteropServices;"
        "using System.Windows.Forms;"
        "public class FgOwner : IWin32Window {"
        "  [DllImport(\"user32.dll\")] static extern IntPtr GetForegroundWindow();"
        "  public IntPtr Handle { get { return GetForegroundWindow(); } }"
        "}' -ReferencedAssemblies 'System.Windows.Forms','System.Drawing','System'"
        " -ErrorAction Stop;"
        " $owner = New-Object FgOwner"
        "} catch { $owner = $null }"
    )


def _run_ps_dialog(ps: str) -> "list[str]":
    """跑一段弹对话框的 PowerShell, 返回它打印的行 (每行一项, 已去空白)。"""
    if os.name != "nt":
        return []
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    return [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]


def _pick_folder_dialog() -> "str | None":
    """弹出系统原生文件夹选择框, 返回所选路径 (取消/失败返回 None)。

    用 PowerShell + .NET 的 FolderBrowserDialog, **不用 Python tkinter** ——
    Tk 在非主线程创建/销毁会触发 tcl86t.dll abort 让进程无声消失
    (build.spec 注释与 T52 用例已明确禁用 tkinter)。
    该调用发生在 Flask 请求线程, 会阻塞到用户选完或超时(300s)。"""
    lines = _run_ps_dialog(
        _ps_dialog_preamble()
        + "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = '\u9009\u62e9\u8981\u5bfc\u5165\u56fe\u5e93\u7684\u6587\u4ef6\u5939"
        "\uff08\u5176\u4e2d\u7684\u56fe\u7247\u4f1a\u8fdb\u56fe\u5e93\uff0c\u539f\u6587\u4ef6"
        "\u9001\u56de\u6536\u7ad9\uff09';"
        "$d.ShowNewFolderButton = $false;"
        "$r = if ($owner) { $d.ShowDialog($owner) } else { $d.ShowDialog() };"
        "if ($r -eq [System.Windows.Forms.DialogResult]::OK)"
        " { [Console]::Out.Write($d.SelectedPath) }"
    )
    return lines[0] if lines else None


def _pick_files_dialog() -> "list[str]":
    """弹系统原生「打开文件」框 (可多选图片), 返回所选文件的绝对路径列表。

    为什么要两个入口: Windows 的通用对话框一次只能「选多张文件」或「选一个文件夹」,
    没有二者合一的形态 —— 所以前端给两个按钮, 各自走各自的对话框。"""
    return _run_ps_dialog(
        _ps_dialog_preamble()
        + "$d = New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Title = '\u9009\u62e9\u8981\u5bfc\u5165\u56fe\u5e93\u7684\u56fe\u7247"
        "\uff08\u53ef\u591a\u9009\uff09';"
        "$d.Multiselect = $true;"
        "$d.Filter = '\u56fe\u7247\u6587\u4ef6|*.jpg;*.jpeg;*.png;*.webp;*.gif|"
        "\u6240\u6709\u6587\u4ef6|*.*';"
        "$r = if ($owner) { $d.ShowDialog($owner) } else { $d.ShowDialog() };"
        "if ($r -eq [System.Windows.Forms.DialogResult]::OK)"
        " { foreach ($f in $d.FileNames) { [Console]::Out.Write($f + \"`n\") } }"
    )


def _recycle_files(paths: "list[str]") -> "tuple[int, list[str]]":
    """把文件送进 Windows **回收站** (可恢复), 返回 (成功数, 失败的路径列表)。

    为什么不用 shutil.move / os.remove :
    导入是「把图片搬进图库」, 但硬移动/删除是不可逆的 —— 用户误选一个文件夹就再也找不回。
    改成「先复制入库 → 成功后才把源文件送回收站」, 效果仍是搬动, 但随时能从回收站还原。
    整批一次调用(用临时清单文件传路径), 几百张也只有一个 PowerShell 进程的启动开销。
    非 Windows 或调用整体失败 → 返回全部失败, 调用方只报告, 绝不因此回滚已入库的文件。"""
    paths = [str(p) for p in (paths or []) if str(p or "").strip()]
    if os.name != "nt" or not paths:
        return 0, paths
    import tempfile as _tf
    lst = None
    try:
        fd, lst = _tf.mkstemp(prefix="pixiv_recycle_", suffix=".txt")
        os.close(fd)
        with open(lst, "w", encoding="utf-8") as f:
            f.write("\n".join(paths))
        ps = (
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false;"
            "Add-Type -AssemblyName Microsoft.VisualBasic;"
            "$ok = 0; $bad = @();"
            "foreach ($p in [System.IO.File]::ReadAllLines($env:PIXIV_RECYCLE_LIST,"
            " [System.Text.Encoding]::UTF8)) {"
            " if (-not $p) { continue }"
            " try {"
            "  if (Test-Path -LiteralPath $p) {"
            "   [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p,"
            " 'OnlyErrorDialogs', 'SendToRecycleBin')"
            "  }"
            "  $ok++"
            " } catch { $bad += $p }"
            "};"
            "[Console]::Out.Write(\"$ok`n\");"
            "foreach ($b in $bad) { [Console]::Out.Write($b + \"`n\") }"
        )
        env = dict(os.environ, PIXIV_RECYCLE_LIST=str(lst))
        p = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=env)
        out = [ln.strip() for ln in (p.stdout or "").splitlines()]
        n = int(out[0]) if out and out[0].isdigit() else 0
        bad = [x for x in out[1:] if x]
        return n, bad
    except Exception:
        return 0, paths
    finally:
        if lst:
            try:
                os.unlink(lst)
            except OSError:
                pass


# =====================================================================
# 应用内回收站 (软删除的物理层) —— 
# =====================================================================
def _trash_root() -> Path:
    """应用内回收站的物理根: data_root/trash。

    **必须在 img/ 之外** (见 db/migrations/016 的说明): 对账/自检/散落图整理都用
    `img_root.rglob` 全盘扫, 回收站放进去就会被当成散落图搬走或被当孤儿清掉。
    放外面则所有这些扫描天生看不见它, 一条过滤都不用加。
    """
    return Path(paths.user_data("trash"))


def _apply_rel_moves(moves, src_root: Path, dst_root: Path) -> "tuple[int, list[str]]":
    """按 [(src_rel, dst_rel)] 成批搬文件 (自动建父目录), 返回 (成功数, 失败描述)。

    默认**不覆盖**已存在的目标 —— 回收站与恢复都不该悄悄顶掉别人的文件
    (用户"女仆"比喻: 原位置住了新住户, 就不能硬把旧的塞回去)。
    单个失败不影响其他项: 报告出来, 由上层决定提示方式。
    """
    ok, bad = 0, []
    for src_rel, dst_rel in (moves or []):
        try:
            s = Path(src_root) / str(src_rel).replace("\\", "/")
            d = Path(dst_root) / str(dst_rel).replace("\\", "/")
            if not s.is_file():
                bad.append(f"源文件不存在: {src_rel}")
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            if d.exists():
                bad.append(f"目标已存在, 未覆盖: {dst_rel}")
                continue
            os.replace(str(s), str(d))
            ok += 1
        except Exception as e:
            bad.append(f"{src_rel}: {e}")
    return ok, bad


def _trash_abs(rel: str) -> Path:
    """回收站相对路径 → 绝对路径 (供 _recycle_files 送 Windows 电脑回收站)。"""
    return _trash_root() / str(rel or "").replace("\\", "/")


# ── 🖼 桌面壁纸 (Windows 幻灯片) 溯源 —— 2026-09-24 ────────────────────────────
# 溯源规则已搬到 core/wallpaper_trace.py: 网页面板与系统托盘要用**同一份**(不搬的话
# 第二份迟早漂)。这里只留别名, 让 /api/local-img 的 root=wall 分支与旧调用点一行都不用改。
# _wall_orb_candidates 留在本文件 —— 它要复用 _ls_searcher 单例, 搬进 core 会加载两份特征库。
_wallpaper_file = wallpaper_file
_wall_relation = wall_relation
_img_v = img_v
_local_img_url = local_img_url
_WALL_DEEP_CACHE = WALL_DEEP_CACHE      # 同一个 dict 对象: 测试清缓存即清 core 的缓存


def _shuffle_arg() -> bool:
    """``?shuffle=1`` → 「顺序 / 随机」走随机 (小控件那个切换键传过来的)。"""
    return str(request.args.get("shuffle") or "").strip().lower() in ("1", "true", "on", "yes")


# ── 🧹 壁纸副本清理 (「省空间」) —— 2026-09-24 只做省空间, 且必须逐条人工确认 ──
# 壁纸夹 7.4 GB / 1181 张, 全量算 MD5 要几十秒, 请求里等不起 → 后台线程扫 + 前端轮询进度。
# 结果**只报告, 绝不自动删**: 删哪几条一律由界面上的勾选决定, 且只进 Windows 回收站 (可还原)。
_WALL_DUPES = {"status": "idle", "done": 0, "total": 0, "current": "", "error": "",
               "at": 0.0, "result": None}
_WALL_DUPES_LOCK = threading.Lock()

# ── 🖼 壁纸库生成 ──
# 从图库挑够格的页复制进壁纸夹 (core/wallpaper_lib.py)。上千行判定 + 几百 MB 的复制,
# 同样不适合放在请求里等 → 后台线程 + 前端轮询, 与上面那份状态一模一样。
_WALL_LIB = {"status": "idle", "done": 0, "total": 0, "current": "", "error": "",
             "at": 0.0, "result": None}
_WALL_LIB_LOCK = threading.Lock()


def _wall_orb_candidates(f: Path, img_root: Path) -> "list[dict]":
    """ORB+颜色融合兜底溯源 (慢路): 用整库检索器找出最像的几页。
    检索器与 /api/library/similar 共用同一个单例 (首次要加载整库特征, 之后秒回)。"""
    global _ls_searcher
    try:
        with _ls_lock:
            if _ls_searcher is None:
                from core.local_search import ImageSearcher
                from utils.reporter import PrintReporter
                _ls_searcher = ImageSearcher(library_path=str(img_root),
                                             reporter=PrintReporter(), config=_config)
            hits = _ls_searcher.search(str(f), top_k=5)
    except Exception as e:
        return [{"error": str(e).splitlines()[0]}]
    out = []
    for h in hits or []:
        name = h.get("name") or ""
        if not isinstance(name, str):
            name = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
        out.append({"rel": name.replace("\\", "/"),
                    "fusion": round(float(h.get("fusion_score") or 0), 3),
                    "orb": round(float(h.get("orb_score") or 0), 3)})
    return out


def _kick_metadata_fill(limit: int = 100):
    """发现新入库图片后, 后台分批联网补全标题/标签/作者/封面, 直到队列清空。

    优先 HTTP 直连 ajax (requests + cookies.bin, 不启浏览器, 每秒数张);
    无 cookies 才回退 Selenium 共享浏览器 (慢但兜底)。连续失败熔断不拖垮
    主流程; 已删除作品不占连续失败; 每批零成功即停 (防已删作品空转)。
    自检是全量保险, 这是增量即时补。"""
    if _meta_fill_state["running"]:
        return
    _meta_fill_state["running"] = True

    def _upsert_meta(db, aid: str, art) -> None:
        db.upsert_artwork_metadata(
            aid, title=art.title,
            author_id=art.author_id or None,
            author_name=art.author_name,
            illust_type=art.illust_type.value,
            tags=list(art.tags),
            publish_date=art.publish_date,
            is_r18=art.is_r18,
            thumb_url=(art.image_urls or [None])[0],
            page_count=art.page_count,
            bookmark_count=int(getattr(art, "bookmark_count", 0) or 0),
            ai_type=getattr(art, "ai_type", 0))

    def _task():
        rep = _reporter()
        ok = 0                        # finally 里按"真补到了几个"决定是否登记脏域
        try:
            # ① 优先 HTTP: 用加密 cookies 直接打 ajax, 不启动浏览器
            http_sess = None
            try:
                from utils.sec import load_cookies_smart
                from core.search_client import build_pixiv_http_session, http_fetch_illust
                cookies = load_cookies_smart(str(_cookies_path()), upgrade=False)
                acc = _load_account()
                uid = str((acc or {}).get("user_id") or "")
                if cookies:
                    http_sess = build_pixiv_http_session(cookies, uid)
            except Exception:
                http_sess = None

            sc = None
            if http_sess is not None:
                mode, sleep_each, streak_limit = "HTTP 直连", 1.0, 8
            else:
                # ② 回退: Selenium 共享浏览器 (自检/登录路径同款)
                try:
                    sc = _get_search_client()
                    try:
                        sc.driver.set_page_load_timeout(20)
                    except Exception:
                        pass
                    online = False
                    try:
                        online = bool(sc.ensure_login() and sc.fetch_logged_in_user())
                    except Exception:
                        online = False
                    if not online:
                        rep.log("📥 检测到新入库图片，但当前未登录，标题/标签等元数据待登录后自检补全")
                        return
                except Exception:
                    rep.warn("📥 元数据补全跳过: 无登录态且浏览器不可用（自检可后续补）")
                    return
                mode, sleep_each, streak_limit = "浏览器", 0.0, 5

            rep.log(f"📥 开始后台补全新入库作品元数据（{mode}，分批进行）…")
            fail = done = streak = backoffs = 0
            stop = False
            while not stop and not _cancel_event.is_set():
                with _db() as db:
                    missing = db.works_missing_metadata(limit=limit)
                if not missing:
                    break
                ok_before_batch = ok
                for aid in missing:
                    aid = str(aid)
                    try:
                        if http_sess is not None:
                            art = http_fetch_illust(http_sess, aid)
                        else:
                            art = sc.fetch_artwork(aid, allow_dom_fallback=False)
                        with _db() as db:
                            _upsert_meta(db, aid, art)
                            # AI 判定必须**无条件落值**(含接口没给的 0), 否则
                            # ai_type IS NULL 的作品永远卡在补全队列头部每轮重问一遍
                            # 
                            db.set_artwork_ai_type(aid, getattr(art, "ai_type", 0))
                        ok += 1
                        streak = 0
                    except Exception as e:
                        fail += 1
                        msg = str(e).splitlines()[0]
                        gone = ("不存在" in msg or "找不到" in msg
                                or "已被删除" in msg or "已删除" in msg)
                        if gone:
                            # 确认失效的 pixiv 作品 → 记进 deleted_at 字段 (客观事实落库,
                            # 不再写「（作品已删除 xxx）」标题占位文案):
                            # ① 它自动退出「缺元数据」队列, 不必每轮 404 空转;
                            # ② 与标题解耦 —— 用户改标题再也不会改变「是否已删除」的判定。
                            # 非 pixiv 作品(ext_/merged_)没有 pixiv 作品页, 不标记。
                            try:
                                with _db() as db:
                                    db.mark_artwork_deleted(aid, True)
                            except Exception:
                                pass
                        else:
                            streak += 1
                            # HTTP 直连被 pixiv 限速 (连续 403) 时退避等待, 不急着放弃整批
                            if http_sess is not None and streak >= 4 and backoffs < 3:
                                wait = 30 * (backoffs + 1)
                                rep.warn(f"📥 连续失败 {streak} 次（疑似限速），等待 {wait}s 后继续…")
                                time.sleep(wait)
                                backoffs += 1
                                streak = 0
                            elif streak >= streak_limit:
                                rep.warn("📥 新图元数据补全连续失败，提前结束（自检可后续补）")
                                stop = True
                                break
                        # 作品已删/不可见不算连续失败
                    done += 1
                    if sleep_each:
                        time.sleep(sleep_each)
                    if done % 20 == 0:
                        rep.log(f"📥 元数据补全中: 已处理 {done}，成功 {ok}，失败 {fail}")
                if ok == ok_before_batch:
                    break  # 整批零成功 (多为已删作品/接口全挂已熔断), 不空转, 留给下次自检
            # 补完强制对账 + 散落图归位, 作者校正即时生效
            try:
                _cached_reconcile(force=True)
                img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
                moved = _organize_loose_images(img_root)
                if moved:
                    _cached_reconcile(force=True)
                    rep.log(f"📥 新图已自动归入作者文件夹 {moved} 张")
            except Exception:
                pass
            rep.log(f"📥 新图元数据补全结束: 成功 {ok}，失败 {fail}")
        except Exception as e:
            rep.warn(f"📥 元数据后台补全异常: {str(e).splitlines()[0]}")
        finally:
            _meta_fill_state["running"] = False
            # 非 HTTP 触发的写入 → 显式登记, 打开着的图库/作者页自发刷新 (事件驱动);
            # 一个都没补成功(未登录/全失败)就不登记, 免得白刷一遍
            if ok:
                mark_dirty("artworks", "authors", "facets", "stats")

    threading.Thread(target=_task, daemon=True).start()


_page_count_fill_state = {"running": False}


def _normalize_timestamps_at_startup():
    """启动时把历史上写歪的时间戳纠正成库内统一格式 (本地操作, 毫秒级)。

    不修的话「按修改时间」排序会一直错乱: 带 'T' 的行 (老代码几处用 isoformat() 写的)
    字符串比较永远排在所有正常时间戳前面 —— 那几条记录会一直顶在最前面。
    同一件事在体检「② 离线清理」里也做一遍 (幂等)。
    """
    try:
        with _db() as db:
            n = db.normalize_all_timestamps()
        if n:
            _reporter().log(f"🕒 已统一 {n} 处时间戳格式（修复「按修改时间」排序）")
        return n
    except Exception as e:
        try:
            _reporter().warn(f"时间戳格式统一失败: {str(e).splitlines()[0]}")
        except Exception:
            pass
        return 0


def _backfill_page_counts():
    """作品视图「原 N 页」历史数据补齐: 后台逐条 HTTP 抓作品总页数回填 total_pages。

    只处理 total_pages 为空的已有作品 (新下载/反搜归档的已由当场落库覆盖);
    0.6s/个 + 连续失败退避, daemon 线程不阻塞启动; 未登录自动跳过。
    顺带回填 bookmark_count (收藏排序用), COALESCE 语义绝不覆盖已有值。
    """
    if _page_count_fill_state["running"]:
        return
    _page_count_fill_state["running"] = True

    def _task():
        rep = _reporter()
        done = 0                      # finally 里要按"真补到了几个"决定是否登记脏域
        try:
            from utils.sec import load_cookies_smart
            from core.search_client import (build_pixiv_http_session,
                                            http_fetch_illust, ArtworkNotFoundError)
            cookies = load_cookies_smart(str(_cookies_path()), upgrade=False)
            session = build_pixiv_http_session(cookies)
            if not session:
                rep.warn("未登录, 跳过作品页数回填 (登录后重启或自检可补)")
                return
            with _db() as db:
                ids = [str(r["artwork_id"]) for r in db.conn.execute(
                    "SELECT artwork_id FROM artworks WHERE total_pages IS NULL").fetchall()]
            if not ids:
                return
            rep.log(f"🔄 页数回填: {len(ids)} 个作品缺总页数, 后台逐条补齐…")
            streak, backoffs, skip_deleted = 0, 0, 0
            for aid in ids:
                if _quitting:
                    return
                try:
                    art = http_fetch_illust(session, aid)
                    with _db() as db:
                        db.upsert_artwork_metadata(
                            aid, page_count=art.page_count,
                            bookmark_count=int(getattr(art, "bookmark_count", 0) or 0),
                            ai_type=getattr(art, "ai_type", 0))
                    streak = 0
                    done += 1
                    if done % 100 == 0:
                        rep.log(f"🔄 页数回填进度 {done}/{len(ids)}")
                except ArtworkNotFoundError:
                    skip_deleted += 1  # 已删作品页数无意义, 不占失败计数
                except PermissionError:
                    rep.warn("登录态失效, 停止页数回填")
                    return
                except Exception:
                    streak += 1
                    if streak >= 4 and backoffs < 3:
                        time.sleep(30 * (backoffs + 1))
                        backoffs += 1
                        streak = 0
                    if streak >= 12:
                        rep.warn("页数回填连续失败, 提前结束 (可稍后自检再补)")
                        return
                time.sleep(0.6)
            rep.log(f"✅ 页数回填完成: {done} 个已补, {skip_deleted} 个已删跳过")
        except Exception as e:
            rep.warn(f"🔄 页数回填异常: {str(e).splitlines()[0]}")
        finally:
            _page_count_fill_state["running"] = False
            if done:   # 真补到了才登记; 未登录/无待补/全失败的启动空转不该产生刷新噪声
                mark_dirty("artworks", "stats")   # 「原 N 页」/收藏数变了 → 图库与统计自发刷新

    threading.Thread(target=_task, daemon=True).start()


# 「无信息文件名」: 纯数字顺序号 (00000001) / 纯哈希串 (贴吧 32 位、Ehentai -7a93b54c836a829)。
# 一键整理只规范这一类 —— 名字本身可读的 (含中日文/英文单词/人名) 一律保留, 不把好名改差。
_MEANINGLESS_SEQ_RE = re.compile(r'^\d{6,}$')
# 允许以 - 开头 (Ehentai 的 -7a93b54c836a829 就是这种), 后面是 11+ 位十六进制/-/_ 
_MEANINGLESS_HEX_RE = re.compile(r'^[0-9a-fA-F\-][0-9a-fA-F\-_]{11,}$')


# 含中日韩文字 = 人读信息, 一律不当「无信息」
_CJK_RE = re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]')


def _is_meaningless_filename(stem: str) -> bool:
    """文件名主体是否「无信息」(纯顺序号 / 纯哈希串 / 单双字符碎片): 只有这类才值得规范化。

    中文/日文/韩文名字哪怕只有两个字(如「视奸」「雨」)也是人读信息, 绝不能当垃圾改名。
    """
    s = (stem or "").strip()
    if not s:
        return True
    if _CJK_RE.search(s):
        return False
    if _MEANINGLESS_SEQ_RE.match(s) or s.isdigit():
        return True
    if _MEANINGLESS_HEX_RE.match(s) and re.fullmatch(r'[0-9a-fA-F\-_]+', s):
        return True
    if len(s) <= 2:
        return True
    return False


# ── waifu2x 放大版: 文件名里的放大标记是「这是放大版」的唯一线索, 绝不能当乱码抹掉 ──
#  作为文件名规范化的**特殊格式**处理: 保留标记, 只把写法拉齐成
# `_waifu2x[_noiseN][_scaleNx]` —— 固定顺序、去掉多余/尾随下划线、畸形参数归一
# (`scale2x1`→`scale2x`、danbooru 的缩写 `w2x2x`→`waifu2x_scale2x`)。
# 只认「标记 + 已知参数」结尾, 后面还有别的词(如 `_waifu2x_final`)一律不动, 避免误吞信息。
_WAIFU_TAIL_RE = re.compile(
    r'[_\-]*(?:w2x|waifu2?x)'
    r'(?:[_\-]*(?:noise\d+|scale\d+x?\d*|\d+x?))*'
    r'[_\-]*$', re.IGNORECASE)


def _normalize_waifu_tail(tail: str) -> str:
    """把放大标记统一成 `_waifu2x[_noiseN][_scaleNx]`。"""
    t = (tail or "").lower().strip("_-")
    m_noise = re.search(r'noise[_\-]?(\d+)', t)
    noise = m_noise.group(1) if m_noise else None
    rest = t
    if m_noise:
        rest = (t[:m_noise.start()] + t[m_noise.end():]).strip("_-")
    m_scale = re.search(r'scale[_\-]?(\d+)', rest)
    if m_scale:
        scale = m_scale.group(1)
    elif rest.startswith("w2x"):
        # `w2x2x` 这类缩写: 标记后直接跟倍率数字 (只在缩写形式下找, 否则会把
        # `waifu2x` 里的 2 误当成倍率)
        m_bare = re.search(r'(\d+)', rest[len("w2x"):])
        scale = m_bare.group(1) if m_bare else None
    else:
        scale = None
    out = "waifu2x"
    if noise:
        out += f"_noise{noise}"
    if scale:
        out += f"_scale{scale}x"
    return "_" + out


def _sync_author_folder(db, aid: str, new_name: str, old_id: "str | None" = None) -> int:
    """把磁盘作者文件夹与 DB 现用名对齐 (pixiv 作者: 「现用名（≤2精选别名）_ID」)。
    改名 (自检/网页编辑) 后调用, 并同步 pages.local_path, 保证网页路径显示与磁盘一致。
    返回 1=已改名 / 0=无需或失败 (文件夹缺失/已存在目标名/非数字ID 均安全跳过)。
    old_id: 身份刚换过时用 (本地作者 local_xxx 绑定成 pixiv ID → 磁盘夹名还带着旧 ID)。"""
    if not (str(aid).isdigit() and new_name and new_name.strip()):
        return 0
    probe = str(old_id or aid)          # 旧文件夹名里带的那个 ID (默认就是自己)
    new_dir = _author_folder_name(db, aid, new_name)
    img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
    rows = db.conn.execute(
        "SELECT DISTINCT local_path FROM pages WHERE local_path LIKE ?",
        (f"%_{probe}/%",)).fetchall()
    done = set()
    for (lp,) in rows:
        if not lp:
            continue
        segs = [s for s in str(lp).split("/") if s]
        idx = next((i for i, s in enumerate(segs) if s.endswith(f"_{probe}")), None)
        if idx is None:
            continue
        old_rel = "/".join(segs[:idx + 1])
        if old_rel in done:
            continue
        done.add(old_rel)
        old_path = img_root / old_rel
        new_path = old_path.parent / new_dir  # 保持与旧文件夹同级 (如 X图/ 下), 不放回 img_root
        if not old_path.exists() or old_path == new_path or new_path.exists():
            continue
        old_path.rename(new_path)
        new_rel = "/".join(segs[:idx] + [new_dir])  # 完整相对前缀 (含父级 X图/), 不能只换基名
        db.rewrite_page_path_prefix(old_rel, new_rel)
        return 1
    return 0


def _gather_works_into_author_folder(db, artwork_ids: "list[str]") -> dict:
    """把指定作品的**在库文件**按「一键整理」的同一套期望夹规则归位。

    触发场景: 「本地作者 (如 bilibili 账号) 绑定成真 pixiv 作者」后,
    文件还留在登记时的落脚点 (`#其他来源#/xxx.png`) —— 身份换了磁盘却没跟着走,
    用户还得自己再跑一次一键整理。
    做法: **复用** `_tidy_build_plans` + `_tidy_apply_plans` (不另写一套搬移逻辑, 保证与
    一键整理的摆放结果**永远一致**), 只用 plan_ids 把范围锁在本次合并的作品上。
    只执行 move 计划 —— 文件夹改名已由 `_sync_author_folder` 负责, 文件名规范化不在本次职责内。
    绝不覆盖同名文件 (撞名由 `_tidy_apply_plans` 记进 failed 并跳过)。
    返回 {"moved", "failed", "dests"}。"""
    ids = [str(x) for x in (artwork_ids or []) if str(x).strip()]
    if not ids:
        return {"moved": 0, "failed": [], "dests": []}
    img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
    ph = ",".join("?" * len(ids))
    rels = {str(r[0]).replace("\\", "/") for r in db.conn.execute(
        f"SELECT DISTINCT local_path FROM pages WHERE artwork_id IN ({ph}) "
        f"  AND trashed_at IS NULL AND local_path IS NOT NULL AND local_path != ''",
        ids).fetchall()}
    if not rels:
        return {"moved": 0, "failed": [], "dests": []}
    plans = _tidy_build_plans(db, img_root)
    want = [p["id"] for p in plans
            if p["type"] == "move" and p["auto"] and (set(p["files"]) & rels)]
    if not want:
        return {"moved": 0, "failed": [], "dests": []}
    rep = _tidy_apply_plans(db, img_root, want)
    _want = set(want)
    dests = sorted({p["to"] for p in plans if p["id"] in _want})
    return {"moved": int(rep.get("moved") or 0),
            "failed": list(rep.get("failed") or []),
            "dests": dests}


# ==================================================================
# 一键整理: 全库规范化 (预览后执行)。职责 = 磁盘摆放规范 (移动/改名/归类),
# 与「同步」(账实一致) 正交: 同步管 DB 记录与磁盘一致, 整理管磁盘摆放规范。
#   计划类型:
#     rename    作者文件夹名不规范 (旧名/污染/未标站点) → 规范名
#     move      作品页放在错误/未归类文件夹 → 归位到期望作者文件夹
#     filrename 文件名是无信息乱码 → 规范化
#     rmdir     空夹 (整棵子树除系统杂物外没有任何文件) → 删除
#     categorize 根目录散件/未知作者文件夹/作者归属存疑 → 需人工确认 (auto=False)
# ==================================================================
_TIDY_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# 系统杂物文件名: 一个夹里除了这些再没别的文件, 就算「空夹」(可整棵删)
_TIDY_JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}


def _tidy_dir_is_disposable(d: Path) -> bool:
    """整棵子树里除系统杂物外**没有任何文件** → 这个夹可以整棵删掉 (整理搬空后的残留夹)。

    比健康面板那条「不含图片即可删」更保守: 夹里若有 .psd / .txt / .zip 等**任何真实文件**,
    一律不删 —— 整理只清它自己搬空的、真·空的夹, 不替用户判断非图片资料的价值。"""
    for f in d.rglob("*"):
        if f.is_file() and f.name.lower() not in _TIDY_JUNK_NAMES:
            return False
    return True


def _is_author_folder(db, seg: str) -> bool:
    """seg 是不是「作者夹」`名字_作者ID`（该 ID 在 authors 表里存在，避免把普通目录误判）。
    作者 ID 可能是 `local_xxx`（本身就含下划线），所以从右往左试每一处 `_` 的右半段，
    而不是只看最后一个 `_`。"""
    if not seg:
        return False
    idx = len(seg)
    while True:
        idx = seg.rfind("_", 0, idx)
        if idx <= 0:
            return False
        if db.get_author(seg[idx + 1:]):
            return True


def _tidy_parent_prefix(db, parent: str) -> str:
    """取「期望作者文件夹」的同级前缀（与 `_tidy_expected_folder` 配套）。

    parent 的最后一段若本身就是某位作者的作者夹 —— **不论它是作品自己的作者、还是别人的**
    —— 就把它剥掉，让期望夹与它同级；然后由 `_tidy_expected_folder` 把作品**自己的**作者夹
    重新拼回来。不剥会算出两种自我套娃（真实图库都实测撞到过）:
      · 单来源 local_ 作者的文件已在 `作者夹/站点@账号` → `X图/作者夹/作者夹`；
      · 作品被放错到**别人**的夹里（merged 作品属于「ying yi」却躺在「qing0ying0」夹里）
        → `X图/qing0ying0夹/ying yi夹`（跨作者套娃）。
    单来源 → 平铺回作者夹本身（不建站点子夹）。"""
    if not parent:
        return parent
    seg = parent.split("/")[-1]
    if _is_author_folder(db, seg):
        return parent[:-(len(seg) + 1)] if "/" in parent else ""
    return parent


def _tidy_expected_folder(db, meta, parent_prefix: str) -> "str | None":
    """算作品的期望作者文件夹 rel (与 parent_prefix 同级)。
    新结构  按作者聚合、作者内按来源分。
    - pixiv 作品 → 顶层夹「现用名_ID」(直接放顶层);
    - 外来源作品且作者已建档(数字 pixiv ID 或外来作者 local_xxx) → 顶层「作者名_ID」下的
      「站点@账号」子夹 (同一作者的 pixiv/twitter/bilibili 图都收进一个顶层, 不再散落);
      **单来源也一样建这层子夹** —— 否则夹名里看不出作品来自哪个站 ;
    - 外来源作品且**没有**作者 (未建档/未记录名字) → 平铺「站点@账号」(不臆造层级);
    - 账号为空 → 子夹/平铺用纯站点名 (如 bilibili);
    - 未知来源(other) / 无账号(未知作者) → 夹名加 `#...#` 包裹 
      未知来源 → `#其他来源#`; 真实来源无账号 → `#站点#` (与普通「站点@账号」区分开);
      不用下划线前缀的原因: 部分作者名本身就以下划线开头/包含 (如 `_LM7_`), `_` 前缀区分度不够;
    - 定位不到作者返回 None (交人工确认)。"""
    if not meta:
        return None
    src = str(meta.get("source") or "").strip()
    if src and src != "pixiv":
        acc = str(meta.get("source_author") or "").strip()
        # 未知来源 (other/其他来源/未知来源) → 基底「其他来源」; 否则用站点名
        base = "其他来源" if src.lower() in ("other", "其他来源", "未知来源") \
            else _sanitize_dir(src)
        site_fname = _sanitize_dir(f"{base}@{acc}") if acc else f"#{_sanitize_dir(base)}#"
        aid = str(meta.get("author_id") or "")
        # 外来源作品: 作者是 pixiv 用户(数字 id, 6-9 位) → 聚合到其 pixiv 顶层下的「站点@账号」子夹
        if aid.isdigit():
            a = db.get_author(aid)
            if a and str(a.get("name") or "").strip():
                top = _author_folder_name(db, aid, str(a["name"]))
                return f"{parent_prefix}/{top}/{site_fname}" if parent_prefix \
                    else f"{top}/{site_fname}"
        # 外来作者 (local_xxx) 已记录可读名字 → 顶层按作者名命名, 内部按来源分子夹:
        # 「作者名_local_xxx/站点@账号」(单来源、多来源一视同仁 —— 保证来源始终可见)
        if aid.startswith("local_"):
            a = db.get_author(aid)
            nm = str(a.get("name") or "").strip() if a else ""
            if nm and not _is_unreadable_name(nm):
                top = _author_folder_name(db, aid, nm)
                # **单来源也保留「站点@账号」子夹**。
                # 原来单来源直接平铺「作者名_local_xxx」, 结果夹名里完全看不出作品来自哪个站
                # (实测: `bilibili@Colorful辉色` 被搬成 `Colorful辉色_local_629de578ab2d`,
                # 来源信息丢失, 用户"看文件夹名不知道是 bili 的作品")。
                # 现在统一「顶层作者 / 内部按来源分」, 与多来源、以及 pixiv 作者夹里放
                # 「twitter@账号」子夹的规则完全一致。
                return f"{parent_prefix}/{top}/{site_fname}" if parent_prefix \
                    else f"{top}/{site_fname}"
        # 未记录名字的外来作者 / 无作者 → 平铺「站点@账号」(无账号 → `#站点#`)
        return f"{parent_prefix}/{site_fname}" if parent_prefix else site_fname
    # pixiv 作品: 顶层夹「现用名_ID」
    aid = str(meta.get("author_id") or "")
    if not aid or not aid.isdigit():
        return None
    a = db.get_author(aid)
    if not a or not str(a.get("name") or "").strip():
        return None
    fname = _author_folder_name(db, aid, str(a["name"]))
    return f"{parent_prefix}/{fname}" if parent_prefix else fname


def _tidy_build_plans(db, img_root: Path) -> list:
    """扫描图库生成规范化计划 (纯计算, 不移动任何文件)。
    返回 [{id, type, from, to, reason, files: [相对路径], auto}]。"""
    img_root = Path(img_root).resolve()
    plans = []
    _seq = [0]
    frozen = set()   # 作者归属有争议的文件夹: 冻结, 只出「待确认」, 等用户定完再动

    def _add(ptype, frm, to, reason, files, auto, **extra):
        # 同 (type, from, to) 合并 files (一夹多作品/多文件)
        for pl in plans:
            if (pl["type"], pl["from"], pl["to"]) == (ptype, frm, to):
                for f in files:
                    if f not in pl["files"]:
                        pl["files"].append(f)
                pl["auto"] = pl["auto"] and auto
                return
        _seq[0] += 1
        item = {"id": _seq[0], "type": ptype, "from": frm, "to": to,
                "reason": reason, "files": list(files), "auto": auto}
        item.update(extra)   # 供前端做逐项处理用的附加信息 (如作者争议的两边证据)
        plans.append(item)

    # ---- ① DB 页驱动: 按 (所在文件夹) 检查归属 ----
    from collections import defaultdict
    rows = db.conn.execute(
        "SELECT artwork_id, local_path FROM pages "
        "WHERE local_path IS NOT NULL AND local_path != '' "
        "  AND trashed_at IS NULL").fetchall()
    fgrp = defaultdict(lambda: defaultdict(list))  # {folder: {artwork: [rel]}}
    covered_files = set()
    for r in rows:
        rel = str(r["local_path"]).replace("\\", "/").strip("/")
        if not rel:
            continue
        folder = "/".join(rel.split("/")[:-1])
        fgrp[folder][str(r["artwork_id"])].append(rel)
        covered_files.add(rel)
    covered_folders = set(fgrp.keys())

    for folder, arts in fgrp.items():
        segs = [s for s in folder.split("/") if s]
        parent = "/".join(segs[:-1])
        base = segs[-1] if segs else ""
        # ①a 作者归属争议 → 「待确认」(单向闭环 用户→数据库→磁盘)。
        # 文件夹名里解析出的作者与 DB 记录不一致时**绝不自动改写数据库** —— 那是让数据库听
        # 磁盘的, 方向反了。只把两边证据摆出来交给用户判断; 用户定完作者(写入 DB)后, 下一次
        # 整理才由 DB 驱动去改磁盘。争议未决期间**整夹冻结**(不产生改名/归位/命名规范计划),
        # 否则先按 DB 改了磁盘、又反过来"印证"DB, 证据就没了。
        # 注意: 剥的是「结尾最后一个 _6-9位数字」, kase_22_43460823 → 名 kase_22 / pid 43460823,
        # 作者名里的 _22(2位) 绝不会被当成 pid。
        fm = re.search(r'_(\d{6,9})$', base) if base else None
        if fm:
            f_aid = fm.group(1)
            f_name = _sanitize_dir(base[:fm.start()])
            disputed = set()
            for art_id in sorted(arts):
                meta = db.get_artwork_metadata(art_id)
                if not meta:
                    continue
                m_aid = str(meta.get("author_id") or "")
                if m_aid == f_aid:
                    continue
                oa = db.get_author(m_aid) if m_aid else None
                db_nm = str(oa.get("name") or "").strip() if oa else ""
                # to 带上作品 ID: 一夹多件争议时各自成行 (不合并), 前端才好逐条处理
                _add("categorize", folder, f"(待确认) 作品 {art_id}",
                     f"作者归属存疑: 文件夹名显示「{f_name}」({f_aid}), "
                     f"数据库记录「{db_nm or '无'}」({m_aid or '无'}) —— "
                     f"请确认以哪边为准, 定好后再整理一次",
                     list(arts[art_id]), False,
                     artwork_id=art_id, claim_aid=f_aid, claim_name=f_name,
                     db_aid=m_aid, db_name=db_nm)
                disputed.add(art_id)
            if disputed:
                frozen.add(folder)
                continue
        active = dict(arts)
        if not active:
            continue
        # 单作者旧名/裸ID夹, 或单账号别名夹 (bili@→bilibili@、twi@→twitter@、GGAC@→ggac@)
        # → 整体改名 (规范名)。别名夹即使含同账号多来源 (如 twi@X 里混了 danbooru 文件)
        # 也整体改名, 跨来源文件随后由 move 计划按改名映射重定位搬出。
        author_ids = set()
        acct_set = set()
        for art_id in active:
            meta = db.get_artwork_metadata(art_id)
            if meta:
                aid = str(meta.get("author_id") or "")
                if aid:
                    author_ids.add(aid)
                if str(meta.get("source_author") or "").strip():
                    acct_set.add(str(meta["source_author"]).strip())
        meta0 = db.get_artwork_metadata(next(iter(active)))
        exp = None
        renameable = False
        if len(author_ids) == 1:
            aid0 = str(meta0.get("author_id") or "")
            _e = _tidy_expected_folder(db, meta0, parent)
            # 只允许同级改名 (exp 的父级 == 当前夹父级)。pixiv 顶层夹里混外来源作品时
            # exp 是「夹/子夹」, 绝不能把顶层夹整体改名成子夹, 交给逐作品归位处理。
            if (_e and _e != folder
                    and "/".join(_e.split("/")[:-1]) == parent
                    and (base == aid0 or base.endswith(f"_{aid0}"))):
                exp, renameable = _e, True
        if not renameable and len(acct_set) == 1:
            _fa = parse_foreign_account(base)
            if _fa and next(iter(acct_set)) == _fa[1]:
                _exp_f = f"{_sanitize_dir(_fa[0])}@{_sanitize_dir(_fa[1])}"
                _e = f"{parent}/{_exp_f}" if parent else _exp_f
                if _e != folder:
                    exp, renameable = _e, True
        if renameable and exp:
            files = [f for fs in active.values() for f in fs]
            _add("rename", folder, exp,
                 f"作者文件夹名不规范: {base} → {exp.split('/')[-1]}",
                 files, True)
            continue
        # 逐作品归位 (已更正作者的作品跳过: 文件夹即正确)
        for art_id, files in active.items():
            meta = db.get_artwork_metadata(art_id)
            exp = None
            if meta:
                # 期望夹 prefix: parent 自己若是作者夹 (不论是本作品的作者、还是别人),
                # 先剥掉再拼本作品的作者夹 —— 否则会算出 `作者夹/作者夹` 的自我套娃
                exp = _tidy_expected_folder(db, meta, _tidy_parent_prefix(db, parent))
            if exp and exp != folder:
                _add("move", folder, exp,
                     f"作品 {art_id} 未放在作者文件夹 {exp.split('/')[-1]} 中, 归位",
                     files, True)
            elif not exp and folder == "":
                _add("categorize", "", "(待确认)",
                     f"根目录散件 (作品 {art_id}): 无作者信息, 无法自动归类, 需人工确认",
                     files, False)

    # ---- ② 磁盘驱动: DB 未登记页的文件夹/散件 ----
    rename_folders = {p["from"] for p in plans if p["type"] == "rename"}
    # ②a 未知文件夹: 形如 作者_数字 且 DB 有该作者 → 规范改名 (文件夹内无登记页)
    for d in sorted(img_root.rglob("*")):
        if not d.is_dir():
            continue
        rel = str(d.relative_to(img_root)).replace("\\", "/")
        if rel in covered_folders or rel in rename_folders:
            continue
        segs = [s for s in rel.split("/") if s]
        base = segs[-1]
        m = re.search(r'_(\d{6,9})$', base)
        if not m:
            continue
        aid = m.group(1)
        a = db.get_author(aid)
        if not a or not str(a.get("name") or "").strip():
            continue
        parent = "/".join(segs[:-1])
        fname = _author_folder_name(db, aid, str(a["name"]))
        if base != fname:
            _add("rename", rel, f"{parent}/{fname}" if parent else fname,
                 f"作者文件夹名不规范 (库内无登记页, 仅改名): {base} → {fname}",
                 [], True)
    # ②b 散落文件: 没被 ① 覆盖、且不在待改名文件夹里的
    unknown = defaultdict(list)  # {folder: [rel]}
    for p in sorted(img_root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _TIDY_IMG_EXT:
            continue
        rel = str(p.relative_to(img_root)).replace("\\", "/")
        if rel in covered_files:
            continue
        segs = [s for s in rel.split("/") if s]
        folder = "/".join(segs[:-1])
        base = segs[-1]
        if folder in rename_folders or folder in frozen:
            continue  # 随文件夹改名一起, 或作者争议冻结中, 不单独计划
        nat = pixiv_natural_key(base)
        if nat:
            meta = db.get_artwork_metadata(nat[0])
            # 期望夹 = 「文件所在文件夹的父级」+ 作者夹名; 已在作者夹内(exp==folder)则不动
            parent_prefix = "/".join(segs[:-2])
            exp = _tidy_expected_folder(db, meta, parent_prefix) if meta else None
            if exp:
                if exp != folder:
                    _add("move", folder, exp,
                         f"文件 {base} 属于作品 {nat[0]}, 归位到作者文件夹",
                         [rel], True)
                continue  # pixiv 散件处理完 (含已在正确夹), 不再进 unknown
        fa = parse_foreign_account(base)
        if fa:
            fname = _sanitize_dir(f"{fa[0]}@{fa[1]}")
            parent_prefix = "/".join(segs[:-2])
            exp = f"{parent_prefix}/{fname}" if parent_prefix else fname
            if exp != folder:
                _add("move", folder, exp,
                     f"外来源文件 {base} 归类到 {fname}",
                     [rel], True)
            continue
        unknown[folder].append(rel)
    for folder, files in unknown.items():
        if folder == "":
            _add("categorize", "", "(待确认)",
                 "根目录散件: 无法自动定位归属, 需人工确认", files, False)
        else:
            _add("categorize", folder, "(待确认)",
                 f"文件夹 {folder} 无匹配作者/作品记录, 需人工确认", files, False)

    # ---- ③ 文件名规范化: 「乱码名」→ 统一可读名 ----
    # 原则: 图片名归用户/来源, 系统只在名字**毫无信息**时才动手,
    # 且必须出现在预览表里 (旧名 → 新名 + 理由), 看过再执行, 绝不静悄悄改。
    #   · pixiv 作品 → `{pid}_p{页码}.ext`（与下载命名一致, 也是对账的第一身份）
    #   · 非 pixiv 且原名是无信息串 → `{站点@账号}_{序号:03d}.ext`（不写作者名/不写假页码）
    #   · 名字本身可读（含中日文/英文单词/人名）→ 一律保留, 不把好名字改成差名字
    # 只处理已在正确文件夹内的登记页: 要搬的交给 move、要改夹名的随夹一起, 避免路径交叉。
    move_files = {f for pl in plans if pl["type"] == "move" for f in pl["files"]}
    names_by_folder = defaultdict(set)
    for rr in db.conn.execute(
            "SELECT local_path FROM pages WHERE local_path IS NOT NULL "
            "  AND local_path != '' AND trashed_at IS NULL"):
        rel0 = str(rr[0]).replace("\\", "/")
        names_by_folder["/".join(rel0.split("/")[:-1])].add(rel0.split("/")[-1].lower())

    def _free(folder: str, name: str) -> str:
        """同夹内不撞名: 撞了就加 (2)(3)，与合并的命名策略一致, 绝不覆盖已有文件。"""
        if name.lower() not in names_by_folder[folder]:
            names_by_folder[folder].add(name.lower())
            return name
        _st, _su = Path(name).stem, Path(name).suffix
        k = 2
        while f"{_st} ({k}){_su}".lower() in names_by_folder[folder]:
            k += 1
        cand = f"{_st} ({k}){_su}"
        names_by_folder[folder].add(cand.lower())
        return cand

    _seq_of = defaultdict(int)
    for r in db.conn.execute(
            "SELECT p.artwork_id, p.page_num, p.local_path, a.source, a.source_author "
            "  FROM pages p JOIN artworks a ON a.artwork_id = p.artwork_id "
            " WHERE p.local_path IS NOT NULL AND p.local_path != '' "
            "   AND p.trashed_at IS NULL "
            " ORDER BY p.artwork_id, p.page_num"):
        rel = str(r["local_path"]).replace("\\", "/")
        if rel in move_files:
            continue
        folder = "/".join(rel.split("/")[:-1])
        if folder in rename_folders or folder in frozen:
            continue
        base = rel.split("/")[-1]
        aid = str(r["artwork_id"])
        try:
            page = int(r["page_num"])
        except (TypeError, ValueError):
            page = 0
        ext = Path(base).suffix.lower() or ".jpg"
        stem0 = Path(base).stem
        # ① 放大版特殊格式 (waifu2x / w2x): 放大标记必须保留, 只把写法拉齐;
        #    这一支优先于 pixiv 自然键判断 —— 否则带自然键的放大版会被当成"已有自然键"跳过。
        _wm = _WAIFU_TAIL_RE.search(stem0)
        if _wm and _wm.start() > 0:
            want = f"{stem0[:_wm.start()]}{_normalize_waifu_tail(stem0[_wm.start():])}{ext}"
        elif aid.isdigit():
            # 名字里已经带该作品的自然键 (含裸 ID / 带 `_waifu2x...` 之类后缀) → 不动:
            # 后缀本身是信息(放大工具/倍率), 名字也不算乱码, 没必要churn一堆文件。
            _nat = pixiv_natural_key(base)
            if _nat and _nat[0] == aid:
                continue
            want = f"{aid}_p{page}{ext}"
        elif _is_meaningless_filename(stem0):
            # 非 pixiv 乱码名 → `{站点@账号}_{序号}.ext` (2026-09-14 定案):
            # **不写作者名** —— 作者名归数据库管; 一旦写进文件名就会被"可读名一律不动"永久
            # 固化, 之后作者改名/更正都纠正不回来。也不写 `_p{页码}`: 非 pixiv 没有页序, 恒为假 p0。
            # 序号按作品 ID 排序生成(与遍历顺序一致), 可重现、不随扫描抖动。
            _seq_of[folder] += 1
            _src = str(r["source"] or "").strip()
            acc = str(r["source_author"] or "").strip()
            site = "其他来源" if _src.lower() in ("other", "其他来源", "未知来源") \
                else (_src or "未署名")
            who = _sanitize_dir(f"{site}@{acc}") if acc else _sanitize_dir(site)
            want = f"{who}_{_seq_of[folder]:03d}{ext}"
        else:
            continue
        if want == base:
            continue
        want = _free(folder, want)
        _add("filrename", rel, f"{folder}/{want}" if folder else want,
             f"文件名不规范: {base} → {want}", [rel], True)

    # ---- ④ 清理空夹: 整棵子树只有空目录/系统杂物的文件夹 ----
    # 为什么要放进整理 (一键整理要自带清空夹):
    # 整理自己会把图搬走 (归位/平铺) 从而**制造**空夹, 善后理应在同一次"一键"里做完,
    # 而不是让用户再跑去健康面板点一次「删除空文件夹」。实测真实库整理完留着 16 个空夹。
    # 只列**最上层**那一处 (子夹随它一起删), 免得同一片残留列出一排重复行。
    _todo = [img_root]
    while _todo:
        _d = _todo.pop()
        try:
            _subs = sorted([x for x in _d.iterdir() if x.is_dir()])
        except OSError:
            continue
        for _s in _subs:
            _rel = str(_s.relative_to(img_root)).replace("\\", "/")
            if _rel in frozen:
                continue        # 作者争议冻结中 → 它的夹一个指头都不碰
            if _tidy_dir_is_disposable(_s):
                _add("rmdir", _rel, "(删除)",
                     f"空文件夹: {_rel}（除系统杂物外没有任何文件）", [], True)
            else:
                _todo.append(_s)
    return plans


def _tidy_apply_plans(db, img_root: Path,
                      plan_ids: "list[int] | None" = None) -> dict:
    """执行整理计划: 先文件夹改名, 再逐文件移动, 再文件名规范化, 最后清空夹; 同步 pages.local_path。
    只执行 auto 计划 (categorize/待确认 永不自动执行)。
    返回 {renamed, moved, filenamed, rmdirs, failed}。"""
    plans = _tidy_build_plans(db, img_root)
    if plan_ids is not None:
        want = {int(x) for x in plan_ids}
        plans = [p for p in plans if p["id"] in want]
    # 安全: 只执行 auto 计划 (改/移有据可依); categorize(待确认) 只展示, 永不自动执行
    plans = [p for p in plans if p["auto"]]
    img_root = Path(img_root).resolve()
    renamed = moved = filenamed = 0
    failed = []
    # ① 文件夹改名 (先做, 让后续移动路径不再变) + 记录改名映射 (move 源被改名后按映射重定位)
    rename_map = {}
    for p in plans:
        if p["type"] != "rename":
            continue
        src = img_root / p["from"]
        dst = img_root / p["to"]
        if not src.is_dir():
            failed.append(f"{p['from']} → {p['to']}: 源文件夹不存在")
            continue
        if dst.exists():
            # NTFS 大小写不敏感: dst 与 src 仅大小写不同 = 同一个夹, 允许改名 (规范化大小写,
            # 如 GGAC@毒瘤 → ggac@毒瘤); 否则是真冲突, 跳过
            if os.path.normcase(str(dst)) != os.path.normcase(str(src)):
                failed.append(f"{p['from']} → {p['to']}: 目标已存在, 跳过")
                continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            renamed += 1
            rename_map[p["from"]] = p["to"]
            db.rewrite_page_path_prefix(p["from"], p["to"])
            db.refresh_ext_titles()  # 文件夹整体改名 → 外来作品标题里的路径同步刷新
        except Exception as e:
            failed.append(f"{p['from']} → {p['to']}: {str(e).splitlines()[0]}")
    # ② 逐文件移动
    for p in plans:
        if p["type"] != "move":
            continue
        for rel in p["files"]:
            src = img_root / rel
            if not src.is_file():
                # 文件所在文件夹已被整体改名 (如 twi@X → twitter@X) → 按改名映射重定位源
                alt = rel
                for _old, _new in rename_map.items():
                    if rel.startswith(_old + "/"):
                        alt = _new + rel[len(_old):]
                        break
                if alt != rel:
                    rel = alt
                    src = img_root / alt
            if not src.is_file():
                failed.append(f"{rel}: 源文件不存在")
                continue
            dst = img_root / p["to"] / src.name
            if dst.exists():
                failed.append(f"{rel}: 目标 {p['to']}/{src.name} 已存在, 跳过")
                continue
            try:
                (img_root / p["to"]).mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                moved += 1
                new_rel = f"{p['to']}/{src.name}"
                if rel != new_rel:
                    db.relink_page_path(rel, new_rel)
            except Exception as e:
                failed.append(f"{rel}: {str(e).splitlines()[0]}")
    # ③ 文件名规范化 (已在正确文件夹内 → 原地改名 + 同步 DB 路径; 目标已存在则跳过, 绝不覆盖)
    for p in plans:
        if p["type"] != "filrename":
            continue
        for rel in p["files"]:
            src = img_root / rel
            if not src.is_file():
                failed.append(f"{rel}: 源文件不存在")
                continue
            dst = img_root / p["to"]
            if dst.exists():
                failed.append(f"{rel}: 目标 {p['to']} 已存在, 跳过")
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                filenamed += 1
                db.relink_page_path(rel, p["to"])   # 内含 ext_ 标题刷新
            except Exception as e:
                failed.append(f"{rel}: {str(e).splitlines()[0]}")
    # ④ 清理空夹 (整棵子树只有空目录/系统杂物): 由深到浅删, 目标已不在就跳过
    import shutil as _sh
    rmdirs = 0
    for _p in sorted([x for x in plans if x["type"] == "rmdir"],
                     key=lambda x: -x["from"].count("/")):
        _d = img_root / _p["from"]
        if not _d.is_dir():
            continue
        try:
            _sh.rmtree(str(_d))
            rmdirs += 1
        except OSError as e:
            failed.append(f"{_p['from']}: 删除空夹失败 {str(e).splitlines()[0]}")
    # ⑤ (已移除) 旧「作者更正」执行段: 曾用文件夹名解析出的作者**无条件覆盖** DB。
    # 数据只能「用户 → 数据库 → 磁盘」单向流动, 检测到磁盘与 DB 不一致时
    # 由 ①a 生成「待确认」项交给用户判断, 绝不自动改写数据库。
    return {"renamed": renamed, "moved": moved, "filenamed": filenamed,
            "rmdirs": rmdirs, "failed": failed}


def _readable_author_name(db, aid: str, weird: str) -> str:
    """现用名不可辨认 (空白符号/纯符号) 时回退到最近可辨认名, 让界面/文件夹可读可搜。
    优先当前 DB 主名 (它往往就是「上一次的可读名」, 首次改名时还没进曾用名),
    其次曾用名; 全部不可辨认才保留原值 (作者所有名字都奇形怪状)。"""
    try:
        a = db.get_author(aid)
        cur = str(a.get("name") or "").strip() if a else ""
        aliases = list(a.get("aliases") or []) if a else []
    except Exception:
        cur, aliases = "", []
    if cur and cur != weird and not _is_unreadable_name(cur):
        return cur
    for x in aliases:
        xx = _POLLUTE_PAREN.sub('', x or '').strip()
        xx = re.sub(r'_\d{6,9}$', '', xx).strip()
        if xx and not _is_unreadable_name(xx) and not _search_promo(xx) and len(xx) <= 30:
            return xx
    return weird


def _refresh_author_names(db, session, rep, progress_cb=None) -> int:
    """作者改名同步: pixiv 作者常改名, HTTP 直连逐个核对 authors 表昵称, 变了就更新。
    (upsert_author 覆盖旧名; 体检随后的文件夹校正步骤会按新名归位文件夹)
    现用名为空白符号 (作者故意改不改名) 时, 主名称回退到最近的曾用名, 避免界面显示空名。
    返回改名个数。网络失败跳过不熔断; 作者注销 (None) 保留旧名不动。
    progress_cb(i, total, 当前作者) 可选: 体检面板据此显示 x/y 详细进度。"""
    from core.search_client import http_fetch_user_name
    rows = db.conn.execute(
        "SELECT author_id, name FROM authors WHERE author_id GLOB '[0-9]*'"
    ).fetchall()
    renamed = checked = 0
    for _i, r in enumerate(rows, 1):
        if progress_cb:
            try:
                progress_cb(_i, len(rows), str(r["name"] or r["author_id"]))
            except Exception:
                pass
        aid, old = str(r["author_id"]), r["name"]
        try:
            new = http_fetch_user_name(session, aid)
        except Exception:
            continue  # 网络/限速: 跳过, 下次自检再试
        checked += 1
        if new:  # 作者存在: 顺手把存续状态入库 (图库「状态」筛选数据源)
            if _is_unreadable_name(new):
                # 空白/纯符号现用名 → 回退到最近可辨认名 (当前主名或曾用名),
                # 绝不把搜不出的名字当主名称写进数据库
                new = _readable_author_name(db, aid, new)
            if new != old:
                db.upsert_author(aid, new)
                rep.log(f"🩺 [改名] 作者 {old} → {new} ({aid})")
                renamed += 1
                _sync_author_folder(db, aid, new)  # 磁盘文件夹跟随现用名 (净名_ID)
            db.set_author_status(aid, "alive")
        else:  # 用户不存在/已注销: 状态记 gone, 名字保留
            db.set_author_status(aid, "gone")
        time.sleep(0.5)
    rep.log(f"🩺 [改名] 核对 {checked}/{len(rows)} 位作者, 更新 {renamed} 个改名")
    return renamed


# 缩略图预热: 缓存只覆盖浏览过的页面, 翻到没看过的一页时那批图要现场生成
# (大图单张 1s+, 串行排队), 体验像"每次打开都重新生成一遍"。启动后台线程把
# 全库缩略图一次生成进 data/thumbs/, 之后任意翻页全部命中缓存秒开。
_thumb_warm_state = {"running": False, "done": False, "abort": False}
_thumb_warm_thread = None

# 本地以图搜图 (ORB+颜色) 单例: 首次构建特征库较慢, 之后常驻秒回
_ls_lock = threading.Lock()
_ls_searcher = None


def warm_thumbs_background(force: bool = False) -> None:
    """后台逐张预生成图库缩略图 (每张间隔让路用户请求)。

    默认只跑一次 (done 标志防重入); force=True 时重置 done, 用于设置页
    「立即预热全部缩略图」按钮: 新入库图片后再次批量补齐缓存。"""
    global _thumb_warm_thread
    if _thumb_warm_state["running"]:
        return
    if _thumb_warm_state["done"] and not force:
        return
    _thumb_warm_state["done"] = False
    _thumb_warm_state["abort"] = False
    _thumb_warm_state["running"] = True

    def _task():
        rep = _reporter()
        try:
            with _db() as db:
                rels = [r["local_path"] for r in db.conn.execute(
                    "SELECT DISTINCT local_path FROM pages "
                    "WHERE local_path IS NOT NULL AND local_path != ''").fetchall()]
            if not rels:
                _thumb_warm_state["done"] = True
                return
            img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
            # ① 先快速核对缓存: 只挑出「本地文件在、缩略图还没缓存」的, 不生成/不限速。
            #    缓存是持久的 (data/thumbs), 所以正常情况下这里几乎全命中 ——
            #    不做这一步的话, 每次开服务都会拿 20ms/张 的限速把全库空转一遍 (~1800 张 ≈ 40s)。
            pending = [(img_root / rel, rel) for rel in rels
                       if (img_root / rel).is_file()
                       and not _thumb_cached(img_root / rel, "img", rel)]
            if not pending:
                _thumb_warm_state["done"] = True
                rep.log(f"🖼 缩略图缓存已就绪 (全库 {len(rels)} 张), 无需预热")
                return
            # ② 只为缺的这批生成, 才需要限速让路用户请求
            rep.log(f"🖼 开始后台预热缩略图: 缺 {len(pending)}/{len(rels)} 张…")
            ok = fail = 0
            for i, (f, rel) in enumerate(pending):
                # 中止信号: 清除缓存时 abort=True → 跳出循环, 不再生成新缓存
                if _thumb_warm_state["abort"]:
                    rep.log(f"🖼 缩略图预热已中止, 已处理 {i}/{len(pending)}")
                    return
                if _get_thumb(f, "img", rel) is not None:
                    ok += 1
                else:
                    fail += 1
                if (i + 1) % 300 == 0:
                    rep.log(f"🖼 缩略图预热中: {i + 1}/{len(pending)}")
                time.sleep(0.02)  # 让路用户请求
            _thumb_warm_state["done"] = True
            rep.log(f"🖼 缩略图预热完成: {ok} 张就绪, {fail} 张失败(浏览时回退原图)")
        except Exception:
            pass
        finally:
            _thumb_warm_state["running"] = False
            _thumb_warm_state["abort"] = False
            _thumb_warm_thread = None

    t = threading.Thread(target=_task, daemon=True)
    _thumb_warm_thread = t
    t.start()


def _thumbs_stats() -> dict:
    """返回缩略图缓存统计: 文件数 + 总字节数 + 是否正在预热。"""
    d = _thumb_dir()
    count = 0
    total = 0
    try:
        for p in d.iterdir():
            if p.is_file():
                count += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    try:
        total_n, orphans_n = _thumbs_orphan_count()
    except Exception:
        total_n, orphans_n = count, 0
    return {
        "count": count,
        "size_bytes": total,
        "size_mb": round(total / 1024 / 1024, 1),
        "orphans": orphans_n,
        "warming": _thumb_warm_state["running"],
        "done": _thumb_warm_state["done"],
    }


_valid_names_cache = {"ts": 0.0, "names": None}


def _valid_thumb_names() -> "set[str]":
    """当前所有图片对应的合法缩略图文件名 (常规键 md5(根|rel|mtime|size) + stable 键)。
    用于识别孤儿缓存: 图片被移动/改名/删除后, 旧键缩略图不再属于任何当前图片。
    带 10s 缓存: 预热期间每 5s 轮询 stats 时避免每轮全盘走一遍。"""
    now = time.time()
    if _valid_names_cache["names"] is not None \
            and now - _valid_names_cache["ts"] < 10:
        return _valid_names_cache["names"]
    valid: "set[str]" = set()
    for root_key, sub in (("img", _config["paths"]["img_subdir"]),
                          ("input", _config["paths"].get("input_subdir", "input"))):
        root = Path(paths.user_data(sub))
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in _IMG_EXT:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            valid.add(hashlib.md5(f"{root_key}|{rel}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest() + ".jpg")
            valid.add(f"stable_{hashlib.md5(f'stable|{root_key}|{rel}'.encode()).hexdigest()}.jpg")
    _valid_names_cache.update(ts=now, names=valid)
    return valid


def _thumbs_orphan_count() -> "tuple[int, int]":
    """(缓存文件总数, 孤儿数) — 孤儿 = 图被移动/改名/删除后的残留缓存。"""
    d = _thumb_dir()
    try:
        files = [f for f in d.iterdir() if f.is_file()]
    except Exception:
        return 0, 0
    if not files:
        return 0, 0
    try:
        valid = _valid_thumb_names()
    except Exception:
        return len(files), 0
    orphans = sum(1 for f in files if f.name not in valid)
    return len(files), orphans


def _clear_thumbs_orphans() -> int:
    """删除孤儿缩略图 (不含任何当前图片对应键的缓存)。返回删除数。"""
    d = _thumb_dir()
    try:
        valid = _valid_thumb_names()
    except Exception:
        return 0
    n = 0
    for p in d.iterdir():
        if p.is_file() and p.name not in valid:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    _valid_names_cache["names"] = None  # 删完使缓存失效, 下次重算
    return n


def _clear_thumbs_cache() -> int:
    """删除 thumbs/ 目录下全部缓存文件 (stable_* + 普通哈希名)。返回删除数。

    若预热正在进行, 先发 abort 信号让 warm 循环尽快退出,
    再等待 warm 线程真正结束 (running=False, 最多 5s), 避免清完又被写回。"""
    global _thumb_warm_thread
    # 1. 发中止信号: warm 循环顶部检查 abort, 立刻 return (不生成新缓存)
    if _thumb_warm_state["running"]:
        _thumb_warm_state["abort"] = True
        # 2. 等 warm 线程真正退出 (它 finally 把 running 设 False), 最多 5s
        for _ in range(50):
            if not _thumb_warm_state["running"]:
                break
            time.sleep(0.1)
    # 3. 此刻 warm 已退出或超时; 进入临界区删文件 (仍可能有一个 in-flight
    #    _get_thumb 写入, 但循环已停, 不会再有下一个, 删完即可)
    d = _thumb_dir()
    n = 0
    try:
        for p in d.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
    except Exception:
        pass
    # 重置预热状态: 清空后允许下次预热重新跑
    _thumb_warm_state["done"] = False
    _thumb_warm_state["abort"] = False
    _valid_names_cache["names"] = None
    return n


# ---------- dataclass → JSON ----------
def _art_to_dict(a) -> "dict | None":
    if a is None:
        return None
    author = getattr(a, "author", None)
    return {
        "artwork_id": a.artwork_id,
        "author_id": a.author_id,
        "title": a.title,
        "illust_type": getattr(a.illust_type, "value", a.illust_type),
        "page_count": a.page_count,
        "tags": list(a.tags) if a.tags else [],
        "image_urls": list(a.image_urls) if a.image_urls else [],
        "publish_date": a.publish_date,
        "is_r18": bool(a.is_r18),
        # pixiv 官方 AI 判定 (1=非AI, 2=AI, 0=未知): 查询页据此隐藏 AI 卡片 
        "ai_type": int(getattr(a, "ai_type", 0) or 0),
        "author_name": getattr(author, "name", None) or getattr(a, "author_name", None),
        "pixiv_url": f"https://www.pixiv.net/artworks/{a.artwork_id}",
    }


def _author_to_dict(a) -> "dict | None":
    if a is None:
        return None
    return {
        "author_id": a.author_id,
        "name": a.name,
        "avatar_url": a.avatar_url,
        "followers": a.followers,
        "following": a.following,
        "twitter": a.twitter,
        "bio": a.bio,
        "pixiv_url": a.pixiv_url or f"https://www.pixiv.net/users/{a.author_id}",
        # 名下作品总数  「共 N 件 · 换一批」用; None = 拿不到总数 (DOM 兜底)
        "works_total": getattr(a, "works_total", None),
    }


def _serialize_search(res: dict) -> dict:
    typ = res.get("type")
    if typ == "artwork":
        art = _art_to_dict(res.get("artwork"))
        auth = _author_to_dict(res.get("author"))
        # _execute_locked 把 author 作为独立键返回(没挂到 artwork.author slot),
        # 这里补齐 author_name, 否则作品卡作者名显示为空
        if art and auth and not art.get("author_name"):
            art["author_name"] = auth["name"]
        return {
            "type": "artwork",
            "artwork": art,
            "author": auth,
            "related_works": [_art_to_dict(w) for w in (res.get("related_works") or [])],
        }
    if typ == "author":
        return {
            "type": "author",
            "author": _author_to_dict(res.get("author")),
            "works": [_art_to_dict(w) for w in (res.get("works") or [])],
        }
    if typ == "keyword_works":
        return {
            "type": "keyword_works",
            "keyword": res.get("keyword", ""),
            "works": [_art_to_dict(w) for w in (res.get("works") or [])],
        }
    if typ == "keyword_authors":
        return {
            "type": "keyword_authors",
            "keyword": res.get("keyword", ""),
            "authors": [_author_to_dict(a) for a in (res.get("authors") or [])],
        }
    return {
        "type": "keyword",
        "keyword": res.get("keyword", ""),
        "related_authors": [_author_to_dict(a) for a in (res.get("related_authors") or [])],
        "works": [_art_to_dict(w) for w in (res.get("works") or [])],
    }


def _attach_local_status(db, res: dict) -> dict:
    """给搜索结果的作品补上本地状态。

    搜索结果来自 pixiv 实时抓取, 天然不含本地信息 —— 不补的话前端一律显示
    「📁 无本地文件」, 已入库的作品还会显示可点的「加入清单」, 点了才被告知
    「已下载完成」。补 local_path(首个本地文件相对路径) 与 dl_state
    (downloaded/in_queue/partial/none), 前端据此直接渲染
    「✓ 已入库 / ⏳ 已在下载列表 / ⬇ 补下缺失页」。
    """
    arts: list = []
    for key in ("artwork", "works", "related_works"):
        v = res.get(key)
        if isinstance(v, dict):
            arts.append(v)
        elif isinstance(v, list):
            arts.extend(x for x in v if isinstance(x, dict))
    aids = [str(a["artwork_id"]) for a in arts if a.get("artwork_id")]
    if not aids:
        return res
    try:
        paths = db.get_first_local_paths(aids)
        states = db.artwork_download_states(aids)
    except Exception:
        return res          # 本地状态查询失败不影响搜索本身
    for a in arts:
        aid = str(a.get("artwork_id") or "")
        if not aid:
            continue
        a["local_path"] = paths.get(aid)
        a["dl_state"] = states.get(aid, "none")
    return res


# =============================================================
# 查询页「没输入内容时显示一批推荐」(两个来源可切换)
# 🏆 排行榜 / ✨ 个性化推荐 —— 见 GET /api/search/feed
# =============================================================
_FEED_TTL = 600.0          # 推荐内容缓存 10 分钟: 来回切 tab 不该反复打 pixiv
_feed_cache: dict = {}     # kind -> (ts, list[ArtworkInfo])
_ai_fill_cache: dict = {}  # artwork_id -> ai_type (本进程内累积, 免反复联网问)
_feed_lock = threading.Lock()
_AI_MISS = object()        # 哨兵: "没问过" —— 跟"问过了, 结果是 0"必须能区分开


def _feed_ai_types(db, works, *, want: bool) -> int:
    """把推荐结果的 AI 判定补齐, 返回"本次实际联网问了几条"。

    **为什么非得补**: 排行榜接口 (ranking.php) 的返回体里**没有 aiType**(逐字段实测
    确认), 不补的话「隐藏 AI 作品」对排行榜完全失效 —— 而用户最在意的就是别看到 AI。
    个性化推荐 (discovery) 自带 aiType, 所以那个来源一条都不用问。

    三档来源, 一次比一次贵, 能用便宜的就别用贵的:
      ① ArtworkInfo.ai_type 已带值 (discovery)
      ② 本进程缓存 / 本地图库已回填的 ai_type
      ③ 联网问 pixiv —— **只有 want=True 才走** (前端开着「隐藏 AI 作品」时才值得花
         这个成本; 用户没开过滤时一个额外请求都不该发)

    ⚠ 判"问过没有"必须用哨兵而不是真值判断: pixiv 对老作品返回的就是 **0**,
    拿 `if not ai` 当"还没问过"会导致这批老作品**每次开页面都被重问一遍**
    (实测 discovery 那 10 条里就有 2 条是这样, 每次固定多花 2 个请求)。
    """
    from concurrent.futures import ThreadPoolExecutor

    pending: list = []
    for w in works:
        aid = str(w.artwork_id)
        if int(getattr(w, "ai_type", 0) or 0) in (1, 2):
            continue                                  # ① 接口自带
        hit = _ai_fill_cache.get(aid, _AI_MISS)
        if hit is _AI_MISS:                           # ② 本地图库已知
            try:
                md = db.get_artwork_metadata(aid)
            except Exception:
                md = None
            if md is not None and md.get("ai_type") is not None:
                hit = int(md["ai_type"])              # 0 也是"回填过了"的确定结论
                _ai_fill_cache[aid] = hit
        if hit is not _AI_MISS:
            w.ai_type = hit
        elif want:
            pending.append(w)                         # ③ 待联网
        # want=False 时**不写缓存**: 没问过就别假装知道, 免得挡住以后图库回填的结果
    if not pending:
        return 0

    sess = _pixiv_http_session()
    if sess is None:
        return 0            # 没登录/没 cookies → 保持「未知按非 AI」, 不报错也不空等

    from core.search_client import http_fetch_illust

    def _one(w):
        try:
            art = http_fetch_illust(sess, str(w.artwork_id), timeout=12.0)
            return w, int(getattr(art, "ai_type", 0) or 0)
        except Exception:
            return w, None          # 单条失败不影响其余 (删除/权限/网络都能容错)

    # 6 路并发: 约 24 条约 2s 问完 (实测 2022ms); 再高容易触发 pixiv 限速
    with ThreadPoolExecutor(max_workers=6) as ex:
        for w, at in ex.map(_one, pending):
            if at is None:
                continue            # 问失败不入缓存 → 下次还有机会问对
            w.ai_type = at
            _ai_fill_cache[str(w.artwork_id)] = at   # 含 0: 问过就是问过
    return len(pending)


_img_size_cache: "dict[str, tuple[int, int]]" = {}
_IMG_SIZE_CACHE_MAX = 8192

def _read_image_size(path) -> "tuple[int, int] | None":
    """读图片原始宽高 (仅解 header, 不解像素)。带 (mtime, size) 缓存,
    避免图库每页 60 张重复磁盘 IO。PIL 缺失/解码失败返回 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
    hit = _img_size_cache.get(key)
    if hit:
        return hit
    try:
        from PIL import Image
        with Image.open(path) as im:
            wh = (im.width, im.height)
    except Exception:
        return None
    if len(_img_size_cache) > _IMG_SIZE_CACHE_MAX:
        _img_size_cache.clear()
    _img_size_cache[key] = wh
    return wh


_ABS_PATH_CACHE: "dict[str, Path]" = {}
_ABS_PATH_CACHE_MAX = 40000


def _abs_path(img_root: Path, rel: str) -> Path:
    """(img_root / rel).resolve() 结果缓存: 图库一次响应要 resolve 200+ 次,
    Windows realpath(GetFinalPathName) 单次 ~0.2ms, 缓存后按 rel 命中零成本。
    rel 以页表 local_path 为 key, resolve 只是路径规范化, 内容不变则结果不变。"""
    v = _ABS_PATH_CACHE.get(rel)
    if v is None:
        v = (img_root / rel).resolve()
        if len(_ABS_PATH_CACHE) > _ABS_PATH_CACHE_MAX:
            _ABS_PATH_CACHE.clear()
        _ABS_PATH_CACHE[rel] = v
    return v


def _with_dl_state(db, items: list[dict]) -> list[dict]:
    """给图库条目补 dl_state (与搜索结果 _attach_local_status 同一套语义)。

    为什么非有不可 (「删一个作品, 整个列表的按钮都闪一下」):
    图库条目不带 dl_state 时, 卡片上的「加入下载」按钮一律按默认样式渲染, 再由前端
    decorateCartButtons 异步请求改成「✔ 已下载 / ⏳ 已在下载列表」—— 于是每次列表重画
    都会闪一遍(而删除会让列表连刷两次, 所以特别明显)。带上 dl_state 后首屏就是终态。
    """
    ids = sorted({str(x.get("artwork_id")) for x in items if x.get("artwork_id")})
    if not ids:
        return items
    try:
        states = db.artwork_download_states(ids)
    except Exception:
        return items                 # 状态查询失败不影响列表本身
    for x in items:
        x["dl_state"] = states.get(str(x.get("artwork_id")), "none")
    return items


def _lib_items(rows: list[dict], local_map: "dict[str, str] | None" = None,
               missing_set: "set[str] | None" = None,
               name_fallback: "dict[str, str] | None" = None,
               page_info: "dict[str, dict] | None" = None,
               db=None,
               page_list: "dict[str, list[dict]] | None" = None) -> list[dict]:
    """DB 行 → 前端网格需要的字段。

    - 缩略图「本地优先」: 作品有本地已下载文件就用 /api/local-img 直读本地,
      秒开且不受 pixiv 防盗链影响; 无本地文件才回退远端 thumb_url。
    - 附带 file_missing: 该作品在磁盘上已无任何文件(曾下载但被手动删了)。
    - 附带 local_path / local_dir: 首图绝对路径与所在文件夹(文件管理器式展示,
      前端可点击在资源管理器中定位)。
    - 附带 file_size / width / height: 首图文件字节数与原始宽高(卡片/大图显示用)。
      优先用 pages 表记录, 缺失时惰性读原图 header 并回填 DB (db 提供时)。
    - 附带 pages: 该作品库内页清单 (作品视图显示「原 N 页/库内哪几张」+ Lightbox 翻页)。
    - name_fallback: author_id→author_name 兜底(反搜归档作品 authors 表可能无名)。
    """
    img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
    local_map = local_map or {}
    missing_set = missing_set or set()
    page_info = page_info or {}
    pending_dim: "list[tuple[str, int, int, int]]" = []   # 缺尺寸 → 回填 DB
    out = []
    for d in rows:
        d = dict(d)
        aid = str(d["artwork_id"])
        d["pixiv_url"] = f"https://www.pixiv.net/artworks/{aid}"
        d["file_missing"] = aid in missing_set
        d["tags"] = d.get("tags") or []
        # 来源: DB 空值视为 pixiv; 其他来源 (ext_* 占位) 前端显示标记且不给 pixiv 链接
        d["source"] = d.get("source") or "pixiv"
        d["source_url"] = d.get("source_url") or ""
        # 「作品页已失效」这个客观事实来自 deleted_at 字段 (不是靠标题文案猜)
        d["deleted"] = bool(d.get("deleted_at"))
        if d["source"] != "pixiv" or aid.startswith("ext_"):
            d["pixiv_url"] = ""
        if not d.get("author_name") and name_fallback:
            d["author_name"] = name_fallback.get(str(d.get("author_id"))) or d.get("author_name")
        if not d.get("author_name"):
            # 空作者显示兜底: 外来源有账号 → 显示账号; 作品级无任何作者 → 「未知作者」
            # (保证未署名作品不会被当成"没有作者"而显示空白, 与状态筛选「未知作者」口径一致)
            if d.get("source_author"):
                d["author_name"] = str(d["source_author"])
            elif not d.get("author_id"):
                d["author_name"] = "未知作者"
        # 首图大小/尺寸 (无本地文件或读不到时保持 None, 前端不显示)
        d["file_size"] = None
        d["width"] = None
        d["height"] = None
        lp = local_map.get(aid)
        if lp and (img_root / lp).is_file():
            # 本地文件优先做缩略图(秒开, 不受防盗链影响); thumb=1 走 ~480px 缓存图, 不直出原图
            d["image_urls"] = [_local_img_url(lp, img_root)]
            d["has_local"] = True
            d["file_missing"] = False
            abs_p = _abs_path(img_root, lp)
            d["local_path"] = str(abs_p)
            d["local_dir"] = str(abs_p.parent)
            pi = page_info.get(aid) or {}
            d["file_size"] = pi.get("file_size_bytes")
            if pi.get("width") and pi.get("height"):
                d["width"], d["height"] = int(pi["width"]), int(pi["height"])
            else:
                wh = _read_image_size(abs_p)
                if wh:
                    d["width"], d["height"] = wh
                    if pi.get("page_num") is not None:
                        pending_dim.append((aid, int(pi["page_num"]), wh[0], wh[1]))
            if not d["file_size"]:
                try:
                    d["file_size"] = abs_p.stat().st_size
                except OSError:
                    pass
        elif lp:
            # 本地文件已被删(手动/意外), 但 DB 仍记录该路径: 缩略图仍走 /api/local-img,
            # 后端 api_local_img 在源文件缺失时回退 stable 稳定缩略图 → 能认出删掉的是哪张图;
            # 连稳定缩略图也没有时才 404 → 前端 onerror 灰「缺失」占位。
            d["image_urls"] = [_local_img_url(lp, None)]
            d["has_local"] = False
            d["file_missing"] = True
            abs_p = _abs_path(img_root, lp)
            d["local_path"] = str(abs_p)
            d["local_dir"] = str(abs_p.parent)
            pi = page_info.get(aid) or {}
            d["file_size"] = pi.get("file_size_bytes")
        else:
            # 从未下载过本地文件(只补了元数据/远端图) → 回退远端 thumb_url
            d["has_local"] = False
            d["local_path"] = None
            d["local_dir"] = None
            thumb = d.get("thumb_url") or ""
            d["image_urls"] = [thumb] if thumb else []
        # 库内页清单 (作品视图「原 N 页 / 库内哪几张」+ Lightbox 翻页)
        d["pages"] = [{
            "page_num": int(p["page_num"]),
            "url": _local_img_url(p["local_path"], img_root),
            "file_size": p.get("file_size_bytes"),
            "width": p.get("width"),
            "height": p.get("height"),
            "origin": p.get("origin") or "web",   # web=原网页 / merged=合并附加页
        } for p in (page_list or {}).get(aid, [])]
        out.append(d)
    if pending_dim and db is not None:
        try:
            db.fill_page_dimensions(pending_dim)
        except Exception:
            pass  # 回填失败不影响本次返回
    return out


def _lib_pages(rows: list[dict], name_fallback: "dict[str, str] | None" = None) -> list[dict]:
    """图片视图: 每「页」一张卡。行来自 store.list_images (pages 有 local_path 的页)。

    每行附带: url(本地缩略图) / file_missing(磁盘是否还在) / 所属作品/作者信息。
    前端双击该卡 → Lightbox 单张预览; 加清单/开文件夹按所属作品走。
    """
    img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
    out = []
    for d in rows:
        d = dict(d)
        aid = str(d["artwork_id"])
        rel = d.get("local_path") or ""
        abs_p = _abs_path(img_root, rel) if rel else None
        exists = bool(abs_p and abs_p.is_file())
        d["file_missing"] = not exists
        d["has_local"] = exists
        d["local_path"] = str(abs_p) if exists and abs_p else None
        d["local_dir"] = str(abs_p.parent) if exists and abs_p else None
        d["url"] = _local_img_url(rel, img_root) if rel else ""
        d["image_urls"] = [d["url"]] if d["url"] else []
        d["tags"] = []
        d["pixiv_url"] = f"https://www.pixiv.net/artworks/{aid}"
        d["source"] = d.get("source") or "pixiv"
        d["source_url"] = d.get("source_url") or ""
        d["deleted"] = bool(d.get("deleted_at"))   # 客观字段: 作品页已确认失效
        if d["source"] != "pixiv" or aid.startswith("ext_"):
            d["pixiv_url"] = ""
        if not d.get("author_name") and name_fallback:
            d["author_name"] = name_fallback.get(str(d.get("author_id"))) or d.get("author_name")
        if not d.get("author_name"):
            # 空作者显示兜底 (与 _lib_items 一致): 外来源有账号 → 账号; 无任何作者 → 未知作者
            if d.get("source_author"):
                d["author_name"] = str(d["source_author"])
            elif not d.get("author_id"):
                d["author_name"] = "未知作者"
        out.append(d)
    return out


# 活动日志里**一律打码**的字段名 (2026-09-27 补 api_key/auth: 「✨ 壁纸文案」的密钥会随
# POST /api/ai 进来, 而活动日志是明文落盘的 —— 密钥绝不能进日志文件)。
# 注: 别加裸的 "auth" —— 它是 "author_name" 的子串, 会把作者名一起打码掉。
_ACT_SECRET_KEYS = ("cookie", "password", "passwd", "token", "secret", "credential", "ck",
                    "api_key", "apikey", "authorization")
# 活动日志的进程级小状态 (启动线只记一次)
_ACT_STATE = {"startup_logged": False}


# 「其他来源 / 自定义作品」在图库里的落地目录 (与 guess_foreign_source / 反搜归档同一约定)。
# 单点定义: 导入时的"建自定义作品" 与 反搜收件箱里的"手动入库" 都用它, 免得两处各写一份漂移。
_CUSTOM_SRC_DIR = "#其他来源#"


def _act_short(obj, limit: int = 160) -> str:
    """把请求参数/请求体/响应压成一行短摘要 (给活动日志用): 长列表只留条数 + 头几个身份。

    为什么列表要留"头几个身份": 2026-09-18 排查"删图点了没反应"时, 日志只写了
    `items[1]` —— 看不出删的是哪个作品的哪一页, 关键线索缺失。现在写成
    `items[1]:125504541#1` (作品ID#页号), 一看就知道点的是哪张。
    """
    try:
        if not isinstance(obj, dict):
            return str(obj)[:limit]

        def _brief(x) -> str:
            if isinstance(x, dict):
                aid = x.get("artwork_id") or x.get("id") or x.get("artwork_ids") or "?"
                if "page_num" in x:
                    return f"{aid}#{x.get('page_num')}"
                return str(aid)
            return str(x).replace("\n", " ")[:24]

        parts = []
        for k, v in list(obj.items())[:12]:
            kk = str(k)
            if any(s in kk.lower() for s in _ACT_SECRET_KEYS):
                parts.append(f"{kk}=***")
            elif isinstance(v, (list, tuple)):
                head = ":".join(_brief(x) for x in list(v)[:3]) if v else ""
                parts.append(f"{kk}[{len(v)}]" + (f":{head}" if head else ""))
            elif isinstance(v, dict):
                parts.append(f"{kk}{{{len(v)}}}")
            else:
                parts.append(f"{kk}={str(v).replace(chr(10), ' ')[:60]}")
        return " ".join(parts)[:limit]
    except Exception:
        return ""


def _act_resp_summary(resp) -> str:
    """把 JSON 响应压成一行短摘要 —— 排查"点了没反应"时, 响应说了什么往往才是答案。

    安全前提: 只处理 application/json 且 direct_passthrough=False 的响应
    (后者是 send_file 这类流式响应, 去读它会把整个文件吸进内存)。读失败一律返回空。
    """
    try:
        if resp.direct_passthrough:
            return ""
        if str(getattr(resp, "mimetype", "")) != "application/json":
            return ""
        txt = resp.get_data(as_text=True)
        if not txt or len(txt) > 20000:
            return ""
        return _act_short(json.loads(txt), limit=110)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 📁 路径设置 (所有路径都要有设置入口,
#   以及数据/配置/缓存不占系统盘)
#
#   分层口径 (按「体积 × 增量」分):
#     · 图片   = 又大又会长 → 单独一项可指定盘 (通常指到非系统盘); 相对名 = 数据目录下的子目录
#     · 数据根 = 数据库(百 MB 量级, 增量温和) + 日志 + 缩略图, 默认跟着程序目录 (在 C 盘也不碍事),
#                真嫌大的用户可以整根搬走 (data_root_override)
#     · 临时   = 浏览器 profile / 上传缓冲, 既能自动清理又能手动一键清; 默认数据目录下的 temp
#     · 配置 (config/user.json) 与内置资源都很小且不怎么长, 只展示不可改
#   三处改动**都要重启**才生效 (utils/paths._paths_cfg() 有进程内缓存, SQLite 连接也已建好);
#   界面上只写一句「重启 Vixiq 后生效」, 不摆理由。
# ---------------------------------------------------------------------------
def _norm_dir_setting(raw, *, allow_rel: bool = False) -> "tuple[str, str]":
    """规整一项目录设置: 返回 (值, 错误)。空串 = 清除回默认。

    ``allow_rel=True`` (图片目录这种挂在数据根下的子目录) 额外接受**单段相对名** ——
    不接受 ``a/b`` 或 ``..``: 前者语义含糊, 后者能爬到数据根外面去。"""
    s = str(raw if raw is not None else "").strip().strip('"')
    if not s:
        return "", ""
    try:
        p = Path(s).expanduser()
    except Exception:
        return "", "路径不合法"
    if p.is_absolute():
        return str(p), ""
    if not allow_rel:
        return "", "只接受绝对路径，例：D:/vixiq-data"
    if any(ch in s for ch in '\\/:*?"<>|') or s in (".", ".."):
        return "", "相对路径只能填一个子目录名；想放别的盘请填绝对路径"
    return s, ""


def _resolve_dir(override: str, fallback: Path) -> Path:
    """与 utils.paths._abs 同口径: 空/非法 → fallback。"""
    if not override:
        return fallback
    try:
        p = Path(str(override)).expanduser()
        return p if p.is_absolute() else fallback
    except Exception:
        return fallback


def _size_mb(target: "str | Path", *, cap: int = 30000) -> "float | None":
    """占用 (MB); 路径不存在 → None。

    目录是**有上限地数**: 最多数 ``cap`` 个文件就收手 (数字只算个大概的「有没有涨起来」,
    但设置页每次切换都要算一遍, 不能因为一个几十万文件的 temp 目录卡在那儿)。"""
    try:
        p = Path(target)
        if p.is_file():
            return round(p.stat().st_size / 1048576, 1)
        if not p.is_dir():
            return None
        total, seen = 0, 0
        stack = [p]
        while stack and seen < cap:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            seen += 1
                            if seen >= cap:
                                break
                    except OSError:
                        continue
        return round(total / 1048576, 1)
    except Exception:
        return None


def _paths_snapshot() -> dict:
    """路径卡片的一份快照: ``saved``(用户填过的) + ``defaults``(默认落点) +
    ``effective``(实际落点) + ``sizes``(占用 MB)。

    effective **不用** utils.paths.data_root() 去算 —— 它带进程内缓存, 刚改完配置也返回旧值;
    这里按落盘后的配置直接推, 保证「保存后立刻看到新落点」。"""
    from utils import paths as _p
    cfg = load_config().get("paths", {}) or {}
    overd = str(cfg.get("data_root_override") or "")
    overt = str(cfg.get("temp_root_override") or "")
    img_cfg = str(cfg.get("img_subdir") or "").strip()
    # "img" 是出厂默认, 视同「没设置过」—— 界面上那句清除按钮只在真改过时才出现
    saved_img = "" if img_cfg in ("", "img") else img_cfg
    wr = _p.writable_root()
    rr = _p.resource_root()
    default_data = wr / "data"
    droot = _resolve_dir(overd, default_data)
    troot = _resolve_dir(overt, droot / "temp")
    # 图片目录: 相对名挂在数据根下, 绝对路径 (D:/…) 直接就是它 —— 与 paths.user_data 同一语义
    img = Path(img_cfg) if img_cfg and Path(img_cfg).is_absolute() else (droot / (img_cfg or "img"))
    db = droot / str(cfg.get("db_filename") or "pixiv.db")
    thumbs = droot / "thumbs"
    feat = droot / str(cfg.get("feature_db_subdir") or "feature_db")
    return {
        "saved": {"data_root_override": overd, "temp_root_override": overt,
                  "img_subdir": saved_img},
        "defaults": {"data_root": str(default_data),
                     "temp_root": str(default_data / "temp"), "img_subdir": "img"},
        "effective": {
            "data_root": str(droot),
            "temp_root": str(troot),
            "img": str(img),
            "db": str(db),
            "logs": str(droot / str(cfg.get("logs_subdir") or "logs")),
            "thumbs": str(thumbs),
            "feature_db": str(feat),
            "wallpaper_dir": str(cfg.get("wallpaper_dir") or ""),
            "config_file": str(_p.user_config_path()),
            "install_root": str(wr),
            "resource_root": str(rr),
        },
        "sizes": {"db": _size_mb(db), "thumbs": _size_mb(thumbs),
                  "temp": _size_mb(troot), "feature_db": _size_mb(feat),
                  "img": _size_mb(img)},
    }


# (键, 是否允许单段相对名) —— 只有图片目录允许相对, 它本来就常填 "img" 这种子目录名
_PATH_SETTING_KEYS = (("img_subdir", True),
                      ("data_root_override", False),
                      ("temp_root_override", False))


def _save_path_overrides(body: dict) -> "tuple[dict | None, str]":
    """校验并落盘路径设置。成功 → (新快照, "")；失败 → (None, 错误)。"""
    from utils.config import save_user_config
    upd = {}
    for k, allow_rel in _PATH_SETTING_KEYS:
        if k not in body:
            continue
        val, err = _norm_dir_setting(body.get(k), allow_rel=allow_rel)
        if err:
            return None, err
        if val and os.path.isabs(val):
            # 建一下并验可写: 存个建不出来的路径进去, 下次启动就是一堆报错, 不如此刻就说清
            try:
                Path(val).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return None, f"这个路径建不出来：{e}"
            if not os.access(val, os.W_OK):
                return None, "这个路径当前不可写"
        upd[k] = val
    if not upd:
        return None, "没有可识别的路径设置"
    try:
        save_user_config({"paths": upd})
    except Exception as e:
        return None, str(e)
    return _paths_snapshot(), ""


def create_app() -> Flask:
    app = Flask(__name__)
    static_dir = Path(__file__).resolve().parent / "static"

    # ---- 实时活动日志  ----------------------------------
    # 把「程序在做什么 + 用户点了什么」逐条打屏并落盘, 出事时有现场可回放。
    # 刻意**跳过**高频噪声路径: 图片代理/缩略图(一次首页就上百条)、心跳/再见、
    # SSE 长连接、静态资源、以及活动上报接口自身 —— 否则真事件会被洪水淹没。
    from utils import activity_log as _act
    _ACT_SKIP = ("/api/local-img", "/api/img", "/api/thumb", "/api/library/backup-img",
                 "/api/heartbeat", "/api/bye", "/api/focus", "/api/events",
                 "/api/activity", "/static/", "/favicon")

    def _act_skip(path: str) -> bool:
        return any(path.startswith(x) for x in _ACT_SKIP)

    # 启动落盘线程 + 记一条开机线 (之后每条日志都能和它对齐时间/版本)。
    # 只在**本进程第一次** create_app 时记: 冒烟会反复建 app, 不然日志开头全是重复的启动线。
    try:
        _act.start()
        if not _ACT_STATE["startup_logged"]:
            _ACT_STATE["startup_logged"] = True
            _act.log("SYS", "服务启动",
                     data_root=str(paths.data_root()),
                     img=_config["paths"].get("img_subdir", "img"),
                     safemode=bool((_config.get("safemode") or {}).get("enabled")),
                     log_dir=str(Path(paths.data_root()) / "logs" / "activity"))
    except Exception:
        pass

    @app.before_request
    def _act_timer_start():
        if not _act_skip(request.path):
            request.environ["_act_t0"] = time.perf_counter()

    @app.after_request
    def _act_log_request(resp):
        """每个 /api 请求记一条: 方法/路径/参数/请求体摘要/状态码/耗时。

        这是"程序在做什么"的流水 —— 与「用户点了什么」(前端上报) 合起来就能还原现场:
        用户点了哪个按钮 → 前端上报的 CLICK → 它触发的 HTTP → 结果与耗时。"""
        if _act_skip(request.path):
            return resp
        try:
            t0 = request.environ.get("_act_t0")
            cost = f"{(time.perf_counter() - t0) * 1000:.0f}ms" if t0 else ""
            q = request.args.to_dict(flat=True)
            body = request.get_json(silent=True)
            _act.log("HTTP", f"{request.method} {request.path}",
                     status=getattr(resp, "status_code", ""), cost=cost,
                     query=_act_short(q) if q else "",
                     body=_act_short(body) if isinstance(body, dict) else "",
                     resp=_act_resp_summary(resp))
        except Exception:
            pass
        return resp

    # ---- 前端行为上报 / 回读 ----------------------------------------------
    @app.route("/api/activity", methods=["POST"])
    def api_activity_report():
        """接收前端批量上报的用户行为 (点击/控件变化/切页), 落进活动日志。

        body: {"events": [{"k": "CLICK"|"CHANGE"|"NAV"|"KEY", "m": "描述", "f": {k:v}}]}
        设计要点: 必须**批量**(前端每 ~0.7s 或攒够一批才发), 且本接口自身不进日志,
        否则会形成"上报 → 记日志 → 再上报"的自激循环。
        """
        data = request.get_json(silent=True) or {}
        evs = data.get("events") or []
        n = 0
        for e in evs[:400]:
            if not isinstance(e, dict):
                continue
            f = e.get("f") if isinstance(e.get("f"), dict) else {}
            _act.log(str(e.get("k") or "UI"), str(e.get("m") or ""), **f)
            n += 1
        return jsonify({"status": "ok", "logged": n})

    @app.route("/api/activity/tail")
    def api_activity_tail():
        """回读最近若干行活动日志 (自检用; 也可在设置页看现场)。"""
        n = request.args.get("n", "200")
        day = request.args.get("day") or None
        try:
            return jsonify({"status": "ok", **_act.tail(int(n), day)})
        except (TypeError, ValueError):
            return jsonify({"status": "ok", **_act.tail(200)})

    @app.after_request
    def _mark_dirty_after_write(resp):
        """事件驱动的入口: 每次**成功的写请求**自动登记它影响的数据域 (见文件头 mark_dirty)。

        放在 after_request 而不是逐个接口里手动调用 —— 这样新加的写接口自动被覆盖,
        不会因为"忘了通知前端"再出现"改了但界面没变"。失败(4xx/5xx)不登记。"""
        try:
            if (request.path.startswith("/api/") and request.method != "GET"
                    and int(getattr(resp, "status_code", 500)) < 400):
                body = request.get_json(silent=True)
                scopes, ids = _scopes_for_request(
                    request.path, request.method,
                    body if isinstance(body, dict) else None,
                    request.args.to_dict(flat=True))
                if scopes and not (request.path.startswith("/api/library/sync")
                                   and not _sync_changed(resp)):
                    # 「同步/对账」是幂等写: 200 也可能一张没变 (切到图库页就会调一次),
                    # 看响应里的改动计数再决定要不要通知前端 —— 写后校验, 免得空转刷新
                    mark_dirty(*scopes, ids=ids)
        except Exception:
            pass
        return resp

    @app.errorhandler(Exception)
    def _handle_uncaught(e):
        """未捕获异常 → 记完整堆栈 + 给前端可读 JSON，而不是 Flask 默认的 HTML 500 页。

        为什么: 前端拿到裸 500 只能显示「HTTP 500」，用户和我都无从排查（exe 是窗口化
        进程，stderr 看不到）。落一份 data/logs/errors.log 让下次能直接看堆栈。
        """
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e      # 404/405 等按原样走
        import traceback as _tb
        try:
            p = Path(paths.data_root()) / "logs" / "errors.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"{request.method} {request.path}\n{_tb.format_exc()}\n")
        except Exception:
            pass
        first = (str(e).splitlines() or [""])[0]
        try:
            _reporter().error(f"接口异常 {request.path}: {type(e).__name__} {first}")
        except Exception:
            pass
        # 「database is locked」取证  写锁被别的任务长期占着时, 逐个打印
        # 存活线程的调用栈 —— 谁在持锁/卡在哪儿一目了然, 不必再靠猜。
        # (本次真凶就是这样定位的: 自检「作者改名同步」里的裸 UPDATE 从不 commit,
        #  循环里还有逐个作者的 HTTP 请求, 写锁被握数分钟。)
        if isinstance(e, sqlite3.OperationalError) and "locked" in str(e).lower():
            try:
                import sys as _sys
                import traceback as _tb2
                frames = _sys._current_frames()
                for t in threading.enumerate():
                    fr = frames.get(t.ident)
                    if fr is None:
                        continue
                    stack = "".join(_tb2.format_stack(fr, limit=8)).rstrip()
                    _reporter().error(
                        f"🔒 锁占用取证 · 线程「{t.name}」(daemon={t.daemon}):\n{stack}")
            except Exception:
                pass
            return jsonify({
                "status": "error", "path": request.path, "locked": True,
                "error": "数据库正被后台任务占用（正在整理/补全图库），请稍后重试；"
                         "本次操作没有生效"}), 409
        return jsonify({"status": "error", "path": request.path,
                        "error": f"{type(e).__name__}: {first}".strip()}), 500

    def _asset_ver() -> str:
        """app.js / style.css 的 mtime 指纹 —— 自动版本串的后半段。

        为什么自动加指纹 (2026-09-16 教训): 以前只靠人手改 index.html 的 `?v=xxx`,
        漏改/漏打包就会出现「代码改了但浏览器拿的是缓存旧文件」的假象 —— 一晚踩了两次
        (CSS 的 token 连续两轮忘了升, 页面仍在用旧样式)。挂上文件 mtime 后, 只要
        app.js/style.css 有改动, 版本串必然变化, 不再需要人工维护。
        """
        parts = []
        for name in ("app.js", "style.css"):
            try:
                parts.append(str(int((static_dir / name).stat().st_mtime)))
            except OSError:
                parts.append("0")
        return "-".join(parts)

    def _render_index_html() -> str:
        """把 index.html 里 app.js/style.css 的 `?v=` 注入为「手写标签-mtime指纹」。

        手写标签保留可读性 (看得出是哪一版), 指纹保证失效性 (改了必定换 URL)。
        """
        try:
            html = (static_dir / "index.html").read_text(encoding="utf-8")
        except OSError:
            return ""
        ver = _asset_ver()
        return re.sub(r'((?:app\.js|style\.css)\?v=)([\w.\-]+)',
                      lambda m: f"{m.group(1)}{m.group(2)}-{ver}", html)

    # ---------------- 页面 / 静态 ----------------
    @app.route("/")
    def index():
        # index.html 不缓存: 页面本身每次都重新拉取, 子资源靠 ?v= 版本号长缓存。
        # 防止 exe 重启/发版后浏览器还拿旧页面(旧 app.js), 造成"改了没生效"的假象。
        html = _render_index_html()
        resp = (Response(html, mimetype="text/html") if html
                else send_from_directory(str(static_dir), "index.html"))
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(str(static_dir), filename)

    @app.route("/favicon.ico")
    def favicon_ico():
        """浏览器默认请求根路径 favicon, 重定向到静态资源。"""
        return send_from_directory(str(static_dir), "favicon.ico",
                                   mimetype="image/x-icon", max_age=86400)

    # ---------------- SSE 进度流 ----------------
    @app.route("/api/events")
    def sse_events():
        def gen():
            q = _hub.subscribe()
            try:
                yield "retry: 1000\n\n"
                while True:
                    try:
                        ev = q.get(timeout=15)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                _hub.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.route("/api/focus", methods=["POST"])
    def api_focus():
        """桌面双击 exe 时单实例进程调用: 向所有已打开标签页广播 app_focus,
        让已有窗口跳到前台 (不重复启动服务、也不新开标签页)。
        返回 clients: 当前活跃 SSE 连接数, 调用方据此决定只激活窗口、
        还是没有窗口需要直接打开浏览器。"""
        try:
            _hub.publish({"type": "app_focus"})
        except Exception:
            pass
        return jsonify({"ok": True, "clients": _hub.client_count()})

    @app.route("/api/health")
    def api_health():
        return jsonify({"ok": True, "app": "vixiq",
                        "cookies_exist": _cookies_path().exists()})

    @app.route("/api/diag/liveness")
    def api_diag_liveness():
        """诊断「浏览器是否还活着」的原始信号 (排障用, 只读)。

        自动退出服务依赖这些信号, 出问题时 (处处 failed to fetch) 用它确认
        服务当时看到的到底是什么: clients>0 或心跳新鲜 → 本就不该退出。
        - clients: 当前 SSE (EventSource) 连接数
        - hb_ago : 距上次浏览器心跳的秒数 (None=本实例从未收到过心跳)
        - bye_ago: 距上次页面卸载告别信号的秒数 (None=没收到过)"""
        now = time.time()
        hb = _heartbeat
        return jsonify({
            "pid": os.getpid(),
            "clients": _hub.client_count(),
            "ever_heartbeat": bool(hb["ever"]),
            "hb_ago": round(now - hb["last"], 1) if hb["ever"] else None,
            "bye_ago": round(now - hb["bye_at"], 1) if hb["bye_at"] else None,
        })

    @app.route("/api/dirty")
    def api_dirty():
        """事件驱动刷新的**补偿通道**: 返回各数据域当前版本与待刷新 id。

        正常路径是 SSE 推送 {"type":"dirty"}; 这个接口用于 ① 页面刚打开时对齐基线
        ② SSE 断线/被浏览器节流时的兜底轮询 (since=上次收到的 rev, 只回更新的域)。
        since=0 返回全部域当前状态。"""
        try:
            since = int(request.args.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        snap = _dirty_snapshot(since)
        return jsonify({"status": "ok", "since": since, **snap})

    @app.route("/api/heartbeat")
    def api_heartbeat():
        """浏览器页面心跳: 页面开着时前端每 10s ping 一次。
        任何心跳都表示「还有页面活着」→ 取消 bye 快速退出倒计时。"""
        _heartbeat["last"] = time.time()
        _heartbeat["ever"] = True
        _heartbeat["bye_at"] = 0.0
        return jsonify({"ok": True})

    @app.route("/api/bye", methods=["POST"])
    def api_bye():
        """页面关闭信号: 前端 pagehide 时 sendBeacon 发出 (刷新/关标签/关浏览器都会触发)。
        不立即退出——刷新也会触发 pagehide, 由 main.py 守护线程等 12s,
        期间若有任何心跳 (刷新后的新页面/其他标签) 就取消退出。"""
        _heartbeat["bye_at"] = time.time()
        return jsonify({"ok": True})

    # ---------------- 搜索 ----------------
    @app.route("/api/search")
    def api_search():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify({"status": "error", "text": "搜索内容为空"}), 400
        scope = (request.args.get("scope", "") or "").strip()  # 'artwork' | 'author' | ''=auto
        # offset  作者结果里作品列表的「换一批」起点 (默认 0 = 最新一批)
        try:
            offset = max(0, int(request.args.get("offset", 0) or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            sc = _get_search_client()
            pq = parse_input(q)
            if scope in ('artwork', 'author'):
                res = _serialize_search(sc.execute_scoped(pq, scope, offset=offset))
            else:
                res = _serialize_search(sc.execute(pq))
            # 补本地状态: 前端据此显示「已入库/已在下载列表/补下缺失页」与本地路径
            with _db() as db:
                res = _attach_local_status(db, res)
            res["status"] = "ok"
            res["parsed"] = q
            res["works_offset"] = offset      # 作者作品列表本批起点 (前端算页数/按钮态)
            return jsonify(res)
        except (ArtworkNotFoundError, AuthorNotFoundError) as e:
            return jsonify({"status": "not_found", "text": str(e)}), 404
        except Exception as e:
            # 共享 driver 会话可能已损坏(崩溃/超时), 丢弃重建, 下次查询自愈
            _reset_search_client()
            tb = str(e).splitlines()[0] if str(e) else repr(e)
            return jsonify({"status": "error",
                            "text": f"查询出错: {tb}\n(若提示登录问题, 请到设置页重新登录)"}), 500

    @app.route("/api/artwork/<artwork_id>/pages")
    def api_artwork_pages(artwork_id: str):
        """某作品的「全部页 URL + 库内页状态」—— 查询页大图预览 / 按页下载用。

        只读接口, 不起浏览器。错误映射 (前端要能区分「没登录」与「请求失败」):
          未登录 → **200 + {"status":"need_login"}** (不要 401);
          作品不存在 → 404; 登录态失效/取页失败 → 502。
        `low` = urls.regular (弹窗默认先显示它), `high` = urls.original (点「显示原图」才用)。
        前端**一律走既有 /api/img?url= 代理**取图 —— i.pximg.net 不带 Referer 会 403。
        合并库内页状态: 已入库 → 该页 status 取 pages 表 (PSt 常量, 缺行 = 'none'); 未入库 → 全 'none'。"""
        aid = str(artwork_id or "").strip()
        if not aid.isdigit():
            return jsonify({"status": "error", "text": "作品 ID 无效"}), 400
        # 用模块级别名, 别用同名局部函数 (见 _pixiv_http_session_module 处的说明)
        sess = _pixiv_http_session_module()
        if sess is None:
            return jsonify({"status": "need_login",
                            "text": "需要先在设置页登录 pixiv 才能查看原图 / 下载"}), 200
        from core.search_client import http_fetch_illust, http_fetch_illust_pages
        try:
            info = http_fetch_illust(sess, aid)
            raw_pages = http_fetch_illust_pages(sess, aid)
        except ArtworkNotFoundError as e:
            return jsonify({"status": "not_found", "text": str(e)}), 404
        except PermissionError as e:
            return jsonify({"status": "error", "text": str(e)}), 502
        except Exception as e:
            tb = str(e).splitlines()[0] if str(e) else repr(e)
            return jsonify({"status": "error", "text": f"获取页列表失败: {tb}"}), 502

        in_lib = False
        local: dict = {}
        with _db() as db:
            meta = db.get_artwork_metadata(aid)
            in_lib = bool(meta and meta.get("trashed_at") is None)
            for r in db.conn.execute(
                    "SELECT page_num, status FROM pages "
                    "WHERE artwork_id=? AND trashed_at IS NULL", (aid,)).fetchall():
                local[int(r[0])] = int(r[1] or 0)

        total = int(getattr(info, "page_count", 0) or len(raw_pages))
        pages = []
        for p in raw_pages:
            pn = int(p["page_num"])
            pages.append({
                "page_num": pn,
                "low": p["regular"],
                "high": p["original"],
                # small/thumb 给「缩略图多选条」用 (整条几十格全用 regular 太费流量)
                "small": p["small"],
                "thumb": p["thumb"],
                "width": p["width"],
                "height": p["height"],
                "status": (local.get(pn, "none") if in_lib else "none"),
                "is_extra": bool(total > 0 and pn >= total),
            })
        author = getattr(info, "author", None)
        return jsonify({
            "status": "ok",
            "in_library": in_lib,
            "artwork": {
                "artwork_id": aid,
                "title": getattr(info, "title", "") or "",
                "author_id": getattr(info, "author_id", None),
                "author_name": (getattr(author, "name", None)
                                or getattr(info, "author_name", None) or ""),
                "page_count": total,
                "is_r18": bool(getattr(info, "is_r18", False)),
                "illust_type": str(getattr(getattr(info, "illust_type", ""), "value",
                                          getattr(info, "illust_type", "")) or ""),
            },
            "pages": pages,
        })

    @app.route("/api/search/feed")
    def api_search_feed():
        """查询页没输入内容时显示的推荐批次 (, 两个来源可切换)。

        kind=ranking   🏆 pixiv 日榜 (内容确定, 不依赖个性化)
        kind=discovery ✨ pixiv 个性化推荐 (按浏览/收藏记录算, 需登录)

        hide_ai=1 表示前端开着「隐藏 AI 作品」。**它只影响"要不要为排行榜联网补
        aiType"这一个成本决策** —— 响应永远带 ai_type 字段, 由前端统一过滤,
        这样用户拨开关能就地重渲染, 不必再请求一次。
        """
        from core.search_client import FEED_KINDS
        kind = (request.args.get("kind", "ranking") or "ranking").strip()
        if kind not in FEED_KINDS:
            kind = "ranking"
        hide_ai = (request.args.get("hide_ai", "0") or "0") != "0"

        now = time.time()
        with _feed_lock:
            hit = _feed_cache.get(kind)
        if hit and now - hit[0] < _FEED_TTL:
            works = list(hit[1])
        else:
            try:
                sc = _get_search_client()
                works = (sc.search_discovery() if kind == "discovery"
                         else sc.search_ranking())
            except Exception as e:
                # 共享 driver 可能已损坏 → 丢弃重建, 与 /api/search 同一套自愈
                _reset_search_client()
                tb = str(e).splitlines()[0] if str(e) else repr(e)
                return jsonify({"status": "error",
                                "text": f"取推荐失败: {tb}\n"
                                        f"(「为你推荐」需要登录；可切回「排行榜」)"}), 502
            if not works:
                return jsonify({"status": "error", "text": "没取到推荐内容, 稍后再试"}), 502
            # 缓存的就是这批 ArtworkInfo 对象本身 —— 后面补到的 aiType 会留在对象上,
            # 下次开页面直接命中, 不必重新联网 (这是刻意的, 不是副作用)
            with _feed_lock:
                _feed_cache[kind] = (now, list(works))
            works = list(works)

        filled = 0
        try:
            with _db() as db:
                filled = _feed_ai_types(db, works, want=hide_ai)
                res = {"type": "feed", "kind": kind,
                       "works": [_art_to_dict(w) for w in works]}
                res = _attach_local_status(db, res)
        except Exception as e:
            # AI 判定/本地状态补不上不该让整个推荐挂掉 —— 照旧把作品给出去
            tb = str(e).splitlines()[0] if str(e) else repr(e)
            res = {"type": "feed", "kind": kind,
                   "works": [_art_to_dict(w) for w in works]}
            res["warn"] = tb
        res["status"] = "ok"
        res["ai_filled"] = filled        # 本次为 AI 判定联网问了几条 (0 = 没花成本)
        return jsonify(res)

    # ---------------- 登录管理 ----------------
    @app.route("/api/auth/status")
    def api_auth_status():
        # 老版本升级后有 cookies 无 account.json: 后台补一次账号信息 (头像/昵称)
        _maybe_probe_account_background()
        return jsonify({"cookies_exist": _cookies_path().exists(),
                        "cookies_file": str(_cookies_path()),
                        "account": _load_account()})

    @app.route("/api/auth/avatar")
    def api_auth_avatar():
        """头像代理: pximg.net 有防盗链 (必须带 pixiv Referer), 浏览器直链会 403,
        所以由后端带 Referer 拉取并缓存后返回。"""
        acc = _load_account() or {}
        url = acc.get("avatar") or ""
        if not url:
            return ("", 204)
        try:
            req = Request(url, headers={
                "Referer": "https://www.pixiv.net/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            with urlopen(req, timeout=15) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
            return Response(data, content_type=ctype,
                            headers={"Cache-Control": "max-age=86400"})
        except Exception:
            return ("", 204)

    @app.route("/api/auth/login", methods=["POST"])
    def api_auth_login():
        """打开可见浏览器到 Pixiv 登录页; 后端轮询检测登录成功后【自动保存 Cookies】,
        用户无需再点任何保存按钮。状态走 GET /api/auth/login-status 轮询。"""
        global _manual_driver
        with _login_lock:
            if _login_state["state"] == "waiting":
                return jsonify({"status": "already_open"})
        # 关掉上一轮残留的登录浏览器
        with _manual_lock:
            old = _manual_driver
            _manual_driver = None
        if old is not None:
            try:
                old.quit()
            except Exception:
                pass

        def _open_and_watch():
            global _manual_driver
            from core.auth import save_cookies
            from utils.browser import make_browser, fetch_pixiv_user
            rep = _reporter()
            try:
                driver, _ = make_browser(headless=False, reporter=rep, config=_config)
                driver.get("https://accounts.pixiv.net/login?lang=zh")
                with _manual_lock:
                    _manual_driver = driver
                with _login_lock:
                    _login_state.update(state="waiting", account=None,
                                        msg="浏览器已打开, 请在弹出的窗口中登录")
                rep.log("登录浏览器已打开, 登录成功后 Cookies 会自动保存…")

                # 轮询: 仅当浏览器落在 pixiv.net 域才探测 (登录页在 accounts.pixiv.co.jp,
                # 主动导航会打断用户输入); 登录成功跳回 www.pixiv.net 后即可读到账号
                deadline = time.time() + 600   # 最长等 10 分钟
                while time.time() < deadline:
                    time.sleep(2.5)
                    try:
                        url = driver.current_url or ""
                    except Exception:
                        # 窗口被用户手动关掉
                        with _login_lock:
                            _login_state.update(state="idle", msg="登录窗口已关闭")
                        with _manual_lock:
                            _manual_driver = None
                        return
                    if "pixiv.net" not in url:
                        continue
                    info = None
                    try:
                        info = fetch_pixiv_user(driver)   # 已在 pixiv.net 域, 不会再导航
                    except Exception:
                        info = None
                    if info and info.get("user_id"):
                        ok = save_cookies(driver, _cookies_path(), rep)
                        if ok:
                            _save_account(info)
                            _reset_search_client()
                            with _login_lock:
                                _login_state.update(state="success", account=info,
                                                    msg=f"登录成功, 欢迎 {info.get('name') or ''}")
                            rep.log(f"登录成功, Cookies 已自动保存 ✓ ({info.get('name')})")
                            return
                with _login_lock:
                    _login_state.update(state="idle", msg="登录等待超时, 已取消 (可重新点登录)")
            except Exception as e:
                rep.error(f"打开登录浏览器失败: {str(e).splitlines()[0]}")
                with _login_lock:
                    _login_state.update(state="idle", msg=f"打开失败: {str(e).splitlines()[0]}")

        threading.Thread(target=_open_and_watch, daemon=True).start()
        return jsonify({"status": "opening"})

    @app.route("/api/auth/login-status")
    def api_auth_login_status():
        with _login_lock:
            st = dict(_login_state)
        # success 是一次性事件: 取过之后回 idle(账号信息已持久化, status 里仍可查)
        if st["state"] == "success":
            with _login_lock:
                _login_state.update(state="idle", msg="")
        return jsonify(st)

    @app.route("/api/auth/cancel-login", methods=["POST"])
    def api_auth_cancel_login():
        """用户放弃登录: 关掉登录浏览器窗口。"""
        global _manual_driver
        with _manual_lock:
            driver, _manual_driver = _manual_driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        with _login_lock:
            _login_state.update(state="idle", msg="")
        return jsonify({"status": "ok"})

    @app.route("/api/auth/show-browser", methods=["POST"])
    def api_auth_show_browser():
        """把共享查询浏览器切成【可见窗口】并打开 Pixiv (默认无头, 想围观时用)。
        Cookies 自动注入, 看到的就是登录后的真实状态。"""
        def _task():
            try:
                sc = _get_search_client(headless=False)
                sc.ensure_login()
                sc.driver.get("https://www.pixiv.net/")
                _reporter().log("已显示浏览器窗口 (登录态自动注入)")
            except Exception as e:
                _reporter().error(f"显示浏览器失败: {str(e).splitlines()[0]}")
        threading.Thread(target=_task, daemon=True).start()
        return jsonify({"status": "opening"})

    @app.route("/api/auth/hide-browser", methods=["POST"])
    def api_auth_hide_browser():
        """关闭可见浏览器窗口, 恢复无头模式 (下次查询自动重建无头浏览器)。"""
        _reset_search_client()
        return jsonify({"status": "ok"})

    @app.route("/api/auth/reset-client", methods=["POST"])
    def api_auth_reset():
        _reset_search_client()
        return jsonify({"status": "ok"})

    @app.route("/api/auth/logout", methods=["POST"])
    def api_auth_logout():
        """退出登录: 删除本地 Cookies + 账号信息, 重置共享查询浏览器。
        切换账号 = 退出后点【登录 Pixiv】, 在弹出的浏览器里登录另一个号即可。"""
        removed = False
        try:
            cf = Path(_cookies_path())
            if cf.is_file():
                cf.unlink()
                removed = True
        except Exception:
            pass
        _clear_account()
        _reset_search_client()   # 无头共享浏览器可能缓存了登录 cookie, 一并重置
        return jsonify({"status": "ok", "cookies_removed": removed})

    # ---------------- 缩略图缓存 ----------------
    @app.route("/api/thumbs/stats", methods=["GET"])
    def api_thumbs_stats():
        """返回 thumbs/ 缓存统计 (文件数/大小/是否预热中)。设置页卡片用。"""
        return jsonify(_thumbs_stats())

    @app.route("/api/thumbs/warm", methods=["POST"])
    def api_thumbs_warm():
        """立即重新预热全部缩略图 (force=True, 跳过 done 标志)。
        新入库图片后或清空缓存后, 点此按钮批量重新生成。"""
        if _thumb_warm_state["running"]:
            return jsonify({"status": "already_running"})
        warm_thumbs_background(force=True)
        return jsonify({"status": "started"})

    @app.route("/api/thumbs/clear", methods=["POST"])
    def api_thumbs_clear():
        """清空缩略图缓存目录 (thumbs/ 下所有文件)。下次浏览原图时自动重建。
        不会删除原图, 仅清缓存。"""
        n = _clear_thumbs_cache()
        _reporter().log(f"🖼 缩略图缓存已清空: 删除 {n} 个文件")
        return jsonify({"status": "ok", "deleted": n})

    @app.route("/api/thumbs/clear-orphans", methods=["POST"])
    def api_thumbs_clear_orphans():
        """只删除孤儿缩略图 (图片被移动/改名/删除后的残留缓存), 不动当前图片的缓存。"""
        n = _clear_thumbs_orphans()
        if n:
            _reporter().log(f"🧹 缩略图孤儿缓存已清理: 删除 {n} 个")
        return jsonify({"status": "ok", "deleted": n})

    # ---------------- 程序退出 ----------------
    @app.route("/api/app/quit", methods=["POST"])
    def api_app_quit():
        """设置页「退出程序」按钮: 完全终止进程并释放所有占用端口。

        流程: 先立即返回 JSON 让前端收到响应 → 独立线程延迟 0.8s (等响应发出
        + 前端 toast) → 停全部后台线程 → 关 Selenium/浏览器驱动 → os._exit(0)
        硬退出 (OS 立刻回收全部 socket/端口; atexit 不执行所以浏览器驱动要显式
        关掉, 见 _close_all_browsers)。"""
        global _quitting
        if _quitting:
            return jsonify({"status": "already_quitting"})
        _quitting = True
        _reporter().log("⏻ 收到退出指令, 正在停止服务并释放端口…")
        # 广播退出事件: 所有已打开的标签页 (SSE /api/events 连接) 收到后各自弹遮罩并
        # 尝试 window.close() —— 多窗口/多标签场景下一次退出全部关闭, 而不是只关点击的那个
        try:
            _hub.publish({"type": "app_quit"})
        except Exception:
            pass

        def _do_quit():
            time.sleep(0.8)  # 等 HTTP 响应发回浏览器
            print("🧹 正在清理...", flush=True)
            try:
                # 1. 停后台循环: 元数据补全 / 缩略图预热
                _cancel_event.set()
                try:
                    _thumb_warm_state["abort"] = True
                except Exception:
                    pass
                # 2. 关 Selenium 共享/手动登录浏览器驱动 (os._exit 不触发 atexit)
                _close_all_browsers()
            finally:
                # 3. 硬退出: OS 立即回收端口 7790/78xx 与全部子进程句柄
                os._exit(0)

        threading.Thread(target=_do_quit, daemon=True).start()
        return jsonify({"status": "quitting"})

    # ---------------- 购物车 ----------------
    @app.route("/api/cart", methods=["GET"])
    def api_cart_list():
        with _db() as db:
            items = db.queue_list()
            thumbs = db.artwork_thumbs(it.get("artwork_id") for it in items)
        for it in items:
            it["thumb_url"] = thumbs.get(str(it.get("artwork_id"))) or ""
        return jsonify({"items": items})

    @app.route("/api/cart/retry-failed", methods=["POST"])
    def api_cart_retry_failed():
        """把所有失败任务重置为待下载并立即重跑, 返回被重置数量。"""
        with _db() as db:
            n = db.queue_reset_failed()
        if n:
            _launch_run_cart()
        return jsonify({"status": "ok", "resetted": n})

    @app.route("/api/cart/<int:item_id>/retry", methods=["POST"])
    def api_cart_retry(item_id: int):
        """单条重试: 该条重置为 pending 并只重跑它自己。"""
        with _db() as db:
            db.queue_update_status(item_id, "pending")
            # 确认存在, 否则报错
            exists = any(int(it["id"]) == item_id for it in db.queue_list())
        if not exists:
            return jsonify({"status": "error", "text": "任务不存在"}), 404
        _launch_run_cart([item_id])
        return jsonify({"status": "ok", "item_id": item_id})

    def _cart_add_item(db, data: dict) -> dict:
        """加一条购物车项: 入队 + 顺带把搜索已知的元数据写进 artworks/authors 表。
        供单个 /api/cart 与批量 /api/cart/batch-add 共用。

        pixez 式去重 , 返回 {reason, item_id?}:
          - 'in_queue'            : 同目标已有 pending/running 队列项 → 不重复插入;
          - 'already_downloaded'  : 作品已 DONE → 不开任务;
          - 'added'               : 正常入队。
        关键保护: 已在库且「已全部下载完成」(DONE) 的作品, 绝不重置状态/不覆盖元数据
        —— 否则 DONE 被降级成 NOT_YET, 状态显示错乱, 下载引擎也失去「整作品已下完」
        跳过能力 (2026-09-10 审计发现, 已记录)。

        force=True = 显式「重下修复」(2026-09-16 方案): 允许已 DONE 的作品入队并
        强制 overwrite 策略 (下载器把被覆盖的旧文件备份成 .bak)。**只有用户点了
        「♻ 重下修复」才会带 force** —— 「加入下载」(单个/批量) 永不传, 语义永远是
        "有则跳过、缺页补全", 所以批量场景不存在"该覆盖还是跳过"的猜测。"""
        force = bool(data.get("force"))
        item_type = data.get("item_type") or "artwork"
        aid = data.get("artwork_id")
        author_id = data.get("author_id")

        # ① 未完成队列里已有同目标 → 拒绝重复入队 (force 同理: 已在队列就等它跑)
        if db.queue_find_active(
                item_type,
                artwork_id=aid if item_type == "artwork" else None,
                author_id=author_id if item_type == "author_all" else None):
            return {"reason": "in_queue"}

        # ② 作品已下载完成 → 不开新任务 (partial/NOT_YET 允许入队补缺失页);
        #    显式强制重下 (force) 例外 —— 那正是"用官方源覆盖本地"的入口
        if item_type == "artwork" and aid and not force:
            info = db.get_work_info(str(aid))
            if info is not None and info.done:
                return {"reason": "already_downloaded"}

        item_id = db.queue_add(
            item_type=item_type,
            title=data.get("title"),
            author_id=author_id,
            author_name=data.get("author_name"),
            artwork_id=aid,
            page_count=data.get("page_count"),
            strategy=(DownloadStrategy.OVERWRITE.value if force
                      else (data.get("strategy") or DownloadStrategy.ONLY_NEW.value)),
            source_note=data.get("source_note") or "Web 加入清单",
            # 按页下载  只有作品项才认 pages; 不传 = 整部 (向后兼容)。
            # 注: page_count 始终是**远端总页数** (前端卡片带着), 绝不能改成选中页数 ——
            # 否则下面的 change_work_status(total_pages=...) 会把整部作品判成已下完。
            pages=(data.get("pages") if item_type == "artwork" else None),
        )
        if aid:
            if author_id and data.get("author_name"):
                db.upsert_author(str(author_id), str(data["author_name"]))
            raw_title = str(data.get("title") or "")
            m = re.match(r"^#\d+\s+(.*)$", raw_title)
            clean_title = m.group(1) if m else (raw_title or None)
            _info = db.get_artwork_metadata(str(aid))
            if _info is None or _info.get("status") != WSt.DONE:
                db.change_work_status(
                    str(aid), WSt.NOT_YET,
                    title=clean_title,
                    author_id=str(author_id) if author_id else None,
                    total_pages=data.get("page_count") or "hold",
                )
        return {"reason": "added", "item_id": item_id}

    @app.route("/api/cart", methods=["POST"])
    def api_cart_add():
        data = request.get_json(silent=True) or {}
        with _db() as db:
            res = _cart_add_item(db, data)
        # 已在队列/已下载: 409 语义 + reason, 前端据此给明确提示且不启动下载
        if res["reason"] != "added":
            return jsonify({"status": "ignored", **res}), 409
        return jsonify({"status": "ok", **res})

    @app.route("/api/cart/batch-add", methods=["POST"])
    def api_cart_batch_add():
        """图库批量加入清单: items=[{artwork_id,title,author_id,author_name,page_count},...]。
        前端已按作品去重 (图片视图选中多张自动归并到所属作品)。缺 ID 跳过计数;
        已在队列/已下载由 _cart_add_item 拦截并分类计数 (不重复开任务)。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or []
        if not isinstance(items, list):
            return jsonify({"status": "error", "error": "items 必须是数组"}), 400
        counts = {"added": 0, "in_queue": 0, "already_downloaded": 0, "invalid": 0}
        with _db() as db:
            for it in items:
                if not isinstance(it, dict) or not it.get("artwork_id"):
                    counts["invalid"] += 1
                    continue
                res = _cart_add_item(db, {
                    "artwork_id": str(it["artwork_id"]),
                    "title": it.get("title"),
                    "author_id": it.get("author_id"),
                    "author_name": it.get("author_name"),
                    "page_count": it.get("page_count"),
                })
                counts[res["reason"]] += 1
        return jsonify({"status": "ok", **counts,
                        "skipped": counts["in_queue"] + counts["already_downloaded"]
                                 + counts["invalid"]})

    @app.route("/api/library/download-status", methods=["GET"])
    def api_library_download_status():
        """批量查作品/作者下载态, 供搜索结果按钮 pixez 式回显:
        ?ids=<作品ID逗号分隔>&author_ids=<作者UID逗号分隔>"""
        ids = [x for x in str(request.args.get("ids") or "").split(",") if x.strip()]
        author_ids = [x for x in str(request.args.get("author_ids") or "").split(",")
                      if x.strip()]
        with _db() as db:
            return jsonify({
                "works": db.artwork_download_states(ids),
                "authors": db.author_queue_state(author_ids),
            })

    @app.route("/api/cart/<int:item_id>", methods=["DELETE"])
    def api_cart_remove(item_id: int):
        with _db() as db:
            db.queue_remove(item_id)
        return jsonify({"status": "ok"})

    @app.route("/api/cart/clear", methods=["POST"])
    def api_cart_clear():
        """清空下载队列。scope 与「下载任务」页四个筛选栏一一对应:
        all(全部, 默认) / running(未完成) / done(已完成) / failed(失败)。

        「清空一条进行中的任务」= 撤销该次下载
。所以清掉的行**真的不会被下载**, 兑现方式在引擎侧:
          - 每项开跑前先 queue_claim_item 认领, 认领不到 (行已被清掉) → 整项跳过;
          - 正在跑的那一项, 引擎注册的 abort 回调逐页回查 queue_item_alive,
            行没了 → 当前作品立刻中止 (已下好的页保留, 图库里如实显示成「缺页」)。
        本接口自己不需要动 _cancel_event —— 那会把元数据补全等无关后台循环也停掉。
        """
        data = request.get_json(silent=True) or {}
        scope = str(data.get("scope") or request.args.get("scope") or "all")
        with _db() as db:
            n = db.queue_clear_scope(scope)
        return jsonify({"status": "ok", "removed": n, "scope": scope})

    @app.route("/api/cart/download", methods=["POST"])
    def api_cart_download():
        data = request.get_json(silent=True) or {}
        try:
            strategy = DownloadStrategy(data.get("strategy") or DownloadStrategy.ONLY_NEW.value)
        except Exception:
            strategy = DownloadStrategy.ONLY_NEW
        _launch_run_cart(strategy=strategy)
        return jsonify({"status": "started"})

    def _backup_work_files(db, img_root: Path, aid: str) -> int:
        """重下修复前, 把该作品当前已落盘的每张图复制一份 `.bak` 再开下载。

        为什么要在这里做 (2026-09-16 实测发现): 下载器只在「它自己的落盘路径已存在文件」
        时才备份 .bak; 而用户的图库大多被手工分类进 `X图/作者_ID/` 这类子目录, 下载器的
        落盘目录未必等于当前文件所在目录 → 覆盖发生了却没有 .bak, 「备份管理」形同虚设。
        这里按数据库里**真实的 local_path** 逐张备份, 与下载器落盘位置无关, 保证"覆盖前
        一定留过一份旧文件"。.bak 不参与对账(后缀不匹配图片), 不会产生幽灵记录。"""
        import shutil as _sh
        n = 0
        try:
            rows = db.conn.execute(
                "SELECT local_path FROM pages WHERE artwork_id=? "
                "AND local_path IS NOT NULL AND local_path <> ''", (str(aid),)).fetchall()
        except Exception:
            return 0
        for r in rows:
            rel = str(r[0]).replace("\\", "/")
            try:
                src = img_root / rel
                if not src.is_file():
                    continue
                bak = src.with_name(src.name + ".bak")
                if bak.exists():
                    bak.unlink()          # 只留最新一份, 不无限堆叠
                _sh.copy2(str(src), str(bak))
                n += 1
            except Exception:
                continue
        return n

    @app.route("/api/cart/repair", methods=["POST"])
    def api_cart_repair():
        """♻ 重下修复: 用官方源**覆盖重下**选中的作品 (旧文件自动备份为 .bak)。

        与「加入下载」的区别只在**意图是否显式声明** —— 这个接口永远是"覆盖重下",
        所以批量也无需任何隐式判定 (2026-09-16 方案):
          - 「加入下载」(单个/批量) = 有则跳过、缺页补全, 永不覆盖;
          - 「重下修复」= 永远覆盖, 必须由用户点这个按钮来表达。
        只接受纯数字 pixiv 作品 (外来源/合并产物没有可重下的官方源, 如实跳过并给出原因)。
        入队后只跑刚入队的这几条 (only_ids) 且固定 overwrite 策略,
        不会把队列里其他普通任务也一起变成"覆盖"。"""
        data = request.get_json(silent=True) or {}
        raw_ids = [str(x) for x in (data.get("ids") or []) if str(x).strip()]
        if not raw_ids:
            return jsonify({"status": "error", "error": "缺少作品 ids"}), 400
        if _cart_busy.is_set():
            return jsonify({"status": "error",
                            "error": "已有下载任务在运行，等它结束再重下修复"
                                     "（避免两套下载策略混在一轮里）"}), 409
        ids = list(dict.fromkeys(raw_ids))
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        out = {"added": 0, "in_queue": 0, "skipped": [], "item_ids": [], "backed_up": 0}
        with _db() as db:
            for aid in ids:
                if not aid.isdigit():
                    out["skipped"].append({"artwork_id": aid,
                                           "reason": "非 pixiv 作品，没有官方源可重下"})
                    continue
                meta = db.get_artwork_metadata(aid)
                if meta is None:
                    out["skipped"].append({"artwork_id": aid, "reason": "不在库中"})
                    continue
                res = _cart_add_item(db, {
                    "item_type": "artwork", "artwork_id": aid,
                    "title": meta.get("title"),
                    "author_id": meta.get("author_id"),
                    "author_name": meta.get("author_name"),
                    "page_count": meta.get("page_count") or meta.get("total_pages"),
                    "strategy": DownloadStrategy.OVERWRITE.value,
                    "force": True,
                    "source_note": "重下修复（覆盖本地）",
                })
                if res.get("reason") == "added":
                    # 入队成功 = 这一轮马上会覆盖本地 → 立刻按 DB 里的真实路径逐张留 .bak
                    out["backed_up"] += _backup_work_files(db, img_root, aid)
                    out["added"] += 1
                    out["item_ids"].append(res["item_id"])
                elif res.get("reason") == "in_queue":
                    out["in_queue"] += 1
                else:
                    out["skipped"].append({"artwork_id": aid,
                                           "reason": res.get("reason") or "未入队"})
        if out["added"]:
            audit_log.record("update", "重下修复（覆盖本地）",
                             f"{out['added']} 个作品将用官方源覆盖重下"
                             f"（旧文件备份为 .bak：{out['backed_up']} 张）",
                             out["added"], ids=ids)
            _launch_run_cart(only_ids=out["item_ids"], strategy=DownloadStrategy.OVERWRITE)
        return jsonify({"status": "ok", **out})

    @app.route("/api/tasks/stop", methods=["POST"])
    def api_tasks_stop():
        _cancel_event.set()
        return jsonify({"status": "stopping"})

    # ---------------- 图库管理 ----------------
    # ---- 来源治理: 记录/解析来源 URL → 归类; 无 URL 才进反搜 (2026-09-10 方案) ----
    _SOURCE_SITES = {
        "pixiv": ("pixiv", "Pixiv 插画站"),
        "twitter": ("twitter", "X / Twitter"),
        "bili": ("bili", "bilibili"),
        "ggac": ("ggac", "GGAC 专业画师社区"),
        "danbooru": ("danbooru", "Danbooru 图站"),
        "saucenao": ("saucenao", "SauceNAO 识图站"),
        "artstation": ("artstation", "ArtStation 作品集"),
        "deviantart": ("deviantart", "DeviantArt 图站"),
        "niconico": ("niconico", "Nico静画"),
        "skeb": ("skeb", "Skeb 约稿"),
        "yandere": ("yandere", "yande.re"),
        "konachan": ("konachan", "Konachan"),
        "gelbooru": ("gelbooru", "Gelbooru"),
        "sankaku": ("sankaku", "Sankaku Channel"),
        "safebooru": ("safebooru", "Safebooru"),
        "zerochan": ("zerochan", "Zerochan"),
        "animepictures": ("animepictures", "Anime-Pictures"),
        "drawr": ("drawr", "drawr"),
        "shutterstock": ("shutterstock", "Shutterstock"),
        "mangadex": ("mangadex", "MangaDex"),
        "fakku": ("fakku", "FAKKU"),
        "kemono": ("kemono", "Kemono"),
        "nhentai": ("nhentai", "nHentai"),
        "ehentai": ("ehentai", "E-Hentai"),
        "bcy": ("bcy", "bcy.net"),
        "medibang": ("medibang", "MediBang"),
        "portalgraphics": ("portalgraphics", "PortalGraphics"),
        "madokami": ("madokami", "Madokami"),
        "doujinshi": ("doujinshi", "DoujinshiDB"),
        "2dmarket": ("2dmarket", "2D-Market"),
    }
    # 域名白名单: 站点键 → URL host 片段 (供 _classify_url 归类, 不提取作品 ID)
    _SOURCE_HOSTS = {
        "twitter": ("twitter.com", "x.com"),
        "bili": ("bilibili.com", "b23.tv"),
        "ggac": ("ggac.com",),
        "danbooru": ("danbooru.com", "donmai.us"),
        "saucenao": ("saucenao.com",),
        "artstation": ("artstation.com",),
        "deviantart": ("deviantart.com",),
        "niconico": ("nicovideo.jp",),
        "skeb": ("skeb.jp",),
        "yandere": ("yande.re",),
        "konachan": ("konachan.com", "konachan.net"),
        "gelbooru": ("gelbooru.com",),
        "sankaku": ("sankakucomplex.com",),
        "safebooru": ("safebooru.org",),
        "zerochan": ("zerochan.net",),
        "animepictures": ("anime-pictures.net",),
        "drawr": ("drawr.net",),
        "shutterstock": ("shutterstock.com",),
        "mangadex": ("mangadex.org",),
        "fakku": ("fakku.net",),
        "kemono": ("kemono.party",),
        "nhentai": ("nhentai.net",),
        "ehentai": ("e-hentai.org",),
        "bcy": ("bcy.net",),
        "medibang": ("medibang.com",),
        "portalgraphics": ("portalgraphics.net",),
        "madokami": ("madokami.com",),
        "doujinshi": ("doujinshi.org",),
        "2dmarket": ("2d-market.com",),
    }

    def _classify_url(url: str) -> "dict | None":
        """解析来源 URL → {site, label, artwork_id?, handle?, note}；无法识别返回 None。
        pixiv 优先提取作品页/作者页 ID, 其余站点只归类不推断作品 ID;
        画师主页 URL (artstation/twitter/skeb/deviantart/ggac 等) 顺带提取账号 handle,
        供 source-set 自动落 source_author。"""
        if not url:
            return None
        u = str(url).strip().lower()
        note = ""
        if "pixiv.net" in u:
            m = re.search(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)", u)
            if m:
                return {"site": "pixiv", "label": "Pixiv 插画站",
                        "artwork_id": m.group(1), "note": "作品页"}
            m2 = re.search(r"pixiv\.net/(?:[a-z]{2}/)?users/(\d+)", u)
            if m2:
                return {"site": "pixiv", "label": "Pixiv 插画站",
                        "artwork_id": None, "handle": m2.group(1), "note": "作者主页"}
            return {"site": "pixiv", "label": "Pixiv 插画站",
                    "artwork_id": None, "note": "站点页面"}
        # 画师主页/账号 URL → 域名后第一段即账号 (排除非账号路径段, 如 artstation.com/artwork/…)
        _HANDLE_HOSTS = {"artstation.com", "twitter.com", "x.com", "skeb.jp",
                         "deviantart.com", "ggac.com"}
        _NON_HANDLE_SEG = {"artwork", "artworks", "posts", "post", "view", "users",
                           "user", "u", "artist", "authors", "profile", "art",
                           "home", "settings", "gallery", "projects", "store", "status",
                           "search", "explore", "login", "signup", "about", "jobs",
                           "work", "feed", "portfolio", "prints", "subscriptions",
                           "video", "space", "opus", "notifications", "messages"}
        for key in _SOURCE_SITES:
            if key == "pixiv":
                continue
            for host in _SOURCE_HOSTS.get(key, ()):
                if host not in u:
                    continue
                seg = ""
                m = re.match(r"https?://[^/]+/(?:@)?([^/?#]+)", u)
                if m:
                    seg = m.group(1).strip("/").lower()
                if host in _HANDLE_HOSTS and seg and seg not in _NON_HANDLE_SEG:
                    return {"site": key, "label": _SOURCE_SITES[key][1],
                            "artwork_id": None, "handle": seg, "note": ""}
                return {"site": key, "label": _SOURCE_SITES[key][1],
                        "artwork_id": None, "note": ""}
        if "http://" in u or "https://" in u:
            return None  # 是 URL 但站点未知
        return None

    @app.route("/api/library/source-detect")
    def api_library_source_detect():
        """检测作品来源 URL: 有 URL → 解析归类; 无 → 提示进反搜。"""
        aid = str(request.args.get("artwork_id") or "")
        if not aid:
            return jsonify({"status": "error", "error": "缺少 artwork_id"}), 400
        with _db() as db:
            row = db.conn.execute(
                "SELECT source, source_url FROM artworks WHERE artwork_id=?",
                (aid,)).fetchone()
        if row is None:
            return jsonify({"status": "error", "error": "作品不存在"}), 404
        url = (row["source_url"] or "").strip() if row["source_url"] else ""
        return jsonify({
            "artwork_id": aid,
            "source": row["source"] or "pixiv",
            "has_url": bool(url),
            "url": url,
            "detected": _classify_url(url) if url else None,
        })

    @app.route("/api/library/source-set", methods=["POST"])
    def api_library_source_set():
        """保存来源 URL（可选同时写 source 归类）。校验 source 必须是已知站点键。
        可选 author_name/author_id: 反搜确认当场把作者建库并关联作品
        (仅当作品尚无作者或作者为 local_ 本地作者时, 不覆盖已有 pixiv 作者)。"""
        data = request.get_json(silent=True) or {}
        aid = str(data.get("artwork_id") or "")
        if not aid:
            return jsonify({"status": "error", "error": "缺少 artwork_id"}), 400
        fields = {}
        url = str(data.get("source_url") or "").strip()
        if url:
            fields["source_url"] = url
        src = str(data.get("source") or "").strip()
        if src:
            if src not in _SOURCE_SITES and src not in ("other", "pixiv"):
                return jsonify({"status": "error",
                                "error": f"未知来源站点: {src}"}), 400
            fields["source"] = src
        sa = str(data.get("source_author") or "").strip()
        if sa:
            fields["source_author"] = sa
        # URL 里带画师账号 (画师主页链接) → 自动落 source_author (只增不覆盖, 用户未显式给时)
        if url and not sa:
            det = _classify_url(url)
            if det and det.get("handle"):
                with _db() as db:
                    cur = db.get_artwork_metadata(aid) or {}
                    if not str(cur.get("source_author") or "").strip():
                        fields.setdefault("source_author", str(det["handle"]))
        # 只带作者信息(无来源字段)也应允许 —— 用于把拆出来的图重新挂回作者
        # (拆分后想改回作者, 却因 fields 为空被 400 挡掉)
        _aid_in = str(data.get("author_id") or "").strip()
        _aname_in = str(data.get("author_name") or "").strip()
        if not fields and not (_aid_in or _aname_in):
            return jsonify({"status": "error", "error": "没有可写入的字段"}), 400
        author_id_out = None
        _rk: list = []
        with _db() as db:
            if fields:                    # 只给作者时 fields 为空, 跳过来源字段更新
                db.update_artwork(aid, **fields)
            # 反搜确认附带作者信息: 建作者记录 + 关联到作品
            a_id = str(data.get("author_id") or "").strip()
            a_name = str(data.get("author_name") or "").strip()
            if a_id or a_name:
                cur = db.get_artwork_metadata(aid)
                existing = (cur or {}).get("author_id") or ""
                # 安全: 已有 pixiv 作者不覆盖; 仅空作者或本地作者时可补
                if not existing or str(existing).startswith("local_"):
                    target_id = None
                    if a_id and a_id.isdigit():
                        if a_name:
                            db.upsert_author(a_id, a_name)
                        target_id = a_id
                    elif a_name:
                        # 外来源作者: 用「站点 + 账号」当身份 (账号稳定, 显示名只是名字),
                        # 同一个人不会因为"显示名/账号/改名"被拆成两条作者记录
                        _site = fields.get("source") or (cur or {}).get("source") or ""
                        _hd = (fields.get("source_author")
                               or (cur or {}).get("source_author") or "")
                        target_id = db.create_or_get_local_author(
                            a_name, source=str(_site), handle=str(_hd))
                    if target_id:
                        db.set_artworks_author([aid], target_id)
                        author_id_out = target_id
            # 来源一确认就顺带纠正身份: 合并产物一旦被标明是外部来源
            # (推特/ArtStation/…), 主键与标题就该从 merged_xxx 变成 ext_ 语义, 不能一直背着
            # 「合并」身份。只动 merged_ 前缀的作品, 纯 pixiv 合并产物不受影响。
            if fields.get("source") or fields.get("source_url"):
                try:
                    _rk = db.reidentify_merged_external(only_aid=aid)
                except Exception:
                    _rk = []
        resp = {"status": "ok", "updated": fields}
        if author_id_out:
            resp["author_id"] = author_id_out
        if _rk:
            # ID 变了 → 告诉前端新主键, 否则它还在按旧 ID 发后续请求
            resp["reidentified"] = [{"old": o, "new": n} for o, n in _rk]
            resp["artwork_id"] = _rk[-1][1]
        _parts = []
        if "source_url" in fields:
            _parts.append(f"来源URL={fields['source_url']}")
        if "source" in fields:
            _parts.append(f"来源={fields['source']}")
        if "source_author" in fields:
            _parts.append(f"来源作者={fields['source_author']}")
        if author_id_out:
            _parts.append(f"作者→{author_id_out}")
        audit_log.record("update", "标注来源",
                         f"{_labels_inline([aid])}：" + "，".join(_parts), 1, ids=[aid])
        return jsonify(resp)

    def _pixiv_http_session():
        """用本地 cookies.bin/account.json 构建 pixiv 直连会话 (与下载/查询同一套)。"""
        from utils.sec import load_cookies_smart
        from core.search_client import build_pixiv_http_session
        cf = Path(paths.user_data(_config["paths"]["cookies_filename"]))
        cookies = load_cookies_smart(str(cf), upgrade=False)
        uid = ""
        try:
            acc = json.loads(
                (paths.data_root() / "account.json").read_text(encoding="utf-8"))
            uid = str(acc.get("user_id") or "")
        except Exception:
            pass
        return build_pixiv_http_session(cookies, uid)

    def _apply_claim_moves(res: dict, img_root: Path) -> "tuple[int,int]":
        """执行认领/批量认领返回的物理移动: 同字节已存在 → 删源, 否则 rename。
        返回 (moved, dropped)。与 merge-pages 接口同一套物理规则。"""
        moved = dropped = 0
        for src_rel, dst_rel in res["rel_moves"]:
            try:
                s = Path(img_root) / src_rel
                d = Path(img_root) / dst_rel
                if s.is_file() and s != d:
                    d.parent.mkdir(parents=True, exist_ok=True)
                    if d.exists() and d.is_file() and d.stat().st_size == s.stat().st_size:
                        s.unlink(missing_ok=True)
                    else:
                        s.replace(d)
                    moved += 1
            except Exception:
                continue
        for rel in res.get("drop_files", []):
            try:
                (Path(img_root) / rel).unlink(missing_ok=True)
                dropped += 1
            except Exception:
                continue
        return moved, dropped

    # 🪄 批量认领散落页的后台进度 (与 _SRC_SEARCH 同款)
    _CLAIM_BATCH = {
        "running": False, "total": 0, "done": 0, "ok": 0, "failed": 0,
        "current": "", "results": None, "error": "",
        "started_at": None, "finished_at": None}

    @app.route("/api/library/claim-pixiv", methods=["POST"])
    def api_library_claim_pixiv():
        """✅ 认领转正: 把本地占位作品 (merged_xxx/ext_xxx) 变成真正的 pixiv 作品。

        body: {"artwork_id": 本地作品ID, "pixiv_id": "88007979"}
        当场拉 pixiv ajax 权威元数据 (标题/作者/页数/R18), 文件名 {pid}_pN 定页码,
        文件规范重命名、旧作品删除合并; 页 origin='web'。身份对不上/目标已被别的图占用
        一律拒绝, 绝不覆盖 (db.claim_pixiv_work 内硬闸)。"""
        data = request.get_json(silent=True) or {}
        old_id = str(data.get("artwork_id") or "").strip()
        pid = str(data.get("pixiv_id") or "").strip()
        if not old_id or not pid:
            return jsonify({"status": "error",
                            "message": "缺少 artwork_id 或 pixiv_id"}), 400
        if not pid.isdigit():
            return jsonify({"status": "error", "message": "pixiv_id 必须是纯数字"}), 400
        if old_id == pid:
            return jsonify({"status": "error",
                            "message": "该作品已经是正式 pixiv 作品"}), 400
        session = _pixiv_http_session()
        if session is None:
            return jsonify({"status": "error",
                            "message": "本地无可用的 pixiv 登录 cookies，"
                                       "请到「设置」页登录后再认领"}), 400
        from core.search_client import http_fetch_illust, ArtworkNotFoundError
        try:
            info = http_fetch_illust(session, pid)
        except PermissionError as e:
            return jsonify({"status": "error",
                            "message": f"pixiv 登录态已失效，请重新登录后再试（{e}）"}), 400
        except ArtworkNotFoundError:
            return jsonify({"status": "error",
                            "message": f"pixiv 上不存在作品 #{pid}（已删除或 ID 有误）"}), 404
        except Exception as e:
            return jsonify({"status": "error",
                            "message": f"拉取 pixiv 信息失败: {str(e).splitlines()[0]}"}), 502

        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        old_label = _labels([old_id]).get(old_id) or old_id
        try:
            with _db() as db:
                res = db.claim_pixiv_work(old_id, pid, info=info, img_root=img_root)
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)}), 400
        # DB 提交后执行文件重命名 + 同字节删重 (与合并接口同一套物理移动)
        moved, dropped = _apply_claim_moves(res, img_root)
        audit_log.record(
            "update", "认领转正",
            f"{old_label} 认领为 pixiv 作品 #{pid}《{res.get('title') or ''}》"
            f" · 作者 {res.get('author_name') or ''}：{len(res['pages'])} 页转正"
            + (f"，{moved} 个文件重命名" if moved else "")
            + (f"，{dropped} 个重复文件删除" if dropped else ""),
            len(res["pages"]), ids=[pid])
        return jsonify({"status": "ok", **res, "files_moved": moved,
                        "files_dropped": dropped})

    @app.route("/api/library/claim-scan", methods=["POST"])
    def api_library_claim_scan():
        """🪄 离线扫描「散落的 pixiv 页」: 所有非数字 ID 作品 (merged_/ext_/...) 里,
        文件名自带 {pid}_pN 的页本该属于哪个正式作品。不联网、不改库。
        返回 ready(全部页同一 pid, 可直接认领) / mixed(多 pid 混杂, 需先拆) / no_id。"""
        with _db() as db:
            scan = db.scan_claimable_pixiv_works()
        return jsonify({"status": "ok", **scan})

    @app.route("/api/library/claim-batch/status", methods=["GET"])
    def api_library_claim_batch_status():
        return jsonify(_CLAIM_BATCH)

    @app.route("/api/library/claim-batch", methods=["POST"])
    def api_library_claim_batch():
        """🪄 批量认领转正: body {"items":[{"artwork_id": 占位ID, "pixiv_id": pid}, ...]}。
        后台线程串行: 同一 pid 的 ajax 权威信息只拉一次 → claim_pixiv_work 硬闸校验
        → 文件重命名/删重。单条失败(身份不符/异图占用/404)只记录, 不拖垮整批;
        cookies 失效(403)整批中止。进度走 /api/library/claim-batch/status 轮询。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or []
        clean, seen = [], set()
        for it in items:
            aid = str((it or {}).get("artwork_id") or "").strip()
            pid = str((it or {}).get("pixiv_id") or "").strip()
            if not aid or not pid.isdigit() or aid == pid:
                continue
            k = (aid, pid)
            if k not in seen:
                seen.add(k)
                clean.append({"artwork_id": aid, "pixiv_id": pid})
        if not clean:
            return jsonify({"status": "error",
                            "message": "没有可认领的条目（需指定非数字作品 ID + 纯数字 pixiv ID）"}), 400
        if _CLAIM_BATCH["running"]:
            return jsonify({"status": "error",
                            "message": "已有批量认领任务进行中，请等待完成"}), 409

        session = _pixiv_http_session()
        if session is None:
            return jsonify({"status": "error",
                            "message": "本地无可用的 pixiv 登录 cookies，请到「设置」页登录"}), 400
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        _CLAIM_BATCH.update(running=True, total=len(clean), done=0, ok=0, failed=0,
                            current="", results=[], error="",
                            started_at=time.time(), finished_at=None)

        def _task():
            from core.search_client import http_fetch_illust, ArtworkNotFoundError
            info_cache: "dict[str, object]" = {}

            def _info(pid: str):
                if pid not in info_cache:
                    info_cache[pid] = http_fetch_illust(session, pid)
                return info_cache[pid]

            labels = _labels([it["artwork_id"] for it in clean])
            try:
                for i, it in enumerate(clean, 1):
                    aid, pid = it["artwork_id"], it["pixiv_id"]
                    _CLAIM_BATCH["current"] = f"{aid} → #{pid}"
                    row = {"artwork_id": aid, "pixiv_id": pid, "ok": False,
                           "title": "", "pages": 0, "files_moved": 0, "error": ""}
                    try:
                        info = _info(pid)
                    except PermissionError as e:
                        row["error"] = f"登录态失效（{str(e).splitlines()[0]}），整批中止"
                        _CLAIM_BATCH["results"].append(row)
                        _CLAIM_BATCH.update(
                            done=i, failed=_CLAIM_BATCH["failed"] + 1,
                            error=row["error"], running=False, finished_at=time.time())
                        return
                    except ArtworkNotFoundError:
                        row["error"] = "pixiv 上不存在该作品（已删除或 ID 有误）"
                    except Exception as e:
                        row["error"] = f"拉取信息失败: {str(e).splitlines()[0]}"
                    else:
                        try:
                            with _db() as db:
                                res = db.claim_pixiv_work(aid, pid, info=info,
                                                          img_root=img_root)
                            moved, dropped = _apply_claim_moves(res, img_root)
                            row.update(ok=True, title=res.get("title") or "",
                                       pages=len(res["pages"]), files_moved=moved)
                            audit_log.record(
                                "update", "批量认领转正",
                                f"{labels.get(aid) or aid} 认领为 pixiv 作品 #{pid}"
                                f"《{row['title']}》：{row['pages']} 页"
                                + (f"，{moved} 个文件重命名" if moved else ""),
                                row["pages"], ids=[pid])
                        except ValueError as e:
                            row["error"] = str(e)
                        except Exception as e:
                            row["error"] = f"系统错误: {str(e).splitlines()[0]}"
                    _CLAIM_BATCH["results"].append(row)
                    _CLAIM_BATCH.update(done=i,
                                       ok=_CLAIM_BATCH["ok"] + (1 if row["ok"] else 0),
                                       failed=_CLAIM_BATCH["failed"]
                                       + (0 if row["ok"] else 1))
                _CLAIM_BATCH.update(running=False, finished_at=time.time())
            except Exception as e:
                _CLAIM_BATCH.update(running=False, error=f"批量任务异常: {e}",
                                    finished_at=time.time())
            finally:
                # 非 HTTP 触发的写入 → 显式登记 (转正会改作品/页/来源分类);
                # 一个都没转正成功就不登记
                if _CLAIM_BATCH.get("ok"):
                    mark_dirty("artworks", "pages", "sources", "authors", "facets", "stats")

        threading.Thread(target=_task, daemon=True, name="claim-batch").start()
        return jsonify({"status": "started", "total": len(clean)})

    def _fill_filename_hits_author(hits: "list[dict]") -> None:
        """给「文件名捷径」命中的行补上作者 —— **只查本地 DB, 不联网**。

        文件名 `{pid}_pN` 只给出**作品 ID**, 给不出作者。这里以前把 `author_name`/`author_id`
        写死成空串, 于是结果行只剩「✅ 命中 Pixiv #xxx」, 作者一栏空着、确认框里也没有
        「作者 = …」那一行 —— `115531046`: 作品 ID 明明 能直接打开原网页,
        系统却说没识别出作者。而本地 `artworks`/`authors` 里其实早就记着这位作者了。

        库里有就填, 库里没有(占位作品/未入库)才留空 —— 保持这条捷径「秒回、不启浏览器」的性质。
        `author_src` 仍标 `filename`: 身份(作品 ID)确实来自文件名, 前端那颗
        「🟡 文件名自带ID·待确认」黄标不能因此变成「以图搜图命中」。
        """
        want = sorted({str(h["artwork_id"]) for h in hits if h.get("artwork_id")})
        if not want:
            return
        got = {}
        with _db() as db:
            ph = ",".join("?" * len(want))
            for ar in db.conn.execute(
                    "SELECT a.artwork_id AS aid, a.author_id AS auid, u.name AS aname "
                    "FROM artworks a LEFT JOIN authors u ON u.author_id = a.author_id "
                    f"WHERE a.artwork_id IN ({ph})", want):
                if ar["auid"]:
                    got[str(ar["aid"])] = (str(ar["aname"] or ""), str(ar["auid"]))
        for h in hits:
            nm, auid = got.get(str(h["artwork_id"]), ("", ""))
            if auid:
                h["author_name"], h["author_id"] = nm, auid

    @app.route("/api/library/source-search", methods=["POST"])
    def api_library_source_search():
        """对作品本地页跑 SauceNAO 反搜 (假设 pixiv 来源), 返回逐张命中。
        最多搜前 6 张, 串行; 任一失败不拖垮整次。
        后台线程执行, 进度经 /api/reverse/status 的 src_search 字段轮询 (前端弹窗进度条)。"""
        data = request.get_json(silent=True) or {}
        aid = str(data.get("artwork_id") or "")
        if not aid:
            return jsonify({"status": "error", "error": "缺少 artwork_id"}), 400
        # saucenao='force': 即使文件名自带 pixiv ID 也强制上传 SauceNAO 以图搜图核对
        force_sn = str(data.get("saucenao") or "") == "force"
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        rows = []
        with _db() as db:
            rows = db.conn.execute(
                "SELECT local_path FROM pages WHERE artwork_id=? "
                "AND local_path IS NOT NULL AND local_path != '' "
                "ORDER BY page_num LIMIT 6", (aid,)).fetchall()
        if not rows:
            return jsonify({"status": "error", "error": "该作品无本地图片"}), 400
        # 文件名捷径 (信任链: 文件名自带作品 ID 高于 SauceNAO): 文件名已是 {pid}_pN 规范
        # 命名时直接给出结论, 不耗 SauceNAO 配额/不启浏览器; 命中的页不再上传反搜。
        from db.store import pixiv_natural_key
        filename_hits, sn_rows = [], []
        for r in rows:
            lp = r["local_path"]
            nkey = None if force_sn else pixiv_natural_key(Path(lp).name)
            if nkey:
                filename_hits.append({
                    "file": lp, "site": "pixiv", "artwork_id": nkey[0],
                    "author_name": "", "author_id": "",
                    "url": f"https://www.pixiv.net/artworks/{nkey[0]}",
                    "handle": "", "author_src": "filename", "page": nkey[1]})
            else:
                sn_rows.append(r)
        _fill_filename_hits_author(filename_hits)
        st = _SRC_SEARCH
        if st["running"]:
            return jsonify({"status": "error", "error": "已有反搜来源任务进行中，请稍候再试"}), 409
        st.update(running=True, artwork_id=aid, total=len(rows), done=0,
                  current="", phase="running", error="", result=None,
                  started_at=time.time(), finished_at=None)

        def _task():
            from core.reverse_search import ReverseSearch
            hits = list(filename_hits)
            for i, h in enumerate(hits, 1):
                st["current"] = h["file"]
                st["done"] = i
            if not sn_rows:
                # 全部页都能从文件名定身份 → 不起浏览器, 秒回
                st["phase"] = "done"
                st["result"] = hits
                st["running"] = False
                st["finished_at"] = time.time()
                return
            try:
                from utils.reporter import PrintReporter
                with ReverseSearch(search_path=str(img_root), config=_config,
                                   reporter=PrintReporter()) as rs:
                    for r in sn_rows:
                        i = len(hits) + 1
                        lp = r["local_path"]
                        st["current"] = lp
                        st["done"] = i - 1
                        abs_path = (Path(img_root) / lp).resolve()
                        if not abs_path.is_file():
                            hits.append({"file": lp, "error": "本地文件不存在"})
                            st["done"] = i
                            continue
                        try:
                            hit = rs.search_by_image(abs_path, detail=True)
                            hits.append({
                                "file": lp,
                                "site": hit.get("site"),
                                "artwork_id": hit.get("artwork_id"),
                                "author_name": hit.get("author_name"),
                                "author_id": hit.get("author_id"),
                                "url": hit.get("url") or "",
                                "handle": hit.get("handle") or "",
                                # page=源网页当前作者(权威) / handle=URL 里的账号 /
                                # saucenao_index=第三方索引快照(可能过时, 界面需提示核对)
                                "author_src": hit.get("author_src") or "",
                            })
                        except Exception as e:
                            hits.append({"file": lp, "error": str(e).splitlines()[0]})
                        st["done"] = i
                st["phase"] = "done"
            except Exception as e:
                st["phase"] = "error"
                st["error"] = f"反搜引擎启动失败: {str(e).splitlines()[0]}"
                if not hits:
                    hits.append({"file": "", "error": st["error"]})
            finally:
                st["result"] = hits
                st["running"] = False
                st["finished_at"] = time.time()

        threading.Thread(target=_task, daemon=True).start()
        return jsonify({"status": "started", "artwork_id": aid,
                        "filename_hits": len(filename_hits),
                        "saucenao_queued": len(sn_rows)})

    @app.route("/api/library/batch-source-search", methods=["POST"])
    def api_library_batch_source_search():
        """🔎 批量搜索来源 (2026-09-16 需求)。

        body: {"artwork_ids": [...]}
        逐个作品两阶段处理:
          ① 已记录 source_url 且能归类的 → **直接归类**(零成本, 不占反搜额度);
          ② 其余 → 取首张本地图跑 SauceNAO, 命中结果**只报告**由用户确认 (不自动改库,
             反搜可能误判, 不能代为决定)。
        SauceNAO 每张 5~15 秒且有限速, 所以后台串行 + 前端轮询 _SRC_SEARCH["batch"]。"""
        data = request.get_json(silent=True) or {}
        aids = [str(x).strip() for x in (data.get("artwork_ids") or []) if str(x).strip()]
        if not aids:
            return jsonify({"status": "error", "error": "未选择作品"}), 400
        st = _SRC_SEARCH
        if st["running"]:
            return jsonify({"status": "error",
                            "error": "已有搜索来源/反搜任务进行中，请稍候再试"}), 409
        st.update(running=True, artwork_id=None, total=len(aids), done=0, current="",
                  phase="running", error="", result=None, started_at=time.time(),
                  finished_at=None,
                  batch={"total": len(aids), "done": 0, "auto_classified": 0,
                         "items": [{"artwork_id": a, "status": "pending"} for a in aids]})

        def _task():
            from utils.reporter import PrintReporter
            img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
            bat = st["batch"]
            try:
                # ---- ① 零成本: 已记录 URL 的直接归类 ----
                todo = []
                for it in bat["items"]:
                    aid = it["artwork_id"]
                    st["current"] = aid
                    with _db() as db:
                        row = db.conn.execute(
                            "SELECT source_url FROM artworks WHERE artwork_id=?",
                            (aid,)).fetchone()
                    url = (row["source_url"] or "").strip() if row and row["source_url"] else ""
                    det = _classify_url(url) if url else None
                    if url and det and det.get("site"):
                        try:
                            with _db() as db:
                                db.update_artwork(aid, source=det["site"])
                            it.update(status="classified", site=det["site"],
                                      label=det.get("label"), url=url)
                            bat["auto_classified"] += 1
                        except Exception as e:
                            it.update(status="error", url=url,
                                      error=str(e).splitlines()[0])
                        bat["done"] += 1          # 归类完成才计数
                    else:
                        # 留给阶段②处理, 在那边计数 —— 这里若也 +1 会重复,
                        # 出现「4/2」这种 done 超过 total 的进度
                        it.update(status="queued", url=url)
                        todo.append(it)
                    st["done"] = bat["done"]

                # ---- ② 需要反搜的逐个跑 (慢, 引擎只在有活时才启动) ----
                if todo:
                    from core.reverse_search import ReverseSearch
                    with ReverseSearch(search_path=str(img_root), config=_config,
                                       reporter=PrintReporter()) as rs:
                        for it in todo:
                            aid = it["artwork_id"]
                            st["current"] = aid
                            try:
                                with _db() as db:
                                    r = db.conn.execute(
                                        "SELECT local_path FROM pages WHERE artwork_id=? "
                                        "AND local_path IS NOT NULL AND local_path != '' "
                                        "ORDER BY page_num LIMIT 1", (aid,)).fetchone()
                                if not r:
                                    it.update(status="error", error="该作品无本地图片")
                                    continue
                                abs_p = (img_root / r["local_path"]).resolve()
                                if not abs_p.is_file():
                                    it.update(status="error", error="本地文件不存在")
                                    continue
                                hit = rs.search_by_image(abs_p, detail=True)
                                it.update(site=hit.get("site"),
                                          artwork_id=hit.get("artwork_id"),
                                          author_name=hit.get("author_name"),
                                          url=hit.get("url") or "",
                                          handle=hit.get("handle") or "",
                                          status="hit" if (hit.get("site")
                                                           or hit.get("artwork_id"))
                                          else "miss")
                            except Exception as e:
                                it.update(status="error",
                                          error=str(e).splitlines()[0])
                            finally:
                                # finally 保证任何分支(含 continue)都计数一次
                                bat["done"] += 1
                                st["done"] = bat["done"]
                st["phase"] = "done"
            except Exception as e:
                st["phase"] = "error"
                st["error"] = f"批量搜索来源失败: {str(e).splitlines()[0]}"
            finally:
                st["running"] = False
                st["finished_at"] = time.time()
                # 批量搜来源会写回 source/source_url/source_author → 显式登记脏域
                if any(it.get("status") == "hit" for it in bat["items"]):
                    mark_dirty("artworks", "sources", "facets", "stats")

        threading.Thread(target=_task, daemon=True).start()
        return jsonify({"status": "started", "total": len(aids)})

    # ---- 备份管理: OVERWRITE 强制重下时旧文件自动存 .bak, 这里提供列出/对比/恢复/删除 ----
    _BACKUP_MIME = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                    '.webp': 'image/webp', '.gif': 'image/gif'}

    def _local_bak_rel(rel: str) -> "Path | None":
        """把前端传的 .bak 相对路径解析成 img_root 内绝对路径; 跨出根目录返回 None。"""
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()
        p = (img_root / rel.replace("\\", "/")).resolve()
        if p != img_root and img_root not in p.parents:
            return None
        return p

    @app.route("/api/library/backups")
    def api_library_backups():
        """全库 .bak 备份清单 (按作品聚合, 供「备份管理」弹窗对比/恢复/删除)。"""
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        items = []
        if img_root.is_dir():
            for p in img_root.rglob("*"):
                if not p.is_file() or p.suffix.lower() != ".bak":
                    continue
                rel = str(p.relative_to(img_root)).replace("\\", "/")
                orig_name = p.name[:-4]
                nat = pixiv_natural_key(orig_name)
                try:
                    sz = p.stat().st_size
                    mt = p.stat().st_mtime
                except OSError:
                    sz, mt = 0, 0
                items.append({
                    "artwork_id": nat[0] if nat else None,
                    "page_num": nat[1] if nat else None,
                    "rel": rel,
                    "orig_rel": rel[:-4],          # 去掉 .bak 后的原文件相对路径
                    "orig_name": orig_name,
                    "backup_exists": p.exists(),
                    "size": sz,
                    "mtime": mt,
                })
        items.sort(key=lambda x: (x["artwork_id"] or "", x["page_num"] or 0))
        return jsonify({"total": len(items), "items": items})

    @app.route("/api/library/backup-img")
    def api_library_backup_img():
        """读 .bak 文件二进制并以正确图片 mime 返回 (浏览器 <img> 才能渲染 .bak)。"""
        rel = request.args.get("rel", "")
        if not rel:
            return jsonify({"error": "missing rel"}), 400
        p = _local_bak_rel(rel)
        if p is None or not p.is_file() or p.suffix.lower() != ".bak":
            return jsonify({"error": "not found"}), 404
        stem = p.name[:-4]
        ext = Path(stem).suffix.lower()
        mime = _BACKUP_MIME.get(ext, "application/octet-stream")
        try:
            return send_file(str(p), mimetype=mime,
                             max_age=0, conditional=False)
        except Exception:
            return jsonify({"error": "read failed"}), 500

    @app.route("/api/library/backup/restore", methods=["POST"])
    def api_library_backup_restore():
        """恢复备份: 把 .bak 复制回原名覆盖当前文件, 然后删除 .bak。"""
        data = request.get_json(silent=True) or {}
        rel = data.get("rel") or ""
        p = _local_bak_rel(rel)
        if p is None or not p.is_file() or p.suffix.lower() != ".bak":
            return jsonify({"status": "error", "error": "备份不存在"}), 404
        try:
            import shutil as _sh
            _sh.copy2(str(p), str(p)[:-4])          # .bak → 原名 (覆盖当前新版)
            p.unlink()                               # 恢复后清理备份, 避免空间堆积
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"status": "error",
                            "error": f"恢复失败: {str(e).splitlines()[0]}"}), 500

    @app.route("/api/library/backup/delete", methods=["POST"])
    def api_library_backup_delete():
        """删除备份 (对比后决定放弃旧版)。"""
        data = request.get_json(silent=True) or {}
        rel = data.get("rel") or ""
        p = _local_bak_rel(rel)
        if p is None or not p.is_file() or p.suffix.lower() != ".bak":
            return jsonify({"status": "error", "error": "备份不存在"}), 404
        try:
            p.unlink()
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"status": "error",
                            "error": f"删除失败: {str(e).splitlines()[0]}"}), 500

    @app.route("/api/library/db-backups")
    def api_library_db_backups():
        """数据库备份清单 (data/backup/pixiv_backup_*.zip): 供「💾 备份」中心展示/清理。

        以前「一键备份」只生成 zip 却不显示任何列表, 用户只能自己去文件夹里翻。"""
        with _db() as db:
            bdir = Path(db.backup_dir)
        items = []
        try:
            for p in sorted(bdir.glob("pixiv_backup_*.zip"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                st = p.stat()
                items.append({"name": p.name, "size": st.st_size,
                              "mtime": int(st.st_mtime)})
        except Exception:
            pass
        return jsonify({"status": "ok", "dir": str(bdir), "items": items})

    @app.route("/api/library/db-backup/delete", methods=["POST"])
    def api_library_db_backup_delete():
        """删除一个数据库备份 zip (只删 backup 目录下的 pixiv_backup_*.zip, 防路径穿越)。"""
        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "").strip()
        with _db() as db:
            bdir = Path(db.backup_dir).resolve()
        p = (bdir / name)
        try:
            ok = (p.is_file() and p.parent.resolve() == bdir
                  and p.name.startswith("pixiv_backup_") and p.suffix.lower() == ".zip")
        except OSError:
            ok = False
        if not ok:
            return jsonify({"status": "error", "error": "备份不存在"}), 404
        try:
            p.unlink()
        except Exception as e:
            return jsonify({"status": "error",
                            "error": f"删除失败: {str(e).splitlines()[0]}"}), 500
        audit_log.record("delete", "删除数据库备份", name, 1)
        return jsonify({"status": "ok"})

    @app.route("/api/library/stats")
    def api_library_stats():
        with _db() as db:
            stats = db.stats_overview()
            stats["missing_metadata"] = db.count_missing_metadata()
            return jsonify(stats)

    @app.route("/api/library/breakdown")
    def api_library_breakdown():
        """统计页分布数据: 作品来源/作者状态/作品类型/R18 各维度聚合(按作品数)。"""
        with _db() as db:
            return jsonify(db.stats_breakdown())

    @app.route("/api/library/authors")
    def api_library_authors():
        """作者管理页数据: 筛选(q/status/source) + 排序 + 分页, 返回 {items, total}。
        每行含作品数/来源分布/状态/曾用名。统计页的来源/状态分布另由 stats 提供。"""
        args = request.args
        q = args.get("q", "")
        status = args.get("status", "") or None
        source = args.get("source", "") or None
        sort = args.get("sort", "count")
        order = args.get("order", "desc")
        limit = int(args.get("limit", 50))
        offset = int(args.get("offset", 0))
        with _db() as db:
            items = db.list_authors(q=q, status=status, source=source,
                                    sort=sort, order=order,
                                    limit=limit, offset=offset)
            # 「同名但不同身份」提示 (同一作者出现两条记录):
            # 本地作者(= 站点账号 local_xxx)与真实 pixiv 作者同名时会各占一行 —— 这是**对的**,
            # 但界面要看得出来、并给一键绑定。只标出来让人判断, 谁也不自动合并。
            _same = db.same_name_pixiv_authors(
                [str(x.get("name") or "") for x in items
                 if not str(x.get("author_id") or "").isdigit()])
            for x in items:
                hit = _same.get(str(x.get("name") or ""))
                if hit and str(hit["author_id"]) != str(x.get("author_id")):
                    x["same_name_pixiv"] = hit
            return jsonify({
                "items": items,
                "total": db.count_authors_filtered(q=q, status=status, source=source),
            })

    def _norm_author_links(raw) -> list[dict]:
        """作者「来源链接」规范化 + 识别站点/账号 (链接不能只是记录)。

        复用作品来源那套 URL 分类 `_classify_url` → 每条链接带上:
          site   站点键 (用 _SITE_TO_SOURCE 归一到与 artworks.source 同口径, 如 bili→bilibili)
          label  人读站名 (X / Twitter、bilibili…)
          handle 该站账号 (画师主页 URL 里域名后第一段) ★关联的关键★
        有了 handle, 链接才能参与"作品账号回填 / 归档子夹 / 按链接搜索 / pixiv 身份绑定"。"""
        from db.store import _SITE_TO_SOURCE
        out: list[dict] = []
        for it in (raw or []):
            url = str((it.get("url") if isinstance(it, dict) else it) or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                continue
            det = _classify_url(url) or {}
            site = str(det.get("site") or "")
            item = {"site": _SITE_TO_SOURCE.get(site, site), "url": url}
            if det.get("label"):
                item["label"] = str(det["label"])
            if det.get("handle"):
                item["handle"] = str(det["handle"])
            out.append(item)
        return out

    @app.route("/api/library/author-edit", methods=["POST"])
    def api_library_author_edit():
        """作者管理页编辑: body: {"author_id": "...", "name"?, "status"?, "aliases"?[list],
        "urls"?[list]}。改名时旧名自动并入曾用名; aliases/urls 传则整体替换。
        urls 可多条 (pixiv/X/bilibili/GGAC/danbooru… 主页链接), 保存时会识别出站点与账号,
        并**顺手把该作者名下来源=该站点且没记账号的作品补上 @账号** (链接产生实际关联)。"""
        data = request.get_json(silent=True) or {}
        aid = str(data.get("author_id") or "").strip()
        if not aid:
            return jsonify({"status": "error", "message": "缺少 author_id"}), 400
        links = _norm_author_links(data["urls"]) if "urls" in data else None
        filled = 0
        with _db() as db:
            try:
                old_author = db.get_author(aid)
                author = db.update_author(
                    aid,
                    name=(str(data["name"]).strip()
                          if data.get("name") is not None else None),
                    status=(str(data["status"]).strip()
                            if data.get("status") is not None else None),
                    aliases=([str(x).strip() for x in data["aliases"]
                              if str(x).strip()] if "aliases" in data else None),
                    urls=links,
                )
                if links is not None:
                    filled = db.fill_source_author_from_links(aid, links)
                # 改名后磁盘作者文件夹跟随新名 (pixiv 作者: 净名_ID), 网页路径同步
                new_name = author.get("name")
                if new_name and (not old_author or old_author.get("name") != new_name):
                    _sync_author_folder(db, aid, new_name)
            except KeyError as e:
                return jsonify({"status": "error", "message": str(e)}), 404
        _old_n = (old_author or {}).get("name")
        _new_n = author.get("name")
        audit_log.record("update", "编辑作者",
                         f"作者 {aid}："
                         + (f"改名「{_old_n}」→「{_new_n}」" if _old_n and _old_n != _new_n else "")
                         + ("状态改为 " + str(author.get("status")) if data.get("status") is not None else "")
                         + ("，曾用名整体替换" if "aliases" in data else "")
                         + (f"，来源链接 {len(links)} 条" + (f"（回填作品账号 {filled} 个）" if filled else "")
                            if links is not None else ""), 1)
        return jsonify({"status": "ok", "author": author,
                        "urls": author.get("urls") if author else None,
                        "source_author_filled": filled})

    @app.route("/api/library/author-bind-pixiv", methods=["POST"])
    def api_library_author_bind_pixiv():
        """把「本地作者」绑定到真实 pixiv 作者 ID —— 链接产生"关联"的核心动作。

        典型流程: 给作者记了 https://www.pixiv.net/users/12345
        → 说明他其实就是 pixiv 作者 12345 → 一键把该作者**全部作品迁到 pixiv 作者名下**,
        作者行合并 (曾用名/链接跟着走), 磁盘文件夹按新名同步 (名字_local_xxx → 名字_12345),
        之后体检的「作者改名同步/存续状态」也认得他。"""
        data = request.get_json(silent=True) or {}
        src = str(data.get("author_id") or "").strip()
        pid = str(data.get("pixiv_id") or "").strip()
        if not src or not pid.isdigit():
            return jsonify({"status": "error", "error": "缺少 author_id 或 pixiv_id(纯数字)"}), 400
        if src.isdigit():
            return jsonify({"status": "error", "error": "该作者已经是 pixiv 作者，无需绑定"}), 400
        with _db() as db:
            if db.get_author(src) is None:
                return jsonify({"status": "error", "error": "作者不存在"}), 404
            src_name = str((db.get_author(src) or {}).get("name") or "")
            res = db.merge_author_into(src, pid)
            # 目标作者没有可读名 → 用原作者的现用名顶着, 再联网校正 (失败不影响绑定)
            dst = db.get_author(pid) or {}
            if not str(dst.get("name") or "").strip() and src_name:
                db.upsert_author(pid, src_name)
            _fill_author_name_by_id(db, pid)
            new_name = str((db.get_author(pid) or {}).get("name") or src_name)
            try:
                # 磁盘夹名里还带着旧 ID (名字_local_xxx) → 一起换成 名字_12345
                res["folder_renamed"] = _sync_author_folder(db, pid, new_name, old_id=src)
            except Exception:
                res["folder_renamed"] = 0
            # 身份换了 → 文件也要跟着走 (绑定后文件还留在 #其他来源#/)
            try:
                _g = _gather_works_into_author_folder(db, res.get("artwork_ids") or [])
            except Exception as e:
                _g = {"moved": 0, "failed": [f"{type(e).__name__}: {e}"], "dests": []}
            res["files_gathered"] = int(_g.get("moved") or 0)
            res["gather_dests"] = _g.get("dests") or []
            res["gather_failed"] = _g.get("failed") or []
        audit_log.record("update", "作者绑定 pixiv ID",
                         f"{src} → pixiv {pid}（迁移 {res.get('artworks', 0)} 个作品）",
                         res.get("artworks", 0))
        return jsonify({"status": "ok", "author_id": pid, "name": new_name, **res})

    @app.route("/api/library/author-refresh", methods=["POST"])
    def api_library_author_refresh():
        """轻量刷新**单个作者** (定向刷新): 联网核对现用名 + 存续状态。

        编辑完作者还得等"全量自检"才会更新状态 —— 不合理。
        自检已经是"一锅端拆成小项"了, 但每一项仍是**全量**的 (③ 要逐个核对几百位作者)。
        这里只查这一个人: HTTP 直连 /ajax/user/ 一次请求, 毫秒级;
        名字变了顺带把磁盘文件夹按新名对齐。"""
        data = request.get_json(silent=True) or {}
        aid = str(data.get("author_id") or "").strip()
        if not aid:
            return jsonify({"status": "error", "error": "缺少 author_id"}), 400
        if not aid.isdigit():
            return jsonify({"status": "error",
                            "error": "本地作者没有 pixiv 账号可核对"
                                     "（可先在「编辑作者」里记上 pixiv 主页链接并绑定）"}), 400
        with _db() as db:
            a = db.get_author(aid)
            if a is None:
                return jsonify({"status": "error", "error": "作者不存在"}), 404
            sess = _pixiv_http_session()
            if sess is None:
                return jsonify({"status": "error",
                                "error": "没有可用的登录凭据，无法联网核对"
                                         "（到「设置」页登录 Pixiv 后再试）"}), 400
            from core.search_client import http_fetch_user_name
            old_name = str(a.get("name") or "")
            try:
                nm = http_fetch_user_name(sess, aid)
            except Exception as e:
                return jsonify({"status": "error",
                                "error": f"联网核对失败：{str(e).splitlines()[0]}"}), 502
            st = "alive" if nm else "gone"      # None = 账号已注销
            renamed = 0
            if nm:
                nm2 = _readable_author_name(db, aid, nm)
                if nm2 != old_name:
                    db.upsert_author(aid, nm2)
                    renamed = 1
            db.set_author_status(aid, st)
            new_name = str((db.get_author(aid) or {}).get("name") or old_name)
            folder = 0
            if renamed:
                try:
                    folder = _sync_author_folder(db, aid, new_name)
                except Exception:
                    folder = 0
        audit_log.record("update", "刷新作者状态",
                         f"作者 {aid}（{new_name}）："
                         + ("活跃中" if st == "alive" else "已注销")
                         + ("，并同步了新名字" if renamed else ""), 1)
        return jsonify({"status": "ok", "author_id": aid, "name": new_name,
                        "author_status": st, "renamed": renamed,
                        "folder_renamed": folder})

    def _reclassify_work_source(db, aid: str) -> int:
        """按已记录的 source_url 重新识别站点并写回 (纯本地, 不联网)。

        只处理"现在等于未知/other 或空"的作品 —— 用户已确认过的来源绝不改动。
        这是「定向刷新」里"来源"那部分的本地动作 (对外来源图尤其有用)。"""
        row = db.conn.execute(
            "SELECT source, source_url FROM artworks WHERE artwork_id=?",
            (str(aid),)).fetchone()
        if row is None:
            return 0
        cur = str(row[0] or "").strip().lower()
        url = str(row[1] or "").strip()
        if not url or cur not in ("", "other", "unknown", "未知"):
            return 0
        det = _classify_url(url)
        if not det or not det.get("site"):
            return 0
        from db.store import _SITE_TO_SOURCE
        site = _SITE_TO_SOURCE.get(str(det["site"]), str(det["site"]))
        kw = {"source": site}
        if det.get("handle"):
            kw["source_author"] = str(det["handle"])
        db.update_artwork(str(aid), **kw)
        return 1

    @app.route("/api/library/refresh-scope", methods=["POST"])
    def api_library_refresh_scope():
        """🎯 定向(增量)刷新: 只把**刚动过的那一小块**重新核对到正确, 不跑全量。

        "自检虽然拆成了小项, 但每一项仍是全量; 能不能在触动某项时
        只针对这部分做一次快速自检" —— 这个端点就是那件事的后端:
          scope="work"   ids=[作品ID…]  本地核对: 状态/页数重算 + 来源 URL 归类 + 磁盘文件在不在
          scope="author" ids=[作者ID…]  本地重算该作者名下作品 + 联网核对这几位作者(1 次 HTTP/人)
        只碰这几条记录, 毫秒级(联网部分几百毫秒); 联网失败不影响本地重算结果。"""
        data = request.get_json(silent=True) or {}
        scope = str(data.get("scope") or "").strip()
        ids = [str(x) for x in (data.get("ids") or []) if str(x).strip()]
        out = {"status": "ok", "scope": scope, "works": 0, "authors": 0,
               "source_classified": 0, "renamed": 0, "missing_files": [], "note": ""}
        if scope == "work":
            img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
            with _db() as db:
                for aid in ids:
                    if db.get_artwork_metadata(aid) is None:
                        continue
                    db._refresh_work_progress(aid)
                    out["source_classified"] += _reclassify_work_source(db, aid)
                    out["works"] += 1
                    rows = db.conn.execute(
                        "SELECT local_path FROM pages WHERE artwork_id=? "
                        "AND local_path IS NOT NULL AND local_path <> '' "
                        "AND trashed_at IS NULL", (aid,)).fetchall()
                    miss = [str(r[0]) for r in rows
                            if not (img_root / str(r[0]).replace("\\", "/")).is_file()]
                    if miss:
                        out["missing_files"].append({"artwork_id": aid, "files": miss})
            return jsonify(out)
        if scope == "author":
            sess = _pixiv_http_session()
            with _db() as db:
                for aid in ids:
                    if db.get_author(aid) is None:
                        continue
                    out["authors"] += 1
                    for r in db.conn.execute(
                            "SELECT artwork_id FROM artworks WHERE author_id=?",
                            (aid,)).fetchall():
                        wid = str(r[0])
                        db._refresh_work_progress(wid)
                        out["source_classified"] += _reclassify_work_source(db, wid)
                    if aid.isdigit() and sess is not None:
                        try:
                            from core.search_client import http_fetch_user_name
                            nm = http_fetch_user_name(sess, aid)
                            if nm:
                                nm2 = _readable_author_name(db, aid, nm)
                                if nm2 != str((db.get_author(aid) or {}).get("name") or ""):
                                    db.upsert_author(aid, nm2)
                                    out["renamed"] += 1
                                    try:
                                        _sync_author_folder(db, aid, nm2)
                                    except Exception:
                                        pass
                            db.set_author_status(aid, "alive" if nm else "gone")
                        except Exception as e:
                            out["note"] = f"联网核对失败：{str(e).splitlines()[0]}"
                    elif aid.isdigit():
                        out["note"] = "未登录，只做了本地核对（状态需联网）"
            return jsonify(out)
        out["note"] = "该范围前端刷新列表即可，无需后端处理"
        return jsonify(out)

    @app.route("/api/library/author-reconcile", methods=["POST"])
    def api_library_author_reconcile():
        """🗂 作者对账台: 左边"我库里来源未定的图" 右边"他 pixiv 主页的作品", 点一下就认领转正。

        场景: 一批外来源图被赋予了本地 ID (ext_xxx)、作者已手动指定成
        pixiv 画师, 但作品本身不是正式 pixiv 卡片。全站以图搜图(SauceNAO)慢且不稳,
        逐张翻 pixiv 又太贵 —— 候选集只要限定在**这个作者**就便宜得多。
        本接口只取数据不写库: pixiv 侧走 HTTP 直连(不开浏览器), 本地侧列出该作者名下
        "非纯数字 ID"的作品。"""
        data = request.get_json(silent=True) or {}
        aid = str(data.get("author_id") or "").strip()
        if not aid:
            return jsonify({"status": "error", "error": "缺少 author_id"}), 400
        if not aid.isdigit():
            return jsonify({"status": "error",
                            "error": "该作者还是本地作者（没有 pixiv 账号），"
                                     "请先在「编辑作者」里记上 pixiv 主页并绑定"}), 400
        limit = int(data.get("limit") or 500)
        with _db() as db:
            author = db.get_author(aid) or {}
            name = str(author.get("name") or aid)
            rows = db.conn.execute(
                """SELECT artwork_id, title, source, total_pages, author_id, is_r18,
                          (SELECT local_path FROM pages p WHERE p.artwork_id=a.artwork_id
                            AND p.local_path IS NOT NULL AND p.local_path <> ''
                            AND p.trashed_at IS NULL
                            ORDER BY p.page_num LIMIT 1) AS local_path,
                          (SELECT COUNT(*) FROM pages p WHERE p.artwork_id=a.artwork_id
                            AND p.trashed_at IS NULL) AS pages
                     FROM artworks a
                    WHERE a.author_id=? AND a.trashed_at IS NULL
                      AND NOT (a.artwork_id GLOB '[0-9]*' AND a.artwork_id NOT GLOB '*[^0-9]*')
                    ORDER BY a.artwork_id""", (aid,)).fetchall()
            locals_ = [{"artwork_id": str(r["artwork_id"]),
                        "title": r["title"] or "",
                        "source": r["source"] or "",
                        "local_path": r["local_path"] or "",
                        "pages": int(r["pages"] or 0),
                        "is_r18": int(r["is_r18"] or 0),
                        "total_pages": r["total_pages"]} for r in rows]
            # 库里已有的 pixiv 作品 (对账台上标"已在库", 避免重复认领)
            have = {str(x[0]) for x in db.conn.execute(
                "SELECT artwork_id FROM artworks WHERE author_id=? AND trashed_at IS NULL",
                (aid,))}
        sess = _pixiv_http_session()
        if sess is None:
            return jsonify({"status": "ok", "author": {"author_id": aid, "name": name},
                            "pixiv": [], "locals": locals_,
                            "note": "没有可用的登录凭据，拉不到 pixiv 作品列表"
                                    "（到「设置」页登录 Pixiv 后再试）；本地待认领图见左侧"})
        try:
            from core.search_client import http_fetch_user_works
            works = http_fetch_user_works(sess, aid, limit=limit)
        except Exception as e:
            return jsonify({"status": "error",
                            "error": f"拉取作者作品失败：{str(e).splitlines()[0]}",
                            "locals": locals_}), 502
        pixiv = [{"artwork_id": w.artwork_id, "title": w.title,
                  "page_count": int(getattr(w, "page_count", 1) or 1),
                  "thumb_url": (w.image_urls or [""])[0],
                  "is_r18": bool(getattr(w, "is_r18", False)),
                  "publish_date": getattr(w, "publish_date", None) or "",
                  "in_library": w.artwork_id in have}
                 for w in works]
        return jsonify({"status": "ok", "author": {"author_id": aid, "name": name},
                        "pixiv": pixiv, "locals": locals_, "note": ""})

    @app.route("/api/library/claim-image", methods=["POST"])
    def api_library_claim_image():
        """✅ 配对认领: 把一张"来源未定"的本地图挂到某个 pixiv 作品的第 N 页并转正。

        body: {local_artwork_id, target_artwork_id, page?, local_page?}
        做的事 (「作者对账台」的核心动作):
          ① 目标作品不在库 → 用 pixiv 元数据建壳 (标题/作者/页数/封面/标签…);
          ② 文件移进目标作品所在目录并规范命名为 `{pid}_p{N}{ext}`;
          ③ pages 写新行 (MD5/尺寸/大小一并落库) + 删掉本地那条;
          ④ 本地作品被搬空 → 删除该 ext_ 记录; 重算目标作品状态。
        页冲突(那页已有别的文件)会明确拒绝, 绝不覆盖。"""
        import shutil as _sh
        from utils.hashutil import md5_of
        data = request.get_json(silent=True) or {}
        lid = str(data.get("local_artwork_id") or "").strip()
        tid = str(data.get("target_artwork_id") or "").strip()
        if not lid or not tid:
            return jsonify({"status": "error", "error": "缺少 local_artwork_id / target_artwork_id"}), 400
        if not tid.isdigit():
            return jsonify({"status": "error", "error": "目标必须是 pixiv 作品的纯数字 ID"}), 400
        if lid == tid:
            return jsonify({"status": "error", "error": "本地作品与目标作品是同一个"}), 400
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        with _db() as db:
            lrow = db.conn.execute(
                """SELECT artwork_id, title, source, source_url FROM artworks
                    WHERE artwork_id=?""", (lid,)).fetchone()
            if lrow is None:
                return jsonify({"status": "error", "error": f"本地作品 {lid} 不存在"}), 404
            lp = db.conn.execute(
                """SELECT page_num, local_path, md5 FROM pages
                    WHERE artwork_id=? AND local_path IS NOT NULL AND local_path <> ''
                      AND trashed_at IS NULL
                    ORDER BY page_num""", (lid,)).fetchall()
            if not lp:
                return jsonify({"status": "error", "error": "本地作品没有已落盘的图片"}), 400
            local_page = int(data.get("local_page") or 0)
            pick = next((r for r in lp if int(r["page_num"]) == local_page), lp[0])
            src_rel = str(pick["local_path"]).replace("\\", "/")
            src = img_root / src_rel
            if not src.is_file():
                return jsonify({"status": "error",
                                "error": f"本地文件不存在：{src_rel}"}), 404
            # 目标作品: 不在库就先按 pixiv 元数据建壳
            tinfo = db.get_artwork_metadata(tid)
            if tinfo is None:
                sess = _pixiv_http_session()
                if sess is None:
                    return jsonify({"status": "error",
                                    "error": "目标作品不在库，需要联网取元数据"
                                             "（到「设置」页登录 Pixiv 后再试）"}), 400
                try:
                    from core.search_client import http_fetch_illust
                    art = http_fetch_illust(sess, tid)
                except Exception as e:
                    return jsonify({"status": "error",
                                    "error": f"取目标作品信息失败：{str(e).splitlines()[0]}"}), 502
                db.change_work_status(tid, WSt.NOT_YET, title=art.title,
                                      author_id=art.author_id or None,
                                      illust_type=art.illust_type.value,
                                      tags=list(art.tags), publish_date=art.publish_date,
                                      is_r18=art.is_r18,
                                      thumb_url=(art.image_urls or [None])[0],
                                      total_pages=int(getattr(art, "page_count", 1) or 1))
                if art.author_id and art.author_name:
                    db.upsert_author(art.author_id, art.author_name)
                tinfo = db.get_artwork_metadata(tid)
            # 目标页: 默认第一个空位; 显式指定则用指定的
            used = {int(r[0]) for r in db.conn.execute(
                "SELECT page_num FROM pages WHERE artwork_id=? AND status=?",
                (tid, PSt.DONE))}
            page = int(data["page"]) if str(data.get("page") or "").strip() else None
            if page is None:
                page = next((i for i in range(0, max(1, len(used) + 1)) if i not in used),
                            (max(used) + 1) if used else 0)
            if page in used:
                return jsonify({"status": "error",
                                "error": f"目标作品第 {page} 页已有文件，换个页号或先处理它"}), 409
            # 目标目录: 优先该作品已有页所在目录, 否则用作者夹
            tdir = None
            trow = db.conn.execute(
                """SELECT local_path FROM pages WHERE artwork_id=?
                    AND local_path IS NOT NULL AND local_path <> '' LIMIT 1""",
                (tid,)).fetchone()
            if trow:
                rel = str(trow[0]).replace("\\", "/")
                if "/" in rel:
                    cand = img_root / rel.rsplit("/", 1)[0]
                    if cand.is_dir():
                        tdir = cand
            if tdir is None:
                auid = str((tinfo or {}).get("author_id") or "")
                aname = ""
                if auid:
                    aname = str((db.get_author(auid) or {}).get("name") or "")
                tdir = img_root / (_author_folder_name(db, auid or "0", aname or "未知画师"))
            tdir.mkdir(parents=True, exist_ok=True)
            ext = src.suffix.lower() or ".png"
            target = tdir / f"{tid}_p{page}{ext}"
            if target.exists():
                return jsonify({"status": "error",
                                "error": f"目标文件已存在：{target.name}"
                                         f"（该页可能已归到别处）"}), 409
            try:
                _sh.move(str(src), str(target))
            except Exception as e:
                return jsonify({"status": "error",
                                "error": f"移动文件失败：{str(e).splitlines()[0]}"}), 500
            new_rel = target.relative_to(img_root).as_posix()
            try:
                from PIL import Image as _Image
                with _Image.open(target) as _im:
                    w_, h_ = _im.size
                fmd5 = md5_of(str(target))
                size = target.stat().st_size
            except Exception:
                w_ = h_ = None
                fmd5 = str(pick["md5"] or "") or None
                size = None
            # 超出 pixiv 原网页页数的认领页 = 用户手动并入的「附加页」: 标 origin='merged',
            # 以后可再拆出; 作品 total_pages 保持原值不被覆盖 (2026-09-17 需求)。
            tp = int((tinfo or {}).get("total_pages") or 0)
            is_extra = bool(tp) and page >= tp
            db.mark_page_status(tid, page, PSt.DONE, local_path=new_rel, md5=fmd5,
                                width=w_, height=h_, file_size_bytes=size,
                                origin=("merged" if is_extra else None))
            # 删本地那条 (delete_pages 自己会: 搬空则级联删作品 + 重算剩余状态; 只删库不删文件,
            # 文件已经被我们移走了, 所以它返回的旧路径无需处理)
            _rels, emptied = db.delete_pages(
                [{"artwork_id": lid, "page_num": int(pick["page_num"])}])
            removed_local = bool(emptied and lid in [str(x) for x in emptied])
            db._refresh_work_progress(tid)
        audit_log.record("update", "配对认领转正",
                         f"本地 {lid} → pixiv 作品 {tid} 第 {page + 1} 页（{new_rel}）"
                         + ("（附加页）" if is_extra else "")
                         + (f"，已删占位作品 {lid}" if removed_local else ""),
                         1, ids=[tid])
        return jsonify({"status": "ok", "target_artwork_id": tid, "page": page,
                        "new_path": new_rel, "local_removed": bool(removed_local),
                        "is_extra": is_extra, "total_pages": tp,
                        "target_title": (tinfo or {}).get("title") or ""})

    @app.route("/api/library/similar", methods=["POST"])
    def api_library_similar():
        """本地以图搜图: 上传一张图 → ORB+颜色融合 → 返回图库里最相似的 N 张。
        首次调用会先构建特征库 (扫描全部图片提取 ORB/颜色特征, 可能几分钟), 之后秒回。"""
        f = request.files.get("image")
        if not f or not f.filename:
            return jsonify({"status": "error", "message": "缺少图片"}), 400
        tmpdir = paths.user_data("tmp")
        os.makedirs(str(tmpdir), exist_ok=True)
        ext = os.path.splitext(f.filename)[1].lower()
        tmp = os.path.join(str(tmpdir), f"sim_query_{int(time.time() * 1000)}{ext or '.png'}")
        f.save(tmp)
        try:
            global _ls_searcher
            with _ls_lock:
                if _ls_searcher is None:
                    from core.local_search import ImageSearcher
                    from utils.reporter import PrintReporter
                    img_root = str(Path(paths.user_data(_config["paths"]["img_subdir"])))
                    _ls_searcher = ImageSearcher(library_path=img_root,
                                                 reporter=PrintReporter(), config=_config)
            results = _ls_searcher.search(tmp, top_k=12)
        except Exception as e:
            return jsonify({"status": "error",
                            "message": f"检索失败: {str(e).splitlines()[0]}"}), 500
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
        with _db() as db:
            items = []
            for r in results:
                rel = r.get("name") or ""
                if not isinstance(rel, str):  # 防御: npz 加载路径可能带 numpy 类型
                    rel = rel.decode('utf-8', 'replace') if isinstance(rel, bytes) else str(rel)
                rel = rel.replace("\\", "/")
                if not rel:
                    continue
                row = db.conn.execute(
                    "SELECT artwork_id FROM pages WHERE local_path=? LIMIT 1", (rel,)).fetchone()
                title = author = ""
                if row:
                    art = db.conn.execute(
                        "SELECT a.title, COALESCE(au.name, a.author_id) AS author_name "
                        "FROM artworks a LEFT JOIN authors au ON au.author_id = a.author_id "
                        "WHERE a.artwork_id=?", (str(row["artwork_id"]),)).fetchone()
                    if art:
                        title = art["title"] or ""
                        author = art["author_name"] or ""
                items.append({
                    "rel": rel,
                    "fusion_score": round(float(r.get("fusion_score") or 0), 4),
                    "orb_score": round(float(r.get("orb_score") or 0), 4),
                    "color_score": round(float(r.get("color_score") or 0), 4),
                    "artwork_id": str(row["artwork_id"]) if row else None,
                    "thumb": f"/api/local-img?p={quote(rel)}&thumb=1",
                    "title": title,
                    "author": author,
                })
        return jsonify({"status": "ok", "items": items})

    @app.route("/api/library/recent")
    def api_library_recent():
        limit = int(request.args.get("limit", 200))
        with _db() as db:
            return jsonify({"items": _lib_items(
                db.list_recent_artworks(limit=limit),
                name_fallback=db.queue_author_names())})

    @app.route("/api/library/search")
    def api_library_search():
        q = request.args.get("q", "").strip()
        with _db() as db:
            return jsonify({"items": _lib_items(
                db.search_artworks(q, limit=200),
                name_fallback=db.queue_author_names())})

    @app.route("/api/library/missing")
    def api_library_missing():
        with _db() as db:
            return jsonify({"count": db.count_missing_metadata()})

    @app.route("/api/library/items")
    def api_library_items():
        """图库通用查询: 筛选(q/类型/R18/作者/标签/缺元数据/缺文件) + 排序 + 分页。

        view=artwork (默认): 一个作品一行, 附带 pages(库内页清单);
        view=image: 一张图片一行 (pages 有本地文件的页), 图片视图用。"""
        args = request.args
        only_missing_file = args.get("file_missing", "") == "1"
        view = args.get("view", "artwork")
        # 「缺失文件」筛选时强制重扫对账, 保证结果即时准确
        missing_set = set(_cached_reconcile(force=only_missing_file).missing_ids or [])
        file_missing_ids = missing_set if only_missing_file else None
        common = dict(
            q=args.get("q", ""),
            illust_type=args.get("type", ""),
            is_r18=args.get("r18", ""),
            # 「AI 作品」筛选  '2' 只看 AI / '0' 只看非 AI / 空 = 全部
            ai=args.get("ai", ""),
            author_id=args.get("author_id", "") or None,
            tag=args.get("tag", "") or None,
            missing=args.get("missing", "") == "1",
            deleted=args.get("deleted", "") == "1",
            author_status=args.get("status", "") or None,
            source=args.get("source", "") or None,
            # 「仅查看该作品」 精确 ID 硬过滤, 覆盖其他条件
            only_aid=args.get("only", "") or None,
        )
        with _db() as db:
            if view == "image":
                items = db.list_images(
                    **common, sort=args.get("sort", "date"),
                    order=args.get("order", "desc"),
                    limit=int(args.get("limit", 200)),
                    offset=int(args.get("offset", 0)),
                )
                name_fallback = db.queue_author_names()
                total = db.count_images_filtered(**common)
                return jsonify({
                    "items": _with_dl_state(db, _lib_pages(items, name_fallback)),
                    "total": total, "view": "image",
                })
            items = db.list_artworks(
                **common, sort=args.get("sort", "date"),
                order=args.get("order", "desc"),
                limit=int(args.get("limit", 200)),
                offset=int(args.get("offset", 0)),
                file_missing_ids=file_missing_ids,
            )
            ids = [str(r["artwork_id"]) for r in items]
            local_map = db.get_first_local_paths(ids)
            page_info = db.get_first_page_info(ids)
            page_list = db.get_artwork_page_list(ids)
            name_fallback = db.queue_author_names()
            total = db.count_artworks_filtered(
                **common, file_missing_ids=file_missing_ids,
            )
            return jsonify({
                "items": _with_dl_state(db, _lib_items(
                    items, local_map, missing_set, name_fallback,
                    page_info, db, page_list)),
                "total": total, "view": "artwork",
            })

    @app.route("/api/library/sync", methods=["POST"])
    def api_library_sync():
        """图库 ↔ 磁盘实时对账 (前端进图库/刷新/手动同步/导入时调用)。

        以磁盘为准扫描: 识别手动删文件后残留的「缺失作品」、空目录、并补入磁盘新文件。
        对账完成后顺带清孤儿对象 (tags/authors/pages 残留), 保证顶部「作者」统计数字
        与实际作品一致——否则手动删完一个作者下全部作品后, authors 表残留行会让
        用户看到"明明没文件了还显示 3 个作者"的幻觉。

        periodic=1: 15s 定时轮询用, TTL 内不强制重扫磁盘 (图库大时全扫一次
        要遍历上千文件, 周期强扫会和图片加载抢磁盘 IO); 手动点同步/导入后
        仍 force 即时对账。
        """
        periodic = request.args.get("periodic", "") == "1"
        rep = _cached_reconcile(force=not periodic)
        resp = _reconcile_to_json(rep)
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        if periodic:
            # 15s 定时轮询**只读**: 以前这里每轮都写库 (补作者表 + 清孤儿), 与自检/元数据
            # 补全这类长任务抢写锁 —— SQLite 单写者, 长任务持锁时轮询就报
            # `OperationalError: database is locked` (日志连报 4 次)。
            # 定时轮询本来只为「磁盘↔图库」状态显示, 写操作留给手动同步/导入/自检, 省 IO。
            resp["authors_adopted"] = 0
            resp["orphans_removed"] = 0
            return jsonify(resp)
        try:
            with _db() as db:
                # 文件夹名「作者名_ID」离线补 authors 表, 图库作者列直接显示名称
                resp["authors_adopted"] = _adopt_authors_from_folders(db, img_root)
                orph = db.cleanup_orphans() or {}
            resp["orphans_removed"] = sum(int(v) for v in orph.values())
        except sqlite3.OperationalError as e:
            # 后台长任务 (自检/批量下载/元数据补全) 正持写锁时, 手动同步也不该 500:
            # 对账结果照常返回, 只是这轮没写库, 提示用户稍后再点一次即可
            resp["authors_adopted"] = 0
            resp["orphans_removed"] = 0
            resp["write_skipped"] = str(e).splitlines()[0]
        # 磁盘上无 pixiv ID 的其他来源文件 (danbooru/推特/GGAC/贴吧等) 登记入库,
        # 纳入图库统一管理 (title=文件夹/文件名, source 启发式识别)。
        # periodic 轮询不做 (省 IO), 手动同步/导入/对账发现新文件时登记。
        if not periodic:
            try:
                IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
                disk_rels = [p.relative_to(img_root).as_posix()
                             for p in img_root.rglob("*")
                             if p.is_file() and p.suffix.lower() in IMG_EXT]
                with _db() as db:
                    resp["foreign_registered"] = db.register_foreign_files(
                        db.foreign_unregistered_files(disk_rels))
                    # 按文件夹命名 (bili@/twi@/GGAC@ 等) 把「其他来源」细分到具体站点
                    resp["sources_refined"] = db.refresh_foreign_sources()
            except Exception:
                resp["foreign_registered"] = 0
        # 对账发现新入库图片 → 后台联网补标题/标签/作者 (导入即完整, 不等自检)
        if rep.new_pages_linked > 0:
            _kick_metadata_fill()
        return jsonify(resp)

    @app.route("/api/library/health")
    def api_library_health():
        """深度健康检查: 在普通对账基础上, 额外对每个图片做 PIL 打开校验, 识别损坏文件。"""
        img_root = paths.user_data(_config["paths"]["img_subdir"])
        with _db() as db:
            rep = db.reconcile_output(img_root, verify_corrupt=True)
        return jsonify(_reconcile_to_json(rep))

    @app.route("/api/local-img")
    def api_local_img() -> "Response | tuple[str, int]":
        """本地图片直读代理 (图库缩略图本地优先)。p 为相对 data/img 的路径, 含路径穿越防护。

        thumb=1: 返回 ~480px JPEG 缩略图 (data/thumbs/ 磁盘缓存, 首次生成后零成本);
        图库/反搜网格一律走缩略图, 不传 thumb 才给原图 (详情/打开文件夹场景)。"""
        p = request.args.get("p", "")
        root_key = request.args.get("root", "img")  # img=图库根(默认); input=反搜输入目录; trash=应用内回收站
        if not p:
            return "", 400
        if root_key == "input":
            root_abs = Path(paths.user_data(_config["paths"].get("input_subdir", "input"))).resolve()
        elif root_key == "trash":
            # 回收站里的图 (软删除已把文件搬过去) —— 回收站页要能显示缩略图,
            # 否则用户认不出自己删的是哪张。
            root_abs = _trash_root().resolve()
        elif root_key == "wall":
            # 壁纸夹里的图 (当前壁纸所在文件夹) —— 「当前壁纸」面板显示桌面那张的缩略图用。
            # 根目录由服务端从注册表现算, 客户端只给夹内文件名 → 没有任意路径读取的口子
            # (路径穿越仍由下面的 parents 校验兜住)。2026-09-24
            wf = _wallpaper_file()
            if wf is None:
                return "", 404
            root_abs = wf.parent.resolve()
        else:
            root_abs = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()
        f = (root_abs / p).resolve()
        if f != root_abs and root_abs not in f.parents:  # 防路径穿越
            return "", 403
        thumb_req = request.args.get("thumb", "") == "1"
        if not f.is_file():
            # 源文件缺失: 若生成过「稳定缩略图」则回退返回 (用户能认出删掉的是哪张图)
            if thumb_req:
                st = _stable_thumb_path(root_key, p)
                if st.is_file():
                    return send_file(str(st), mimetype="image/jpeg",
                                     conditional=True, max_age=30 * 86400)
            return "", 404
        # 限缩略图常见图片类型
        if f.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return "", 403
        if thumb_req:
            tc = _get_thumb(f, root_key, p)
            if tc is not None:
                # 键含 mtime+大小, 内容变了 URL 对应文件自然变 → 长缓存
                return send_file(str(tc), mimetype="image/jpeg",
                                 conditional=True, max_age=30 * 86400)
            # 生成失败则回退原图, 绝不留白
        try:
            return send_file(str(f), conditional=True, max_age=3600)
        except Exception:
            return "", 404

    # ---------------- 🖼 当前壁纸 (桌面幻灯片) ----------------
    def _wall_info_payload(wall_path) -> "dict | None":
        """按 ``/api/wallpaper/current`` 的口径取某张壁纸的完整 info (含 ✨ 文案)。

        2026-09-29: next/prev/recycle 换完壁纸**就地** trace 一份塞进响应 —— 桌面小控件
        点「下一张」本来还要再拉一轮 current (一次 HTTP 往返 + md5/尺寸全套重算), 现在
        响应里就是成品, 客户端拿去直接换卡, 链路短一截。
        """
        try:
            info = trace_current_wallpaper(
                deep=False,
                wall_path=wall_path,
                db_factory=_db,
                img_root=Path(paths.user_data(_config["paths"]["img_subdir"])).resolve(),
                img_url_fn=_local_img_url,
                orb_fn=_wall_orb_candidates,
            )
            from core.ai_text import texts_for
            info["ai"] = texts_for(info)
            return info
        except Exception:
            return None

    def _notify_widget() -> None:
        """桌面小控件的主动通知: 别等它 8 秒轮询, 现在就去拉一轮。

        换壁纸 / 文案生成完之后调用。``refresh`` 只是起个后台线程取数再投消息,
        不碰窗口线程, 在 Flask 请求线程里调是安全的; 小控件没开 (或旧版进程) 时
        模块里 ``_ACTIVE_WIDGET`` 是 None, 什么都不发生。
        """
        try:
            import utils.widget_window as _ww
            w = getattr(_ww, "_ACTIVE_WIDGET", None)
            if w is not None:
                w.refresh()
        except Exception:
            pass

    @app.route("/api/wallpaper/current")
    def api_wallpaper_current():
        """现在桌面播的是哪一张 + 它在图库里的来龙去脉 (只读, 不动任何文件)。

        deep=1 才跑 ORB 模糊溯源 (首次要加载整库特征, 可能几秒~几十秒);
        默认只走「文件就在图库内 / MD5 字节一致 / 文件名作品ID」这三条秒回的路。"""
        deep = request.args.get("deep", "") == "1"
        info = trace_current_wallpaper(
            deep=deep,
            wall_path=_wallpaper_file(),      # 读注册表; 传死路径即可离线跑 (冒烟 T130)
            db_factory=_db,                   # 每次新建连接 (SQLite 连接绑定创建线程)
            img_root=Path(paths.user_data(_config["paths"]["img_subdir"])).resolve(),
            img_url_fn=_local_img_url,
            orb_fn=_wall_orb_candidates,      # 复用本文件的整库检索器单例, 不加载两份
        )
        # ✨ 壁纸文案  附上「标题 + 氛围句」。**只读缓存, 从不在这里发请求** ——
        # 生成是设置页那颗「上传当前壁纸」按钮手动触发的 (换壁纸不自动调接口)。
        # 小控件 8 秒轮询一次, 靠 info_key 里的 ai_sig 发现「文案到了」再重画卡片。
        try:
            from core.ai_text import texts_for
            info["ai"] = texts_for(info)
        except Exception as e:
            info["ai"] = {"state": "off", "error": f"{type(e).__name__}: {e}"}
        return jsonify(info)

    @app.route("/api/wallpaper/next", methods=["POST"])
    def api_wallpaper_next():
        """跳到壁纸夹里的下一张 (按文件名自然排序, 到底回到第一张)。

        Windows 没给「让幻灯片走下一张」的公开接口 (`shell:::{…}`、Shell 动词、CommandStore
        的 slideshow 委托都试过, 换了也不动), 所以这里自己挑下一张再用 SPI_SETDESKWALLPAPER
        设上去。**只换当前显示哪张**, 幻灯片的间隔/乱序/目录一律不动 (实测 BackgroundType 保持 2)。

        ``?shuffle=1`` 时**不按顺序**, 从候选夹里随机挑一张 (小控件上的「顺序/随机」键走这个参数)。

        注册表里那张已经不存在时**不再直接报错** —— 只要配了壁纸夹, 就从夹子的一端起头
        (2026-09-26 修: 那张壁纸被移出后注册表值不会自己更新, 之前这里一律 400, 表现就是
        点「下一张」也没有响应)。
        """
        cur = _wallpaper_file()
        nxt = next_wallpaper(cur, _shuffle_arg())
        if nxt is None:
            return jsonify({"status": "error",
                            "msg": "没找到可换的图（壁纸夹里只有这一张，或还没指定壁纸夹）"}), 400
        if not apply_wallpaper(nxt):
            return jsonify({"status": "error", "msg": "系统没接受这次换壁纸"}), 500
        _notify_widget()
        return jsonify({"status": "ok", "file": str(nxt), "name": nxt.name,
                        "info": _wall_info_payload(nxt)})

    @app.route("/api/wallpaper/prev", methods=["POST"])
    def api_wallpaper_prev():
        """跳到壁纸夹里的上一张 (按文件名自然排序, 到顶回到最后一张)。

        与 ``/api/wallpaper/next`` 同一份规则 (``core.wallpaper_trace._step_wallpaper``),
        只是步长取 -1 —— 桌面上小控件的「上一张」键走这里; ``?shuffle=1`` 同 next (随机)。
        """
        cur = _wallpaper_file()
        prv = prev_wallpaper(cur, _shuffle_arg())
        if prv is None:
            return jsonify({"status": "error",
                            "msg": "没找到可换的图（壁纸夹里只有这一张，或还没指定壁纸夹）"}), 400
        if not apply_wallpaper(prv):
            return jsonify({"status": "error", "msg": "系统没接受这次换壁纸"}), 500
        _notify_widget()
        return jsonify({"status": "ok", "file": str(prv), "name": prv.name,
                        "info": _wall_info_payload(prv)})

    @app.route("/api/wallpaper/dir")
    def api_wallpaper_dir_get():
        """壁纸夹设置 (只读): 用户配的那个 + 现在**实际生效**的那个。

        配置项 ``paths.wallpaper_dir`` 是 2026-09-26 加的: 系统幻灯片的壁纸夹是在 Windows
        设置里选的, 应用看不到 —— 于是「上一张/下一张/幻灯片」只能猜「当前这张所在文件夹」。
        配了就听用户的。``effective`` 是回退之后的最终目录, 界面直接显示它更直观。
        """
        from core.wallpaper_trace import candidates_dir, wallpaper_dir
        cur = _wallpaper_file()
        eff = candidates_dir(cur)
        return jsonify({"dir": str(wallpaper_dir() or ""),
                        "effective": str(eff or "")})

    @app.route("/api/wallpaper/dir", methods=["POST"])
    def api_wallpaper_dir_set():
        """改壁纸夹 (空字符串 = 清除, 回到「当前壁纸所在文件夹」这条默认口径)。

        存之前先验: 路径必须真实存在且是目录 —— 存一个不存在的路径进去, 之后「下一张」永远
        提示没有别的图可换, 还得自己回想是哪里打错了。
        """
        data = request.get_json(silent=True) or {}
        raw = str(data.get("dir") or "").strip().strip('"')
        if raw:
            p = Path(raw)
            if not p.is_dir():
                return jsonify({"ok": False, "error": "这个路径不是有效文件夹"}), 400
            raw = str(p)
        from utils.config import save_user_config
        try:
            save_user_config({"paths": {"wallpaper_dir": raw}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        from core.wallpaper_trace import candidates_dir, wallpaper_dir
        cur = _wallpaper_file()
        eff = candidates_dir(cur)
        return jsonify({"ok": True, "dir": str(wallpaper_dir() or ""),
                        "effective": str(eff or "")})

    @app.route("/api/wallpaper/dir/pick", methods=["POST"])
    def api_wallpaper_dir_pick():
        """弹**系统「选择文件夹」对话框**挑壁纸夹, 选中即存; 取消则什么都不改。

        早期界面是「保存 + 读取」两个按钮, 且输入框空着时点保存等于什么都没做,
        现已合并为单个按钮。
        浏览器拿不到用户选中目录的绝对路径 (``<input webkitdirectory>`` 只给相对路径), 所以
        「选目录」只能由服务端进程弹原生对话框来完成, 见 ``utils/folder_dialog.pick_folder``。

        对话框是**阻塞**的: 会占住当前请求线程直到用户选完 (dev server 是 threaded, 不影响
        其他请求)。选中即落配置, 不需要用户再点一次保存。
        """
        from utils.folder_dialog import pick_folder
        from core.wallpaper_trace import candidates_dir, wallpaper_dir
        cur = _wallpaper_file()
        start = wallpaper_dir() or candidates_dir(cur)
        try:
            picked = pick_folder("选择壁纸文件夹", initial=str(start) if start else None)
        except Exception as e:
            # 这条路全是 ctypes/COM 调用, 任一环节失败都**必须是 JSON** 而不是 Flask 的 500 页:
            # 2026-09-26 真机上 SHBrowseForFolder 的结构体字段赋值抛 TypeError, 前端因为拿不到
            # error 字段, 把这次失败当成"用户取消"接着往下走 → 反而把输入框清空了。
            return jsonify({"ok": False, "error": f"弹不出系统对话框: {e}"}), 500
        if not picked:
            return jsonify({"ok": False, "cancelled": True,
                            "dir": str(wallpaper_dir() or ""),
                            "effective": str(candidates_dir(cur) or "")})
        from utils.config import save_user_config
        try:
            save_user_config({"paths": {"wallpaper_dir": picked}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "saved": True, "dir": str(wallpaper_dir() or ""),
                        "effective": str(candidates_dir(cur) or "")})

    @app.route("/api/wallpaper/open-folder", methods=["POST"])
    def api_wallpaper_open_folder():
        """在资源管理器里定位当前壁纸文件 (路径全部由服务端从注册表读, 客户端不传路径)。"""
        f = _wallpaper_file()
        if f is None:
            return jsonify({"status": "error", "msg": "读不到当前壁纸路径"}), 400
        try:
            subprocess.Popen(f'explorer /select,"{f}"', shell=True)
            return jsonify({"status": "ok", "path": str(f)})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)}), 500

    @app.route("/api/wallpaper/recycle", methods=["POST"])
    def api_wallpaper_recycle():
        """把当前这张壁纸送进 **Windows 电脑回收站** (可还原)。

        只动壁纸夹里这**一个**文件: 图库、数据库改都不改 —— 用户嫌这张当壁纸不好看,
        删壁纸不该连带把库里的收藏也删掉。文件本身就在图库内时**拒绝执行**
        (那属于"从图库删", 走图库的软删除流程, 才有记录和恢复)。

        **删之前先把桌面切到下一张**: 只删图片文件并不会换掉当前桌面壁纸。原因: 桌面显示的是**已经加载进内存的位图**,
        源文件没了它照样挂着不动 —— 只有 ``SPI_SETDESKWALLPAPER`` 指到另一个文件才会真的换。
        所以顺序必须是「先 apply 下一张, 再把旧文件送回收站」; 壁纸夹里只剩这一张时切不了,
        那就只删 (桌面会继续显示这张, 直到用户自己换)。
        """
        f = _wallpaper_file()
        if f is None:
            return jsonify({"status": "error", "msg": "读不到当前壁纸路径"}), 400
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()
        if (f == img_root) or (img_root in f.parents):
            return jsonify({"status": "error",
                            "msg": "这张壁纸就是图库里的文件，请在图库里删除（会进应用内回收站）"}), 400
        nxt = next_wallpaper(f)
        switched = bool(nxt is not None and apply_wallpaper(nxt))
        n, bad = _recycle_files([str(f)])
        if n <= 0:
            return jsonify({"status": "error",
                            "msg": "没能送进回收站" + (f"：{bad[0]}" if bad else "")}), 500
        try:
            audit_log.record("delete", "移出壁纸",
                             f"把壁纸 {f.name} 送进电脑回收站（可还原）"
                             + (f"，桌面已切到 {nxt.name}" if switched else ""))
        except Exception:
            pass
        _notify_widget()
        return jsonify({"status": "ok", "file": str(f), "recycled": n,
                        "switched": switched, "next": str(nxt) if switched else "",
                        "info": _wall_info_payload(nxt) if switched else None})

    # ---------------- 🧹 壁纸副本清理 (「省空间」) ----------------
    @app.route("/api/wallpaper/dupes")
    def api_wallpaper_dupes():
        """上一次「扫副本」的状态与结果 (只读)。

        status: ``idle`` 还没扫过 / ``scanning`` 正在扫 / ``done`` / ``error``。
        扫的过程中 ``done/total/current`` 就是进度。
        """
        with _WALL_DUPES_LOCK:
            return jsonify(dict(_WALL_DUPES))

    @app.route("/api/wallpaper/dupes/scan", methods=["POST"])
    def api_wallpaper_dupes_scan():
        """扫壁纸夹里与库内页 MD5 一致的副本 (后台跑, 立刻返回)。

        7.4 GB 全量算指纹要几十秒, 放在请求里等不起 —— 进度与结果看 ``GET /api/wallpaper/dupes``。
        第二次点会直接复用 (同一时间只跑一个扫描)。
        """
        cur = _wallpaper_file()
        if cur is None:
            return jsonify({"status": "error", "msg": "读不到当前壁纸路径"}), 400
        with _WALL_DUPES_LOCK:
            if _WALL_DUPES["status"] == "scanning":
                return jsonify({"status": "ok", "already": True})
            _WALL_DUPES.update({"status": "scanning", "done": 0, "total": 0,
                                "current": "", "error": "", "result": None})
        wall_dir = candidates_dir(cur)      # 用户指定的壁纸夹优先, 没配才是当前壁纸所在文件夹

        def _task():
            try:
                def _prog(done, total, name):
                    with _WALL_DUPES_LOCK:
                        _WALL_DUPES.update({"done": done, "total": total, "current": name})

                res = find_wall_dupes(wall_dir, _db, progress_cb=_prog)
            except Exception as e:
                with _WALL_DUPES_LOCK:
                    _WALL_DUPES.update({"status": "error", "error": str(e), "current": ""})
                return
            with _WALL_DUPES_LOCK:
                _WALL_DUPES.update({"status": "done", "at": time.time(),
                                    "current": "", "result": res})

        threading.Thread(target=_task, daemon=True, name="wall-dupes").start()
        return jsonify({"status": "ok"})

    @app.route("/api/wallpaper/dupes/recycle", methods=["POST"])
    def api_wallpaper_dupes_recycle():
        """把勾选的副本送进 **Windows 电脑回收站** (可还原)。

        安全边界 (用户"绝不自动删"的要求就落在这三条上):
          ① 只接受**相对文件名**, 解析后必须仍落在当前壁纸夹内 —— 挡 `../` 与绝对路径;
          ② 落在图库目录里的一律拒绝 (那属于"从图库删", 走图库的软删除才有记录);
          ③ 一条都没通过就整体不执行, 绝不"部分成功"。
        """
        data = request.get_json(silent=True) or {}
        names = [str(n) for n in (data.get("names") or []) if str(n or "").strip()]
        if not names:
            return jsonify({"status": "error", "msg": "没有勾选任何一项"}), 400
        cur = _wallpaper_file()
        if cur is None:
            return jsonify({"status": "error", "msg": "读不到当前壁纸路径"}), 400
        wall_dir = candidates_dir(cur).resolve()
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()
        targets, refused = [], []
        for n in names:
            p = Path(n)
            if p.is_absolute():
                refused.append(f"{n}: 只接受壁纸夹里的文件名")
                continue
            try:
                abs_f = (wall_dir / p).resolve()
            except Exception:
                refused.append(f"{n}: 路径解析失败")
                continue
            if abs_f.parent != wall_dir:
                refused.append(f"{n}: 不在当前壁纸夹内")
                continue
            if abs_f == img_root or img_root in abs_f.parents:
                refused.append(f"{n}: 这是图库里的文件，请在图库里删除")
                continue
            if not abs_f.is_file():
                refused.append(f"{n}: 文件已不在")
                continue
            targets.append((n, abs_f))
        if not targets:
            return jsonify({"status": "error",
                            "msg": "没有一项可以删：" + "；".join(refused[:5])}), 400
        pre = {}
        for n, p in targets:
            try:
                pre[n] = p.stat().st_size
            except OSError:
                pre[n] = 0
        n_ok, bad = _recycle_files([str(p) for _n, p in targets])
        bad_set = set(bad)
        gone = {n for n, p in targets if str(p) not in bad_set}
        freed = sum(pre.get(n, 0) for n in gone)
        # 扫描结果里把已删的那几条摘掉, 面板不用重扫也能对上号
        with _WALL_DUPES_LOCK:
            res = _WALL_DUPES.get("result")
            if isinstance(res, dict) and res.get("items"):
                keep = [it for it in res["items"] if it.get("name") not in gone]
                res["bytes"] = sum(int(it.get("size") or 0) for it in keep)
                res["count"] = len(keep)
                res["items"] = keep
        try:
            audit_log.record("delete", "清理壁纸副本",
                             f"把 {n_ok} 个与图库字节一致的壁纸副本送进电脑回收站"
                             f"（可还原）；释放 {freed / 1048576:.1f} MB")
        except Exception:
            pass
        return jsonify({"status": "ok", "recycled": n_ok, "freed": freed,
                        "refused": refused, "failed": bad})

    # ---------------- 🖼 壁纸库 (从图库生成, 2026-09-27) ----------------
    @app.route("/api/wallpaper/lib")
    def api_wallpaper_lib():
        """上一次「从图库生成壁纸」的状态与结果 (只读)。

        status: ``idle`` 还没跑过 / ``running`` 正在跑 / ``done`` / ``error``。
        跑的过程中 ``done/total/current`` 就是进度。
        """
        with _WALL_LIB_LOCK:
            return jsonify(dict(_WALL_LIB))

    @app.route("/api/wallpaper/lib/scan", methods=["POST"])
    def api_wallpaper_lib_scan():
        """扫图库 → 把够格当桌面的页复制进壁纸夹并登记 (后台跑, 立刻返回)。

        规则与「为什么是手动按钮」见 ``core/wallpaper_lib.py``。上千行判定 + 几百 MB 的复制
        放在请求里等不起, 进度与结果看 ``GET /api/wallpaper/lib``。

        **没配壁纸夹时先在这里挡住**: 生成的目标夹必须由用户明确指定 (设置页那颗「选择文件夹」),
        不能回退成「当前壁纸所在文件夹」—— 那等于把几百张图倒进用户的系统壁纸夹, 猜错了收不回来。
        """
        from core.wallpaper_trace import wallpaper_dir
        d = wallpaper_dir()
        if d is None:
            return jsonify({"ok": False,
                            "error": "还没指定壁纸夹（设置 → 桌面与壁纸 → 壁纸夹）"}), 400
        with _WALL_LIB_LOCK:
            if _WALL_LIB["status"] == "running":
                return jsonify({"ok": True, "already": True})
            _WALL_LIB.update({"status": "running", "done": 0, "total": 0,
                              "current": "", "error": "", "result": None})
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()

        def _task():
            from core.wallpaper_lib import generate
            try:
                def _prog(done, total, name):
                    with _WALL_LIB_LOCK:
                        _WALL_LIB.update({"done": done, "total": total, "current": name})

                db = _db()
                try:
                    res = generate(db, wall_dir=d, img_root=img_root, progress_cb=_prog)
                finally:
                    db.close()
            except Exception as e:
                with _WALL_LIB_LOCK:
                    _WALL_LIB.update({"status": "error", "current": "",
                                      "error": f"{type(e).__name__}: {e}"})
                return
            with _WALL_LIB_LOCK:
                _WALL_LIB.update({"status": "done", "at": time.time(),
                                  "current": "", "result": res})
            try:
                audit_log.record("add", "生成壁纸",
                                 f"从图库生成 {res['added']} 张壁纸到 {res['wall_dir']}"
                                 f"（过门槛 {res['passed']} 张，已有 {res['skipped_exist']} 张）")
            except Exception:
                pass

        threading.Thread(target=_task, daemon=True, name="wall-lib").start()
        return jsonify({"ok": True, "dir": str(d)})

    @app.route("/api/library/open-folder", methods=["POST"])
    def api_library_open_folder():
        """在资源管理器中打开图片所在文件夹并定位文件 (文件管理器式管理)。"""
        data = request.get_json(silent=True) or {}
        p = str(data.get("path") or "")
        if not p:
            return jsonify({"status": "error", "msg": "缺少 path"}), 400
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()
        f = Path(p)
        # 相对路径 (整理预览 from 等) → 按图库根解析; 含 .. 的会被 parents 校验拦下
        if not f.is_absolute():
            f = (img_root / f).resolve()
        # 安全校验: 只允许打开图库根目录内的文件
        if f != img_root and img_root not in f.parents:
            return jsonify({"status": "error", "msg": "路径不在图库目录内"}), 403
        if f.is_dir():
            target = f
        elif f.is_file():
            target = f
        else:
            # 文件已不存在(被手动删了): 退化为打开图库根目录
            target = img_root
        try:
            if target.is_file():
                subprocess.Popen(f'explorer /select,"{target}"', shell=True)
            else:
                os.startfile(str(target))
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)}), 500

    @app.route("/api/library/clean-missing", methods=["POST"])
    def api_library_clean_missing():
        """一键清理所有「文件缺失」幽灵记录 (手动删了文件、库里的残留)。"""
        rep = _cached_reconcile(force=True)
        ids = list(rep.missing_ids or [])
        if not ids:
            return jsonify({"status": "ok", "cleaned": 0, "files_removed": 0})
        files_removed = 0
        img_root = paths.user_data(_config["paths"]["img_subdir"])
        label_txt = _labels_inline(ids)   # 必须在删库前取: 删完就查不到标题/作者了
        with _db() as db:
            rel_paths = db.get_artwork_local_paths(ids)
            cleaned = db.delete_artworks(ids)
        for rel in rel_paths:
            try:
                f = Path(img_root) / rel
                if f.is_file():
                    f.unlink(missing_ok=True)
                    files_removed += 1
            except Exception:
                continue
        if cleaned:
            audit_log.record("delete", "清理缺失记录",
                             f"清理 {cleaned} 个「磁盘文件已丢失」的作品记录：{label_txt}"
                             + (f"（同时删除残留文件 {files_removed} 个）" if files_removed else ""),
                             cleaned, ids=ids)
        return jsonify({"status": "ok", "cleaned": cleaned, "files_removed": files_removed})

    @app.route("/api/library/clean-empty-dirs", methods=["POST"])
    def api_library_clean_empty_dirs():
        """删除扫描到的空文件夹 (只删 reconcile 判定「无任何图片文件」的作者目录)。

        注意: reconcile 判定空目录 =「无子目录链深处有图片文件」, 但里面可能有
        Thumbs.db / .DS_Store / 子空目录 / 配置文件等杂物, 所以必须用
        shutil.rmtree 而不是 d.rmdir() (后者严格空才能删, 会吞错导致按钮失效)。
        为安全起见, 删除前再次核对: 真的不含任何图片文件才动手。
        """
        import shutil
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        removed = 0
        failed = 0
        if img_root.is_dir():
            # 先叶子后根, 保证能递归清掉嵌套空目录
            dirs = [d for d in img_root.rglob('*') if d.is_dir()]
            dirs.sort(key=lambda d: str(d).count(os.sep), reverse=True)
            for d in dirs:
                if not d.exists():
                    continue
                if any(f.is_file() and f.suffix.lower() in IMG_EXT for f in d.rglob('*')):
                    continue   # 有图片, 不删
                try:
                    shutil.rmtree(str(d))
                    removed += 1
                except OSError:
                    failed += 1
                    continue
        _cached_reconcile(force=True)  # 刷新对账缓存
        if removed:
            audit_log.record("delete", "删除空文件夹",
                             f"删除 {removed} 个不含任何图片的文件夹"
                             + (f"（{failed} 个被占用跳过）" if failed else ""),
                             removed)
        return jsonify({"status": "ok", "removed": removed, "failed": failed})

    @app.route("/api/library/facets")
    def api_library_facets():
        """分类数据: 全部标签 + 全部作者 + 实际来源 (下拉筛选/分类浏览用)。"""
        with _db() as db:
            return jsonify({
                "tags": db.list_all_tags(),
                "authors": db.list_authors(),
                "sources": db.list_sources(),
            })

    @app.route("/api/library/deleted-flag", methods=["POST"])
    def api_library_deleted_flag():
        """手动纠正「作品页已失效」这个客观标记 (自动标记来自联网补全时的 404)。

        body: {"artwork_id": "...", "deleted": true|false}
        只对 pixiv 数字作品 ID 有效 —— 非 pixiv 作品(ext_/merged_)没有 pixiv 作品页。
        """
        data = request.get_json(silent=True) or {}
        aid = str(data.get("artwork_id") or "").strip()
        if not aid:
            return jsonify({"status": "error", "message": "缺少 artwork_id"}), 400
        deleted = bool(data.get("deleted"))
        if not aid.isdigit():
            return jsonify({"status": "error",
                            "message": "只有 pixiv 数字作品 ID 才有「作品页是否失效」的概念"}), 400
        with _db() as db:
            n = db.mark_artwork_deleted(aid, deleted)
            exists = bool(db.get_artwork_metadata(aid))
        if not exists:
            return jsonify({"status": "error", "message": f"作品 {aid} 不存在"}), 404
        audit_log.record("update", "标记作品已失效" if deleted else "撤销作品已失效",
                         f"{_labels_inline([aid])}："
                         + ("作品页已确认不存在（不再参与联网补全）" if deleted
                            else "撤销失效标记，恢复参与联网补全"),
                         1, ids=[aid])
        return jsonify({"status": "ok", "deleted": deleted, "changed": int(n or 0)})

    @app.route("/api/library/delete", methods=["POST"])
    def api_library_delete():
        """批量删除作品 → **移入应用内回收站**(软删除), 一条 DB 记录都不删。

        body: {"ids": ["123", "456", ...]}

        语义变更 (双层保护): 旧实现是 `DELETE FROM` + 文件 unlink,
        误删一个作品就再也找不回 —— 太危险。现在这里只做两件可逆的事:
          ① DB 打 trashed_at 标记 → 图库立即不再显示它;
          ② 图片文件从 img/ 搬进 data_root/trash/ (还在本机, 用户随时能拿回)。
        真正的"卸载记录"只发生在回收站的「彻底删除」, 且那时文件也只送 **Windows
        电脑回收站** + 写操作日志留痕 —— 全程有退路。"""
        data = request.get_json(silent=True) or {}
        ids = [str(x) for x in (data.get("ids") or []) if str(x)]
        if not ids:
            return jsonify({"status": "ok", "deleted": 0, "files_removed": 0})
        label_txt = _labels_inline(ids)   # 必须在软删前取: 记录还在但列表已不显示它
        with _db() as db:
            res = db.soft_delete_artworks(ids)
        # DB 标记完成后再搬文件 (DB 是真源: 搬失败也只报告, 不回滚标记, 让用户能重试)
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        moved, bad = _apply_rel_moves(res["rel_moves"], img_root, _trash_root())
        if res["count"]:
            audit_log.record("update", "移入回收站",
                             f"把 {res['count']} 个作品移入回收站（可恢复）：{label_txt}"
                             + (f"；图片已搬入回收站 {moved} 张" if moved else "")
                             + (f"；{len(bad)} 张搬运失败" if bad else ""),
                             res["count"], ids=ids)
        return jsonify({"status": "ok", "deleted": res["count"], "trashed": True,
                        "files_removed": moved, "move_errors": bad[:5]})

    @app.route("/api/library/delete-pages", methods=["POST"])
    def api_library_delete_pages():
        """图片视图: 把选中的单张图片**移入应用内回收站**(软删除)。

        body: {"items": [{"artwork_id": "123", "page_num": 0}, ...],
               "cascade_choice": "keep" | "trash"   (可选)}

        级联语义: 单图软删**不会**把整个作品也拖进回收站 ——
        删一张图就只是少一张图。但若这会导致某作品变成 0 个有效页, 先**不改任何东西**、
        返回 need_choice 让用户选:
          · "keep"  → 作品保留在图库(显示 0 页), 只删图;
          · "trash" → 作品也一并进回收站(trashed_reason='cascade')。
        画线原因: 这两种结果用户意图差别很大, 不该替他猜。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or []
        if not isinstance(items, list) or not items:
            return jsonify({"status": "ok", "pages_deleted": 0,
                            "artworks_deleted": 0, "files_removed": 0})
        choice = str(data.get("cascade_choice") or "").strip()
        page_aids = list(dict.fromkeys(
            str(it.get("artwork_id")) for it in items
            if isinstance(it, dict) and it.get("artwork_id")))
        label_txt = _labels_inline(page_aids)
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        with _db() as db:
            # ① 预检: 会不会把某些作品删空? 会且用户还没选 → 先不做任何修改
            would_empty = db.pages_would_empty(items)
            if would_empty and choice not in ("keep", "trash"):
                # 契约 (前端按此消费, 别混):
                #   would_empty        = 作品ID **数组** ["125504541"]
                #   would_empty_labels = 人读**字符串** "《标题》 · 作者 X · 来源 pixiv"
                # 2026-09-18 事故: 前端把 labels 当数组去 .join → TypeError 被吞成
                # 「移入回收站失败…」toast, 用户看到"点了没反应"。改字段时两边一起改。
                return jsonify({"status": "need_choice", "would_empty": would_empty,
                                "would_empty_labels": _labels_inline(would_empty)})
            res = db.soft_delete_pages(items)
            # ② 用户选「一并进回收站」→ 把这些已空的整作品也标记进回收站
            # (搬移统一在 with 外一次做完, 避免同一批文件被搬两遍)
            cascaded = []
            if choice == "trash" and would_empty:
                cres = db.soft_delete_artworks(would_empty, reason="cascade")
                res["rel_moves"] += cres["rel_moves"]
                cascaded = would_empty
        moved, bad = _apply_rel_moves(res["rel_moves"], img_root, _trash_root())
        if res["count"] or cascaded:
            audit_log.record("update", "移入回收站",
                             f"把 {res['count']} 张图片移入回收站（可恢复）：{label_txt}"
                             + (f"；其中 {len(cascaded)} 个作品被删空, 已一并移入回收站"
                                if cascaded else "")
                             + (f"；{len(bad)} 张搬运失败" if bad else ""),
                             res["count"], ids=page_aids)
        return jsonify({
            "status": "ok",
            "pages_deleted": res["count"],
            "artworks_trashed": len(cascaded),
            "would_empty": res["would_empty"],
            "files_removed": moved,
            "trashed": True,
        })

    # ---------------- 🗑 应用内回收站 API (列表/恢复/彻底删除) ----------------
    @app.route("/api/library/trash/count")
    def api_library_trash_count():
        """回收站数量 (导航角标): 作品 N 个 / 单图 M 张。轻量, 不返回明细。"""
        with _db() as db:
            return jsonify({"status": "ok", **db.count_trashed()})

    @app.route("/api/library/trash/list")
    def api_library_trash_list():
        """回收站清单。kind=artwork(默认, 整作品) / page(单张图片)。"""
        kind = str(request.args.get("kind") or "artwork").strip()
        if kind not in ("artwork", "page"):
            kind = "artwork"
        with _db() as db:
            res = db.list_trashed(kind)
        # 缩略图: 文件已搬进回收站 → 走 /api/local-img?root=trash 直读 (R18 仍由前端屏蔽)
        for it in res.get("items", []):
            rel = it.get("cover_trash") or it.get("trash_rel_path")
            it["thumb"] = (f"/api/local-img?root=trash&p={quote(rel)}&thumb=1"
                           if rel else None)
        return jsonify({"status": "ok", **res})

    @app.route("/api/library/trash/preview", methods=["POST"])
    def api_library_trash_preview():
        """恢复**之前**的冲突预检 (只读): 哪些能恢复、哪些因冲突只能跳过。

        body: {"ids": [...]} 或 {"items": [{"artwork_id","page_num"}, ...]}
        冲突判据见 store._page_conflicts: 页号被占 / 原路径被不同内容占用。
        """
        data = request.get_json(silent=True) or {}
        ids = [str(x) for x in (data.get("ids") or []) if str(x)]
        items = data.get("items") or []
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        with _db() as db:
            res = db.preview_restore(ids, items, img_root=img_root,
                                     trash_root=_trash_root())
        return jsonify({"status": "ok", **res})

    @app.route("/api/library/trash/restore", methods=["POST"])
    def api_library_trash_restore():
        """从回收站恢复: 无冲突的搬回原位, 冲突项**跳过**并回报原因(绝不覆盖)。

        body: {"ids": [...]} 或 {"items": [{"artwork_id","page_num"}, ...]}
        """
        data = request.get_json(silent=True) or {}
        ids = [str(x) for x in (data.get("ids") or []) if str(x)]
        items = data.get("items") or []
        if not ids and not items:
            return jsonify({"status": "ok", "restored": 0, "skipped": []})
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        with _db() as db:
            res = db.restore_trashed(ids, items, img_root=img_root,
                                     trash_root=_trash_root())
        moved, bad = _apply_rel_moves(res["rel_moves"], _trash_root(), img_root)
        n = len(res["restored_artworks"]) + len(res["restored_pages"])
        if n or res["skipped"]:
            audit_log.record("update", "从回收站恢复",
                             f"恢复 {len(res['restored_artworks'])} 个作品 / "
                             f"{len(res['restored_pages'])} 张图片"
                             + (f"（{len(res['skipped'])} 项因冲突跳过）"
                                if res["skipped"] else ""),
                             n, ids=ids or None)
        return jsonify({"status": "ok", "restored": n,
                        "restored_artworks": res["restored_artworks"],
                        "restored_pages": res["restored_pages"],
                        "skipped": res["skipped"],
                        "files_moved": moved, "move_errors": bad[:5]})

    @app.route("/api/library/trash/purge", methods=["POST"])
    def api_library_trash_purge():
        """**彻底删除**回收站内容: 卸载 DB 记录 + 文件送 **Windows 电脑回收站** + 日志留痕。

        body: {"ids": [...]}、{"items": [{"artwork_id","page_num"}, ...]} 或 {"all": true}
        这是最深的一层删除 —— 但即使在这一层, 文件也只是进电脑回收站(可还原),
        并且操作日志有留痕。顺序铁律: **先取路径 → 送回收站 → 再删 DB 记录**
        (删完就查不到路径了, server 层为这个顺序踩过坑)。
        """
        data = request.get_json(silent=True) or {}
        ids = [str(x) for x in (data.get("ids") or []) if str(x)]
        items = data.get("items") or []
        if data.get("all"):
            with _db() as db:
                ids = [str(r["artwork_id"]) for r in db.conn.execute(
                    "SELECT artwork_id FROM artworks WHERE trashed_at IS NOT NULL").fetchall()]
                items = [{"artwork_id": str(r["artwork_id"]), "page_num": int(r["page_num"])}
                         for r in db.conn.execute(
                             "SELECT p.artwork_id, p.page_num FROM pages p "
                             "WHERE p.trashed_at IS NOT NULL AND NOT EXISTS "
                             "(SELECT 1 FROM artworks a WHERE a.artwork_id=p.artwork_id "
                             " AND a.trashed_at IS NOT NULL)").fetchall()]
        if not ids and not items:
            return jsonify({"status": "ok", "deleted": 0, "recycled": 0})
        label_txt = _labels_inline(ids[:50])
        with _db() as db:
            ra = db.purge_trashed_artworks(ids) if ids else {"rel_paths": [], "deleted": 0}
            rp = db.purge_trashed_pages(items) if items else {"rel_paths": [], "deleted": 0}
        rel_paths = list(ra.get("rel_paths", [])) + list(rp.get("rel_paths", []))
        # ② 文件送 Windows 电脑回收站 (可还原) —— 失败只报告, DB 已卸载不再回滚
        abs_paths = [str(_trash_abs(r)) for r in rel_paths]
        recycled, recycle_bad = _recycle_files(abs_paths)
        # ③ 清理空的回收站子目录 (搬空了就别留一堆空文件夹)
        try:
            for d in sorted(_trash_root().rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        except Exception:
            pass
        n = int(ra.get("deleted", 0)) + int(rp.get("deleted", 0))
        if n:
            audit_log.record("delete", "彻底删除（已送电脑回收站）",
                             f"彻底删除 {ra.get('deleted', 0)} 个作品 / "
                             f"{rp.get('deleted', 0)} 张图片"
                             + (f"：{label_txt}" if label_txt else "")
                             + (f"；文件已送电脑回收站 {recycled} 个"
                                if recycled else "；磁盘无对应文件")
                             + (f"；送回收站失败 {len(recycle_bad)} 个"
                                if recycle_bad else ""),
                             n, ids=ids or None)
        return jsonify({"status": "ok", "deleted": n, "recycled": recycled,
                        "recycle_failed": [Path(x).name for x in recycle_bad][:5]})

    @app.route("/api/library/merge-pages", methods=["POST"])
    def api_library_merge_pages():
        """批量把选中的图片页合并为同一个作品（图片视图批量操作）。
        body: {"items": [{"artwork_id", "page_num"}, ...]（至少 2 张）,
               "target": "existing"|"new", "target_id": "...", "title": "..."}
        已有作品 → 页码追加到末尾; 新建作品 → 页码从 0 重排。
        图片文件移入目标作品目录但**保留原文件名**(同名才加 (2)), 不改写成 {ID}_p{n}
        —— 图片名归用户/来源, 内部 ID 只在数据库里管 (2026-09-13 定案)。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or []
        if not isinstance(items, list) or len(items) < 2:
            return jsonify({"status": "error", "message": "至少选择 2 张图片才能合并"}), 400
        target = str(data.get("target") or "new")
        img_root_p = Path(paths.user_data(_config["paths"]["img_subdir"]))
        src_aids = list(dict.fromkeys(
            str(it.get("artwork_id")) for it in items
            if isinstance(it, dict) and it.get("artwork_id")))
        src_label = _labels_inline(src_aids, 2)   # 源作品可能被搬空删除 → 合并前先取标签
        try:
            with _db() as db:
                if target == "existing":
                    tid = str(data.get("target_id") or "").strip()
                    if not tid:
                        return jsonify({"status": "error", "message": "请选择要合并到的已有作品"}), 400
                    res = db.merge_pages(items, target_id=tid, img_root=img_root_p)
                else:
                    res = db.merge_pages(items,
                                         title=str(data.get("title") or "").strip() or None,
                                         img_root=img_root_p)
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)}), 400
        tgt_label = _labels([res["target_id"]]).get(res["target_id"]) or res["target_id"]
        # DB 提交后执行文件移动 (尽力而为; 目标已存在同大小文件则删源)
        moved = 0
        img_root = img_root_p
        for src_rel, dst_rel in res["rel_moves"]:
            try:
                s = Path(img_root) / src_rel
                d = Path(img_root) / dst_rel
                if s.is_file() and s != d:
                    d.parent.mkdir(parents=True, exist_ok=True)
                    if d.exists() and d.is_file() and d.stat().st_size == s.stat().st_size:
                        s.unlink(missing_ok=True)
                    else:
                        s.replace(d)
                    moved += 1
            except Exception:
                continue
        if res["pages_moved"]:
            audit_log.record(
                "update", "合并图片",
                f"把 {res['pages_moved']} 张图片合并为 {tgt_label}；来源：{src_label}"
                + (f"（其中 {len(res['artworks_removed'])} 个原作品已被搬空并删除）"
                   if res["artworks_removed"] else ""),
                res["pages_moved"], ids=[res["target_id"]])
        return jsonify({"status": "ok", "target_id": res["target_id"],
                        "pages_moved": res["pages_moved"],
                        "artworks_removed": res["artworks_removed"],
                        "files_moved": moved})

    @app.route("/api/library/split-pages", methods=["POST"])
    def api_library_split_pages():
        """✂ 把选中的图片页从各自作品拆出, 每张独立成一个新作品（图片视图批量操作）。
        body: {"items": [{"artwork_id", "page_num"}, ...]}（单张也能拆）
        文件不动, 只改数据库归属 —— 拆出的新作品目录 = 原文件所在目录。
        注: 合并时源页记录已搬走, 原归属无法还原, 所以拆出是**新建作品**而非回退。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or []
        if not isinstance(items, list) or not items:
            return jsonify({"status": "error", "message": "请先选择要拆分的图片"}), 400
        src_aids = list(dict.fromkeys(
            str(it.get("artwork_id")) for it in items
            if isinstance(it, dict) and it.get("artwork_id")))
        src_label = _labels_inline(src_aids, 2)   # 源作品可能被搬空删除 → 拆分前先取标签
        try:
            with _db() as db:
                res = db.split_pages(items)
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)}), 400
        if res["pages_moved"]:
            audit_log.record(
                "update", "拆分图片",
                f"从 {src_label} 拆出 {res['pages_moved']} 张图片, 每张独立成新作品"
                + (f"（{len(res['artworks_removed'])} 个原作品已被搬空并删除）"
                   if res["artworks_removed"] else ""),
                res["pages_moved"], ids=res["created"][:5])
        return jsonify({"status": "ok", "created": res["created"],
                        "pages_moved": res["pages_moved"],
                        "artworks_removed": res["artworks_removed"]})

    @app.route("/api/library/reorder-pages", methods=["POST"])
    def api_library_reorder_pages():
        """↔ 调整作品内页序 / 把附加页填进空位 (2026-09-17 需求: 手动认领/自动溯源
        可能把页挂反, 用户要能事后调)。body 二选一:
          {"artwork_id", "order": [旧页码的完整排列]}      如 [2,0,1] → 页号压实成 0..n-1
          {"artwork_id", "assign": [{"old":5,"new":2}, ...]} 点对点改号, 只动点名的页
            —— 有空洞的作品(删过页)也能用 [{A→B},{B→A}] 交换两页; 把「附加页」填进空位
               也只动那一页, 其它页的页号一个都不变 (以前有洞就整体
               拒绝, 于是附加页既不能移动、更不能填洞)。
        DB 重排页号 (md5/origin 跟文件走, total_pages 不动); 规范命名的文件两阶段
        同步重命名, 用户自命名的文件只改库内顺序。

        磁盘改名与改库都在 store.reorder_pages 里完成: **先改名、成功才写库**,
        改名失败会回滚磁盘并抛 ValueError → 这里返回 400, 绝不会出现
        「库改了、盘没改」的分裂状态 (后收紧)。"""
        data = request.get_json(silent=True) or {}
        aid = str(data.get("artwork_id") or "").strip()
        order = data.get("order")
        assign = data.get("assign")
        if not aid:
            return jsonify({"status": "error", "message": "缺少 artwork_id"}), 400
        has_order = isinstance(order, list) and bool(order)
        has_assign = isinstance(assign, list) and bool(assign)
        if not has_order and not has_assign:
            return jsonify({"status": "error",
                            "message": "缺少页序 order（页码数组）或 assign（改号表）"}), 400
        img_root_p = Path(paths.user_data(_config["paths"]["img_subdir"]))
        try:
            with _db() as db:
                res = db.reorder_pages(aid, order if has_order else None,
                                       img_root=img_root_p,
                                       assign=assign if has_assign else None)
        except ValueError as e:
            return jsonify({"status": "error", "message": str(e)}), 400
        tgt_label = _labels([aid]).get(aid) or aid
        seq = " → ".join(f"p{m['old']}→p{m['new']}" for m in res["mapping"]
                         if m["old"] != m["new"]) or "未变动"
        audit_log.record("update", "调整页序",
                         f"{tgt_label}：{seq}"
                         + (f"；同步重命名 {res['files_renamed']} 个文件"
                            if res["files_renamed"] else ""),
                         res["n"], ids=[aid])
        return jsonify({"status": "ok", "artwork_id": aid,
                        "mapping": res["mapping"],
                        "files_renamed": res["files_renamed"]})

    @app.route("/api/library/set-author", methods=["POST"])
    def api_library_set_author():
        """手动批量设置作者: body: {"ids": [...], "author_name": "...", "author_id": "可选pixiv ID"}。
        - 给了纯数字 author_id (pixiv 作者): 直接关联, 有名字一并 upsert。
        - 只给名字 (danbooru/推特等来源、或 pixiv 已注销查无 ID): 建「本地作者」
          (ID=local_<名字哈希>, 不参与 pixiv 联网核对)。
        适用于其他来源文件无作者、或已删除作品作者归属的人工修正。"""
        data = request.get_json(silent=True) or {}
        ids = [str(x) for x in (data.get("ids") or []) if str(x)]
        author_id = str(data.get("author_id") or "").strip()
        author_name = str(data.get("author_name") or "").strip()
        if not ids:
            return jsonify({"status": "error", "message": "缺少作品 ids"}), 400
        with _db() as db:
            if author_id.isdigit():
                aid = author_id
                if author_name:
                    db.upsert_author(aid, author_name)
                else:
                    # 只给了 pixiv 作者 ID: 顺手联网补上现用名 (否则图库显示裸 ID)
                    _fill_author_name_by_id(db, aid)
            elif author_name:
                aid = db.create_or_get_local_author(author_name)
            else:
                return jsonify({"status": "error", "message": "请填写作者名称或 pixiv 作者 ID"}), 400
            updated = db.set_artworks_author(ids, aid)
            row = db.conn.execute(
                "SELECT name FROM authors WHERE author_id=?", (aid,)).fetchone()
            name = row[0] if row else author_name
        if updated:
            audit_log.record("update", "指定作者",
                             f"{updated} 个作品归入作者「{name}」：{_labels_inline(ids)}",
                             updated, ids=ids)
        return jsonify({"status": "ok", "updated": updated,
                        "author_id": aid, "name": name})

    @app.route("/api/library/pixiv-author", methods=["POST"])
    def api_library_pixiv_author():
        """—— 把这个名字/ID 问清楚。

        以前手动填作者时, 库里没有的名字一律建「本地作者」, 没法声明"这是 pixiv 作者"。
        这个接口只查不写, 三种模式:
          {"artwork_id": "12345"}  → 直接问作品页的作者 (最准; 作品是纯数字 pixiv ID 时首选)
          {"author_id": "12345"}   → 按作者 ID 查现用名 (填了 ID 却不知道名字时用)
          {"name": "作者名"}       → 按名字搜 pixiv 作者, 返回候选让用户挑
        前端拿到候选后填成「作者名_作者ID」, 再走原有保存路径 (那条路本来就支持 pixiv 关联)。"""
        data = request.get_json(silent=True) or {}
        art = str(data.get("artwork_id") or "").strip()
        auid = str(data.get("author_id") or "").strip()
        name = str(data.get("name") or "").strip()
        from core.search_client import http_fetch_illust, http_fetch_user_name
        if art.isdigit() or auid.isdigit():
            sess = _pixiv_http_session()
            if sess is None:
                return jsonify({"status": "error",
                                "error": "没有可用的登录凭据，无法联网核实（到「设置」页登录 Pixiv 后再试）"}), 400
            try:
                if art.isdigit():
                    info = http_fetch_illust(sess, art)
                    if not info.author_id:
                        return jsonify({"status": "ok", "source": "artwork", "candidates": []})
                    return jsonify({"status": "ok", "source": "artwork",
                                    "candidates": [{"author_id": str(info.author_id),
                                                    "name": info.author_name or ""}]})
                nm = str(http_fetch_user_name(sess, auid) or "")
                return jsonify({"status": "ok", "source": "author_id",
                                "candidates": [{"author_id": auid, "name": nm}]})
            except Exception as e:
                return jsonify({"status": "error",
                                "error": f"联网查询失败：{str(e).splitlines()[0]}"}), 502
        if not name:
            return jsonify({"status": "error", "error": "请提供作者名 / 作者ID / 作品ID"}), 400
        # 按名字搜 pixiv 作者 (只能走搜索页 DOM, 比 HTTP 直连慢, 前端要显示"查询中…")
        try:
            sc = _get_search_client()
            hits = sc.search_authors(name, limit=8) or []
        except Exception as e:
            return jsonify({"status": "error",
                            "error": f"搜索失败：{str(e).splitlines()[0]}"}), 502
        return jsonify({"status": "ok", "source": "search",
                        "candidates": [{"author_id": str(h.author_id),
                                        "name": h.name or ""} for h in hits]})

    @app.route("/api/library/tags", methods=["POST"])
    def api_library_tags():
        """标签增删改: body: {"artwork_id": "...", "tags": ["a","b"]} 整体替换。"""
        data = request.get_json(silent=True) or {}
        aid = str(data.get("artwork_id") or "")
        tags = [str(t) for t in (data.get("tags") or []) if str(t).strip()]
        if not aid:
            return jsonify({"status": "error", "message": "缺少 artwork_id"}), 400
        with _db() as db:
            db.set_artwork_tags(aid, tags)
        audit_log.record("update", "修改标签",
                         f"{_labels_inline([aid])} 标签改为 {tags if tags else '(清空)'}",
                         1, ids=[aid])
        return jsonify({"status": "ok", "artwork_id": aid, "tags": tags})

    @app.route("/api/library/update", methods=["POST"])
    def api_library_update():
        """编辑作品元数据: body: {"artwork_id": "...", "title", "illust_type",
        "is_r18", "source", "tags"[list], "author_id"(pixiv数字), "author_name"(名字/名字_ID)}"""
        data = request.get_json(silent=True) or {}
        aid = str(data.get("artwork_id") or "")
        if not aid:
            return jsonify({"status": "error", "message": "缺少 artwork_id"}), 400
        fields = {k: v for k, v in data.items() if k != "artwork_id"}
        author_id = str(fields.pop("author_id", "") or "").strip()
        author_name = str(fields.pop("author_name", "") or "").strip()
        with _db() as db:
            # 作者: 纯数字=pixiv ID; 给名字=本地作者 (danbooru/已注销), 同名 pixiv 作者自动复用
            if author_id.isdigit():
                if author_name:
                    db.upsert_author(author_id, author_name)
                else:
                    # 只填了 pixiv 作者 ID → 联网补现用名 (否则库里作者名空, 显示裸 ID)
                    _fill_author_name_by_id(db, author_id)
                fields["author_id"] = author_id
            elif author_name:
                fields["author_id"] = db.create_or_get_local_author(author_name)
            db.update_artwork(aid, **fields)
            name = None
            if fields.get("author_id"):
                row = db.conn.execute(
                    "SELECT name FROM authors WHERE author_id=?", (fields["author_id"],)).fetchone()
                name = row[0] if row else author_name
        audit_log.record("update", "编辑作品",
                         f"{_labels_inline([aid])} 改动字段: {', '.join(fields) or '(无)'}",
                         1, ids=[aid])
        # 回author_id: 前端据此说明"已关联到 pixiv 作者 ID"(名字可能还没联网补上)
        return jsonify({"status": "ok", "name": name,
                        "author_id": fields.get("author_id")})

    @app.route("/api/library/batch-update", methods=["POST"])
    def api_library_batch_update():
        """批量编辑选中作品: body: {"ids": [...], 仅提交要改的字段:
        "illust_type"/"is_r18"/"source", "author_id"/"author_name",
        "tags"[list] + "tag_mode": "replace"(默认)|"append"|"clear"}。
        未提供的字段不动 (前端每个字段带「修改」勾选)。"""
        data = request.get_json(silent=True) or {}
        ids = [str(x) for x in (data.get("ids") or []) if str(x)]
        if not ids:
            return jsonify({"status": "error", "message": "缺少作品 ids"}), 400
        fields = {}
        if data.get("illust_type") not in (None, ""):
            fields["illust_type"] = int(data["illust_type"])
        if data.get("is_r18") not in (None, ""):
            fields["is_r18"] = int(data["is_r18"])
        if data.get("source"):
            fields["source"] = str(data["source"])
        author_id = str(data.get("author_id") or "").strip()
        author_name = str(data.get("author_name") or "").strip()
        tags = data.get("tags")
        tag_mode = str(data.get("tag_mode") or "replace")
        with _db() as db:
            if author_id.isdigit():
                if author_name:
                    db.upsert_author(author_id, author_name)
                else:
                    _fill_author_name_by_id(db, author_id)
                fields["author_id"] = author_id
            elif author_name:
                fields["author_id"] = db.create_or_get_local_author(author_name)
            # 标签三策略: 替换 / 追加(去重) / 清空
            if tags is not None or tag_mode == "clear":
                tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
                for wid in ids:
                    if tag_mode == "clear":
                        db.update_artwork(wid, tags=[])
                    elif tag_mode == "append":
                        base = db.get_artwork_tag_names(wid)
                        db.update_artwork(wid, tags=list(dict.fromkeys(base + tag_list)))
                    else:
                        db.update_artwork(wid, tags=tag_list)
            n = db.batch_update_artworks(ids, **fields) if fields else len(ids)
            name = None
            if fields.get("author_id"):
                row = db.conn.execute(
                    "SELECT name FROM authors WHERE author_id=?", (fields["author_id"],)).fetchone()
                name = row[0] if row else author_name
        changed = [k for k in fields if k != "author_id"]
        if fields.get("author_id"):
            changed.append("author_id")
        if tags is not None or tag_mode == "clear":
            changed.append("tags")
        audit_log.record("update", "批量编辑",
                         f"批量修改 {n} 个作品的字段: {', '.join(changed) or '(无)'}"
                         f"；{_labels_inline(ids)}",
                         n, ids=ids)
        return jsonify({"status": "ok", "updated": n, "name": name})

    @app.route("/api/library/complement", methods=["POST"])
    def api_library_complement():
        """手动触发「补全缺元数据作品」。

        2026-09-15 审计: 旧实现自带一个**无锁**后台线程, 可与启动时的自动补全
        `_kick_metadata_fill` 并发 (同一批作品两路 fetch + 写库)。两者本来就是同一件事,
        现统一委托给 `_kick_metadata_fill` —— 共用 `_meta_fill_state` 单飞,
        自动/手动/并发点击都只会串行跑一轮。"""
        data = request.get_json(silent=True) or {}
        limit = int(data.get("limit", 50))
        if _meta_fill_state["running"]:
            return jsonify({"status": "running",
                            "error": "已有元数据补全任务进行中，请等待完成"}), 409
        _kick_metadata_fill(limit=limit)
        return jsonify({"status": "started", "count": 0})

    # ==================================================================
    # 🩺 体检 (原「一键自检」) —— 拆成 6 个可独立运行的步骤
    #   公共设施 (_CHECK_STEPS / _check_state / _journal_*) 定义在文件头部。
    #   这里只放: 各步骤实现 + 进度上报 + 汇总报告 + 忽略机制 + 任务续跑接口。
    # ==================================================================
    def _ck_img_root() -> Path:
        return Path(paths.user_data(_config["paths"]["img_subdir"]))

    def _ck_progress(step: str, done: int, total: int, current: str = "") -> None:
        """上报进度: 内存给前端轮询, 同时写进任务记录 —— 用户要的「看得到详细进度」。"""
        prev = dict(_check_state.get("progress") or {})
        prog = {"step": step, "done": int(done), "total": int(total), "current": current,
                "started_at": (prev.get("started_at") if prev.get("step") == step
                               else time.time())}
        _check_state["progress"] = prog
        key = _check_state.get("journal_key")
        if key:
            try:
                _journal_write(key, "running", progress=prog)
            except Exception:
                pass

    # ---------- ① 磁盘对账 ----------
    def _ck_sync_corrupt_report(files: "list[str]") -> None:
        """把本步校验出的损坏文件**当场**写进报告 (报告里能直接看到是哪个文件)。

        为什么必须由 ① 自己写: 图片损坏只有这一步校验得了 (它真去 PIL 开图, 是最重的 I/O),
        而报告一直是「⑥ 复检报告」在生成 —— 于是**单跑 ①** 时用户只看到一个「损坏 1」的数字,
        翻遍报告也查不到是哪个文件。
        口径: 谁校验得了谁负责维护报告里的 corrupt 组 —— 先删掉报告里全部旧的 corrupt
        条目 (已经修好的自动消失, 不留清不掉的幽灵条目), 再写入这次的**真实**清单。
        """
        try:
            _ck_load_report_from_disk()          # 内存里没有就先从盘读回上一版
        except Exception:
            pass
        rep_d = _check_state.get("report")
        if not isinstance(rep_d, dict):
            rep_d = {"time": _now_str(), "suspicious": [], "todos": [], "ignored": 0}
            _check_state["report"] = rep_d
        # 相对路径 → 作品ID: 报告里那条「♻ 重下修复」要按作品重下 (与 ⑥ 同一套映射)
        aid_of_rel: "dict[str, str]" = {}
        try:
            with _db() as db:
                for pr in db.conn.execute(
                        "SELECT artwork_id, local_path FROM pages "
                        "WHERE local_path IS NOT NULL AND local_path <> ''"):
                    aid_of_rel[str(pr[1]).replace("\\", "/")] = str(pr[0])
        except Exception:
            pass
        img_root = _ck_img_root()
        for bucket in ("suspicious", "todos"):
            rep_d[bucket] = [x for x in (rep_d.get(bucket) or [])
                             if str((x or {}).get("type") or "") != "corrupt"]
        for cf in files:
            path_s = str(cf)
            try:
                rel = Path(path_s).resolve().relative_to(Path(img_root).resolve()).as_posix()
            except Exception:
                rel = ""
            extra = {"path": path_s}
            aid = aid_of_rel.get(rel, "")
            if aid:
                extra["artwork_id"] = aid
            rep_d["suspicious"].append(dict(
                type="corrupt", ref=path_s,
                text=f"图片文件损坏，无法打开：{path_s}", **extra))
        # 本步真校验过损坏 → 报告里那句「这一次没有校验图片损坏」就不该再出现
        rep_d["corrupt_verified"] = True
        _save_self_check_compat()

    def _ck_step_reconcile(ctx: dict) -> dict:
        """扫描磁盘↔数据库一致性, 并校验图片能否打开 (最重的磁盘 I/O, 只读＋路径自愈)。

        这一步只做"核对与自愈": 不整理文件、不联网, 所以随时可中断且无害。"""
        _ck_progress("reconcile", 0, 1, "读取磁盘与数据库…")
        img_root = _ck_img_root()
        with _db() as db:
            rec = db.reconcile_output(img_root, verify_corrupt=True)
        ctx["rec"] = rec
        ctx["corrupt_keep"] = list(getattr(rec, "corrupt_files", None) or [])
        _ck_sync_corrupt_report(ctx["corrupt_keep"])
        _ck_progress("reconcile", 1, 1, "")
        return {"scanned": int(getattr(rec, "scanned_images", 0) or 0),
                "new_pages_linked": int(getattr(rec, "new_pages_linked", 0) or 0),
                "paths_relinked": int(getattr(rec, "pages_relinked", 0) or 0),
                "missing": len(rec.missing_ids or []),
                "corrupt": len(ctx["corrupt_keep"]),
                "empty_dirs": len(rec.empty_dirs or [])}

    # ---------- ② 离线清理 ----------
    def _ck_step_offline(ctx: dict) -> dict:
        """纯本地数据整理 (毫秒级, 不联网不动磁盘文件): 全是"改了就对"的事。

        旧实现把它夹在磁盘对账和联网之间, 一旦后面某步崩了, 这步的成果也一起没了。"""
        img_root = _ck_img_root()
        out: dict = {}
        phases = 7

        def _ph(i: int, txt: str) -> None:
            _ck_progress("offline", i, phases, txt)

        with _db() as db:
            _ph(0, "补作者名…")
            out["authors_adopted"] = _adopt_authors_from_folders(db, img_root)
            _ph(1, "登记其他来源文件…")
            disk_rels = [p.relative_to(img_root).as_posix() for p in img_root.rglob("*")
                         if p.is_file() and p.suffix.lower() in _TIDY_IMG_EXT]
            out["foreign_registered"] = db.register_foreign_files(
                db.foreign_unregistered_files(disk_rels))
            _ph(2, "来源细分…")
            out["sources_refined"] = db.refresh_foreign_sources()
            _ph(3, "清理孤儿数据…")
            orph = db.cleanup_orphans() or {}
            out["orphans_removed"] = sum(int(v) for v in orph.values())
            _ph(4, "修复无意义标题…")
            out["titles_repaired"] = db.repair_id_titles()
            _ph(5, "纠正合并产物身份 merged_ → ext_…")
            out["reidentified"] = len(db.reidentify_merged_external())
            _ph(6, "统一时间戳格式…")
            # 历史上几处写歪的 ISO 串(带 'T'/微秒)会让「按修改时间」排序错乱 ——
            # 带 'T' 的行字符串比较永远排最前 (几条旧记录一直顶在最前)
            out["timestamps_fixed"] = db.normalize_all_timestamps()
        _ck_progress("offline", phases, phases, "")
        return out

    # ---------- ③ 作者改名同步 ----------
    def _ck_step_authors(ctx: dict) -> dict:
        """联网核对作者现用名与存续状态 (每位作者一次 HTTP 请求)。

        旧实现把它塞在联网补元数据里, 于是"只是想补几个作品的标题"也要等几百位作者
        逐个核完 (每次还 sleep 0.5s)。现在拆开: 不想等就不跑这一步。"""
        out = {"checked": 0, "renamed": 0, "skipped": ""}
        try:
            from utils.sec import load_cookies_smart
            from core.search_client import build_pixiv_http_session
            ck = load_cookies_smart(str(_cookies_path()), upgrade=False)
            if not ck:
                out["skipped"] = "未检测到登录凭据，已跳过（到「设置」页登录后再跑这项）"
                return out
            _acc = _load_account()
            sess = build_pixiv_http_session(ck, str((_acc or {}).get("user_id") or ""))
        except Exception as e:
            out["skipped"] = f"登录态不可用，已跳过：{str(e).splitlines()[0]}"
            return out
        with _db() as db:
            total = int(db.conn.execute(
                "SELECT COUNT(*) FROM authors WHERE author_id GLOB '[0-9]*'"
            ).fetchone()[0])
        out["checked"] = total
        if not total:
            out["skipped"] = "库里还没有 pixiv 作者"
            return out
        _ck_progress("authors", 0, total, "")
        with _db() as db:
            out["renamed"] = _refresh_author_names(
                db, sess, _reporter(),
                progress_cb=lambda i, t, name: _ck_progress("authors", i, t, name))
        return out

    def _ck_metadata_ids() -> list[str]:
        """联网补全名单 = 缺元数据的 pixiv 作品 + authors 表缺行的 pixiv 作品
        + 图库根目录散落的 pixiv 命名图所属作品。

        **一律只保留纯数字 ID**  非 pixiv 作品(ext_/merged_)在 pixiv 上
        没有页面, 去问必然 404 —— 旧实现把它们也塞进来, 于是报告里永远挂着
        「元数据补全失败」这种补不回来的假问题。"""
        img_root = _ck_img_root()
        loose_aids: list[str] = []
        for p in img_root.iterdir():
            if p.is_file() and p.suffix.lower() in _TIDY_IMG_EXT:
                nkey = pixiv_natural_key(p.name)
                if nkey:
                    loose_aids.append(str(nkey[0]))
        with _db() as db:
            missing = [str(x) for x in db.works_missing_metadata(limit=100)]
            need_author = [str(r["artwork_id"]) for r in db.conn.execute(
                """SELECT a.artwork_id FROM artworks a
                   LEFT JOIN authors au ON a.author_id=au.author_id
                   WHERE a.author_id IS NOT NULL AND a.author_id != ''
                     AND au.author_id IS NULL""")]
        return [a for a in dict.fromkeys(missing + need_author + loose_aids)
                if a.isdigit()]

    # ---------- ④ 联网补元数据 ----------
    def _ck_step_metadata(ctx: dict) -> dict:
        """逐个作品抓标题/标签/封面/作者 (最慢的一步), 带 x/y 详细进度。

        只处理 pixiv 作品; 未登录直接给出提示并跳过 (不傻跑几百个超时);
        连续失败 5 次熔断 (网络/登录异常时不再耗下去)。被中断后重跑本步即可接着补 ——
        已补全的作品不会再进名单。"""
        out = {"filled": 0, "failed": 0, "deleted_marked": 0, "skipped": ""}
        if _meta_fill_state.get("running"):
            out["skipped"] = "已有「补全元数据」任务在跑，等它结束再跑这项"
            return out
        ids = _ck_metadata_ids()
        if not ids:
            out["skipped"] = "没有需要联网补全的 pixiv 作品"
            return out
        sc = _get_search_client()
        try:
            sc.driver.set_page_load_timeout(20)
        except Exception:
            pass
        try:
            online = bool(sc.ensure_login() and sc.fetch_logged_in_user())
        except Exception:
            online = False
        if not online:
            out["skipped"] = "未检测到有效登录态，已跳过（到「设置」页登录后再跑这项）"
            ctx.setdefault("metadata_alerts", []).append(
                "未登录或登录态已失效：联网补元数据已跳过，登录后单独跑这项即可自动补全")
            return out
        _ck_progress("metadata", 0, len(ids), "")
        fail_streak = 0
        for i, aid in enumerate(ids, 1):
            if _quitting or _cancel_event.is_set():
                out["skipped"] = f"被中断（已完成 {i - 1}/{len(ids)}，重跑本项即可接着补）"
                ctx.setdefault("metadata_alerts", []).append(out["skipped"])
                break
            _ck_progress("metadata", i - 1, len(ids), f"#{aid}")
            try:
                art = sc.fetch_artwork(aid, allow_dom_fallback=False)
                fail_streak = 0
                with _db() as db:
                    db.change_work_status(
                        aid, WSt.NOT_YET, title=art.title,
                        author_id=art.author_id or None,
                        illust_type=art.illust_type.value,
                        tags=list(art.tags), publish_date=art.publish_date,
                        is_r18=art.is_r18,
                        thumb_url=(art.image_urls or [None])[0])
                    if art.author_id and art.author_name:
                        db.upsert_author(art.author_id,
                                         _readable_author_name(db, art.author_id,
                                                               art.author_name))
                out["filled"] += 1
            except Exception as e:
                msg = str(e).splitlines()[0]
                if ("不存在" in msg or "找不到" in msg or "已删除" in msg):
                    # 作品页确认失效 → 客观事实落字段 (自动退出补全队列, 不再当"可疑项"挂着)
                    try:
                        with _db() as db:
                            db.mark_artwork_deleted(aid, True)
                        out["deleted_marked"] += 1
                    except Exception:
                        pass
                    continue
                out["failed"] += 1
                ctx.setdefault("metadata_failed_ids", []).append(str(aid))
                fail_streak += 1
                if fail_streak >= 5:
                    out["skipped"] = (f"连续失败 {fail_streak} 次（疑似网络/登录异常），"
                                      f"已提前结束（完成 {i}/{len(ids)}）")
                    ctx.setdefault("metadata_alerts", []).append(out["skipped"])
                    break
        _ck_progress("metadata", out["filled"] + out["failed"], len(ids), "")
        return out

    # ---------- ⑤ 文件整理 ----------
    def _ck_step_organize(ctx: dict) -> dict:
        """散落图归位 + 作者文件夹校正 + 历史文件名规范化 (唯一会移动磁盘文件的步骤)。

        所以它单独成项: 不放心的可以不跑; 跑之前/之后各步的结果都还在。"""
        import shutil as _shutil
        rep = _reporter()
        img_root = _ck_img_root()
        out = {"organized": 0, "legacy_renamed": 0, "relinked": 0}
        out["organized"] += _organize_loose_images(img_root)
        _ck_progress("organize", 0, 1, "校正作者文件夹…")

        # 5b 作者文件夹校正: 放错文件夹的图, 按 DB 里的作者移到正确文件夹
        with _db() as db:
            rows = db.conn.execute(
                """SELECT a.artwork_id AS aid, a.author_id AS author_id, au.name AS name
                     FROM artworks a LEFT JOIN authors au ON a.author_id=au.author_id
                    WHERE a.author_id IS NOT NULL AND a.author_id != ''""").fetchall()
            author_map = {str(r["aid"]): (str(r["author_id"]), r["name"] or "")
                          for r in rows}
            # 规范夹名 (与一键整理同一套, 2026-09-21 治本): 在连接有效期内算好循环里直接用。
            # 以前这里用「删掉非法字符」拼夹名, 与一键整理的「非法字符→下划线」对不上,
            # 结果同一位作者被摆成两个夹 (实测: 作者甲/作者乙)。
            folder_of = {aid: _author_folder_name(db, aid, nm)
                         for aid, (_a, nm) in author_map.items() if nm}
        dirs = [d for d in sorted(list(img_root.iterdir())) if d.is_dir()]
        for _di, author_dir in enumerate(dirs, 1):
            _ck_progress("organize", _di, len(dirs) + 1, author_dir.name)
            parts = author_dir.name.rsplit("_", 1)
            if len(parts) != 2 or not parts[1].isdigit():
                continue                      # 非「名字_ID」标准命名, 不动
            folder_aid = parts[1]
            for img_file in sorted(list(author_dir.iterdir())):
                if not (img_file.is_file()
                        and img_file.suffix.lower() in _TIDY_IMG_EXT):
                    continue
                m = re.search(r'(\d+)_p\d+', img_file.name)
                if not m:
                    continue
                entry = author_map.get(m.group(1))
                if not entry or not entry[0] or not entry[1]:
                    continue                  # DB 也不知道作者是谁
                db_aid, db_name = entry
                if db_aid == folder_aid:
                    continue                  # 放对了
                target_dir = img_root / folder_of[db_aid]
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / img_file.name
                if target.exists():
                    try:
                        if target.stat().st_size == img_file.stat().st_size:
                            img_file.unlink()
                            out["organized"] += 1
                    except OSError:
                        pass
                    continue
                _shutil.move(str(img_file), str(target))
                out["organized"] += 1
                rep.log(f"🩺 校正: {img_file.name} {author_dir.name} → {target_dir.name}")

        # 5c 历史文件名规范化: 剥掉旧版反搜加的 "(识别ID)" 前缀。
        #   旧 bug: 这里引用了未定义的 `_km`, 一旦命中就抛 NameError 把整个自检打断
        #   (同类的还有未导入的 `_shutil`); 现在用从文件名解析出的主体 ID 判作者是否已验证。
        _ck_progress("organize", len(dirs) + 1, len(dirs) + 1, "历史文件名规范化…")
        legacy_re = re.compile(r'^\((\d+)\)(.+\.(?:jpg|jpeg|png|webp|gif))$', re.IGNORECASE)
        for f in sorted(img_root.rglob('*')):
            if not (f.is_file() and f.suffix.lower() in _TIDY_IMG_EXT):
                continue
            lm = legacy_re.match(f.name)
            if not lm:
                continue
            bracket_id, rest_name = lm.group(1), lm.group(2)
            nkey = pixiv_natural_key(rest_name)
            if not nkey:
                continue
            try:
                with _db() as db:
                    ok = db.conn.execute(
                        "SELECT 1 FROM artworks WHERE artwork_id=? "
                        "AND author_id IS NOT NULL AND author_id != '' LIMIT 1",
                        (str(nkey[0]),)).fetchone()
                if not ok:
                    # 主体作品作者未验证 → 改名有风险, 留给人工确认 (进报告)
                    ctx.setdefault("legacy_pending", []).append(
                        {"name": f.name, "bracket_id": bracket_id,
                         "subject": str(nkey[0])})
                    continue
                target = f.with_name(rest_name)
                if target.exists():
                    try:
                        if target.stat().st_size == f.stat().st_size:
                            f.unlink()        # 同内容副本
                    except OSError:
                        continue
                else:
                    try:
                        from utils.hashutil import md5_of as _md5_of
                        with _db() as db:
                            db.reverse_search_put(
                                f.name, _md5_of(str(f)), 0.0, "LegacyFilename",
                                hit_artwork_id=bracket_id,
                                raw_json=json.dumps(
                                    {"canonical_artwork_id": str(nkey[0])},
                                    ensure_ascii=False))
                    except Exception:
                        pass
                    _shutil.move(str(f), str(target))
                out["legacy_renamed"] += 1
                rep.log(f"🩺 规范化历史文件名: {f.name} → {rest_name}")
            except Exception as oe:
                rep.warn(f"🩺 规范化 {f.name} 失败: {str(oe).splitlines()[0]}")

        # 挪/改名后再对账, 让 pages 路径凭 MD5 自愈到新位置
        if out["organized"] or out["legacy_renamed"]:
            with _db() as db:
                rec = db.reconcile_output(img_root, verify_corrupt=False)
            out["relinked"] = int(getattr(rec, "pages_relinked", 0) or 0)
        return out

    # ---------- ⑥ 复检报告 ----------
    #   条目分类 (分开显示):
    #     可疑项 = 系统异常, 通常能自动/一键修 (文件损坏/丢失/重复/数据存疑/空夹);
    #     待办项 = 需人工决策的 (缺作者名要指定或反搜; 散落图要识别来源)。
    #   旧实现混在一起, 于是"待办"永远挂在"可疑"里, 数字看着永远不降。
    _CK_SUSPICIOUS = {"corrupt", "missing_file", "duplicate", "legacy_name", "empty_dir"}
    # 其余类型 (no_author / loose_image / metadata_failed …) 一律进「待办项」
    # 两个桶的判据 = **谁能修** (不是严重程度): 可疑项=系统异常、有现成的一键处理;
    # 待办项=缺信息、只能人工补。前端标题就按这个写。
    _ck_reverify_state = {"at": 0.0}      # GET 报告时顺手本地复核的节流时间戳

    # 本步 = **复检上一版报告** (, 原先是"顺带汇总"):
    #   "每个部分在检测到问题时不应该直接加入报告吗? 这个部分应该把现有的汇总报告重新检查一遍,
    #    看看问题是否还在, 已经解决了就清理掉, 或者看看问题是不是变化了。"
    # 于是: ① 从对账/DB/磁盘现场**重新推导**本次事实(=重新检查); ② 与上一版逐条 diff:
    #   已解决 → 清理(并列出让用户看见); 仍存在 → 保留但用最新事实刷新文案(变了就看得出来);
    #   新增 → 并入; ③ **本次没条件复检的**(对应来源步骤没跑) → 保留并标注, 绝不误清。
    # 为什么 ③ 必须有: 单跑本步时 ④ 的"联网补全失败"、⑤ 的"历史文件名存疑"都拿不到,
    #   若把它们当成"已解决"清掉, 用户会以为问题消失了 —— 那是骗人, 比不报更糟。
    _CK_FRESH_ALWAYS = {"loose_image", "no_author"}      # 现场重算, 任何情况下都能复检
    _CK_FRESH_BY_STEP = {"reconcile": {"missing_file", "empty_dir", "duplicate"},
                         "organize": {"legacy_name"},
                         "metadata": {"metadata_failed"}}

    def _ck_item_digest(it: dict) -> str:
        """条目"问题内容"的指纹 (只看类型/标识/人读文案)。
        上下两版同一条目的指纹不同 → 说明"问题变了"(文案里的数量/路径/标签会跟着变)。"""
        return "|".join(str(it.get(k) or "") for k in ("type", "ref", "text"))

    def _ck_step_report(ctx: dict) -> dict:
        """复检上一版报告: 已解决清掉 / 变化的更新 / 仍存在的保留 / 未复检的如实标注。

        顺带把本次**所有步骤新发现**的问题并进报告 —— 所以"每个部分发现问题就直接进报告"
        这条本来就有 (本步从各步产物 + DB + 磁盘现场重新推导), 只是过去没跟上一版对账。
        """
        img_root = _ck_img_root()
        light = False
        rec = ctx.get("rec")
        if rec is None:
            # 单跑本步时没有对账结果 → 现场轻扫一次 (不校验损坏, 快)。
            # 损坏校验只属于「① 磁盘对账」, 所以这里要如实标注"没校验过损坏",
            # 否则用户会以为"可疑 0 项 = 一切正常"。
            with _db() as db:
                rec = db.reconcile_output(img_root, verify_corrupt=False)
            ctx["rec"] = rec
            light = True
        keep_corrupt = ctx.get("corrupt_keep")
        verified_corrupt = keep_corrupt is not None      # ① 跑过了才算真校验过损坏
        if keep_corrupt is None:
            keep_corrupt = list(getattr(rec, "corrupt_files", None) or [])
        with _db() as db:
            rows = db.list_recent_artworks(limit=100000)
            row_of = {str(r["artwork_id"]): r for r in rows}
            # 已失效作品不进「缺作者名」待办: 作品页都没了, 作者补不回来
            no_author = [r for r in rows
                         if not r.get("author_name") and not r.get("deleted_at")]
            na_ids = [str(r["artwork_id"]) for r in no_author[:50]]
            ids_with_local: "set[str]" = set()
            if na_ids:
                ph = ",".join("?" * len(na_ids))
                for pr in db.conn.execute(
                        f"SELECT DISTINCT artwork_id FROM pages WHERE artwork_id IN ({ph}) "
                        f"AND local_path IS NOT NULL AND local_path <> ''", na_ids):
                    ids_with_local.add(str(pr[0]))
            dismissed = db.dismissed_keys()
            # 「损坏文件」在报告里也要能一键「♻ 重下修复」→ 顺手建 相对路径→作品ID 的映射
            # (corrupt_files 是绝对路径; pages.local_path 是相对路径)
            aid_of_rel: "dict[str, str]" = {}
            try:
                for _pr in db.conn.execute(
                        "SELECT artwork_id, local_path FROM pages "
                        "WHERE local_path IS NOT NULL AND local_path <> ''"):
                    aid_of_rel[str(_pr[1]).replace("\\", "/")] = str(_pr[0])
            except Exception:
                pass

        items: list[dict] = []

        def _add(kind: str, ref: str, text: str, **extra):
            items.append(dict(type=kind, ref=str(ref), text=text, **extra))

        for aid in (rec.missing_ids or []):
            _add("missing_file", str(aid),
                 f"{_artwork_label(row_of.get(str(aid)), str(aid))}"
                 f" 数据库有记录，但磁盘上的图片文件已丢失", artwork_id=str(aid))
        for cf in keep_corrupt:
            # 尽量把损坏文件对回所属作品 → 报告里能直接「♻ 重下修复」(拿官方源覆盖坏的这张)
            _cfs = str(cf)
            _rel = ""
            try:
                _rel = Path(_cfs).resolve().relative_to(img_root.resolve()).as_posix()
            except Exception:
                _rel = ""
            _aid_c = aid_of_rel.get(_rel, "")
            _extra = {"path": _cfs}
            if _aid_c:
                _extra["artwork_id"] = _aid_c
            _add("corrupt", _cfs, f"图片文件损坏，无法打开：{_cfs}", **_extra)
        for ed in (rec.empty_dirs or []):
            _add("empty_dir", str(ed), f"空文件夹（可删）：{ed}", path=str(ed))
        for dup in (getattr(rec, "duplicates", None) or []):
            paths_ = dup.get("paths") or []
            aid = dup.get("artwork_id") or "?"
            page = dup.get("page")
            who = f"作品 {aid}" + (f" 第 {int(page)+1} 页" if page is not None else "")
            _add("duplicate", str(dup.get("md5") or (paths_[0] if paths_ else aid)),
                 f"{who} 有 {len(paths_)} 份重复副本：" + "；".join(paths_),
                 md5=dup.get("md5"), paths=paths_)
        for p in sorted(img_root.iterdir()):
            if p.is_file() and p.suffix.lower() in _TIDY_IMG_EXT:
                _add("loose_image", p.name,
                     f"未整理图片「{p.name}」：无法确定作者（可到反搜页识别后自动归档）",
                     name=p.name)
        for lp in (ctx.get("legacy_pending") or []):
            _add("legacy_name", str(lp.get("name")),
                 f"历史反搜文件「{lp.get('name')}」带存疑前缀 ({lp.get('bracket_id')})，"
                 f"但主体作品 {lp.get('subject')} 尚未验证作者，暂不自动改名",
                 name=lp.get("name"))
        for r in no_author[:50]:
            _add("no_author", str(r["artwork_id"]),
                 f"{_artwork_label(r, str(r['artwork_id']))}"
                 f" 仍缺作者名（文件夹名里读不到，联网也补不到）",
                 artwork_id=str(r["artwork_id"]),
                 label=_artwork_label(r, str(r["artwork_id"])),
                 has_local=str(r["artwork_id"]) in ids_with_local)
        # 联网补全失败: 只可能是 pixiv 作品 (非 pixiv 已不进该队列)
        for mf in (ctx.get("metadata_failed_ids") or []):
            _add("metadata_failed", str(mf),
                 f"作品 {mf} 元数据补全失败（作品可能受限，或需重新登录后再试）",
                 artwork_id=str(mf))
        for i, alert in enumerate(ctx.get("metadata_alerts") or []):
            _add("metadata_failed", f"alert:{i}", str(alert))

        kept = [it for it in items if (it["type"], it["ref"]) not in dismissed]

        # ---- 复检: 与上一版报告逐条对账 ----
        # 本次"有条件复检"的类型: 现场重算的恒可; 依赖某步产物的, 看那步这次有没有跑成功。
        ran = set(ctx.get("_ran") or ())
        fresh = set(_CK_FRESH_ALWAYS)
        for step, types in _CK_FRESH_BY_STEP.items():
            if step in ran:
                fresh |= types
        if light:
            # 单跑本步时自己做了一次轻扫 → 这几类确实是"本次核对过"的
            fresh |= {"missing_file", "empty_dir", "duplicate"}
        if verified_corrupt:
            fresh.add("corrupt")
        prev = (list((_check_state.get("report") or {}).get("suspicious") or [])
                + list((_check_state.get("report") or {}).get("todos") or []))
        prev_map = {(str(x.get("type") or ""), str(x.get("ref") or "")): x for x in prev}
        new_map = {(it["type"], it["ref"]): it for it in kept}
        resolved, changed, unverified = [], [], []
        for key, p in prev_map.items():
            ptype = str(p.get("type") or "")
            if ptype not in fresh:
                unverified.append(p)          # 本次没条件复检 → 保留, 绝不算"已解决"
            elif key in new_map:
                if _ck_item_digest(p) != _ck_item_digest(new_map[key]):
                    changed.append({"type": ptype, "ref": key[1],
                                    "was": p.get("text") or "",
                                    "now": new_map[key].get("text") or ""})
            else:
                resolved.append({"type": ptype, "ref": key[1], "text": p.get("text") or ""})
        # 未复检的旧条目照原样留在报告里 (标注来源未跑), 不冒充本次结果
        for p in unverified:
            key = (str(p.get("type") or ""), str(p.get("ref") or ""))
            if key not in new_map and key not in dismissed:
                kept.append(p)
                new_map[key] = p
        added = len([k for k in new_map if k not in prev_map])

        suspicious = [it for it in kept if it["type"] in _CK_SUSPICIOUS]
        todos = [it for it in kept if it["type"] not in _CK_SUSPICIOUS]
        return {"suspicious": len(suspicious), "todos": len(todos),
                "ignored": len(dismissed),
                "resolved": len(resolved), "changed": len(changed),
                "added": added, "unverified": len(unverified),
                "_items": kept, "_suspicious": suspicious, "_todos": todos,
                "_resolved": resolved, "_changed": changed,
                # 本次真正复核过的**来源步骤** (不含 ⑥ 自己 —— 它是执行者不是来源)。
                # 由本步在执行时如实上报, 别在 _ck_run 里取 (那时 'report' 已被加进去了)。
                "_steps_ran": sorted(ran),
                "_corrupt_verified": verified_corrupt}

    _CK_FN = {"reconcile": _ck_step_reconcile, "offline": _ck_step_offline,
              "authors": _ck_step_authors, "metadata": _ck_step_metadata,
              "organize": _ck_step_organize, "report": _ck_step_report}

    def _check_summary_text(name: str, res: dict) -> str:
        keys = {"reconcile": ("scanned", "new_pages_linked", "paths_relinked",
                              "missing", "corrupt"),
                "offline": ("authors_adopted", "foreign_registered", "orphans_removed",
                            "titles_repaired", "reidentified", "timestamps_fixed"),
                "authors": ("checked", "renamed"),
                "metadata": ("filled", "failed", "deleted_marked"),
                "organize": ("organized", "legacy_renamed", "relinked"),
                "report": ("suspicious", "todos", "resolved", "changed",
                           "added", "unverified", "ignored")}.get(name, ())
        parts = [f"{k}={res.get(k)}" for k in keys if res.get(k)]
        return "，".join(parts) or "完成"

    def _save_self_check_compat() -> None:
        """把报告写成旧的 self_check.json 结构, 让老入口/审批窗口继续可用。

        2026-09-18 修正: `suspicious` **不再**把 todos 拼进来 —— 老写法里同一件作品
        会在 `suspicious` 与 `todos` 各存一份(重复), 用户看文件/报告时以为"同一条出现两次";
        而且只在 `suspicious` 里删掉的条目会被 `todos` 那份又合并回来(见 self-check-dismiss)。
        读回侧 (`_ck_load_report_from_disk`) 本就同时吃两个字段并按 (type,ref) 去重, 不受影响。
        """
        rep = _check_state.get("report") or {}
        last_fixed = (_check_state.get("last_run") or {}).get("fixed") or {}
        if not last_fixed:
            # 从盘里读回的报告没有 last_run → 保住文件里原本的 fixed, 别把这份摘要清空
            last_fixed = (_ck_read_legacy_result() or {}).get("fixed") or {}
        legacy = {"time": rep.get("time"),
                  "fixed": last_fixed,
                  "suspicious": list(rep.get("suspicious") or []),
                  "todos": list(rep.get("todos") or []),
                  "ignored": rep.get("ignored", 0),
                  # 复检结论也落盘: 重启后仍能看到"上次清掉了哪些已解决的"
                  "recheck": rep.get("recheck") or {},
                  "bar_dismissed": rep.get("bar_dismissed") or ""}
        _self_check_state["result"] = legacy
        try:
            Path(paths.data_root()).joinpath("self_check.json").write_text(
                json.dumps(legacy, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _ck_legacy_drop(kind: str, ref: str, *, artwork_id: str = "") -> int:
        """从落盘的旧结构报告里删掉一条 (内存 + 文件同步), 返回删了几处。

        **两个字段都要删** (`suspicious` 与 `todos`) —— 
        「体检的条目处理完了还一直显示」, 根因就是老版只删了 `suspicious`: 老写法把
        todos 也拼进了 suspicious, 于是同一个条目在 `todos` 里还有一份, 重启读盘时
        (`_ck_load_report_from_disk` 合并两个字段) 又被合并回来, 看着就像"根本删不掉"。
        """
        res = _ck_read_legacy_result()
        if not isinstance(res, dict):
            return 0
        dropped = 0
        for key in ("suspicious", "todos"):
            arr = res.get(key)
            if not isinstance(arr, list):
                continue
            keep = [x for x in arr
                    if not (isinstance(x, dict)
                            and x.get("type") == kind
                            and (str(x.get("ref")) == ref
                                 or (artwork_id and str(x.get("artwork_id")) == artwork_id)))]
            dropped += len(arr) - len(keep)
            res[key] = keep
        _self_check_state["result"] = res
        try:
            Path(paths.data_root()).joinpath("self_check.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        return dropped

    def _ck_reverify_local(force: bool = False) -> int:
        """打开体检相关界面时顺手做的**本地轻量复核**: 把已经解决的条目从报告里清掉。

        体检条目处理完了还一直显示: 报告是上次体检的快照, 只有再跑
        一次才刷新 —— 但「缺作者名 / 缺文件 / 空夹 / 散落图 / 损坏图(文件已删)」这几类
        **本地就能判定**, 没必要让用户为了消掉一条去跑一整轮体检。
        只判能现场判的类型; `legacy_name / metadata_failed` 需要重跑对应步骤,
        一律保持原样 —— **绝不误清**(宁可多留, 不可假报"已解决")。
        「损坏图」是 2026-09-21 加进来的, 且**只判文件存在性**: 见 `_ck_item_gone` 里那段注释。
        节流 60s: 这个函数挂在 GET 报告上, 不能每次轮询都全库扫。
        """
        rep = _check_state.get("report")
        if not isinstance(rep, dict) or not rep:
            return 0
        now = time.time()
        if not force and (now - float(_ck_reverify_state.get("at") or 0)) < 60.0:
            return 0
        _ck_reverify_state["at"] = now
        img_root = _ck_img_root()
        removed = 0
        try:
            with _db() as db:
                for bucket in ("suspicious", "todos"):
                    keep = []
                    for it in (rep.get(bucket) or []):
                        if isinstance(it, dict) and _ck_item_gone(it, db, img_root):
                            removed += 1
                            continue
                        keep.append(it)
                    rep[bucket] = keep
        except Exception:
            return 0
        if removed:
            from datetime import datetime as _dt
            rk = dict(rep.get("recheck") or {})
            rk["auto_resolved"] = int(rk.get("auto_resolved") or 0) + removed
            rk["auto_at"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            rep["recheck"] = rk
            _save_self_check_compat()
            _reporter().log(f"🩺 体检报告顺手复核: 清掉 {removed} 条已解决的条目")
        return removed

    def _ck_item_gone(it: dict, db, img_root: Path) -> bool:
        """条目描述的问题**现在**是否已经不存在了 (True=可清掉)。

        判不了的类型一律 False (保持原样): 宁可在报告里多留一条, 也绝不假报"已解决"。"""
        t = str(it.get("type") or "")
        if t == "no_author":
            aid = str(it.get("artwork_id") or it.get("ref") or "")
            if not aid:
                return False
            row = db.conn.execute(
                """SELECT a.trashed_at, a.deleted_at, au.name AS author_name
                     FROM artworks a
                LEFT JOIN authors au ON a.author_id = au.author_id
                    WHERE a.artwork_id = ?""", (aid,)).fetchone()
            if row is None:
                return True                      # 作品记录都没了 → 这个问题不存在了
            if row["trashed_at"] or row["deleted_at"]:
                return True                      # 已删除/已失效的作品不该再占用待办
            return bool(str(row["author_name"] or "").strip())
        if t == "missing_file":
            aid = str(it.get("artwork_id") or it.get("ref") or "")
            rows = db.conn.execute(
                "SELECT local_path FROM pages WHERE artwork_id=? "
                "AND local_path IS NOT NULL AND local_path <> ''", (aid,)).fetchall()
            if not rows:
                return True                      # 页记录没了 → 没有"文件丢失"这回事
            return any((img_root / str(r[0])).is_file() for r in rows)   # 文件回来了
        if t == "empty_dir":
            d = Path(str(it.get("path") or it.get("ref") or ""))
            return not d.is_dir() or not _tidy_dir_is_disposable(d)      # 夹没了 / 里面已经有东西
        if t == "corrupt":
            # 「明明把那张损坏图删了, 报告里还在」。
            # **只判文件还在不在**, 不重新校验像素: 文件还在就原样保留 —— 哪怕它其实已经被
            # 修好了, 也不在没重跑①的情况下宣称"损坏已修复"(那才是真的丢信息, 见本函数
            # 顶部那段设计取舍)。文件没了(删掉/移走/进了回收夹) → 这条描述本身不成立 → 清掉。
            #
            # 只认 `path`, **不认 `ref`**: ①/⑥ 写出来的 corrupt 条目一定带 path(绝对路径),
            # 而老报告里那条没标识的 corrupt 在读回来时 ref 会被兜底成 "idx:0" 这种下标
            # 占位符 —— 拿它去 is_file() 必然 False, 会把"判不了"误当成"文件已删"清掉。
            p = str(it.get("path") or "").strip()
            if not p:
                return False                     # 没有真实路径 → 判不了 → 原样保留
            return not Path(p).is_file()
        if t == "loose_image":
            return not (img_root / str(it.get("name") or it.get("ref") or "")).is_file()
        return False

    def _ck_read_legacy_result() -> "dict | None":
        """读落盘的 self_check.json (旧结构)。内存里没有时用它兜底 ——
        页面刷新/服务重启 (以及测试直接塞文件) 时, 老入口仍能拿到上次结果。"""
        res = _self_check_state.get("result")
        if res:
            return res
        try:
            p = Path(paths.data_root()) / "self_check.json"
            if p.is_file():
                res = json.loads(p.read_text(encoding="utf-8"))
                _self_check_state["result"] = res
        except Exception:
            res = None
        return res

    def _ck_load_report_from_disk() -> None:
        """启动时把上次的报告读回内存: 页面刷新/服务重启后仍能看到上次结果并逐条忽略。

        必须同时读 `suspicious` **和** `todos` 两个字段 (按 (type, ref) 去重):
        落盘格式里两个键都可能承载条目 (老版本只写 suspicious; 手写/第三方写的报告
        常把待办放 todos)。2026-09-18 实测踩到: 只读 suspicious 时, 放在 todos 里的
        待办项在重启后凭空消失 —— 叠到"复检"上就更糟: 它会显得"问题自己没了"。
        """
        if _check_state.get("report"):
            return
        try:
            p = Path(paths.data_root()) / "self_check.json"
            if not p.is_file():
                return
            old = json.loads(p.read_text(encoding="utf-8"))
            merged: "list[dict]" = []
            seen: "set[tuple]" = set()
            for src in (old.get("suspicious") or []), (old.get("todos") or []):
                for x in src:
                    if not isinstance(x, dict):
                        continue
                    k = (str(x.get("type") or ""), str(x.get("ref") or ""))
                    if k in seen:
                        continue
                    seen.add(k)
                    merged.append(x)
            for i, it in enumerate(merged):
                # 旧结构里有些条目 (corrupt / 整步失败的提示) 没带任何标识 → 用下标兜底,
                # 保证每条都能被单独「忽略」(前端按下标取条目, 缺 ref 会让忽略按钮报错)
                if not it.get("ref"):
                    it["ref"] = str(it.get("artwork_id") or it.get("name")
                                    or it.get("path") or it.get("md5")
                                    or f"idx:{i}")
            _check_state["report"] = {
                "time": old.get("time"),
                "suspicious": [x for x in merged if x.get("type") in _CK_SUSPICIOUS],
                "todos": [x for x in merged if x.get("type") not in _CK_SUSPICIOUS],
                "ignored": old.get("ignored", 0),
                "recheck": old.get("recheck") or {},
                # 用户点过「✕ 不再显示」→ 顶部那条汇总保持隐藏 (下次真跑体检才恢复)
                "bar_dismissed": old.get("bar_dismissed") or "",
                "from_disk": True,
            }
            _self_check_state["result"] = old
            # 老文件的 layout 可能与内存里规范化后的不一致 —— 老写法把 todos 拼进了
            # suspicious(同一件作品在文件里存了两份)。读回来顺手按新 layout 回写一次,
            # 免得用户/其它读文件的地方看到重复条目 ("同一条出现两次")。
            _rep = _check_state["report"]
            if (list(old.get("suspicious") or []) != list(_rep.get("suspicious") or [])
                    or list(old.get("todos") or []) != list(_rep.get("todos") or [])
                    or "bar_dismissed" not in old):
                _save_self_check_compat()
        except Exception:
            pass

    def _ck_dirty_scopes(name: str) -> tuple:
        """体检某一步改动了哪些数据域 (非 HTTP 触发 → 显式登记, 走同一套事件驱动刷新)。"""
        return {"reconcile": ("artworks", "pages", "facets", "stats"),
                "offline": ("authors", "artworks", "pages", "facets", "stats", "sources"),
                "authors": ("authors", "facets", "stats"),
                "metadata": ("artworks", "authors", "facets", "stats"),
                "organize": ("artworks", "pages", "facets", "stats"),
                "report": ("check",)}.get(name, ())

    def _ck_run(names: "list[str]", ctx: "dict | None" = None,
                key: "str | None" = None) -> dict:
        """依次跑给定步骤。每步独立: 某步异常只记录该步失败, 不拖垮其余步骤
        (旧实现 once error 全盘皆输)。返回各步汇总出来的自动修复计数。"""
        rep = _reporter()
        ctx = ctx if ctx is not None else {}
        fixed: dict = {}
        batch = len(names) > 1
        key = key or ("check:all" if batch else f"check:{names[0]}")
        _check_state["journal_key"] = key
        _journal_start(key,
                       "🧹 体检（全部）" if batch else f"🩺 体检 · {_check_title(names[0])}",
                       resumable=all(_check_resumable(n) for n in names),
                       steps=list(names))
        ok_all = True
        for i, name in enumerate(names):
            title = _check_title(name)
            _check_state.update(running=name)
            _ck_progress(name, 0, 0, "")
            rep.log(f"🩺 [体检] {title} …")
            try:
                res = _CK_FN[name](ctx)
                # 「本次复检过哪些来源」= 成功跑完的步骤。⑥ 复检报告靠它判断能不能把
                # 上一版的某条问题判为"已解决"; 失败/没跑的步骤绝不参与判定。
                ctx.setdefault("_ran", set()).add(name)
                summary = {k: v for k, v in res.items() if not k.startswith("_")}
                _check_state["steps"][name] = {
                    "ok": True, "at": _now_str(), "summary": summary, "error": ""}
                if name == "report":
                    _check_state["report"] = {
                        "time": _now_str(),
                        "suspicious": list(res.get("_suspicious") or []),
                        "todos": list(res.get("_todos") or []),
                        "ignored": res.get("ignored", 0),
                        "corrupt_verified": bool(res.get("_corrupt_verified", True)),
                        # 复检结论 ("已解决的清掉、变了的更新" → 要让他看见结果)
                        "recheck": {
                            "at": _now_str(),
                            "steps_ran": list(res.get("_steps_ran") or []),
                            "resolved": int(res.get("resolved", 0)),
                            "changed": int(res.get("changed", 0)),
                            "added": int(res.get("added", 0)),
                            "unverified": int(res.get("unverified", 0)),
                            "resolved_items": list(res.get("_resolved") or [])[:200],
                            "changed_items": list(res.get("_changed") or [])[:200],
                        },
                    }
                    _save_self_check_compat()
                else:
                    fixed.update({k: v for k, v in res.items()
                                  if isinstance(v, int) and not k.startswith("_")})
                rep.log(f"🩺 [体检] {title} 完成：{_check_summary_text(name, res)}")
                mark_dirty(*_ck_dirty_scopes(name))   # 每步完成各自登记数据域
                _journal_step_done(key, name, names[i + 1] if i + 1 < len(names) else None)
                _journal_write(key, "running",
                               note=f"{title} 完成（{_check_summary_text(name, res)}）")
            except Exception as e:
                ok_all = False
                err = str(e).splitlines()[0]
                _check_state["steps"][name] = {
                    "ok": False, "at": _now_str(), "summary": {}, "error": err}
                rep.error(f"🩺 [体检] {title} 出错: {err}")
                _journal_step_done(key, name, names[i + 1] if i + 1 < len(names) else None)
                _journal_write(key, "running", note=f"{title} 出错：{err}")
        _check_state.update(running=None)
        _check_state["progress"] = {}
        _check_state["last_run"] = {"steps": list(names), "at": _now_str(), "fixed": fixed,
                                    "ok": ok_all}
        _journal_finish(key, ok=ok_all,
                        note="体检完成" if ok_all
                             else "有步骤出错（其余步骤已完成，可单独重跑出错的那项）")
        return fixed

    def _ck_start_bg(names: "list[str]", key: "str | None" = None,
                     thread_name: str = "check") -> None:
        def _task():
            try:
                _ck_run(names, key=key)
                if len(names) > 1:
                    rep = _check_state.get("report") or {}
                    _reporter().log(
                        f"🩺 体检完成：可疑 {len(rep.get('suspicious') or [])} 项 · "
                        f"待办 {len(rep.get('todos') or [])} 项"
                        f"（已忽略 {rep.get('ignored', 0)} 项）")
            except Exception as e:
                _reporter().error(f"体检异常: {str(e).splitlines()[0]}")
                _check_state.update(running=None, progress={})
        threading.Thread(target=_task, daemon=True, name=thread_name).start()

    # ---------- 体检接口 ----------
    @app.route("/api/library/check", methods=["POST"])
    def api_library_check():
        """跑体检的**单个**步骤: body {"step": reconcile|offline|authors|metadata|
        organize|report} 或 {"all": true} 依次跑全部。后台执行, 进度看 check-status。"""
        if _check_state.get("running"):
            return jsonify({"status": "error",
                            "error": f"「{_check_title(_check_state['running'])}」正在运行，"
                                     f"请等它结束",
                            "running": _check_state["running"]}), 409
        data = request.get_json(silent=True) or {}
        if data.get("all"):
            names = list(_CHECK_STEP_NAMES)
        else:
            step = str(data.get("step") or "").strip()
            if step not in _CK_FN:
                return jsonify({"status": "error",
                                "error": f"未知的体检项: {step or '(空)'}"}), 400
            names = [step]
        if not data.get("all"):
            _check_state["steps"].pop(names[0], None)
        _ck_start_bg(names)
        return jsonify({"status": "started", "steps": names})

    @app.route("/api/library/check-status")
    def api_library_check_status():
        """体检面板状态: 每项最近结果 + 当前进度 + 报告计数。"""
        rep = _check_state.get("report") or {}
        return jsonify({
            "running": _check_state.get("running"),
            "progress": _check_state.get("progress") or {},
            "steps": _check_state.get("steps") or {},
            "outline": [{"name": n, "title": t, "desc": _CHECK_DESC_OF[n]}
                        for n, t, _d in _CHECK_STEPS],
            "last_run": _check_state.get("last_run"),
            "report_count": {"suspicious": len(rep.get("suspicious") or []),
                             "todos": len(rep.get("todos") or []),
                             "ignored": rep.get("ignored", 0),
                             "time": rep.get("time")},
        })

    @app.route("/api/library/check-report")
    def api_library_check_report():
        """体检报告: 分「🔧 系统异常(可一键修)」与「✋ 缺信息(需人工补齐)」。

        返回前会做一次**本地轻量复核**(节流 60s): 缺作者/缺文件/空夹/散落图这些
        本地就能判定的条目, 已经解决了就顺手清掉 —— 
        「处理完了还一直显示」, 不必为消掉一条去跑一整轮体检。
        """
        rep = _check_state.get("report")
        if rep is None:
            _ck_load_report_from_disk()
        _ck_reverify_local()
        rep = _check_state.get("report")
        with _db() as db:
            ignored = db.dismissed_issues()
        return jsonify({"status": "ok", "report": rep,
                        "ignored_items": ignored,
                        "running": _check_state.get("running")})

    @app.route("/api/library/check-report/dismiss", methods=["POST"])
    def api_library_check_report_dismiss():
        """把图库顶部那条「🩺 上次体检：…」汇总收起来。

        只隐藏**这条汇总提示**(含「查看详情」按钮), 报告本身不删、体检面板照旧能看;
        下次真跑体检时会自动重新显示(新报告不带这个标记)。
        """
        rep = _check_state.get("report")
        if not isinstance(rep, dict):
            return jsonify({"status": "ok", "dismissed": False})
        from datetime import datetime as _dt
        rep["bar_dismissed"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_self_check_compat()
        return jsonify({"status": "ok", "dismissed": True,
                        "at": rep["bar_dismissed"]})

    @app.route("/api/library/check-ignore", methods=["POST"])
    def api_library_check_ignore():
        """忽略 / 恢复一条体检条目 (待办项能"不再提示")。
        body: {kind, ref, undo?: true}"""
        data = request.get_json(silent=True) or {}
        kind = str(data.get("kind") or "").strip()
        ref = str(data.get("ref") or "").strip()
        if not kind or not ref:
            return jsonify({"status": "error", "error": "缺少 kind / ref"}), 400
        undo = bool(data.get("undo"))
        with _db() as db:
            if undo:
                n = db.restore_issue(kind, ref)
            else:
                db.dismiss_issue(kind, ref, str(data.get("note") or ""))
                n = 1
            ignored = db.dismissed_issues()
        # 同步从当前报告里摘掉 (前端不用等下次体检) —— 恢复时条目要等下次体检才重新列出,
        # 但「已忽略 N 项」这个计数当场就能校准, 否则用户看到恢复后数字没变会以为没生效
        rep = _check_state.get("report")
        if isinstance(rep, dict):
            if not undo:
                for bucket in ("suspicious", "todos"):
                    rep[bucket] = [x for x in (rep.get(bucket) or [])
                                   if not (x.get("type") == kind
                                           and str(x.get("ref")) == ref)]
                # 落盘那份也一起摘 (两个字段都摘) —— 否则重启读盘会把它合并回来
                _ck_legacy_drop(kind, ref)
            rep["ignored"] = len(ignored)
        audit_log.record("update", "体检恢复忽略" if undo else "体检忽略",
                         f"{kind} · {ref}", n)
        return jsonify({"status": "ok", "affected": n, "ignored_count": len(ignored),
                        "ignored_items": ignored})

    @app.route("/api/library/check-ignored")
    def api_library_check_ignored():
        """已忽略的体检条目 (可在这里一键恢复显示)。"""
        with _db() as db:
            return jsonify({"status": "ok", "items": db.dismissed_issues()})

    # ---------- 任务运行记录: 异常中断检测 / 续跑 ----------
    @app.route("/api/tasks/interrupted")
    def api_tasks_interrupted():
        """上次异常中断的任务 (进程被守护线程砍断留下的记录), 前端启动时提示是否继续。"""
        items = _journal_pending()
        return jsonify({"status": "ok", "items": items, "count": len(items)})

    @app.route("/api/tasks/resume", methods=["POST"])
    def api_tasks_resume():
        """继续上次中断的任务 (只重跑没跑完的步骤; 每步都幂等, 不会有副作用叠加)。"""
        data = request.get_json(silent=True) or {}
        key = str(data.get("key") or "").strip()
        t = {x["key"]: x for x in _journal_pending()}.get(key)
        if not t:
            return jsonify({"status": "error",
                            "error": "该任务已不在待处理列表（可能已被忽略或已跑完）"}), 400
        if not t.get("resumable"):
            return jsonify({"status": "error",
                            "error": f"「{t.get('title')}」不支持中途接续，请重新运行"}), 400
        if _check_state.get("running"):
            return jsonify({"status": "error", "error": "已有体检任务在跑，请等它结束"}), 409
        remain = [s for s in (t.get("remain") or []) if s in _CK_FN]
        if not remain:
            _journal_ack(key)
            return jsonify({"status": "error", "error": "没有可继续的步骤"}), 400
        _journal_ack(key)          # 先消掉待处理提示, 避免重复弹窗
        _ck_start_bg(remain, key=key, thread_name="check-resume")
        return jsonify({"status": "started", "steps": remain,
                        "title": t.get("title") or "体检"})

    @app.route("/api/tasks/ack", methods=["POST"])
    def api_tasks_ack():
        """用户选择"不继续" → 标记已确认, 不再提示。"""
        data = request.get_json(silent=True) or {}
        key = str(data.get("key") or "").strip()
        if not key:
            return jsonify({"status": "error", "error": "缺少 key"}), 400
        _journal_ack(key)
        return jsonify({"status": "ok", "items": _journal_pending()})

    # ---------- 兼容入口 (旧按钮/旧测试仍在用) ----------
    @app.route("/api/library/self-check", methods=["POST"])
    def api_library_self_check():
        """一键自检 (兼容入口): 等价于依次跑完 6 个体检步骤。

        保留它是因为「一次全跑一遍」仍是常用操作; 但每一步现在都能单独跑
        (见 /api/library/check), 出错不会再"整体白跑", 中断也不会前功尽弃。"""
        if _check_state.get("running"):
            return jsonify({"status": "running", "step": _check_state["running"]})
        _check_state["steps"] = {}
        _ck_start_bg(list(_CHECK_STEP_NAMES))
        return jsonify({"status": "started"})

    @app.route("/api/library/self-check-status")
    def api_library_self_check_status():
        """兼容旧轮询: running + 报告 (旧前端靠 !running && result 判断结束)。"""
        return jsonify({"running": bool(_check_state.get("running")),
                        "step": _check_state.get("running"),
                        "progress": _check_state.get("progress") or {},
                        "result": _self_check_state.get("result")})

    @app.route("/api/library/self-check-last")
    def api_library_self_check_last():
        """最近一次体检报告 (含明细), 供随时回看。

        内存里有就用内存的 (最新); 否则读落盘的 self_check.json —— 页面刷新或
        服务重启后仍能看到上次结果。"""
        res = _ck_read_legacy_result()
        return jsonify({"running": bool(_check_state.get("running")), "result": res})

    @app.route("/api/library/self-check-dismiss", methods=["POST"])
    def api_library_self_check_dismiss():
        """审批窗口: 逐条处理完「缺作者名」后, 把它从最近一次报告里去掉。

        等价于「忽略」的旧写法 (内存 + 落盘 self_check.json 同步), 保留兼容旧入口。
        2026-09-18 修: 落盘那份必须**两个字段都删** —— 老写法只删 suspicious, todos 里
        还留着一份, 重启读盘时又合并回来, 用户看到的就是"处理完了这条还在"。
        """
        data = request.get_json(silent=True) or {}
        aid = str(data.get("artwork_id") or "").strip()
        res = _ck_read_legacy_result()
        if not res or not isinstance(res, dict):
            return jsonify({"status": "ok", "removed": 0, "result": res})
        removed = _ck_legacy_drop("no_author", aid, artwork_id=aid) if aid else 0
        rep = _check_state.get("report")
        if isinstance(rep, dict) and aid:
            for bucket in ("suspicious", "todos"):
                rep[bucket] = [
                    x for x in (rep.get(bucket) or [])
                    if not (x.get("type") == "no_author"
                            and (str(x.get("artwork_id")) == aid
                                 or str(x.get("ref")) == aid))
                ]
        return jsonify({"status": "ok", "removed": removed,
                        "result": _ck_read_legacy_result()})

    @app.route("/api/logs/operations")
    def api_logs_operations():
        """📋 操作记录 (审计): 图库/作者的增/改/删历史, 供设置页查看。
        query: limit=200, kind=add|update|delete (空=全部)。"""
        limit = request.args.get("limit", "200")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 200
        kind = (request.args.get("kind") or "").strip()
        return jsonify(audit_log.read(limit=limit, kind=kind))

    @app.route("/api/logs/operations/clear", methods=["POST"])
    def api_logs_operations_clear():
        """清空操作记录 (不影响任何图库数据, 只删审计文本)。"""
        return jsonify({"status": "ok", "cleared": audit_log.clear()})

    @app.route("/api/library/dedup", methods=["POST"])
    def api_library_dedup():
        """🧹 清理重复副本: 按 MD5 分组, 每组保留作者文件夹内的一份, 删其余。"""
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        root_resolved = img_root.resolve()
        removed, kept, groups = [], [], 0
        with _db() as db:
            rec = db.reconcile_output(img_root, verify_corrupt=False)
            for dup in (getattr(rec, "duplicates", None) or []):
                rels = [r for r in (dup.get("paths") or []) if r]
                if len(rels) < 2:
                    continue
                groups += 1

                def _score(rel):
                    # 优先层级深的(在作者文件夹内), 其次路径短的
                    return (-len(Path(rel).parts), len(rel))
                keeper = sorted(rels, key=_score)[0]
                for rel in rels:
                    if rel == keeper:
                        kept.append(rel)
                        continue
                    victim = img_root / rel
                    try:
                        if victim.resolve().is_relative_to(root_resolved):
                            victim.unlink()
                            removed.append(rel)
                    except Exception:
                        pass
            # 删后再对账, 让 DB 状态收敛
            if removed:
                db.reconcile_output(img_root, verify_corrupt=False)
        if removed:
            audit_log.record("delete", "清理重复副本",
                             f"删除 {len(removed)} 个重复副本（{groups} 组，每组保留一份）",
                             len(removed))
        return jsonify({"status": "ok", "groups": groups,
                        "removed": removed, "kept": kept})

    @app.route("/api/library/backup", methods=["POST"])
    def api_library_backup():
        with _db() as db:
            p = db.backup_to_zip()
        return jsonify({"status": "ok", "path": str(p)})

    @app.route("/api/library/tidy/preview", methods=["POST"])
    def api_library_tidy_preview():
        """一键整理预览: 扫描图库生成规范化计划 (不移动任何文件)。"""
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        plans = []
        with _db() as db:
            plans = _tidy_build_plans(db, img_root)
        counts = {
            "rename": sum(1 for p in plans if p["type"] == "rename"),
            "move": sum(1 for p in plans if p["type"] == "move"),
            "categorize": sum(1 for p in plans if p["type"] == "categorize"),
            "filrename": sum(1 for p in plans if p["type"] == "filrename"),
            "rmdir": sum(1 for p in plans if p["type"] == "rmdir"),
            "files": sum(len(p["files"]) for p in plans),
            "auto": sum(1 for p in plans if p["auto"]),
            "need_confirm": sum(1 for p in plans if not p["auto"]),
            "total": len(plans),
        }
        return jsonify({"status": "ok", "plans": plans, "counts": counts})

    @app.route("/api/library/tidy/apply", methods=["POST"])
    def api_library_tidy_apply():
        """执行整理计划 (移动+改名+同步 local_path), 随后强制对账。"""
        data = request.get_json(silent=True) or {}
        plan_ids = data.get("plans")  # None = 全部
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        with _db() as db:
            rep = _tidy_apply_plans(db, img_root, plan_ids)
        try:
            _cached_reconcile(force=True)
        except Exception:
            pass
        _n = (int(rep.get("renamed", 0) or 0) + int(rep.get("moved", 0) or 0)
              + int(rep.get("filenamed", 0) or 0) + int(rep.get("rmdirs", 0) or 0))
        if _n:
            _fl = rep.get("failed") or []
            _parts = []
            if rep.get("renamed"):
                _parts.append(f"目录规范 {rep['renamed']} 个")
            if rep.get("moved"):
                _parts.append(f"位置规范 {rep['moved']} 张")
            if rep.get("filenamed"):
                _parts.append(f"命名规范 {rep['filenamed']} 个")
            if rep.get("rmdirs"):
                _parts.append(f"清理空夹 {rep['rmdirs']} 个")
            audit_log.record("update", "一键整理",
                             "，".join(_parts) + (f"（{len(_fl)} 项失败）" if _fl else ""), _n)
        return jsonify({"status": "ok", **rep})

    @app.route("/api/library/tidy/adopt-claim", methods=["POST"])
    def api_library_tidy_adopt_claim():
        """采纳「文件夹名」作为作者归属 —— 用户在「待确认」清单里判断后的回流写入。
        单向闭环里的唯一写库入口: 只改作品的作者归属; 作者已建档则**不动它的名字**
        (库里的现用名是权威值, 夹名只是用来定位"这个作品属于谁"), 未建档才用夹名建。"""
        data = request.get_json(silent=True) or {}
        artwork_id = str(data.get("artwork_id") or "").strip()
        claim_aid = str(data.get("author_id") or "").strip()
        claim_name = str(data.get("author_name") or "").strip()
        if not artwork_id or not claim_aid.isdigit():
            return jsonify({"status": "error", "message": "缺少作品 ID 或 pixiv 作者 ID"}), 400
        with _db() as db:
            if db.get_author(claim_aid) is None:
                if not claim_name:
                    return jsonify({"status": "error",
                                    "message": "该作者尚未建档且文件夹名里没有可用名字"}), 400
                db.upsert_author(claim_aid, claim_name)
            updated = db.set_artworks_author([artwork_id], claim_aid)
            row = db.conn.execute(
                "SELECT name FROM authors WHERE author_id=?", (claim_aid,)).fetchone()
            name = row[0] if row else claim_name
        if updated:
            audit_log.record("update", "确认作者归属",
                             f"{_labels_inline([artwork_id])} 归入作者「{name}」({claim_aid})",
                             updated, ids=[artwork_id])
        return jsonify({"status": "ok", "updated": updated,
                        "author_id": claim_aid, "name": name})

    # ---------------- 反搜来源 ----------------
    @app.route("/api/reverse/status")
    def api_reverse_status():
        # 待反搜图片放在独立输入目录 (input/), 不和图库 img/ 混放
        # 必须 .resolve: `_rev_resolve_input` 会把候选路径 resolve() 后再判"是否在 input/ 内",
        # 两边形式不一致时(短名 8.3 路径 / 大小写)会被当成"越界"而全部拒绝 ——
        # 2026-09-18 实测就因此把还在收件箱的文件误判成"已不在", 连带把它的失败记录剪掉。
        input_root = Path(paths.user_data(
            _config["paths"].get("input_subdir", "input"))).resolve()
        input_root.mkdir(parents=True, exist_ok=True)
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()
        IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        pending = []
        # **递归**列: 之前只扫顶层, 而 SauceNAO 模式把未命中的图挪进了 input/not_found/ →
        # 那些图从待反搜区消失、再也勾不到, 用户以为"卡住了没法处理"。
        # 现在连子目录一起列, 并带 sub 标记, 前端显示成「未找到」并给「手动入库 / 丢弃」出口。
        for p in sorted(input_root.rglob("*")):
            if not (p.is_file() and p.suffix.lower() in IMG_EXT):
                continue
            rel = p.relative_to(input_root).as_posix()
            sub = rel.split("/")[0] if "/" in rel else ""
            pending.append({
                "name": p.name,
                "rel": rel,
                "sub": sub,                      # '' = 顶层; 'not_found' = 上次未识别被挪进来的
                "not_found": sub == "not_found",
                "url": f"/api/local-img?root=input&p={quote(rel)}&thumb=1",
            })
        # 最近一次反搜结果 (由 /api/reverse/run 完成后写入)
        # 先清掉"文件已不在收件箱"的过时条目: 否则入库/丢弃之后, 那些「未识别」失败项
        # 每次刷新又被重建出来, 用户点清空也没用 
        _rev_prune_results(input_root)
        last_result = None
        rp = Path(paths.data_root()) / "reverse_results.json"
        if rp.is_file():
            try:
                last_result = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:
                last_result = None
        return jsonify({
            "scan_dir": str(input_root),
            "image_files": len(pending),
            "pending": pending,
            "last_result": last_result,
            "progress": dict(_REVERSE_PROGRESS),
            "src_search": dict(_SRC_SEARCH),
        })

    def _rev_prune_results(input_root: Path) -> int:
        """清掉「最近一次反搜结果」里**已经不在收件箱**的过时条目 (成功归档的那批除外)。

        手动入库/丢弃之后, 任务表里那些「未识别」失败项**反复出现**,
        点清空也没用 —— 因为任务表是从 `reverse_results.json` 重建的, 而入库/丢弃/清空
        **谁都没动过这个文件**, 于是每次刷新又把那几条捞回来, 还反过来提示"文件仍在待反搜区"
        (实际收件箱早空了)。
        规则: `failed / conflict / skipped / in_library` 这几类本来都对应"文件还在收件箱里等处理",
        文件一旦不在里面了(入库/丢弃/清空/手工搬走), 这条记录就过时 → 删掉;
        `success`(已归档) 是历史记录, 它的文件本来就搬进图库了, 保留。
        返回清掉的条数 (只在真有变化时写盘)。
        """
        rp = Path(paths.data_root()) / "reverse_results.json"
        if not rp.is_file():
            return 0
        try:
            res = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            return 0
        if not isinstance(res, dict):
            return 0
        dropped = 0
        for key in ("failed", "conflict", "skipped", "in_library"):
            arr = res.get(key)
            if not isinstance(arr, list):
                continue
            keep = []
            for x in arr:
                name = str(x.get("original_file") or x.get("file") or "") \
                    if isinstance(x, dict) else str(x or "")
                if name and _rev_resolve_input(input_root, name) is None:
                    dropped += 1
                    continue
                keep.append(x)
            res[key] = keep
        if dropped:
            try:
                rp.write_text(json.dumps(res, ensure_ascii=False, indent=1),
                              encoding="utf-8")
            except Exception:
                pass
        return dropped

    def _rev_resolve_input(input_root: Path, rel: str) -> "Path | None":
        """把前端给的"待反搜文件"解析成 input/ 下的真实文件, 含两道护栏:
        ① 只接受 input/ 内部的路径(防 ../ 穿越); ② 裸文件名也允许, 依次在顶层与 not_found/ 找。

        `input_root` 在这里**再 resolve 一次**: 候选路径下面会 resolve 后比 parents, 调用方传进来的
        若是未 resolve 的短名/异形路径, 两边形式不一致 → 全部被误判成"越界"。2026-09-18 实测
        `api_reverse_status` 就漏了 resolve, 把还在收件箱的文件判成"已不在", 剪掉了它的失败记录。
        """
        import os as _os
        try:
            input_root = Path(input_root).resolve()
        except Exception:
            pass
        rel = str(rel or "").replace("\\", "/").strip().lstrip("/")
        if not rel:
            return None
        cands = [rel] if "/" in rel else [rel, f"not_found/{rel}"]
        for c in cands:
            f = (input_root / c)
            try:
                f = f.resolve()
            except Exception:
                continue
            if f != input_root and input_root not in f.parents:
                continue                      # 穿过 input/ 边界 → 拒绝
            if f.is_file():
                return f
        return None

    @app.route("/api/reverse/adopt", methods=["POST"])
    def api_reverse_adopt():
        """**手动入库**: 把反搜收件箱里的图直接收进图库 (建自定义作品)。

        为什么需要:
        反搜只有"命中才自动归档"这一条路, 没命中的图此前**没有任何出口** —— 永远堆在 input/。
        现在允许使用者自行决定: 收进 img/#其他来源#/ 并登记成 ext_ 壳作品 (标题=文件名),
        与导入时的「建自定义作品」完全同一套落地方式。

        body: {"files": ["a.png", "not_found/b.png", ...]}
        """
        import shutil as _sh
        data = request.get_json(silent=True) or {}
        rels = data.get("files")
        if not isinstance(rels, list) or not rels:
            return jsonify({"status": "error", "error": "缺少 files"}), 400
        input_root = Path(paths.user_data(
            _config["paths"].get("input_subdir", "input"))).resolve()
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()
        dest_dir = img_root / _CUSTOM_SRC_DIR
        IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        imported, skipped, failed = [], [], []
        # 单次上限: 超出的必须**如实告诉用户**, 不能悄悄丢掉 (自查清单: "静默失败"类)
        CAP = 500
        over_limit = max(0, len(rels) - CAP)
        for rel in rels[:CAP]:
            src = _rev_resolve_input(input_root, rel)
            if src is None:
                failed.append({"file": str(rel), "reason": "找不到该文件（可能已被处理/移走）"})
                continue
            if src.suffix.lower() not in IMG_EXT:
                skipped.append({"file": src.name, "reason": "不是图片"})
                continue
            target = dest_dir / src.name
            if target.exists():
                # 同名已存在: 绝不覆盖 (宁可不导, 也不动用户已有的那份)
                skipped.append({"file": src.name, "reason": "图库里已有同名文件"})
                continue
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                _sh.copy2(str(src), str(target))       # 先复制入库 (此步失败源文件不动)
            except Exception as e:
                failed.append({"file": src.name, "reason": f"复制失败: {e}"})
                continue
            rel_img = str(target.relative_to(img_root)).replace("\\", "/")
            ok = False
            try:
                with _db() as db:
                    ok = bool(db.register_foreign_file(rel_img))
                    if ok:
                        db.cleanup_orphans()
            except Exception as e:
                failed.append({"file": src.name, "reason": f"登记失败: {e}"})
                continue
            if not ok:
                # 登记不上 (例如已登记过同一路径) → 把复制进来的副本撤掉, 别留半成品
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                skipped.append({"file": src.name, "reason": "已在图库登记过"})
                continue
            try:
                src.unlink(missing_ok=True)            # 入库成功才收回件箱里的源文件
            except OSError:
                pass
            imported.append(rel_img)
        if imported:
            audit_log.record("add", "反搜收件箱手动入库",
                             f"把 {len(imported)} 张待反搜图片收进图库（建自定义作品）："
                             + "；".join(Path(x).name for x in imported[:5])
                             + ("…" if len(imported) > 5 else ""),
                             len(imported))
            mark_dirty("artworks", "pages", "facets", "stats", "reverse")
        return jsonify({"status": "ok", "imported": imported,
                        "skipped": skipped, "failed": failed,
                        "over_limit": over_limit})

    @app.route("/api/reverse/discard", methods=["POST"])
    def api_reverse_discard():
        """把待反搜区里选中的图**丢弃** —— 送进 Windows 电脑回收站(可还原), 不是硬删。

        body: {"files": [...]}  传 ["*"] 表示全部
        """
        data = request.get_json(silent=True) or {}
        rels = data.get("files")
        if not isinstance(rels, list) or not rels:
            return jsonify({"status": "error", "error": "缺少 files"}), 400
        input_root = Path(paths.user_data(
            _config["paths"].get("input_subdir", "input"))).resolve()
        IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        targets: "list[Path]" = []
        CAP = 500
        over_limit = 0
        if rels == ["*"]:
            targets = [p for p in sorted(input_root.rglob("*"))
                       if p.is_file() and p.suffix.lower() in IMG_EXT]
        else:
            over_limit = max(0, len(rels) - CAP)
            for rel in rels[:CAP]:
                f = _rev_resolve_input(input_root, rel)
                if f is not None:
                    targets.append(f)
        if not targets:
            return jsonify({"status": "ok", "removed": 0, "recycled": 0, "over_limit": over_limit})
        recycled, bad = _recycle_files([str(p) for p in targets])
        # 清掉搬空后的 not_found/ 之类空目录
        try:
            for d in sorted(input_root.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        except Exception:
            pass
        if recycled:
            audit_log.record("delete", "丢弃待反搜图片（已送电脑回收站）",
                             f"丢弃 {recycled} 张待反搜图片："
                             + "；".join(p.name for p in targets[:5])
                             + ("…" if len(targets) > 5 else ""), recycled)
            mark_dirty("reverse")
        return jsonify({"status": "ok", "removed": len(targets), "recycled": recycled,
                        "over_limit": over_limit,
                        "failed": [p.name for p in targets][:5] if bad else []})


    @app.route("/api/reverse/clear", methods=["POST"])
    def api_reverse_clear():
        """清空待反搜区: 把 input/ 下所有图片 (含 not_found 子目录) 送进 **Windows 电脑回收站**。

        2026-09-18 改为走回收站 (原来是硬删 unlink): 用户定过规矩 —— 即使最深层的删除
        也只该到电脑回收站, 留一条退路; 这里删的是用户自己导入的图, 更不该例外。
        """
        input_root = Path(paths.user_data(
            _config["paths"].get("input_subdir", "input"))).resolve()
        IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
        targets: "list[Path]" = []
        if input_root.exists():
            targets = [p for p in sorted(input_root.rglob("*"))
                       if p.is_file() and p.suffix.lower() in IMG_EXT]
        recycled, _bad = _recycle_files([str(p) for p in targets])
        try:
            for d in sorted(input_root.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        except Exception:
            pass
        if recycled:
            audit_log.record("delete", "清空待反搜（已送电脑回收站）",
                             f"清空待反搜区 {recycled} 张图片", recycled)
        return jsonify({"ok": True, "removed": len(targets), "recycled": recycled})

    @app.route("/api/reverse/clear-result", methods=["POST"])
    def api_reverse_clear_result():
        """清空「最近一次反搜结果」面板 (删除 reverse_results.json)。"""
        rp = Path(paths.data_root()) / "reverse_results.json"
        try:
            if rp.exists():
                rp.unlink()
        except OSError:
            pass
        return jsonify({"ok": True})

    @app.route("/api/reverse/run", methods=["POST"])
    def api_reverse_run():
        data = request.get_json(silent=True) or {}
        # 单飞 (2026-09-15 审计): 反搜会移动 input 文件 + 落库, 并发两单会互相抢文件。
        if _REVERSE_PROGRESS.get("running"):
            return jsonify({"status": "error",
                            "error": "已有反搜任务进行中，请等待当前批次完成"}), 409
        mode = data.get("mode") or "id"  # 'id' 按文件名ID直查pixiv | 'saucenao' 以图搜源
        # 勾选的图片 (basename 白名单); 不传 = 全部
        files = data.get("files")
        if files is not None:
            import os as _os
            files = [_os.path.basename(str(f).replace("\\", "/")) for f in files]
            files = [f for f in files if f]
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        input_root = Path(paths.user_data(_config["paths"].get("input_subdir", "input")))
        input_root.mkdir(parents=True, exist_ok=True)

        def _rel_to_imgroot(abs_path: str) -> str:
            """归档后文件绝对路径 → 相对 img_root 的路径 (供 /api/local-img 显示缩略图)。"""
            try:
                return str(Path(abs_path).resolve().relative_to(img_root.resolve())).replace("\\", "/")
            except Exception:
                return ""

        def _task():
            results = {"success": [], "failed": [],
                       "conflict": [], "in_library": [], "skipped": []}
            # 归档方式: True=自动归档(移动文件+落库) | False=仅识别(文件留在 input/, 结果面板展示)
            archive = bool(data.get("archive", True))
            # 归档时目标已有同名文件: copy=加副本 / skip=跳过 / overwrite=覆盖
            existing = data.get("existing") or "copy"
            if existing not in ("skip", "copy", "overwrite"):
                existing = "copy"
            # ---- 反搜进度 (前端 1s 轮询 /api/reverse/status 展示进度条+批次队列) ----
            rscfg = _config.get("reverse_search", {})
            bs = max(1, int(data.get("batch_size") or 0)
                     or int(rscfg.get("batch_size") or 5))
            bbr = max(1, float(data.get("batch_break_sec") or 0)
                      or float(rscfg.get("batch_break_sec") or 30))
            prog = _REVERSE_PROGRESS
            prog.update(running=True, total=0, done=0, current="",
                        phase="running", rest_sec=0, batch=0, error="",
                        batch_size=bs, batch_break_sec=bbr,
                        started_at=time.time(), finished_at=None)

            def _on_progress(idx, total, name, status, rest_sec=None):
                prog["done"] = int(idx or 0)
                prog["total"] = int(total or 0)
                prog["current"] = str(name or "")
                prog["phase"] = status
                prog["rest_sec"] = int(rest_sec or 0)
                if prog["batch_size"]:
                    prog["batch"] = (int(idx or 0) // int(prog["batch_size"])) + 1

            try:
                if mode == "saucenao":
                    from core.reverse_search import ReverseSearch
                    with ReverseSearch(str(input_root), reporter=_reporter(),
                                       config=_config, archive_path=str(img_root)) as rs:
                        results = rs.run(files=files, archive=archive,
                                         existing=existing, on_progress=_on_progress,
                                         batch_size=bs, batch_break_sec=bbr) or results
                else:
                    from core.reverse_source import ReverseSource
                    with ReverseSource(search_path=str(input_root), reporter=_reporter(),
                                       config=_config, archive_path=str(img_root)) as rs:
                        results = rs.run(files=files, archive=archive,
                                         existing=existing, on_progress=_on_progress,
                                         batch_size=bs, batch_break_sec=bbr) or results
            except Exception as e:
                _reporter().error(f"反搜任务异常: {str(e).splitlines()[0]}")
                # 异常不能静默吞掉: 把原因带给前端结果面板, 否则用户看到「归档0 未识别0」空面板
                # 以为结果没显示 (真实案例: saucenao 网络/驱动异常时 results 保持全空)
                err = f"反搜任务异常: {str(e).splitlines()[0]}"
                prog["phase"] = "error"
                prog["error"] = err
                if not any(results[k] for k in ("success", "failed", "conflict",
                                                "in_library", "skipped")):
                    results["failed"] = [{"file": "（任务异常）", "reason": err}]
            finally:
                # ① 归档文件入库: 反搜只移动文件, 靠 reconcile 补 pages/建 artworks, 图库立即可见
                #    「仅识别不归档」模式不移动文件, 跳过入库/落历史 (文件仍留在 input/)
                if archive:
                  try:
                    _cached_reconcile(force=True)
                    with _db() as db:
                        # 归档文件夹名规范为「作者名_作者ID」, 顺带补 authors 表,
                        # 图库作者列直接显示名称而不是裸 ID
                        from utils.hashutil import md5_of
                        src_label = "SauceNAO" if mode == "saucenao" else "PixivID"
                        # 信任级别 → confidence 数值 (仅展示/排序用, 1.0=字节级确定)
                        _trust_conf = {"hash": 1.0, "consistent": 0.8,
                                       "unverified": 0.4, "conflict": 0.0}
                        def _put_history(path, md5, hit_aid, hit_wid, trust, extra):
                            db.reverse_search_put(
                                path or "", md5, _trust_conf.get(trust, 0.0), src_label,
                                hit_author_id=hit_aid or None,
                                hit_artwork_id=hit_wid or None,
                                raw_json=json.dumps(
                                    {"trust": trust, **(extra or {})},
                                    ensure_ascii=False))
                        for s in results.get("success", []):
                            try:
                                folder = Path(s.get("dest") or "").parent.name
                                name, _, aid = folder.rpartition("_")
                                if aid.isdigit() and name:
                                    db.upsert_author(aid, name)
                                orig = s.get("original_file") or s.get("file") or ""
                                dest = s.get("dest") or ""
                                fmd5 = md5_of(dest) if dest else None
                                _put_history(orig, fmd5, aid, s.get("artwork_id"),
                                             s.get("trust") or "consistent",
                                             {"renamed": bool(s.get("renamed")),
                                              "archived_as": s.get("file"),
                                              "warning": "仅 SauceNAO 参考, 未人工确认"
                                                  if s.get("trust") == "unverified" else None})
                            except Exception:
                                pass
                        # 冲突项: 文件留在 input/ 不动, 双方主张都落库待人工裁决
                        for c in results.get("conflict", []):
                            try:
                                fmd5 = md5_of(str(input_root / c["file"]))
                                _put_history(c["file"], fmd5, None, c.get("saucenao_id"),
                                             "conflict",
                                             {"status": "conflict_unresolved",
                                              "filename_claim_id": c.get("filename_id"),
                                              "saucenao_id": c.get("saucenao_id"),
                                              "saucenao_author": c.get("saucenao_author")})
                            except Exception:
                                pass
                        # 指纹命中: 图库里已有同一张, 记录确定身份
                        for it in results.get("in_library", []):
                            try:
                                fmd5 = md5_of(str(input_root / it["file"]))
                                _put_history(it["file"], fmd5, None, it.get("artwork_id"),
                                             "hash", {"status": "already_in_library"})
                            except Exception:
                                pass
                        db.cleanup_orphans()
                  except Exception as e:
                      _reporter().warn(f"反搜后对账失败: {str(e).splitlines()[0]}")
                # ② 结果落盘 (含缩略图相对路径), 供前端结果面板展示
                try:
                    from datetime import datetime
                    for s in results.get("success", []):
                        rel = _rel_to_imgroot(s.get("dest", ""))
                        if rel:
                            s["url"] = f"/api/local-img?p={quote(rel)}&thumb=1"
                        elif s.get("archived") is False and s.get("file"):
                            # 仅识别不归档: 文件仍在 input/, 用 input 缩略图
                            s["url"] = f"/api/local-img?root=input&p={quote(s['file'])}&thumb=1"
                        else:
                            s["url"] = ""
                    # 冲突/已跳过/已在图库的文件仍留在 input/, 用 input 缩略图
                    for it in (list(results.get("conflict", []))
                               + list(results.get("in_library", []))
                               + list(results.get("skipped", []))):
                        it["url"] = f"/api/local-img?root=input&p={quote(it['file'])}&thumb=1"
                    payload = {
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "mode": mode,
                        "success": results.get("success", []),
                        "failed": results.get("failed", []),
                        "conflict": results.get("conflict", []),
                        "in_library": results.get("in_library", []),
                        "skipped": results.get("skipped", []),
                    }
                    rp = Path(paths.data_root()) / "reverse_results.json"
                    rp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception:
                    pass
            # 进度收尾: 结果已写完, 统一置位; 前端轮询到 running=False 即停并刷新结果面板
            prog["running"] = False
            prog["finished_at"] = time.time()
            if prog["phase"] in ("running", "break", "ok"):
                prog["phase"] = "done"

        threading.Thread(target=_task, daemon=True).start()
        return jsonify({"status": "started"})

    @app.route("/api/reverse/upload", methods=["POST"])
    def api_reverse_upload():
        """➕ 添加待反搜图片: multipart 多文件 → 存入 input/ (重名自动加 (2))。"""
        import os as _os
        input_root = Path(paths.user_data(_config["paths"].get("input_subdir", "input")))
        input_root.mkdir(parents=True, exist_ok=True)
        IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        saved, rejected = [], []
        for f in request.files.getlist("files") or request.files.getlist("images"):
            # 只取 basename, 防路径穿越
            base = _os.path.basename((f.filename or "").replace("\\", "/"))
            if not base or Path(base).suffix.lower() not in IMG_EXT:
                rejected.append(base or "(空文件名)")
                continue
            target = input_root / base
            if target.exists():  # 重名加 (2)/(3)…
                stem, suf = target.stem, target.suffix
                i = 2
                while (input_root / f"{stem} ({i}){suf}").exists():
                    i += 1
                target = input_root / f"{stem} ({i}){suf}"
            try:
                f.save(str(target))
                saved.append(target.name)
            except Exception:
                rejected.append(base)
        return jsonify({"status": "ok", "saved": saved, "rejected": rejected})

    @app.route("/api/library/import", methods=["POST"])
    def api_library_import():
        """📥 导入已下载好的图片到图库 (multipart 多文件):
        - 文件名含 {作品ID}_p{页码} (pixiv 规范命名) → 存入 img/ 根目录,
          对账登记 + 后台联网补标题/标签/作者 + 自动归入作者文件夹;
        - 文件名无作品 ID (截图/随便命名) → 转存 input/ 反搜收件箱, 去反搜页识别;
        - 只取 basename 防路径穿越; 重名自动加 (2)。
        批量场景也可以直接把文件/文件夹拷进 data/img 目录, 打开图库自动同步。"""
        import os as _os
        img_root = Path(paths.user_data(_config["paths"]["img_subdir"]))
        input_root = Path(paths.user_data(_config["paths"].get("input_subdir", "input")))
        img_root.mkdir(parents=True, exist_ok=True)
        input_root.mkdir(parents=True, exist_ok=True)
        IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        imported, redirected, rejected = [], [], []

        def _save_unique(folder: Path, base: str, f) -> str:
            target = folder / base
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 2
                while (folder / f"{stem} ({i}){suf}").exists():
                    i += 1
                target = folder / f"{stem} ({i}){suf}"
            f.save(str(target))
            return target.name

        for f in request.files.getlist("files") or request.files.getlist("images"):
            base = _os.path.basename((f.filename or "").replace("\\", "/"))
            if not base or Path(base).suffix.lower() not in IMG_EXT:
                rejected.append(base or "(空文件名)")
                continue
            try:
                if pixiv_natural_key(base):
                    imported.append(_save_unique(img_root, base, f))
                else:
                    # 无作品 ID: 图库无法登记归属, 转反搜收件箱
                    redirected.append(_save_unique(input_root, base, f))
            except Exception:
                rejected.append(base)
        # 落盘即对账登记 + 离线作者回填 + 根目录散落图即时归位/去重
        rep = _cached_reconcile(force=True)
        with _db() as db:
            _adopt_authors_from_folders(db, img_root)
            db.cleanup_orphans()
        if _organize_loose_images(img_root):
            rep = _cached_reconcile(force=True)  # 归位后 MD5 自愈刷新 local_path
        if rep.new_pages_linked > 0:
            _kick_metadata_fill()
        if imported or redirected:
            audit_log.record("add", "导入图片",
                             f"图库入库 {len(imported)} 张，转反搜收件箱 {len(redirected)} 张"
                             + (f"，跳过非图片 {len(rejected)} 个" if rejected else "")
                             + (f"，新登记 {rep.new_pages_linked} 页" if rep.new_pages_linked else ""),
                             len(imported) + len(redirected))
        return jsonify({"status": "ok", "imported": imported,
                        "redirected": redirected, "rejected": rejected,
                        "new_pages": rep.new_pages_linked,
                        "conflicts": getattr(rep, "conflicts", []) or []})

    # ---------------- ℹ️ 版本/构建信息 ----------------
    @app.route("/api/version")
    def api_version():
        """当前运行的构建信息 —— 排查「改了怎么没生效」时先看这里。

        built_at 取可执行文件的修改时间(即打包时刻); frontend 是 index.html 里引用的
        前端资源版本串, 用于确认浏览器加载的是不是最新前端。"""
        import sys as _sys
        exe = Path(getattr(_sys, "executable", "") or "")
        built_at = ""
        try:
            if exe.is_file():
                built_at = time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(exe.stat().st_mtime))
        except OSError:
            pass
        fe = ""
        try:
            html = (Path(__file__).resolve().parent / "static"
                    / "index.html").read_text(encoding="utf-8")
            m = re.search(r"app\.js\?v=([\w.\-]+)", html)
            fe = m.group(1) if m else ""
        except OSError:
            pass
        # 与页面实际拿到的版本串保持一致 (标签 + app.js/style.css 的 mtime 指纹):
        # 页面里显示的 frontend 与这里相同, 便于判断「浏览器是不是最新前端」
        if fe:
            fe = f"{fe}-{_asset_ver()}"
        return jsonify({
            "status": "ok",
            "built_at": built_at,
            "frontend": fe,
            "frozen": bool(getattr(_sys, "frozen", False)),
            "executable": str(exe),
            "python": _sys.version.split()[0],
        })

    # ---------------- 📁 从本地文件夹导入 (源文件进回收站, 可恢复) ----------------
    @app.route("/api/library/pick-folder", methods=["POST"])
    def api_library_pick_folder():
        """弹系统原生文件夹选择框; 返回 {path} (用户取消则 path 为 null)。"""
        return jsonify({"status": "ok", "path": _pick_folder_dialog()})

    @app.route("/api/library/pick-files", methods=["POST"])
    def api_library_pick_files():
        """弹系统原生「打开文件」框 (可多选图片); 返回 {paths: [...]} (取消则空数组)。

        为什么要两个入口: Windows 通用对话框一次只能「选多张文件」或「选一个文件夹」,
        没有二者合一的形态 —— 前端因此给两个按钮 (支持多选图片)。"""
        return jsonify({"status": "ok", "paths": _pick_files_dialog()})

    @app.route("/api/library/import-from-path", methods=["POST"])
    def api_library_import_from_path():
        """📁 把本地文件夹 / 选中的图片**搬进图库**: 先复制入库, 成功后源文件送**回收站**。

        body: {path}          —— 选一个文件夹 (递归其内所有图片)
              {paths: [...]}  —— 选中的多张图片
              {dry_run: true} —— 只统计不动文件 (前端先预览再确认)
          - 文件名含 pixiv 作品ID → 入库 img/ (对账登记 + 后台补元数据 + 归入作者夹)
          - 无 ID (截图等)        → 入库 input/ 反搜收件箱
          - 目标同名              → **跳过, 源文件留在原处** (
                                    宁可不导, 也不动源文件)
        语义变更: 原来是 shutil.move 硬搬 + 重复时 os.unlink 永久删,
        误选一个文件夹就再也找不回。现在一律「复制成功 → 源文件进回收站(可还原)」,
        送回收站失败只如实报告, 绝不回滚已入库的图。"""
        import shutil as _sh
        data = request.get_json(silent=True) or {}
        raw = str(data.get("path") or "").strip().strip('"')
        raw_list = data.get("paths")
        dry = bool(data.get("dry_run"))
        make_custom = bool(data.get("make_custom_artworks"))
        if not raw and not (isinstance(raw_list, list) and raw_list):
            return jsonify({"status": "error",
                            "error": "缺少 path（文件夹）或 paths（多张图片）"}), 400

        img_root = Path(paths.user_data(_config["paths"]["img_subdir"])).resolve()
        input_root = Path(paths.user_data(
            _config["paths"].get("input_subdir", "input"))).resolve()
        guarded = ((img_root, "图库图片目录"), (input_root, "反搜收件箱"))
        IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

        files: "list[Path]" = []          # 待导入的源图片 (绝对路径)
        src_label = ""                    # 预览里回显的"来源"
        if isinstance(raw_list, list) and raw_list:
            # 多选图片: 每个都要单独校验 (文件必须存在, 且不在图库/收件箱内部)
            picked = []
            for x in raw_list[:2000]:
                fp = Path(str(x or "").strip().strip('"'))
                if not fp.is_file():
                    return jsonify({"status": "error",
                                    "error": f"文件不存在：{fp}"}), 400
                res = fp.resolve()
                if res.suffix.lower() not in IMG_EXT:
                    continue          # 与文件夹模式一致: 非图片静默跳过
                for g, label in guarded:
                    if res.is_relative_to(g):
                        return jsonify({"status": "error",
                                        "error": f"不能从{label}内部导入"}), 400
                picked.append(res)
            files = picked
            src_label = f"选中的 {len(files)} 张图片"
        else:
            src = Path(raw)
            if not src.is_dir():
                return jsonify({"status": "error", "error": f"不是有效文件夹：{raw}"}), 400
            src_res = src.resolve()
            # 安全闸: 既不能选图库自身/其内部(会自我搬运), 也不能选它的上级(会把整棵树搬走)
            for g, label in guarded:
                if (src_res == g or src_res.is_relative_to(g)
                        or g.is_relative_to(src_res)):
                    return jsonify({"status": "error",
                                    "error": f"不能选择{label}本身、其内部或其上级目录"}), 400
            if src_res.parent == src_res:
                return jsonify({"status": "error", "error": "不能直接选择整个磁盘根目录"}), 400
            src_label = str(src_res)
            MAX_SCAN = 50000
            seen = 0
            for root_dir, _dirs, names in os.walk(src_res):
                for nm in names:
                    seen += 1
                    if seen > MAX_SCAN:
                        return jsonify({"status": "error",
                                        "error": "该目录文件过多（>5 万），请选择更具体的文件夹"}), 400
                    fp = Path(root_dir) / nm
                    if fp.suffix.lower() in IMG_EXT:
                        files.append(fp)

        # 三种去向 ("导入前先判定"):
        #   pixiv  → 标准 P 站作品, 直接入库 img/ (对账登记 + 后台补元数据);
        #   custom → 不是标准 P 站作品, 且用户选了「建自定义作品」→ 入 img/#其他来源#/ 建 ext_ 壳;
        #   reverse→ 不是标准 P 站作品, 用户选「转反搜」(默认) → 入 input/ 收件箱。
        CUSTOM_DIR = _CUSTOM_SRC_DIR
        plan = []
        for fp in files:
            if pixiv_natural_key(fp.name):
                plan.append((fp, img_root, "pixiv"))
            elif make_custom:
                plan.append((fp, img_root / CUSTOM_DIR, "custom"))
            else:
                plan.append((fp, input_root, "reverse"))

        n_pixiv = sum(1 for x in plan if x[2] == "pixiv")
        n_custom = sum(1 for x in plan if x[2] == "custom")
        taken = sum(1 for fp, dr, _k in plan if (dr / fp.name).exists())
        if dry:
            return jsonify({"status": "ok", "dry_run": True, "path": src_label,
                            "total": len(plan), "with_id": n_pixiv,
                            "without_id": len(plan) - n_pixiv, "conflicts": taken})

        imported, redirected, skipped, failed, to_recycle = [], [], [], [], []
        custom_files: "list[Path]" = []      # 待登记的「自定义作品」文件 (复制成功才登记)
        for fp, dest_root, kind in plan:
            target = dest_root / fp.name
            if target.exists():
                skipped.append(fp.name)   # 库内已有同名 → 跳过, 源文件留在原处
                continue
            try:
                dest_root.mkdir(parents=True, exist_ok=True)
                _sh.copy2(str(fp), str(target))   # 先复制入库 (此步失败则源文件原封不动)
            except Exception:
                failed.append(fp.name)
                continue
            if kind == "pixiv":
                imported.append(fp.name)
            elif kind == "custom":
                custom_files.append(target)
            else:
                redirected.append(fp.name)
            to_recycle.append(str(fp))
        # 入库成功后才动源文件: 送回收站 (可恢复), 失败只报告
        recycled, recycle_bad = _recycle_files(to_recycle)
        recycle_failed = [Path(x).name for x in recycle_bad]

        # 「建自定义作品」: 把复制进 #其他来源#/ 的文件登记成 ext_ 壳作品 (标题=文件名)
        custom_created = []
        if custom_files:
            with _db() as db:
                for t in custom_files:
                    rel = str(t.relative_to(img_root)).replace("\\", "/")
                    try:
                        if db.register_foreign_file(rel):
                            custom_created.append(rel)
                    except Exception:
                        pass

        # 落盘即对账 + 作者归位 + 后台补全 (与上传导入同一套收尾)
        rep = _cached_reconcile(force=True)
        with _db() as db:
            _adopt_authors_from_folders(db, img_root)
            db.cleanup_orphans()
        if _organize_loose_images(img_root):
            rep = _cached_reconcile(force=True)
        if rep.new_pages_linked > 0:
            _kick_metadata_fill()
        n = len(imported) + len(redirected) + len(custom_created)
        if n or skipped or failed or custom_created:
            audit_log.record("add", "导入图片(源文件进回收站)",
                             f"从 {src_label} 入库 {len(imported)} 张，"
                             f"转反搜收件箱 {len(redirected)} 张"
                             + (f"，建自定义作品 {len(custom_created)} 个" if custom_created else "")
                             + (f"，源文件已送回收站 {recycled} 个" if recycled else "")
                             + (f"，同名跳过 {len(skipped)} 张(源文件未动)" if skipped else "")
                             + (f"，送回收站失败 {len(recycle_failed)} 个" if recycle_failed else "")
                             + (f"，复制失败 {len(failed)} 张" if failed else ""),
                             n)
        return jsonify({"status": "ok", "imported": imported,
                        "redirected": redirected, "custom_created": custom_created,
                        "skipped": skipped,
                        "failed": failed, "recycled": recycled,
                        "recycle_failed": recycle_failed,
                        "new_pages": rep.new_pages_linked,
                        "conflicts": getattr(rep, "conflicts", []) or []})

    # ---------------- 图片缩略图代理 (绕 pixiv 防盗链) ----------------
    @app.route("/api/img")
    def api_img():
        url = request.args.get("url", "")
        if not url or not url.startswith("http"):
            return "", 400
        try:
            req = Request(url, headers={
                "Referer": "https://www.pixiv.net/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            })
            resp = urlopen(req, timeout=15)
        except Exception:
            return "", 502
        ctype = resp.headers.get("Content-Type", "image/jpeg")

        def _stream():
            # 流式转发: 首字节随读随发 —— 原来 `resp.read` 要先等 pixiv 把整张图下完再一次性
            # 回给浏览器, 「点开大图 → 出画面」的时间 = 整图下载耗时 (
            # 「只是打开低清的都要加载很久」的主因之一)。注意不能在 `with urlopen(...)` 里
            # return 生成器: 那会在 return 时就把连接关掉, 所以这里显式在生成器里 close。
            try:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resp.close()

        # 带缓存头: 同一 URL 24h 内浏览器直接复用, 不重复走 Flask→pixiv CDN 这条链路
        return Response(_stream(), content_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})

    # ---------------- 对 pixiv 账号的写操作 (收藏 / 关注) ----------------
    @app.route("/api/pixiv-actions")
    def api_pixiv_actions_get():
        """读取「下载后自动收藏」设置 + 当前登录态（设置页那张卡片用）。"""
        from utils.config import load_config
        pa = load_config().get("pixiv_actions", {})
        return jsonify({"auto_bookmark": bool(pa.get("auto_bookmark")),
                        "bookmark_private": bool(pa.get("bookmark_private")),
                        "logged_in": _pixiv_http_session() is not None})

    @app.route("/api/pixiv-actions", methods=["POST"])
    def api_pixiv_actions_set():
        """设置自动收藏开关/可见性（写回 config/user.json 持久化）。"""
        body = request.get_json(silent=True) or {}
        upd = {}
        if "auto_bookmark" in body:
            upd["auto_bookmark"] = bool(body.get("auto_bookmark"))
        if "bookmark_private" in body:
            upd["bookmark_private"] = bool(body.get("bookmark_private"))
        if not upd:
            return jsonify({"ok": False, "error": "没有可保存的字段"}), 400
        from utils.config import save_user_config
        try:
            save_user_config({"pixiv_actions": upd})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        vis = "非公开" if upd.get("bookmark_private", False) else "公开"
        if upd.get("auto_bookmark"):
            _reporter().log(f"设置已保存: 下载后自动收藏到 pixiv（{vis}）")
        return jsonify({"ok": True, **upd})

    @app.route("/api/pixiv/follow-status")
    def api_pixiv_follow_status():
        """查是否已关注某位 pixiv 作者（纯读，按钮回显用）。"""
        aid = str(request.args.get("author_id") or "").strip()
        if not aid.isdigit():
            return jsonify({"ok": False, "error": "只有 pixiv 作者（纯数字 ID）能关注"}), 400
        sess = _pixiv_http_session()
        if sess is None:
            return jsonify({"ok": False, "error": "未登录 pixiv（到设置页登录后再试）"}), 400
        try:
            followed = web_follow_state(sess, aid)
        except PixivWebActionError as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        return jsonify({"ok": True, "author_id": aid, "followed": followed})

    @app.route("/api/pixiv/follow", methods=["POST"])
    def api_pixiv_follow():
        """关注 / 取关某位 pixiv 作者。

        body: {"author_id": "...", "follow": true|false, "private"?: bool}
        follow 不传 = 按当前状态取反（前端一个按钮来回切）。
        """
        data = request.get_json(silent=True) or {}
        aid = str(data.get("author_id") or "").strip()
        if not aid.isdigit():
            return jsonify({"ok": False, "error": "只有 pixiv 作者（纯数字 ID）能关注"}), 400
        sess = _pixiv_http_session()
        if sess is None:
            return jsonify({"ok": False, "error": "未登录 pixiv（到设置页登录后再关注）"}), 400
        try:
            want = data.get("follow")
            if want is None:
                want = not web_follow_state(sess, aid)
            want = bool(want)
            if want:
                web_follow(sess, aid, private=bool(data.get("private")))
            else:
                web_unfollow(sess, aid)
        except PixivWebActionError as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        who = aid
        try:
            with _db() as db:
                who = str((db.get_author(aid) or {}).get("name") or aid)
        except Exception:
            pass
        audit_log.record("update", "关注 pixiv 作者",
                         f"{'已关注' if want else '已取关'} {who}（{aid}）", 1)
        _reporter().log(f"{'✅ 已关注' if want else '↩ 已取关'} pixiv 作者 {who}（{aid}）")
        mark_dirty("authors")
        return jsonify({"ok": True, "author_id": aid, "followed": want})

    @app.route("/api/safemode")
    def api_safemode_get():
        """读取安全模式状态 (R18 打码开关)。"""
        from utils.config import load_config
        sm = load_config().get("safemode", {})
        return jsonify({"enabled": bool(sm.get("enabled")), "blur_mode": bool(sm.get("blur_mode", True))})

    @app.route("/api/safemode", methods=["POST"])
    def api_safemode_set():
        """设置安全模式开关 (写回 config/user.json 持久化)。"""
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        from utils.config import save_user_config
        try:
            save_user_config({"safemode": {"enabled": enabled}})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "enabled": enabled})

    @app.route("/api/launcher")
    def api_launcher_get():
        """读取启动器形态开关 (托盘常驻 / 自动开浏览器 / 壁纸小控件 …)。

        键集以 utils/config.py 的 LAUNCHER_DEFAULTS 为准, 前端设置页照着它画开关;
        default.json 里带 _note 之类的非布尔键不返回, 免得前端还要过滤一遍。"""
        from utils.config import load_config, LAUNCHER_DEFAULTS
        ln = load_config().get("launcher", {})
        return jsonify({k: bool(ln.get(k, v)) for k, v in LAUNCHER_DEFAULTS.items()})

    @app.route("/api/launcher", methods=["POST"])
    def api_launcher_set():
        """改启动器形态开关 (写回 config/user.json 持久化), 存盘后**就地热应用**。

        白名单过滤: 只认 LAUNCHER_DEFAULTS 里的键, 防止前端 POST 一个 _note 或别的节
        把 user.json 写脏。未出现的键不动, 交 _deep_merge 与已有 user.json 合并。

        → 除了
        「启动自动开浏览器」(那一刻才有浏览器可开) 以外全部改成存盘即刻生效。热应用钩子由
        main.py 的 set_launcher_apply 交进来 (web 层碰不到托盘/小控件对象); 逐项结果放进
        ``applied`` 回传, 让界面能如实说清哪一项没立刻生效、为什么。"""
        body = request.get_json(silent=True) or {}
        from utils.config import LAUNCHER_DEFAULTS, save_user_config
        upd = {k: bool(body[k]) for k in LAUNCHER_DEFAULTS if k in body}
        if not upd:
            return jsonify({"ok": False, "error": "没有可识别的启动器开关"}), 400
        try:
            save_user_config({"launcher": upd})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        applied = None
        hook = _launcher_apply
        if hook is not None:
            try:
                applied = hook(dict(upd))
            except Exception as e:
                applied = {"error": str(e)}
        mark_dirty("settings")
        return jsonify({"ok": True, **upd, "applied": applied})

    # ---------------- 📁 路径设置 (所有路径都要有设置入口) ----------------
    @app.route("/api/paths")
    def api_paths_get():
        """读路径设置: 可改的两个重定向 + 其余位置的只读落点 (给设置页「📁 路径」卡片画)。"""
        return jsonify(_paths_snapshot())

    @app.route("/api/paths", methods=["POST"])
    def api_paths_set():
        """改数据目录 / 临时目录重定向 (写回 config/user.json)。

        空串 = 清除, 回到默认落点。非空必须是**绝对路径且能建出来、能写** —— 校验口径见
        ``_norm_path_override`` / ``_save_path_overrides``。改完**要重启**才生效 (理由见
        ``_paths_snapshot`` 上方的模块注释): 响应里带 ``restart_required`` 让界面如实提示。"""
        body = request.get_json(silent=True) or {}
        payload, err = _save_path_overrides(body)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        mark_dirty("settings")
        return jsonify({"ok": True, "restart_required": True, **payload})

    @app.route("/api/paths/pick", methods=["POST"])
    def api_paths_pick():
        """弹**系统「选择文件夹」对话框**挑数据目录 / 临时目录, 选中即存; 取消什么都不改。

        ``which`` = ``img``(图片目录) / ``data``(数据目录) / ``temp``(临时目录)。浏览器拿不到用户选中目录的绝对
        路径, 所以「选目录」只能由服务端弹原生对话框 (复用 utils/folder_dialog.pick_folder,
        与壁纸夹那处同一套实现)。对话框阻塞当前请求线程到用户选完, dev server 是 threaded,
        不影响别的请求。"""
        body = request.get_json(silent=True) or {}
        which = str(body.get("which") or "data")
        _PICK = {"img": ("img_subdir", "选择图片目录（原始图片都存这里，建议放非系统盘）", "img"),
                 "data": ("data_root_override", "选择数据目录（数据库·日志·缩略图）", "data_root"),
                 "temp": ("temp_root_override", "选择临时目录", "temp_root")}
        if which not in _PICK:
            return jsonify({"ok": False, "error": "未知的路径项"}), 400
        key, title, cur_key = _PICK[which]
        cur = _paths_snapshot()["effective"][cur_key]
        from utils.folder_dialog import pick_folder
        try:
            picked = pick_folder(title, initial=str(cur) if cur else None)
        except Exception as e:
            # 这条路全是 ctypes/COM: 任一环节失败都必须是 JSON, 不能让前端把 500 当"取消"
            return jsonify({"ok": False, "error": f"弹不出系统对话框: {e}"}), 500
        if not picked:
            return jsonify({"ok": False, "cancelled": True, **_paths_snapshot()})
        payload, err = _save_path_overrides({key: picked})
        if err:
            return jsonify({"ok": False, "error": err}), 400
        mark_dirty("settings")
        return jsonify({"ok": True, "saved": True, "restart_required": True, **payload})

    @app.route("/api/paths/temp/clean", methods=["POST"])
    def api_paths_temp_clean():
        """一键清空临时目录 (浏览器 profile / 上传缓冲 / 各种临时文件)。

        临时目录本来就是「随时可整删」的, 手动清与启动时那次自动清理同一套逻辑
        (``aggressive=True`` 表示连当前进程正在用的也删, 下次用时重建)。"""
        from utils import paths as _p
        try:
            st = _p._cleanup_temp(max_age_h=0, aggressive=True)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        _reporter().log(f"🧹 临时目录已清理: 删除 {st.get('removed', 0)} 项, 释放 {st.get('freed_mb', 0)} MB")
        return jsonify({"ok": True, "removed": st.get("removed", 0),
                        "freed_mb": st.get("freed_mb", 0), "sizes": _paths_snapshot()["sizes"]})

    # ---------------- ✨ 壁纸文案 AI ----------------
    @app.route("/api/ai")
    def api_ai_get():
        """读壁纸文案 AI 的设置 (给设置页画表单)。

        **不回密钥原文** —— 只回「填过没有 + 后四位」。这是本机自用的界面, 但把可用的密钥
        明文塞进任何响应体都是坏习惯 (页面截图、日志、别人借用这台机器都能顺手看到)。
        """
        from core.ai_text import ai_section
        sec = ai_section()
        key = str(sec.get("api_key") or "")
        return jsonify({"enabled": bool(sec.get("enabled")),
                        "base_url": str(sec.get("base_url") or ""),
                        "model": str(sec.get("model") or ""),
                        "vision": bool(sec.get("vision")),
                        "timeout": int(sec.get("timeout") or 25),
                        "has_key": bool(key),
                        "key_tail": key[-4:] if len(key) >= 8 else ""})

    def _ai_form_upd(body: dict) -> dict:
        """设置页这次填的地址/模型/密钥 → 待写的配置片段。

        ``api_key`` 只在**这次真的传了非空值**时才写 —— 密钥框不回显, 只改模型名的一次提交
        不该把它抹成空。
        """
        upd: dict = {}
        for k in ("base_url", "model"):
            if body.get(k):
                upd[k] = str(body[k]).strip()
        if str(body.get("api_key") or "").strip():
            upd["api_key"] = str(body["api_key"]).strip()
        return upd

    def _ai_save_form(body: dict) -> "tuple | None":
        """存下这次填的配置。返回 ``(响应, 状态码)`` 表示失败了, ``None`` 表示成功。"""
        from utils.config import save_user_config
        upd = _ai_form_upd(body or {})
        if not upd:
            return None
        try:
            save_user_config({"ai_text": upd})
            mark_dirty("settings")
        except Exception as e:
            return jsonify({"ok": False, "error": f"配置保存失败: {e}"}), 500
        return None

    @app.route("/api/ai/test", methods=["POST"])
    def api_ai_test():
        """「🔌 测试连接」: 存下这次填的配置, 再用最便宜的一次请求验一遍。

        接口报错时加回来的 —— 只有一颗按钮
        就没有任何排障入口, 用户只能盲猜是哪一项填错。**不发图、不要求有当前壁纸**,
        地址 / 密钥 / 模型名三项错哪一项, 由接口返回的原话来说明。
        """
        from core.ai_text import AIError, test_connection
        body = request.get_json(silent=True) or {}
        failed = _ai_save_form(body)
        if failed:
            return failed
        try:
            out = test_connection(form=body, log=lambda m: _reporter().log(m))
        except AIError as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502
        return jsonify({"ok": True, "model": out.get("model", ""), "ms": out.get("ms", 0)})

    @app.route("/api/ai/run", methods=["POST"])
    def api_ai_run():
        """「⬆ 上传当前壁纸」: 存下这次填的地址/模型/密钥, 给**当前这张壁纸**生成一次文案。

        设置页只留这一颗按钮 —— 点一次 = 保存配置 + 上传当前壁纸 +
        当场拿回「标题 + 氛围句」(同时写进缓存, 桌面小控件随即显示)。换壁纸**不自动**再调
        接口, 所以这条是**同步等待**的 (用户点了就是在等结果)。
        """
        from core.ai_text import AIError, generate_now
        from utils.config import save_user_config
        body = request.get_json(silent=True) or {}
        failed = _ai_save_form(body)
        if failed:
            return failed
        info = trace_current_wallpaper(
            deep=False,
            wall_path=_wallpaper_file(),
            db_factory=_db,
            img_root=Path(paths.user_data(_config["paths"]["img_subdir"])).resolve(),
            img_url_fn=_local_img_url,
            orb_fn=_wall_orb_candidates,
        )
        try:
            out = generate_now(info, form=body, log=lambda m: _reporter().log(m))
        except AIError as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502
        # 生成成功才把「已启用」落上 —— 之后 texts_for 读缓存才会认这两行, 设置页也能显示
        # 「已配置」而不是一直「还缺 API 密钥」。
        try:
            save_user_config({"ai_text": {"enabled": True, "vision": True}})
            mark_dirty("settings")
        except Exception:
            pass
        # 文案刚落缓存, 别让小控件干等下一轮 8 秒轮询 —— 立刻捅它一下。
        _notify_widget()
        return jsonify({"ok": True, "title": out.get("title", ""), "mood": out.get("mood", "")})

    # 启动收尾: ① 把上次停在 running 的任务记录标成"异常中断"(关页面后进程被守护线程
    # 直接退出的痕迹), 前端启动时会提示是否继续; ② 把上次的体检报告读回内存。
    try:
        _journal_mark_interrupted()
    except Exception:
        pass
    _ck_load_report_from_disk()

    return app


def _close_all_browsers() -> None:
    """进程退出前统一收尾, 杀掉共享 Selenium / 手动登录可见浏览器,
    避免按 Ctrl+C / 关窗口后残留 msedge / msedgedriver 占用端口与进程
    (对应「进程不能完全刷新」问题)。"""
    try:
        _reset_search_client()
    except Exception:
        pass
    try:
        with _manual_lock:
            d = _manual_driver
            if d is not None:
                try:
                    d.quit()
                except Exception:
                    pass
                _manual_driver = None
    except Exception:
        pass


# atexit 注册在 create_app 时执行一次: dev server (threaded, use_reloader=False)
# 进程正常退出(Ctrl+C / 关窗口)会触发, 清理残留浏览器进程。
atexit.register(_close_all_browsers)
