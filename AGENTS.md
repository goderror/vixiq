# AGENTS.md · 接手须知

> 给后续接手本项目的 Codex / 其他代理看。最后更新 2026-11-17（当前前端版本 `20261117d`，上一提交 `6014800`，本轮壁纸页顶部调整尚未提交）。
> 产品面向普通用户的说明在 [README.md](README.md)，历史排障在 [DEBUG_HISTORY.md](DEBUG_HISTORY.md)，
> 表结构在 [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)，版本记录在 [CHANGELOG.md](CHANGELOG.md)。

---

## 0. 先读这三条

1. **一次只改一项**。用户明确要求「我一项项跟你说」，不要一次实现多个需求。
   上一轮一口气改了 7 项，被整体回退（见 §4.1）。有歧义先问，不要自行扩大范围。
2. **改完必须验证**：`py_compile` + 全量冒烟（当前 135 用例，必须全过，2026-11-16 已 135/135 通过），同步相应程序/资源并验收安装版；仅更新交接文档按 §2.2 核对，不重启或部署程序。
3. **不碰安全红线**（§7）：绝不自动删文件、R18 不加载真实像素、不 force push。

---

## 1. 项目与环境（本机）

- 工作区：`d:\__workspace__\项目\vixiq-dev`
- 解释器：`.venv\Scripts\python.exe`（Python 3.14）
- 技术栈：Flask + 原生 JS（`web/static`，无构建步骤）+ SQLite + Selenium（Edge）
- 本机配置 `config/user.json`：
  - 源码测试端口 **7811**，仅监听 `127.0.0.1`
  - 数据目录 `<项目根>\_dev_runtime\data`（独立图库、SQLite、日志、cookies）
  - 临时目录 `<项目根>\_dev_runtime\temp`（含源码浏览器缓存）
  - 测试壁纸夹 `<项目根>\_dev_runtime\wallpapers`，不连接真实壁纸夹
  - 源码不自动启动托盘、桌面小控件或浏览器；测试配置备份在 `_dev_runtime\backups`，整个目录不入库
  - 本地 `launcher.isolated=true`：启动不执行全局浏览器清理，`--quit` 只通知测试端口；源码按工作区单独持有单实例锁，可与安装版并行。
  - 安全模式 **已开启**
- 安装版（用户日常使用）：`D:\Program Files\Vixiq\vixiq.exe`，端口 **7810**，数据 `F:\Vixiq\data`、临时目录 `F:\Vixiq\temp`，由安装目录自己的 `config/user.json` 决定。
- 源码运行：`.venv\Scripts\python.exe main.py`（读项目内 user.json，端口 7811）；禁止复制真实数据库或 cookies 到测试区。

### 关键目录

| 路径 | 作用 |
|---|---|
| `main.py` | 启动器：Flask 服务、托盘、桌面小控件、心跳守护、孤儿进程清理 |
| `web/server.py` | 全部 HTTP 接口（约 1 万行，按功能分块注释） |
| `web/static/{index.html,app.js,style.css,wallpaper-admin.js}` | 前端主页面代码；无打包，改完升版本串即可 |
| `web/static/{management-drawer-state.js,management-toolbar.js,management-toolbar.css}` | 三个管理页共用的吸顶抽屉状态机 / 控制器 / 样式（2026-11-16 新增，见 §4.3、§6.2） |
| `web/wallpaper_admin.py` | 壁纸管理整理接口（与 `web/server.py` 配合） |
| `core/wallpaper_trace.py` | 壁纸溯源（只读）：当前壁纸、上一张/下一张、MD5 索引 |
| `core/wallpaper_index.py` | 壁纸夹归属索引（`wallpaper_files` 表）：扫描、列表、重复组 |
| `core/wallpaper_lib.py` | 从图库按尺寸/比例生成壁纸 |
| `db/store.py` | 数据访问层；`db/migrations/NNN_*.sql` 迁移 |
| `utils/proc.py` | **所有子进程统一入口**（隐藏控制台窗口），见 §6.1 |
| `autotest/smoke_all.py` | 全量冒烟，135 个用例；新增功能必须加断言 |

---

## 2. 运行、验证、打包、部署

### 2.1 验证（每次改完都要做）

```powershell
.venv\Scripts\python.exe -m py_compile web\server.py core\wallpaper_index.py
.venv\Scripts\python.exe -m autotest.smoke_all        # 135 用例必须全过
```

