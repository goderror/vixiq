# build.spec — PyInstaller 打包配置
# 用法:pyinstaller build.spec   (需先 pip install pyinstaller)
# 产物:dist/vixiq/  (整个文件夹拷走即用)
#
# 打包前准备:
#   1. 把 msedgedriver.exe 放到 resources/(从旧项目 msedgedriver-win64/ 复制)
#   2. 准备 resources/icon.ico(可选,没有就不打图标行)
#   3. 用 Python 3.11/3.12 的 venv,pip install -r requirements-dev.txt
#
# ⚠ 打包后必做(--noconfirm 会**整个删掉** dist/vixiq/):
#   dist/vixiq/config/user.json 是打包后人工补回的**用户配置**(web.port + F 盘 data/temp
#   重定向), 不在版本库里, 每跑一次 PyInstaller 都会被删掉。丢了之后 exe 会静默按 resources/default.json
#   启动 → 端口回落 7790、数据目录变成 dist/vixiq/data/(一个全新空库, 真实图库完全不可见)。
#   正确姿势: 构建**前**把 dist/vixiq/config/user.json 备份出去, 构建**后**复制回原位,
#             再 `Test-Path dist\vixiq\config\user.json` 确认, 最后才启动验证。

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.building.splash import Splash

# cv2 / numpy 在 PyInstaller 下默认 hook 偶有遗漏,显式收集数据与子模块
datas = []
datas += collect_data_files('cv2')
datas += collect_data_files('numpy')

# 随包只读资源(放到 exe 内 resources/ 下,运行期 utils.paths.resource() 读)
extra_resources = [
    ('resources/default.json', 'resources'),
    ('resources/xpaths.json', 'resources'),
    ('resources/cookies.example.txt', 'resources'),
]
# driver 与图标若存在则打包,缺失不报错(开发期方便)
import os as _os
for _src, _dst in [
    ('resources/msedgedriver.exe', 'resources'),
    ('resources/icon.ico', 'resources'),
]:
    if _os.path.exists(_src):
        extra_resources.append((_src, _dst))
datas += extra_resources

# Web 前端资源(必须打包! Web 版页面/脚本/样式,漏掉则 exe 的 / 返回 404 "not found")
# 运行期 web/server.py 用 Path(__file__).parent/"static" 定位 → 冻结后是 _internal/web/static
#
# ── 版权立绘 to-top*.png 的打包开关 ────────────────────────────────────────────
#   web/static/to-top.png 是**他人 pixiv 作品的派生图**(画师 示例画师 / pixiv 102593990
#   的 Q 版初音贴纸; 角色版权归 Crypton, 画作版权归画师), 只能个人自用,
#   **不得随软件公开分发**。所以它有两个出口, 都必须堵:
#     出口①版本库   → .gitignore 已排除(始终生效, 无需操作)
#     出口②打包产物 → 由下面这个开关控制
#
#   默认(不设环境变量) = 本地自用模式 → **包含**立绘, 日常双击 exe 能看到初音
#   VIXIQ_RELEASE=1   = 发布模式     → **排除**立绘, 产物可安全上传/公开分发
#                       排除后前端自动降级为 纯色圆 + 白箭头 (style.css #to-top.no-img)
#
#   ⚠ 发布前务必用发布模式重新打包, 并核对产物里没有 to-top*:
#       Get-ChildItem dist\vixiq\_internal\web\static
#
#   ⚠ 注意: 早先写成 ('web/static','web/static') 整个目录打包, 把 .gitignore 排掉的
#     文件又原样塞回 dist → 发布包一直带着这张图。.gitignore 只管 git, 不管 PyInstaller。
_RELEASE = _os.environ.get('VIXIQ_RELEASE') == '1'

_web_static = []
for _root, _dirs, _files in _os.walk('web/static'):
    for _f in _files:
        if _RELEASE and _f.lower().startswith('to-top'):
            continue                      # 发布模式: 版权立绘不进包
        _p = _os.path.join(_root, _f)
        _web_static.append((_p, _os.path.dirname(_p).replace('\\', '/')))
datas += _web_static

print("=" * 70)
if _RELEASE:
    print("[Vixiq] 发布模式 (VIXIQ_RELEASE=1)")
    print("        已排除版权立绘 to-top*.png → 产物可公开分发 ✓")
else:
    print("[Vixiq] 本地自用模式")
    print("        已包含版权立绘 to-top*.png (画师 示例画师 的 Q 版初音立绘)")
    print("        此产物【不可公开分发】。要发布请改用:")
    print("          $env:VIXIQ_RELEASE=1; pyinstaller build.spec --noconfirm")
print("=" * 70)

# Selenium 4.48 的 webdriver.Edge 等是懒加载 (函数内才 import selenium.webdriver.edge.webdriver),
# PyInstaller 静态分析会漏掉这些子模块 → 打包版一启动浏览器就报
# "No module named 'selenium.webdriver.edge.webdriver'"。必须显式 collect_submodules('selenium')。
hiddenimports = collect_submodules('cv2') + collect_submodules('numpy') + collect_submodules('selenium')
hiddenimports += ['PIL', 'PIL._tkinter_finder', 'tqdm']

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'PyQt5', 'PySide2', 'PySide6', 'tkinter.test', 'tests'],
    noarchive=False,
)

pyz = PYZ(a.pure)

# 启动画面 (PyInstaller 原生 Splash): exe 双击后由引导器**立刻**显示 (Python 还没启动
# 就能画出来), 覆盖解释器/重模块导入阶段的「无反馈」窗口。状态文字靠 pyi_splash
# IPC 动态更新 —— 全程不用 Python tkinter (用 Tkinter 非主线程画窗口会在销毁/GC 时
# 触发 tcl86t.dll abort, 进程无声消失, 详见 main.py _splash_text_anim 注释)。
splash = Splash(
    'resources/splash.png',
    binaries=a.binaries,
    datas=a.datas,
    always_on_top=True,
    text_pos=(90, 138),      # 文字区左下角 (启用文字: 不设则不显示任何文字)
    text_size=11,
    text_color='#9fb0c3',
    text_default='正在启动',
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    [],
    exclude_binaries=True,   # --onedir 模式:二进制放 COLLECT,exe 本身轻量
    name='vixiq',
    console=False,           # --windowed,无控制台
    icon='resources/icon.ico' if _os.path.exists('resources/icon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    splash.binaries,
    name='vixiq',
    strip=False,
    upx=False,
)
