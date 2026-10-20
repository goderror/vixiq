# utils/reporter.py
"""
Reporter 协议:取代旧 Tee(stdout 劫持方案)。

为什么废弃 Tee:
- 旧 utils.py 的 Tee.fileno() 返回 sys.__stdout__.fileno(),
  --windowed 打包后无控制台,sys.__stdout__ 为 None,fileno() 直接崩;
  tqdm 探测终端时调 getattr(file,'fileno',None) 就会炸。
- 子线程不能直接写 Tkinter 控件,需要 queue + root.after 桥接。

改用 Reporter:
- core 通过 reporter.log/progress/warn/error 上报,与 UI 解耦
- PrintReporter : 开发/CLI 直接 print
- FileReporter   : 落 data/logs/
- QueueReporter  : GUI 用,把事件塞 queue,主线程 after 取出再写控件
- MultiReporter   : 组合多个,一次上报多处

tqdm 在 GUI 模式不再用,改 reporter.progress(cur, total, status)。

P4b · 购物车 Item 级进度:
  reporter.item_progress(item_id, cur, total, status)
  按购物车 DB 行的 id (download_queue_items.id) 单独上报每一条清单项的进度,
  CartDrawer Treeview 的对应行独立更新 (█░ 字符画进度条 + 百分比)。
  cur/total 语义:
    - artwork:        cur 已下页数 / total 总页数
    - author_all:     cur 已处理作品数 / total 总作品数
    - reverse_sourced:cur=0 未开始, cur=1 完成 (单张补齐)
"""
import datetime
import queue
import threading
from abc import ABC, abstractmethod
from pathlib import Path


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


class Reporter(ABC):
    """上报接口协议。所有 core 方法接收 reporter 参数。"""

    @abstractmethod
    def log(self, msg: str) -> None: ...

    @abstractmethod
    def progress(self, cur: int, total: int, status: str = "") -> None: ...

    @abstractmethod
    def warn(self, msg: str) -> None: ...

    @abstractmethod
    def error(self, msg: str) -> None: ...

    @abstractmethod
    def is_cancelled(self) -> bool: ...

    def item_progress(self, item_id: int, cur: int, total: int,
                      status: str = "") -> None:
        """P4b · 购物车 Item 级进度。默认把它当普通 progress 忽略 (PrintReporter 等)。

        item_id 是 download_queue_items 表的自增 id, GUI 用它直接定位 Treeview 行。
        """
        pass

    def put(self, kind: str, payload: dict) -> None:
        """直通事件: 给 app 层塞自定义 (kind, payload) GUI 事件。

        Reporter 基类默认 no-op。只有 QueueReporter / 组合给 GUI 用的 Reporter 会真
        把 (kind, payload) 放进主线程 queue, 触发 _drain_queue 对应分支。
        用途:
          - P4a · SearchClient 子线程查询完 → `reporter.put('search_done', {...})` 回主线程渲染
          - P4a E2E 冒烟: monkey patch 替代 _do_search_sync, 不跑 Selenium 直接塞 stub 结果
          - P4a+ · 自动登录线程 → `reporter.put('login_status', {...})` 回主线程打 chip
        """
        pass


class PrintReporter(Reporter):
    """开发/CLI 用,直接 print 到控制台。不可取消。"""

    def log(self, msg: str) -> None:
        print(f"[{_ts()}] {msg}")

    def progress(self, cur: int, total: int, status: str = "") -> None:
        pct = f"{cur}/{total}" if total else str(cur)
        print(f"[{_ts()}] ▶ {pct} {status}".rstrip(), flush=True)

    def warn(self, msg: str) -> None:
        print(f"[{_ts()}] ⚠ {msg}")

    def error(self, msg: str) -> None:
        print(f"[{_ts()}] ✖ {msg}")

    def is_cancelled(self) -> bool:
        return False

    def item_progress(self, item_id, cur, total, status=""):
        """CLI 版直接打印 item 级进度 (加 [item#id] 前缀即可)。"""
        pct = f"{cur}/{total}" if total else str(cur)
        print(f"[{_ts()}] ▶ item#{item_id} {pct} {status}".rstrip(), flush=True)


class FileReporter(Reporter):
    """把日志落 data/logs/。转发给一个内部 reporter 做控制台/GUI 输出。"""

    def __init__(self, log_file: Path, inner: "Reporter | None" = None):
        self.log_file = log_file
        self.inner = inner
        self._lock = threading.Lock()

    def _write(self, level: str, msg: str) -> None:
        with self._lock:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(f"[{_ts()}] [{level}] {msg}\n")
            except Exception:
                pass  # 日志失败不能影响主流程

    def log(self, msg: str) -> None:
        self._write("INFO", msg)
        if self.inner:
            self.inner.log(msg)

    def progress(self, cur, total, status="") -> None:
        if self.inner:
            self.inner.progress(cur, total, status)

    def item_progress(self, item_id, cur, total, status="") -> None:
        if self.inner:
            self.inner.item_progress(item_id, cur, total, status)

    def put(self, kind, payload) -> None:
        if self.inner:
            self.inner.put(kind, payload)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)
        if self.inner:
            self.inner.warn(msg)

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)
        if self.inner:
            self.inner.error(msg)

    def is_cancelled(self) -> bool:
        if self.inner:
            return self.inner.is_cancelled()
        return False


class MultiReporter(Reporter):
    """组合多个 reporter,一次上报多处。单个出错不影响其他。"""

    def __init__(self, *reporters: Reporter):
        self.reporters = reporters

    def _dispatch(self, method: str, *args, **kwargs):
        for r in self.reporters:
            try:
                getattr(r, method)(*args, **kwargs)
            except Exception:
                pass

    def log(self, msg: str) -> None:
        self._dispatch("log", msg)

    def progress(self, cur, total, status="") -> None:
        self._dispatch("progress", cur, total, status)

    def item_progress(self, item_id, cur, total, status="") -> None:
        self._dispatch("item_progress", item_id, cur, total, status)

    def put(self, kind, payload):
        self._dispatch("put", kind, payload)

    def warn(self, msg: str) -> None:
        self._dispatch("warn", msg)

    def error(self, msg: str) -> None:
        self._dispatch("error", msg)

    def is_cancelled(self) -> bool:
        return any(r.is_cancelled() for r in self.reporters)


class QueueReporter(Reporter):
    """把 core 的事件序列化进 queue，供主线程/后台循环取出渲染 (线程安全)。
    原位于旧 Tk 界面层 gui/runner.py，P7 起 gui/ 移除后并入 reporter 体系
    (web 冒烟事件链 / 下载进度仍复用)。"""

    def __init__(self, q: "queue.Queue", cancel_event: threading.Event):
        self.q = q
        self.cancel_event = cancel_event

    def log(self, msg: str) -> None:
        self.q.put(("log", {"msg": msg, "level": "info"}))

    def warn(self, msg: str) -> None:
        self.q.put(("log", {"msg": msg, "level": "warn"}))

    def error(self, msg: str) -> None:
        self.q.put(("log", {"msg": msg, "level": "error"}))

    def progress(self, cur: int, total: int, status: str = "") -> None:
        self.q.put(("progress", {"cur": cur, "total": total, "status": status}))

    def item_progress(self, item_id: int, cur: int, total: int,
                      status: str = "") -> None:
        """购物车 Item 级进度事件。"""
        self.q.put(("item_progress", {
            "item_id": int(item_id),
            "cur": int(cur), "total": int(total),
            "status": status,
        }))

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def put(self, kind: str, payload: dict) -> None:
        """自定义事件直通 (search_done/login_status 等应用层事件)。"""
        self.q.put((kind, payload))
