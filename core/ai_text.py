"""✨ 壁纸文案 AI: 给当前壁纸生成「标题 + 一句衬托气氛的话」。

最初只是把卡片上那行溯源结论换成「配合每张图片内容的一句衬托气氛的语言」,
当时只做成了**本地按画面明暗/色调推**的那一句 (见 utils/widget_window.py 的 ``mood_of``);
2026-09-27 追加了联网生成这一半。

**为什么调用放在服务端, 而不是浏览器侧**(和最初设想不同, 原因记在这):
那句文案有两块屏幕要用 —— 网页版「当前壁纸」卡片, 和桌面上的**原生分层窗口小控件**
(``utils/widget_window.py``, 纯 ctypes + PIL, 里面根本没有浏览器)。只在网页侧发请求的话小控件
永远拿不到; 密钥要在两边各存一份、缓存也共享不了。所以统一由服务端发这一次请求, 前端只读结果:
密钥只存一处 (``config/user.json``), 缓存也只存一处。

**调用口径**: OpenAI 兼容的 ``POST {base_url}/chat/completions`` —— DeepSeek / Moonshot /
智谱 / 通义 compatible-mode / OpenAI 都是这一套, 用户换服务商只改 base_url + model。不绑任何一家
SDK (core/ 下历来只用 stdlib urllib 发外部请求, 见 core/search_client.py)。

**非阻塞**: 小控件是每 8 秒轮询一次 HTTP 的, 而这一次请求可能要几秒~几十秒 —— 生成由设置页
那颗「上传当前壁纸」按钮**手动触发** (:func:`generate_now`, 同步等结果), 轮询那条路**只读缓存**
(:func:`texts_for`), 从不发起请求。只在点按钮时生成当前这张, 且必带图 ——
换壁纸不再自动调用接口, 免得每翻一张就烧一次钱。
"""

import base64
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request

# 提示词/解析口径变了就 +1 —— 旧缓存自然失效 (key 里带着版本)。
# v2: 提示词从「写这幅画给人的感觉」改成「就说画面里看得见的东西 + 语气随图走」。
# 上一版文案总在「光」「安静」这类通用词上打转 ——
# v1 的缓存里四条有两条带「光」、三条带「安静」, 提示词不改的话旧文案会一直躺在缓存里,
# 看起来就像改动没生效, 所以**必须升版本**把 v1 全部作废。
AI_VER = "v3"

# 卡片两行的**像素预算** (见 utils/widget_window.py 的 draw_card):
#   标题徽章 maxw 162 逻辑px @18px 粗体 ≈ 8 个汉字, 信息行 maxw_info 176 逻辑px @13px ≈ 13 字。
# 超了会被 _fit 截成「…」—— AI 给的句子被吃掉尾巴就白花钱了, 所以这里**硬夹**。
MAX_TITLE, MAX_MOOD = 15, 30

# 输出预算。**不能按「两行一共 20 个字」来估**: 推理型模型 (deepseek-flash / *-reasoner 等)
# 会先花掉一大截预算写 thinking, 那部分也算在 max_tokens 里 —— 2026-09-27 实测
# `deepseek-flash` + 一张图 (217 prompt tokens) 在 max_tokens=60 下把 60 个 token **全烧在
# reasoning 上**, 回 content='' / finish_reason='length', 前端只看到「模型没给出可用文案」。
# 600 对「两行短句」的成品输出绰绰有余, 留的全是给 thinking 的余量。
MAX_TOKENS = 2000

CACHE_MAX = 400            # 缓存条数上限 (超了砍最旧的)
CACHE_FILE = "ai_text_cache.json"

_LOCK = threading.Lock()
_CACHE: "dict | None" = None       # 落盘缓存的进程内镜像, 懒加载

