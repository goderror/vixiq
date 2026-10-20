# utils/widget_window.py
"""桌面壁纸小控件 (开关 ``launcher.wall_widget``, 默认关): 贴屏幕边框的三态分层窗口。

三态 (2026-09-24 规格):

* ``EDGE`` —— 平时收起来, 只在屏幕边框上留**一条边缘线**。线本身只有 4px, 太细停不上,
  所以窗口做到 11px 当命中区 (多出来的一大截全透明, 专门接鼠标)。
* ``TAG``  —— 鼠标移到边缘线上, 从边框「伸」出一张**长条便签** (204×72, 零圆角, 伸出端带
  **人字防滑纹路**): 圆角缩略图 + 上一张/播放/下一张三键 + 底部进度线。
* ``CARD`` —— 在便签上左键, 展开成完整窗口, 右上角有**小叉叉**。

交互闭环: 移开便签缩回边缘线 / 便签上三键点击 (上一张 / 播放幻灯片 / 下一张)、空白处左键展开 /
窗口里的**壁纸控制条** (同款四颗图标键: 上一张 / 播放 / 下一张 / 切换顺序·随机) /
小叉叉关窗口变回便签 / 右键**按住拖动**挪位置换边。鼠标指到可点击的键上会浮出键帽 +
accent 荧光 (accent 取自当前壁纸缩略图主色); 便签的键帽是**斜切**的, 卡片的是**微圆角**
。

**只在桌面露出来**: 光标前面挡着别的窗口时整块**全透明** —— 连边缘线都不画。
判定走:meth:`WallWidget._desktop_under` (光标底下那个窗口的类名爬上去是不是 ``Progman``/``WorkerW``)。
「便签的触发只应该在桌面, 否则平时浏览其他窗口也会跳出来」。
右键原地单击**不做事** (2026-09-25 起): 便签一悬停就伸出来, 太容易误点, 点一下就整块消失
还不容易找回; 退出唯一口径是托盘菜单 / ``vixiq.exe --quit`` (走 main.py ``_cleanup``)。

**为什么不用 Edge ``--app`` + ``wall.html``** (上阶段方案, 2026-09-24 作废): 浏览器窗口带
自己的边框, 做不出「贴边收成一条线」的形态, 也拿不到逐帧补间; 改用 ctypes 分层窗口
(``WS_EX_LAYERED`` + ``UpdateLayeredWindow``) 自己画, **零新依赖**。

**线程模型照抄** :mod:`utils.tray`: 窗口是线程亲和的, 分层窗口必须在跑消息循环的那个
线程里建 —— 本模块在 ``start`` 里起一个 daemon 线程 (``vixiq-widget``) 建窗 + 跑
``GetMessageW`` 循环, 与托盘各占一个线程。

要点 (踩过的坑, 别退回去):

* **鼠标按键用轮询而非 ``WM_*BUTTON*``**: ``WS_EX_NOACTIVATE`` 分层窗口的鼠标消息投递
  不稳, 而 16ms 定时器本来就在跑, 顺手读一次 ``GetAsyncKeyState`` 最稳。
* **补间**: 尺寸按 ease-out-quad 插值、位置仍由:func:`dock_geom` 贴边对齐、画面
  ``Image.blend`` 交叉淡化; 起点取「当前实际画着的那一帧」, 动画中途被打断也不跳。
* **右键拖动期间不切形状**: 一按就缩成便签会让窗口停在左上角, 看着像「拖不动」。
* **关窗口不动 anchor**: 小叉叉在卡片右上角, 拿它当新锚点会把便签拽到屏幕顶端。
* 描边/填色一律 ``alpha_composite``/带 mask 的 ``paste``, 不能用 ``paste`` 叠自定义形状
  —— 那是替换语义, 会把边缘 1px 的 alpha 换成描边色自身的 alpha, 轮廓出现发丝透明缝。
"""
from __future__ import annotations

import collections
import colorsys
import ctypes
import io
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

try:
    # 不做 DPI 感知的话, 125% 缩放下工作区读成 1536×864 而非 1920×1030,
    # GetCursorPos 也拿虚拟坐标 → 贴边会差出几十像素 (DEBUG_HISTORY L2523 同类教训)。
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_DPI_AWARE
except Exception:
    pass

# **必须用私有实例, 不能写 ``ctypes.windll.*``** (2026-09-25 修「右键点便签 → 托盘也没了」)。
# ``ctypes.windll`` 是**带缓存的** LibraryLoader —— ``ctypes.windll.user32 is ctypes.windll.user32``
# 为 True, 于是 ``argtypes``/``restype`` 是**进程级共享**的。而:mod:`utils.tray` 用的是它自己那套
# ``WNDCLASSW``(lpfnWndProc 是 c_void_p) 和带 ``lPrivate`` 的 ``MSG``, 与本模块的 ``WNDCLASS``/
# ``wintypes.MSG`` 类型不兼容。main.py 是「先起托盘, 再 ``from utils.widget_window import ...``」,
# 本模块 import 时钉下去的那几行签名就把运行中的托盘函数原型覆盖掉了 → 托盘线程在
# ``GetMessageW`` 上抛 ``ArgumentError: expected LP_MSG instance instead of pointer to MSG``
# 当场死掉, ``finally`` 里 ``NIM_DELETE`` 把图标摘了 —— 用户看到的就是「托盘也不见了」。
# ``ctypes.WinDLL("user32")`` 每次调用都新建实例 (``is`` 为 False), 原型各自独立, 互不干扰。
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
k32 = ctypes.WinDLL("kernel32")
try:
    winmm = ctypes.WinDLL("winmm")     # 定时器精度: 同样走私有实例, 不碰进程级共享原型
except Exception:                       # 无 winmm 的环境直接跳过 (只是动画节奏差一点)
    winmm = None

# ── Win32 常量 ──
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080      # 不在任务栏/Alt-Tab 留痕
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000      # 不抢焦点
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1
DIB_RGB_COLORS, BI_RGB = 0, 0
SWP_NOACTIVATE = 0x0010
SW_SHOWNOACTIVATE = 4
WM_DESTROY, WM_CLOSE, WM_TIMER = 0x0002, 0x0010, 0x0113
WM_APP = 0x8000
WM_REFRESH = WM_APP + 1            # 刷新线程投递: 壁纸数据变了, 重建卡片
WM_PAINTED = WM_APP + 2            # 渲染线程投递: 重活画完了, 窗口线程收货换缓存
SPI_GETWORKAREA = 0x0030
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02
IDC_ARROW = 32512

CLASS_NAME = "VixiqWallWidget"
THREAD_NAME = "vixiq-widget"
TID = 1
FRAME_MS = 16                      # 定时器周期 (≈60fps), 补间 + 轮询都靠它
DOCKS = ("left", "right", "top", "bottom")

# ── EDGE 平时收起来: 边缘线 ──
EDGE_W, EDGE_HIT, EDGE_LEN = 4, 11, 68
EDGE_COL, EDGE_A = (150, 158, 176), 235

# ── TAG 悬停伸出的便签: 横长条 + 伸出端防滑纹路  ──
TAG_LEN, TAG_THK = 204, 72          # 往画面里探 204px, 沿屏幕边 72px
TAG_GRIP = 16                       # 伸出端的**防滑纹路区**宽度 (外轮廓是完整长方形)
                                    # 沿革: 2026-09-25 曾是「燕尾/羊角」造型, 这里凹掉一个 V;
                                    # 2026-09-26 改为: 不是单纯改回长方形, 而是补回缺口,
                                    # 且在补回的那部分配合缺口箭头做成防滑纹路。
TAG_XOFF = TAG_GRIP + 6             # 内容右让: 伸出的纹路区得让出来, 别跟缩略图叠上
TAG_GRIP_STEP = 3                   # 人字防滑纹的间距 (越小越密)
TAG_GRIP_A = 64                     # 纹路 alpha —— 与长边刻度线同一档, 靠明度差而不是彩色
TAG_GRIP_EDGE_A = 104               # 原缺口那道「箭头」本身描亮一档, 让箭头读得出来
TAG_TS = 46                         # 便签里缩略图的边长 (圆角方图)
TAG_ISZ, TAG_IGAP = 22, 15          # 三个图标键的边长 / 键间距
TAG_HIT = 17                        # 键的命中半径 (半边长): 比键本身大一圈才好点
TAG_BAR_Y = TAG_THK - 9             # 底部进度线的中线
# 便签的荧光色:**从当前壁纸缩略图里取主色**再「荧光化」, 于是每张壁纸的便签颜色都不一样,
# 又永远是画面里本来就有的色, 不会跟壁纸打架。没缩略图时退回这支青色。
NEON_FALLBACK = (58, 220, 214)
TAG_SAT_MIN, TAG_VAL_MIN = 0.72, 0.98     # 荧光化下限: 饱和度/明度都拉到这个值以上

# ── CARD 展开的窗口 (2026-09-25 定案: 对角斜切 + 幽灵键; 2026-09-26 按钮改**微圆角**) ──
CARD_W, CARD_H = 548, 392          # 2026-09-28 四期**收窄**回 548: 「小窗口还是窄一些吧,
                                   # 标题可以滚动播放, 这样标题再长也不怕了」—— 既然标题改跑马灯,
                                   # 就不必再为「15 字要 390px」把窗口撑到 640; 收窄后信息区仍有
                                   # 340px (≈22 个 15px 汉字), 标题栏 328px 之外的文字靠滚动看全。
                                   # 沿革: 384 只够 8/12 字 (2026-09-27 初) → 512 → 548 (标题
                                   # 18→21px) → 640 (为 26px 的 15 字, 2026-09-28 三期) → 548 (本版)。
CARD_SS = 2                        # 卡片的绘制倍率 (面积是便签的 8 倍, 跟便签一样上 4× 太费)
CUT_CARD = {"tl": 32, "br": 32}     # 窗口 = **对角斜切** (左上 + 右下)
CUT_PH = {"tl": 16, "br": 16}       # 卡片里的缩略图跟着面板对角斜切
BADGE_RAD = 9                       # 标题徽章**圆角** (「显示标题那一栏的两边
                                    # 不要四角斜切, 而是圆弧」—— 面板/缩略图的对角斜切保留,
                                    # 那是「窗口的形状」; 只有标题这一栏改圆弧)
CUT_BTN = 0                         # 按钮不再斜切 (留着给老冒烟断言一个明确的「已作废」值)
BTN_RAD = 4                         # 按钮圆角: 微微圆边
X_SZ = 28
X_BOX = (CARD_W - 12 - X_SZ, 12, CARD_W - 12, 12 + X_SZ)     # (600, 12, 628, 40)

# 卡片内部的排版锚点 (缩略图/文字/控制条/按钮都引用它, 免得改一处尺寸就对不上)
C_PL, C_PT = 14, 20            # 左边距 / 上边距
C_TW, C_TH = 124, 160          # 缩略图
C_GAP = 60                     # 缩略图底 → 分隔线的留白 (放大字号后氛围句要换到第 3 行, 42 不够)
C_DIV = C_PT + C_TH + C_GAP    # 缩略图下的分隔线
MOOD_LINES = 3                 # 氛围句**最多换几行** (超了才在末行裁「…」; 标题始终一行)
# ── 文案排版 (方正舒体, 大字号, 加粗, 带阴影) ──
# 三档字号再提一档 (标题 21→26 / 说明行 14→15 / 氛围句 16→20), **行距同步 30→34** ——
# 只放大字号而不动行距, 会变成「字贴在一起」的拥挤感, 而不是「字大了」。
TITLE_SIZE, INFO_SIZE, MOOD_SIZE = 26, 15, 20
INFO_PITCH = 34                # 说明行 / 氛围句的行距 (px, 基准图坐标)
BADGE_H = 48                   # 标题徽章高度 (26px 的字塞进旧的 40px 会顶到框)
TRACK_LS = 0.4                 # 标题字距 (基准图 px)
# 标题徽章与**可视区** (跑马灯的窗口) —— 徽章不再随标题长度伸缩, 宽度定死, 长标题靠滚动看全。
BADGE_PAD = 12                 # 徽章内左右留白 (也是文字区到徽章边的距离)
BADGE_X0 = C_PL + C_TW + 18 - BADGE_PAD     # 144 (与下面几行文案的左边缘对齐)
BADGE_X1 = X_BOX[0] - BADGE_PAD             # 496 (给右上角裸线叉叉让出位置)
BADGE_W = BADGE_X1 - BADGE_X0               # 352
TITLE_W = BADGE_W - BADGE_PAD * 2           # 328 —— 标题可视区宽 (也是跑马灯窗口宽)
# ── 文案特效: 阴影 / **流转的霓虹流光** (2026-09-28: 要醒目, 带阴影,
# 以及流转的霓虹光 → 当天二期改用华文琥珀且不加粗) ──
# 合成加粗 (``stroke_width`` 描边) 随华文琥珀一起撤了 —— 琥珀体本身笔画就极重, 再描一圈会糊。
SHADOW_OFF = (2, 3)            # 文字阴影的偏移 (基准图 px, 右下) —— 光自左上来
SHADOW_BLUR = 1.4              # 阴影柔化半径 (基准图 px)
SHADOW_K = 140                 # 阴影浓度 (纯黑层的整体透明度)
# ── 霓虹流光 (2026-09-28 四期: 光要像沿着笔画描过去, 像金属的反光顺着型面滑) ──
# 前三期都错了, 记下来免得再绕回去: 二期是**一条水平亮带横扫**(所有字在同一 x 同时亮) —— 那是
# 「扫光」; 三期是**斑点场乘蒙版**(亮的是一整片区域, 而且整片在来回移动) —— 那是「雾」。
# 目标是光**顺着每一笔的线条挪**, 像金属型面上那道反光顺着型面滑。四期的做法:
#   ① 把文案蒙版**细化成 1px 骨架**(Zhang-Suen);
#   ② 在骨架上按**测地距离**建一张「沿笔画走的路程场」D —— 像素离「起点」沿笔画有多少 px;
#   ③ 每帧只让几道**波前**在这条路上往前走, 亮的位置天生只落在笔画上。
# 于是光既不是一条横扫的带、也不是一团挪动的斑, 而是**沿着笔画一段段地被描亮**。
# 2026-09-28 「波动太密集和频繁了」→ 四道减到**两道**、每道走得更慢、呼吸也更缓; 亮带同时
# 加宽, 少两道也不会稀疏。密度由 NEON_SRC 与 NEON_WAVE 定, 频率由 NEON_SPEED 与 NEON_FADE 定。
NEON_SRC = 2                   # 同时跑几道波前 (速度/相位各不相同 → 不整齐、不成闭环)
NEON_WAVE = 12                 # 波前亮带的半宽 (px) —— 道数少了, 亮带相应加宽才不显稀
NEON_SPEED = (1.35, 1.85)              # 各道波前每拍前进的 px (彼此不可公约 → 永不同步)
NEON_FADE = (0.042, 0.029)             # 各道波前的明暗呼吸角速度 (拍⁻¹) —— 越小闪得越慢
NEON_OFF1 = (0.0, 233.0)               # 各道波前的初始路程偏移 (px) —— 别一起从起点出发
NEON_OFF2 = (0.0, 2.4)                 # 各道波前呼吸相位的初相
NEON_EVERY = 2                 # 每几个 tick 推进一拍 (16ms 定时器 → 2 拍 ≈ 30fps)
NEON_PAD = 36                  # 流光/阴影只算文案外接框外扩这么多 (成品 px)。要容下 glow 的
                               # 光晕 (3×NEON_GLOW_BLUR = 24px), 少算了光晕会被裁成一条硬边
NEON_GLOW_BLUR = 6             # 波前柔光半径 (成品 px)。光晕要漫到字**外面**一圈, 不然只是
                               # 「字亮了」; 但太大就糊成一团光雾, 看不出是**哪几笔**在亮
NEON_HALO_GAIN = 2             # 光晕蒙版的增益 —— blur 会把细笔画的蒙版摊薄, 不放大一档,
                               # 字周围那圈光晕淡到看不见
NEON_GLOW_K = 200              # 波前柔光强度 (accent 色)
NEON_CORE_K = 215              # 波前核心强度 (近白) —— 描到哪几笔, 哪几笔就整段提亮
# **标题跑马灯** (2026-09-28 四期: 标题滚动播放, 再长也不怕)
TITLE_GAP = 64                 # 跑马灯两遍标题之间的间隔 (px) —— 太短会读成两句话黏在一起
TITLE_FIT = 18                 # 短标题 (不滚) 裁窗时两侧留给辉光/阴影的余量 (px)。必须
                               # **小于 TITLE_GAP**, 否则窗口右端会越过第二遍标题的起点
TITLE_SCROLL = 2.4             # 滚动速度 (px / 拍 → ≈72px/s)
TITLE_MAX = 1800               # 标题条最长只做到这么宽 (px) —— 极端长的标题先裁「…」再滚
# **壁纸控制条**: 上一张 / 播放 / 下一张 / 切换顺序 —— 四颗**图标键**, 与便签同一套手感。
# 不拆成两个按钮, 播放键与便签一样是图标, 切换播放顺序的档位放在同一排。
# 四颗在卡片里**居中均分**, 不随窗口宽度变化跑偏。
ICON_N = 4                     # 控制条固定四颗: 上一张/播放/下一张/档位
IBAR_CY = C_DIV + 32           # 键心中线
IBAR_SZ, IBAR_GAP = 24, 16     # 图标外接尺寸 / 相邻键帽的净距
IBAR_KEY = 1.5                 # 键帽边长 = 图标 × 这个 (与便签 icon_btn 的 k = s*1.5 一致)
IBAR_RAD = 5                   # 键帽微圆角 —— 与文字键同一档「一点点圆边」
BTN_TOP = IBAR_CY + 36         # 文字键第一行
BTN_COLS, BTN_GAP, BTN_H, BTN_ROW_H = 2, 10, 28, 36
BTN_ROWS = 2                   # 最多几行 (在库中查看/所在文件夹/移出壁纸 = 2 行)
BTN_W = (CARD_W - C_PL * 2 - BTN_GAP) // BTN_COLS

# ── 设计 token (2026-09-25 定案: 深色磨砂 / 纯白细线 / 发丝描边 / 零霓虹) ──
# 彩色只来自壁纸缩略图本身 (以及由它派生的 accent), chrome 全部灰阶 —— 层次只靠明度差。
GLASS = (16, 17, 21)               # 玻璃板底色
GA_TOP, GA_BOT = 210, 194          # 板子上下 alpha: 只差 16, 有体积但不「渐变味」
HAIR = 42                          # 发丝描边 alpha (上亮下暗, 见 hair_ramp)
ICON = (246, 247, 249)             # 图标主色 (近白)
DIM = (255, 255, 255, 178)         # 次级文字
DIM2 = (255, 255, 255, 202)        # 三级文字 (氛围句): 2026-09-27 换文案体 + 加辉光后**提亮** ——
                                   # 16px 的主角文案再压到 142, 会比它上面那两行 14px 说明还淡
TRACK = 46                         # 进度线底槽 alpha
FILL = (255, 255, 255, 235)        # 进度线填充 (参考图就是纯白)
ICON_W = 1.7                       # 图标线宽 (1x px)
TICK_A, CHAMFER_A = 64, 104        # 长边刻度线 / 斜边亮线的 alpha (科技纹路全靠明度差)
SLIDE_SEC = 30.0                   # 幻灯片间隔 (Windows 没有公开的「走下一张」接口, 见 _slide)

