# web/event_bus.py
"""
SSE 事件总线 + WebReporter。

把 core 的 Reporter 协议事件实时广播到所有浏览器连接(EventSource):
- 后台线程(下载/反搜)通过 reporter 上报 → hub.publish → SSE → 前端实时刷新
- 取代 Tkinter 版「queue + root.after 轮询」桥接方案, Web 下浏览器自己订阅即可
"""
import datetime
import queue
import threading

from utils.reporter import Reporter


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


class EventHub:
    """线程安全广播器: 每个 SSE 连接持一个订阅队列。"""

    def __init__(self) -> None:
        self._subs: set["queue.Queue"] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            self._subs.discard(q)

    def client_count(self) -> int:
        """当前活跃 SSE 连接数 (≈已打开的浏览器标签页数)。"""
        with self._lock:
            return len(self._subs)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 满则丢最旧一条, 保证新事件能进来(进度不中断)
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except queue.Empty:
                    pass
            except Exception:
                pass


class WebReporter(Reporter):
    """把 core 事件转成 SSE 事件广播; 同时支持取消(共享 cancel_event)。"""

    def __init__(self, hub: EventHub, cancel_event: threading.Event):
        self.hub = hub
        self.cancel_event = cancel_event

    def log(self, msg: str) -> None:
        self.hub.publish({"type": "log", "level": "info", "msg": f"[{_ts()}] {msg}"})

    def warn(self, msg: str) -> None:
        self.hub.publish({"type": "log", "level": "warn", "msg": f"[{_ts()}] ⚠ {msg}"})

    def error(self, msg: str) -> None:
        self.hub.publish({"type": "log", "level": "error", "msg": f"[{_ts()}] ✖ {msg}"})

    def progress(self, cur: int, total: int, status: str = "") -> None:
        self.hub.publish({"type": "progress", "cur": int(cur), "total": int(total),
                          "status": status})

    def item_progress(self, item_id: int, cur: int, total: int, status: str = "") -> None:
        self.hub.publish({"type": "item_progress", "item_id": int(item_id),
                          "cur": int(cur), "total": int(total), "status": status})

    def put(self, kind: str, payload: dict) -> None:
        self.hub.publish({"type": kind, **payload})

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()
