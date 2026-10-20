# utils/image.py
"""图像处理工具。从旧 utils.py 迁移,临时目录改走 paths.user_data。"""
import tempfile
from pathlib import Path

from PIL import Image

from utils import paths


def compress_image_for_upload(img_path: Path, max_size: int = 600, temp_dir: "Path | None" = None) -> Path:
    """
    把图片压缩到最长边 ≤ max_size,返回临时压缩文件路径。
    压缩在内存中进行,原图丝毫不受影响。用于 saucenao 上传(限制图片大小)。
    """
    with Image.open(img_path) as im:
        # 已是小图直接返回原路径
        if max(im.width, im.height) <= max_size:
            return Path(img_path)

        # 保留宽高比缩放
        ratio = max_size / max(im.width, im.height)
        new_size = (int(im.width * ratio), int(im.height * ratio))
        im_resized = im.resize(new_size, Image.Resampling.LANCZOS)

        if temp_dir is None:
            temp_dir = paths.user_data("temp_upload")
        temp_dir.mkdir(parents=True, exist_ok=True)

        # 创建临时文件(程序负责后续清理)
        tmp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=Path(img_path).suffix,
            dir=str(temp_dir),
            prefix="upload_",
        )
        im_resized.save(tmp_file.name, quality=95, optimize=True)
        tmp_file.close()
        return Path(tmp_file.name)
