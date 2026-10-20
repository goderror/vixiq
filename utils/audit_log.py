# utils/audit_log.py
"""
操作审计日志: 记录图库/作者的「增 / 改 / 删」操作, 供设置页「📋 操作记录」查看。

与实时日志 (utils/logger + SSE) 的区别:
  - 实时日志 = 后端状态广播 (下载进度/浏览器启动/联网补全), 面向排障, 看过即散;
  - 操作审计 = 「谁在什么时候改了什么」, 需要结构化落盘、可按类型筛选、可清空。

落盘: <data_root>/logs/operations.jsonl (一行一条 JSON), 超过 _MAX_LINES 只留最近若干条。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from utils import paths

_LOCK = threading.Lock()
_MAX_LINES = 3000      # 单文件最多保留条数, 超出丢弃最旧 (防止无上限增长)

_KINDS = ("add", "update", "delete")
_KIND_LABEL = {"add": "新增", "update": "修改", "delete": "删除"}


def kind_label(kind: str) -> str:
    return _KIND_LABEL.get(kind, kind or "")


def _log_path() -> Path:
    return paths.user_data("logs/operations.jsonl")


def record(kind: str, action: str, detail: str = "", count: int = 1,
           ids: "list[str] | None" = None) -> dict:
    """记一条操作。kind ∈ add/update/delete; action=动作名; detail=人读描述;
    ids=本次涉及的作品 ID (前端据此显示可点击的小灰字 ID, 跳图库定位)。"""
    if kind not in _KINDS:
        kind = "update"
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 1
    row = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "kind_label": kind_label(kind),
        "action": str(action or ""),
        "detail": str(detail or ""),
        "count": n,
    }
    if ids:
        row["ids"] = [str(x) for x in ids if str(x)][:20]
    line = json.dumps(row, ensure_ascii=False)
    with _LOCK:
        try:
            p = _log_path()
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            _trim(p)
        except Exception:
            pass
    return row


def _trim(p: Path) -> None:
    """超过上限只保留最近 _MAX_LINES 条。"""
    try:
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _MAX_LINES:
            return
        p.write_text("".join(lines[-_MAX_LINES:]), encoding="utf-8")
    except Exception:
        pass


def read(limit: int = 200, kind: str = "") -> dict:
    """读最近操作记录 (新→旧)。返回 {items, total, counts}。"""
    rows: list[dict] = []
    try:
        p = _log_path()
        if p.is_file():
            with p.open("r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        continue
    except Exception:
        rows = []
    counts = {"add": 0, "update": 0, "delete": 0}
    for r in rows:
        k = r.get("kind")
        if k in counts:
            counts[k] += 1
    total = len(rows)
    if kind in _KINDS:
        rows = [r for r in rows if r.get("kind") == kind]
    try:
        lim = max(1, int(limit))
    except (TypeError, ValueError):
        lim = 200
    rows = list(reversed(rows))[:lim]
    return {"items": rows, "total": total, "counts": counts}


def clear() -> int:
    """清空操作记录, 返回清掉的条数。"""
    with _LOCK:
        try:
            p = _log_path()
            if not p.is_file():
                return 0
            n = sum(1 for ln in p.open("r", encoding="utf-8") if ln.strip())
            p.unlink(missing_ok=True)
            return n
        except Exception:
            return 0
