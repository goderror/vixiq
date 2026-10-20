# utils/config.py
"""
配置加载:resources/default.json(只读底)深度合并 config/user.json(用户覆盖)。

default.json 在只读资源区(打包进 exe),user.json 在可写区(用户可改,gitignore)。
启动时 load_config() 读两者并合并;GUI 设置页改完调 save_user_config() 回写。
"""
import copy
import json

from utils import paths


# 启动器形态的用户可选项 (2026-09-24 · 托盘常驻): default.json 的 launcher 节、
# 本文件的兜底、web/server.py 的 /api/launcher 接口、utils/tray.py 的托盘行为
# 都读这一份, 免得四处键名各写一遍迟早漂。
LAUNCHER_DEFAULTS = {
    "tray_enabled": True,            # 双击 exe 后常驻右下角托盘
    "auto_open_browser": False,      # 启动时是否自动弹浏览器界面
    "wall_widget": False,            # 是否显示桌面壁纸小控件
    "notify_on_wall_change": False,  # 壁纸切换时是否弹提示气泡
    "exit_on_browser_close": False,  # 浏览器页面全关后是否自动退出
}

# ✨ 壁纸文案 AI (2026-09-27): OpenAI 兼容接口, 给当前壁纸生成「标题 + 一句氛围话」。
# 默认**全关** —— 开了才会往用户填的那个地址发请求, 密钥也是用户自己填。
# 详见 core/ai_text.py 顶部的设计说明 (为什么调用在服务端而不是浏览器侧)。
AI_DEFAULTS = {
    "enabled": False,
    "base_url": "https://api.deepseek.com/v1",   # 只改地址+模型就能换服务商 (OpenAI 兼容)
    "model": "deepseek-chat",
    "api_key": "",
    "vision": False,      # True = 连缩略图一起发 (识别内容更准, 也更贵)
    "timeout": 25,
}


