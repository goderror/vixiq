"""作者名 / 作者文件夹命名的**唯一**规则 (治本)。

背景: 此前「Windows 非法字符」有两套处理 —— 一键整理把 `/` 换成 `_`,
下载器/反搜/自检却直接把它删掉。同一位作者于是长出两个夹
(实测: pixiv 现用名 `作者甲/作者乙` → 磁盘上同时存在
`作者甲_4997269` 与 `作者乙_4997269`),
一键整理每次都想改名、又因目标夹已存在而永远跳过 (点多少次都白点)。
现在全项目只保留这里一套规则, 建作者夹的地方一律 import 使用。
"""

import re
import unicodedata
from pathlib import Path

# 分类夹名关键词 (2026-11-18): 作者夹可能被用户手工分到 X图 / 涩图(R18) 等子目录。
# 同一作者夹出现在多个分类下时，按作品 R18 选对应分类；认不出就把选择权交回调用方。
R18_SECTION_HINTS = ("r18", "r-18", "涩图")
GENERAL_SECTION_HINTS = ("x图", "普通", "一般", "全年龄")

# 污染括号: 下载时被追加进文件夹名/昵称的「源站账号」或「旧名_旧ID」, 不是作者真名
# (真名自带的括号如 藤ちょこ（藤原）、弱電波(@JackDempa) 不匹配这些模式, 不会误剥)
POLLUTE_PAREN = re.compile(
    r'[（(](?:[a-zA-Z0-9_-]*(?:twi|bili|ggac|twitter|danbooru|x\.com)\s*@[^（）()]*'
    r'|[^（）()]*_\d{6,9})[）)]')

# Windows 非法文件名字符 (pixiv 现用名可能带 * : ? / 等, 文件夹名必须清洗)
# 统一替换成 `_` 而不是删掉: 删掉会让 `Re:Rin`→`ReRin`、`HxxG / ホン`→`HxxG  ホン`,
# 与一键整理算出来的名字对不上 (见模块 docstring)。
_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 宣传/状态后缀 (pixiv 昵称里常见的 @画集発売中 / 夏コミxx / FANBOX 等, 不是真名)
# 注意: @摊位号/@账号 是真名一部分 (La~La@bqdtc.617、XilmO@夕末), 只有 @ 后带宣传关键词才剥
_PROMO_KW = r'画集|単行本|新刊|委託|募集|FANBOX|修行|コミ|販売|通販|入稿|活動|発売|更新|誕生|イベント|曜'
_PROMO_RE = re.compile(
    r'(?:'
    r'[@＠](?=[^（）()@＠]*(?:' + _PROMO_KW + r'))[^（）()@＠]*'
    r'|[/／]\s*(?=[^（）()/／]*(?:' + _PROMO_KW + r'))[^（）()/／]*'
    r'|(?:画集発売中|単行本発売中|新刊委託中|お仕事募集中|FANBOX|修行中|販売中|通販中|入稿中?|活動中|発売中|更新中|誕生祭\S*|'
    r'夏コミ\S*|冬コミ\S*|コミケ\S*|ﾃｨｱさ\S*|[日月火水木金土]曜\S*))$', re.UNICODE)


def sanitize_dir(s: str) -> str:
    """清洗成合法 Windows 文件夹名 (非法字符→_ , 去首尾点/空格)。"""
    s = _WIN_BAD.sub('_', s or '').strip().strip('. ')
    return s or 'unknown'


def clean_author_name(name: str) -> str:
    """清洗作者名用于建夹 (与 `sanitize_dir` 同一套非法字符规则), 空则「未知画师」。

    下载器/反搜/自检共用。注意 pixiv 昵称允许纯数字 (如画师就叫 "310123"),
    不能把纯数字当无效名拒掉, 否则会把真实昵称错改成「未知画师」。
    """
    s = _WIN_BAD.sub('_', (name or '').strip()).strip().strip('. ')
    return s or "未知画师"


