# utils/sec.py
"""
凭据安全存储(Windows DPAPI) —— 对齐浏览器"本机绑定+无感"的安全模式。

对外核心三件套:
    1) save_encrypted_cookies(cookies_list, path)  -> 加密存盘+锁ACL+删旧明文
    2) load_cookies_smart(path, legacy_path=None)   -> 自动识别格式(.bin/.txt明文)解密
                                                        读到明文时自动升级成.bin + 安全删除旧文件
    3) upgrade_if_needed = True                     -> 默认开启向后兼容

依赖: 仅 Windows ctypes (CryptProtectData/UnprotectData)，无需安装任何库。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Iterable


# ================================================================
# 1) DPAPI 底层封装（CryptProtectData / UnprotectData）
# ================================================================
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


_CryptProtectData = ctypes.windll.crypt32.CryptProtectData
_CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),
    wintypes.LPCWSTR,
    ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DATA_BLOB),
]
_CryptProtectData.restype = wintypes.BOOL

_CryptUnprotectData = ctypes.windll.crypt32.CryptUnprotectData
_CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),
    ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DATA_BLOB),
]
_CryptUnprotectData.restype = wintypes.BOOL

# CRYPTPROTECT_UI_FORBIDDEN: 禁止任何 UI 弹框（后台/服务场景必须）
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_LOCALFREE = ctypes.windll.kernel32.LocalFree


def dpapi_protect(data: bytes, description: str = "pixiv-toolkit-cookies") -> bytes:
    """用 DPAPI 绑定当前 Windows 用户+机器加密，返回密文字节串。非 Windows 平台抛 RuntimeError。"""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 凭据加密仅支持 Windows 平台")
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes/bytearray")
    blob_in = _DATA_BLOB()
    blob_in.cbData = len(data)
    blob_in.pbData = ctypes.cast(
        (ctypes.c_byte * len(data)).from_buffer_copy(data),
        ctypes.POINTER(ctypes.c_byte))
    blob_out = _DATA_BLOB()
    ok = _CryptProtectData(
        ctypes.byref(blob_in),
        description,
        None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok or blob_out.cbData == 0 or not blob_out.pbData:
        raise ctypes.WinError(ctypes.get_last_error())
    # 注意: ctypes.POINTER(c_byte)切片是有符号字节(-128~127),传给 bytes() 会报错。
    # 使用 ctypes.string_at 直接取原始字节串(0~255)。
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    # 释放系统分配的密文内存
    if blob_out.pbData:
        _LOCALFREE(blob_out.pbData)
    return result


def dpapi_unprotect(blob: bytes) -> bytes:
    """DPAPI 解密。拷到别的机器/别的用户账号 → 返回 None。"""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 凭据解密仅支持 Windows 平台")
    if not blob:
        raise ValueError("empty blob")
    blob_in = _DATA_BLOB()
    blob_in.cbData = len(blob)
    blob_in.pbData = ctypes.cast(
        (ctypes.c_byte * len(blob)).from_buffer_copy(blob),
        ctypes.POINTER(ctypes.c_byte))
    blob_out = _DATA_BLOB()
    descr_ptr = wintypes.LPWSTR()
    ok = _CryptUnprotectData(
        ctypes.byref(blob_in),
        ctypes.byref(descr_ptr),
        None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    try:
        if descr_ptr:
            _LOCALFREE(descr_ptr)
    except Exception:
        pass
    if not ok or blob_out.cbData == 0 or not blob_out.pbData:
        # 常见原因:拷到其他机器 / 换用户 / 重装系统
        return None
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    if blob_out.pbData:
        _LOCALFREE(blob_out.pbData)
    return result


# ================================================================
# 2) cookies 高层 API（自动处理 JSON ↔ 字节 ↔ DPAPI ↔ base64 文件）
# ================================================================
_FILE_MAGIC = "PTC1"  # "Pixiv-Toolkit-Cookies v1"，用于标识格式版本


def save_encrypted_cookies(cookies: list, path: str | os.PathLike,
                           lock_acl: bool = True) -> Path:
    """保存 cookies 列表为 DPAPI 加密文件。返回实际写入路径。

    文件格式 (纯文本可传输,无二进制风险):
        第一行: 魔术串 PTC1
        第二行: base64(DPAPI(json_bytes))
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    json_bytes = json.dumps(cookies, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8")
    ct = dpapi_protect(json_bytes, description="pixiv-toolkit-cookies-v1")
    content = f"{_FILE_MAGIC}\n{base64.b64encode(ct).decode('ascii')}\n"
    # 先写临时文件 → os.replace 原子替换,避免中途掉电写坏
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, path)
    # 黄金版:锁 ACL 仅当前用户
    if lock_acl and sys.platform == "win32":
        _lock_acl_current_user(path)
    return path


