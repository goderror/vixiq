"""作者名 / 作者文件夹命名的**唯一**规则。

背景: 此前「Windows 非法字符」有两套处理 —— 一键整理把 `/` 换成 `_`,
下载器/反搜/自检却直接把它删掉。同一位作者于是长出两个夹
(实测: pixiv 现用名 `作者甲/作者乙` → 磁盘上同时存在
`作者甲_4997269` 与 `作者乙_4997269`),
一键整理每次都想改名、又因目标夹已存在而永远跳过 (点多少次都白点)。
现在全项目只保留这里一套规则, 建作者夹的地方一律 import 使用。
"""

import re

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