- 冒烟无参数、跑全量约 50 秒；输出报告在 `autotest/SMOKE_REPORT.md`。
- 只想快速验证某个用例，可直接 import 调用：

```powershell
.venv\Scripts\python.exe -c "from autotest.smoke_all import t137_wallpaper_index_and_admin_page as t; t(); print('OK')"
```

- 改了前端（`web/static/*`）**必须升版本串**（§3.1），否则冒烟会失败。

### 2.2 打包与部署到安装版

**仅更新交接文档**：核对当前代码、既有验证日志和部署清单即可；无需重新运行全部冒烟、打包、部署或重启。代码和前端改动仍按 §2.1 验证。

**仅改前端**：先在独立源码/合成数据环境验收并提升版本，再备份原资源，原子同步到隔离候选包、`dist/vixiq/_internal/web/static/` 和 `D:/Program Files/Vixiq/_internal/web/static/`。新资源先到位，`index.html` 最后替换；不用停止或重启安装版。当前 7 个资源：`app.js`、`style.css`、`index.html`、`wallpaper-admin.js`、`management-drawer-state.js`、`management-toolbar.js`、`management-toolbar.css`。核对资源哈希，并确认各自 `config/user.json` 前后哈希不变。

本机本轮执行记录：`_dev_runtime/diagnostics/deploy_management_drawer.py` 与 `management-drawer-deployment.json`。它们在忽略目录中，是本轮本地记录；后续复用前须核对版本、资源清单和目标路径，不作为通用发布脚本盲目执行。

**后端或打包配置变更**：先完成源码验证，在项目隔离目录生成候选包，不要先停正在使用的安装版：

```powershell
.venv/Scripts/python.exe -B -m PyInstaller build.spec --noconfirm --distpath _dev_runtime/packages --workpath _dev_runtime/pack-build
```

本地自用不设 `VIXIQ_RELEASE=1`，避免剔除自用立绘。候选包必须使用独立端口（本机验收为 7813）与 `_dev_runtime` 内的测试 data/temp/wallpapers；禁止复制真实数据库或 cookies 到测试区。

当前冻结版与安装版共享全局单实例互斥，不能承诺二者并行启动。完成构建与源码/静态检查后，在用户已授权的短暂停启窗口中退出安装版、用合成数据验收冻结候选；候选退出后再替换程序并恢复安装版。验收失败应恢复旧程序，不自动覆盖业务数据库。更新程序不得复制测试配置覆盖安装目录的 `config/user.json`。

所有升级使用新的唯一备份目录，保留旧程序与旧备份；未经明确指令不删除备份。部署后核对程序/资源、配置哈希和健康状态；汇报源码、dist、安装版与 GitHub 四处状态，未收到推送指令则注明 GitHub 尚未推送。



### 2.3 提交与推送

- 可以直接 `git push origin master`（仓库规则已配 Bypass）。
- **不要** `git reset --hard` / `push --force`。回退用 `git revert`（保留历史，见 §4）。
- 只提交本次相关文件。以下内容**不要提交**：
  - `tools/cloudflared.exe`（55 MB 临时二进制）
  - `main.py` / `resources/default.json` / `utils/config.py` 如果只是换行差异（`git diff --numstat` 为空）

---

## 3. 前端约定

### 3.1 版本串

`index.html` 里 `style.css?v=` 与 `app.js?v=` 必须同值，**且 `autotest/smoke_all.py` 里有 4 处同样的断言**。
改前端后一起升（当前源码 `20261117d`，格式 `yyyyMMdd+字母`）。漏一处冒烟必失败。
2026-11-16 新增的三个共享菜单资源同样带版本串（`management-drawer-state.js` / `management-toolbar.js` / `management-toolbar.css`）。

### 3.2 文案与风格

- 界面文案**不使用第二人称**、不口语化；中文注释。
- 图标若属于「文字默认呈现」码位（如 🗑 🖼 🏷），用户可见处必须带 U+FE0F（`🗑️`），
  冒烟 T98 会查。
- 前端是原生 JS，函数多为全局 `window.xxx`；DOM 事件用委托，避免拼 `onclick="fn('...')"` 时被文件名里的引号打断。

### 3.3 安全模式（重要）

- 全局变量 `safemode` 来自 `/api/safemode`；R18 **连缩略图都不请求**，显示 `R18_MASK_SVG` 占位图。
- 已知 R18 状态用 `imgSrc(url, {is_r18})`；**不确定是否 R18** 用 `safeThumb(url, w)`（fail-safe，一律遮）。
- 新增任何画图片的页面都必须接入这两者之一，否则安全模式下会泄露 R18 像素。