DRAG_SLOP = 6      # 右键位移小于这个值算「只是点了一下」(不挪位置, 也不退出), 大于算拖动
TAG_PAD = 6        # 便签的收缩判定外扩, 免得贴边时疯狂伸缩
DRAG_PAD = 4       # 右键抓取窗口的外扩
ANIM_MS = 240      # 状态切换的补间时长; 定时器 16ms → 约 15 帧 (2026-09-26 为更顺滑调定)
REFRESH_SEC = 8.0   # 壁纸换了的跟上间隔 (本地 HTTP, 很便宜; 2026-09-29 30→8: 文案生成完
                    # 别让用户干等半分钟才在卡片上看到, 服务端另有成功即推的主动通知)
CONFIRM_SEC = 5.0   # 「移出壁纸」两步确认的有效期


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                              wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


# x64 下 ctypes 默认 restype 是 c_int, 会把句柄截成 32 位 —— 必须逐个钉死。
# (托盘那边同样的写法; 漏一个的后果是「静默失败」, 只表现为窗口没出来)
k32.GetModuleHandleW.restype = wintypes.HMODULE
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_uint
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_uint]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), ctypes.c_uint32,
    ctypes.POINTER(BLENDFUNCTION), ctypes.c_uint32]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
# 消息循环相关的签名也必须钉死, 否则 LPARAM 超 2^31 时 ctypes 会 OverflowError
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               ctypes.c_uint, ctypes.c_uint]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, ctypes.c_uint]
# 「鼠标底下是不是桌面」用 (见 WallWidget._desktop_under)。x64 下句柄必须钉 restype,
# 否则默认 c_int 会把 HWND 截成 32 位。
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [POINT]
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

# 「命中自己时跳过自己往下找」用 (见 top_window_under)。EnumWindows 是按 **z 序从顶到底**
# 回调的, 所以回调里遇到的第一个包含该点的窗口就是"真正露在最上面"的那个。
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.PtInRect.restype = wintypes.BOOL
user32.PtInRect.argtypes = [ctypes.POINTER(RECT), POINT]

# 被 DWM 隐藏 (cloaked) 的窗口 —— 别的虚拟桌面上的、或"已显示但未激活"的 UWP —— 没占着
# 这块像素, 不能拿来当"挡在前面的窗口"。dwmapi 拿不到就当"都不隐藏"。
DWMWA_CLOAKED = 14
try:
    _dwmapi = ctypes.WinDLL("dwmapi")
    _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    _dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]
except OSError:                                    # 理论上 Win8+ 都有, 缺了也不该炸
    _dwmapi = None

# 桌面窗口的类名: 没有图标/有图标分别落在 Progman 与 WorkerW 上, 桌面上按右键拿到的是
# 两者的子窗口 (SysListView32 之类), 所以判定要**往上爬**而不是只看最底那层。
DESKTOP_CLASSES = ("Progman", "WorkerW")


# ══════════════════════════════════════════════════════════════════════
#  数据 (走本地 HTTP, 与网页「当前壁纸」卡片同一份结论)
# ══════════════════════════════════════════════════════════════════════
def fetch_info(port: int) -> "dict | None":
    """当前壁纸溯源结论; 服务端没起来就返回 None (小控件画占位图)。

    **不请求 deep** (ORB 兜底): 卡片上那颗「图像溯源」已撤 ,
    小控件是每 8 秒轮询的, 让它去跑可能要几十秒的整库特征检索毫无道理 —— 深溯源留给网页版。
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}/api/wallpaper/current", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def fetch_thumb(port: int, info: "dict | None") -> "Image.Image | None":
    """当前壁纸缩略图 (服务端按原图裁好返回, 省得小控件拉 7MB 大图)。"""
    if not info or not info.get("active"):
        return None
    f = info.get("file") or {}
    if not f.get("name"):
        return None
    root = "img" if f.get("in_library") else "wall"
    url = (f"http://127.0.0.1:{int(port)}/api/local-img?p="
           + urllib.parse.quote(f["name"]) + f"&root={root}&thumb=1")
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGBA")
    except Exception:
        return None


def info_key(info: "dict | None") -> tuple:
    """判断「壁纸换了没」的键 (纯函数, 便于冒烟)。

    第三个元素是**壁纸夹可用不可用**: 当前壁纸读不到时, 卡片只剩「播放 + 档位」两个键,
    而这两个键出不出现全看壁纸夹 —— 不带进来的话, 用户在设置里配好壁纸夹, 小控件的 30s
    轮询会因为「文件名没变」把这份新数据整个丢掉, 卡片仍旧空着。

    第四项是 **AI 文案签名** (``ai_sig``): 文案是异步生成的, 到货时文件名并没有变 ——
    不带进来, 那句就永远刷不到卡片上。
    """
    if not info:
        return ("", False, False, ai_sig(None))
    fo = info.get("folder") or {}
    return (str((info.get("file") or {}).get("name") or ""), bool(info.get("active")),
            bool(fo.get("ready")), ai_sig(info))


def folder_frac(info: "dict | None") -> float:
    """当前壁纸在壁纸夹里的位置 (0~1) —— 便签底部那条进度线填到哪。

    注册表只写「现在播的是哪张」, 没有「第几张 / 共几张」; 服务端按目录自然排序算出
    ``folder: {index, total}``。取不到就回 0 (只画空槽), 不做假进度。
    """
    fo = (info or {}).get("folder") or {}
    try:
        i, n = int(fo.get("index", -1)), int(fo.get("total", 0))
    except (TypeError, ValueError):
        return 0.0
    return (i + 1) / n if n > 0 and i >= 0 else 0.0


# ══════════════════════════════════════════════════════════════════════
#  绘制
# ══════════════════════════════════════════════════════════════════════
SS = 4          # 超采样倍数: 斜边/文字的锯齿都在这里消掉

_FONTS: dict = {}

# 文案字体栈 (2026-09-28 二期): **华文琥珀** —— 两轮对比后定下的一支 (先试方正舒体,
# 再看效果改为华文琥珀且不加粗): 琥珀体笔画本身就是一大块实心的圆头粗笔,
# 自带「醒目」, 不必再描边加粗 (描一圈在这么重的字上只会糊成一坨)。本机缺了就往后退
# (方正舒体 → 幼圆 → 楷体 → 雅黑兜底)。只给「文案」用 (标题 + 氛围句): 出处/尺寸/按钮这些
# **说明与控件**仍走雅黑 —— 一屏小字全换成展示体会读着发飘, 展示体 + UI 体两种字才是常规搭配。
COPY_FONTS = ("STHUPO.TTF", "FZSTK.TTF", "SIMYOU.TTF", "simkai.ttf", "msyh.ttc")
_FONT_DIR = "C:/Windows/Fonts/"
_COPY_FONT: "str | None" = None


def _copy_font_path() -> str:
    """文案字体栈里第一个**真装在本机**的字体文件 (一个都没有就回雅黑)。"""
    for name in COPY_FONTS:
        if os.path.isfile(_FONT_DIR + name):
            return _FONT_DIR + name
    return _FONT_DIR + "msyh.ttc"


def font(size: int, bold: bool = False, light: bool = False, copy: bool = False):
    """字体。正文字重分三档 (``bold`` 标题 / 常规 / ``light`` 次级说明)—— 「光靠字号分层次」
    是不够的, 参考图里标题和副文案的字重差得很明显。

    ``copy=True`` 走**文案字体栈** (:data:`COPY_FONTS`), 只有标题与氛围句用它; 展示体没有
    bold/light 变体, 这两档在文案体上自然失效 —— 层次由**字号与徽章**承担, 展示体本身的
    笔画重量就够「醒目」, 不另做合成加粗。
    """
    global _COPY_FONT
    key = (size, bold, light, copy)
    if key not in _FONTS:
        if copy:
            if _COPY_FONT is None:
                _COPY_FONT = _copy_font_path()
            path = _COPY_FONT
        else:
            path = _FONT_DIR + ("msyhbd.ttc" if bold else
                                ("msyhl.ttc" if light else "msyh.ttc"))
        try:
            _FONTS[key] = ImageFont.truetype(path, size)
        except OSError:
            _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]


def _ease(p: float) -> float:
    """ease-out-quad: 起步轻、收尾稳。

    2026-09-26 便签弹出要更顺滑 —— 原来是 ease-out-**cubic**, 第一帧就走完
    65%、第二帧 87% (定时器 16ms 一帧), 看着是「啪」地弹出来而不是「伸」出来; 二次方第一帧
    只走 14%, 起步明显更柔和, 收尾也仍是减速的。
    """
    p = 0.0 if p < 0 else (1.0 if p > 1 else p)
    return p * (2 - p)


def _fit(text: str, size: int, maxw: int, bold: bool = False,
         copy: bool = False) -> str:
    """按**像素宽**裁断加省略号 —— 卡片尺寸一变, 靠 [:N] 数字符数就会溢出到框外。

    ``copy=True`` 时按**文案字体**量宽度 (幼圆与雅黑的字面宽不同, 用错字体量出来的
    「塞得下」是假象)。
    """
    fnt = font(size, bold, copy=copy)
    if fnt.getlength(text) <= maxw:
        return text
    for n in range(len(text) - 1, 0, -1):
        if fnt.getlength(text[:n] + "…") <= maxw:
            return text[:n] + "…"
    return "…"


def mood_wrap(text: str, maxw: int, size: int,
              maxlines: int = MOOD_LINES) -> "list[str]":
    """氛围句按**像素宽换行** —— 标题不换行 (仍走:func:`_fit` 裁「…」), 只有这一行换。