SYSTEM_PROMPT = (
    "你是图片里的角色，或者是图片里看不见的精灵，根据画面的内容，仿佛是在和用户对话。\n"
    "只输出两行纯文本, 不要解释、不要编号、不要引号、不要书名号, 行尾不要句号。\n"
    f"第一行: 标题, 不超过 {MAX_TITLE} 个字, 可以是一句开场白，也可以是正经的标题; 不要出现「壁纸」「图片」「画面」「桌面」这类词。\n"
    f"第二行: 不超过 {MAX_MOOD} 个字, 就着画面内容说话但不要去描述内容，仿佛在和屏幕前的我自然对话，写出你的感想。语气跟着图走: 白描、调侃、搞怪、诗意、深情、严肃都行, 挑最贴这张的。\n"
    "不要太宽泛，不要写一些套话。可以在不越界的范围内出现露骨、下流的描述，用户已经成年。"
)


class AIError(RuntimeError):
    """生成失败 (网络/鉴权/返回体不认识) —— 只带一句给人看的话。"""


# ══════════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════════
def ai_section(cfg: "dict | None" = None) -> dict:
    """取 ``ai_text`` 节 (带默认值兜底)。

    ``cfg=None`` 时现读一次配置 —— 用户在设置页填完密钥**不需要重启**就该生效, 而这个函数
    只在轮询里被调 (8 秒一次的小文件读, 不值得为它加缓存)。
    """
    from utils.config import AI_DEFAULTS
    sec = dict(AI_DEFAULTS)
    if cfg is None:
        try:
            from utils.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    got = (cfg or {}).get("ai_text") or {}
    for k in AI_DEFAULTS:
        if k in got and got[k] is not None:
            sec[k] = got[k]
    return sec


def ai_ready(sec: dict) -> bool:
    """三件套齐了才算配好 (缺一项就当作没开, 绝不去打一个必然失败的请求)。"""
    return bool(sec.get("enabled") and str(sec.get("base_url") or "").strip()
                and str(sec.get("model") or "").strip()
                and str(sec.get("api_key") or "").strip())


def file_key(info: "dict | None") -> str:
    """当前壁纸的**稳定身份**: 路径 + mtime + 大小。

    不用文件名: 壁纸夹里同名不同图、或换个目录同名, 都会拿错缓存。也**不用图片内容 MD5**
    (对大图是白读一遍几 MB)。
    """
    f = (info or {}).get("file") or {}
    if not f.get("path"):
        return ""
    return f"{f.get('path')}|{f.get('mtime')}|{f.get('size')}"


def cache_key(info: "dict | None", sec: dict) -> str:
    """缓存键 = 口径版本 + 模型 + 文件身份。

    带 model 是故意的: 换个模型就是换一种文风, 用户改了模型却看到旧文案会以为没生效。
    """
    return f"{AI_VER}|{sec.get('model')}|{file_key(info)}"