def is_unreadable_name(name: str) -> bool:
    """名字是否「根本搜不出」: 纯空白/零宽不可见字符, 或纯符号 (如。 *** ㅤ);
    或占位名 (未知作者/未知画师/未知/佚名/unknown 等, 不是真名)。
    这类名字是作者故意改成藏身份的或系统兜底, 界面/文件夹/主名称都应回退,
    不当主名、不因它建作者文件夹。"""
    s = str(name or "").strip()
    # 空串按「可辨认」处理 (T43 语义: 空名是正常状态, 不是损坏名; 调用方自行判空)
    if re.fullmatch(r'[\s\u200b-\u200d\ufeff\u3164\u00a0\u3000\W_]+', s):
        return True
    return s.lower() in {
        "未知", "未知作者", "未知画师", "佚名", "无名氏", "无名", "待定",
        "unknown", "unknown author", "unknown_author", "unknown artist",
        "none", "null", "n/a", "na", "anonymous", "anonym",
    }


def strip_promo(name: str) -> str:
    """剥离昵称尾部宣传/状态后缀, 保留真名主体 (Anmi@画集発売中→Anmi)。"""
    s = (name or '').strip()
    for _ in range(3):
        s2 = _PROMO_RE.sub('', s).rstrip(' /／@＠').strip()
        if s2 == s:
            break
        s = s2
    return s


def folder_base(name: str) -> str:
    """文件夹基底名: 剔除污染括号 + 宣传后缀, 保留真名自带括号/@ (弱電波(@JackDempa))。"""
    return sanitize_dir(POLLUTE_PAREN.sub('', strip_promo(name) or '').strip())


def search_promo(name: str) -> bool:
    """昵称是否含宣传/状态关键词 (选别名时丢弃, 也是文件夹基底的补充判断)。"""
    return bool(re.search(
        r'画集|単行本|新刊|委託|募集|FANBOX|修行|コミ|販売|通販|入稿|活動|発売|更新|誕生|イベント|[日月火水木金土]曜', name))


def author_folder_name(db, aid: str, cur_name: str) -> str:
    """按约定生成作者文件夹名: 「现用名（≤2个精选别名）_ID」。
    别名优先级: 源站账号/旧名; 剔除与现用名重复、被现用名包含的读音片段、
    「旧名_旧ID」残留。现用名为空白符号时退回最近一个可辨认别名当基底 (DB 仍记现用名)。

    这是**全项目唯一**的作者夹名生成器 —— 下载器、反搜归档、自检归位、一键整理
    必须都调它, 否则同一位作者会被摆成两个夹 (见模块 docstring)。
    """
    cur = (cur_name or '').strip()
    base = folder_base(cur)
    aliases: "list[str]" = []
    try:
        a = db.get_author(aid)
        if a:
            aliases = list(a.get("aliases") or [])
    except Exception:
        aliases = []
    # 现用名不可辨认 → 用第一个可辨认别名做文件夹基底 (如 ちんぽ王国 → ㅤ)
    if not base or is_unreadable_name(base):
        for x in aliases:
            xx = POLLUTE_PAREN.sub('', x or '').strip()
            xx = re.sub(r'_\d{6,9}$', '', xx).strip()
            if xx and not is_unreadable_name(xx):
                return f"{sanitize_dir(xx)}_{aid}"
        return f"unknown_{aid}"
    # 精选别名 (≤2): 只记「值得搜」的, 读音/稀有名/不可见名/超长名/状态后缀不记
    _PROMO = re.compile(r'画集発売中|単行本発売中|新刊|委託中|お仕事募集中|FANBOX|修行中|'
                        r'夏コミ|冬コミ|コミケ|ﾃｨｱさ|日曜|月曜|火曜|水曜|木曜|金曜|土曜|'
                        r'販売中|通販中|入稿|活動中|誕生祭|発売中|更新中')
    sel: "list[str]" = []
    for x in aliases:
        x = POLLUTE_PAREN.sub('', x or '').strip()
        x = re.sub(r'_\d{6,9}$', '', x).strip()
        # 别名若是「现用名（别名）」复合 → 取括号内真正有用的部分 (H（冰马Hyouuma）→ 冰马Hyouuma)
        m = re.match(r'^(.*?)[（(]([^（）()]*)[）)]$', x)
        if m and m.group(1).strip() == cur:
            x = m.group(2).strip()
        x = x.strip('/').strip().lstrip('@＠').strip()
        if not x or x == cur or x in sel or len(x) > 30:
            continue
        if is_unreadable_name(x) or _PROMO.search(x) or x in cur:
            continue  # 不可见/纯符号 / 状态后缀 / 读音被现用名包含 (セトラ) → 跳过
        sel.append(x)
        if len(sel) >= 2:
            break
    if sel:
        # 多个别名用顿号连接 —— 绝不能用 `/`: 它会被当成路径分隔符, 把文件夹名劈成两层
        # (潜伏 bug, 2026-09-14 首次踩到: 某作者出现第 2 个别名时才会触发, 此前一直没暴露)
        return f"{sanitize_dir(base)}（{'、'.join(sanitize_dir(s) for s in sel)}）_{aid}"
    return f"{sanitize_dir(base)}_{aid}"