---

## 4. 当前状态（务必先理解）

> 历史撤销与当前完成必须分开看：§4.1 是 2026-11-16 当时用 `git revert` 回退到的基线，
> §4.2 以下是后续逐项重新做完的现状。代码状态已经不是 `f52054b`。

### 4.1 历史记录：当时的回退（仅说明当时基线）

用户说「一口气说太多你做不好」，要求在「还得改进」之前重来。
`dab2e19` 已用 `git revert` 撤销了下面两个提交（**回退当时的代码状态 = `f52054b`**）：

- `067a631` 壁纸查重改同图口径 + 壁纸管理界面完善 + 图库工具栏分组/顶部固定
- `3e5e453` 上述改动的 bug 修复

被撤销的需求清单（**2026-11-16 已逐项重新做完，当前代码已包含全部 7 项**，见 §4.2～§4.6；
锁屏按用户要求仍不动）：

1. 重复检测口径：2026-11-16 已在源码单独重做；同作品不同图不重复，同图的原图/裁剪版归组。**已完成**。
2. 设壁纸查重提示里提供「选择已有壁纸作为当前壁纸」（缩略图选择弹窗）。**已完成**。
3. 壁纸管理界面完善（搜索/排序/单张操作：设为壁纸、在库中查看、删除）。**已完成**。
4. 图库工具栏按性质分组、去掉「批量」、下载+修复合并为「重新下载 ▾（补完作品/修复原图）」。**已完成**。
5. 分页栏显示「共 X 个作品 · Y 张图片」，原位置显示「已选 X 个作品 · Y 张图片」。**已完成**。
6. 三个管理页顶部菜单固定 + 下滑收起（注意 §6.2 的闪烁坑）。**已完成**，见 §4.3。
7. 导航顺序：图库 → 壁纸 → 作者。**已完成**（当前导航为 图库管理 → 壁纸管理 → 作者管理 → 统计 → 设置 → 回收站）。

回退前的未提交改动与新测试备份在 `build\revert_backup_20261116\`（`working.diff` +
`test_wallpaper_management.py`），可作参考，**不要直接套用**，也**不要删除**这份备份。

### 4.2 当前保留的功能

- 壁纸管理页（`cdc01d0` 引入）：tab 紧邻「图库管理」，位于「作者管理」之前
  - 归属索引表 `wallpaper_files`（迁移 `021_wallpaper_files.sql`、`022_wallpaper_management.sql`）
  - 快扫（文件名作品 ID / MD5）+ 深度扫描（ORB，复用 `_wall_orb_candidates` 单例）
  - 当前源码的重复组按具体图片归属（作品 ID + 图片页号）或非空 MD5 聚合，包含裁剪版本；仅作品归属不判重。旧的只识别到作品的条目需深度扫描补出图片页号，不能默认当作第 1 张。
  - 用户确认移出/从库中删除 → 应用回收站（保存原文件、归属、收藏和分组，可恢复）；从磁盘删除 → Windows 回收站。当前桌面使用的文件须先切换再重命名/删除。
- 大图查看器「设为壁纸」是左下角圆形图标按钮，点击先关闭查看器再进裁剪
- 导航（从左到右）：图库管理 → 壁纸管理 → 作者管理 → 统计 → 设置 → 回收站（最右角落，仅图标）
- 子进程全部隐藏控制台窗口（`e3f1ea4`）

共享菜单见 §4.3，防回跳边界见 §4.4；其余当前功能和部署状态见 §4.5，图库工具栏与统计见 §4.6。

### 4.3 三管理页共享吸顶抽屉（2026-11-16，版本 `20261116i`）

共享资源（三页共用，不要各写一份）：

- `web/static/management-drawer-state.js`：无 DOM 依赖的开合状态机。
- `web/static/management-toolbar.js`：滚动/滚轮/键盘/触摸/拖动滚动条/分页/tab 切换的控制器。
- `web/static/management-toolbar.css`：吸顶与 `management-compact` 样式、`--management-extra-limit`。

布局事实：**滚动容器是 `main`**（`main` 才是 `scrollTop` 与 scroll 事件主体），sticky 吸顶在 `main` 内部完成，
不要改用 `window` / `document` 的滚动假设。

开合规则（§6.2 的统一实现）：

- 迟滞阈值：>160 收起、<80 展开；class 没变不写 DOM。
- 滚动本身只是触发条件，真正开合由**用户方向意图**决定（`intent` 带 TTL，1800ms）。
- `beginLayout()` / `settle()` 保护：切 class、改 `--management-extra-limit`、换 tab、resize 都会引起回流
  与 `scrollTop` 变化，必须包在布局保护里；settle 用双 `requestAnimationFrame` + 世代号 `settleGen` 只认最新一次。
  否则菜单变矮会把 `scrollTop` 钳制下去，被当成「用户上滑」又展开，在临界点反复循环。
- 手动展开（点「展开菜单」）后记录 `manualTop`，必须再往下滑 **60px** 才允许自动收回；回到顶部或点按钮恢复。
- 搜索、视图切换、选中项动作、分页**常驻可见**；低频信息筛选放进 `extra` 抽屉内部（内部滚动不触发开合，
  焦点在 `extra` 内时也不自动收起）。
- 三个 tab 共享同一套状态机制；切页时回顶并强制展开，但**不清空**各页的搜索条件与选择。

### 4.4 本轮实际发现并修好的两个边界（重要）

1. **短列表把 `scrollTop` 压 0**：输入过滤让列表变短后，浏览器把 `scrollTop` 钳制到 0，
   这不是用户上滑。此时必须保持收起，不能自激展开（`layout` 保护 + 意图驱动 + 只认真实位移）。
2. **Home / 向上输入可能根本不产生 scroll 事件**：已在顶部时按 Home、或在被钳制为 0 的状态下向上滚，
   不会再滚动，也就没有 `scroll` 事件。控制器**必须消费 `state.input(dir, top)` 的返回值并立即 `paint()`**，
   不能只写 `intent` 然后干等 scroll（`setIntent()` 已按此实现，`#to-top` 点击同理）。