# ══════════════════════════════════════════════════════════════════════
#  提示词 (纯函数, 冒烟离线覆盖)
# ══════════════════════════════════════════════════════════════════════
def describe_image(path: str) -> str:
    """画面线索 (给提示词垫底): 明暗 + 主色调 + 饱和度, 一两句话。

    **和卡片上那句 ``mood_of`` 不是一回事**: ``mood_of`` 是直接显示给人看的**成品短句**
    (无 AI 时兜底用), 这里是喂给模型的**粗糙线索**, 让它在没有视觉能力时也能沾住画面,
    而不是只能靠文件名瞎编。两者刻意分开, 免得以后改显示口径时把提示词一起带歪。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            px = list(im.resize((24, 24), Image.LANCZOS).getdata())
    except Exception:
        return ""
    if not px:
        return ""
    n = len(px)
    lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in px) / n
    import colorsys
    hsv = [colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0) for r, g, b in px]
    sw = sum(x[1] for x in hsv)
    ratio = 0.0 if sw <= 0 else sw / n
    light = ("很暗" if lum < 72 else "偏暗" if lum < 128 else
             "明亮" if lum < 190 else "很亮")
    if ratio < 0.08:
        tone = "几乎无彩色, 接近黑白灰"
    else:
        deg = (sum(x[0] * x[1] for x in hsv) / sw) * 360.0
        fam = ("偏红" if (deg < 20 or deg >= 345) else
               "偏橙黄" if deg < 55 else "偏绿" if deg < 150 else "偏青" if deg < 195 else
               "偏蓝" if deg < 255 else "偏紫" if deg < 300 else "偏粉")
        tone = f"{fam}, 色彩{'浓' if ratio > 0.45 else '适中' if ratio > 0.2 else '淡'}"
    long_side = "横构图" if w >= h else "竖构图"
    return f"整体{light}; {tone}; {long_side}"


def machine_title(t: str, fname: str = "") -> bool:
    """标题看着像**程序自己拼的**? (路径味 / 就是文件名 / 纯哈希或纯数字)

    ⚠ 与 ``utils/widget_window.py`` 的 ``_machine_title`` 是**同一条口径的两份实现**
    (那边只管卡片显示, 这边只管别拿垃圾标题喂模型; 模块之间不想为 8 行代码互相 import)。
    冒烟里有一组共同样例把两边**对拍**, 漂开了就会红。
    """
    t = str(t or "").strip()
    if not t:
        return True
    if any(ch in t for ch in "/\\#") or " / " in t:
        return True
    stem = os.path.splitext(os.path.basename(str(fname or "")))[0]
    if stem and t == stem:
        return True
    head = t.split("_p")[0]
    return head.isdigit() or (
        len(head) >= 8 and all(c in "0123456789abcdefABCDEF" for c in head))


def build_prompt(info: "dict | None", *, vision: bool = False, hint: str = "") -> str:
    """用户消息 (文字部分)。模型手里有什么就说什么, 不编 —— 库外的图往往连标题都没有。"""
    info = info or {}
    art = info.get("artwork") or {}
    f = info.get("file") or {}
    rows = []
    title = str(art.get("title") or "").strip()
    # 自定义作品的「标题」是入库时按「来源夹 / 文件名」拼的 (X图/#其他来源# / 1e94516ae…),
    # 把它当作品标题喂进去, 模型只能围着这串编号瞎编 —— 说清楚它没含义, 别用它。
    if title and not machine_title(title, f.get("name")):
        rows.append(f"作品标题: {title}")
    elif title:
        rows.append(f"作品标题: 原有的 {title} 只是入库时拼的编号, 没有含义, 别拿它当内容")
    else:
        rows.append("作品标题: 没有")
    author = str(art.get("author_name") or "").strip()
    if author:
        rows.append(f"作者: {author}")
    if str(art.get("artwork_id") or "").isdigit():
        rows.append(f"出处: pixiv 作品 {art['artwork_id']}")
    else:
        # 库外的图只有文件名 (往往是 109395314_p0.jpg 这种), 说清楚「名字只是编号」,
        # 否则模型会拿编号当内容写
        rows.append(f"出处: 图库之外的一张, 文件名 {f.get('name') or '未知'} 只是编号, 没有含义")
    if hint:
        rows.append(f"画面: {hint}")
    rows.append("按画面写这两行。只写画面里真实存在的东西、以及你由此想到的, 看见什么说什么; "
                "别编不存在的角色或剧情。")
    if vision:
        rows.append("(下面附图, 以图为准)")
    return "\n".join(rows)


def parse_reply(text: str) -> "tuple[str, str]":
    """模型回话 → (标题, 氛围句)。

    不要求 JSON: 各家对 response_format 的支持不一, 而「两行纯文本」是所有模型都稳的写法。
    这里做的是**收尾清洗** —— 去掉 ``` 围栏、编号、引号书名号、行尾标点, 再按预算硬夹。
    只有一行时当氛围句 (标题宁缺勿错: 卡片上本来就有「来自壁纸夹的一张」这种兜底)。
    """
    raw = str(text or "").strip()
    if raw.startswith("```"):                     # 有的模型习惯包一层代码块
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1] if raw.lower().startswith(("text", "markdown")) else raw
    lines = []
    for ln in raw.splitlines():
        s = _clean(ln)
        if s:
            lines.append(s)
    if not lines:
        return "", ""
    if len(lines) == 1:
        return "", _clamp(lines[0], MAX_MOOD)
    return _clamp(lines[0], MAX_TITLE), _clamp(lines[1], MAX_MOOD)