。氛围句放到 30 字 ≈ 390px,
    单行怎么加宽都塞不下, 换行比砍掉半句好看。**超过 maxlines 才**在末行裁「…」, 所以
    行数不够时是「看不见」而不是「溢出到框外」。
    """
    fnt = font(size, copy=True)                   # 氛围句走文案字体, 别用 _fit (它按裁断算)
    rest = str(text or "").strip()
    if not rest:
        return []
    lines: list = []
    while rest:
        last = len(lines) == maxlines - 1
        if last or fnt.getlength(rest) <= maxw:
            if fnt.getlength(rest) > maxw:        # 末行还放不下 → 老实裁省略号
                for n in range(len(rest) - 1, 0, -1):
                    if fnt.getlength(rest[:n] + "…") <= maxw:
                        rest = rest[:n] + "…"
                        break
                else:
                    rest = "…"
            lines.append(rest)
            break
        n = len(rest)                             # 贪婪取「放得下的最长前缀」
        while n > 1 and fnt.getlength(rest[:n]) > maxw:
            n -= 1
        cut = rest[:n]
        # 拉丁文按**词**断, 别把单词啃成两半 (中文整句无空格, 这条自然不生效)
        if n < len(rest) and rest[n] != " ":
            sp = cut.rfind(" ")
            if sp > 0:
                cut = cut[:sp]
        lines.append(cut)
        rest = rest[len(cut):].lstrip()
    return lines


def chamfer_pts(w: int, h: int, cuts: dict, ss: int = 1) -> list:
    """**斜切矩形**的顶点环。``cuts`` = 每个角切掉多少 (1x px), 缺省 0 = 直角。

    面板 / 缩略图 / 按钮 / 标题徽章**共用这一套几何** —— 「科技化的纹路」基本就靠斜切 +
    沿边的刻度线 + 斜边亮线, 不靠颜色 (「整个边框和按钮的边框都要有
    科技化的纹路」)。
    """
    tl, tr, br, bl = (int(cuts.get(k, 0)) * ss for k in ("tl", "tr", "br", "bl"))
    return [(tl, 0), (w - tr, 0), (w, tr), (w, h - br), (w - br, h),
            (bl, h), (0, h - bl), (0, tl)]


def tag_pts(w: int, h: int, ss: int = 1) -> list:
    """便签的顶点环 —— **纯长方形** (``ss`` 尺度)。基准图里 **左 = 伸出端, 右 = 贴屏幕边**。

    2026-09-25 这里是燕尾 (伸出端凹掉一个 V); 2026-09-26 改为: 不是单纯改回长方形,
    而是**补回缺口**, 且在补回的那部分**配合缺口箭头做成防滑纹路**。
    于是外轮廓回到完整矩形, 原来那个 V 改由 :func:`grip_arrow` 画成人字防滑纹
    (斜率与原内缘完全一致, 所以读起来仍是同一个箭头, 只是不再是缺口)。

    四个朝向都是这张基准图旋转出来的, 所以这里只做「左端」这一件事。
    """
    return [(0, 0), (w, 0), (w, h), (0, h)]


def grip_arrow(d, h: int, ss: int = 1) -> None:
    """伸出端的**防滑纹路** —— 一组嵌套的人字纹 (V), 张口朝外、顶点落在原缺口的位置。

    「配合缺口箭头」= 每道人字纹的斜率跟原来燕尾的内缘**一模一样** (``(h/2)/TAG_GRIP``),
    只是从"把纸咬掉一块"改成"刻在纸面上的防滑槽" (想成相机手柄上那种 grip)。
    最外那道 (k=0) 就是原缺口本身, 提亮一档好让「箭头」读得出来。

    ``d`` 是便签的**超采样**画布, 所以坐标一律乘 ``ss``; 画在 ink 层上再整体合成,
    免得 ImageDraw 的替换语义把底下的壁纸晕染打出洞。
    """
    G = TAG_GRIP * ss
    if G <= 0:
        return
    mid = h / 2.0
    slope = mid / float(G)                    # 与燕尾内缘同一斜率: 36/16
    step = max(1, TAG_GRIP_STEP * ss)
    lw = max(1, ss)
    for k in range(G // step + 1):
        ax = G - k * step
        if ax <= 0:
            break
        a = TAG_GRIP_EDGE_A if k == 0 else TAG_GRIP_A
        d.line([(0, mid - slope * ax), (ax, mid), (0, mid + slope * ax)],
               fill=(255, 255, 255, a), width=lw, joint="curve")


def diag_segs(pts: list) -> list:
    """多边形里所有**斜边** (既非水平也非垂直) —— 斜切的角、人字纹的内缘都要描亮才看得见。"""
    out = []
    for i in range(len(pts)):
        (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
        if x0 != x1 and y0 != y1:
            out.append(((x0, y0), (x1, y1)))
    return out


def poly_mask(size, pts: list) -> "Image.Image":
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    return m


def chamfered_pts_box(box, cut: float) -> list:
    """斜切矩形 (按钮/键帽) 的顶点环; ``box`` 允许浮点。"""
    x0, y0, x1, y1 = box
    return [(x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut), (x1, y1 - cut),
            (x1 - cut, y1), (x0 + cut, y1), (x0, y1 - cut), (x0, y0 + cut)]


def box_segs(box, cut: float) -> list:
    """键面四条斜边 (画亮线用)。"""
    p = chamfered_pts_box(box, cut)
    return [(p[7], p[0]), (p[1], p[2]), (p[3], p[4]), (p[5], p[6])]


def chamfered_box(d, box, cut: float, *, fill=None, outline=None, width=1) -> None:
    pts = chamfered_pts_box(box, cut)
    if fill is not None:
        d.polygon(pts, fill=fill)
    if outline is not None:
        d.line(pts + [pts[0]], fill=outline, width=max(1, int(width)), joint="curve")


def relayer(size, alpha_map, color=(255, 255, 255), k=255) -> "Image.Image":
    """L 蒙版 → 单色 RGBA 层, 整体再乘一个透明度 ``k``。装饰件一律走它合成到画布上。"""
    lay = Image.new("RGBA", size, tuple(color[:3]) + (0,))
    lay.putalpha(alpha_map.point(lambda v: max(0, v) * k // 255))
    return lay


def hair_ramp(size, *, top=255, bot=96) -> "Image.Image":
    """竖向 alpha 斜坡 —— 描边「上亮下暗」, 光自上方来。现代 UI 的体积感基本都靠这一条。"""
    w, h = size
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=int(top + (bot - top) * y / max(1, h - 1)))
    return m


def rline(d, p0, p1, w, fill) -> None:
    """带圆头的细线。PIL 不给线帽, 端点画个同宽小圆补上 —— 图标全是圆头的才有那手感。"""
    d.line([p0, p1], fill=fill, width=max(1, int(w)))
    r = w / 2
    for x, y in (p0, p1):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def hollow_tri(d, pts: list, w, fill) -> None:
    """空心三角形: 三条圆头细线闭合 (``polygon`` 的 ``width`` 各版本 Pillow 支持不一)。"""
    for i in range(3):
        rline(d, pts[i], pts[(i + 1) % 3], w, fill)


def text_ls(d, x, y, s: str, fnt, fill, *, ls=0.0, center=False) -> None:
    """带字距的文本 (PIL 不支持 letter-spacing, 逐字符画)。``y`` = 行的垂直中心。"""
    total = sum(d.textlength(ch, font=fnt) for ch in s) + ls * max(0, len(s) - 1)
    cx = x - total / 2 if center else x
    for ch in s:
        d.text((cx, y), ch, font=fnt, fill=fill, anchor="lm")
        cx += d.textlength(ch, font=fnt) + ls


def text_mask(size, x, y, s: str, fnt, *, ls=0.0, center=False) -> "Image.Image":
    """把:func:`text_ls` 那串字渲染成 **L 蒙版** —— 辉光、阴影与渐变填充都从**同一个蒙版**
    派生, 免得同一串字画三遍、各自还算出不一样的坐标。"""
    m = Image.new("L", size, 0)
    text_ls(ImageDraw.Draw(m), x, y, s, fnt, 255, ls=ls, center=center)
    return m


def vgrad(size, top, bot) -> "Image.Image":
    """竖向线性渐变 (上 ``top`` → 下 ``bot``) 的 RGBA 层, 套:meth:`~PIL.Image.Image.putalpha`
    当文字填充色。

    只算一列像素再横向拉满 —— 渐变与 x 无关, 逐像素铺满整张画布是白费 (卡片画布是
    ``CARD_SS`` 倍的 1096×744)。
    """
    w, h = size
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        col.putpixel((0, y), tuple(int(round(top[c] + (bot[c] - top[c]) * t))
                                   for c in range(3)))
    return col.resize((w, h), Image.BILINEAR).convert("RGBA")


def tint_light(rgb, k: float = 0.55) -> tuple:
    """把颜色往白里提 (``k`` = 混白比例)。渐变字的下端用它 —— 直接拿 accent 当字色会整行
    跑成彩色, 提到接近白才既有色相又不失可读。"""
    return tuple(int(round(c + (255 - c) * k)) for c in tuple(rgb)[:3])


def inv_ramp(size, a0: int, a1: int) -> "Image.Image":
    """竖向 alpha 斜坡 (上 ``a0`` → 下 ``a1``)。玻璃板与进度线的底槽都用它。"""
    w, h = size
    m = Image.new("L", size)
    d = ImageDraw.Draw(m)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=int(a0 + (a1 - a0) * y / max(1, h - 1)))
    return m


def ink_layer(size):
    """装饰层。

    **必须单独开一层再 ``alpha_composite``** —— 直接在 RGBA 画布上 ``ImageDraw`` 是
    **替换语义**不是叠加: ``fill=(255,255,255,12)`` 会把玻璃板的 alpha 改写成 12,
    等于在板上打了个洞, 露出后面亮壁纸 → 看着就是「发白方块 + 黑字」(2026-09-25 踩到)。
    在透明层上写一次再把整层合上去, 才是真正的半透明叠加。
    """
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def wall_back(thumb: "Image.Image | None", w: int, h: int, ss: int, *,
              where: str = "edge", blur: float = 3.0, dim: float = 0.44):
    """壁纸**晕染**底: 大模糊 + 压暗。

    分层窗口没有 ``backdrop-filter``, 「玻璃后面透出来的壁纸」只能**预渲染**。关键是
    **采样控件真正压住的那块壁纸** —— 面板把它身后同一片内容磨砂掉, 边缘与桌面自然接上,
    像贴上去的毛玻璃; 按整张缩略图取平均就会发脏。

    小控件手里只有服务端给的 480px 缩略图(同一张画), 所以按「控件占 1920×1080 的比例」
    在缩略图里取对应的一块: ``where='edge'`` 取右边缘垂直居中 (便签贴边),
    ``'center'`` 取画面中央 (卡片)。``dim`` = 往暗色里混多少 —— 别太大, 大了退回死黑
    (过强会退回死黑)。没缩略图 → None, 调用方退回素色玻璃。
    """
    if thumb is None:
        return None
    tw, th = thumb.size
    bw = max(1, min(tw, int(round(tw * w / 1920.0))))
    bh = max(1, min(th, int(round(th * h / 1080.0))))
    if where == "edge":
        x0, y0 = tw - bw, max(0, (th - bh) // 2)
    else:
        x0, y0 = max(0, (tw - bw) // 2), max(0, (th - bh) // 2)
    im = thumb.convert("RGB").crop((x0, y0, x0 + bw, y0 + bh))
    im = im.resize((w * ss, h * ss), Image.LANCZOS)
    im = im.filter(ImageFilter.GaussianBlur(blur * ss))
    return Image.blend(im, Image.new("RGB", im.size, (14, 15, 19)), dim)


def grain(size) -> "Image.Image":
    """纸质颗粒: 极轻的白噪点。纸条质感靠它 —— 只要颗粒, 不要纤维/扫描线那种具象纹理。"""
    n = Image.effect_noise(size, 8).convert("L").point(
        lambda v: min(255, max(0, abs(v - 128) - 12) * 7))
    lay = Image.new("RGBA", size, (255, 255, 255, 0))
    lay.putalpha(n)
    return lay


def glass(w: int, h: int, *, pts: list, back=None, ss: int = SS, hair: int = HAIR,
          paper: bool = False, ticks: "str | None" = None, tick_alpha: int = TICK_A,
          chamfer_alpha: int = CHAMFER_A, shadow: float = 7) -> "Image.Image":
    """控件底: 壁纸晕染 (``back``) 或素色玻璃 + **上亮下暗**的发丝描边 + 斜边亮线 +
    沿长边的刻度线 (科技纹路, 全靠明度差) + 可选纸质颗粒 + 一圈很淡的落地阴影。

    **返回图严格等于 w*ss × h*ss** —— 阴影超出形状的部分会被裁掉。控件必须与窗口矩形
    同尺寸: 带上一圈 padding 的话, 贴边对齐、命中判定、小叉叉/按钮的坐标全都要各自
    减一遍偏移, 迟早错位成「点左边点到了右边」。
    """
    W, H = w * ss, h * ss
    if back is not None:
        body = back.convert("RGB").resize((W, H), Image.LANCZOS).convert("RGBA")
        body.putalpha(inv_ramp((W, H), GA_TOP, GA_BOT))
    else:
        body = Image.new("RGBA", (W, H), GLASS + (255,))
        body.putalpha(inv_ramp((W, H), GA_TOP, GA_BOT))
    if paper:
        body = Image.alpha_composite(body, grain((W, H)))
    core = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    core.paste(body, (0, 0), poly_mask((W, H), pts))
    # 发丝描边「上亮下暗」: 就算 alpha 只有 42, 也看得出来板子的边界在哪
    ol = Image.new("L", (W, H), 0)
    ImageDraw.Draw(ol).polygon(pts, outline=255, width=max(1, ss // 2))
    a = ImageChops.multiply(ol, hair_ramp((W, H))).point(lambda v: v * hair // 255)
    core = Image.alpha_composite(core, relayer((W, H), a))
    # 科技纹路 ①: 斜边亮线 —— 没有它, 深色底上的斜切/燕尾内缘等于没做
    if chamfer_alpha:
        mk = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mk)
        for p0, p1 in diag_segs(pts):
            md.line([p0, p1], fill=255, width=max(1, ss // 2))
        core = Image.alpha_composite(core, relayer((W, H), mk, k=chamfer_alpha))
    # 科技纹路 ②: 沿**长边**的刻度线 (像 HUD 的量尺)。'y' = 竖板走左右长边, 'x' = 横板走上下长边
    if ticks in ("x", "y"):
        tk = Image.new("L", (W, H), 0)
        td = ImageDraw.Draw(tk)
        step, ln, inset = (26, 5, 6) if ticks == "x" else (24, 5, 7)
        step, ln, inset = step * ss, ln * ss, inset * ss
        span = (W if ticks == "x" else H) - 4 * ss
        for v in range(step, span, step):
            if ticks == "x":
                td.line([(v, inset), (v, inset + ln)], fill=255, width=max(1, ss // 2))
                td.line([(v, H - inset), (v, H - inset - ln)], fill=255, width=max(1, ss // 2))
            else:
                td.line([(inset, v), (inset + ln, v)], fill=255, width=max(1, ss // 2))
                td.line([(W - inset, v), (W - inset - ln, v)], fill=255, width=max(1, ss // 2))
        tk = ImageChops.multiply(tk, poly_mask((W, H), pts))   # 刻度不能画到板子外面去
        core = Image.alpha_composite(core, relayer((W, H), tk, k=tick_alpha))
    if not shadow:
        return core
    pad = int(shadow * ss * 1.2)
    canvas = Image.new("RGBA", (W + pad * 2, H + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon(
        [(x + pad, y + pad + int(shadow * ss * 0.35)) for x, y in pts], fill=(0, 0, 0, 96))
    sh = sh.filter(ImageFilter.GaussianBlur(shadow * ss * 0.55))
    canvas = Image.alpha_composite(canvas, sh)
    canvas.alpha_composite(core, (pad, pad))
    return canvas.crop((pad, pad, pad + W, pad + H))


def edge_visible_box(dock: str) -> tuple:
    """边缘线**可见那几 px** 在窗口里的位置 (窗口比它厚, 多出来的是命中的透明区)。"""
    if dock == "right":
        return (EDGE_HIT - EDGE_W, 0, EDGE_HIT, EDGE_LEN)
    if dock == "left":
        return (0, 0, EDGE_W, EDGE_LEN)
    if dock == "top":
        return (0, 0, EDGE_LEN, EDGE_W)
    return (0, EDGE_HIT - EDGE_W, EDGE_LEN, EDGE_HIT)


def draw_edge(dock: str = "right") -> "Image.Image":
    """EDGE 态: 贴在屏幕边框上的一条边缘线, 永远画在**贴边那一侧**。"""
    S = SS
    vert = dock in ("left", "right")
    w, h = (EDGE_HIT, EDGE_LEN) if vert else (EDGE_LEN, EDGE_HIT)
    img = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    x0, y0, x1, y1 = edge_visible_box(dock)
    ImageDraw.Draw(img).rectangle([x0 * S, y0 * S, x1 * S - 1, y1 * S - 1],
                                  fill=EDGE_COL + (EDGE_A,))
    return img.resize((w, h), Image.LANCZOS)


def neonize(rgb) -> tuple:
    """把一个颜色「荧光化」: 饱和度/明度都抬到下限以上 —— 便签要的是发光感, 不是照搬原色。"""
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return tuple(int(round(c * 255)) for c in
                 colorsys.hsv_to_rgb(h, max(s, TAG_SAT_MIN), max(v, TAG_VAL_MIN)))


def accent_of(thumb: "Image.Image | None", fallback=NEON_FALLBACK) -> tuple:
    """当前壁纸的主色 → 便签荧光色。

    取「占比 × 饱和度」最高的那一档 (纯取平均会得到一团灰); 暗到发黑的档直接淘汰,
    否则夜景/暗调壁纸会配出一支看不见的荧光色。没图时退回固定的青色。
    """
    if thumb is None:
        return fallback
    try:
        q = thumb.convert("RGB").resize((48, 48), Image.LANCZOS).quantize(colors=8)
        pal = q.getpalette() or []
    except Exception:
        return fallback
    best, top = None, 0.0
    for cnt, idx in (q.getcolors() or []):
        if idx * 3 + 2 >= len(pal):
            continue
        r, g, b = pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]
        mx = max(r, g, b)
        if mx < 70:
            continue
        score = ((mx - min(r, g, b)) / mx) * (cnt ** 0.5)
        if score > top:
            best, top = (r, g, b), score
    return neonize(best) if best else fallback


def tag_key_xy(i: int) -> tuple:
    """便签第 ``i`` 个键 (0=上一张 1=播放 2=下一张) 的键心, **基准图坐标**。"""
    return (TAG_XOFF + TAG_TS + 16 + TAG_ISZ / 2 + i * (TAG_ISZ + TAG_IGAP),
            TAG_THK / 2 - 2)


def tag_size(dock: str) -> tuple:
    """便签在窗口里的尺寸 —— 吸左右边时长边竖着, 吸上/下边时旋转 90° 就横过来了。"""
    return (TAG_LEN, TAG_THK) if dock in ("left", "right") else (TAG_THK, TAG_LEN)


def tag_map(dock: str, x: float, y: float) -> tuple:
    """便签**基准图坐标 → 窗口坐标**。

    四个朝向都是同一张基准图 (``TAG_LEN``×``TAG_THK``, 左 = 伸出端, 右 = 贴屏幕边)
    旋转出来的, 这里与:func:`draw_tag` 里的 ``Image.transpose`` 一一对应
    (ROTATE_90 是**逆时针**: 左端 → 下端、上端 → 左端)。所以是 ``LEN-1-x`` 而不是
    ``LEN-x`` —— 差 1px 对 34px 的命中框无所谓, 但冒烟要按像素取样 (凹口/尖角),
    差一格就取到隔壁像素了。
    绘制、命中判定、冒烟取样全部走它 —— 各转各的, 吸上/下边时点击必然落空。
    """
    if dock == "right":
        return x, y
    if dock == "left":                       # FLIP_LEFT_RIGHT
        return TAG_LEN - 1 - x, y
    if dock == "top":                        # ROTATE_90
        return y, TAG_LEN - 1 - x
    return TAG_THK - 1 - y, x                # ROTATE_270 (bottom)


def tag_key_box(i: int, dock: str = "right") -> tuple:
    """便签第 ``i`` 个键在**窗口坐标**里的命中框 (键心外扩:data:`TAG_HIT`)。

    比键面大一圈: 便签只有 72px 厚、键只有 22px, 按键面本身收鼠标会点得人恼火;
    而三键间距 37 > 34, 外扩后也不会互相重叠。
    """
    x, y = tag_map(dock, *tag_key_xy(i))
    return (int(round(x - TAG_HIT)), int(round(y - TAG_HIT)), TAG_HIT * 2, TAG_HIT * 2)


def media_icon(d, kind: str, cx: float, cy: float, s: float, *, lw=None,
               fill=ICON, solid_state: bool = True) -> None:
    """播放器图标 (``kind`` ∈ prev / play / pause / next / shuffle / order)。``s`` = 图标外接
    尺寸 (绘制尺度)。

    「状态」键实心、「移动」键空心 —— 照参考图的手感, 实心只留给真正会变状态的那一个。
    ``shuffle`` / ``order`` 是卡片控制条第 4 颗 (换图档位): 画的是**当前所在的那个档**
    (与播放键「⏸ = 现在正在播」同一套读法), 交叉箭头 = 随机档, 三横 = 顺序档。
    """
    w = lw if lw is not None else ICON_W * (s / (20.0 * SS))
    bar_w, bar_h = max(1.0, w * 1.6), s * 0.30
    if kind in ("play", "pause") and solid_state:
        if kind == "play":
            d.polygon([(cx - s * 0.185, cy - s * 0.285), (cx - s * 0.185, cy + s * 0.285),
                       (cx + s * 0.30, cy)], fill=fill)
        else:
            for k in (-1, 1):
                d.rounded_rectangle([cx + k * s * 0.175 - bar_w / 2, cy - s * 0.285,
                                     cx + k * s * 0.175 + bar_w / 2, cy + s * 0.285],
                                    radius=bar_w / 2, fill=fill)
        return
    if kind == "play":
        hollow_tri(d, [(cx - s * 0.17, cy - s * 0.27), (cx - s * 0.17, cy + s * 0.27),
                       (cx + s * 0.26, cy)], w, fill)
        return
    if kind == "pause":
        for k in (-1, 1):
            rline(d, (cx + k * s * 0.17, cy - s * 0.26), (cx + k * s * 0.17, cy + s * 0.26),
                  w * 1.5, fill)
        return
    if kind == "order":
        for k in (-1, 0, 1):
            rline(d, (cx - s * 0.30, cy + k * s * 0.20), (cx + s * 0.30, cy + k * s * 0.20),
                  w, fill)
        return
    if kind == "shuffle":
        # 交叉双箭头: 两条斜线, 右端各带一个实心箭头尖 (指向线的前进方向)
        ha, hb = s * 0.28, s * 0.21
        for sy in (-1, 1):
            p0, p1 = (cx - ha, cy - sy * hb), (cx + ha, cy + sy * hb)
            rline(d, p0, p1, w, fill)
            ux, uy = 2 * ha, 2 * sy * hb
            ln = (ux * ux + uy * uy) ** 0.5
            ux, uy = ux / ln, uy / ln
            hl = s * 0.17
            d.polygon([p1,
                       (p1[0] - ux * hl - uy * hl * 0.55, p1[1] - uy * hl + ux * hl * 0.55),
                       (p1[0] - ux * hl + uy * hl * 0.55, p1[1] - uy * hl - ux * hl * 0.55)],
                      fill=fill)
        return
    k = -1.0 if kind == "prev" else 1.0
    rline(d, (cx + k * s * 0.30, cy - bar_h), (cx + k * s * 0.30, cy + bar_h), bar_w, fill)
    hollow_tri(d, [(cx - k * s * 0.26, cy - s * 0.29), (cx - k * s * 0.26, cy + s * 0.29),
                   (cx + k * s * 0.20, cy)], w, fill)


def icon_btn(ink, kind: str, cx: float, cy: float, s: float, *, accent,
             hover: bool = False, playing: bool = False,
             radius: "float | None" = None) -> None:
    """图标键。平时只有**裸线图标**(没有框); 鼠标指到才浮出一块**键帽** + accent 荧光,
    图标转纯白。

    ``radius=None`` (缺省) = **斜切键帽**, 键帽的斜边亮线就是「按钮边框的科技纹路」(便签);
    给了 ``radius`` = **微圆角键帽** (角上那道亮线换成一道极淡的顶边线), 卡片控制条走这个 ——
    「小窗口的按钮不斜切了, 改成一点点圆边(只有一点点)」。

    图标必须画在**独立层**再合上去: 在 RGBA 画布上直接 ``ImageDraw`` 是**替换语义**,
    会把刚铺好的荧光 alpha 啃掉, 图标边缘留一圈暗边 (2026-09-25 踩到)。
    """
    if hover:
        k, cut = s * 1.5, s * 0.28
        box = [cx - k / 2, cy - k / 2, cx + k / 2, cy + k / 2]
        rad = None if radius is None else max(1, int(radius))
        g = Image.new("L", ink.size, 0)
        gd = ImageDraw.Draw(g)
        if rad is None:
            gd.polygon(chamfered_pts_box(box, cut), fill=255)
        else:
            gd.rounded_rectangle(box, radius=rad, fill=255)
        g = g.filter(ImageFilter.GaussianBlur(s * 0.34))
        ink.alpha_composite(relayer(ink.size, g, accent, k=150))      # 外圈柔光
        key = Image.new("RGBA", ink.size, (0, 0, 0, 0))
        kd = ImageDraw.Draw(key)
        lw = max(1, int(s * 0.055))
        if rad is None:
            chamfered_box(kd, box, cut, fill=tuple(accent) + (40,),
                          outline=tuple(accent) + (205,), width=lw)
            for p0, p1 in box_segs(box, cut):                         # 斜边亮线
                kd.line([p0, p1], fill=(255, 255, 255, 150), width=max(1, int(s * 0.045)))
        else:
            kd.rounded_rectangle(box, radius=rad, fill=tuple(accent) + (40,),
                                 outline=tuple(accent) + (205,), width=lw)
            hl = max(1, int(s * 0.045))                               # 微圆角没有斜边,
            kd.line([(box[0] + rad, box[1] + hl), (box[2] - rad, box[1] + hl)],   # 用顶边
                    fill=(255, 255, 255, 150), width=hl)              # 亮线顶体积感
        ink.alpha_composite(key)
    if kind == "play" and playing:
        kind = "pause"
    lay = Image.new("RGBA", ink.size, (0, 0, 0, 0))
    media_icon(ImageDraw.Draw(lay), kind, cx, cy, s,
               fill=(255, 255, 255) if hover else ICON)
    ink.alpha_composite(lay)


def rmask(size, r) -> "Image.Image":
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1],
                                        radius=max(0, int(r)), fill=255)
    return m


def thumb_sq(src, size: int, *, rad: float = 0.0, ss: int = SS,
             dim: float = 0.90, ring: int = 64) -> "Image.Image":
    """缩略图方图 (``size`` 是 1x 边长, 内部按 ``ss`` 超采样)。

    便签里的是**圆角**(纸条 + 圆角缩略图), 卡片里的跟着面板对角斜切, 形状由调用方定。
    稍微压一点亮度, 免得缩略图比周围的灰阶 chrome 还亮。没图 → 走:func:`cover` 的占位渐变。
    """
    W = size * ss
    im = tag_base(src, W, W) if src is not None else cover(None, W, W)
    if dim < 1:
        im = Image.blend(im.convert("RGBA"),
                         Image.new("RGBA", im.size, (0, 0, 0, 255)), 1 - dim)
    out = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    out.paste(im.convert("RGBA"), (0, 0), rmask((W, W), int(rad * ss)))
    ol = Image.new("L", (W, W), 0)
    ImageDraw.Draw(ol).rounded_rectangle([0, 0, W - 1, W - 1], radius=max(0, int(rad * ss)),
                                         outline=255, width=max(1, ss // 2))
    return Image.alpha_composite(out, relayer((W, W), ol, k=ring))


def progress(d, ox: float, oy: float, w: float, frac: float, *,
             ss: int = SS, h: float = 2, track: int = TRACK, fill=FILL) -> None:
    """极细进度线: 底槽 + 白色填充, 两端圆头 (``frac`` = 填到哪, 0 = 只有底槽)。"""
    y = oy + h / 2
    rline(d, (ox, y), (ox + w, y), h * ss, (255, 255, 255, track))
    if frac > 0:
        rline(d, (ox, y), (ox + max(h * ss, w * max(0.0, min(1.0, frac))), y), h * ss, fill)


def draw_tag(dock: str = "right", thumb: "Image.Image | None" = None, accent=None,
             hot: "int | None" = None, playing: bool = False,
             frac: float = 0.0) -> "Image.Image":
    """TAG 态: 从边框伸出的**长条便签** —— 204×72, 零圆角, 伸出端带**防滑纹路**。

    内容横排: 圆角缩略图 → 上一张 / 播放 / 下一张三键 → 底部一条极细进度线。
    底是**壁纸晕染玻璃板** (把控件背后那块壁纸磨砂, 别太强烈) + 沿长边的刻度线 + 极轻纸颗粒。
    伸出端那 16px 是人字防滑纹 (见:func:`grip_arrow`) —— 2026-09-26 定下的形状:
    不再咬掉一个燕尾缺口, 而是把缺口补回来、在原处刻上配合箭头走向的防滑槽。
    四个朝向都由同一张基准图 (**左 = 伸出端, 右 = 贴屏幕边**) 旋转出来, 映射见:func:`tag_map`。

    ``hot`` = 鼠标正指着的键 (0/1/2); ``playing`` 时播放键画成 ⏸;
    ``frac`` = 进度线填到哪 (当前壁纸在壁纸夹里的位置)。
    """
    W, H, S = TAG_LEN, TAG_THK, SS
    if accent is None:
        accent = accent_of(thumb)
    img = glass(W, H, pts=tag_pts(W * S, H * S),
                back=wall_back(thumb, W, H, S, where="edge"),
                ss=S, paper=True, ticks="x")
    ink, d = ink_layer(img.size)
    grip_arrow(d, H * S, S)
    ink.alpha_composite(thumb_sq(thumb, TAG_TS, rad=TAG_TS * 0.26, ss=S),
                        (TAG_XOFF * S, ((H - TAG_TS) // 2) * S))
    for i, kind in enumerate(("prev", "play", "next")):
        kx, ky = tag_key_xy(i)
        icon_btn(ink, kind, kx * S, ky * S, TAG_ISZ * S, accent=accent,
                 playing=playing, hover=(hot == i))
    progress(d, TAG_XOFF * S, TAG_BAR_Y * S, (W - TAG_XOFF - 14) * S, frac, ss=S, h=2)
    img = Image.alpha_composite(img, ink).resize((W, H), Image.LANCZOS)
    rot = {"left": Image.FLIP_LEFT_RIGHT,
           "top": Image.ROTATE_90,           # 逆时针 90°: 左端 → 下端
           "bottom": Image.ROTATE_270}.get(dock)
    return img.transpose(rot) if rot is not None else img


def tag_base(thumb: "Image.Image", w: int, h: int) -> "Image.Image":
    """便签的底图: 缩略图按便签比例裁一块, **一步**缩到目标尺寸, 再补一道轻锐化。

    便签只有 204×72 = 14688 个像素, 原料是服务端 480px 的 JPEG 缩略图 —— 横向缩比近 8:1。
    这种量级的下采样要用**面积平均** (BOX, 纯低通): LANCZOS 的负瓣会在花瓣/网点这类高频
    纹理上压出振铃, 缩小后就是一片脏点。而且必须**一步到位** —— 先 LANCZOS 缩到一半、
    再缩到目标尺寸, 两轮重采样叠起来照样糊 (别改回:func:`cover`, 那个是给卡片用的)。
    面积平均会丢一点局部对比度, 所以再补一道很轻的 UnsharpMask 找回来 ——
    「太模糊」, 这是清晰度的底线。
    """
    sw, sh = thumb.size
    ar = w / h
    if sw / sh > ar:                       # 源比便签更宽 → 裁掉左右
        cw = max(1, int(round(sh * ar)))
        box = ((sw - cw) // 2, 0, (sw - cw) // 2 + cw, sh)
    else:                                  # 源更高 → 裁掉上下
        ch = max(1, int(round(sw / ar)))
        box = (0, (sh - ch) // 2, sw, (sh - ch) // 2 + ch)
    im = thumb.crop(box).resize((w, h), Image.BOX)
    return im.filter(ImageFilter.UnsharpMask(radius=1.0, percent=90, threshold=3))


def cover(img: "Image.Image | None", w: int, h: int) -> "Image.Image":
    """等比裁切成 w×h (object-fit: cover)。没图就画个渐变占位。"""
    if img is None:
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(out)
        for y in range(h):
            t = y / max(1, h - 1)
            d.line([(0, y), (w, y)],
                   fill=(int(40 + 30 * t), int(44 + 34 * t), int(54 + 40 * t), 255))
        return out
    sw, sh = img.size
    k = max(w / sw, h / sh)
    img = img.resize((max(1, int(sw * k)), max(1, int(sh * k))), Image.LANCZOS)
    l, t = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((l, t, l + w, t + h))


def chamfer_mask(size, cuts: dict, ss: int = 1) -> "Image.Image":
    """斜切形状的遮罩 —— 给「跟着面板斜切」的缩略图用 (圆角遮罩已经不要了)。"""
    return poly_mask(size, chamfer_pts(size[0], size[1], cuts, ss))


def btn_box(i: int) -> tuple:
    """第 i 个**文字**按钮在卡片内的矩形。**画和点击命中必须共用这一个函数** —— 各算一遍必然错位。"""
    c, r = i % BTN_COLS, i // BTN_COLS
    return (C_PL + c * (BTN_W + BTN_GAP), BTN_TOP + r * BTN_ROW_H, BTN_W, BTN_H)


def icon_key_xy(i: int) -> tuple:
    """壁纸控制条第 ``i`` 颗**图标**键的键心 (卡片坐标)。四颗在卡片里居中均分。"""
    k = IBAR_SZ * IBAR_KEY
    span = ICON_N * k + (ICON_N - 1) * IBAR_GAP
    return ((CARD_W - span) / 2 + k / 2 + i * (k + IBAR_GAP), IBAR_CY)


def icon_key_box(i: int) -> tuple:
    """图标键的命中框 (键帽外扩一圈)。与:func:`btn_box` 同理: 画和命中同源。"""
    cx, cy = icon_key_xy(i)
    r = IBAR_SZ * IBAR_KEY / 2 + 5
    return (int(round(cx - r)), int(round(cy - r)), int(round(r * 2)), int(round(r * 2)))


def card_icons(info: "dict | None", *, shuffle: bool = False, playing: bool = False) -> list:
    """卡片**壁纸控制条**上该出现的图标键 —— 返回 ``[(动作键, 图标 kind), …]``。

    壁纸控制条统一用**图标**键: 上一张 / 播放·暂停 / 下一张 / 切换顺序, 与便签同一套手感,
    不再拆成「播放幻灯片」「顺序播放」两颗文字键。图标画的是**当前所在的那个档** (与 ⏸ 同一套读法):
    正在播 → ⏸、随机档 → 交叉箭头、顺序档 → 三横线。

    没有当前壁纸 (注册表那张已被删/移走) 时只留播放与档位: 上一张/下一张要有个「当前」才谈得上
    前后, 而播放键按下会从壁纸夹一端起步把桌面接回来 (那一格)。连壁纸夹都
    没有就一个键都不给 —— 那时候点什么都无处可去。
    """
    info = info or {}
    f = info.get("file") or {}
    if not f.get("name"):
        if not (info.get("folder") or {}).get("ready"):
            return []
        return [("play", "pause" if playing else "play"),
                ("shuffle", "shuffle" if shuffle else "order")]
    return [("prev", "prev"),
            ("play", "pause" if playing else "play"),
            ("next", "next"),
            ("shuffle", "shuffle" if shuffle else "order")]


def card_actions(info: "dict | None") -> list:
    """卡片上该出现哪些**文字**按钮 —— 与网页版同一口径: **没有就不出现, 而不是画成灰的**。

    返回 ``[(动作键, 文案, 是否危险色), …]``, 顺序就是排版顺序。数据没到手时一个都不画
    (那时候点哪个都没意义)。最多 3 个 = 2 行, 正好塞得下 (见:func:`btn_box`)。

    壁纸控制 (上一张/播放/下一张/档位) 不在这一列 —— 那四颗是图标键, 见:func:`card_icons`。
    「复制作品链接」「图像溯源」一并撤掉: 小控件本来就没有「作者主页」(网页版那颗跳应用内的
    作者详情页, 这里只能甩外部浏览器再让使用者自己找), 只留三个真用得上的。
    """
    info = info or {}
    art = info.get("artwork") or {}
    f = info.get("file") or {}
    if not f.get("name"):
        return []
    acts = []
    if str(art.get("artwork_id") or "").isdigit():
        acts.append(("library", "在库中查看", False))
    acts.append(("folder", "所在文件夹", False))
    if not f.get("in_library"):
        # 壁纸文件本身就在图库里的, 不给「移出壁纸」—— 那属于「从图库删」(另一套流程)
        acts.append(("recycle", "移出壁纸", True))
    return acts


def ai_sig(info: "dict | None") -> str:
    """AI 那两行的签名 —— 生成是**异步**的 (pending → ready), 而轮询是按 ``info_key`` 去重的:
    没有这一项, 「文案到了」会被当成「文件名没变」丢掉, 卡片就一直挂着本地那句。
    """
    ai = (info or {}).get("ai") or {}
    return f"{ai.get('state') or 'off'}:{ai.get('title') or ''}|{ai.get('mood') or ''}"


def ai_texts(info: "dict | None") -> dict:
    """``info["ai"]`` 里**已经能用**的那两行 (off/miss/error 一律当没有)。

    2026-09-27 改成「点按钮才生成」后, 服务端只会回 ``off`` / ``miss`` / ``ready`` 三种 ——
    ``miss`` 是「这张还没生成过」, 走本地兜底句。
    """
    ai = (info or {}).get("ai") or {}
    if str(ai.get("state") or "") != "ready":
        return {}
    return {"title": str(ai.get("title") or "").strip(),
            "mood": str(ai.get("mood") or "").strip()}


def _machine_title(t: str, fname: str = "") -> bool:
    """标题看着像**程序自己拼的**? (路径味 / 就是文件名 / 纯哈希或纯数字)

    自定义作品与合并产物入库时, 「标题」取的是 ``X图/#其他来源# / 1e94516aeda0e378`` 这种
    来源夹+文件名拼出来的串 —— 摆在壁纸卡的标题位上一眼就是没做完。
    「库里那些直接拿路径做标题的也可以换成更像标题的句子」。所以这种标题**不算真标题**,
    让 AI 那句顶上来 (真标题仍然优先, 见:func:`card_title`)。
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


