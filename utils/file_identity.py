"""文件对象身份: 按实际打开的对象核验/删除, 以及不覆盖提交。

用途 (重下修复回滚 / 跨格式撤销):
  - 路径随时可能被另一操作换成同名新文件; 只在路径上做「检查后再删/替换」会误伤。
  - Windows 下用 CreateFileW 拿句柄, GetFileInformationByHandle 比对
    (卷序列号 + 文件索引 + 大小 + mtime), 再用 SetFileInformationByHandle 删除
    **句柄指向的对象**; 即使路径在打开后被 os.replace 换掉, 也不会误删新文件。
  - 恢复提交用 rename_no_overwrite: 目标已存在时直接失败, 绝不覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

_WIN_API = None


def win_api() -> dict:
    """惰性准备 CreateFileW / GetFileInformationByHandle / SetFileInformationByHandle。"""
    global _WIN_API
    if _WIN_API is not None:
        return _WIN_API
    import ctypes
    from ctypes import wintypes

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    info = k32.GetFileInformationByHandle
    info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    info.restype = wintypes.BOOL
    dispose = k32.SetFileInformationByHandle
    dispose.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                        wintypes.DWORD]
    dispose.restype = wintypes.BOOL
    close = k32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    _WIN_API = {
        "ctypes": ctypes, "wintypes": wintypes,
        "BY_HANDLE_FILE_INFORMATION": _BY_HANDLE_FILE_INFORMATION,
        "FILE_DISPOSITION_INFO_STRUCT": _FILE_DISPOSITION_INFO,
        "CreateFileW": create, "GetFileInformationByHandle": info,
        "SetFileInformationByHandle": dispose, "CloseHandle": close,
        "GENERIC_READ": 0x80000000, "GENERIC_WRITE": 0x40000000,
        "DELETE": 0x00010000,
        "FILE_SHARE_READ": 0x1, "FILE_SHARE_WRITE": 0x2,
        "FILE_SHARE_DELETE": 0x4, "OPEN_EXISTING": 3,
        "FILE_ATTRIBUTE_NORMAL": 0x80, "FileDispositionInfo": 4,
        "INVALID_HANDLE_VALUE": ctypes.c_void_p(-1).value,
    }
    return _WIN_API


def file_identity(path: "str | Path") -> "dict | None":
    """当前路径指向的文件对象身份; 读不到返回 None。"""
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    return {"size": int(st.st_size),
            "mtime_ns": int(getattr(st, "st_mtime_ns", 0) or 0),
            "ino": int(getattr(st, "st_ino", 0) or 0),
            "dev": int(getattr(st, "st_dev", 0) or 0)}


def same_identity(a: "dict | None", b: "dict | None") -> bool:
    if not a or not b:
        return False
    return (int(a.get("size", -1)) == int(b.get("size", -1))
            and int(a.get("mtime_ns", -1)) == int(b.get("mtime_ns", -1))
            and int(a.get("ino", -1)) == int(b.get("ino", -1))
            and (int(a.get("dev", -1)) & 0xFFFFFFFF)
            == (int(b.get("dev", -1)) & 0xFFFFFFFF))


def unlink_if_identity(path: "str | Path", expected: "dict | None") -> "tuple[str, str]":
    """按文件对象身份移除文件。

    返回 (removed|kept|replaced, reason):
      - removed: 已删除该对象, 或路径已不存在;
      - kept: 被占用/权限不足/无法核验/删除挂起, 已保留;
      - replaced: 同路径已换成别的文件, 新文件保留。
    """
    if not expected:
        return ("kept", "无法核验文件身份，已保留")
    exp_size = int(expected.get("size", -1))
    exp_mtime = int(expected.get("mtime_ns", -1))
    exp_ino = int(expected.get("ino", -1))
    exp_dev = int(expected.get("dev", -1)) & 0xFFFFFFFF
    if os.name == "nt":
        api = win_api()
        ct = api["ctypes"]
        handle = api["CreateFileW"](
            str(path), api["GENERIC_READ"] | api["DELETE"],
            # 核验与删除期间拒绝写共享: 已有写入句柄 → CreateFileW 直接失败,
            # 进入保留分支; 我们持有句柄期间外部也无法再打开写句柄。
            api["FILE_SHARE_READ"] | api["FILE_SHARE_DELETE"],
            None, api["OPEN_EXISTING"], api["FILE_ATTRIBUTE_NORMAL"], None)
        if not handle or handle == api["INVALID_HANDLE_VALUE"]:
            err = ct.get_last_error()
            if err in (2, 3):
                return ("removed", "")
            if err == 32:
                return ("kept", "文件被占用，已保留")
            return ("kept", f"无法打开文件（WinError {err}），已保留")
        deleted = False
        try:
            info = api["BY_HANDLE_FILE_INFORMATION"]()
            if not api["GetFileInformationByHandle"](handle, ct.byref(info)):
                err = ct.get_last_error()
                return ("kept", f"无法核验文件身份（WinError {err}），已保留")
            fsize = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
            findex = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
            ft = (int(info.ftLastWriteTime.dwHighDateTime) << 32) \
                | int(info.ftLastWriteTime.dwLowDateTime)
            fmtime = (ft - 116444736000000000) * 100
            same = (int(info.dwVolumeSerialNumber) == exp_dev
                    and findex == exp_ino and fsize == exp_size
                    and fmtime == exp_mtime)
            if not same:
                return ("replaced", "同路径文件已被替换，已保留（未删除）")
            disp = api["FILE_DISPOSITION_INFO_STRUCT"]()
            disp.DeleteFile = True
            if not api["SetFileInformationByHandle"](
                    handle, api["FileDispositionInfo"], ct.byref(disp),
                    ct.sizeof(disp)):
                err = ct.get_last_error()
                return ("kept", f"文件无法删除（WinError {err}），已保留")
            deleted = True
        finally:
            api["CloseHandle"](handle)
        if not os.path.exists(str(path)):
            return ("removed", "")
        now = file_identity(path)
        if now is not None and not same_identity(now, expected):
            return ("replaced", "删除期间同路径已换成别的文件，新文件已保留")
        return ("kept", "删除挂起（仍有句柄占用），已保留")
    # 非 Windows 退路: 打开后 fstat 比对再 unlink
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        return ("removed", "")
    except OSError as e:
        return ("kept", f"无法打开文件：{e}")
    try:
        st = os.fstat(fd)
        same = (int(st.st_size) == exp_size
                and int(getattr(st, "st_mtime_ns", 0)) == exp_mtime
                and int(getattr(st, "st_ino", 0)) == exp_ino
                and (int(getattr(st, "st_dev", 0)) & 0xFFFFFFFF) == exp_dev)
        if not same:
            return ("replaced", "同路径文件已被替换，已保留（未删除）")
        try:
            os.unlink(str(path))
            return ("removed", "")
        except FileNotFoundError:
            return ("removed", "")
        except OSError as e:
            return ("kept", f"文件无法删除：{e}")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def rename_no_overwrite(src: "str | Path", dst: "str | Path") -> None:
    """把 src 提交到 dst; dst 已存在时抛 FileExistsError, 绝不覆盖。

    Windows 的 os.rename 本身拒绝覆盖; POSIX 用硬链接原子占位后再删 src。
    """
    if os.name == "nt":
        os.rename(str(src), str(dst))
        return
    os.link(str(src), str(dst))
    os.unlink(str(src))