def _deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base,递归合并嵌套 dict。"""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def _canonicalize(cfg: dict) -> dict:
    """
    全局兜底: 无论 default.json / user.json 怎么缺节, 返回的 cfg 永远至少包含:
      - cookies.cookies_filename (缺则从 paths.cookies_filename 搬)
      - cookies.use_dpapi / cookies.rotate_days / cookies.minimum_version
      - paths.output (缺则回退到 paths.img_subdir 再回退 'img')
      - paths.cookies_filename (缺则从 cookies.cookies_filename 搬, 双向一致)
      - paths.img_subdir / input_subdir / db_filename 等基础项
    避免。
    """
    # ---- 保证顶层节存在 ----
    for top_key in ('browser', 'crawler', 'reverse_search', 'local_search',
                    'paths', 'cookies', 'logging', 'web', 'safemode',
                    'pixiv_actions', 'activity_log', 'launcher', 'ai_text'):
        cfg.setdefault(top_key, {})

    # ---- pixiv_actions 账号写操作 (2026-09-18): 下载后自动收藏(♡)/公开或非公开 ----
    pa = cfg['pixiv_actions']
    # 默认开启 : 用户明确要求"点下载就自动收藏该作品", 保守默认会让
    # 他以为功能没做 —— 设置页有开关可随时关掉。
    pa.setdefault('auto_bookmark', True)       # 下载成功后自动收藏该作品
    pa.setdefault('bookmark_private', False)   # True = 非公开收藏 (只有自己可见)
    # 注: R18 作品在 _auto_bookmark_after_download 里**强制走非公开**, 不受本开关影响

    # ---- activity_log 实时活动日志 (2026-09-17): 打屏 + 落盘, 按大小滚动/保留 N 天 ----
    al = cfg['activity_log']
    al.setdefault('enabled', True)      # 总开关 (关掉则只打屏不落盘)
    al.setdefault('console', True)      # 是否同时在终端打屏
    al.setdefault('max_mb', 8)          # 单文件上限, 超了顺延 .2/.3…
    al.setdefault('keep_days', 7)       # 保留天数, 超期文件自动清

    paths = cfg['paths']
    cookies = cfg['cookies']

    # ---- paths / cookies 双向同步 cookies_filename ----
    if not cookies.get('cookies_filename') and paths.get('cookies_filename'):
        cookies['cookies_filename'] = paths['cookies_filename']
    if not paths.get('cookies_filename') and cookies.get('cookies_filename'):
        paths['cookies_filename'] = cookies['cookies_filename']
    # 终极兜底: 两侧都没, 写死 'cookies.bin'
    if not cookies.get('cookies_filename'):
        cookies['cookies_filename'] = 'cookies.bin'
        paths['cookies_filename'] = 'cookies.bin'

    # ---- cookies 其它安全默认 ----
    cookies.setdefault('use_dpapi', True)
    cookies.setdefault('rotate_days', -1)
    cookies.setdefault('minimum_version', 1)

    # ---- browser 基础默认 (P4a 后全程默认无头, 手动登录才开可见浏览器) ----
    browser = cfg['browser']
    browser.setdefault('headless', True)
    browser.setdefault('page_load_strategy', 'eager')
    browser.setdefault('implicit_wait', 3)
    browser.setdefault('explicit_wait_crawler', 10)
    browser.setdefault('use_chromium', True)
    # 页面加载硬超时(秒): 网络不通/代理挂掉时 driver.get 最多等这么久就报错,
    # 防止「搜索永远转圈」(P4a+ 二修)
    browser.setdefault('page_load_timeout', 45)

    # ---- reverse_search 反搜默认 (批次防拉黑: 每 batch_size 张休息 batch_break_sec 秒) ----
    rs = cfg['reverse_search']
    rs.setdefault('max_attempts', 6)
    rs.setdefault('retry_interval_sec', 1.0)
    rs.setdefault('between_images_delay_min', 5.0)
    rs.setdefault('between_images_delay_max', 8.0)
    rs.setdefault('compress_max_size', 600)
    rs.setdefault('max_error_count', 9)
    rs.setdefault('batch_size', 5)
    rs.setdefault('batch_break_sec', 30)

    # ---- paths 基础默认 ----
    paths.setdefault('img_subdir', 'img')
    paths.setdefault('input_subdir', 'input')
    paths.setdefault('failed_subdir', 'failed')
    paths.setdefault('logs_subdir', 'logs')
    paths.setdefault('temp_subdir', 'temp_upload')
    paths.setdefault('feature_db_subdir', 'feature_db')
    paths.setdefault('db_filename', 'pixiv.db')
    # 输出/数据重定向(用户强烈诉求: 大文件放 F 盘等非系统盘)
    # - data_root_override: 空串/None = 沿用旧逻辑(可写区下 data/); 填绝对路径= 整个 data/ (db/img/logs/cookies) 都放在该目录下
    #   例子: "D:/vixiq-data"
    # - temp_root_override: 空串/None = 沿用 data_root/temp (项目独立 temp, 不落系统盘); 填绝对路径= 所有 Selenium profile/Python 临时文件放该目录
    #   例子: "D:/vixiq-temp"
    # - temp_max_age_h: temp_root 下旧目录/文件超过 X 小时自动清理, 默认 24h
    # - temp_max_size_mb: temp_root 总容量超多少 MB 强制按时间从旧到新清理, 默认 5120MB (5GB)
    paths.setdefault('data_root_override', '')
    paths.setdefault('temp_root_override', '')
    try:
        v = int(paths.setdefault('temp_max_age_h', 24))
        paths['temp_max_age_h'] = max(1, v)
    except Exception:
        paths['temp_max_age_h'] = 24
    try:
        v = int(paths.setdefault('temp_max_size_mb', 5120))
        paths['temp_max_size_mb'] = max(256, v)
    except Exception:
        paths['temp_max_size_mb'] = 5120
    # output 图保存根目录: 与 P3 app.py download_cart() / crawler 一致
    if not paths.get('output'):
        paths['output'] = paths.get('img_subdir') or 'img'

    # ---- web 界面默认 (P5 · Web 版启动器, 替代 Tkinter gui/) ----
    web = cfg['web']
    web.setdefault('port', 7788)

    # ---- 启动器形态默认 (2026-09-24 · 托盘常驻) ----
    # 双击 exe 只驻右下角托盘, 不再强制弹浏览器; 退出唯一口径 = 托盘右键「退出 Vixiq」。
    # 用 setdefault 而非 cfg['launcher']: 老 user.json 里没这一节也不会 KeyError。
    ln = cfg.setdefault('launcher', {})
    for _k, _v in LAUNCHER_DEFAULTS.items():
        ln.setdefault(_k, _v)

    # ---- 壁纸文案 AI 默认 (2026-09-27 · OpenAI 兼容接口) ----
    ai = cfg.setdefault('ai_text', {})
    for _k, _v in AI_DEFAULTS.items():
        ai.setdefault(_k, _v)

    # ---- 安全模式默认 (R18 内容打码遮挡; 图库默认筛选非 R18 见前端 libFilters.r18='0') ----
    sm = cfg['safemode']
    sm.setdefault('enabled', False)
    sm.setdefault('blur_mode', True)   # True=模糊打码; 保留字段便于将来改「完全隐藏」
    return cfg


def load_config() -> dict:
    """读 default.json 作底,合并 user.json(若存在)。user.json 损坏则退回默认。"""
    default_path = paths.resource("default.json")
    with open(default_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    user_path = paths.user_config_path()
    if user_path.exists():
        try:
            # utf-8-sig 而非 utf-8: Windows 记事本/PowerShell 写出的 UTF-8 常带 BOM(EF BB BF),
            # 而 json.load 会把 BOM 当非法字符 → 抛 JSONDecodeError → 这里静默退回默认配置。
            # 后果极重: 端口回落、data_root_override 丢失 → 用户看到「cookie 没了、数据库不见了」
            # (2026-09-17 真实事故: dist/config/user.json 带 BOM, exe 跑成 7790 + 空库)。
            with open(user_path, "r", encoding="utf-8-sig") as f:
                user_cfg = json.load(f)
            cfg = _deep_merge(cfg, user_cfg)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] user.json 读取失败,使用默认配置: {e}")
    return _canonicalize(cfg)


def save_user_config(updates: dict) -> None:
    """把用户改动合并写回 user.json(保留已有键,深度合并)。"""
    user_path = paths.user_config_path()
    existing: dict = {}
    if user_path.exists():
        try:
            # 同 load_config: 容忍 BOM, 否则在设置页保存任何一项都会把带 BOM 的
            # user.json 读成 {} → F 盘重定向等全部覆盖项被静默抹掉。
            with open(user_path, "r", encoding="utf-8-sig") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}
    merged = _deep_merge(existing, updates)
    with open(user_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