这两条已包含在 T143 的 14 项 Node 回归里（开合状态与控制器集成），其中就覆盖「控制器漏接 input 返回值」。

### 4.5 本轮已重新授权并实现（2026-11-16）

- 重复提示中的已有版本选择器：安全缩略图、原图预览、精确标出当前桌面版本、确认设置、失败可重试。
- 紧凑壁纸卡片、右上更多操作、分页、搜索/排序、作品/自定义分组叠加筛选、未归属筛选、多选操作条。
- 桌面设置、作品归属（可只指定作品，不伪造图片页号）、分组管理、文件信息/原图/来源链接、单文件重命名、收藏、标记、应用回收与恢复、Windows 回收。
- 手动归属与解除归属优先，原文件内容没变时不被扫描覆盖。作品壁纸数量实时统计。
- 锁屏：用户 2026-11-16 明确要求「先不动锁屏」，未接入入口、接口或系统操作。
- 新界面脚本 `web/static/wallpaper-admin.js`，后端整理接口 `web/wallpaper_admin.py`。七个资源显式版本统一为 `20261116i`；后端 mtime 缓存指纹目前只覆盖 `app.js`、`style.css`、`wallpaper-admin.js`，新管理菜单三个资源通过显式版本号失效，修改时必须一起升版本，不能只依赖 mtime。
- T140（已有壁纸 9 项）、T141（完整管理 18 项）纳入全量；2026-11-16 新增 T143（三管理页共享吸顶抽屉：6 项 Python 结构/接线检查 + 14 项 Node 开合状态与控制器集成回归），全量共 **135 个用例**；系统桌面设置与回收使用测试替身。
- 冒烟实测（2026-11-16，日志 `_dev_runtime\diagnostics\management-drawer-verified-smoke.log`）：**135 用例 / PASS=135 / FAIL=0 / 58.00s**。
- 部署核对（`_dev_runtime\diagnostics\management-drawer-deployment.json`）：7 个前端资源的哈希在源码、独立候选包
  `_dev_runtime\packages\vixiq`、`dist\vixiq`、`D:\Program Files\Vixiq` 四处一致；各落点 `config/user.json` 哈希保持原值；
  `runtime_changed=false`、`configuration_changed=false`——即**程序未重启、数据未动**。备份目录为各落点下的
  `.backup-management-drawer-20261116i-1513b047`（**不要删**）。
- GitHub：**未提交、未推送**（本轮未收到推送指令）。
- 浏览器实测已完成：三页开合、选中项操作、手动展开、回顶部、短列表钳制、零位置 Home、500 像素窄窗口。
  **最后一次重新加载/截图因用户拒绝浏览器权限而未完成**，因此不得声称「最终截图已完成」或「最终版全部浏览器场景均通过」。