def card_title(info: "dict | None") -> str:
    """卡片标题 —— 有**真标题**就用真标题; 否则优先 AI 那句, 再回落到固定文案。

    「库里那些直接拿路径做标题的也可以换成更像标题的句子」—— 壁纸文件名是
    ``109395314_p0.jpg`` 这种路径味的东西, 摆在标题位上读着就像没做完。2026-09-27 接上网
    生成 (core/ai_text.py) 之后: 生成了就用模型给的标题, 没开/没生成好仍是那几句固定兜底 ——
    绝不空着, 也绝不拿文件名顶替。

    2026-09-27 补: 光看「有没有 title 字段」不够 —— 程序自己拼的路径串也是有字段的, 得用
    :func:`_machine_title` 把它挡掉, 否则 AI 标题永远轮不到显示 (「文案改了, 标题没改」)。
    """
    info = info or {}
    art = info.get("artwork") or {}
    f = info.get("file") or {}
    t = str(art.get("title") or "").strip()
    if t and not _machine_title(t, f.get("name")):
        return t
    if not f.get("name"):
        return "桌面此刻空着"
    got = str(ai_texts(info).get("title") or "")
    if got:
        return got
    return "这一张还没有名字" if art.get("artwork_id") else "来自壁纸夹的一张"


def mood_line(info: "dict | None", thumb: "Image.Image | None") -> str:
    """卡片第三行: AI 生成的那句优先; 没开/还没生成好就用本地按画面推的 ``mood_of``。

    两条路**不是二选一**: 本地那句立刻就在, AI 那句几秒后才到 —— 有则换掉, 无则先用着,
    中间不存在空白期。
    """
    return str(ai_texts(info).get("mood") or "") or mood_of(thumb)


def card_byline(info: "dict | None") -> str:
    """标题下那行出处 (``#作品ID · 作者``); 缺哪半就只给哪半, 都没有才说「未识别出处」。"""
    art = (info or {}).get("artwork") or {}
    bits = []
    if art.get("artwork_id"):
        bits.append(f"#{art['artwork_id']}")
    if str(art.get("author_name") or "").strip():
        bits.append(str(art["author_name"]).strip())
    return " · ".join(bits) or "未识别出处"


# 「衬托气氛」的一行字: 明度一档 × 主色相族一档。只用**看得见的两件事**拼, 不编内容 ——
# 于是每张图都有自己的一句, 同一张永远同一句 (无随机, 冒烟可复现)。
# **两句都写短**: 那行只有 176px, 13px 字排到 12 个字就会被 _fit 截成「…」——
# 一句话被吃掉尾巴就白写了 (长度由冒烟钉住, 见 smoke_all.py 的 mood 断言)。
_MOOD_LIGHT = ((72, "夜色很沉"), (128, "光线转暗"), (190, "光线正好"), (256, "天光很足"))
_MOOD_HUE = (("暖", "暖色留着余温"), ("金", "像晒过的午后"), ("绿", "绿意漫上来"),
             ("青", "水色清冷"), ("蓝", "蓝调铺满背景"), ("紫", "紫粉在暗处"),
             ("粉", "粉色浮在表面"), ("素", "颜色都收着"))


def mood_of(thumb: "Image.Image | None") -> str:
    """缩略图 → 一句**衬托气氛**的话。

    卡片上原来那行是溯源结论 (「字节一致 · 像素完全一致 · 库内已是最清晰」)

    → 改成从这里取 (结论那行仍在 ``info["summary"]["line"]``, 托盘 tooltip 还在用)。

    只看整体**明度**与**饱和加权色相**(纯平均色相会被大片灰底拽成没意义的中间值)。没有缩略图
    时不说话 —— 编一句跟画面对不上的话, 比空着更糟。
    """
    if thumb is None:
        return ""
    try:
        px = list(thumb.convert("RGB").resize((24, 24), Image.LANCZOS).getdata())
    except Exception:
        return ""
    if not px:
        return ""
    n = len(px)
    lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in px) / n
    hsv = [colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0) for r, g, b in px]
    sw = sum(h[1] for h in hsv)
    if sw <= 0.02 or sw / n < 0.08:
        fam = "素"
    else:
        deg = (sum(h[0] * h[1] for h in hsv) / sw) * 360.0
        fam = ("暖" if (deg < 20 or deg >= 345) else
               "金" if deg < 55 else "绿" if deg < 150 else "青" if deg < 195 else
               "蓝" if deg < 255 else "紫" if deg < 300 else "粉")
    light = next(t for lim, t in _MOOD_LIGHT if lum < lim)
    return f"{light}，{next(t for k, t in _MOOD_HUE if k == fam)}"


# ══════════════════════════════════════════════════════════════════════
#  文案特效: 排版坐标 / 合成加粗 / 阴影 / **流转的霓虹流光**
#  
# ══════════════════════════════════════════════════════════════════════
def copy_geom(info: "dict | None", thumb: "Image.Image | None",
              ss: int = CARD_SS) -> dict:
    """卡片上**正文文案**的落点清单: 说明两行 + 氛围句各行 (+ 标题的字体 / 行心 / 徽章框)。

    画卡片、描阴影、铺霓虹流光三处都从这里取坐标 —— 各算一遍 ``PT + 52 + i * INFO_PITCH``
    的话, 改一次排版就会出现「阴影拖在字的旁边」「流光扫在字缝里」这种没人一眼看穿的错位。

    **标题的字形不出在这里**: 它进了跑马灯 (见:func:`title_prep`), 每帧按滚动量裁窗重贴,
    所以标题的字由标题条自己画, 这里只交出它的字体、行心与**定宽**的徽章框 (徽章不再随标题
    长短伸缩 —— 长了就滚, 见 ``BADGE_W``)。

    ``rows`` / ``mood`` 里是 ``(行左, 行顶, 串, 字体, 填充色)``; 行顶与 ``ImageDraw.text``
    的默认锚点一致。
    """
    S = ss
    x = (C_PL + C_TW + 18) * S
    maxw_info = (X_BOX[0] - (C_PL + C_TW + 18) - 12) * S     # 信息行不受叉叉高度影响, 可以顶到右边
    phy, plh = (C_PT - 13) * S, BADGE_H * S

    f = (info or {}).get("file") or {}
    dim = (f"{f.get('width')}×{f.get('height')}" if f.get("width") else "尺寸未知")
    if (info or {}).get("bigger"):
        dim += " · 库内有更高清"

    rows: list = []
    for i, (line, col) in enumerate(((card_byline(info), DIM),
                                     (dim, (255, 255, 255, 234)))):
        if line:
            rows.append((x, (C_PT + 52 + i * INFO_PITCH) * S,
                         _fit(line, INFO_SIZE * S, maxw_info), font(INFO_SIZE * S), col))
    mfnt = font(MOOD_SIZE * S, copy=True)
    mood = [(x, (C_PT + 52 + (2 + i) * INFO_PITCH) * S, ln, mfnt, DIM2)
            for i, ln in enumerate(mood_wrap(mood_line(info, thumb), maxw_info, MOOD_SIZE * S))]
    return {"tstr": card_title(info), "tfnt": font(TITLE_SIZE * S, copy=True),
            "tcy": phy + plh / 2, "badge": (BADGE_X0 * S, phy, BADGE_X1 * S, phy + plh),
            "tx": (BADGE_X0 + BADGE_PAD) * S, "tw_inner": TITLE_W * S,
            "rows": rows, "mood": mood}


def copy_mask(info: "dict | None", thumb: "Image.Image | None",
              ss: int = CARD_SS, geom: "dict | None" = None) -> "Image.Image":
    """**正文文案** (说明行 + 氛围句) 的合成 L 蒙版 —— 阴影与霓虹流光都从它派生。

    字形与:func:`draw_card` 画出来的那一份**逐像素对齐** (同一份:func:`copy_geom` 落点),
    差一点阴影就会偏离字、看着像重影。

    **标题不在里面**: 它要跑马灯, 若把标题也算进流光蒙版, 一滚起来光就描在「已经不在这儿」
    的字上 (亮点会落在笔画之间的空白)。标题条自带辉光与阴影 (见:func:`title_prep`)。
    """
    S = ss
    m = Image.new("L", (CARD_W * S, CARD_H * S), 0)
    d = ImageDraw.Draw(m)
    if geom is None:
        geom = copy_geom(info, thumb, S)
    for rx, ry, rs, rf, _col in geom["rows"] + geom["mood"]:
        d.text((rx, ry), rs, font=rf, fill=255)
    return m


def copy_box(mask: "Image.Image", ss: int = CARD_SS) -> "tuple | None":
    """文案蒙版的**外接框** (外扩:data:`NEON_PAD`, 并夹在画布内); 空蒙版返回 ``None``。

    辉光、阴影、流光都只算这一小块 —— 全画布的 ``GaussianBlur`` 实测 18ms 一发, 裁到文案框
    (1× 下 ≈280×195) 只剩零头; 而外扩量:data:`NEON_PAD` 大过光晕半径与阴影偏移,
    裁了看不出差别 (``ImageChops.offset`` 的环绕只带回框外的空白)。
    """
    box = mask.getbbox()
    if not box:
        return None
    pad = NEON_PAD * ss
    w, h = CARD_W * ss, CARD_H * ss
    return (max(0, box[0] - pad), max(0, box[1] - pad),
            min(w, box[2] + pad), min(h, box[3] + pad))


# ── 霓虹流光的底层: 骨架 → 沿笔画的路程场 → 逐帧波前 ────────────────────────
_NP = None
_CV2 = None
_GRID_OFF = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
             (-1, -1, 1.41421356), (-1, 1, 1.41421356),
             (1, -1, 1.41421356), (1, 1, 1.41421356))


def _np():
    """numpy **延迟加载** —— 只有真要用流光时才 import (托盘启动不必为它买单)。"""
    global _NP
    if _NP is None:
        import numpy
        _NP = numpy
    return _NP


def _cv2():
    """OpenCV 同理延迟加载; 这里只用到 ``distanceTransformWithLabels``。"""
    global _CV2
    if _CV2 is None:
        import cv2
        _CV2 = cv2
    return _CV2


