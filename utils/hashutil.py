# utils/hashutil.py
"""文件内容指纹工具。

MD5 用于本地图片的「身份识别」(非安全场景):
  - 下载成功后算指纹落 pages.md5;
  - 对账时文件被移动/改名(连文件名自然键都失效)也能凭指纹认回来;
  - 同指纹多份 = 重复文件。
分块读取, 避免大图一次性读进内存。
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def md5_of(path: "Path | str", *, chunk: int = 1 << 16) -> "str | None":
    """计算文件 MD5 (十六进制字符串)。文件不存在/不可读返回 None。"""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()
    except OSError:
        return None
