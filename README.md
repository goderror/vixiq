# Vixiq

一个本机运行的 Pixiv 图片资产管理工具（Web 版）：下载 → 图库管理 → 以图搜源 → 本地以图搜图 → 作者管理，可打包成 exe 双击即用。**100% 本地运行，数据不落 C 盘系统盘，不调用任何第三方服务器。**

> 内部开发/排错记录（DEBUG_HISTORY.md）只保留在本地，不随本仓库分发。

---

## 功能一览

八个页签（网络查询 / 下载任务 / 反搜来源 / 图库管理 / 回收站 / 作者管理 / 统计 / 设置）+ 一组桌面层能力。

| 页签 | 说明 |
|------|------|
| 🌐 网络查询 | 作品 ID / 作者 URL / 关键字搜索；关键字结果按 24 条连续切片「换一批」（跨页自动续取 + 页级缓存防风控）；无输入时回落到「🏆 排行榜 / ✨ 为你推荐」两个来源可切换；一键「🚫 隐藏 AI 作品」 |
| 📥 下载任务 | 独立任务页：开始所有未完成 / 停止 / 重试失败 / 清空当前栏（四栏筛选各清各的，进行中的会被中止）/ 缩略图 / 排序 / 四栏筛选；队列「先入先跑」，总进度按任务条数推进；动图（ugoira）合成 GIF |
| 🔎 反搜来源 | 文件名含作品 ID 直查；SauceNAO 以图搜源；可勾选单张反搜 / 反搜全部 / ➕ 添加任意本地图片；无 ID 图片转反搜收件箱 |
| 📚 图库管理 | 卡片网格 / 列表双视图、单击选择 / 双击大图预览、Shift 范围多选、拖拽框选（普通=追加、Ctrl=取消）、框选滚动、筛选 / 排序 / 分页（每页 60 张，含按修改时间排序）、标签 / 批量编辑 / 批量删除、拆分与合并作品、同步、以图搜图、一键自检、缩略图缓存、文件大小与原始长宽显示、**默认只显示非 R18**；页内还有「🖼️ 当前壁纸」卡片，读注册表拿到桌面正在播的那张并做四档溯源（库内文件 / MD5 字节一致 / 文件名 ID / ORB 图像） |
| 🗑️ 回收站 | 软删除双层保护：误删可恢复、同名绝不覆盖、彻底删除送系统回收站 |
| 👤 作者管理 | 作者搜索 / 筛选 / 排序 / 分页 / 改名（留曾用名）/ 主页跳转 / 来源链接 / 配对认领 |
| 📊 统计 | 概览卡片 + 来源 / 作者状态 / 作品类型 / R18 / 标签 Top 60 / 作者作品数分布 |
| ⚙ 设置 | 五组卡片：账户 · 桌面与壁纸 · 浏览与下载 · 日志与记录 · 维护（含 🩺 体检与审批、ℹ️ 版本与构建） |

**桌面层（打包版的日常入口）**

| 模块 | 说明 |
|------|------|
| 🖼️ 桌面小控件 | 贴边的便签（缩略图 + 播放键 + 上一张 / 下一张 / 换图档位）+ 悬停展开的卡片（三颗文字键：在库中查看 / 所在文件夹 / 移出壁纸）；**只在桌面（Progman / WorkerW）露出**，浏览其他窗口时不会跳出来 |
| 🖼️ 壁纸夹 / 壁纸库 | 选一个壁纸夹做幻灯片源；「⬆ 从图库生成壁纸」按尺寸（≥1920×1080）与比例（偏离 16:9 ≤10%）门槛挑出够格的页，原样复制并登记，幂等不重复 |
| ✨ 壁纸文案 AI | 接 OpenAI 兼容接口（DeepSeek / Moonshot / 智谱 / 通义 / OpenAI 等），上传当前壁纸生成「标题 + 一句氛围话」显示在小控件卡片上；落盘缓存，同一张图只生成一次 |
| 🖥 托盘常驻 | 托盘图标 + 菜单；开关：托盘常驻 / 启动自动开浏览器 / 桌面壁纸小控件 / 🔔 壁纸切换提示 / 🚪 关页面即退出 |
| 🛡 安全模式 | 设置页开关（持久化）：开启后 R18 内容在搜索结果 / 图库中**模糊打码 + 🔞 徽标**，双击大图也需点「🔒 显示」才解锁，适合公共场合；「涩图」文件夹图片已自动归类为 R18 |
| 🎀 回到顶部 | 右下角浮动按钮，滚动后淡入，点击平滑回顶 |

---

## 快速开始（开发运行）

```bash
# 1. 建虚拟环境（Python 3.11+ 均可，requirements 已带版本标记；3.14 实测可用）
python -B -m venv .venv

# 2. 装依赖
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 3. 运行（自动开浏览器到 http://127.0.0.1:7790/，端口见配置）
.venv\Scripts\python.exe main.py
```

