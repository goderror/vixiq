# utils/net.py
"""统一的 urllib 出口: 自动跟随 Windows 系统代理。

背景 (2026-11 实测): Selenium 启动的 Edge 走系统代理, 搜索页能正常打开 pixiv;
但后端 urllib 默认只读环境变量 HTTP_PROXY, **不读** Windows「Internet 选项」
里的系统代理 —— 于是搜索结果能出来、缩略图/元数据/下载却全部超时。
直连 i.pximg.net 超时、走系统代理 200, 就是这条链路。

只给访问外网的调用方用; 访问本机 127.0.0.1 的代码 (托盘/小控件/健康检查)
继续用 urllib.request.urlopen, 免得被代理绕一圈。
"""
from __future__ import annotations

import urllib.request

# False=还没探测过; None=本机没启用系统代理; 字符串=代理地址
_cached_proxy: "str | None | bool" = False


def system_proxy() -> "str | None":
    """读 HKCU Internet Settings 的系统代理; 未启用/读不到返回 None。"""
    global _cached_proxy
    if _cached_proxy is not False:
        return _cached_proxy  # type: ignore[return-value]
    proxy = None
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        s = str(server or "").strip()
        if enable and s:
            # "http=127.0.0.1:7890;https=127.0.0.1:7890" → 取 https 或 http 那项
            if "=" in s:
                parts = dict(p.split("=", 1) for p in s.split(";") if "=" in p)
                s = parts.get("https") or parts.get("http") or ""
            if s:
                proxy = s if "://" in s else "http://" + s
    except Exception:
        proxy = None
    _cached_proxy = proxy
    return proxy


class _ProxyHandler(urllib.request.ProxyHandler):
    """系统代理 handler: 本机地址 (127.0.0.1/localhost) 一律直连。

    测试桩/本机服务如果被塞进代理, 会拿到 404 或直接超时 (冒烟 T32 实测)。
    """

    def proxy_open(self, req, proxy, type):
        host = (req.host or "").split(":")[0].strip("[]").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            return None
        return super().proxy_open(req, proxy, type)


def build_opener(headers: "dict | None" = None) -> urllib.request.OpenerDirector:
    """带系统代理的 opener; headers 给了就作为默认请求头 (如 Referer)。"""
    proxy = system_proxy()
    handlers = []
    if proxy:
        handlers.append(_ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    if headers:
        opener.addheaders = list(headers.items())
    return opener


def urlopen(req, timeout: float = 15):
    return build_opener().open(req, timeout=timeout)