def load_encrypted_cookies(path: str | os.PathLike) -> list | None:
    """解析加密 cookies。失败/格式不对/绑定非本机均返回 None(调用方再 fallback 到明文)。"""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        if not lines or lines[0].strip() != _FILE_MAGIC:
            return None  # 格式非 PTC1,让调用方 fallback 明文
        if len(lines) < 2:
            return None
        b64 = lines[1].strip()
        ct = base64.b64decode(b64, validate=True)
        pt = dpapi_unprotect(ct)
        if pt is None:
            return None  # 非本机绑定
        cookies = json.loads(pt.decode("utf-8"))
        if isinstance(cookies, list):
            return cookies
        return None
    except Exception:
        return None


def load_plain_cookies(path: str | os.PathLike) -> list | None:
    """读旧版明文 JSON cookies(向后兼容)。格式对但不是合法 list 也返回 None。"""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def load_cookies_smart(path: str | os.PathLike,
                       legacy_plain_path: str | os.PathLike | None = None,
                       *, upgrade: bool = True) -> list | None:
    """智能加载:优先加密版;不存在或解密失败 → 回退明文。

    upgrade=True 且明文回退成功:自动写加密版并安全删除明文(用户无感升级)。
    """
    path = Path(path)
    data = load_encrypted_cookies(path)
    if data:
        return data

    leg = Path(legacy_plain_path) if legacy_plain_path else path.with_suffix(".txt")
    plain = load_plain_cookies(leg)
    if plain and upgrade and sys.platform == "win32":
        try:
            save_encrypted_cookies(plain, path, lock_acl=True)
            secure_delete(leg)  # 升级成功就抹掉明文
        except Exception:
            # 升级失败不影响本次读取,只是继续用明文(下次启动再试)
            pass
    return plain


# ================================================================
# 3) 辅助: 安全删除(防简单恢复工具读明文) + ACL 锁
# ================================================================
def secure_delete(path: str | os.PathLike, passes: int = 3) -> bool:
    """覆盖写入再删除,避免误删后 Recuva/FTK 直接读回明文。SSD 效果有限,但对 HDD 有用。"""
    path = Path(path)
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
        with open(path, "r+b") as f:
            for i in range(passes):
                f.seek(0)
                if i == passes - 1:
                    payload = secrets.token_bytes(size)
                else:
                    pattern = bytes([0x00 if i % 2 == 0 else 0xFF]) * 1024 * 1024
                    remaining = size
                    while remaining > 0:
                        chunk = pattern[: min(len(pattern), remaining)]
                        f.write(chunk)
                        remaining -= len(chunk)
                f.flush()
                os.fsync(f.fileno())
        os.remove(path)
        return True
    except Exception:
        try:
            os.remove(path)  # 覆盖失败但至少删掉
        except Exception:
            pass
        return False


def _lock_acl_current_user(path: str | os.PathLike) -> bool:
    """为 cookies 加一层 ACL 防御同机器的其他普通用户读取。

    设计取舍(重要):
      · 不移除父级继承 → 不挡 SYSTEM / Administrators(他们本就可以通过备份权限随意读)
      · 给当前用户显式 FULL → 确保沙箱 / Temp 特殊 ACL 目录下自己也能正常读写
      · 拒绝 Guests / 匿名 Everyone 读、写、删 → 这才是实际目标(家里共用电脑/来宾账户)
    """
    path = Path(path)
    if not path.is_file() or sys.platform != "win32":
        return False
    try:
        user = os.environ.get("USERNAME")
        if not user:
            return False
        # 重要: Windows 的"拒绝 ACE"优先级高于任何允许 ACE,
        # 因此绝对不能对 Everyone(*S-1-1-0) deny —— 当前交互式登录用户也在 Everyone 里,
        # 一加 deny 就会把自己也锁死。只对 Guests(GU) 和匿名登录这两个"陌生人"SID deny,
        # 当前登录用户不可能属于这两个 SID。
        #
        # 顺序: 先显式给当前用户 FULL(确保 Temp/沙箱这类特殊 ACL 目录也可读)
        base = ["icacls", str(path), "/grant:r", f"{user}:(F)", "/C", "/Q"]
        r1 = subprocess.run(base, capture_output=True, text=True, timeout=15)
        # 再对"陌生人"SID deny 读/写/删。SID 列表参考:
        #   GU       = Guests 组
        #   S-1-5-7  = ANONYMOUS LOGON (匿名登录)
        for deny_target in ("GU", "*S-1-5-7"):
            try:
                subprocess.run(
                    ["icacls", str(path), "/deny",
                     f"{deny_target}:(RX,WDAC,WO,DE)", "/C", "/Q"],
                    capture_output=True, text=True, timeout=10)
            except Exception:
                pass
        # 最终自检:确保我们自己仍然可读
        try:
            with open(path, "rb") as f:
                f.read(16)
            readable_self = True
        except Exception:
            readable_self = False
        return r1.returncode == 0 and readable_self
    except Exception:
        return False


__all__ = [
    "save_encrypted_cookies",
    "load_cookies_smart",
    "load_encrypted_cookies",
    "load_plain_cookies",
    "dpapi_protect",
    "dpapi_unprotect",
    "secure_delete",
]