首次使用在设置页登录 pixiv，登录态持久化，之后自动无头登录。

### 第一次使用（上手路径）

1. **登录**：设置页 →「账户」→ 登录 Pixiv 账号。登录态写 `cookies.bin`（DPAPI 加密），之后自动无头登录；**所有联网功能都挂在登录态上**，cookies 过期就要重新登一次。
2. **配数据目录**（可选）：默认数据落在程序同级的 `data/`；想放别的盘就改 `config/user.json` 的 `paths.data_root_override` / `temp_root_override`。
3. **查询 → 加入清单**：查询页搜作品 ID / 作者 URL / 关键字，卡片上点「加入清单」。
4. **下载**：下载任务页 →「▶ 开始所有未完成」。
5. **看图库**：图库管理页筛 / 排 / 搜；顺手点「🩺」跑一次体检，缺作者 / 缺文件的项会列出来。
6. **桌面层**（可选）：设置页 →「桌面与壁纸」配一个壁纸夹 → 到「🚀 启动与托盘」把「🖼️ 桌面壁纸小控件」和「🖥 托盘常驻」点亮。
7. **AI 文案**（可选）：设置页 →「✨ 壁纸文案」填接口地址（到 `/v1` 为止）/ 模型名 / 密钥 →「🔌 测试连接」→「⬆ 上传当前壁纸」。

## 打包发布（独立版本）

```bash
# 需要先把 msedgedriver.exe 放到 resources/（见下方「未随仓库分发的文件」）
.venv\Scripts\python.exe -m PyInstaller build.spec --noconfirm
# 产物在 dist/vixiq/，整个文件夹拷走即可双击运行（windowed，无控制台）
```

**打包后必做（重要）**：
1. 把开发期的 `config/user.json` 复制进 `dist/vixiq/config/user.json`，否则发布版会把数据写到 exe 同级的 `data/`（C 盘）。本项目 `config/user.json` 已将数据目录重定向到 F 盘（见下文「存储结构」）。
2. **验证网页能打开**：启动 exe 后浏览器访问 `http://127.0.0.1:端口/` 必须返回 200 且正常渲染（不能只测 API）。`web/static/` 已由 build.spec 打包进 `_internal/web/static`——若改动 build.spec 的 `datas`，务必保留 `('web/static', 'web/static')` 这一行，漏掉会导致 exe 根页面 404 "not found"。

分享发布包时请删除 `dist/vixiq/config/`（含你的本机配置），下载方按自己的机器重新配置。

### 公开发布清单（分享给别人前逐条核对）

默认打出来的 `dist/vixiq/` 是**本地自用版**，里面带画师 示例画师 的 Q 版初音立绘（`web/static/to-top.png`），**不可公开分发**。发布要走发布模式，并建议打到单独目录，免得覆盖你日常自用的那份：

```powershell
$env:VIXIQ_RELEASE=1
.venv\Scripts\python.exe -m PyInstaller build.spec --noconfirm --distpath dist-release
$env:VIXIQ_RELEASE=$null     # 打完记得撤销，否则后续默认都打成发布版
```

逐条核对：

- [ ] 产物里**没有**立绘：`Get-ChildItem dist-release\vixiq\_internal\web\static` 应只有 `app.js / style.css / index.html / favicon.ico / favicon.svg`
- [ ] 删掉 `dist-release\vixiq\config\`（含本机路径），下载方自己配
- [ ] 启动 exe，浏览器打开 `http://127.0.0.1:<端口>/` 返回 200 且正常渲染（**不能只测 API**）
- [ ] README 功能一览与实际一致（改了功能记得同步这一节）
- [ ] `git status` 干净、`git log -1` 是本次发布点，然后 `git tag v1.0.0`

---

## 未随仓库分发的文件（gitignore）

这几个文件**不影响代码运行**，缺失时程序会自动降级或回退，因此没放进版本库：