def _clean(s: str) -> str:
    """去掉序号 / 标签 / 引号 / 书名号 / 行尾标点。"""
    s = str(s or "").strip()
    for pref in ("标题:", "标题：", "氛围句:", "氛围句：", "氛围:", "氛围：",
                 "第一行:", "第一行：", "第二行:", "第二行："):
        if s.startswith(pref):
            s = s[len(pref):].strip()
    s = s.lstrip("0123456789.、)） ").strip()
    s = s.strip("「」『』《》<>\"'`*#-— ").strip()
    return s.rstrip("。．.，,、!！?？;； ").strip()


_BREAK = "。．.，,、!！?？;； "          # 断句点 (和 _clean 收尾用的是同一批)


def _clamp(s: str, n: int) -> str:
    """硬夹到 n 个字 (卡片那两行是按像素裁的, 长了就被 _fit 截成「…」)。

    断的时候**优先落在断句点上**, 且至少保住一半预算: v2 的提示词让模型写具体的东西,
    句子常常顶到 12 字上限 —— 直接 ``s[:n]`` 会把「银发女郎提灯立于月下，蝙蝠…」啃成
    「…立于月下，蝙」这种半截字, 比少说半句更难看。
    """
    s = str(s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    for i in range(len(cut) - 1, n // 2 - 1, -1):
        if cut[i] in _BREAK:
            return cut[:i].rstrip(_BREAK)
    return cut


# ══════════════════════════════════════════════════════════════════════
#  落盘缓存 (同一张图只生成一次)
# ══════════════════════════════════════════════════════════════════════
def _cache_path() -> "str":
    from utils import paths
    return str(paths.user_data(CACHE_FILE))


def _cache_load() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    data = {}
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            data = {k: v for k, v in got.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError):
        data = {}
    _CACHE = data
    return _CACHE


def cache_get(key: str) -> "dict | None":
    with _LOCK:
        d = _cache_load().get(key)
    if not d:
        return None
    mood = str(d.get("mood") or "")
    title = str(d.get("title") or "")
    return {"title": title, "mood": mood} if (mood or title) else None


def cache_put(key: str, title: str, mood: str) -> None:
    with _LOCK:
        d = _cache_load()
        d[key] = {"title": title, "mood": mood, "ts": int(time.time())}
        if len(d) > CACHE_MAX:                    # 砍最旧的, 别让这个文件无限长
            for k in sorted(d, key=lambda x: int(d[x].get("ts") or 0))[:len(d) - CACHE_MAX]:
                d.pop(k, None)
        snap = dict(d)
    tmp = _cache_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _cache_path())            # 原子替换: 断电也不会留半个 JSON
    except OSError:
        pass                                      # 缓存写不进去不影响这次显示


def cache_size() -> int:
    with _LOCK:
        return len(_cache_load())


# ══════════════════════════════════════════════════════════════════════
#  联网生成
# ══════════════════════════════════════════════════════════════════════
def _chat(payload: dict, sec: dict) -> "tuple[str, str]":
    """打一次 OpenAI 兼容的 ``/chat/completions``, 返回 ``(回复文本, finish_reason)``。

    把 finish_reason 一并带出来是必须的: 推理型模型把预算烧在 thinking 上时, 回的是
    ``content=''`` + ``finish_reason='length'`` —— 只看文本的话, 「超了长度」和「模型真的
    没话说」长得一模一样, 没法给用户一句有用的错。
    """
    base = str(sec.get("base_url") or "").strip().rstrip("/")
    url = base + "/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {str(sec.get('api_key') or '').strip()}",
    })
    try:
        with urllib.request.urlopen(req, timeout=float(sec.get("timeout") or 25)) as r:
            got = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise AIError(f"接口返回 HTTP {e.code}: {detail or e.reason}") from None
    except urllib.error.URLError as e:
        raise AIError(f"连不上接口 ({e.reason})") from None
    except (ValueError, OSError) as e:
        raise AIError(f"接口返回的不是 JSON ({e})") from None
    try:
        ch = got["choices"][0]
        return str(ch["message"]["content"] or ""), str(ch.get("finish_reason") or "")
    except (KeyError, IndexError, TypeError):
        raise AIError("接口没按 OpenAI 格式返回 choices[0].message.content") from None