def _author_id_in_name(name: str, ids: set):
    """目录名里是否带 authors 表里的 ID（从右往左试 `_`，兼容 local_xxx）。"""
    idx = len(name)
    while True:
        idx = name.rfind("_", 0, idx)
        if idx <= 0:
            return None
        cand = name[idx + 1:]
        if cand in ids:
            return cand


def collect_author_dir_parents(root, author_ids) -> dict:
    """扫描图库，返回 {author_id: {作者夹的父相对路径}}。

    父路径 `""` 表示作者夹就在图库根目录；`"X图"` 表示在分类子目录下。
    作者夹判定与 `_is_author_folder` 同一口径：目录名从右往左试 `_` 后面的
    每一段，命中 authors 表里的 ID 即算（`local_xxx` 本身含下划线）。
    只读目录名，不碰文件；用于「同一作者已有分类夹时，新文件跟着走」。
    """
    root = Path(root)
    ids = {str(x) for x in (author_ids or []) if str(x)}
    out: "dict[str, set]" = {}
    if not ids:
        return out
    try:
        dirs = [p for p in root.rglob("*") if p.is_dir()]
    except OSError:
        return out
    for p in dirs:
        aid = _author_id_in_name(p.name, ids)
        if aid:
            rel = p.relative_to(root)
            parent = "" if str(rel) == p.name else rel.parent.as_posix()
            out.setdefault(aid, set()).add(parent)
    return out


def collect_section_dirs(root, author_ids) -> set:
    """图库顶层的「分类目录」候选：不是作者夹、不是 `#来源#` 夹、不以 `.` 开头。

    用户手工把作者夹分到 `X图` / `涩图` 等子目录时，这些目录要先被认出来，
    新作者才能按 R18 落进对应分类，而不是继续堆在根目录。
    """
    root = Path(root)
    ids = {str(x) for x in (author_ids or []) if str(x)}
    out = set()
    try:
        tops = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return out
    for p in tops:
        name = p.name
        if name.startswith(".") or (name.startswith("#") and name.endswith("#")):
            continue
        if ids and _author_id_in_name(name, ids):
            continue
        out.add(name)
    return out


def pick_section_parent(parents, is_r18):
    """从作者夹的父路径集合里选一个分类前缀。

    返回 `""`（根目录）、具体前缀（如 `"X图"`），或 `None`（多个分类且按
    R18 猜不准 —— 调用方应保持现状，不擅自搬家）。
    """
    non_root = sorted({str(p) for p in (parents or ()) if str(p)})
    if not non_root:
        return ""
    if len(non_root) == 1:
        return non_root[0]
    if is_r18 is None:
        return None
    hints = R18_SECTION_HINTS if is_r18 else GENERAL_SECTION_HINTS
    hits = [p for p in non_root if any(h in p.lower() for h in hints)]
    return hits[0] if len(hits) == 1 else None


def pick_default_section(sections, is_r18):
    """新作者该落哪个分类：按 R18 关键词唯一命中才用，认不出返回 None。"""
    names = sorted({str(x) for x in (sections or ()) if str(x)})
    if not names or is_r18 is None:
        return None
    hints = R18_SECTION_HINTS if is_r18 else GENERAL_SECTION_HINTS
    hits = [n for n in names if any(h in n.lower() for h in hints)]
    return hits[0] if len(hits) == 1 else None


def normalize_author_name(name: str) -> str:
    """用于查重的规范化名字：全半角统一、去空白/零宽、去宣传后缀与污染括号、转小写。

    只用于判断「看起来是不是同一个名字」，不写回数据库、不改显示名。
    同名不等于同一人，查重结果只用于提示/让用户确认，绝不自动合并。
    """
    s = POLLUTE_PAREN.sub("", strip_promo(str(name or "")) or "")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\s\u200b-\u200d\ufeff\u3164\u00a0\u3000]+", "", s)
    return s.strip().lower()