def _shift2d(a, dy: int, dx: int, fill=0):
    """把 ``a`` 整体平移 ``(dy, dx)``, 空出来的边用 ``fill`` 补 —— 邻域运算的地基。

    **不能用 ``np.roll``**: 它是上下/左右**环绕**的, 左边缘的笔画会跟右边缘连出一条假通路,
    骨架细化与测地距离都会算错。
    """
    np = _np()
    out = np.full_like(a, fill)
    h, w = a.shape
    ys, yd = slice(max(0, dy), h + min(0, dy)), slice(max(0, -dy), h + min(0, -dy))
    xs, xd = slice(max(0, dx), w + min(0, dx)), slice(max(0, -dx), w + min(0, -dx))
    out[yd, xd] = a[ys, xs]
    return out


def _dilate3(m):
    """3×3 二值膨胀一档 —— 15px 小字的笔画只有 1~2px, 不先加粗一点, 细化会碎成一串断点。

    膨胀多出来的那一圈不会污染结果: 光最后还是要乘回原始蒙版 (见:func:`neon_flow`),
    这里只是为了让**骨架连通**。
    """
    out = m.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                out |= _shift2d(m, dy, dx)
    return out


def _thin(m):
    """Zhang-Suen 细化 → **1px 骨架** (bool)。

    笔画是一段**有宽度**的面, 而「光顺着笔画走」要的是一条**线**, 细化就是把面收成中线。
    八邻域按 P2(上) 起顺时针编号, 判据两条: 邻域里前景数 2~6, 且 0→1 的跳变**恰好一次**
    (两端点/内部点才留下, 孤立点与实心块内部去掉); 再交替删「北/东/南」与「南/东/西」三面
    —— 只删一面会留下一格一格的台阶, 交替删才收得干净。循环到一轮删不动为止。
    """
    np = _np()
    img = m.astype(np.uint8)
    while True:
        removed = False
        for step in (0, 1):
            p = [_shift2d(img, -1, 0), _shift2d(img, -1, 1), _shift2d(img, 0, 1),
                 _shift2d(img, 1, 1), _shift2d(img, 1, 0), _shift2d(img, 1, -1),
                 _shift2d(img, 0, -1), _shift2d(img, -1, -1)]              # P2..P9
            b = sum(x.astype(np.int16) for x in p)
            a = sum(((p[i] == 0) & (p[(i + 1) % 8] == 1)).astype(np.int16) for i in range(8))
            if step == 0:
                c1, c2 = p[0] * p[2] * p[4], p[2] * p[4] * p[6]
            else:
                c1, c2 = p[0] * p[2] * p[6], p[0] * p[4] * p[6]
            cond = (img == 1) & (b >= 2) & (b <= 6) & (a == 1) & (c1 == 0) & (c2 == 0)
            if cond.any():
                img[cond] = 0
                removed = True
        if not removed:
            return img.astype(bool)


def _skel_graph(sk):
    """骨架 → ``(ys, xs, nbr, comp)``: 各骨架点坐标、八邻接表、所属**连通块**编号。

    光要在**一笔之内**走, 所以得知道哪些骨架点连成一块 —— 一个汉字、乃至一行里每个字都是
    独立的一块; 测地距离只能在连通的骨架上传播, 不给每块安排起点, 远处的块就永远不亮。
    """
    np = _np()
    ys, xs = np.nonzero(sk)
    n = len(ys)
    idx = -np.ones(sk.shape, np.int32)
    idx[ys, xs] = np.arange(n)
    h, w = sk.shape
    nbr: list = []
    for i in range(n):
        y, x = int(ys[i]), int(xs[i])
        r = []
        for dy, dx, c in _GRID_OFF:
            yy, xx = y + dy, x + dx
            if 0 <= yy < h and 0 <= xx < w:
                j = int(idx[yy, xx])
                if j >= 0:
                    r.append((j, c))
        nbr.append(r)
    comp = [-1] * n
    cid = 0
    for i in range(n):
        if comp[i] >= 0:
            continue
        stack = [i]
        comp[i] = cid
        while stack:
            j = stack.pop()
            for k, _c in nbr[j]:
                if comp[k] < 0:
                    comp[k] = cid
                    stack.append(k)
        cid += 1
    return ys, xs, nbr, comp


def _graph_dist(nbr, root: int, n: int):
    """骨架上从 ``root`` 出发的 Dijkstra 路程 (px)。到不了的块留 ``inf``。"""
    np = _np()
    import heapq
    d = [float("inf")] * n
    d[root] = 0.0
    pq = [(0.0, root)]
    while pq:
        cur, i = heapq.heappop(pq)
        if cur > d[i]:
            continue
        for j, c in nbr[i]:
            nd = cur + c
            if nd < d[j]:
                d[j] = nd
                heapq.heappush(pq, (nd, j))
    return np.array(d, np.float32)


