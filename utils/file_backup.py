"""覆盖前 `.bak` 备份规则 (下载器 / 重下修复入口共用)。

普通成功任务沿用旧策略: 用当前在位文件刷新 `.bak`。
若上一轮登记失败且旧图尚未恢复, 正式路径是失败的新图, 而 `.bak` 才是
**登记中的原图** (用户回执里给过的可恢复副本)。这时重试绝不能拿失败新图
覆盖 `.bak`, 否则原图字节再无副本。判定只依赖登记指纹, 不新增标记文件:

  - 在位文件 == 登记文件: 正常状态, 按旧策略备份在位文件;
  - 在位文件 != 登记文件, 且 `.bak` == 登记文件: 沿用上一轮原图备份;
  - 在位文件 != 登记文件, 且 `.bak` 无法核验为登记文件: 拒绝覆盖, 交给人工;
  - `.bak` 与在位文件字节一致: 直接复用, 不重复复制。
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path


def file_sig(path: "str | Path") -> "tuple[str | None, int | None]":
    try:
        from utils.hashutil import md5_of
        return md5_of(str(path)), os.path.getsize(str(path))
    except Exception:
        return None, None


def ensure_overwrite_backup(src_path: "str | Path", bak_path: "str | Path",
                            registered_md5: "str | None" = None,
                            registered_size: "int | None" = None) -> dict:
    """准备覆盖前的 `.bak`，返回
    {action: reuse|copy|refuse, reason, bak_md5, bak_size, created}。
    """
    src = Path(src_path)
    bak = Path(bak_path)
    src_md5, src_size = file_sig(src) if src.is_file() else (None, None)
    bak_md5, bak_size = file_sig(bak) if bak.is_file() else (None, None)
    reg_md5 = str(registered_md5) if registered_md5 else None
    try:
        reg_size = int(registered_size) if registered_size is not None else None
    except (TypeError, ValueError):
        reg_size = None
    has_reg = bool(reg_md5 and reg_size is not None)
    src_is_reg = bool(has_reg and src_md5 == reg_md5 and src_size == reg_size)
    bak_is_reg = bool(has_reg and bak_md5 == reg_md5 and bak_size == reg_size)

    if src_md5 is None:
        return {"action": "refuse", "reason": "无法读取当前文件指纹，已停止覆盖",
                "bak_md5": bak_md5, "bak_size": bak_size, "created": False}
    # 有可信登记指纹时, 在位图必须先与登记一致; 不符时只认与登记一致的旧备份。
    # 不能把错误在位图 (或与它相同的错误 .bak) 当成可恢复原图。
    if has_reg and not src_is_reg:
        if bak_md5 is not None and bak_is_reg:
            return {"action": "reuse",
                    "reason": "沿用上一轮已报告的原图备份",
                    "bak_md5": bak_md5, "bak_size": bak_size, "created": False}
        return {"action": "refuse",
                "reason": "当前文件与登记不一致，且现有备份无法核验为原图，"
                          "已停止覆盖",
                "bak_md5": bak_md5, "bak_size": bak_size, "created": False}
    # 在位图匹配登记 (或无历史指纹): 允许复用同字节备份, 否则刷新 .bak
    if bak_md5 is not None and bak_md5 == src_md5 and bak_size == src_size:
        return {"action": "reuse", "reason": "现有备份与当前文件字节一致",
                "bak_md5": bak_md5, "bak_size": bak_size, "created": False}

    # 普通覆盖 / 没有可用旧备份: 用当前在位文件刷新 .bak
    tmp = bak.with_name(bak.name + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(tmp))
        got_md5, got_size = file_sig(tmp)
        if got_md5 is None or got_md5 != src_md5 or got_size != src_size:
            raise OSError("备份文件指纹校验失败")
        os.replace(str(tmp), str(bak))
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return {"action": "refuse", "reason": f"备份文件写入失败：{e}",
                "bak_md5": None, "bak_size": None, "created": False}
    return {"action": "copy", "reason": "已备份当前在位文件",
            "bak_md5": src_md5, "bak_size": src_size, "created": True}
