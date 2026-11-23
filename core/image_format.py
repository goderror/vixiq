"""入库图片格式统一: 静态图片转 PNG, 并记录转换来源。

2026-11-21 4B-1: 下载/补页/强制重下/浏览器上传/图库路径导入共用这一套。
- 原生 PNG 直接登记; 动图(GIF/多帧 WEBP)保持原格式;
- 其他已支持的静态格式(jpg/jpeg/webp/bmp/tif/tiff)转 PNG;
- 转换成功且调用方要求时才删除源文件 (图库内只留 PNG, 原 JPG 不归档);
- 先把 PNG 写临时文件并校验可打开, 成功后才替换目标, 失败保留可用原文件。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

# 静态图片: 统一转 PNG
STATIC_TO_PNG = {".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
# 动图/多帧: 继续按原格式处理 (GIF 一律不转, 多帧 WEBP 由运行时检测)
ANIMATED_KEEP = {".gif"}


def format_of(path: "str | Path") -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return "jpg" if ext == "jpeg" else ext


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_animated(path: "str | Path") -> bool:
    """多帧图片判定 (GIF/WEBP/APNG); 打不开时按「非动图」交给后续转换报错。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return bool(getattr(im, "is_animated", False)) \
                or int(getattr(im, "n_frames", 1) or 1) > 1
    except Exception:
        return False