### 4.6 图库工具栏与统计（2026-11-16）

- 操作分为下载、编辑、整理、信息，删除图标单独靠右；重新下载下拉含补完作品/修复原图，仅启用有效 Pixiv 作品，原覆盖备份逻辑不变。
- `lib-total` 显示选择的作品数与图片数，选择元数据跨页保留；上下分页栏左侧为当前筛选结果作品/图片数，右侧为页码。
- `/api/library/items` 附带 `counts={artworks,images}`，统计复用查询筛选条件且不受分页影响，去重标签关联，排除回收和未下载占位图片。
- 壁纸管理移除顶部说明，壁纸夹路径与张数/大小/扫描时间合并一行。
- T142 覆盖 8 项接口/前端行为回归，包含 Node.js 下载替身验证；不真实联网下载。当时源码与前端资源版本为 `20261116h`（现为 `20261116i`）。

### 4.6.1 三管理页分页行修正（2026-11-17，版本 `20261117c`）

- 顶部总数与页码移入吸顶工具栏（`#lib-pager-top`、`#authors-pager-top`、`#wf-pager`），
  收起菜单后仍可见、可翻页；底部分页（`#lib-pager-bottom`、`#authors-pager-bottom`）仍随列表滚走。
- 三页统一 `.pager-inner` 结构：左侧 `.pager-total`（如「共 1600 个作品 · 2186 张图片」），右侧首页/上一页/页码输入/下一页/尾页。
- 壁纸管理分页由「上一页/下一页」半成品补齐为同一套控件（`wallGoPage`，页码输入回车生效）；壁纸每页 80 张。
- 作者页删除已不存在的 `#authors-total-inline` 引用，总数改由 pager 渲染，单页时只显示「共 N 位」。
- 验收：全量冒烟 135/135；合成数据无头实测（`_dev_runtime/diagnostics/check_pager_ui.py`）三页总数靠左、页码靠右、收起态分页可见；
  7 个资源已同步候选包/dist/安装版，配置哈希未变。安装版程序当时未运行，下次启动生效。
- 同日追加（版本 `20261117d`）：壁纸夹路径与「共 X 张 · 大小 · 上次扫描」移入筛选按钮行并靠右（`.wf-location-row` 在 `#wf-filters` 内）；
  `#wf-stats` 四个统计卡改为紧凑小卡片横排（高约 55px、总宽约 400px），不再占大块高度。

### 4.7 壁纸相关接口速查

| 接口 | 说明 |
|---|---|
| `GET /api/wallpaper/files` | 归属清单 + 统计 + 重复组（读 `wallpaper_files`） |
| `POST /api/wallpaper/files/scan` | 后台扫描，body `{deep: bool}`；进度看 `GET .../scan/status` |
| `POST /api/wallpaper/files/recycle` | 勾选文件进回收站，只收壁纸夹内相对名 |
| `GET /api/wallpaper/dupes/check?artwork_id=&page_num=` | 设为壁纸前的查重（源码已按具体图片匹配，包含裁剪版） |
| `POST /api/wallpaper/apply` | 从图库页设壁纸，支持 `crop` 与 `force` |
| `POST /api/wallpaper/use` | 选择并确认后使用壁纸夹中的已有图片，不生成或复制图片 |
| `GET /api/wallpaper/current` | 当前桌面壁纸溯源（只读） |

数据流：扫描 → 写 `wallpaper_files` → 列表/查重都读这张表 → 秒回。
`match_kind`：`md5` / `name` / `visual` / `manual`；`page_num` 可能为 NULL（文件名无页号）。

---

## 5. 用户的工作方式

- 一次一项，做完 → 验证 → 部署 → 汇报，再等下一项。
- 汇报用大白话，不堆术语；说清楚改了什么、验证了什么、还有哪些没做。
- 用户很在意「四处同步」：本地源码、`dist` 打包版、`D:\Program Files\Vixiq` 安装版、GitHub 仓库。
  每次汇报都说明这四处状态；未收到推送指令时如实注明 GitHub 尚未推送。
- 不要主动生成无关文档；不要擅自修改全局规则文件。

---

## 6. 已知坑（都是实际踩过的，改之前先看）

### 6.1 子进程闪命令窗口

打包版是 `runw.exe`（无控制台），任何控制台程序（wmic / taskkill / netstat / icacls /
powershell / cmd）不设隐藏标志就会闪黑框。**一律走 `utils/proc.py`**：