def neon_relief(mask_img: "Image.Image"):
    """文案蒙版 → **沿笔画走的路程场** ``(D, OFF, SPAN)`` (三张等大的 float32 ndarray)。

    ``D`` = 这个笔画像素「离本块起点、顺着笔画走有多少 px」—— 拿它当坐标, 光一往前走就必然
    顺着笔画, 不会跑到笔画之间的空白或字外。三步:

    ① 蒙版轻微膨胀 → **细化成 1px 骨架** → 在骨架上跑 Dijkstra 量到起点的路程 (这就是
       「沿笔画量距离」, 也是与「按直线横扫」彻底分家的一步);
    ② 每个连通块取一个起点, 并给整块一个**固定相位偏移** (``OFF``) 与**本块最大路程**
       (``SPAN``) —— 于是各块的光不会一齐亮、也不会一起到终点;
    ③ 骨架只有 1px 宽, 直接用会让光变成一根细线 —— 再把骨架上的值按「最近骨架点」摊回
       **整个笔画宽度** (cv2 的 ``distanceTransformWithLabels`` 顺带给出这个最近关系),
       并加上垂直方向的微小距离, 光就铺满笔画而不是一根线。

    整张图只在**换壁纸 / 打开卡片**时算一次 (见:func:`neon_prep`), 每帧只读不算。
    """
    np = _np()
    cv2 = _cv2()
    # 门槛取 64 而不是 >0: 1× 的字是抗锯齿画出来的, 边缘那圈很淡的灰若也算「笔画」, 膨胀后
    # 会拖出一圈毛刺骨架 —— 光就描在这些毛刺上。渲染仍用**没削过**的原蒙版 (边上照样柔和)。
    m = np.asarray(mask_img, dtype=np.uint8) >= 64
    if not m.any():
        return None
    sk = _thin(_dilate3(m))
    if not sk.any():
        return None
    ys, xs, nbr, comp = _skel_graph(sk)
    n = len(ys)
    dval = [0.0] * n
    oval = [0.0] * n
    sval = [1.0] * n
    buckets: dict = {}
    for i in range(n):
        buckets.setdefault(comp[i], []).append(i)
    for cid, nodes in buckets.items():
        d = _graph_dist(nbr, nodes[0], n)
        finite = d[np.isfinite(d)]
        mx = float(finite.max()) if finite.size else 0.0
        span = max(mx + NEON_WAVE * 3.0, NEON_WAVE * 4.0)
        off = (cid * 137.0 + 41.0) % span      # 各块错开起点相位 —— 别让整行字一起亮
        for i in nodes:
            dv = float(d[i])
            dval[i] = dv if dv == dv and dv != float("inf") else 0.0
            oval[i] = off
            sval[i] = span
    # 骨架上的值 → 整个笔画: labels 给出每个像素**最近的那个骨架点** (DIST_LABEL_PIXEL 时,
    # 骨架像素按光栅序拿到 1..n, 正好对上 np.nonzero 的顺序)。
    src_img = np.where(sk, 0, 255).astype(np.uint8)
    dist_sk, labels = cv2.distanceTransformWithLabels(
        src_img, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    sel = (labels - 1).astype(np.int32)
    D = dist_sk + np.asarray(dval, np.float32)[sel]
    OFF = np.asarray(oval, np.float32)[sel]
    SPAN = np.asarray(sval, np.float32)[sel]
    D = np.where(m, D, -1.0).astype(np.float32)
    return D, OFF.astype(np.float32), SPAN.astype(np.float32)


def neon_prep(info: "dict | None", thumb: "Image.Image | None",
              ss: int = 1) -> "tuple | None":
    """把正文流光的**定稿件**算好: (清晰蒙版, 柔光蒙版, 路程场, 文案框)。

    每帧就只剩「按相位切一道波前 + 相乘 + 合成」。没有文案时返回 ``None`` —— 那就别铺流光。
    细化 / 最短路 / 模糊全在这里做掉, 放进每帧就是白烧掉帧率。

    ``ss`` **默认 1**: :func:`draw_card` 交出来的是缩放回 1× 的成品帧, 流光就叠在它上面 ——
    这里要按 ``CARD_SS`` 算, 就会得到一张 2× 的蒙版贴到 1× 画布上, 直接错位到卡外。
    """
    S = ss
    mask = copy_mask(info, thumb, S)
    box = copy_box(mask, S)
    if not box:
        return None
    cut = mask.crop(box)
    relief = neon_relief(cut)
    if relief is None:
        return None
    # 光晕蒙版: blur 会把「细笔画摊薄」, 直接拿来当光晕是一圈看不见的灰 —— 用 ``point`` 建一张
    # 查找表放大:data:`NEON_HALO_GAIN` 档 (point 是 C 层, 只算一次 256 项 LUT, 便宜)。
    halo = cut.filter(ImageFilter.GaussianBlur(NEON_GLOW_BLUR * S)).point(
        lambda v: min(255, v * NEON_HALO_GAIN))
    return (cut, halo, relief, box)


def _flow_band(relief, phase: float):
    """在路程场上切出几道**沿笔画前进**的亮带 (float32 ndarray, 0..1)。

    每道波前有**自己的速度与呼吸相位**: 位置 ``c`` 随 ``phase`` 一路往前推, 只让路程
    ``D ≈ c`` 的那一段笔画亮 —— 于是光是一段段地**描**过去的。``abs`` 取到路的两端后用
    ``SPAN - u`` 折回来 (块的终点与起点同属一条回路), 光就不会「走到头消失」。

    明暗呼吸 (``NEON_FADE``) 让几道波前此起彼伏 —— 这就是用户要的「随机闪烁」, 而不是按
    固定节奏循环。``phase`` 是**拍数**(不取模), 呼吸项由 ``sin`` 自带周期, 位置项按各块的
    ``SPAN`` 折返, 两者频率互不公约 → 长时间看不成闭环。
    """
    np = _np()
    D, OFF, SPAN = relief
    # 笔画外 (D < 0) 一律不亮 —— 这一条**预乘一次**: 每道波前各扫一遍布尔索引 (numpy 最贵
    # 的一类操作), 四道就是四遍白账; 换成一次布尔数组的逐元素乘法, 结果完全一样。
    live = D >= 0
    inv = -1.0 / NEON_WAVE
    out = None
    for k in range(NEON_SRC):
        # ``fmod`` 与 ``%`` 在**非负**被除数上同值, 却快 4 倍 (numpy 的 remainder 为兼容负数
        # 被除数多做一次修正) —— 这里的被除数 = 拍数×正速度 + 两项非负偏移, 恒 ≥ 0, 可以放心换。
        c = OFF + (phase * NEON_SPEED[k] + NEON_OFF1[k])
        np.fmod(c, SPAN, out=c)
        u = np.abs(D - c)
        np.minimum(u, SPAN - u, out=u)           # 走到路两端就折回来 (终点与起点同属一条回路)
        v = u * inv + 1.0                        # 1 - u / NEON_WAVE (u ≥ 0 → 只需夹下界)
        np.maximum(v, 0.0, out=v)
        v *= v                                   # 平方 → 亮带更细更利, 才有金属反光的锐度
        v *= 0.40 + 0.60 * (0.5 + 0.5 * math.sin(phase * NEON_FADE[k] + NEON_OFF2[k]))
        v *= live
        out = v if out is None else np.maximum(out, v)
    return out


def _neon_paint(dst: "Image.Image", neon: tuple, phase: float, accent) -> None:
    """把波前本体**就地**画到 ``dst`` 上。``neon`` = (清晰蒙版, 柔光蒙版, 路程场, 框)。

    正文 (:func:`neon_flow`) 与标题跑马灯 (:func:`title_frame`) 共用 —— 两处的「光沿笔画
    描过去」是同一套东西, 分两份写迟早各自跑偏。
    """
    m, mg, relief, (x0, y0, _x1, _y1) = neon
    band = _flow_band(relief, phase)
    bandimg = Image.fromarray((band * 255.0 + 0.5).astype("uint8"), "L")
    dst.alpha_composite(
        relayer(m.size, ImageChops.multiply(mg, bandimg), accent, k=NEON_GLOW_K), (x0, y0))
    dst.alpha_composite(
        relayer(m.size, ImageChops.multiply(m, bandimg), (255, 255, 255), k=NEON_CORE_K),
        (x0, y0))


def neon_flow(base: "Image.Image", neon: tuple, phase: float, accent) -> "Image.Image":
    """把**沿笔画描过去**的霓虹流光叠上去。

    路程场切波前 → 与文案蒙版**相乘** → 亮的位置天生只落在笔画上 (蒙版外一律 0), 于是既不
    是一条横扫的亮带、也不是一团挪动的斑, 而是「金属反光顺着型面滑」的那道亮边。

    结果画在 ``base.copy`` 上, **不碰**缓存里那张底图; 且只算文案外接框那一小块。
    """
    out = base.copy()
    _neon_paint(out, neon, phase, accent)
    return out


def title_prep(info: "dict | None", thumb: "Image.Image | None", accent,
               ss: int = CARD_SS) -> "dict | None":
    """把标题做成一条**可滚动的标题条** (跑马灯) —— 再长的标题也不怕。

    ① 在 ``ss`` 倍画布上预渲染**两遍**标题 (渐变字面 + accent 辉光 + 阴影, 与正文一脉),
       中间留:data:`TITLE_GAP`, 条的两端各留:data:`TITLE_W` 的空白 —— 短标题要居中、
       长标题滚过条尾时也要取得到下一遍;
    ② 再缩回 1× 交成品帧: 每帧只按滚动量裁窗贴回 (见:func:`title_frame`), 不再光栅化文字;
    ③ 顺带交出标题的**清晰蒙版 / 柔光蒙版 / 沿笔画的路程场**, 建在**条坐标系**里 —— 滚的
       时候字与路程场一起裁, 光就跟得住笔画 (若按屏幕坐标铺光, 一滚光就会被字划过去)。

    没有标题时返回 ``None``。``scroll`` 判据 = 一遍标题的自然宽度超过可视区:data:`TITLE_W`。
    """
    S = ss
    geo = copy_geom(info, thumb, S)
    tstr, tfnt = geo["tstr"], geo["tfnt"]
    if not tstr:
        return None
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    ls = TRACK_LS * S
    limit = TITLE_MAX * S

    def _w(s: str) -> int:
        return int(round(sum(probe.textlength(ch, font=tfnt) for ch in s)
                         + ls * max(0, len(s) - 1)))

    if _w(tstr) > limit:                       # 极端长的标题: 先裁「…」再滚
        keep = tstr
        while keep and _w(keep + "…") > limit:
            keep = keep[:-1]
        tstr = keep + "…"
    tw_ss = _w(tstr)
    tw_ss += (-tw_ss) % S          # 对齐到 ss 倍栅格: 两遍标题缩回 1× 的重采样相位才一致,
    pad, gap, ch_h = geo["tw_inner"], TITLE_GAP * S, BADGE_H * S   # 接缝才逐像素重合 (不跳)
    cw = 2 * pad + 2 * (tw_ss + gap)
    tmask = text_mask((cw, ch_h), pad, ch_h / 2, tstr, tfnt, ls=ls)
    tmask = ImageChops.lighter(
        tmask, text_mask((cw, ch_h), pad + tw_ss + gap, ch_h / 2, tstr, tfnt, ls=ls))
    lay = Image.new("RGBA", (cw, ch_h), (0, 0, 0, 0))
    lay.alpha_composite(relayer((cw, ch_h), tmask.filter(ImageFilter.GaussianBlur(2.2 * S)),
                                accent, k=118))
    lay.alpha_composite(relayer(
        (cw, ch_h),
        ImageChops.offset(tmask, SHADOW_OFF[0] * S, SHADOW_OFF[1] * S)
        .filter(ImageFilter.GaussianBlur(SHADOW_BLUR * S)),
        (0, 0, 0), k=SHADOW_K))
    grad = vgrad((cw, ch_h), (255, 255, 255), tint_light(accent))
    grad.putalpha(tmask)
    lay.alpha_composite(grad)
    w1 = max(1, cw // S)
    mask1 = tmask.resize((w1, BADGE_H), Image.LANCZOS)
    relief = neon_relief(mask1)
    return {"rgba": lay.resize((w1, BADGE_H), Image.LANCZOS),
            "mask": mask1,
            "halo": mask1.filter(ImageFilter.GaussianBlur(NEON_GLOW_BLUR)).point(
                lambda v: min(255, v * NEON_HALO_GAIN)),
            "D": relief[0] if relief else None,
            "OFF": relief[1] if relief else None,
            "SPAN": relief[2] if relief else None,
            "x": BADGE_X0 + BADGE_PAD, "y": C_PT - 13,
            "w": TITLE_W, "h": BADGE_H,
            "pad": pad // S, "tw": tw_ss // S, "period": (tw_ss + gap) // S,
            "fit": min(TITLE_W, tw_ss // S + 2 * TITLE_FIT),
            "scroll": tw_ss > TITLE_W * S}


def title_frame(base: "Image.Image", t: "dict | None", phase: float,
                accent) -> "Image.Image":
    """把**标题跑马灯**叠上去: 按当前滚动量裁窗贴回 + 沿笔画的流光。

    短标题 (``scroll`` 为假) 只裁**一遍标题本体**、居中贴回 —— 按可视区宽裁的话, 窗口右端会
    越过第二遍标题的起点, 同一块里出现两个标题, 而且因为不滚而一动不动 (
    「标题并没有滚动, 而是静止的, 而且还会重复显示」)。
    长标题按 ``phase * TITLE_SCROLL`` 匀速左移, 移到第二遍标题的起点时与开头**逐像素重合**,
    于是滚动首尾相接、看不到接缝。结果画在 ``base.copy`` 上。
    """
    if not t:
        return base
    W, H, tw = t["w"], t["h"], t["tw"]
    if t["scroll"]:
        ox, vw = W + int((phase * TITLE_SCROLL) % t["period"]), W
    else:
        vw = t["fit"]
        ox = t["pad"] - (vw - tw) // 2
    ox = max(0, min(ox, t["rgba"].size[0] - vw))
    px = t["x"] + (W - vw) // 2
    out = base.copy()
    out.alpha_composite(t["rgba"].crop((ox, 0, ox + vw, H)), (px, t["y"]))
    if t["D"] is not None:
        _neon_paint(out,
                    (t["mask"].crop((ox, 0, ox + vw, H)),
                     t["halo"].crop((ox, 0, ox + vw, H)),
                     (t["D"][:, ox:ox + vw], t["OFF"][:, ox:ox + vw],
                      t["SPAN"][:, ox:ox + vw]),
                     (px, t["y"], px + vw, t["y"] + H)),
                    phase, accent)
    return out


def card_base(info: "dict | None", thumb: "Image.Image | None",
              accent=None) -> "Image.Image":
    """卡片的**底图** (``CARD_SS`` 倍, 未缩回 1×): 玻璃板 + 缩略图 + 徽章底 + 正文 + 氛围句 +
    分隔线 —— **不含任何按钮/图标/叉叉**。

    2026-09-29 拆层 (性能): 悬停高亮 / 播放态 / 两步确认只动:func:`card_overlay` 那层
    (几十 ms), 底图随数据走 (一百多 ms) —— 两者各画各的, 合成顺序与原先一次画完完全一致
    (alpha over 满足结合律, 逐像素等价)。返回 SS 尺寸, 调用方负责合成与缩放。
    """
    W, H, S = CARD_W, CARD_H, CARD_SS
    PL, PT, TW, TH = C_PL, C_PT, C_TW, C_TH
    if accent is None:
        accent = accent_of(thumb)
    img = glass(W, H, pts=chamfer_pts(W * S, H * S, CUT_CARD, S),
                back=wall_back(thumb, W, H, S, where="center", dim=0.52),
                ss=S, ticks="y", shadow=9)

    tw2, th2 = TW * S, TH * S
    ph = cover(thumb, tw2, th2)
    ph = Image.blend(ph.convert("RGBA"), Image.new("RGBA", ph.size, (0, 0, 0, 255)), 0.06)
    ph.putalpha(chamfer_mask((tw2, th2), CUT_PH, S))
    img.alpha_composite(ph, (PL * S, PT * S))
    ring = Image.new("L", (tw2, th2), 0)
    ImageDraw.Draw(ring).polygon(chamfer_pts(tw2, th2, CUT_PH, S),
                                 outline=255, width=max(1, S // 2))
    img.alpha_composite(relayer((tw2, th2), ring, k=78), (PL * S, PT * S))

    geo = copy_geom(info, thumb, S)
    ink, dr = ink_layer(img.size)

    # ── 标题: **定宽徽章** (2026-09-28 四期: 标题改跑马灯, 徽章不再随标题长短伸缩) ──
    # 标题的**字形不在这里画**: 它由:func:`title_prep` 预渲染成一条可滚动的长条 (自带 accent
    # 辉光与阴影), 每帧按滚动量裁窗贴回 (见:func:`title_frame`) —— 所以这里只剩徽章底。
    _bx0, _bty, _bx1, _bby = geo["badge"]
    dr.rounded_rectangle([_bx0, _bty, _bx1, _bby], radius=BADGE_RAD * S,
                         fill=(0, 0, 0, 150), outline=(255, 255, 255, 62),
                         width=max(1, S // 2 + 1))
    cmask = copy_mask(info, thumb, S, geom=geo)
    cbox = copy_box(cmask, S)
    csz, cxy = (cbox[2] - cbox[0], cbox[3] - cbox[1]), (cbox[0], cbox[1])
    # **阴影**是正文几行**共用的一层** (各自描一遍会在行与行之间叠出双影); 它压在正文柔光
    # 之上, 于是右下角暗、左上角亮 —— 「光自上来」的体积感就靠这一步。
    ink.alpha_composite(relayer(
        csz,
        ImageChops.offset(cmask.crop(cbox), SHADOW_OFF[0] * S, SHADOW_OFF[1] * S)
        .filter(ImageFilter.GaussianBlur(SHADOW_BLUR * S)),
        (0, 0, 0), k=SHADOW_K), cxy)

    # 出处 / 尺寸 / **氛围** —— 第三行原来是溯源结论 (「字节一致 · 像素完全一致」), 用户
    # 2026-09-26 嫌它不像给人看的, 换成配合这张画的一句氛围话。2026-09-27 起: 开了
    # 「✨ 壁纸文案」就用模型给的那句, 没开/没生成好回落到本地按画面推的 mood_of。
    # 氛围句从 2026-09-27 起**允许换行** (上限放到 30 字, 单行塞不下), 前两行仍是一行一条。
    # 落点 / 串 / 字体全来自:func:`copy_geom` (与阴影蒙版同一份坐标), 这里只负责画。
    for rx, ry, rs, rf, col in geo["rows"]:
        dr.text((rx, ry), rs, font=rf, fill=col)
    if geo["mood"]:
        # 氛围句是这一屏的**主角文案**: accent 柔光**一次性**从整块蒙版派生 —— 逐行各 blur 一遍,
        # 换行处会叠出两道亮边。蒙版直接在文案框的坐标系里画 (`copy_box` 已经留了外扩量)。
        mmask = Image.new("L", csz, 0)
        mdr = ImageDraw.Draw(mmask)
        for rx, ry, rs, rf, _col in geo["mood"]:
            mdr.text((rx - cbox[0], ry - cbox[1]), rs, font=rf, fill=255)
        ink.alpha_composite(relayer(csz, mmask.filter(ImageFilter.GaussianBlur(1.6 * S)),
                                    accent, k=64), cxy)
        for rx, ry, rs, rf, col in geo["mood"]:
            dr.text((rx, ry), rs, font=rf, fill=col)

    dy = C_DIV * S
    rline(dr, (PL * S, dy), ((W - PL) * S, dy), S, (255, 255, 255, 34))
    return Image.alpha_composite(img, ink)


def card_overlay(info: "dict | None", accent, *, armed: bool = False,
                 shuffle: bool = False, playing: bool = False,
                 hot: "int | None" = None, icon_hot: "int | None" = None
                 ) -> "Image.Image":
    """卡片的**按钮覆盖层** (``CARD_SS`` 倍透明层): 右上角裸线叉叉 + 壁纸控制条四颗图标键 +
    文字幽灵键 (含悬停高亮 / 两步确认文案)。

    只随交互态 (``armed`` / ``shuffle`` / ``playing`` / 两个 ``hot``) 变, 比底图便宜一个量级;
    与:func:`card_base` 合成即等价于整卡一次画完 (见:func:`draw_card`)。
    """
    W, H, S = CARD_W, CARD_H, CARD_SS
    ink, dr = ink_layer((W * S, H * S))
    # 右上角: 裸线叉叉。
    xc = (X_BOX[0] + X_BOX[2]) // 2 * S
    yc = (X_BOX[1] + X_BOX[3]) // 2 * S
    q = 5 * S
    rline(dr, (xc - q, yc - q), (xc + q, yc + q), 1.7 * S, (255, 255, 255, 210))
    rline(dr, (xc + q, yc - q), (xc - q, yc + q), 1.7 * S, (255, 255, 255, 210))

    for i, (key, kind) in enumerate(card_icons(info, shuffle=shuffle, playing=playing)):
        cx, cy = icon_key_xy(i)
        icon_btn(ink, kind, cx * S, cy * S, IBAR_SZ * S, accent=accent,
                 hover=(icon_hot == i), radius=IBAR_RAD * S)

    for i, (key, label, danger) in enumerate(card_actions(info)):
        bx, by, bw, bh = btn_box(i)
        box = [bx * S, by * S, (bx + bw) * S, (by + bh) * S]
        rad = BTN_RAD * S
        if hot == i:                                    # 悬停: 键周围一圈 accent 柔光
            g = Image.new("L", ink.size, 0)
            ImageDraw.Draw(g).rounded_rectangle(box, radius=rad, fill=255)
            g = g.filter(ImageFilter.GaussianBlur(5 * S))
            ink.alpha_composite(relayer(ink.size, g, accent, k=170))
        dr.rounded_rectangle(box, radius=rad,
                             fill=tuple(accent) + (46,) if hot == i else
                             ((255, 118, 118, 26) if danger else (255, 255, 255, 24)),
                             outline=tuple(accent) + (210,) if hot == i else
                             ((255, 124, 124, 150) if danger else (255, 255, 255, 68)),
                             width=max(1, S // 2 + 1))
        # 微圆角没有斜边可描, 顶边一道亮线顶住「上面来光」的体积感 (原来是斜边亮线)
        dr.line([(box[0] + rad, box[1] + max(1, S // 2)),
                 (box[2] - rad, box[1] + max(1, S // 2))],
                fill=(255, 255, 255, 190 if hot == i else 108), width=max(1, S // 2))
        dr.text(((bx + bw / 2) * S, (by + bh / 2) * S + S),
                "确认移出？" if (armed and key == "recycle") else label,
                font=font(14 * S),
                fill=(255, 255, 255) if hot == i else
                     ((255, 200, 200, 245) if danger else (242, 244, 247, 250)),
                anchor="mm")
    return ink


def draw_card(info: "dict | None", thumb: "Image.Image | None",
              confirm: bool = False, hot: "int | None" = None,
              icon_hot: "int | None" = None, accent=None,
              shuffle: bool = False, playing: bool = False) -> "Image.Image":
    """CARD 态: 展开的完整窗口。= 底图 (:func:`card_base`) + 按钮层 (:func:`card_overlay`)
    合成后缩回 1× —— 两层的绘制顺序与拆层前的单层画法一致 (alpha over 结合律, 逐像素等价)。

    和便签同一家族: **壁纸晕染玻璃板** + **对角斜切** (左上 + 右下) + 斜边亮线 + 沿长边的刻度线;
    缩略图跟着面板对角斜切; 右上角是**裸线叉叉**。分隔线下面分两段: 上段是**壁纸控制条**,
    下段是**文字键** (在库中查看 / 所在文件夹 / 移出壁纸), 按钮都是**幽灵键** —— 平时只有
    发丝描边, 鼠标指到才浮出 accent 荧光。

    ``hot`` = 鼠标正指着的**文字键**序号, ``icon_hot`` = 正指着的**图标键**序号;
    ``confirm=True`` 时「移出壁纸」的文案变成「确认移出？」(破坏性动作走两步);
    ``shuffle`` / ``playing`` 决定控制条第 4 颗与播放键画哪个图标。
    """
    if accent is None:
        accent = accent_of(thumb)
    base = card_base(info, thumb, accent)
    over = card_overlay(info, accent, armed=confirm, shuffle=shuffle,
                        playing=playing, hot=hot, icon_hot=icon_hot)
    return Image.alpha_composite(base, over).resize((CARD_W, CARD_H), Image.LANCZOS)


# ══════════════════════════════════════════════════════════════════════
#  几何 (纯函数, 冒烟离线覆盖)
# ══════════════════════════════════════════════════════════════════════
def dock_geom(wa: tuple, dock: str, anchor: int, w: int, h: int) -> tuple:
    """按吸边 + 锚点算窗口左上角, 并保证**贴住工作区那条边**。

    ``wa`` = (left, top, right, bottom) 的工作区; ``anchor`` 是沿边方向的中心坐标。
    吸上/下边时 anchor 是 x, 吸左/右边时是 y。
    """
    left, top, right, bottom = wa
    if dock == "right":
        x, y = right - w, min(max(anchor - h // 2, top), bottom - h)
    elif dock == "left":
        x, y = left, min(max(anchor - h // 2, top), bottom - h)
    elif dock == "top":
        x, y = min(max(anchor - w // 2, left), right - w), top
    else:                                                   # bottom
        x, y = min(max(anchor - w // 2, left), right - w), bottom - h
    return x, y


def nearest_dock(wa: tuple, rect: tuple) -> "tuple[str, int]":
    """离矩形中心最近的那条边 → (dock, anchor)。纯函数, 便于冒烟。"""
    left, top, right, bottom = wa
    x, y, w, h = rect
    cx, cy = x + w // 2, y + h // 2
    dist = {"left": cx - left, "right": right - cx,
            "top": cy - top, "bottom": bottom - cy}
    dock = min(dist, key=dist.get)
    return dock, (cx if dock in ("top", "bottom") else cy)


def round_rect(p, r, pad: int = 0) -> bool:
    """光标 p=(x,y) 是否落在 rect r=(x,y,w,h) 内 (pad 为外扩)。"""
    return (r[0] - pad <= p[0] <= r[0] + r[2] + pad
            and r[1] - pad <= p[1] <= r[1] + r[3] + pad)


def is_desktop_class(name: str) -> bool:
    """这个窗口类名算不算「桌面」。

    单独抽成纯函数是为了**冒烟能离线断言** —— 真机上没法造一个 Progman 出来,
    但「哪些类名算桌面」这条判定必须锁住 (见:data:`DESKTOP_CLASSES`)。
    """
    return str(name or "") in DESKTOP_CLASSES


def win_is_cloaked(hwnd) -> bool:
    """这个窗口是不是被 DWM 隐藏了 (别的虚拟桌面上的 / 未激活的 UWP) —— 它没占着像素。"""
    if _dwmapi is None or not hwnd:
        return False
    v = ctypes.c_int(0)
    try:
        hr = _dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED,
                                           ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        return False
    return hr == 0 and v.value != 0


def top_window_under(x: int, y: int, skip: int = 0) -> int:
    """光标底下**真正**那个顶层窗口, 跳过 ``skip`` (我们自己的窗口); 取不到 → 0。

    为什么非要有它: 小控件是 **topmost** 的, 边缘线/便签一画出来, ``WindowFromPoint`` 就把"自己"
    还回来了; 老写法把"自己"直接算成桌面 → 于是**永远**认为压在桌面上, 底下那个最大化的浏览器
    再也看不见 (自证循环)。

    ``EnumWindows`` 是按 **z 序从顶到底** 回调的, 所以跳过自己之后遇到的第一个**包含该点**的
    窗口, 就是真正露在那儿的那个。不可见 / 已最小化 / 被 DWM 隐藏的窗口不占这块像素, 不算。
    """
    pt = POINT(int(x), int(y))
    found = [0]

    def _cb(hwnd, _lp):
        if found[0]:
            return False                   # 最上面那个已经找到了, 停
        h = int(hwnd or 0)
        if not h or h == int(skip or 0):
            return True
        if not user32.IsWindowVisible(h) or user32.IsIconic(h) or win_is_cloaked(h):
            return True
        r = RECT()
        if not user32.GetWindowRect(h, ctypes.byref(r)):
            return True
        if user32.PtInRect(ctypes.byref(r), pt):
            found[0] = h
            return False
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    except Exception:
        return 0
    return int(found[0] or 0)


def window_chain_has_desktop(hwnd) -> bool:
    """从 ``hwnd`` 往上爬到顶, 这条链上有没有桌面类名。

    要爬到顶是因为: 桌面上放了图标之后, 最底下那层是 ``Progman``/``WorkerW`` 的**子窗口**
    (``SysListView32`` 之类), 只看最底那层会永远判 False。爬的层数就几层, 8 是防死循环的上限。
    """
    h = int(hwnd or 0)
    for _ in range(8):
        if not h:
            return False
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, buf, 64)
        if is_desktop_class(buf.value):
            return True
        h = int(user32.GetParent(h) or 0)
    return False


def premultiplied_bgra(img: "Image.Image") -> bytes:
    """``UpdateLayeredWindow`` 要的是**预乘 alpha** 的 BGRA, 不是 PIL 的 RGBA。

    **必须走 C 层**: 便签放大到 204×72 后每帧 147k 像素, 逐像素生成器 (``v * a // 255``)
    实测 0.3s/帧 —— 补间动画直接卡成幻灯片。``ImageChops.multiply`` 是 C 实现, 同样的数学,
    快两个数量级; ``split/merge`` 与切片重排也都是整块内存操作。

    **别在这里"顺手"把通道对调**: ``tobytes("raw", "BGRA")`` 本身就会把 R/B 换一次 —— 早先
    这里先把 r/b 对调再交给它, 等于连着换两次, 结果是**红蓝整体互换**(缩略图颜色整体偏移, 看起来像反色)。实测纯红 (255,0,0,255) 应出
    ``[0,0,255,255]``, 错版出 ``[255,0,0,255]``。
    """
    r, g, b, a = img.split()
    pre = Image.merge("RGBA", (ImageChops.multiply(r, a),
                              ImageChops.multiply(g, a),
                              ImageChops.multiply(b, a),
                              a))
    return pre.tobytes("raw", "BGRA")


# ══════════════════════════════════════════════════════════════════════
#  窗口
# ══════════════════════════════════════════════════════════════════════
EDGE, TAG, CARD = "edge", "tag", "card"

_ACTIVE_WIDGET = None


def _widget_wnd_proc(hwnd, msg, wparam, lparam):
    w = _ACTIVE_WIDGET
    if w is not None:
        try:
            r = w.handle_message(hwnd, msg, wparam, lparam)
            if r is not None:
                return r
        except Exception:
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ⚠ 必须保留模块级引用: 局部变量被 GC 掉函数指针后, Windows 回调会跳进空地址 → 进程崩
_WNDPROC_REF = _WNDPROC(_widget_wnd_proc)


class WallWidget:
    """桌面壁纸小控件。``start()`` 起一个 daemon 线程, 建窗与消息循环都在该线程内。"""

    def __init__(self, port: int, *, log=None):
        self.port = int(port)
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = log
        self.hwnd = 0
        self._tid = 0
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._pending = None            # 刷新线程取到的新数据, 由窗口线程取用
        self._key = ("", False)
        self._info = None               # 卡片当前显示的数据 (动作要按它来决定跳哪/删哪)
        self._thumb = None
        self._confirm = ""              # 已「确认」到期的壁纸文件名; 空 = 没有待确认的移出
        self._confirm_until = 0.0
        self._armed = False             # 当前按钮上是不是「确认移出？」形态 (卡片缓存键的一部分)
        self._actions = []              # 卡片上**文字**键的动作键, 与画面一一对应
        self._icons = []                # 卡片上**图标**键 (壁纸控制条) 的动作键, 同上
        self._accent = accent_of(None)  # 便签荧光色 (由缩略图主色派生)
        self.frac = 0.0                 # 便签底部进度线 (当前壁纸在壁纸夹里的位置)
        self.tag_hot = None             # 鼠标指着的便签键 (0/1/2)
        self.card_hot = None            # 鼠标指着的卡片**文字**键序号
        self.icon_hot = None            # 鼠标指着的卡片**图标**键序号 (壁纸控制条)
        self.playing = False            # 幻灯片在跑 (本控件自己的定时器, 见 _tag_action)
        self._slide_due = 0.0
        self.shuffle = False            # 「顺序 / 随机」—— 随机只影响本控件发起的换图
        self.on_desktop = True          # 光标底下是不是桌面 (不是就整块隐形, 见 _desktop_under)
        self._blanks: dict = {}         # 全透明帧缓存 (按尺寸) —— 不在桌面时用
        # "命中自己时往下找"的节流缓存 (见 _desktop_under): 同一像素 0.5s 内复用上次结论
        self._gate_key = None
        self._gate_ts = 0.0
        self._gate_val = True
        # 悬停每 16ms 都可能换一次 hot, 而重画一张超采样便签要几十毫秒 —— 按热键惰性缓存,
        # 只在 (dock, hot, playing) 真的变了才重画。卡片同理 (CARD_SS=2 更便宜, 但也没必要每帧来)。
        self._tagcache: dict = {}
        self._cardcache: dict = {}
        # **霓虹流光**: 沿笔画走, 定稿件
        # (文案蒙版 / 柔光蒙版 / 沿笔画的路程场 / 文案框) 在换数据时算一次 (见:func:`neon_prep`),
        # 每帧只推进相位 —— 蒙版里那串字的位置与内容都跟着数据走, 数据一变就得重算。
        self._neon = None               # neon_prep(...) 的返回, 或 None (没文案就不铺)
        self._title = None              # title_prep(...) 的返回 (跑马灯长条), 或 None
        self._neon_phase = 0.0          # 流光/跑马灯当前**拍数** (不取模, 见 _flow_band/title_frame)
        self._neon_tick = 0             # 距下一拍还差几个 tick (见 NEON_EVERY)
        # ── 后台渲染 (2026-09-29 性能): 重活全搬出窗口线程 ──
        # 病根: draw_card≈178ms / neon_prep≈34ms / title_prep≈65-110ms / draw_tag(SS=4) 几十 ms
        # 原先全在 16ms 定时器的消息循环里**同步**跑, 一换壁纸/一点按钮/一悬停新键, 鼠标与
        # 补间就整块冻结 100-330ms —— 便签与小窗口卡顿的直接原因。
        # 现在窗口线程只做命中判定与毫秒级合成; 重活由专职渲染线程做, 画完 PostMessage
        # (WM_PAINTED) 送回来, 窗口线程原子换缓存再刷一帧。请求槽**永远只留最新一份**
        # (投递即覆盖合并), 产物队列按代数/gen 校验, 过期帧直接丢 —— 慢机器上狂点也不会积压。
        self._rlock = threading.Lock()
        self._rreq = None               # 最新渲染请求 (None = 没活)
        self._revt = threading.Event()  # 有活了叫醒渲染线程
        self._rout = collections.deque(maxlen=6)   # 产物队列 (窗口线程 WM_PAINTED 逐帧取)
        self._rgen = 0                  # 请求代数: 只增, 产物带着它回来, 落后即过期
        self._rstate: dict = {}         # 渲染线程私有累积态 (base/neon/title/成品组), 窗口线程不碰
        self._card_last = None          # 卡片最近一张成品 (缓存 miss 时顶上, 不冻结不闪黑)
        self._tag_last = None           # 便签最近一张成品, 同上
        # ── GDI 复用 (同上): _apply 每帧建/毁 DIB section + 兼容 DC, 30fps 流光帧白白多花
        # 几毫秒还制造内存抖动 —— 按 (w,h) 缓存复用, 尺寸变了才新建 (补间帧尺寸渐变, 上限
        # 24 项防膨胀, 动画结束自然命中稳定尺寸)。
        self._dibcache: dict = {}

        wa = RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0)
        self.wa = (wa.left, wa.top, wa.right, wa.bottom)    # (left, top, right, bottom)
        self.dock = "right"
        self.anchor = (wa.top + wa.bottom) // 2

        self.edges = {k: draw_edge(k) for k in DOCKS}

        self.state = EDGE
        self.dragging = False
        self.drag_from = (0, 0)
        self.rect_from = (0, 0, 0, 0)
        self.moved = 0
        self.l_prev = self.r_prev = False
        self.rect = (0, 0, 1, 1)
        self.shown = None               # 最近一次真正画上去的 (w, h, img)
        self.anim = None                # (起点画面, 起点宽, 起点高, 起点时刻)

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """建窗 + 起消息循环线程。返回窗口是否建起来了 (失败调用方静默降级)。"""
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name=THREAD_NAME)
        self._thread.start()
        self._ready.wait(3.0)
        return bool(self.hwnd)

    def stop(self) -> None:
        """摘窗口: 投 WM_CLOSE 让消息循环线程自己 DestroyWindow 收尾 (不留 `_ACTIVE_WIDGET`)。"""
        self._stop.set()
        if self.hwnd:
            try:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        self._stopped.wait(1.5)

    # ---------- 消息循环 (控件线程内) ----------
    def _run(self) -> None:
        global _ACTIVE_WIDGET
        _ACTIVE_WIDGET = self
        self._tid = int(k32.GetCurrentThreadId())
        hinst = k32.GetModuleHandleW(None)
        # Windows 默认定时器分辨率 15.6ms: FRAME_MS=16 的 WM_TIMER 实际节奏 ~31ms, 动画一卡
        # 一卡 (2026-09-29「希望尽可能优化到极致」)。提到 1ms 让 16ms 定时器真的按 16ms 走;
        # 随窗口线程起停配对恢复, 不留全局副作用。
        if winmm is not None:
            try:
                winmm.timeBeginPeriod(1)
            except Exception:
                pass
        try:
            wc = WNDCLASS()
            wc.lpfnWndProc = _WNDPROC_REF
            wc.hInstance = hinst
            wc.lpszClassName = CLASS_NAME
            wc.hCursor = user32.LoadCursorW(0, IDC_ARROW)
            user32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = int(user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                CLASS_NAME, "Vixiq Wall Widget", WS_POPUP,
                0, 0, EDGE_HIT, EDGE_LEN, 0, 0, hinst, None) or 0)
            if not self.hwnd:
                self._log("桌面小控件: CreateWindowExW 失败")
                return
            self._apply()
            # 分层窗口 UpdateLayeredWindow 只负责画, **不会让窗口显示出来**, 必须自己 Show
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            user32.SetTimer(self.hwnd, TID, FRAME_MS, None)
            self._ready.set()
            # 数据放最后: 拉 HTTP 有超时, 排在建窗之后才不会把「窗口起来了没」拖过就绪窗口
            threading.Thread(target=self._refresh_loop, daemon=True,
                             name=THREAD_NAME + "-refresh").start()
            # 专职渲染线程: 重活 (整卡/便签/流光定稿) 都在这条线画, 画完 WM_PAINTED 送回
            threading.Thread(target=self._render_loop, daemon=True,
                             name=THREAD_NAME + "-render").start()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self._log(f"桌面小控件线程异常: {e}")
        finally:
            self._stop.set()
            self._revt.set()               # 叫醒渲染线程让它看到 _stop 退出
            try:
                user32.KillTimer(self.hwnd, TID)
                user32.DestroyWindow(self.hwnd)
                user32.UnregisterClassW(CLASS_NAME, hinst)
            except Exception:
                pass
            self._free_dibcache()          # 缓存的 DIB/DC 不放就是泄漏
            if winmm is not None:
                try:
                    winmm.timeEndPeriod(1)
                except Exception:
                    pass
            _ACTIVE_WIDGET = None
            self.hwnd = 0
            self._stopped.set()

    def handle_message(self, hwnd, msg, wparam, lparam):
        """窗口线程内处理消息; 返回 None 表示交给 ``DefWindowProcW``。

        按键一律走轮询 (见:meth:`_tick`), 这里只管生命周期 —— 分层 + ``WS_EX_NOACTIVATE``
        窗口的鼠标消息投递不稳, 但 ``WM_TIMER``/``WM_CLOSE`` 是可靠的。
        """
        if msg == WM_TIMER:
            self._tick()
            return 0
        if msg == WM_REFRESH:
            self._adopt_pending()
            return 0
        if msg == WM_PAINTED:
            self._adopt_rendered()
            return 0
        if msg in (WM_CLOSE, WM_DESTROY):
            user32.PostQuitMessage(0)
            return 0
        return None

    # ---------- 数据刷新 ----------
    def _log(self, msg: str) -> None:
        if self.log:
            try:
                self.log(msg)
            except Exception:
                pass

    def _poll_once(self) -> bool:
        """取一次数据; 变了就放 ``_pending`` 并返回 True。"""
        info = fetch_info(self.port)
        if not info:
            return False
        key = info_key(info)
        if key == self._key:
            return False
        self._pending = (key, info, fetch_thumb(self.port, info))
        return True

    def _refresh_loop(self) -> None:
        while True:
            try:
                if self._poll_once() and self.hwnd:
                    user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)
            except Exception:
                pass
            if self._stop.wait(REFRESH_SEC):
                return

    def _adopt_pending(self) -> None:
        p, self._pending = self._pending, None
        if not p:
            return
        self._key, self._info, self._thumb = p
        self._sync_keys()
        self._post_render(("data",))
        self._log(f"桌面小控件: 壁纸数据已更新 ({self._key[0]})")

    def _sync_keys(self) -> None:
        """画面与动作的**键表**同源 (轻, 窗口线程就地跑): ``_armed``/``_actions``/``_icons``。

        画面本身交给渲染线程 (见:meth:`_post_render`); 这里只保证「点了哪个键」与
        「画面上画了哪些键」不错位。
        """
        self._armed = bool(self._confirm) and self._confirm == self._key[0]
        self._actions = [k for k, _, _ in card_actions(self._info)]
        self._icons = [k for k, _ in card_icons(self._info, shuffle=self.shuffle,
                                                playing=self.playing)]

    # ---------- 后台渲染 (2026-09-29 性能) ----------
    def _post_render(self, kinds) -> None:
        """投一份渲染请求 (窗口线程调用)。请求槽**永远只留最新一份**: 后来者覆盖字段、合并
        kinds —— 狂点/狂悬停也只会让渲染线程画「现在的样子」, 不积压不倒序。"""
        with self._rlock:
            merged = set(kinds)
            if self._rreq is not None:
                merged |= self._rreq["kinds"]
            self._rgen += 1
            self._rreq = {"kinds": merged, "gen": self._rgen, "key": self._key,
                          "info": self._info, "thumb": self._thumb, "armed": self._armed,
                          "shuffle": self.shuffle, "playing": self.playing,
                          "dock": self.dock, "hot": self.card_hot, "ihot": self.icon_hot}
        self._revt.set()

    def _render_loop(self) -> None:
        """渲染线程: 醒来就清空请求槽 (最新一份), 画完一批 PostMessage 送回窗口线程。"""
        while not self._stop.is_set():
            if not self._revt.wait(0.5):
                continue
            self._revt.clear()
            while not self._stop.is_set():
                with self._rlock:
                    req, self._rreq = self._rreq, None
                if req is None:
                    break
                try:
                    self._render_job(req)
                except Exception as e:
                    self._log(f"桌面小控件渲染线程异常: {e}")

    def _rput(self, req: dict, kinds: set, **extra) -> None:
        """渲染产物入队 + 通知窗口线程取货 (渲染线程调用)。"""
        out = {"gen": req["gen"], "key": req["key"], "armed": req["armed"],
               "shuffle": req["shuffle"], "playing": req["playing"], "kinds": kinds}
        out.update(extra)
        with self._rlock:
            self._rout.append(out)
        if self.hwnd:
            try:
                user32.PostMessageW(self.hwnd, WM_PAINTED, 0, 0)
            except Exception:
                pass

    def _render_job(self, req: dict) -> None:
        """真正的重活 (渲染线程内跑, 只碰 ``_rstate`` 与请求快照, 绝不碰窗口线程的缓存)。

        三种活: ``data`` (换壁纸: accent/进度/neon/title/base 全套重算) / ``cards`` (交互态
        变了: 只重画按钮层叠出的成品组) / ``tags`` (便签组)。data 任务顺带把卡片与便签的
        **全部悬停组合**预渲染掉 —— 鼠标还没扫过去, 高亮图就已经在缓存里等着了。
        """
        kinds = set(req["kinds"])
        st = self._rstate
        if "data" in kinds or st.get("key") != req["key"] or st.get("base") is None:
            kinds |= {"cards", "tags"}
            accent = accent_of(req["thumb"])
            st.clear()
            st.update(key=req["key"], accent=accent,
                      frac=folder_frac(req["info"]),
                      neon=neon_prep(req["info"], req["thumb"]),
                      title=title_prep(req["info"], req["thumb"], accent),
                      base=card_base(req["info"], req["thumb"], accent),
                      cards={}, tags={})
            # 定稿件先到货: 标题/流光可以先行换新 (画面主体), 按钮成品随后就到
            self._rput(req, {"data"}, accent=st["accent"], frac=st["frac"],
                       neon=st["neon"], title=st["title"])
        if "tags" in kinds:
            dock = req["dock"]
            st["tags"] = {(dock, hot, ply):
                          draw_tag(dock, req["thumb"], st["accent"], hot=hot,
                                   playing=ply, frac=st["frac"])
                          for ply in (False, True) for hot in (None, 0, 1, 2)}
            self._rput(req, {"tags"}, tags=dict(st["tags"]))
        if "cards" in kinds:
            # 悬停组合: 先画**鼠标现在指着的那颗**并立刻送一帧 (点播放/换档位后 ~几十 ms 就
            # 能看到图标切换), 再把其余组合补齐 —— 鼠标扫过去时永远命中缓存, 零延迟。
            combos = [(None, None)] + [(i, None) for i in range(3)] \
                + [(None, i) for i in range(4)]
            prio = (req["hot"], req["ihot"])
            if prio in combos:
                combos.remove(prio)
                combos.insert(0, prio)
            for idx, (h, ih) in enumerate(combos):
                over = card_overlay(req["info"], st["accent"], armed=req["armed"],
                                    shuffle=req["shuffle"], playing=req["playing"],
                                    hot=h, icon_hot=ih)
                st["cards"][(h, ih)] = Image.alpha_composite(st["base"], over).resize(
                    (CARD_W, CARD_H), Image.LANCZOS)
                if idx == 0:
                    self._rput(req, {"cards"}, cards={prio: st["cards"][prio]})
            self._rput(req, {"cards"}, cards=dict(st["cards"]))

    def _adopt_rendered(self) -> None:
        """窗口线程收渲染产物 (WM_PAINTED): 校验代数与状态, 过期即丢, 新鲜则原子换缓存刷帧。"""
        while True:
            with self._rlock:
                if not self._rout:
                    return
                out = self._rout.popleft()
            if out["gen"] < self._rgen and self._rreq is not None:
                continue          # 已有更新的请求在路上, 这帧不折腾 (最新的马上到)
            if (out["key"] != self._key or out["armed"] != self._armed
                    or out["shuffle"] != self.shuffle or out["playing"] != self.playing):
                continue          # 状态又变了, 产物过期
            if "data" in out["kinds"]:
                self._accent, self.frac = out["accent"], out["frac"]
                self._neon, self._title = out["neon"], out["title"]
                self._neon_phase = 0.0    # 新数据 = 新图案, 相位从 0 起
                self._neon_tick = 0
                self.tag_hot = None
            if "tags" in out["kinds"]:
                self._tagcache = out["tags"]
                self._tag_last = None
            if "cards" in out["kinds"]:
                self._cardcache = out["cards"]
                self._card_last = None
            if self.state in (CARD, TAG) and not self.anim:
                self._apply()

    def _tag_img(self) -> "Image.Image":
        """便签画面 (按 ``dock/hot/playing`` 缓存, 组合由渲染线程预渲染)。miss 时用最近一张
        成品顶上并投后台补画 —— 悬停三键首次也不再顿。"""
        k = (self.dock, self.tag_hot, self.playing)
        img = self._tagcache.get(k)
        if img is not None:
            self._tag_last = img
            return img
        self._post_render(("tags",))
        if self._tag_last is not None:
            return self._tag_last
        img = draw_tag(self.dock, self._thumb, self._accent, hot=self.tag_hot,
                       playing=self.playing, frac=self.frac)
        self._tagcache[k] = img           # 首张同步 (仅启动后第一次悬停会走到)
        self._tag_last = img
        return img

    def _card_img(self) -> "Image.Image":
        """卡片画面 (悬停组合由渲染线程预渲染)。miss 时旧成品顶上 + 后台补, 不冻结消息循环。"""
        k = (self.card_hot, self.icon_hot)
        img = self._cardcache.get(k)
        if img is not None:
            self._card_last = img
            return img
        self._post_render(("cards",))
        if self._card_last is not None:
            return self._card_last
        img = draw_card(self._info, self._thumb, self._armed, hot=self.card_hot,
                        icon_hot=self.icon_hot, accent=self._accent,
                        shuffle=self.shuffle, playing=self.playing)
        self._cardcache[k] = img           # 首张同步 (仅启动后第一次展开会走到)
        self._card_last = img
        return img

    def _paint_card(self) -> None:
        """数据/确认态/档位变了: 同步键表 + 投后台重画 (兼容旧名, 画面刷新交给渲染产物)。"""
        self._sync_keys()
        self._post_render(("cards",))

    def _force_refresh(self) -> None:
        """强制重取一次 (忽略「文件名没变」的去重) —— 动作改动了什么之后要立刻看到结果。"""
        info = fetch_info(self.port)
        if not info:
            return
        self._pending = (info_key(info), info, fetch_thumb(self.port, info))
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def refresh(self) -> None:
        """外部主动要求刷新 (线程安全: 只是去后台取一次数, 再投消息给窗口线程)。"""
        threading.Thread(target=self._force_refresh, daemon=True,
                         name=THREAD_NAME + "-refresh").start()

    # ---------- 几何 ----------
    @staticmethod
    def _cursor() -> tuple:
        p = POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return (p.x, p.y)

    @staticmethod
    def _down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def _desktop_under(self, cur) -> bool:
        """光标底下的窗口是不是**桌面** —— 决定小控件该不该露出来。

        「便签的触发只应该在桌面, 否则平时浏览其他窗口也会跳出来」。
        分层窗口的透明像素**不参与命中测试**, 所以光标落在没画东西的那一截时, 这里拿到的是
        底下真正的窗口 —— 正是想要的答案。

        光标压在自己画出来的像素上时拿到的是**本窗口**, 这时**不能**算桌面 (老写法就是这里
        翻的车): 小控件是 topmost, 一旦边缘线画出来就永远只看见自己 → 底下压着的最大化浏览器
        再也看不见, 于是"置顶窗口时还是会被触发"。现在改成:func:`top_window_under` **跳过自己
        往下找**, 拿真正露在那儿的窗口再判。
        """
        h = int(user32.WindowFromPoint(POINT(cur[0], cur[1])) or 0)
        if h and not (self.hwnd and h == self.hwnd):
            return window_chain_has_desktop(h)      # 常规路: 便宜且精确

        # 命中自己 → 得枚举 z 序往下找。这条比上一条贵得多, 而鼠标悬停时几乎每帧都在自己
        # 身上, 所以按"同一像素 0.5s 内复用上次结论"节流, 免得把 16ms 的定时器吃掉。
        now = time.time()
        if self._gate_key == cur and now - self._gate_ts < 0.5:
            return self._gate_val
        val = window_chain_has_desktop(top_window_under(cur[0], cur[1], skip=self.hwnd or 0))
        self._gate_key, self._gate_ts, self._gate_val = cur, now, val
        return val

    def _blank(self, w: int, h: int):
        """全透明帧 (按尺寸缓存) —— 不在桌面时画这个, 等于整块隐形。"""
        key = (w, h)
        img = self._blanks.get(key)
        if img is None:
            if len(self._blanks) > 8:
                self._blanks.clear()
            img = Image.new("RGBA", key, (0, 0, 0, 0))
            self._blanks[key] = img
        return img

    def _gate_desktop(self, cur) -> None:
        """「只在桌面才露出来」的总闸 —— 每 tick 判一次, 状态变了才重画一帧。

        拖动期间**不判**: 右键拖着换边时光标必然扫过别的窗口, 那会儿把它隐形, 看起来就像
        「窗口拖丢了」。
        """
        if self.dragging:
            return
        ok = self._desktop_under(cur)
        if ok == self.on_desktop:
            return
        self.on_desktop = ok
        # **只改"画不画", 不动状态机**: 老写法在这里顺手把 state 掰回 EDGE、anim 清掉, 于是
        # 光标一离开桌面就等于把正在收/放的补间掐断, 而 _hover 下一帧又想把便签伸出来 ——
        # 两边每 16ms 抢一次, 用户看到的就是「点开小窗口再关上卡顿, 便签缩不回去」
        # (2026-09-26 实测报障)。画面交给 _render 的 on_desktop 分支整块交全透明帧,
        # 状态留给用户交互去推, 两者不再互相踩。
        self._apply()

    def _frames_for(self, st: str):
        """某个状态对应的 (尺寸, 画面)。"""
        if st == CARD:
            img = self._card_img()
            return img.size, img
        if st == TAG:
            img = self._tag_img()
            return img.size, img
        return self.edges[self.dock].size, self.edges[self.dock]

    def _rect_of(self, size) -> tuple:
        x, y = dock_geom(self.wa, self.dock, self.anchor, *size)
        return (x, y, size[0], size[1])

    def _xbox(self) -> tuple:
        x, y, _, _ = self.rect
        x0, y0, x1, y1 = X_BOX
        return (x + x0, y + y0, x1 - x0, y1 - y0)

    # ---------- 画面 ----------
    def _render(self):
        """当前该画上去的东西 —— 有动画时就是插值出来的中间帧。

        尺寸按进度插值, 位置仍交给:func:`dock_geom` 贴边对齐, 画面用交叉淡化; 于是边缘线
        是「鼓」成便签、便签是「撑」成窗口的, 不会硬跳。
        """
        (tw, th), timg = self._frames_for(self.state)
        if not self.on_desktop:
            # 光标前面挡着别的窗口 (不在桌面) → **整块全透明**: 既不伸便签, 也不留边缘线。
            # 「便签的触发只应该在桌面, 否则平时浏览其他窗口也会跳出来」。
            return (tw, th), self._blank(tw, th)
        if not self.anim:
            # 卡片稳定态才叠**标题跑马灯 + 霓虹流光**: 补间里叠是白烧 —— 240ms 的展开动画本来
            # 就在每帧 blend + resize, 再加这两层, 展开那一下就会顿。次序是先标题后正文:
            # 标题条在最上层 (它是卡片里唯一会动的一整块), 正文流光压在它下面也不会越界。
            if self.state == CARD:
                if self._title:
                    timg = title_frame(timg, self._title, self._neon_phase, self._accent)
                if self._neon:
                    timg = neon_flow(timg, self._neon, self._neon_phase, self._accent)
            return (tw, th), timg
        img0, w0, h0, t0 = self.anim
        e = _ease((time.time() - t0) * 1000.0 / ANIM_MS)
        if e >= 1:
            self.anim = None
            return (tw, th), timg
        w = max(1, int(round(w0 + (tw - w0) * e)))
        h = max(1, int(round(h0 + (th - h0) * e)))
        a = img0 if img0.size == (w, h) else img0.resize((w, h), Image.BILINEAR)
        b = timg if timg.size == (w, h) else timg.resize((w, h), Image.BILINEAR)
        return (w, h), Image.blend(a, b, e)

    def _apply(self) -> None:
        if not self.hwnd:
            return
        (w, h), img = self._render()
        x, y = dock_geom(self.wa, self.dock, self.anchor, w, h)
        self.rect = (x, y, w, h)
        self.shown = (w, h, img)
        data = premultiplied_bgra(img)

        # GDI 对象按 (w,h) 缓存复用  原先每帧建/毁 DIB section + 兼容 DC,
        # 30fps 流光帧白付几次调用 + 每帧近 1MB 的分配/释放抖动。稳定态尺寸恒定 → 恒命中;
        # 补间帧尺寸渐变 → 最多攒 24 项, 动画一结束自然落回稳定尺寸那一项。
        ent = self._dibcache.get((w, h))
        if ent is None:
            if len(self._dibcache) >= 24:
                self._free_dibcache()
            hdc = user32.GetDC(0)
            mem = gdi32.CreateCompatibleDC(hdc)
            user32.ReleaseDC(0, hdc)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h        # 负数 = top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            hbm = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), DIB_RGB_COLORS,
                                         ctypes.byref(bits), None, 0)
            old = gdi32.SelectObject(mem, hbm)
            self._dibcache[(w, h)] = (mem, hbm, old, bits)
        else:
            mem, hbm, old, bits = ent

        ctypes.memmove(bits, data, len(data))
        bf = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        hdc = user32.GetDC(0)
        user32.UpdateLayeredWindow(
            self.hwnd, hdc, ctypes.byref(POINT(x, y)), ctypes.byref(SIZE(w, h)),
            mem, ctypes.byref(POINT(0, 0)), 0, ctypes.byref(bf), ULW_ALPHA)
        user32.ReleaseDC(0, hdc)

    def _free_dibcache(self) -> None:
        """放掉缓存的 DIB/DC (尺寸膨胀清空、窗口销毁时都会走这里; 只在窗口线程调用)。"""
        for mem, hbm, old, _bits in self._dibcache.values():
            try:
                gdi32.SelectObject(mem, old)
                gdi32.DeleteObject(hbm)
                gdi32.DeleteDC(mem)
            except Exception:
                pass
        self._dibcache.clear()

    # ---------- 状态机 ----------
    def _begin(self, new: str) -> None:
        """逻辑上立刻切状态, 画面交给:meth:`_render` 补间。

        起点取**当前实际画着的那一帧**, 所以动画中途被打断也只是从当前样子接着走。
        """
        if new == self.state:
            return
        img0 = self.shown[2] if self.shown else self._frames_for(self.state)[1]
        self.anim = (img0, self.rect[2], self.rect[3], time.time())
        self.state = new
        # 换了形态, 悬停高亮必须重算 (进 CARD 时鼠标在哪还不知道)
        self.tag_hot = self.card_hot = self.icon_hot = None

    def _go(self, new: str) -> None:
        self._begin(new)
        self._apply()

    def _tick(self) -> None:
        if self._confirm and time.time() > self._confirm_until:
            self._confirm = ""                 # 两步确认超时 → 按钮自己变回去
            self._paint_card()
        if self.playing and time.time() >= self._slide_due:
            self._slide_due = time.time() + SLIDE_SEC
            # 换壁纸要发 HTTP, 不能占着 16ms 的定时器 (否则补间/拖动当场卡成幻灯片)
            threading.Thread(target=self._do_action, args=("next",), daemon=True,
                             name=THREAD_NAME + "-slide").start()
        cur = self._cursor()
        ldown, rdown = self._down(VK_LBUTTON), self._down(VK_RBUTTON)
        self._gate_desktop(cur)

        if self.dragging:
            if rdown:
                self._drag_to(cur)
            else:
                self.dragging = False
                if self.moved >= DRAG_SLOP:
                    self._snap()
                # 位移不够 = 只是右键点了一下: **什么都不做**。以前这里 PostQuitMessage 退出,
                # 但便签太容易被误点 (panel 一悬停就伸出来, 正好在右键习惯位置), 一点就整块
                # 消失且没有回头路 —— 2026-09-25 的实测问题。退出唯一口径回到托盘菜单 /
                # ``vixiq.exe --quit`` (main.py 的 ``_cleanup``)。
        elif rdown and not self.r_prev and round_rect(cur, self.rect, DRAG_PAD):
            self._start_drag(cur)
        elif ldown and not self.l_prev:
            self._click(cur)
        else:
            self._hover(cur)

        self.l_prev, self.r_prev = ldown, rdown
        if self.anim:                      # 补间每帧重画一次
            self._apply()
        elif self._neon_live():
            # **标题跑马灯 + 霓虹流光**: 相位每 NEON_EVERY 个 tick 推一档并重画。
            # 相位就是**拍数**, 而且**不取模** —— 取模等于宣布「这是个循环」, 而
            # 明确要求「不是单调的循环流动, 而是随机闪烁流动在各处」: 波前的推进与明暗呼吸、
            # 标题的滚动量都由相位驱动。各道波前的速度与呼吸频率彼此不可公约 (见:data:`NEON_SPEED`
            # / :data:`NEON_FADE`), 让它一路涨上去, 图案就永远不回到原点 (见:func:`_flow_band`)。
            self._neon_tick += 1
            if self._neon_tick >= NEON_EVERY:
                self._neon_tick = 0
                self._neon_phase += 1.0
                self._apply()

    def _neon_live(self) -> bool:
        """现在该不该推进相位 —— 卡片开着、在桌面上、没有补间、且定稿件或标题条在。

        补间期间不推: :meth:`_render` 那时也不叠这两层, 推了相位只是让它「跳」一段。
        """
        return (bool(self._neon or self._title) and self.state == CARD
                and self.on_desktop and not self.anim)

    def _start_drag(self, cur) -> None:
        self.dragging = True
        self.drag_from, self.rect_from, self.moved = cur, self.rect, 0
        # 保持当前状态 (右键拖窗口就拖窗口), 别切形状 —— 一按就缩成小条、还停在窗口左上角,
        # 看着就是「拖不动」。
        self.anim = None                   # 拖动期间不补间, 免得画面自己在变

    def _drag_to(self, cur) -> None:
        """右键按住拖动: 窗口原样跟着光标走 (位移小于 DRAG_SLOP 时不算拖动)。"""
        dx, dy = cur[0] - self.drag_from[0], cur[1] - self.drag_from[1]
        self.moved = max(self.moved, abs(dx) + abs(dy))
        if self.moved < DRAG_SLOP:
            return
        self.rect = (self.rect_from[0] + dx, self.rect_from[1] + dy,
                     self.rect[2], self.rect[3])
        user32.SetWindowPos(self.hwnd, -1, *self.rect, SWP_NOACTIVATE)

    def _click(self, cur) -> None:
        if self.state == TAG and round_rect(cur, self.rect):
            i = self._tag_key_at(cur)
            if i is None:
                self._go(CARD)             # 便签上左键 (空白处) = 展开窗口
            else:
                self._tag_action(i)
        elif self.state == CARD:
            if round_rect(cur, self._xbox()):
                # 小叉叉 = 关掉窗口变回便签。**不要动 anchor** —— 小叉叉在卡片右上角
                # (屏幕顶端), 拿它当新锚点会把便签整个拽上去。
                self._go(TAG)
                return
            i = self._icon_index_at(cur)
            if i is not None:
                self._run_action(self._icons[i])
                return
            key = self._action_at(cur)
            if key:
                self._run_action(key)

    # ---------- 便签三键 ----------
    def _tag_key_at(self, cur) -> "int | None":
        """光标落在便签第几个键上 (0=上一张 1=播放 2=下一张)。命中框与画面共用
        :func:`tag_key_box` —— 四个朝向的坐标变换也在那里面, 各算一遍必然错位。"""
        if self.state != TAG:
            return None
        x, y = self.rect[0], self.rect[1]
        for i in range(3):
            bx, by, bw, bh = tag_key_box(i, self.dock)
            if round_rect(cur, (x + bx, y + by, bw, bh)):
                return i
        return None

    def _tag_action(self, i: int) -> None:
        """便签三键的点击入口。

        播放 = **本控件自己的 SLIDE_SEC 定时器** —— Windows 没有公开的「幻灯片走下一张」接口
        (``SPI_SETDESKWALLPAPER`` 只能硬切一张, 也动不了 Explorer 自带的轮播设置),
        所以这里是**硬切**, 没有系统轮播那种淡入淡出。
        """
        if i == 1:
            self._toggle_play()
            return
        key = "prev" if i == 0 else "next"
        self._log(f"桌面小控件: 「{'上一张' if i == 0 else '下一张'}壁纸」…")
        threading.Thread(target=self._do_action, args=(key,), daemon=True,
                         name=THREAD_NAME + "-action").start()

    def _toggle_play(self) -> None:
        """播放 / 暂停 (便签播放键与卡片播放键共用这一套状态, 在窗口线程里就地改)。

        开播时**立刻先换一张**再排下一拍: 幻灯片要是先干等 SLIDE_SEC (30s) 才动第一次,
        按下去看着就像没反应; 而且「当前壁纸文件已被删」时那一下正好把桌面接回壁纸夹里
        的图, 卡片不会一直空着 (否则删掉壁纸后画面变空, 点「下一张」也没有响应)。
        """
        self.playing = not self.playing
        self._slide_due = time.time() + SLIDE_SEC if self.playing else 0.0
        self._log("桌面小控件: 幻灯片" + ("开始播放" if self.playing else "已暂停"))
        # ▶ / ⏸ 便签和卡片上是两幅画, 两边都投后台重画 (旧成品先顶着, 新图几十 ms 就到;
        # 不再当场清缓存同步画, 那一下就是便签播放键「点了顿一拍」的原因)
        self._sync_keys()
        self._post_render(("cards", "tags"))
        if self.state == TAG:
            self._apply()
        if self.playing:
            threading.Thread(target=self._do_action, args=("next",), daemon=True,
                             name=THREAD_NAME + "-slide").start()

    # ---------- 卡片键 (壁纸控制条 + 文字键) ----------
    def _icon_index_at(self, cur) -> "int | None":
        """光标落在壁纸控制条第几颗**图标**键上 (不在就返回 None)。命中框与画面共用
        :func:`icon_key_box` —— 与文字键同理, 两边各算一遍布局迟早错位。"""
        if self.state != CARD:
            return None
        x, y = self.rect[0], self.rect[1]
        for i in range(len(self._icons)):
            bx, by, bw, bh = icon_key_box(i)
            if round_rect(cur, (x + bx, y + by, bw, bh)):
                return i
        return None

    def _btn_index_at(self, cur) -> "int | None":
        """光标落在第几个**文字**按钮上 (不在按钮上返回 None)。

        命中矩形与画面共用:func:`btn_box`, 只把卡片内坐标叠上窗口自己的左上角 ——
        两边各算一遍布局, 迟早错位成「点左边界点了右边界」。
        """
        if self.state != CARD:
            return None
        x, y = self.rect[0], self.rect[1]
        for i in range(len(self._actions)):
            bx, by, bw, bh = btn_box(i)
            if round_rect(cur, (x + bx, y + by, bw, bh)):
                return i
        return None

    def _action_at(self, cur) -> "str | None":
        """光标落在哪个按钮上 → 动作键 (不在按钮上返回 None)。"""
        i = self._btn_index_at(cur)
        return self._actions[i] if i is not None else None

    def _run_action(self, key: str) -> None:
        """按钮点击入口 (在窗口线程里跑)。要发 HTTP / 开浏览器的动作一律甩给后台线程 ——
        16ms 的定时器不能被网络请求堵住, 否则补间和拖动立刻卡成幻灯片。

        2026-09-29: 点击不再**无条件**清缓存重画 —— 「在库中查看/所在文件夹/上一张/下一张」
        这些动作画面根本不变, 原先也先来一张 178ms 的整卡重画 (就是「点按钮顿一下」的来源)。
        只有画面真会变的键 (两步确认 / 档位 / 播放) 才投渲染任务。
        """
        if key == "recycle" and self._confirm != self._key[0]:
            # 破坏性动作走两步 (网页版那边是 confirm() 弹窗): 先点亮成「确认移出？」,
            # CONFIRM_SEC 内再点一次才真执行。哪怕进的是回收站, 也不该一抖手就删掉。
            self._confirm, self._confirm_until = self._key[0], time.time() + CONFIRM_SEC
            self._paint_card()
            return
        if self._confirm:                    # 有确认态挂着 (点了别的键/二次确认) → 摘掉
            self._confirm = ""
            self._paint_card()
        if key == "shuffle":
            # 「顺序 / 随机」纯本地状态 (没有任何 IO), **必须在窗口线程里就地改** ——
            # 窗口是线程亲和的, 从别处碰画面会出乱子。
            self.shuffle = not self.shuffle
            self._log(f"桌面小控件: 换图档位 → {'随机' if self.shuffle else '顺序'}")
            self._paint_card()              # 图标与缓存键都跟着档位走 (后台重画)
            return
        if key == "play":
            # 播放 / 暂停也是纯本地状态, 同理就地改 (开播会顺手甩一个后台 next 出去, 见 _toggle_play)
            self._toggle_play()
            return
        threading.Thread(target=self._do_action, args=(key,), daemon=True,
                         name=THREAD_NAME + "-action").start()

    def _wall_path(self, name: str) -> str:
        """换图接口的路径 —— 随机档带上 ``?shuffle=1`` (服务端按它随机挑一张)。"""
        return f"/api/wallpaper/{name}" + ("?shuffle=1" if self.shuffle else "")

    def _do_action(self, key: str) -> None:
        """真正干活的 (后台线程)。结果写进活动日志 —— 卡片太小, 塞不下提示语。

        2026-09-29: next/prev/recycle 的响应里**直接带着新壁纸的 info** (服务端换完就地
        trace), 不用再拉一轮 ``/current`` —— 省掉一次 HTTP 往返 + 整套 md5/尺寸重算,
        点「下一张」到卡片更新的链路短一截。
        """
        info = self._info or {}
        art, f = info.get("artwork") or {}, info.get("file") or {}
        try:
            if key == "folder":
                self._log("桌面小控件: 「所在文件夹」" + self._rac(
                    self._post("/api/wallpaper/open-folder")))
            elif key == "recycle":
                r = self._post("/api/wallpaper/recycle")
                self._log(f"桌面小控件: 「移出壁纸」({f.get('name') or '?'}) " + self._rac(r))
                self._adopt_action_result(r)
            elif key == "library":
                self._open_url(f"{self.base}/#library?only={art.get('artwork_id')}")
            elif key == "next":
                r = self._post(self._wall_path("next"))
                self._log("桌面小控件: 「下一张壁纸」" + self._rac(r))
                self._adopt_action_result(r)
            elif key == "prev":
                r = self._post(self._wall_path("prev"))
                self._log("桌面小控件: 「上一张壁纸」" + self._rac(r))
                self._adopt_action_result(r)
        except Exception as e:
            self._log(f"桌面小控件: 「{key}」失败: {e}")

    def _adopt_action_result(self, r: dict) -> None:
        """换图接口带回来的 info 直接采用 (免二次拉取); 没带上 (老服务端/失败) 走老路兜底。"""
        if r.get("status") == "ok" and isinstance(r.get("info"), dict) \
                and r["info"].get("active"):
            self._pending = (info_key(r["info"]), r["info"],
                             fetch_thumb(self.port, r["info"]))
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)
            return
        self._force_refresh()              # 成功就当场换成下一张, 失败也能刷新回原样

    def _post(self, path: str) -> dict:
        """POST 一个本地接口。服务端用 4xx/5xx 报业务错误 (例如「这张就是图库里的文件」),
        所以 HTTPError 也要把 body 读出来 —— 否则只剩「HTTP 400」, 猜不出为什么。"""
        req = urllib.request.Request(self.base + path, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.load(e)
            except Exception:
                return {"status": "error", "msg": f"HTTP {e.code}"}
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    @staticmethod
    def _rac(r: "dict | None") -> str:
        r = r or {}
        return "成功" if r.get("status") == "ok" else f"没成功: {r.get('msg') or r}"

    @staticmethod
    def _open_url(url: str) -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _hover(self, cur) -> None:
        if not self.on_desktop:
            return                          # 不在桌面: 不伸不收, 整块都是透明的
        if self.state == CARD:
            # 窗口自身是常驻的 (移开鼠标也不收), 但**键**要跟着鼠标亮 —— 悬停「感应反馈」
            # 就是这个: 指到哪一颗, 哪一颗浮出 accent 荧光。控制条的图标键在分隔线上方、
            # 文字键在下方, 两块互不重叠, 分别算各自的 hot。
            bi, ii = self._btn_index_at(cur), self._icon_index_at(cur)
            if bi != self.card_hot or ii != self.icon_hot:
                self.card_hot, self.icon_hot = bi, ii
                self._apply()
            return
        if self.state == TAG:
            # 存活区 = 便签 ∪ 边缘线 (后者沿边更长, 少了它贴边时会疯狂伸缩)
            alive = (self._rect_of(tag_size(self.dock)),
                     self._rect_of(self.edges[self.dock].size))
            if not any(round_rect(cur, r, TAG_PAD) for r in alive):
                self.tag_hot = None
                self._go(EDGE)
                return
            i = self._tag_key_at(cur)
            if i != self.tag_hot:
                self.tag_hot = i
                self._apply()          # 键帽 + 荧光只画在鼠标指着的那一颗上
            return
        if round_rect(cur, self.rect):        # EDGE: 停在边缘线上就伸出来
            self._go(TAG)

    def _snap(self) -> None:
        """松手: 归到离窗口中心最近的那条边, 并记住锚点。"""
        self.dock, self.anchor = nearest_dock(self.wa, self.rect)
        self._post_render(("tags",))      # 便签组按朝向渲染, 换边后重画一套
        self._go(EDGE)
