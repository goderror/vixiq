# web/__init__.py
"""Web 版界面包(替代 Tkinter gui/)。

- event_bus.py : SSE 事件总线 + WebReporter(core 进度 → 浏览器)
- server.py    : Flask 后端, 复用 core/db 全部能力
- static/      : 单页前端(index.html / style.css / app.js)
"""
