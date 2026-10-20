# core/dup_check.py
"""
画师文件夹查重。从旧 check_duplicate.py 迁移,改造点:
- 去掉 tkinter filedialog(移到 GUI 层)和所有 print
- 返回 {author_id: [folder_names]} 中重复项(len>1),GUI 用 Treeview 渲染
"""
import re
from pathlib import Path
from collections import defaultdict


def check_duplicates(target_path: Path) -> "dict[str, list[str]]":
    """
    扫描 target_path 下的一级子文件夹,按末尾 _数字 提取 author_id。
    返回所有重复(出现 >1 次)的 {author_id: [folder_names]}。
    """
    id_to_folders = defaultdict(list)
    for folder in target_path.iterdir():
        if not folder.is_dir():
            continue
        name = folder.name
        # 匹配最后一个下划线后的纯数字,支持 名字_12345 或 名字(whatever)_12345
        match = re.search(r'_(\d+)(?:\s|$)', name)
        if not match:
            continue
        author_id = match.group(1)
        id_to_folders[author_id].append(name)

    duplicates = {aid: folders for aid, folders in id_to_folders.items() if len(folders) > 1}
    return duplicates