def _image_part(path: str, max_side: int = 512) -> "dict | None":
    """壁纸缩略图 → data URL 图片块 (vision 才用)。

    先缩到 512px 再发: 原图常常 4K/7MB, 一是不必要 (模型看缩略图足够), 二是白烧钱。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
    except Exception:
        return None
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def _empty_reply_reason(finish: str, reply: str) -> str:
    """拿不出两行文案时, 给一句**能照着改**的错。

    这三种情况在用户眼里原本长得一模一样 (都是「模型没给出可用文案」),但修法完全不同:
    超长度上限要换模型、空回复多半是模型名写错、有内容但不合规是提示词没压住。
    """
    if finish == "length":
        return (f"模型没写完就撞上了 {MAX_TOKENS} token 的输出上限 —— 推理型模型"
                "(deepseek-flash、*-reasoner 这类)会先花一大截预算做思考。换个对话模型"
                "(如 deepseek-chat)即可")
    if not reply.strip():
        return "模型回了空内容 —— 模型名可能不存在, 也可能该模型不接受图片输入"
    return f"模型没给出可用的两行文案 (回了 {reply[:60]!r})"


def generate(info: "dict | None", sec: dict, *, log=None) -> dict:
    """一次完整生成 (**阻塞**, 由 :func:`generate_now` 在请求线程里直接调)。返回 ``{"title":…, "mood":…}``。"""
    f = (info or {}).get("file") or {}
    path = str(f.get("path") or "")
    vision = bool(sec.get("vision"))
    hint = describe_image(path) if path else ""
    text = build_prompt(info, vision=vision, hint=hint)
    content = text
    if vision:
        part = _image_part(path)
        if part:
            content = [{"type": "text", "text": text}, part]
        elif log:
            log("✨ 文案: 缩略图读不出来, 这次只发文字线索")
    payload = {
        "model": str(sec.get("model") or "").strip(),
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": content}],
        "temperature": 0.9,      # 要的是文风不是准确性; 低温度会让每张图的话都长得一样
        "max_tokens": MAX_TOKENS,
    }
    reply, finish = _chat(payload, sec)
    title, mood = parse_reply(reply)
    if not (title or mood):
        raise AIError(_empty_reply_reason(finish, reply))
    if log:
        log(f"✨ 文案已生成: 标题「{title or '—'}」氛围「{mood or '—'}」"
            f"{' (附图)' if (vision and isinstance(content, list)) else ''}")
    return {"title": title, "mood": mood}


# ══════════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════════
def texts_for(info: "dict | None", cfg: "dict | None" = None) -> dict:
    """当前壁纸的两行文案 —— **只读缓存, 从不发起请求**。

    生成由设置页那颗「上传当前壁纸」按钮手动触发
    (:func:`generate_now`), 换壁纸**不再自动调用接口**。所以这里只回答「这张图生成过没有」:
      ``off``   还没配好 (enabled/地址/模型/密钥 缺一) —— 前端用本地兜底句
      ``miss``  配好了, 但这张还没生成过 —— 同样用本地兜底句
      ``ready`` 有货 (``title`` / ``mood``, 附 ``cached``)
    """
    sec = ai_section(cfg)
    if not ai_ready(sec):
        return {"state": "off"}
    if not file_key(info):
        return {"state": "off"}                # 没有当前壁纸, 无从谈起
    hit = cache_get(cache_key(info, sec))
    if hit:
        return {"state": "ready", "cached": True, **hit}
    return {"state": "miss"}


def sec_from_form(cfg: "dict | None" = None, form: "dict | None" = None,
                  *, vision: bool = True) -> dict:
    """把设置页这次填的值并到已存配置上 —— **表单值优先**, 缺的项从已存配置补。

    生成与「测试连接」共用这一套: 两条路要是各拼各的, 用户就会撞上「测试说连得上、
    上传说失败」(2026-09-27 的排障按钮就是为这个加的, 不能自己先漂开)。
    """
    sec = ai_section(cfg)
    for k in ("base_url", "model"):
        v = str((form or {}).get(k) or "").strip()
        if v:
            sec[k] = v
    key_in = str((form or {}).get("api_key") or "").strip()
    if key_in:
        sec["api_key"] = key_in
    sec["enabled"] = True                      # 点按钮即视为「要用」, 不再单设开关
    sec["vision"] = bool(vision)
    return sec


def generate_now(info: "dict | None", cfg: "dict | None" = None, *,
                 form: "dict | None" = None, log=None) -> dict:
    """「上传当前壁纸」按钮走这条路: **同步**生成一次并落缓存。

    同步等结果 (用户点了一下, 就是在等文案), 超时用 ``timeout``。调用方负责把成功后的
    配置回写 user.json (见 web/server.py 的 ``/api/ai/run``)。
    """
    sec = sec_from_form(cfg, form, vision=True)   # 按钮就叫「上传当前壁纸」→ 一定带图
    # 先验配置再验壁纸: 什么都没填时, 「先把三件套填上」比「没有壁纸」更接近用户要做的下一步
    if not ai_ready(sec):
        raise AIError("先填好接口地址、模型名和 API 密钥")
    if not file_key(info):
        raise AIError("当前没有壁纸, 先让桌面显示一张图片")
    out = generate(info, sec, log=log)
    cache_put(cache_key(info, sec), out.get("title", ""), out.get("mood", ""))
    return {"state": "ready", "cached": False, **out}


def test_connection(cfg: "dict | None" = None, *, form: "dict | None" = None,
                    log=None) -> dict:
    """「🔌 测试连接」按钮走这条路: 拿**最便宜的一次请求**验「地址 + 密钥 + 模型名」三件套。

    **不发图**、也不要求有当前壁纸 —— 它是排障按钮, 要的是快、便宜、随时能用。
    但输出预算跟生成用同一个 :data:`MAX_TOKENS`: 只验「连得上」是不够的, 这次事故恰恰是
    「地址密钥都对、模型把预算烧在 thinking 上」, 预算给小了就测不出来。
    """
    sec = sec_from_form(cfg, form, vision=False)
    if not ai_ready(sec):
        raise AIError("先填好接口地址、模型名和 API 密钥")
    t0 = time.monotonic()
    reply, finish = _chat({
        "model": str(sec.get("model") or "").strip(),
        "messages": [{"role": "user", "content": "回复两个字: 正常"}],
        "max_tokens": MAX_TOKENS,
    }, sec)
    ms = int((time.monotonic() - t0) * 1000)
    if not reply.strip():
        raise AIError(_empty_reply_reason(finish, reply))
    if log:
        log(f"✨ 文案接口连通: {sec.get('model')} ({ms}ms)")
    return {"model": str(sec.get("model") or "").strip(), "ms": ms,
            "reply": reply.strip()[:40]}


def state_of(info: "dict | None") -> str:
    """info 里那份 AI 结果的状态 (前端/小控件画什么全看它)。"""
    return str(((info or {}).get("ai") or {}).get("state") or "off")