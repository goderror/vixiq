# core/failure_recovery.py
"""
把失败图片从源目录搬到目标目录。从旧 execute_failure.py 迁移,改造点:
- print → reporter
- 返回搬运数量(供 GUI 显示)
"""
import shutil
from pathlib import Path

from utils.reporter import Reporter, PrintReporter

_VALID_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}


def recover_failures(failed_list_file: Path,
                     src_dir: Path,
                     dst_dir: Path,
                     reporter: "Reporter | None" = None) -> int:
    """
    读 failed_list_file(每行一个 aid 或文件名),把 src_dir 里命中的图片搬到 dst_dir。
    返回搬运的图片数量。
    """
    reporter = reporter or PrintReporter()
    src_folder = Path(src_dir)
    dst_folder = Path(dst_dir)

    names = [p for p in src_folder.rglob('*') if p.suffix.lower() in _VALID_EXT]

    failed_ids = []
    try:
        with open(failed_list_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    failed_ids.append(line)
    except FileNotFoundError:
        reporter.error(f"失败列表文件不存在: {failed_list_file}")
        return 0

    dst_folder.mkdir(parents=True, exist_ok=True)
    count = 0
    for aid in failed_ids:
        hits = [pic for pic in names if aid in pic.name]
        for hit in hits:
            target = dst_folder / hit.name
            shutil.move(str(hit), str(target))
            reporter.log(f"已移动 → {target}")
            count += 1
    reporter.log(f"全部搬完!共 {count} 个图片")
    return count