def actual_format_info(path: "str | Path") -> dict:
    """只读实际解码格式/帧数 (不信任扩展名), 供 4B-3 判断是否待转 PNG。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            fmt = str(im.format or "").lower()
            frames = int(getattr(im, "n_frames", 1) or 1)
            animated = bool(getattr(im, "is_animated", False)) or frames > 1
        return {"actual_format": fmt, "frames": frames, "animated": animated,
                "readable": True}
    except Exception as e:
        return {"actual_format": None, "frames": 0, "animated": False,
                "readable": False, "error": str(e).splitlines()[0]}


def pixel_digest(path: "str | Path") -> "str | None":
    """解码后的像素指纹: 用于「同口径」确认两张图内容是否一致。

    不看容器格式/压缩字节, 只看解码后的像素 (宽高 + 像素字节)。
    打不开或异常返回 None = 无法确认。"""
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGB")
            data = im.tobytes()
            return f"{im.size[0]}x{im.size[1]}:{hashlib.md5(data).hexdigest()}"
    except Exception:
        return None


def same_pixels(a: "str | Path", b: "str | Path") -> bool:
    da, db = pixel_digest(a), pixel_digest(b)
    return bool(da and db and da == db)


def is_readable(path: "str | Path") -> bool:
    """文件是否存在且图片结构可校验通过 (复用/旧版本可用性判断)。"""
    try:
        from PIL import Image
        p = Path(path)
        if not p.is_file():
            return False
        with Image.open(p) as im:
            im.verify()
        return True
    except Exception:
        return False


class PublishRejected(Exception):
    """发布前复核否决: 携带失败原因码与说明, 供调用方原样回报。"""

    def __init__(self, reason: str, note: str = ""):
        super().__init__(note or reason)
        self.reason = reason
        self.note = note


def _publish_new_file(tmp: "str | Path", out: "str | Path") -> None:
    """把已校验的临时文件发布到正式路径; 目标已存在时抛 FileExistsError, 绝不覆盖。

    4B-3 返修 ①: 预检通过不代表发布时目标仍为空 (另一请求/手动放入都可能抢先)。
    Windows 用 rename (目标存在会抛 FileExistsError), POSIX 用硬链接 (同样拒绝已存在目标)。
    """
    tmp_p, out_p = Path(tmp), Path(out)
    if out_p.exists():
        raise FileExistsError(str(out_p))
    if os.name == "nt":
        os.rename(str(tmp_p), str(out_p))
    else:
        os.link(str(tmp_p), str(out_p))
        tmp_p.unlink()


def normalize_to_png(src_path: "str | Path", *, out_path: "str | Path | None" = None,
                     delete_source: bool = False,
                     overwrite: bool = False,
                     before_publish: "Callable[[], None] | None" = None) -> dict:
    """按入库规则处理一张图, 返回转换结果与来源记录。

    - PNG: 原样登记 (指定了不同 out_path 时复制过去)
    - GIF/多帧: 保持原格式
    - 静态 JPG/JPEG/WEBP/BMP/TIF/TIFF: 生成并校验 PNG 后替换目标
    - 转换失败: ok=False + note, 原文件保留

    返回键: ok / converted / reason / current_path / current_format / current_size /
            source_format / source_file_size / source_md5 / converted_at / note /
            cleanup_failed
    reason: unreadable=源图打不开; exists=目标已存在; convert_failed=生成/替换失败;
            copy_failed=原生 PNG 复制失败; 空=正常。
    overwrite=True 时若目标 PNG 已存在, 先留一份 `.bak` 再覆盖 (沿用覆盖备份流程)。
    before_publish: 编码完成且临时文件校验通过后、正式发布前调用; 回调发现问题时
            抛 PublishRejected(reason, note), 本次不发布并原样回报。4B-3 用它复核
            源文件身份与页登记。正式发布用同盘唯一临时文件 + 拒绝覆盖的重命名,
            目标在预检后出现时返回 reason="exists", 绝不覆盖已有文件。
    """
    src = Path(src_path)
    out = Path(out_path) if out_path else src.with_suffix(".png")
    result = {
        "ok": True, "converted": False, "reason": "",
        "current_path": str(src), "current_format": format_of(src),
        "current_size": None,
        "source_format": None, "source_file_size": None, "source_md5": None,
        "converted_at": None, "note": "",
    }
    try:
        src_size = src.stat().st_size
    except OSError as e:
        result.update(ok=False, reason="unreadable",
                      note=f"源文件不可读：{str(e).splitlines()[0]}")
        return result
    result["current_size"] = src_size
    ext = src.suffix.lower()

    # ① 原生 PNG: 先完整解码校验, 再登记/复制 (损坏输入不得当成成功)
    if ext == ".png":
        try:
            from PIL import Image as _Image
            with _Image.open(src) as _im:
                _im.load()
        except Exception as e:
            result.update(ok=False, reason="unreadable",
                          note=f"源图片无法打开：{str(e).splitlines()[0]}")
            return result
        try:
            if out.resolve() != src.resolve():
                if out.exists() and not overwrite:
                    result.update(ok=False, reason="exists", note="目标 PNG 已存在")
                    return result
                out.parent.mkdir(parents=True, exist_ok=True)
                if overwrite and out.exists():
                    try:
                        shutil.copy2(str(out), str(out.with_name(out.name + ".bak")))
                    except Exception:
                        pass
                shutil.copy2(str(src), str(out))
                if delete_source:
                    try:
                        src.unlink(missing_ok=True)
                    except Exception:
                        result["cleanup_failed"] = True
            result["current_path"] = str(out)
            result["current_format"] = "png"
            result["current_size"] = out.stat().st_size
        except Exception as e:
            result.update(ok=False, reason="copy_failed",
                          note=f"PNG 复制失败：{str(e).splitlines()[0]}")
        return result

    # ② 动图/多帧: 保持原格式
    if ext in ANIMATED_KEEP or is_animated(src):
        result["note"] = "动图/多帧图片保持原格式"
        return result
    if ext not in STATIC_TO_PNG:
        result["note"] = "非静态图片，保持原格式"
        return result

    # ③ 静态图 → PNG: 先写临时文件并校验, 再替换目标
    if out.exists() and out.resolve() != src.resolve():
        if not overwrite:
            result.update(ok=False, reason="exists", note="目标 PNG 已存在")
            return result
        try:
            shutil.copy2(str(out), str(out.with_name(out.name + ".bak")))
        except Exception:
            pass
    try:
        from PIL import Image, ImageOps
    except Exception as e:
        result.update(ok=False, reason="convert_failed",
                      note=f"缺少图片库：{str(e).splitlines()[0]}")
        return result
    # 先确认源图真的能解码: 打不开的输入绝不能进入图库
    try:
        src_img = Image.open(src)
        src_img.load()
    except Exception as e:
        result.update(ok=False, reason="unreadable",
                      note=f"源图片无法打开：{str(e).splitlines()[0]}")
        return result
    cleanup_failed = False
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        # 同盘唯一临时文件名: 并发确认/上次中断残留都不会互相顶掉
        tmp = out.with_name(f".vixiq-png-{uuid.uuid4().hex}.tmp")
        try:
            with src_img as im:
                im = ImageOps.exif_transpose(im)
                if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                    im = im.convert("RGB")
                im.save(tmp, "PNG")
            # 校验: 结构可读 + 能真正解码 + 尺寸有效
            with Image.open(tmp) as chk:
                chk.verify()
            with Image.open(tmp) as chk:
                chk.load()
                if not chk.size[0] or not chk.size[1]:
                    raise ValueError("PNG 尺寸无效")
            # 发布前最后复核 (源身份/页登记), 有问题就地否决, 不碰正式路径
            if before_publish is not None:
                before_publish()
            if overwrite:
                os.replace(str(tmp), str(out))
            else:
                _publish_new_file(tmp, out)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                cleanup_failed = True
    except PublishRejected as e:
        result.update(ok=False, reason=e.reason, note=e.note)
        return result
    except FileExistsError:
        result.update(ok=False, reason="exists", note="目标 PNG 已存在 (发布时检测到)，未覆盖")
        return result
    except Exception as e:
        result.update(ok=False, reason="convert_failed",
                      note=f"PNG 转换失败：{str(e).splitlines()[0]}")
        return result

    try:
        from utils.hashutil import md5_of
        src_md5 = md5_of(src)
    except Exception:
        src_md5 = None
    if delete_source and src.resolve() != out.resolve():
        try:
            src.unlink(missing_ok=True)
        except Exception:
            cleanup_failed = True
    result.update({
        "ok": True, "converted": True, "reason": "",
        "current_path": str(out), "current_format": "png",
        "current_size": out.stat().st_size,
        "source_format": format_of(src), "source_file_size": src_size,
        "source_md5": src_md5, "converted_at": _now(), "note": "",
    })
    if cleanup_failed:
        result["cleanup_failed"] = True
    return result