```python
from utils import proc
proc.run(["taskkill", "/F", "/PID", pid], capture_output=True)
proc.popen(["explorer", f"/select,{path}"])
```

- 不要用 `shell=True`（多起一个 cmd.exe，闪窗主因）。
- `wmic` 的参数列表**不能带外层引号**：`"\"name='x'\""` 会返回无效查询（0x80041017），
  直接传 `"name='x'"` 才正常。
- 冒烟 T138 会静态扫描：出现 `shell=True` 或裸 `subprocess.run/Popen` 直接判失败。

### 6.2 顶部工具栏「抽屉闪烁」

滚动阈值不能只用一个数（如 >120 收起）。收起会让内容变短、滚动位置被钳制，又触发展开，
在临界点反复弹跳。正确做法是**迟滞区间**：>160 收、<80 展；class 没变不写 DOM。
贴住时的底边框要「常驻透明、只变色」，否则高度变化会在临界点抖。

### 6.3 页号边界

判断文件名属于哪一页不能用字符串包含：`"_p1" in "700001_p10.png"` 为 True。
用正则 `_pN(?!\d)`。

### 6.4 大量卡片渲染

壁纸夹 1000+ 张，一次性塞 DOM 会明显卡顿。分页或分批渲染（壁纸管理每页 80 张；其他网格每批 ~120）+ `IntersectionObserver`
滚动补下一批；换视图前先 `disconnect()` 旧 observer。

### 6.5 临时目录短名路径

`TEMP` 可能是 `C:\Users\ADMINI~1\...`（8.3 短名），而服务端会把根目录 `.resolve()` 成长名。
测试里比较路径前先 `.resolve()`，否则「文件是否在图库内」判断会失效（测试环境差异，不是功能 bug）。

### 6.6 查重与裁剪

- 壁纸夹是平铺的，路径安全校验只接受**相对文件名**，拒绝 `../`、绝对路径、图库内文件。
- 裁剪版文件名形如 `{artwork_id}_p{page}_crop.jpg`，天然带归属。
- 用户手动裁剪/另存的图可能既无作品 ID、md5 也不同，只能靠 ORB 视觉溯源；
  首次扫描要加载整库特征（1~2 分钟），之后常驻。
- 「同一个作品」≠「同一张图」：用户要的重复是后者（同页/同内容），这是 §4.1 第 1 条的核心。

### 6.7 安全模式与截图

- 不要用浏览器自动化截「壁纸管理」这类会显示真实缩略图的页面——用户明确提醒过
  「避免违规图片」。需要验证 UI 时使用临时合成图与系统设置替身，禁止连接或截图安装版真实图片。
- 检查 R18 相关内容时，先把安全模式确认开启（`GET /api/safemode`）。

### 6.8 其他

- `_wall_dupes_for` 已改为优先读 `wallpaper_files` 索引，整表为空才退回现场反推；
  索引非空时不要再现场跑 ORB（很慢）。
- SQLite 连接绑定线程：后台线程必须用 `db_factory()` 新建连接，不要跨线程复用。
- 从磁盘删除永远走 `_recycle_files`（Windows 回收站）。应用软删除是跨盘可用的保留式搬移：先独占创建目标副本，复制完成才移除源文件，数据库写入失败则搬回，不覆盖任何目标。

---

## 7. 安全红线（无条件遵守）

1. **绝不自动删除壁纸/图片**。删除必须用户勾选确认；库中删除走应用回收站，从磁盘删除只进 Windows 回收站（可还原）。
2. 图库内的文件不允许从壁纸入口删除（要走图库软删除，才有记录和恢复）。
3. 安全模式下 R18 不加载真实像素；新增页面必须接 `imgSrc` / `safeThumb`。
4. 不执行 `git push --force`、`git reset --hard`；回退用 `git revert`。
5. 不提交密钥、cookies、`tools/cloudflared.exe` 等无关大文件。
6. 不修改用户全局规则文件；不擅自删除用户数据或备份。

---

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 产品功能、安装、使用 |
| [DEBUG_HISTORY.md](DEBUG_HISTORY.md) | 历史 bug 根因与修复归档（很有参考价值） |
| [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | 表结构与字段说明 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |
| [autotest/SMOKE_REPORT.md](autotest/SMOKE_REPORT.md) | 最近一次冒烟结果（每次运行覆盖） |
| `build\revert_backup_20261116\` | 被回退的改动备份（参考用，勿直接套用） |