| 文件 | 为什么不分发 | 缺失时怎么办 |
|------|--------------|--------------|
| `resources/msedgedriver.exe` | 23 MB 二进制，且版本必须与本机 Edge 大版本匹配，随仓库分发没意义 | 从 [Microsoft Edge WebDriver](https://developer.microsoft.com/microsoft-edge/tools/webdriver/) 下同大版本放进 `resources/`；不放进也能跑，Selenium 会自行下载匹配驱动 |
| `web/static/to-top.png` | 「回到顶部」按钮的 Q 版立绘源自他人 pixiv 作品，版权原因不进公开发布包 | 按钮自动降级为纯色圆形 + 白色箭头（`#to-top.no-img`），功能不受影响 |
| `config/user.json` | 含本机路径（数据目录重定向），属个人配置 | 程序按 `resources/default.json` 默认值启动（数据落程序同级 `data/`） |

需自备的 Python 依赖见 [requirements.txt](requirements.txt)（冒烟 T7 会校验「源码 import 的第三方库」都在其中，防止漏声明）。

---

## 存储结构（C 盘程序 / F 盘数据）

**程序本体在 C 盘**（源码或打包后的 exe 目录），**运行时数据全部在 F 盘**。划分由 `utils/paths.py` 统一分发，无硬编码路径：

```
C:\...\pixiv-toolkit\（开发目录，仓库名不变）或 dist\vixiq\（发布）
├── main.py / vixiq.exe              程序入口
├── core/  db/  utils/  web/         源码（发布版打进 _internal）
├── resources/                       只读资源：default.json、xpaths.json、msedgedriver.exe、icon.ico、icon.svg、splash.png
├── config/user.json                 个人配置（gitignore，发布版需手动复制）
└── autotest/                        开发/测试专区（发版删掉即清理）

F:\pixiv-toolkit-data                ← 数据根（data_root_override）
├── pixiv.db                         主数据库
├── cookies.bin                      登录态（DPAPI 加密）
├── account.json / self_check.json / reverse_results.json
├── img\                             图库原图
├── thumbs\                          480px 缩略图缓存
├── logs\                            运行日志（只保留最近 7 天，启动时自动清理）
├── feature_db\                      以图搜图特征库（.npz）
├── input\                           反搜输入目录
├── backup\  tmp\  temp_upload\      备份 / 临时

F:\pixiv-toolkit-temp                ← 临时根（temp_root_override）
└── browser\                         Selenium 临时 profile（随用随删）
```

**配置机制**：`resources/default.json`（只读默认）+ `config/user.json`（用户覆盖，深度合并，`utils/config.py` 带 `_canonicalize` 兜底）。重定向靠两个字段：

```json
"paths": {
  "data_root_override": "D:/vixiq-data",
  "temp_root_override": "D:/vixiq-temp"
}
```

改任何配置字段必须同步 `default.json` 与 `_canonicalize()` 兜底。

---

## 隐私与安全

- **100% 本地运行**：图片、特征库、登录态、数据库、日志只落本机 F 盘，不主动上传任何内容。
- **Cookies 加密**：登录态写 `cookies.bin`，Windows DPAPI（AES-256，密钥由系统按当前账号密码托管）加密 + 文件 ACL 锁定；换系统/改密码后旧 cookies 无法解密，重新登录即可。
- **.gitignore**：忽略 `config/user.json`、`data/`、`*.db`、`logs/`。

## 数据与缓存清理机制（三层自动收敛）

1. **驱动生命周期清理**：Selenium 退出时立即删除临时 profile（`F:\pixiv-toolkit-temp\browser\`）。
2. **临时区收敛**：启动清 >24h 旧文件；超 5GB 按旧到新删；进程退出（atexit）时激进清理。
3. **日志收敛**：启动时删除 logs/ 下超过 7 天的日志。

---

## 常见问题

- **端口被占**：默认 7790（本项目 user.json 用 7810）；有活实例直接复用，僵尸端口自动清理顺延。
- **Edge driver 版本不匹配**：`resources/msedgedriver.exe` 须与 Edge 大版本一致；启动报错时升级 Edge，程序可回退到 Selenium 自动下载匹配驱动。
- **数据跑到 C 盘了？**：检查 `config/user.json` 的 `data_root_override` / `temp_root_override` 是否存在且指向 F 盘；发布版请确认 `dist/.../config/user.json` 已复制。

---

## 开发指引

- **冒烟测试**：`.venv\Scripts\python.exe -m autotest.smoke_all`，改完代码立即跑，全 PASS 才继续（当前 T1~T133，125 用例，全程离线、约 49 秒）。报告落在 `autotest/SMOKE_REPORT.md`。
- **临时诊断脚本**：一律放 `autotest/_live_*.py`，用完即删；调试输出不得散落根目录。
- **文档约定**：每完成功能/修复，同步更新 README（说明）与 DEBUG_HISTORY.md（历史条目）。

---

## 免责声明

- **非官方项目**：Vixiq 是个人开发的本机工具，与 pixiv Inc.（ピクシブ株式会社）**没有任何隶属、赞助或认可关系**，也不是官方客户端。
- **商标**：「Pixiv」「pixiv」为 pixiv Inc. 的商标或注册商标，本项目仅在描述用途时提及（如说明数据来源、登录对象）；**项目名称「Vixiq」与图标均为原创，未使用其商标元素**，配色也刻意避开 pixiv 官方蓝。
- **使用范围**：仅供个人学习、研究与**备份自己有权访问的内容**。请自行遵守 [pixiv 利用規約](https://www.pixiv.co.jp/policy/) 与所在地区法律法规，不要用于批量转载、二次分发、破解付费内容或任何商业用途。
- **风险自担**：平台接口变更、风控策略调整都可能让部分功能失效甚至影响账号；使用本工具产生的任何后果由使用者自行承担，作者不承担任何责任。
- **按现状提供**：软件按「现状」提供，不附带任何明示或默示担保，包括但不限于适销性与特定用途适用性。

## 许可

[MIT License](LICENSE) © 2026 goderror
