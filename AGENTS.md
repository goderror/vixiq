# AGENTS.md · 接手须知

> 给后续接手本项目的 Codex / 其他代理看。最后更新 2026-11-28（当前前端版本 `20261128b`）。
> 产品面向普通用户的说明在 [README.md](README.md)，历史排障在 [DEBUG_HISTORY.md](DEBUG_HISTORY.md)，
> 表结构在 [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)，版本记录在 [CHANGELOG.md](CHANGELOG.md)。思考要用中文。

---

## 0. 先读这三条

1. **一次只改一项**。用户明确要求「我一项项跟你说」，不要一次实现多个需求。
   上一轮一口气改了 7 项，被整体回退（见 §4.1）。有歧义先问，不要自行扩大范围。
2. **改完必须验证**：`py_compile` + 全量冒烟（当前 184 用例，必须全过，2026-11-26 已 184/184 通过），同步相应程序/资源并验收安装版；仅更新交接文档按 §2.2 核对，不重启或部署程序。
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
| `autotest/smoke_all.py` | 全量冒烟，184 个用例；新增功能必须加断言 |

---

## 2. 运行、验证、打包、部署

### 2.1 验证（每次改完都要做）

```powershell
.venv\Scripts\python.exe -m py_compile web\server.py core\wallpaper_index.py
.venv\Scripts\python.exe -m autotest.smoke_all        # 184 用例必须全过
```

- 冒烟无参数、跑全量约 50 秒；输出报告在 `autotest/SMOKE_REPORT.md`。
- 只想快速验证某个用例，可直接 import 调用：

```powershell
.venv\Scripts\python.exe -c "from autotest.smoke_all import t137_wallpaper_index_and_admin_page as t; t(); print('OK')"
```

- 改了前端（`web/static/*`）**必须升版本串**（§3.1），否则冒烟会失败。

### 2.2 打包与部署到安装版

**仅更新交接文档**：核对当前代码、既有验证日志和部署清单即可；无需重新运行全部冒烟、打包、部署或重启。代码和前端改动仍按 §2.1 验证。

**仅改前端**：先在独立源码/合成数据环境验收并提升版本，按本节的程序发布回滚规则备份实际将替换的资源，原子同步到隔离候选包、`dist/vixiq/_internal/web/static/` 和 `D:/Program Files/Vixiq/_internal/web/static/`。新资源先到位，`index.html` 最后替换；不用停止或重启安装版。当前 7 个资源：`app.js`、`style.css`、`index.html`、`wallpaper-admin.js`、`management-drawer-state.js`、`management-toolbar.js`、`management-toolbar.css`。核对资源哈希，并确认各自 `config/user.json` 前后哈希不变。

本机本轮执行记录：`_dev_runtime/diagnostics/deploy_management_drawer.py` 与 `management-drawer-deployment.json`。它们在忽略目录中，是本轮本地记录；后续复用前须核对版本、资源清单和目标路径，不作为通用发布脚本盲目执行。这些脚本采用当时的备份布局，后续发布按下方程序发布回滚备份规则执行。

**后端或打包配置变更**：先完成源码验证，在项目隔离目录生成候选包，不要先停正在使用的安装版：

```powershell
.venv/Scripts/python.exe -B -m PyInstaller build.spec --noconfirm --distpath _dev_runtime/packages --workpath _dev_runtime/pack-build
```

本地自用不设 `VIXIQ_RELEASE=1`，避免剔除自用立绘。候选包必须使用独立端口（本机验收为 7813）与 `_dev_runtime` 内的测试 data/temp/wallpapers；禁止复制真实数据库或 cookies 到测试区。

当前冻结版与安装版共享全局单实例互斥，不能承诺二者并行启动。完成构建与源码/静态检查后，在用户已授权的短暂停启窗口中退出安装版、用合成数据验收冻结候选；候选退出后再替换程序并恢复安装版。验收失败应恢复旧程序，不自动覆盖业务数据库。更新程序不得复制测试配置覆盖安装目录的 `config/user.json`。

#### 程序发布回滚备份

程序回滚资料统一放在 Trae 当前工作区现有的 `_dev_runtime/backups`。回滚清单和按哈希保存的文件使用可识别的发布备份文件名，沿用该目录。候选包、`dist` 和安装目录内不再创建新的 `.backup-*`。

- 同一次发布只建一个带时间戳和版本的回滚清单。按候选产物逐项比较目标文件的 SHA-256，仅备份实际要覆盖或移除的旧程序与资源。已相同的文件跳过；新文件在清单中标明原先不存在，回滚时据此处理。只改前端时按资源差异备份，完整打包也不复制整份旧 `_internal`。
- 旧文件按 SHA-256 在集中目录存一份字节。多个落点或多次部署遇到同一旧文件，共用该文件。清单记录每个目标根目录、相对路径、操作类别、旧新哈希与对应备份。各目标内容不同则分别记录和备份各自的旧内容。开始替换前验证所有需要恢复的备份字节和哈希。
- 每次验收报告附回滚清单路径、备份实际增加字节和部署后的资源哈希。日常保留最近两次已验证发布的回滚入口；较旧清单与没有任何清单引用的文件列入待清理清单，由用户明确授权后清理。避免把同一时点的回滚内容再存一份到其他目录。
- 业务数据库的备份沿用原流程。需要迁移时按项目约定保留迁移前数据库备份，程序回滚清单只记录其位置。回滚程序时保持业务数据库和安装配置原位。Trae 的会话快照用于代码回退。发布回滚使用本节的集中备份，源码阶段按任务提交 Git，避免另外复制整个工作区。

部署后核对程序/资源、配置哈希和健康状态；汇报源码、dist、安装版与 GitHub 四处状态，未收到推送指令则注明 GitHub 尚未推送。



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
改前端后一起升（当前源码 `20261128b`，格式 `yyyyMMdd+字母`）。漏一处冒烟必失败。
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
- 同日追加（版本 `20261117e`）：收起态整块菜单（`.management-main`）完全隐藏，只留贴顶的总数与页码（`main.management-compact` 去掉顶部 20px 留白）；
  标签栏出现叠三角 `#mgmt-peek`，悬停滑出简化菜单（`main.management-peek` 下的 absolute 浮层，不推挤列表），移开/失焦/滚动/Esc 收起。
  结构断言在 T143 的 `test_peek_trigger_and_collapsed_menu`。
- 同日追加（版本 `20261117f`）：顶栏退出按钮左侧新增卡片密度按钮（`#cards-density-btn`，`cardsToggle()`），
  切换 `main.cards-collapsed`：图库/壁纸卡片只留缩略图 + 标题/文件名，其余信息与操作按钮收起；网格列变窄。
  悬停用 `cardPeekShow` 克隆完整卡片做 fixed 浮层（`.card-peek`），靠下时向上展开，不撑动其他卡片；移开/滚动/切页即消失。
  偏好存 `localStorage['vixiq.cards.collapsed']`。结构断言在 T143 的 `test_cards_density_toggle`。
- 同日修复（版本 `20261117g`）：卡片浮层最初用 `mouseover/mouseout` 管理，浮层插入/移走会让浏览器把命中目标在
  `IMG.thumb` 与 `body` 之间反复切换，形成「浮层出现→判定离开→移走→再出现」的闪烁循环（实测 3 秒 99 次）。
  改为 `mousemove` + rAF 节流 + 遍历卡片矩形判断鼠标位置，浮层变化不再触发事件，循环消失（3 秒 1 次）。
  **不要改回 mouseover/mouseout 管理浮层显隐。**
- 同日修正（版本 `20261117h`）：浮层改为「抽屉」——收起态保留与正常卡片同尺寸的缩略图（不再缩窄网格），
  悬停时克隆卡片但隐藏缩略图/标题/选择框/角标，只把标题以下的信息贴在卡片底边向下滑出；底部空间不足时贴视口底部。
  `.card-peek` 样式必须放在 `style.css` 末尾（`.work-card/.wf-card` 基础规则之后），否则 `position:fixed` 会被卡片规则覆盖。
  页数角标（`.pages-badge`）在收起态照常显示。
- 同日修正（版本 `20261117i`）：
  1. 抽屉只在标题条（图库 `.w-body` / 壁纸 `.wf-meta`）触发，鼠标停在缩略图上不展开。
  2. 菜单收起/展开改为高度渐变：`:root { interpolate-size: allow-keywords }` + `.management-main` 的
     `height/opacity` 过渡，`main` 的 `padding-top` 也过渡；不再瞬间跳变把列表顶得一跳。
     `.management-main` 不再 `display:none`，收起态是 `height:0; opacity:0; overflow:hidden`；peek 浮层里再覆盖回 auto。
  3. 修复「在资源管理器中打开」只有提示没反应：`proc.popen(..., hide=False)`——GUI 程序带 `SW_HIDE`
     会被藏起来；命令行改为字符串 `explorer /select,"<路径>"`（逗号必须在引号外，列表参数在含空格路径下会失效）。
     本轮改了 Python 后端，已重新打包并替换 `dist/vixiq` 与 `D:\Program Files\Vixiq` 的程序文件（config 未动）。

### 4.7 手机访问（Cloudflare 快速隧道，2026-11-17，版本 `20261117k`）

- 设置页「📱 手机访问」卡片：访问密码（4-12 位数字，写 `web.access_pin`，并**就地更新**
  `web/server.py` 里的 `_config`，否则新密码要等重启才生效）、开关、当前地址、复制地址。
  开关状态存 `web.mobile_enabled`：开启后下次启动自动开隧道。
- 托盘右键新增一组：状态灰字行 / 开启-关闭手机访问 / 复制手机访问地址。
  菜单仍是纯函数 `tray_menu_items(info, tunnel)` 生成，T128 覆盖各状态。
- `utils/tunnel.py` 管 cloudflared quick tunnel：工作目录固定 `data/logs`（绝不继承安装目录）、
  退出 terminate→kill、启动按 pid 文件清理上次残留（校验进程名，不误伤别的隧道）、
  stderr 单独落 `cloudflared.err`，把 Cloudflare 的拒绝原因原样带回界面。
- 安全：`access_pin` 为空时拒绝开启（`main._start_tunnel`），`/api/tunnel` 是**本机设置接口**。
- 打包：`build.spec` 在 `tools/cloudflared.exe` 存在时随包进 `_internal/resources/`；该二进制不进版本库。
- 已知限制：quick tunnel 地址每次启动都变，且 Cloudflare 对同一 IP 的创建有频率限制，
  短时间内反复开关会返回 `failed to request quick Tunnel`（界面已原样显示）。

### 4.8 壁纸相关接口速查

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

### 4.9 大图查看器手机端修正（2026-11-17，版本 `20261117l`）

手机实测反馈两件事，都出在「点开大图」之后：

1. **放大后拖不动**：`setupZoom` 原来只挂 `mousedown/mousemove/mouseup`，手指拖动只会滚页面。
   现统一改 Pointer Events（`pointerdown/pointermove/pointerup/pointercancel`，鼠标与触屏同一套），
   并在 `.lightbox-img-wrap.is-zoom` 上加 `touch-action: none`；`panMoved` 继续用来区分「拖完松手」与「点一下放大」。
2. **翻页后还是第一张**：连点时每页都会同时请求缩略图 + 原图（本地原图 5~15MB），
   几页叠起来占满浏览器 6 条连接，新页只能排队 —— 页码变了、图还是旧的。
   现在翻页先 `cancelPending()` 取消上一页未完成的 `Image` 请求；原图延后 220ms 再请求，
   这一页已被换掉就跳过。远程大图弹窗本来就有「原图只在点按钮后加载」，不受影响。

验收：Edge 移动仿真（390×844、触摸）下经 CDP 发真实触摸拖动，`transform` 从 `0px,0px` 变为 `-120px,-60px`；
`5Mbps / 100ms` 限速下快速连翻 7 页，最后一页显示的是自己的 p6 缩略图。

### 4.10 手机版自适应（2026-11-17，第一期，版本 `20261117m`）

同一页面按屏宽重排，桌面零改动（所有规则都在 `@media (max-width: 760px)` 内）：

- 手机隐藏顶部 `.tabs`，改用底部 `.m-nav`（查询/图库/下载/壁纸/设置）；
  反搜/作者/统计/回收站收进 `☰ 更多` 的 `.m-sheet`。两边共用 `switchTab` 同步高亮。
- 卡片网格（`.works-grid` / `.wf-grid` / `.rev-grid`）2 列；表格 `.table-wrap` 可横滑；
  顶栏搜索换到第二行，输入框 16px（iOS 聚焦不放大）；`main` 底部留出导航 + 安全区高度。
- 大图：手机隐藏两侧小箭头，改用**单指左右滑翻页**（`swipeFrom`，水平位移 >55px 且明显大于纵向）；
  **双指捏合**张开 → 1:1、收拢 → 还原（`pointers` / `pinchDist`）；放大后单指拖动仍是平移。
  手势统一在 `setupZoom` 里，本地图库与搜索大图两个弹窗共用。
  `.lightbox-img-wrap` **基础态就要 `touch-action: none`** —— 否则横滑被浏览器当成滚动，
  只发 `pointercancel`，翻页回调收不到（实测）。双指落下时必须清掉 `swipeFrom`，否则抬起会被误判成翻页。
- 第二期未做：三管理页表格转卡片、筛选抽屉、设置页分组折叠、PWA 图标/离线壳。

### 4.11 桌面端四项修正（2026-11-17，版本 `20261117n`）

1. **叠三角把列表点成空白**：`#mgmt-peek` 也带 `.tab` 类，通用绑定 `$$('.tab')` 给它跑了
   `switchTab(undefined)` → 所有 `.tabpage` 同时失活。现只绑 `.tab[data-tab]`。
   **新增任何「长得像 tab 但不是页面」的按钮，都不要给它 `data-tab`，并确认通用绑定带了守卫。**
2. **到顶自动展开改成下拉触发**：`ManagementDrawerState.input/scroll` 不再在上滑到低阈值时展开；
   新增 `pull(distance)`（默认累计 90px，`pullThreshold`）与 `resetPull()`。
   控制器在 `scrollTop<=2` 时把滚轮上滚 / 手指下拉换算成 pull，够距离才展开；向下滚清零。
   **必须跟手位移**（2026-11-17 二次反馈「只是更难触发、没有拖拽感」）：列表按
   `累计 × 0.5`（上限 64px）做 `translateY`，工具栏下方浮出「继续下拉展开菜单 / 松开展开菜单」提示；
   滚轮停手 240ms 或未到阈值松手 → 回弹；到阈值先还原位移再展开菜单。相关函数：
   `consumePull / paintPull / settlePull / schedulePullIdle`。
   「回到顶部」按钮是明确动作，仍直接 `state.expand()`。
3. **共计/页码易位**：三个管理页的 `#lib-pager-top` / `#authors-pager-top` / `#wf-pager`
   移到 `.management-main` **之前**，展开菜单只把列表往下推，页码始终紧贴标签栏。
4. **刷新按钮不显眼**：`#smart-refresh` 从 32px 透明细线改成 38px、描边 2.3、淡底 + 边框 + 阴影；
   手机端与置顶按钮错开（置顶出现时刷新上移到 `146px + 安全区`）。

### 4.12 冒烟不得改真实桌面壁纸（2026-11-17）

- **现象**：用户反馈「每次更新代码，正在用的桌面壁纸就被换掉/去掉了」。
- **根因**：T127 的「移出壁纸」用例会调用服务端回收接口，而该接口的逻辑是
  「先 `apply_wallpaper(下一张)` 再删当前文件」——测试里下一张是临时目录的图，
  于是真实写了 `SPI_SETDESKWALLPAPER`；用例结束临时目录被删，桌面壁纸就指向不存在的文件。
- **修复**：`autotest/smoke_all.py` 的 `main()` 在跑套件前把 `web.server.apply_wallpaper` 与
  `core.wallpaper_trace.apply_wallpaper` 全局替换成 `lambda _p: True`（no-op）。
  今后任何测试都必须走这个替身，**绝不允许**真实调用系统壁纸 API。
  用例自己的局部打桩仍可覆盖全局替身。
- **2026-11-18 追加**：T127 单独运行（`python -c "from autotest.smoke_all import t127..."`）时不会走
  `main()` 的全局替身，而「移出壁纸」接口会先 `apply_wallpaper(下一张)` —— 实测把真实桌面壁纸
  改成了临时目录里已删除的文件。现在 T127 自己也会局部替换 `wsrv.apply_wallpaper` 为 no-op；
  **任何单独调用、且会走到删除/换壁纸路径的用例都必须自带替身**。

### 4.13 卡片悬停抽屉的遮挡（2026-11-18，版本 `20261117t`）

- **现象**：卡片滚到吸顶菜单/标签栏下面后，鼠标划过菜单区域仍会弹出卡片详情抽屉。
- **根因**：`cardAtPoint()` 只比较卡片的几何矩形，没考虑上面盖着的吸顶层；
  且 `.card-peek` 的 `z-index:170` 高于标签栏/吸顶菜单，抽屉会画在它们上面。
- **修复**：命中判定改为 `document.elementFromPoint()`，命中 `.topbar/.tabs/.management-toolbar/
  .m-nav/.modal-mask/#lightbox` 一律不响应；抽屉 z-index 降到 19（低于吸顶菜单 20），
  标签栏 `z-index:50`、顶栏 60。向上贴底展开时高度也不许越过卡片顶边。
- **悬停延迟**：`CARD_PEEK_DELAY = 500` —— 鼠标在标题条上停留半秒才展开；
  换卡/移开/滚动立即收起并清定时器（`cancelPeekTimer`）。

### 4.14 叠三角开合时列表跳动（2026-11-18，版本 `20261117t`）

- **现象**：鼠标移到标签栏叠三角，简易菜单展开/收起时图片列表会上下跳一下。
- **根因**：收起浮层时 `.management-main` 从 `position:absolute` 回到文档流，
  而它带着 `height:auto → 0` 的过渡；过渡中途仍有高度，把下面的列表顶了一段。
- **修复**：`peekOpen/peekClose` 时给 `main` 临时加 `.management-peek-noanim`
  （`transition: none !important`），两帧后移除。实测开合过程中首卡位置与 `scrollTop` 完全不变。

### 4.15 作者夹跟随分类目录（2026-11-18，B 止血）

- **现象**：用户手工把作者夹分到 `X图`（普通）/ `涩图`（R18），但下载器和一键整理仍按
  图库根目录建夹，导致根目录与分类目录同时存在作者夹。
- **规则**（`utils/author_naming.py`，三处共用，不要各写一份）：
  - `collect_author_dir_parents`：扫磁盘得出每位作者的作者夹父路径（`""` / `X图` / `涩图`）。
  - `collect_section_dirs`：识别顶层分类目录（排除作者夹、`#来源#`、`.` 开头）。
  - `pick_section_parent`：作者已有分类夹 → 跟随；多分类按 R18 选；认不出返回 `None`，不瞎搬。
  - `pick_default_section`：新作者按 R18 落已有分类（`r18/r-18/涩图` vs `x图/普通/一般/全年龄`）；
    分类名认不出就返回 `None`，保持旧行为（落根目录）。
- `web/server.py` 的 `_tidy_build_plans` 用 `_section_prefix` 决定期望夹前缀；
  `core/crawler.py` 的 `_pick_author_dir` 新增 `is_r18`，补页仍优先落回原夹（`with_work`），
  新作品/新作者按分类落盘。
- 验收：T146（纯函数 + 整理计划 + 下载落点，含新作者与多分类），全量 **138/138**；
  真实库只读预览：根目录 9 个作者夹（11 张非 R18）会被整理进 `X图`。
- 部署：候选包已同步 `dist/vixiq` 与 `D:\Program Files\Vixiq`（各自 `config/user.json` 哈希未变，
  旧程序备份在各自 `.backup-section-follow-20261118-*`），安装版已重启并通过 `/api/health`；
  安装版的整理预览已确认根目录作者夹目标为 `X图/...`；
  GitHub 已本地提交 `c39ecfa` / `9be8976`，**未推送**。
- 注意：B 只改"新建/整理去哪"，**不会自动搬已有文件**；根目录那批夹要用户主动点「一键整理」才移动。
- A 阶段（下一步）：分类字段化、`涩图`→`R18`、`X图`→`普通`（名称可配置）、按规则重排与迁移。

### 4.16 作品编辑选自定义作者不生效（2026-11-18，版本 `20261118i`）

- **现象**：作品编辑里从下拉选已有的自定义作者，点保存提示成功，但作品仍是原作者；
  作者管理里也搜不到某个自建的自定义作者。
- **根因**：作者下拉只渲染名字，选中后只回填名字；保存时 `resolveAuthorInput` 按名字反查，
  拿到 `local_xxx` 形式的 `author_id`。后端 `/api/library/update` 只认「纯数字 pixiv ID」
  或「作者名」，非数字 `author_id` 两个分支都不进，被静默丢掉 —— 所以界面说保存成功、作者没变。
  同名作者（自定义 Kloan 与 pixiv Kloan）光看名字也无法区分。
- **修复**：
  - `makeEditAuthorCombo` 的选项带 `data-id`，点选后写入 `input.dataset.authorId`；
    用户再手打名字时（`input` 事件）清除，避免张冠李戴。
  - `resolveAuthorInput(text, pickedId)` 优先用点选的 ID；作品编辑与批量编辑都传这个 ID。
  - `/api/library/update` 与 `/api/library/batch-update` 接受**已存在**的非数字作者 ID；
    不存在的 ID 返回 400，不再假装成功。
- **验收**：T147（单条/批量/不存在 ID/前端接线），全量 **139/139**；
  安装版已替换并重启（`/api/version` 前端 `20261118i`，`built_at 2026-11-18 02:35:07`）。
- **历史数据**：`冯艺VonKunst` 在 `authors` 表里没有记录，磁盘只留 `冯艺VonKunst_local_f571b2433a31`
  旧夹；作品 `ext_13e01bc59205adda` 现挂在 `Kloan`（113227553）名下，文件仍在旧夹。
  需要用户确认这张图到底属于谁，再决定「恢复冯艺作者」还是「并入 Kloan 并清理旧夹」。
  这类「只在磁盘上的孤儿作者夹」目前不会被自动补建，下一步做通用合并/修复入口时处理。

### 4.17 改作者时的同名身份合并（2026-11-18，版本 `20261118i`）

- **现象**：作品原本挂在自定义作者「Kloan」下，改选同名的 pixiv 作者后，库里留下两条作者记录、
  磁盘留下两个作者夹；文件还停在旧夹里（改作者只改了数据库）。
- **规则**：
  - 前端 `sameNameLocalAuthor(w, auth)` 只在「当前作品作者是自定义作者、新选的是同名 pixiv 作者」时
    返回自定义作者 ID；同名不等于同一人，**绝不自动合并**。
  - `saveLibEdit` 命中时弹「作者身份合并」：确认 → 带 `merge_author_id` 保存；否则只改这张。
  - 后端 `/api/library/update` 收到 `merge_author_id` 时先 `merge_author_into`（作品/队列/曾用名/链接
    并过去、删源作者行），再 `_sync_author_folder(..., old_id=源)` 改文件夹名，
    最后 `_gather_works_into_author_folder` 把文件归到新作者夹；不合并时也会把当前作品的文件归位。
  - 合并/归位失败不影响元数据保存，响应里带 `merged` / `files_gathered` 供界面如实提示。
- **验收**：T148（合并/不合并/不存在 ID/前端接线），全量 **140/140**；
  安装版已替换并重启（前端 `20261118i`，`built_at 2026-11-18 02:35:07`）。
- 注意：合并后旧目录可能留下空夹（`_gather` 只执行 move，不删空夹），跑一次一键整理即可清掉。

### 4.18 作者源头查重（2026-11-18，版本 `20261118i`）

- **目标**：不再等到作者管理里才发现重复。保存作品作者时就拦住「同名但身份不同」的误建。
- **规范化**：`utils.author_naming.normalize_author_name` 统一大小写、全半角、空白/零宽、
  宣传后缀与污染括号；`_find_author_by_name_or_alias` 改用它比较现用名/曾用名，
  写法差异不会再长出第二条自定义作者。
- **查重**：`db.find_author_conflicts(name, exclude_id=None)` 返回同名/同别名的候选
  （`is_pixiv` / `count` / `match`），同名不等于同一人，只做提示，绝不自动合并。
- **保存拦截**：`/api/library/update` 与 `/api/library/batch-update` 在只给 `author_name` 且
  存在同名 pixiv 作者时返回 **409 + conflicts**；前端 `askAuthorConflict` 让用户选
  「复用这个」（改成 author_id 重试）或「同名但另一人」（带 `force_new_author=true` 新建）。
  同名自定义作者则直接复用，不重复建。
- **前端解析**：`resolveAuthorInput` 手打名字不再在前端按名字自动匹配（同名可能不同人），
  统一下沉到后端查重；下拉点选仍优先用 `author_id`。
- **验收**：T149（规范化/409 候选/同名新建不复用/同名自定义复用/前端接线），全量 **141/141**；
  安装版已替换并重启（前端 `20261118i`，`built_at 2026-11-18 02:42:04`）。
- **未覆盖**：下载器、反搜归档、自动元数据补全等无人值守入口不做交互式拦截；
  它们按 pixiv 数字 ID 建档（天然唯一），同名跨类型只在作者管理里提示。
  作者管理页的「重复作者/同名分组」统一视图仍待做。

### 4.19 作者管理「重复作者」视图与通用合并（2026-11-18，版本 `20261118i`）

- **接口**：`GET /api/library/author-duplicates`（只读分组）+
  `POST /api/library/author-merge`（`src_id` → `dst_id`，整体合并并同步文件夹/文件）。
- **分组规则**：`db.find_author_duplicate_groups` 用并查集，把「现用名或曾用名规范化后相同」
  的作者连成一组，覆盖 pixiv 与自定义同名、自定义写法差异、改名后旧名与另一条现用名重合。
  只做发现，**不自动合并**。
- **前端**：作者页 `#authors-dupes-btn`「🔍 重复作者」打开弹窗，每组列出作者名/ID/类型/
  作品数/来源，点「合并到这位」把组内其他条目逐条并入目标；同名不等于同一人，必须逐组确认。
- **验收**：T150（分组/跨类型/合并/错误边界/页面接线），全量 **142/142**；
  安装版已替换并重启（前端 `20261118i`，`built_at 2026-11-18 02:47:45`）。
- 真实库当前 `total=0`：用户已于 02:20 用「绑定为 pixiv 作者」把自定义 Kloan 并入
  `113227553`；三张 B 站图（`ext_13e01bc59205adda` / `ext_d3e4e28bf8ba0013` /
  `ext_eb743a5d84f41553`）已由用户移入应用回收站，需要时可恢复。

### 4.20 标题不再写入文件夹路径（2026-11-18，版本 `20261118i`）

- **现象**：外部导入作品的标题被写成 `X图/#其他来源# / 文件名`，图库标题列显示路径。
- **根因**：`db.register_foreign_file` 登记占位作品时 `title = f"{folder} / {stem}"`。
- **修复（源头）**：标题只写 `stem`（文件名）。文件夹路径属于磁盘摆放信息，不是作品标题；
  以后导入/同步登记的新作品都不会再出现路径标题。
- **历史数据（一次性，已执行）**：备份 `F:\Vixiq\data\backups\pixiv-titlefix-20261118-031811.db`，
  清理真实库 **80 条**，复查命中 0。清理工具 `db.title_from_path_like` 保留（只认带路径特征的
  左侧：`@账号` / `#来源#` / `_local_` / `_foreign_` / 结尾 `_数字`，正常标题 `A / B` 不误伤）。
- **不做常驻功能**：图库没有「清理标题」按钮，也没有 `title-fix` 接口；这是导入 bug 的一次性修复。
- **验收**：T151（登记标题=文件名 / 历史格式识别与清理 / 正常标题不误伤 / 确认不保留按钮接口），
  全量 **143/143**；安装版已替换并重启（前端 `20261118i`，`built_at 2026-11-18 03:22:07`）。
- 图库搜索本来也匹配 `pages.local_path`，标题去掉路径后仍可用路径关键词定位。

### 4.21 A1 图库分类字段化（2026-11-18，后端，前端仍 `20261118i`）

- **迁移**：`023_library_sections.sql` 建 `library_sections` 表（`section_id/name/dir_name/
  match_json/is_default/sort_order`）+ `artworks.section_id`；初始两条：
  `general`（普通，目录 `X图`，规则 `is_r18=0`，默认）、`r18`（R18，目录 `涩图`，规则 `is_r18=1`）。
- **回填**：按现有 `pages.local_path` 顶层目录把作品归到分类（`X图`→general、`涩图`→r18），
  其余留空（整理时按默认分类处理）。`024_path_slashes_and_section_backfill.sql` 先把历史
  `local_path` 里的反斜杠统一成正斜杠（否则 `LIKE 'X图/%'` 匹配不到，实测漏了 1100+ 条），再补回填。
- **读取**：`db.list_sections / section_dir_for_r18 / backfill_sections`；
  `_tidy_build_plans` 的 `_section_prefix` 优先用作品 `section_id` 对应目录，其次作者已有分类夹，
  最后默认分类（只在分类目录已存在时生效，空图库保持根目录旧行为）；
  `crawler._pick_author_dir` 新作者按 R18 落分类目录，已有作者夹（含根目录夹）仍优先复用。
- **真实库结果**：general 1325、r18 265、未分类 12；`X图` 下仍有 82 张 R18（A1 只按路径回填，
  **A2 才按 R18 规则重排**）。安装版已替换并重启（`built_at 2026-11-18 03:38:42`）。
- **验收**：T152，全量 **144/144**。
- A2：目录改名已在 §4.27 完成；按 R18 规则重排与分类管理 UI/筛选仍待做。

### 4.22 卡片收起、壁纸使用记录与设置延迟（2026-11-18，版本 `20261118i`）

- **收起卡片**：`main.cards-collapsed` 下图库卡片保留**标题 + 作者**，隐藏来源标签等其余信息
  （`style.css` 的 `:not(.w-title):not(.w-author)`）。
- **设置壁纸延迟**（三处）：
  - `/api/wallpaper/apply` 的查重改 `deep=False` —— 设置时不跑 ORB（壁纸索引为空时首次要加载整库特征）；
  - 前端设置成功后先 `wallRefresh()` 浅刷新，1.5 秒后再补 `deep:true`；
  - 打开裁剪界面 1.2 秒后的主动深查保留（`/api/wallpaper/dupes/check?deep=1`），
    T139 的视觉兜底仍按原样工作。
- **整图裁剪 = 没裁剪**（2026-11-18 用户反馈「没动裁剪也被当作裁剪」）：
  - 前端 `confirmWallCrop` 判断裁剪框是否等于整图，是则不传 `crop`；
  - 后端 `/api/wallpaper/apply` 也兜底：`cx/cy≈0、cw/ch≈1` 按原图处理，不生成 `_crop.jpg`；
  - 设置成功的响应不再现算 `_wall_info_payload`（大文件 md5/尺寸），改由前端浅刷新异步补。
    剩余 1～3 秒属于 Windows `SystemParametersInfoW` 切换桌面本身。
- **壁纸使用记录**（用户确认：裁剪版进壁纸夹，原图不复制，但壁纸库登记所有设置）：
  - 迁移 `025_wallpaper_usage.sql` 建 `wallpaper_usage`
    （`artwork_id/page_num/file_path/source/used_at`，source = library/crop/wall）；
  - `/api/wallpaper/apply` 成功后登记（原图 `library`，裁剪版 `crop`）；
    `/api/wallpaper/use` 选已有壁纸成功后登记 `wall`（能查到归属时带作品/页号）；
  - `GET /api/wallpaper/usage?limit=50`；壁纸管理页 `#wf-usage` 显示最近 12 条
    （来源标签、作品/页、文件名、时间）。
  - `wallpaper_files` 表保持原语义（只记壁纸夹实际文件），不塞图库原图，避免破坏
    移出/回收/重命名等以「夹内相对名」为前提的操作。
- **验收**：T153，全量 **145/145**；安装版已替换并重启
  （前端 `20261118i`，`built_at 2026-11-18 04:35:54`），`/api/wallpaper/usage` 正常。

### 4.23 一键整理误删刚移入的文件（2026-11-18，严重事故与修复）

- **事故**：根目录 `Kloan_113227553` 要移入 `X图/Kloan_113227553`，同时 `X图/Kloan_113227553`
  当时是空夹，于是生成两个计划：move + rmdir。执行顺序是先 move、后 rmdir，
  rmdir 段用 `shutil.rmtree` 且**不重新检查目录是否仍为空**，把刚移入的两张原图永久删除。
- **修复（两道防线）**：
  1. `_tidy_build_plans` 生成 rmdir 计划时，跳过本次整理 move 的目标夹及其上级
     （`_move_targets` / `_will_receive_files`）；
  2. `_tidy_apply_plans` 执行 rmdir 前重新调用 `_tidy_dir_is_disposable`，
     非空（含刚移入的文件）一律跳过。**新增任何删除类计划都必须做执行时二次校验。**
- **恢复**：`146763591` 从 Windows 回收站的同 MD5 副本找回；`150084334` 用
  `/api/cart/repair` 重下并校验 MD5；全库缺失文件已清零。
- **验收**：T154（move 目标不得列 rmdir + 执行前二次检查），全量 **146/146**；
  安装版已替换并重启（`built_at 2026-11-18 05:40:28`，前端仍 `20261118i`）。

### 4.24 壁纸管理统计改为「壁纸库」口径（2026-11-18，版本 `20261118j`）

- **现象**：壁纸管理页只显示壁纸夹里的实际文件数，用户认为应该是「壁纸库」登记数。
- **口径**：壁纸库 = 壁纸夹文件（`wallpaper_files` 索引且文件仍存在）+ `wallpaper_usage`
  里仍存在、且不在壁纸夹的图库原图（按绝对路径去重）。原图设壁纸不复制文件，
  但仍属于壁纸库，必须计入。
- `/api/wallpaper/files` 的 `stats` 新增 `library_refs`（原图引用数）与
  `library_total`（壁纸夹 + 原图引用）；`wallpaper-admin.js` 的 `#wf-stats` 改为四张卡：
  壁纸库（`library_total`）/ 壁纸夹（`total`）/ 已归属 / 重复图片。
- **验收**：T153 增加真实接口断言（壁纸夹 1 张 + 图库原图 1 张 → `total=1`、
  `library_refs=1`、`library_total=2`；已删除的记录不计），全量 **146/146**。
- 真实库：`wallpaper_files` 390、`wallpaper_usage` 有效原图 2 → 壁纸库 392。

### 4.25 壁纸库补登记与「最近设置」规范化（2026-11-18，版本 `20261118k`）

- **补登记（backfilled）**：清理「与图库字节一致的壁纸副本」后，图库原图仍在，不能从壁纸库消失。
  - 迁移 `026_wallpaper_usage_library.sql`：`wallpaper_usage` 加 `backfilled`（0=真实设置，1=补登记），
    并一次性把旧记录 `used_at` 从 SQLite 的 UTC 默认值转成本地时间（界面原来差 8 小时）。
  - `/api/wallpaper/dupes/recycle` 删除成功后，用扫描结果里的 `artwork_id/page_num/rel`
    把图库原图登记为 `backfilled=1`，只参与壁纸库统计，**不进「最近设置」**。
  - `record_wallpaper_usage(backfilled=...)`：补登记遇到同键真实记录时直接跳过，不覆盖真实时间；
    新记录显式写本地时间。`list_wallpaper_usage(include_backfilled=False)` 默认过滤补登记。
  - `library_refs` / `library_total` 统计时 `include_backfilled=True`。
- **历史补登记（一次性，已执行）**：`_dev_runtime/diagnostics/backfill_wall_library.py`
  枚举 `C:\$Recycle.Bin` 中原路径在壁纸夹下的条目（791 条），按文件名作品 ID + 页号匹配，
  匹配不到再用 `$R` 文件 MD5 与 `pages.md5` 比对；命中且图库原图存在 → `backfilled=1`。
  写入前用 SQLite backup API 备份到 `F:\Vixiq\data\backups\wall-library-20261118-063808.db`。
  结果：789 条命中（仅 `ext_c161648c0a689c20_p0_crop.jpg` 是裁剪版、无对应原图，跳过），
  真实库壁纸库 **1180**（壁纸夹 390 + 原图引用 790）。
- **最近设置规范化**：`wallUsageHtml` 改为四列 grid（来源标签 / 作品·页 / 文件名省略 / 时间右对齐），
  标题行注明「按时间倒序 · 最多 12 条」；`/api/wallpaper/usage` 按
  `(artwork_id, page_num, source)` 去重，同一页的旧路径/重复设置只留最新一条。
- **验收**：T153 增加补登记过滤/不覆盖真实记录/同页去重/前端对齐列断言，全量 **146/146**。
- **2026-11-18 07:04（版本 `20261118l`）**：按用户要求，壁纸管理页**不再显示「最近设置」**
  （移除 `#wf-usage`、`wallUsageHtml` 与 `.wf-usage*` 样式）。`/api/wallpaper/usage` 接口、
  `wallpaper_usage` 表与「设为壁纸」登记**全部保留**，壁纸库统计继续用这些数据；
  T153 改为断言页面上不再出现该区块。

### 4.26 当前壁纸卡移到壁纸管理页并重设计（2026-11-18，版本 `20261118o`）

- **位置**：`#wall-card` 从图库页移到壁纸管理页，放在统计卡下方、壁纸网格上方（随列表滚走，
  仍可折叠，折叠状态存 `vixiq.wall.folded`）。轮询只在壁纸管理页激活：
  `switchTab` 的 `wallpapers` 分支 `{ wallFilesEnter(); wallWatch(); }`，图库页不再启动。
- **展示信息**：缩略图（安全模式）+ 文件名 + 溯源徽章 + 壁纸夹位置「第 X / Y 张」；
  作品标题 / 作品 ID / 作者；当前壁纸尺寸与大小；**原图对比**；溯源结论；
  设置来源与时间；壁纸夹路径。
- **原图对比（用户 2026-11-18 要求）**：一张原图可能派生多个壁纸（裁剪/缩放/放大版），
  比较基准改为**溯源到的那张图库原图**，不是「库内最高清页」。
  `trace_current_wallpaper` 新增 `source_page`（`page_num/rel/width/height/size`）；
  `kind=library` 时也补 `match.page_num`（原先只给 rel，导致拿不到对比页）。
  前端 `wallSourceText` 按 `match.relation` + 面积判断「与原图尺寸一致 / 原图的裁剪版 /
  原图的放大版 / 原图的缩小版 / 原图旋转 90° / 比原图更大 / 比原图小」。
  `bigger`/`biggest` 字段保留（T127/T130 仍用）。
- **设置来源**：`/api/wallpaper/current` 新增 `usage`（`db.find_wallpaper_usage_by_path`，
  只查真实设置记录 `backfilled=0`）：显示「图库原图 / 裁剪版 / 壁纸夹 · 设置于 …」；
  系统幻灯片直接切的没有记录，显示「来源：系统幻灯片切换」。
- **按钮分组**：切换（上一张 / 下一张 / 随机）、查看（作者 / 在库中查看 / 复制链接）、
  工具（打开文件夹 / 图像溯源）、危险（移出壁纸，仅库外文件）。
  `/api/wallpaper/prev` 与 `?shuffle=1` 已接到卡片按钮。
- **清理副本**入口从卡片移到壁纸管理工具栏（扫描归属 / 深度扫描旁边）。
- **验收**：T127 增加 `source_page` 四档断言与移动后的静态护栏；T131/T137 更新按钮文案与
  `switchTab` 接线断言；全量 **146/146**。
- **缩略图空白修复（版本 `20261118o`）**：`root=img` 的 `/api/local-img` 只认**相对 `data/img`
  的完整路径**，而 `file.thumb` 原先只传文件名 → 图库内的当前壁纸缩略图 404。
  现在 `file` 增加 `rel`，`thumb` 与前端 `wallImgUrl` 图库内都用 `rel`；壁纸夹内仍用夹内文件名。
  T127 增加缩略图实际请求 200 断言。

### 4.27 A2 分类目录改名（2026-11-18，真实库已迁移）

- **结果**：`F:\Vixiq\data\img` 下 `X图`→`普通`、`涩图`→`R18`；`pages.local_path`、
  `pages.trash_rel_path`、`wallpaper_usage.file_path` 前缀同步更新；`library_sections.dir_name`
  改为 `普通`/`R18`。真实库 2188 页，改名后缺失文件 **0**；`wallpaper_usage` 图库原图路径
  790 条全部指向新前缀。当前桌面壁纸已重新设置为 `普通/.../其他来源_001.jpg`。
- **一次性脚本**：`_dev_runtime/diagnostics/rename_sections.py`（`--apply` 才执行；先备份数据库，
  再改磁盘，最后改库）。本机真实库已执行，不要把 `X图/涩图` 前缀再喂给它。
- **目录被占用**：桌面壁纸文件在 `X图` 下时 Windows 会锁住，`os.rename` 根目录报 WinError 5；
  脚本先把桌面切到临时 BMP。即使这样根目录仍可能被资源管理器占用 → 改为「新建目标目录 +
  逐项 `os.rename` 子项」，最后删空目录。`涩图` 空目录当时删不掉，已改名为 `.old-涩图`
  （`.` 开头不会被识别为分类目录），可稍后手删。
- **踩坑（已修复）**：脚本拼新前缀时漏了斜杠，`local_path` 变成 `普通作者夹/...`，全库判定缺失；
  `fix_section_slash.py` 已把 1551+637 条补回 `普通/...`、`R18/...`，复查缺失 0。
  **以后改路径前缀必须用 `新名 + "/"` 拼接并立刻做文件存在性复查。**
- **新库**：迁移 023 的初始目录名改为 `普通`/`R18`，回填同时兼容旧的 `X图`/`涩图`。
  T152 断言同步更新；T146 仍保留旧名输入，验证兼容识别。全量 **146/146**。
- **A2-2 按 R18 规则重排（已执行）**：先把所有未删除作品的 `section_id` 按 `is_r18` 归位
  （普通 1292、R18 310），再用 `_dev_runtime/diagnostics/reorder_sections.py` 逐页移动：
  `普通` 下的 R18 页 91 张 → `R18`，`R18` 下的非 R18 页 66 张 → `普通`，共 157 页，
  0 失败、0 冲突；清理旧空作者夹 27 个；`pages.local_path` 与 `wallpaper_usage.file_path`
  同步更新。复查 2188 页缺失 **0**、错位页 **0**；壁纸库仍 1180。
  说明：一键整理不会把作品从一个分类搬到另一个分类（它只保证「夹内/作者夹」正确），
  跨分类重排必须走这个专门脚本。
- **未做**：分类管理 UI 方案已回退，改为「标签维度 + 分区」方案，见 §4.28。

### 4.28 标签维度、标签清洗与分区绑定（2026-11-19，版本 `20261118q`）

- **目录统一回根目录**：`普通/`、`R18/` 下 460 个顶层项、2193 个文件合并回
  `F:\Vixiq\data\img\`，0 冲突；2188 页缺失 **0**；`library_sections.dir_name` 清空、
  `artworks.section_id` 清空；备份 `F:\Vixiq\data\backups\root-merge-20261119-002619.db`。
  `.old-涩图` 空目录被系统占用删不掉（点开头，不会被识别为分类目录）。
- **标签清洗（真实库已执行）**：`_dev_runtime/diagnostics/clean_tags.py`
  - 合并 20 组规范重复（22 个标签），保留频次最高者，迁移 `artwork_tags`。
  - 删除 246 个噪音标签：`xxxxusers入り` 收藏数、单字符、纯数字/符号。
  - 结果：2844 → **2576 个标签**，关联 10347 → **9026**；
    备份 `F:\Vixiq\data\backups\tag-clean-20261119-002750.db`。
- **迁移 027**：`tag_categories`（角色/风格/未分类，`dir_name` 为空=纯逻辑）、
  `category_tags` 多对多（一个标签可属多个维度）、`tags` 增加 `quality` 与
  `normalized_name`；现有标签全部先归入「未分类」。
- **数据层**（`db/store.py`）：`list_tag_categories / create_tag_category /
  update_tag_category / delete_tag_category / assign_tag_categories / list_tags /
  artwork_section_dir`。分区计算取作品命中的分区维度（`dir_name` 非空）中
  `sort_order` 最前的一个；未命中 → 根目录。
- **接口**：`GET/POST /api/tags/categories`、`POST/DELETE /api/tags/categories/<cid>`、
  `GET /api/tags?category_id=&q=`、`POST /api/tags/<tag_id>/categories`（多维度）。
- **前端**：图库筛选行新增 🏷️ 按钮，弹窗内可新增/改名/绑定分区目录/排序/删除空维度，
  并按维度勾选标签（多选即时保存）。维度绑定目录只是「分区配置」，
  **按标签把文件搬进分区目录的迁移向导尚未做**。
- **验收**：T156（数据层/多维度/分区计算/删除边界）、T157（接口与前端接线），
  全量 **148/148**。
- **未做**：分区应用向导（预览/备份/移动）、图库按标签维度筛选、来源字典迁移、
  标签维度的批量归类。

### 4.29 标签管理工作台（2026-11-19，版本 `20261119a`）

- **入口**：图库管理筛选行的「🏷️ 标签管理」（`#lib-tagcat-manage`），弹窗为 `modal-xl`；
  关闭后图库原有筛选、页码和选择保持不变。
- **布局**：左侧类别导航（全部标签 / 未分类 / 各标签类别 / 已停用 + 计数），右侧服务端分页列表
  （每页 20，总数与页码）；搜索覆盖中文名、原名、英文名；点「作品数」切换升降序。
- **列表**：名称、所属类别 pill（不显示「未分类」占位）、关联作品数；支持全选本页、
  跨页/搜索保留勾选、查看已选、清空；切换类别清空勾选。
- **批量**：加入类别保留旧归属，可一次选多个类别；移出只删当前类别；
  标签失去最后一个类别后自动回到「未分类」，有了真实类别后自动退出「未分类」。
- **编辑**：点标签名打开编辑，可改显示名（NFKC 同名检查，全半角也拦截）和类别归属；
  停用前展示关联作品数，停用保留作品关联，可在「已停用」恢复。
- **停用效果**：`tags.status='disabled'` 不进入主列表、不进入 `list_all_tags` 的筛选推荐、
  不计入类别标签数、不参与 `artwork_section_dir` 分区计算。
- **类别设置**：新建、改名、选择/更换/解除绑定文件夹、上移调整分区优先级、删除空类别；
  内部表/接口仍叫 `tag_categories`，界面统一文案「标签类别」。
- **选择文件夹**：本机复用 `utils.folder_dialog.pick_folder(title, initial=...)`，
  默认定位图库根/当前绑定目录；确认后只回填只读草稿，点保存才写规则；
  取消保留原选择；远程（非 127.0.0.1 或带 CF 头）返回 403。服务端只接受图库根内
  真实存在的相对目录，创建和更新类别都做同样校验。
- **数据层**：迁移 `028_tag_status.sql`（`tags.status` + 索引）；
  `tag_workspace_counts / list_tags_page / batch_tag_categories / update_tag_meta /
  set_tags_status`；`list_all_tags`、`list_tag_categories` 与 `artwork_section_dir`
  均过滤停用标签。
- **接口**：`GET /api/tags/page`、`POST /api/tags/batch`（add/remove/disable/restore）、
  `POST /api/tags/<id>/edit`、`POST /api/tags/pick-folder`。
- **验收**：T157 更新为新前端接线与目录越界断言；新增 T158（130 条分页/末页、三种搜索、
  ids 跨页选择、多类别加入保留旧归属、移出最后类别、停用恢复与筛选不可见、编辑/同名/全半角、
  选择文件夹选择/初始定位/取消/越界/保存/解除、远程 403、前端接线），全量 **149/149**
  （2026-11-19 16:13 实测 73.55s）。
- 界面实测（源码 7811，合成 130 个标签）：分页 1/7、搜索、批量加入角色+风格、移出、
  改名、停用/恢复、类别设置新建/删除空类别均通过；浏览器未执行真实「选择文件夹」
  （会弹系统模态），该路径由 T158 用对话框替身覆盖。
- **注意**：`_cleanup_orphan_tags()` 会在删除作品 / 保存作品标签 / 图库同步时删除
  **没有任何作品关联**的标签；当前工作台不新建无主标签，不受影响。后续若增加
  「新建标签」或允许清空标签关联，需要先调整这个旧清理规则。
- **部署（2026-11-19）**：候选包 `_dev_runtime/packages/vixiq`（`built_at 16:13:58`）在独立 7813 +
  `_dev_runtime/candidate-data` 验收通过：迁移 027/028、`tags.status`、三个类别、
  `/api/tags/page`、远程 `pick-folder` 403、页面入口与 `TAGWS` 均正常。
  安装版 `D:\Program Files\Vixiq` 已替换 `vixiq.exe`、`_internal/web/static`、
  `_internal/db/migrations`，旧文件备份 `.backup-tag-workspace-20261119-1625`；
  真实库升级前备份 `F:\Vixiq\data\backups\tag-workspace-20261119-1615.db`。
  安装版 `/api/version` 前端 `20261119a`，真实库 `schema_migrations` 含 028，
  `tags` 2576 条全部 active，`/api/tags/page` 返回 2576 / 129 页；
  浏览器实测分页与搜索正常（搜索为子串匹配，符合「搜索覆盖」要求）。
  `dist/vixiq` 已同步同一份程序/资源/迁移；GitHub **未提交、未推送**。

### 4.30 图库按标签类别筛选（2026-11-19，版本 `20261119b`）

- **入口**：图库筛选栏新增「标签类别」下拉（`#lib-f-tagcat`），选项来自
  `/api/tags/categories`，含「标签类别: 全部 / 角色 / 风格 / 未分类」。
- **联动**：选中类别后「标签」下拉只列该类别下的标签；选具体标签时自动把类别切到
  该标签的第一个真实类别；切换类别会清空已选标签。清空筛选、「全部作品」、
  「仅查看该作品」撤销都会同步回填类别下拉。
- **后端**：`list_artworks / count_artworks_filtered / list_images /
  count_images_filtered` 新增 `tag_category` 参数，用 EXISTS 子查询匹配
  `category_tags`，只认 `tags.status='active'`；作品视图与图片视图同口径，
  可与 `tag` 条件叠加。
- **facets**：`list_all_tags` 返回每个标签的 `categories`，前端据此过滤标签候选。
- **验收**：T159（3 个作品分属角色/风格/未分类、作品+图片双视图、类别+标签叠加、
  停用标签不参与、facets 带类别、前端接线），全量 **150/150**。
  浏览器实测：选「角色」只剩验收作品1、标签候选只剩「验收角色」；切「风格」只剩验收作品2。
- **部署（2026-11-19）**：候选包 `_dev_runtime/packages/vixiq`（`built_at 17:49:32`）在 7813
  独立数据验收通过：`/api/version` 前端 `20261119b`、`/api/library/items?tag_category=...`
  正常、页面含 `#lib-f-tagcat`。`dist/vixiq` 与安装版 `D:\Program Files\Vixiq` 的
  `vixiq.exe` / `_internal/web/static` / `_internal/db/migrations` 已同步（哈希一致），
  旧文件备份 `.backup-tagcategory-20261119-1753`。本次无新迁移。
  **F 盘（USB 外接 SSD）已拔除，安装版保持退出，插回后启动即生效**；桌面壁纸当前指向
  F 盘图库文件，拔盘期间可能失效。GitHub **未提交、未推送**（提交 `59d236b` 仅本地）。

### 4.31 外接数据盘缺失时启动卡动画（2026-11-19，用户实测）

- **现象**：F 盘（USB 外接 SSD，安装版数据目录 `F:\Vixiq\data`）拔掉后启动安装版，
  启动动画卡约 3 分钟才打开。
- **根因**：
  1. `/api/health` 调用 `_cookies_path().exists()`，F 盘不存在时
     `paths.user_data()` 的 `mkdir` 抛 `FileNotFoundError: F:\` → 健康检查返回 500。
  2. `main._port_alive()` 只把 `200 <= status < 500` 当「活着」；`urllib` 对 500 抛
     `HTTPError`，被 `except` 当成「没启动」。
  3. `_start_ui_when_ready()` 轮询 120 次，每次都等不到「就绪」，约 3 分钟才关启动动画。
- **修复**：
  - `api_health` 吞掉数据盘路径异常，固定返回 `200 {ok:true}`；
  - `_port_alive` 捕获 `urllib.error.HTTPError` → 返回 `True`（有 HTTP 状态码就说明
    服务已在监听；业务 500 不算进程死亡）；连接失败才返回 `False`。
- **验收**：T160（health 在数据盘异常时仍 200、HTTP 500 视为活、连接失败为死、
  启动轮询接线），全量 **151/151**。
- **边界**：数据盘缺失时其他业务接口仍会 500（数据确实不可用），但启动动画会立即关闭，
  页面能打开并显示接口错误，不再白等。
- **部署（2026-11-19）**：重新打包 `_dev_runtime/packages/vixiq`（`built_at 19:33:19`），
  安装版 `vixiq.exe` / `_internal/web/static` / `_internal/db/migrations` 已替换，
  旧程序备份 `.backup-boot-health-20261119-1933`；`dist/vixiq` 已同步，三处哈希一致。
  F 盘仍拔出，实测启动到 `/api/health` 返回 200 为 **6.4 秒**（修复前 187 秒仍未就绪）；
  安装版 `/api/version` 前端 `20261119b`、`frozen=true`、`built_at 19:33:19`。
  其他业务接口在 F 盘缺失时仍返回 500（数据确实不可用），插回 F 盘后重启即恢复。
  GitHub 本地提交（见汇报），**未推送**。

### 4.32 图库筛选栏两行紧凑布局（2026-11-20，版本 `20261119c`）

- **范围**：只改图库管理筛选栏的排版与文案，沿用现有筛选 ID、事件、联动和滚动/吸顶约定。
- **桌面两行**：
  - 第一行：作者搜索、标签类别 + 标签搜索、排序 + 升降序、重置筛选、标签管理；
  - 第二行：类型、作者状态、来源 + 来源管理滑杆图标、异常项、R18、AI 生成。
  - 控件高 29px、间距 4px、按钮按文案定宽；来源滑杆 29×29，保留原 SVG 与「管理来源分类」提示。
- **窄屏（≤980px 保守断点，覆盖 720px 场景）**：
  - 第一行只留作者、标签搜索；第二行依次「更多筛选」、排序 + 方向、重置、标签管理；
  - 「更多筛选」展开独立面板，收纳标签类别和类型/作者状态/来源/异常项/R18/AI 生成；
  - 面板收起时按钮右上角蓝点提示有筛选启用；展开/收起/响应式切换保留所选值与输入。
  - 来源管理滑杆随来源并列；不出现第三行或横向滚动。
- **320px**：「重置筛选」缩为「重置」（`aria-label` 保留完整名称），排序方向只显示箭头
  （`aria-label` 保留升降序说明），标签管理只留图标（`aria-label`/`title` 保留），
  触屏控件点击区域至少 42px。
- **文案**：异常→「异常项」、状态→「作者状态」、全部作品→「重置筛选」、AI→「AI 生成」；
  AI 下拉值不变（`''` 全部 / `'2'` 是 / `'0'` 否）。重置恢复默认值，R18 回到「非 R18」。
- **代码**：`index.html` 的 `#lib-filters` 改为 `.gf-band-search` + `.gf-band-facets` 两行；
  `app.js` 新增 `initGalleryFilterLayout/syncLibOrderButton`；`style.css` 新增 `#lib-filters .gf-*`
  作用域规则（选择器必须带 `#lib-filters.gf-toolbar`，否则会被桌面 flex 规则盖掉媒体查询的 grid）。
- **验收**：T161 静态结构/文案/尺寸/断点/接线；Selenium 多视口实测 1280/800/390/320：
  全部两行、`scrollWidth == innerWidth`、桌面控件 29px、触屏 42px、更多筛选联动/蓝点/
  排序/重置非 R18、来源管理与标签管理入口均正常；全量 **152/152**。
- **部署（2026-11-20）**：只改前端，`index.html`/`style.css`/`app.js` 已同步
  `_dev_runtime/packages/vixiq`、`dist/vixiq`、`D:\Program Files\Vixiq` 三处，
  哈希一致；安装版旧资源备份 `.backup-gallery-filters-20261120-1433`。
  安装版已启动（F 盘插回），`/api/version` 前端 `20261119c`；Selenium 在
  1280/800/390/320 四个视口复测通过（两行、无横向滚动、更多筛选/排序/重置/
  标签管理/来源管理入口正常）。GitHub 本地提交，**未推送**。

### 4.33 健康条可点看明细 + 托盘文件夹入口（2026-11-20，版本 `20261120a`）

- **健康条**：`⚠ 文件缺失 / 📁 空目录 / 💥 损坏` 三个徽标由只读 `span` 改为按钮，
  点击打开「明细」弹窗：
  - 空目录：列出绝对路径，每条可在资源管理器打开，底部「删除空文件夹」；
  - 文件缺失：列出作品 ID（可直接跳到图库定位），底部「清理缺失记录」；
  - 损坏文件：列出文件路径，可打开；修复入口仍走「🩺 体检」的重下修复。
  - 「🩺 上次体检：待处理 N」这行文字也可点击，打开完整体检报告。
  - 数据来自实时对账 `window._libHealth`，不是上次体检快照；空目录明细最多列 200 条。
- **托盘**：
  - 原「打开所在文件夹」表述不清，改为「打开当前壁纸文件夹」（打开并选中当前壁纸）；
  - 新增「打开图库文件夹」= 图库根目录（`paths.user_data(paths.img_subdir)`，
    安装版即 `F:\Vixiq\data\img`），不是程序安装目录；命令 `CMD_OPEN_LIBRARY=1008`。
- **测试**：T162 静态接线；T128 托盘菜单顺序/文案/缺文件灰字全部更新；全量 **153/153**。
- **测试用空目录**：`F:\Vixiq\data\img\空文件夹测试`（用户要求保留供点击测试；
  验完可点明细里的「删除空文件夹」清掉）。
- **部署（2026-11-20）**：前端 `20261120a`，安装版 exe `built_at 16:15:31`；
  `_dev_runtime/packages`、`dist/vixiq`、`D:\Program Files\Vixiq` 四处资源哈希一致。
  安装版旧 exe 备份 `.backup-health-tray-exe-20261120-1616`，旧前端资源备份
  `.backup-health-detail-20261120-1458`。安装版已启动，`/api/version` 前端
  `20261120a`；健康条空目录明细在安装版复测通过。托盘新菜单由 T128 覆盖，
  重启后右键可见「打开当前壁纸文件夹 / 打开图库文件夹」。
  GitHub 本地提交，**未推送**。

### 4.34 「一键整理」更名为「整理文件夹」（2026-11-20，版本 `20261120b`）

- 用户反馈「一键整理」名不副实（它做的是归位/改名/归类，不是一键完成所有事），统一改名为「整理文件夹」。
- 界面：图库工具栏按钮、预览/执行/失败弹窗标题、帮助文案同步改名；`app.js` 注释中的历史名称保留。
- 后端：审计日志动作名改为「整理文件夹」；`db/store.py` 跨目录认领报错改为「请先用整理文件夹归拢」。
- 验收：`py_compile` + 全量冒烟 **153/153**（76.70s）；候选包 `built_at 16:56:36` 在 7813 独立数据验收通过。
- 部署：`_dev_runtime/packages`、`dist/vixiq`、`D:\Program Files\Vixiq` 已同步（安装版备份
  `.backup-rename-tidy-20261120-165854`，dist 备份 `.backup-rename-tidy-20261120-165926`）；
  安装版已启动，`/api/version` 前端 `20261120b`，页面按钮显示「🧹 整理文件夹」，
  真实库 1603 作品 / 2191 图正常，整理预览返回 0 计划。
- GitHub 本地提交，**未推送**。

### 4.35 健康条正常时只亮一会儿（2026-11-20，版本 `20261120d`）

- **反馈**：同步磁盘、刷新、整理文件夹等操作完成后，健康条在一切正常时完全不显示，用户看不到结论；
  初版改为常驻后又确认「不是常驻，只显示一会儿就行」。
- **最终行为**：`paintLibHealth(flash)` 正常时亮 **8 秒**（`HEALTH_OK_SHOW_MS`）后自动收起；
  周期轮询传 `flash=false`，不会重新点亮；有缺失/空目录/损坏/体检报告/体检中则一直显示到问题解决。
  同步磁盘、万能刷新、整理文件夹、导入都会走 `libSync()` → `paintLibHealth(!periodic)` 同一处重画。
- **验收**：T162 断言 8 秒定时器与 `paintLibHealth(!periodic)`；全量冒烟 **153/153**；
  安装版无头实测：进入图库亮起「✓ 未发现异常」→ 8 秒后 `display:none` → 点「🔄 同步磁盘」再亮 →
  8 秒后收起 → 等 16 秒周期轮询仍保持收起。
- **部署**：只改前端，`index.html` / `app.js` / `style.css` 已同步
  `_dev_runtime/packages/vixiq`、`dist/vixiq`、`D:\Program Files\Vixiq`，四处哈希一致；
  旧资源备份 `.backup-health-flash-20261120-193555`。安装版无需重启，刷新页面即生效。
- GitHub 本地提交，**未推送**。

### 4.36 整理文件夹：本轮搬空的目录同一次收尾（2026-11-20，版本 `20261120e`）

- **反馈**：整理把文件搬走后原目录变空，还要再点一次「整理文件夹」才能检测出来。
- **预览**：
  - `_tidy_build_plans` ④ 同时列「当前真·空目录」和「本轮 move 后预计搬空的源目录」
    （`planned=True`，reason 注明「本轮移动后预计搬空」）；只列最上层，move 目标夹及其上级
    仍排除（T154 事故防线保留）。
  - 新增 ⑤：move/filrename/rename 目标已存在的，计划上标 `conflict`，reason 写明「执行时会跳过」。
  - 预览计数新增 `rmdir_pending`、`conflict`。
- **执行**：
  - `_tidy_apply_plans(..., expected=None)` 接收前端预览快照；计划消失或 `type/from/to/files`
    变化 → 记入 `stale` 不执行。前端 `applyTidy` 提交 `expected`，stale/db_failed 用弹窗列出。
  - 写库与磁盘分开兜底：`src.rename` 成功后 `relink_page_path` / `rewrite_page_path_prefix` /
    `refresh_ext_titles` 失败记入 `db_failed`（old/new/stage/error）；目标冲突、源消失改记 `stale`。
  - 空夹收尾由 `shutil.rmtree` 改为由深到浅逐个 `os.rmdir`；目录非真空（含任意文件、隐藏文件、
    Thumbs.db、失败残留、被占用）一律保留。`_tidy_dir_is_disposable` 改为「子树里没有任何文件」。
- **健康入口**：健康明细与体检报告的「删除空文件夹」改为打开整理预览；删除 `cleanEmptyDirs`
  与 `POST /api/library/clean-empty-dirs`（旧的递归直删路径下线）。
- **验收**：T62 改为「Thumbs.db/隐藏文件/PSD/TXT 保留」；T154 两道防线保留；新增 T163 覆盖
  同次搬空收尾、含 PSD 保留、目标冲突、预览后放入文件、写库失败；T162 覆盖健康明细入口与旧接口下线；
  全量 **154/154**。安装版无头实测：空目录明细按钮「🧹 打开整理预览」→ 点击打开「🧹 整理文件夹」。
- **部署**：后端+前端，候选包 `built_at 20:13:15` 在 7813 独立数据验收通过；
  安装版 `D:\Program Files\Vixiq` 已替换并重启（前端 `20261120e`，真实库 1603 作品 / 2191 图，
  预览 0 计划），备份 `.backup-tidy-same-pass-20261120-201510`；`dist/vixiq` 已同步，备份
  `.backup-tidy-same-pass-20261120-201626`；三处 7 个资源与 exe 哈希一致。GitHub 本地提交，**未推送**。
- **验收补强（2026-11-20，版本 `20261120f`）**：
  - 预览计划新增稳定 `key`（`type|from|to`）与 `sources`（每个源文件 size/mtime_ns），前端 `expected` 一并提交。
  - 执行按 `key` 匹配已确认项（新增无关计划导致序号后移不影响），并核对 files 与源文件状态；
    同路径同大小替换源文件 → 该项 `stale`、留在原位，不移动被替换的新文件。
  - 新增 T164 覆盖上述两条；全量 **155/155**。候选包 `built_at 22:56:09` 在 7813 验收通过；
    安装版/dist 已同步（前端 `20261120f`），备份 `.backup-tidy-snapshot-20261120-225709` /
    `.backup-tidy-snapshot-20261120-225743`；三处 7 个资源与 exe 哈希一致。GitHub 本地提交，**未推送**。
- **浏览器纳秒精度修复（2026-11-20，版本 `20261120g`）**：
  - 复验发现 `mtime_ns` 以 JSON 数字发送，超过 JS 2^53；浏览器 `JSON.parse` / `stringify`
    往返后变成双精度，未变化的文件也被判「预览后已被替换」。
  - `sources` 的 `size` / `mtime_ns` 一律改为字符串（后端 `str()`），比较只做字符串相等；
    从预览到执行全程不经过 JS Number。
  - T164 加入真实 Node（V8）JSON 往返：超过 2^53 的纳秒值未变化时正常移动，同大小改写后仍拦截；
    新增无关空目录的 key 回归保留。全量 **155/155**。
  - 候选包 `built_at 23:30:05` 在 7813 用合成库完成页面「预览 → 执行」验收（移动归位 + 源目录同次收尾）；
    安装版/dist 已同步（前端 `20261120g`），备份 `.backup-tidy-mtime-string-20261120-233254` /
    `.backup-tidy-mtime-string-20261120-233327`；三处 7 个资源与 exe 哈希一致。GitHub 本地提交，**未推送**。

### 4.37 备份对比接入图库安全模式（2026-11-21，版本 `20261121a`）

- **背景**：备份中心「🔀 对比」左右两侧原来直接用真实 URL 拼 `<img>`，安全模式下 R18 也会加载像素；
  T101 曾给它 `allow-raw-img` 豁免。
- **分级（可靠归属）**：`/api/library/backups` 用 `pages.local_path == 原文件相对路径` 查作品，
  返回 `owner_known` 与 `is_r18`（0/1/None）；文件名的自然键只用于显示，不用于分级。
- **前端**：`compareBackup` 两侧都走 `safeThumb(url, it)` —— 安全模式下只有明确 `is_r18 === 0`
  才加载真实图片；R18、`is_r18` 为空、归属无法确认一律 `R18_MASK_SVG` 占位，不产生图片请求。
  对比弹窗继续显示文件名、大小、时间，并注明占位原因。
- **后端兜底**：`/api/library/backup-img` 在安全模式下按同一归属重新校验，
  非「明确全年龄」返回纯文字 SVG 占位，绝不返回 `.bak` 字节；关闭安全模式后恢复真实字节。
- **测试**：T165 覆盖全年龄/R18/NULL/归属不明四种分级与后端占位；T101 移除 `allow-raw-img`
  豁免并继续扫描裸 `/api/local-img`。全量 **156/156**。
- **页面验收**：候选包 `built_at 00:15:56` 在 7813 用合成 `.bak` 数据实测：
  全年龄两侧各 1 条真实请求；R18 与分级未知 0 条对比请求、`<img>` 为 data URI，文件名/大小/时间可见。
- **部署**：安装版与 `dist` 已同步并重启（前端 `20261121a`，真实库 1603 作品 / 2191 图）；
  备份 `.backup-backup-safemode-20261121-002333` / `.backup-backup-safemode-20261121-002408`；
  三处 7 个资源与 exe 哈希一致。GitHub 本地提交，**未推送**。

### 4.38 智能刷新：前台先快、后台按范围补查 + 进度动画（2026-11-21，版本 `20261121c`）

- **前台**：点击只重拉当前视图/计数（图库列表 force、统计、筛选项、回收站徽标），保留搜索/筛选/页码/勾选；
  不再等待全量磁盘同步，慢盘也不挡页面。按钮点击立即反馈「正在更新页面」。
- **后台**：按 `lastAction` 定向核对 —— 作品/作者按 id 逐个提交 `/api/library/refresh-scope`
  （作者分支含联网核对名字/存续）；无明确范围时（外部往图库塞图片）走 `/api/library/sync` 做磁盘索引同步。
- **进度与动画**：
  - `#refresh-status` 浮层延迟 300ms 出现（短任务不弹）；阶段文字 + 实际计数「1 / 4 项」，
    条宽随真实完成数 180ms 平滑过渡；未知总量（磁盘同步）用流动条 + 「检查目录」。
  - 入场 160ms 淡入/上移；完成显示「新增 N 张 · 重新关联 M 张」等实际结果，3 秒后淡出；
    失败/部分失败停止动画、保留原因，重试只重试失败 id。
  - 连续点击/切页复用 `_refreshJobs` 在途任务与现有进度，不从头重播。
  - `prefers-reduced-motion` 下关闭动画（静态条 + 文字/计数）；读屏只播报阶段变化（计数 aria-hidden）。
  - 窄屏 `right:96px`、`bottom:calc(76px + safe-area-inset-bottom)`，避开底部导航/返回顶部；
    图库筛选栏仍为两行（390px 实测高 62px、无横向溢出）。
- **验收**：T166 静态接线；候选包浏览器实测（合成 3 作品）：
  已知总量 1/4→4/4、宽度单调、入场 `rs-in`、3 秒收起；未知总量流动条 `rs-flow`、
  完成后「新增 1 张 · 重新关联 0 张」+ 整理入口；快速完成不弹浮层；连续 3 次点击只 3 个
  refresh-scope 请求且进度不回退；部分失败 1 项、重试只 1 个失败 id；减少动态动画 none；
  390px 状态条无越界/不挡导航、筛选栏两行。全量 **157/157**。
- **部署**：只改前端，`index.html` / `app.js` / `style.css` 已同步
  `_dev_runtime/packages/vixiq`、`dist/vixiq`、`D:\Program Files\Vixiq`，四处哈希一致；
  各落点旧资源备份 `.backup-smart-progress-20261121-023536`。安装版已重启，前端 `20261121c`。
  GitHub 本地提交，**未推送**。
- **验收补强（2026-11-21，版本 `20261121d`）**：
  - 作者定向刷新只更新资料，不再直接改磁盘作者夹；目录改名必须走整理预览。T167 覆盖
    （旧文件与 `pages.local_path` 保持，整理计划出现 `unknown_77 → New_77`）。
  - 磁盘同步非 2xx / 业务失败进入失败态，不写入 `_libHealth`；部分成功分别展示已完成计数与失败原因。
  - 多范围并行：状态条按当前任务排队；成功任务不覆盖/不收起在途任务；另一范围有失败待重试时，
    新任务和成功任务都不覆盖失败入口。
  - 运行时回归（候选合成库 + 请求拦截）：sync 500 → 失败 + 健康数据不变 + 重试成功；
    A 完成后 B 仍在途 → 显示 B 的「检查目录/检查整理项」且 3 秒后不消失；A 失败 + B 成功 →
    失败入口保留，重试只失败项。全量 **158/158**（新增 T167）。
  - 部署：候选包 `built_at 03:12:26`；安装版/dist 已替换（前端 `20261121d`），备份
    `.backup-smart-refresh-review-20261121-031749` / `.backup-smart-refresh-review-20261121-031829`；
    四处资源与 exe 哈希一致。GitHub 本地提交，**未推送**。
- **复验补缺（2026-11-21，版本 `20261121e`）**：
  - 部分同步失败不再当全成功：`/api/library/sync` 返回 HTTP 200 但带 `write_skipped`
    （如 `database is locked`）时进入失败态，保留已完成的新增/重新关联计数，显示
    「部分同步未完成：<原因>」，可同范围重试；失败/部分失败一律不写 `_libHealth`。
    HTTP 500 JSON 失败、正常完整同步成功两组行为保持不变。
  - 多任务失败改为按范围保留：单值 `_refreshRetry` 换成 `_refreshRetries: Map<key,{act,stage}>`；
    `_refreshRetryAction(key)` 生成「重试」+「下一个失败（N）」，`renderRetryStatus()` 渲染当前失败项，
    `cycleRefreshRetry()` 循环切换，`retryRefreshJob()` 只消费当前 key。重试 B 只请求 B，
    B 成功后仍能看到并重试 A；成功任务不吞掉其他范围失败，也不收起来在途任务。
  - 回归脚本（均在忽略目录）：`check_smart_refresh_recheck.py`（write_skipped / HTTP 500 /
    A、B 双失败独立重试、部分失败只重试失败 id）、`check_smart_refresh_review.py`、
    `check_smart_refresh.py`（进度动画/快速完成/连续点击/并行进度/减少动态/390px）；
    运行时实测：拦截返回 `{status:'ok',new_pages_linked:2,pages_relinked:1,write_skipped:'database is locked'}`
    → 状态条显示部分失败与原因、健康数据不变、重试后成功；A/B 失败下 cycle 切到 A 后重试请求
    仅 `{"scope":"work","ids":["<A>"]}`。全量 **158/158**，`py_compile`、`node --check` 通过。
  - 部署：只改前端，`index.html` / `app.js` 已同步 `_dev_runtime/packages/vixiq`、`dist/vixiq`、
    `D:\Program Files\Vixiq`（备份 `.backup-smart-refresh-recheck-20261121-033205`，`style.css` 未改）；
    安装版未重新打包（exe 仍为 `built_at 03:12:26`），启动后 `/api/version` 前端
    `20261121e-1795202798-1795201946-1795201946`，真实库 1603 作品 / 2191 图正常。
    GitHub 本地提交，**未推送**。

### 4.39 同步磁盘索引：对账结果分项、缺失单独处理（2026-11-21，版本 `20261121f`）

- **入口**：图库工具栏「🔄 同步磁盘索引」（`#lib-sync-btn`）改走 `manualSyncDisk()`，
  复用智能刷新的 `#refresh-status` 进度条（阶段「检查目录」、失败重试、完成后保留「查看同步结果」）。
- **对账口径**（`db/store.py` `reconcile_output`）：
  - 扫描前确认图库根目录可访问：外接盘未接上时返回 `root_available=False` 的空报告，
    **不产生缺失判定**；盘接回后下一次调用立即重扫。`/api/library/sync` 与 `/api/library/health`
    此时返回 503 +「图库不可用」，不可达期间 `clean-missing` 同样 503，不写缺失、不清理记录。
  - 磁盘遍历改 `os.walk(onerror=...)`：子目录/文件读不了时 `scan_incomplete=True`、
    `unreadable_dirs` 记录路径；该目录下已登记作品的缺失判定暂缓，重指候选也跳过。
  - 同 MD5 对多个候选时不再随便挑第一个重指，统一进 `pending`（`ambiguous_md5`）；
    同名不同内容的 `conflicts` 也计入待确认，前端健康条显示「❓️ 待确认 N」。
  - 对账响应新增 `root_available / scan_incomplete / unreadable_dirs / pending`。
- **缺失处理**：`GET /api/library/missing-items` 预览（标题/作者/每条登记路径是否存在）；
  `POST /api/library/clean-missing` 必须传勾选 `ids`，执行前强制重新对账，只处理仍缺失的；
  `recycle_files=false` 只删索引，`true` 才把残留文件送 Windows 回收站（`_recycle_files`，
  不再直接 `unlink`）。预览弹窗默认全选、可逐项取消，两个按钮为「只清理索引记录」
  「清理索引并回收残留文件」。
- **验收**：T168（隔离库：根目录不可用、子目录不可读暂缓缺失、多候选 pending、接口 503、
  恢复后重对账、勾选清理/默认不动磁盘/回收替身），全量 **159/159**，
  `py_compile`、`node --check` 通过。源码 7811 与冻结候选 7813 浏览器实测：
  手动入口阶段「检查目录」→ 完成「新增 1 张 · 重新关联 2 张 · 缺失 1 · 待确认 1」；
  结果面板分项展示；缺失预览 2 项默认全选，取消一项后请求仅勾选 id 且 `recycle_files=false`；
  图库不可用时状态条/健康条均为「图库不可用」，周期轮询静默；恢复后重试成功。
- **部署**：后端+前端，候选包 `built_at 04:07:21` 在 7813 合成数据验收通过；
  安装版 `D:\Program Files\Vixiq` 与 `dist\vixiq` 已替换 `vixiq.exe` + 7 个前端资源
  （哈希一致，`config/user.json` 未变），备份 `.backup-sync-disk-20261121-041859`；
  安装版已启动（前端 `20261121f`），真实库 1603 作品 / 2191 图、缺失 0、安全模式开启。
  GitHub 本地提交，**未推送**。

### 4.40 4B-1 入库统一转 PNG 并记录来源（2026-11-21，版本 `20261121g`）

- **统一流程**：新增 `core/image_format.normalize_to_png`。原生 PNG 直接登记；
  GIF 与多帧 WEBP 保持原格式；JPG/JPEG/WEBP/BMP/TIF/TIFF 等静态格式转 PNG。
  转换先写同目录临时 PNG 并校验（verify + 真正解码 + 尺寸），成功后才替换目标；
  失败保留可用原文件并返回原因。EXIF 方向在转换时转正。
- **下载端**：`crawler._normalize_download` 接入正常下载与补页（浏览器通道与 HTTP 直连两处），
  转完再 `mark_page_status`；`_record_page_conversion` 写转换记录。目标 PNG 已存在时：
  像素一致就删掉刚下的 JPG、复用已有 PNG；不一致才把旧 PNG 备份 `.bak` 后覆盖
  （强制重下沿用既有 `.bak` 流程）。转换失败仍登记原格式文件，记录失败原因。
- **上传/路径导入**：`_ingest_image_file` 统一「应用临时副本 → 转 PNG → 入库」；
  外部源文件只读，转换阶段保持原位，导入成功后仍按既有导入语义送回收站。
  `_record_ingested_conversions` 在对账补页后按 local_path 写转换记录。
  重复导入同一 JPG 用 `pages.source_md5` 识别并复用；同页已有 PNG 用像素指纹核对，
  一致复用、无法确认列 `pending` 并保留现有登记；符合图库只留一份 PNG 的规则。
- **迁移 `029_page_conversion.sql`**（必须带 `-- UP:`/`-- DOWN:` 分节）：
  `pages` 新增 `source_format / source_file_size / source_md5 / converted_at /
  current_format / convert_note`，并建 `source_md5` 索引。记录挂在页上，
  整理/改名只改 `local_path`，记录随页保留。历史 PNG 无记录时 `converted_at` 为空。
- **只读接口**：`GET /api/library/page-conversion?artwork_id=&page_num=` 返回当前格式、
  大小、原格式/原大小/原始指纹、转换时间与失败原因。
- **验收**：T169 覆盖转换函数（JPG/WEBP 转 PNG、原生 PNG、GIF/多帧 WEBP、损坏、
  目标已存在、overwrite 备份）、下载端复用/覆盖/记录、路径导入（只留 PNG、源文件原位、
  重复导入复用、同页冲突 pending、转换失败保留原格式）、上传接口与接口静态接线；
  T31 按新规则更新（同名不同内容不再落第二份，列待确认）。全量 **160/160**，
  `py_compile`、`node --check` 通过。
- **未做**：页级详情展示「由 JPG 转为 PNG / 来源未记录」（留到 4B-2/页详情）；
  手动放入图库的 JPG 在同步时识别与确认转换（4B-3）。
- **部署**：后端+前端，候选包 `built_at 06:06:41` 在 7813 合成数据验收（上传 JPG →
  `990601_p0.png`，转换记录含 source_format/source_md5/大小/时间）；安装版与 `dist\vixiq`
  已替换 `vixiq.exe`、7 个前端资源与 `_internal/db/migrations/029_page_conversion.sql`
  （哈希一致，`config/user.json` 未变），备份 `.backup-png-ingest-20261121-061100`；
  真实库升级前备份 `F:\Vixiq\data\backups\png-ingest-20261121-061100.db`。
  安装版已启动（前端 `20261121g`），真实库 1603 作品 / 2191 图，
  `schema_migrations` 含 029，历史页 `page-conversion` 返回「无转换记录」。
  GitHub 本地提交，**未推送**。
- **验收返修（2026-11-21，版本 `20261121h`，返修单 4 项）**：
  1. 路径导入/文件夹导入不再把外部源文件送回收站：删除 `to_recycle` 与 `_recycle_files`
     调用，成功/复用/转换失败/待确认都只操作应用临时副本；前端预览、完成提示、审计文案同步改为
     「源文件保持原位」。
  2. 来源指纹命中后必须核对库内当前文件：`_ingest_image_file` 只在现图存在且可打开、
     像素一致时复用；现图缺失时继续走导入补回（对账/`restored` 更新页路径），现图损坏则列待确认。
  3. `_normalize_download` 普通下载/补页遇到同页不同内容时不再覆盖：删除刚下的文件、保留现图与登记、
     报告冲突；只有 `force_overwrite=True` 才备份 `.bak` 后覆盖。浏览器与 HTTP 两路共用
     `_finalize_downloaded_page`。
  4. 下载收尾统一看转换/校验结果：打不开的输入不登记（旧图可用则保留旧登记，否则标 FAILED）；
     转换失败但原格式可读时登记原格式并标「后续可重试」；登记/转换记录写入/临时副本清理失败
     都会在任务日志标明阶段并可重试。
  回归：T169 增补四项返修断言（源文件字节不变且回收替身从未调用、现图移走补回、现图损坏待确认、
  普通/补页冲突保留、强制重下、损坏输入、可用回退、登记失败、记录写入失败）；T74 按源文件原位更新。
  全量 **160/160**，`py_compile`、`node --check` 通过。
  部署：候选包 `built_at 06:47:02` 在 7813 合成数据验收（路径导入源文件原位、重复复用、
  现图移走后补回、转换记录完整）；安装版与 `dist\vixiq` 已替换 `vixiq.exe` + 7 个前端资源
  （哈希一致，`config/user.json` 未变），备份 `.backup-png-ingest-recheck-20261121-064935`；
  安装版已启动（前端 `20261121h`），真实库 1603 作品 / 2191 图。GitHub 本地提交，**未推送**。
- **复验返修：强制重下保留现图（2026-11-21，后端，前端仍 `20261121h`）**：
  - `download_with_retry` 不再直接写图库正式文件：HTTP 与浏览器两条通道都先写到
    `paths.temp_root()/downloads/` 的独立暂存文件；下载失败/重试只清理暂存，图库现图不动。
  - `_finalize_downloaded_page(aid, pn, staging, final_path, ...)` 先校验暂存可识别且能完整解码，
    再转换到图库内非图片后缀的候选临时文件；同图复用、普通下载冲突保留现图、
    强制重下才把旧图备份 `.bak` 后 `os.replace` 切换；备份/替换/登记失败都保留旧图与旧记录。
  - 原生 PNG 分支先 `Image.open().load()` 完整解码，损坏输入按 `unreadable` 失败，不再误判成功。
  - 下载失败分支只有确认旧图不可用时才标 `FAILED`，旧页状态/路径/指纹/来源记录保持不变。
  - 回归：新增 T170（保留真实 `download_with_retry`，只替换网络响应：HTML/异常/中断不碰旧图、
    同格式强制重下备份替换且只留一份、两通道静态断言）；T169 适配暂存签名。全量 **161/161**。
  - 部署：候选包 `built_at 07:05:21` 启动并通过 `/api/version` / `/api/health`，7813 导入链路
    复验通过；安装版与 `dist\vixiq` 已替换 `vixiq.exe`（前端资源未变，哈希一致），备份
    `.backup-force-redownload-20261121-070652`；安装版已启动（前端 `20261121h`），
    真实库 1603 作品 / 2191 图。GitHub 本地提交，**未推送**。
- **提交阶段返修：正式文件与登记一致（2026-11-21，后端，前端仍 `20261121h`）**：
  1. 去掉「`os.replace` 失败后 `copy2` 直接覆写正式路径」的回退：候选完整文件已在目标目录，
     只走同盘原子替换；替换失败直接恢复/保留旧图并报 `replace_failed`。
  2. 强制重下同路径时先把旧图原子移到 `.vixiq-old-*.tmp`（同时保留可读 `.bak`），
     新候选原子顶替后再登记；登记失败立即把旧图移回。新旧路径不同或原本无图时，
     事务失败只清理本次写入的候选，保留原登记与原图。
  3. 新增 `PixivDatabase.store_page_result`：页路径/大小/指纹/状态与转换来源在**一个事务**内提交，
     `local_path` 或 `source_format` 触发器失败都会整体回滚并抛给调用方。
  4. 复用现图时如果页登记缺失/状态不是 DONE/路径不一致，先用 `store_page_result` 补登记
     （不写来源，历史记录保持为空），补登记失败则不算成功。
  回归：新增 T171（注入替换失败、`local_path` 触发器、`source_format` 触发器、重试恢复、
  复用补登记、禁止覆写回退静态断言）；T169/T170 保留。全量 **162/162**。
- **文件保护返修：原路径与既有目标（2026-11-21，后端，前端仍 `20261121h`）**：
  1. 强制重下不再先把旧图移到临时位置（避免目录持续拒绝替换时正式路径消失）：`.bak` 用复制准备，
     候选完整校验后只做一次原子替换；失败时当前图仍留在原路径。替换成功但登记失败时，
     同路径从 `.bak` 复制到恢复候选再原子替换；恢复也失败则保留 `.bak` 并在日志记录恢复路径。
  2. 区分「数据库当前文件 / 计划写入目标 / 本次临时文件」：目标已存在且不是当前登记文件时，
     内容一致才复用并登记，不同或无法确认按 `target_conflict` 冲突并保留两份原文件；
     切换前再次确认目标没有被其他操作创建。登记失败只删除可确认由本次任务创建的目标文件。
  回归：T171 增补持续拒绝、登记+恢复双失败、JPG+PNG 并存冲突、目标不存在正常转换、
  目标不存在+登记失败清理等场景。全量 **162/162**。
  部署：候选包 `built_at 19:26:57` 启动并通过 `/api/version` / `/api/health`；安装版与
  `dist\vixiq` 已替换 `vixiq.exe`（前端资源未变，哈希一致，配置哈希未变），备份
  `.backup-file-preserve-20261121-192804`；安装版已启动（前端 `20261121h`），
  真实库 1603 作品 / 2191 图。GitHub 本地提交，**未推送**。
  部署：候选包 `built_at 13:47:55` 启动并通过 `/api/version` / `/api/health`；安装版与
  `dist\vixiq` 已替换 `vixiq.exe`（前端资源未变，哈希一致，配置哈希未变），备份
  `.backup-commit-atomic-20261121-134921`；安装版已启动（前端 `20261121h`），
  真实库 1603 作品 / 2191 图。GitHub 本地提交，**未推送**。

### 4.41 4B-2 按需转为 JPG 与空间预览（2026-11-22，版本 `20261121i`）

- **页级来源**：大图查看器信息行新增 `.lb-format` 与「🗜️ 转为 JPG」按钮。
  有记录显示「由 JPG 转为 PNG · 转换时间 · 旧大小 → 新大小」；无记录显示「来源未记录」；
  当前已是 JPG 时按钮禁用并显示「当前已是 JPG」。
- **质量预览**：`POST /api/library/convert-to-jpg/preview` 用当前 PNG 按质量 60–100 重新编码到临时文件，
  返回当前/JPG 大小、尺寸、节省字节与百分比、是否含透明像素、目标路径、图库登记占用与磁盘说明。
  透明像素固定合成**白色背景**，预览明确提示，用户确认后才切换。
  已是 JPG 或目标 JPG 已存在返回 409（`already_jpg` / `target_conflict`），不覆盖现有文件。
- **确认切换**：`POST /api/library/convert-to-jpg` 必须带 `confirm:true`；预检 → 编码 → 校验新 JPG
  （可打开、尺寸一致）→ 同盘原子发布 → 单事务登记页路径/大小/指纹/来源/`format_choice='jpg'` →
  旧 PNG 送 Windows 回收站。失败按 `unconfirmed/precheck/encode_failed/replace_failed/register_failed`
  报告并保留可用文件；回收失败写 `pending_recycle`，`POST .../retry-recycle` 可重试。
- **同步豁免**：`format_choice='jpg'` 且 `pending_recycle` 未清时，旧 PNG 不再被对账报为同页冲突
  （`db.store._is_old_derived_png`）；后续同步沿用 JPG，不会把旧 PNG 重新收进来。
- **迁移 `030_page_format_choice.sql`**：`pages` 新增 `format_choice`、`pending_recycle`。
- **验收**：T172 覆盖质量预览、确认切换只留一份、重复操作、透明图、目标冲突、回收失败 + 同步豁免 +
  重试、登记失败、编码失败、未确认与静态接线；全量 **163/163**，`py_compile`、`node --check` 通过。
  源码 7811 浏览器实测：来源标记、85→60 质量切换、白色背景提示、确认后 toast 与查看器关闭、
  已 JPG 禁用、来源未记录、目标冲突、回收失败重试均通过。
  候选包 7813 真实接口实测：上传 JPG 自动转 PNG → 预览 → 确认转回 JPG 只留一份；
  透明 PNG 输出透明角变白（255,255,255）；目标冲突 409 保留当前 PNG；真实 UI 来源标记与预览通过。
- **部署（2026-11-22 00:00）**：候选包 `_dev_runtime/packages/vixiq`（`built_at 2026-11-21 23:49:10`）
  在 7813 + `_dev_runtime/candidate-data` 验收通过。安装版 `D:\Program Files\Vixiq` 与 `dist\vixiq`
  已替换 `vixiq.exe`、7 个前端资源与 `_internal/db/migrations/030_page_format_choice.sql`，
  哈希一致，`config/user.json` 未变；旧文件备份
  `.backup-page-format-choice-20261121-235952` / `.backup-page-format-choice-20261122-000010`。
  真实库升级前备份 `F:\Vixiq\data\backups\page-format-choice-20261121-235929.db`；
  安装版已启动（前端 `20261121i`，`built_at 23:49:10`），`schema_migrations` 含 030，
  `pages.format_choice/pending_recycle` 就绪，真实库 1538 作品 / 2107 张已下载页（总 2192 页）。
  GitHub 本地提交（见汇报），**未推送**。

### 4.42 4B-2 返修：确认绑定预览与换图安全（2026-11-22，版本 `20261121j`）

- **确认绑定预览**：预览接口签发一次性 `preview_id`，绑定页、质量、源路径、文件 MD5/大小/mtime
  与页登记；源文件被改写或用其他质量确认返回 `stale_preview`/`source_changed`/`registration_changed`，
  前端立即失效并重新预览。前端用 `seq`/`reqSeq` 丢弃倒序应答；改质量立即禁用确认；
  关窗、换页、重开都会清掉定时器与在途请求。
- **发布防覆盖**：目标 JPG 用 `os.rename` 做无覆盖发布；预检后出现的同名 JPG（含两个同页确认）
  不会被覆盖，本次候选临时文件会清理；登记失败只删除仍能确认是本次写入的目标，
  目标已被其他文件替换时保留现场并报告 `target_kept`。
- **回收身份**：迁移 `031_page_pending_recycle_identity.sql` 增加 `pending_recycle_md5`、
  `pending_recycle_size`；登记 JPG 时同事务写入。首次回收、重试和同步豁免都按路径 + 内容 + 页状态核对。
  旧 PNG 原路径被换图时报告 `content_changed` 并保留新文件；当前 JPG 缺失时同步列
  `current_missing`，页路径继续沿用 JPG；当前文件与待回收路径相同则拒绝并清除危险记录。
- **仅静态单帧 PNG**：前端按 `/api/library/page-conversion` 的
  `static_png/actual_format/frames` 显示入口；后端以 PIL 实际解码为准，GIF、APNG、
  多帧 WEBP、扩展名伪装都保留原文件并给出明确提示。JPG 页详情新增独立的
  「♻ 重试回收旧 PNG」按钮，不再只能从转换弹窗重试。
- **验收**：新增 T173（源文件改写、质量/令牌失效、发布竞态、双确认、登记后目标替换、
  首次/重试回收身份、当前 JPG 缺失、当前路径=待回收、GIF/APNG/动画 WEBP/伪装、前端接线）；
  T172 适配预览令牌。全量 **164/164**，`py_compile`、`node --check` 通过；
  源码 7811 浏览器实测即时确认、倒序应答、关窗重开、JPG 页重试入口、不支持格式入口均通过。
- **部署（2026-11-22 14:46）**：候选包 `_dev_runtime/packages/vixiq`
  （`built_at 2026-11-22 14:40:06`）在 7813 验收（真实接口 + 两套 UI 脚本）；
  安装版 `D:\Program Files\Vixiq` 与 `dist\vixiq` 已替换 `vixiq.exe`、7 个前端资源与
  `_internal/db/migrations/031_page_pending_recycle_identity.sql`，哈希一致，`config/user.json` 未变；
  备份 `.backup-page-recycle-identity-20261122-144513` /
  `.backup-page-recycle-identity-20261122-144549`。
  真实库升级前备份 `F:\Vixiq\data\backups\page-recycle-identity-20261122-144422.db`；
  安装版已启动（前端 `20261121j`，`built_at 14:40:06`），`schema_migrations` 含 031，
  真实库 1538 作品 / 2107 张已下载页（总 2192 页）。GitHub 本地提交（见汇报），**未推送**。

### 4.43 4B-3 同步发现待转 PNG（2026-11-23，版本 `20261123a`）

- **同步只报告**：`reconcile_output` 新增 `normalize_pending`。手动放入图库的静态
  JPG/JPEG/WEBP/BMP/TIF（以 PIL 实际解码格式为准，动图 GIF/多帧 WEBP/APNG 跳过）
  在同步结果与健康条显示「待转为 PNG」，自动扫描/周期轮询都不会自动转换。
  已主动选用 JPG 的页（`format_choice='jpg'`）继续沿用 JPG，不进入待转列表。
- **同页冲突**：已有 PNG 的页再出现同名 JPG 时，同步结果列出「图库当前使用」和
  「磁盘另有一份」；目标 PNG 已存在也列为冲突。预览只报告，不签发确认令牌，
  不覆盖任何现有文件。
- **预览与确认**：`POST /api/library/normalize-pending/preview` 对可转项实际生成临时 PNG，
  返回计划、同页冲突、逐项与总大小变化，并签发一次性 `preview_id`（绑定路径/MD5/大小/mtime）。
  `POST /api/library/normalize-pending/apply` 必须带回 `preview_id`，重新核对源文件、
  目标路径和页状态后转换；`recycle_old` 决定是否把旧 JPG 送系统回收站。
- **失败保护**：源文件改写、目标 PNG 竞态出现、同页登记变化、转换失败/文件占用、
  外部盘不可用都保留原 JPG 并报告阶段；登记失败时只撤回本次 PNG（身份未变时），
  旧 JPG 与旧登记保持可用，可重新预览再试（4B-3 返修 ③）。
  其他来源（无 pixiv 自然键）文件转换后登记为 PNG 当前页。
- **验收**：新增 T174（同步只报告、已选 JPG/动图不报、同页冲突、目标冲突、预览不落文件、
  确认转 PNG、旧文件回收/保留、源文件改写、目标竞态、转换失败、外部盘 503、前端接线）；
  全量 **165/165**，`py_compile`、`node --check` 通过。源码 7811 浏览器实测：
  健康条「待转为 PNG」、同步结果入口、预览计划/冲突/预计空间、确认后 toast 与同步刷新通过；
  候选包 7813 真实接口实测：手动放 JPG → 同步报告 → 预览 → 确认后只留 PNG。
- **部署（2026-11-23 01:44）**：候选包 `_dev_runtime/packages/vixiq`
  （`built_at 2026-11-23 01:40:00`）在 7813 验收；安装版与 `dist\vixiq` 已替换
  `vixiq.exe` 与 7 个前端资源（无新迁移，031 已在），哈希一致、配置未变；
  备份 `.backup-normalize-pending-20261123-014347` /
  `.backup-normalize-pending-20261123-014417`；真实库部署前备份
  `F:\Vixiq\data\backups\normalize-pending-20261123-014315.db`。
  安装版已启动（前端 `20261123a`，`built_at 01:40:00`），真实库 1538 作品 /
  2107 张已下载页（总 2192 页）。真实库现有 1 张手动 JPG
  `蒂卡Tika_local_38e4770d1891/#bilibili#/其他来源_001.jpg`，下次同步会显示「待转为 PNG」，
  不会自动转换。GitHub 本地提交（见汇报），**未推送**。

**4B-3 返修（2026-11-23，前端 `20261123b`，已发布到安装版；GitHub 未推送）**

- **发布防覆盖**：`normalize_to_png` 改为「同盘唯一临时文件 + 完整校验 + 发布拒绝覆盖」。
  目标在预检后出现、或同一项被并发确认时，后到任务报冲突并清理本次临时文件，
  保留目标与原 JPG，登记保持一致。
- **身份绑定**：预览令牌额外绑定页归属、页当前文件指纹与格式选择；预览前后各核对
  一次源文件身份，编码完成、发布前再复核一次。页归属/现图/格式选择变化一律作废，
  不发布、不改登记、不回收。
- **登记失败可重试**：已发布 PNG 在登记失败时只撤回能证明属于本次任务且身份未变的输出；
  本次新建的「其他来源」占位页一并撤回（`PixivDatabase.discard_foreign_shell`），
  旧 JPG 与旧登记保持可用，故障消除后重新预览即可完成。
- **回收失败提示**：转换结果窗口在部分失败、回收失败、主动保留旧文件时都保持打开，
  逐项列出路径与原因（「PNG 已入库，旧 JPG 仍保留」/「旧文件仍占用磁盘」）；
  只有全部成功且无遗留才关闭窗口显示成功 toast。
- **验收**：新增 T175；全量 **166/166**。
- **部署（2026-11-23 03:58）**：候选包 `_dev_runtime/packages/vixiq`（`built_at 03:56:21`，
  前端 `20261123b`）先在 7813 隔离数据跑通 4B-3 端到端；回滚清单
  `_dev_runtime/backups/publish-20261123b-20261123-035826.json`（4 个旧文件按 SHA-256
  去重，新增备份 14,926,543 字节）。`dist/vixiq` 与 `D:\Program Files\Vixiq` 的 exe /
  app.js / index.html / base_library.zip 哈希一致，两处 `config/user.json` 哈希未变；
  安装版 7810 健康、`/api/version` 前端 `20261123b`、`built_at 03:56:21`。GitHub 未推送。

### 4.87 清理缺失作品最终复核与批次互斥（2026-11-28，前端 `20261128b`，已发布到安装版）

- **最终复核**：`_miss_run` 新增 `verifying` 阶段，在 `delete_artworks` 前再对账一次；
  文件补回、记录消失、登记路径变化、图库不可访问/状态无法核实都按跳过结算并保留
  记录；写库前最后一刻再查活动页路径是否出现文件。
- **回收身份**：确认时记录残留文件路径 + 大小 + mtime + MD5；回收前再次核验，复核后
  出现、同名替换/修改、已不存在都进 `recycle_skipped`，只有身份未变的才交给
  `_recycle_files`。已恢复作品不计已清理；结果/前端分别显示已回收、失败、跳过。
- **执行窗口**：新增 `_MISS_EXEC_LOCK`，任务与旧同步清理接口共用；同批运行中返回
  `already` 接回，异批返回 409 `busy` 和当前任务快照且无副作用；旧任务号/进度/终态
  可查，完成后新一批可提交。前端忙时接回运行中的旧任务并提示本批未开始。
- **回归**：T201 新增最终复核补回、写库前补回、回收前同名替换、异批忙不抢占；
  Node 增至 **13 项**。全量 **192/192**（146.24 s），日志
  `_dev_runtime/diagnostics/trae-20261128b-missing-cleanup-rework-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-28 01:48:00`，前端 `20261128b`）在 7813
  隔离图库通过清理缺失 API/UI 检查，恢复配置后通过既有 **12 项**检查（日志
  `trae-20261128b-candidate-checks.log`）。回滚清单
  `_dev_runtime/backups/publish-20261128b-20261128-015114.json`（4 个旧文件、
  8 个替换项、15,251,211 字节）。dist 与安装版 exe `ba9d1106eb27…`、base_library
  `4a58a68b6740…`、app.js `1e767de63e5d…`、index.html `cb357535a465…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261128b-1795801680-1795695345-1795519082`、健康正常，清理缺失/以图搜图/
  副本扫描/壁纸库/回收站 UI 检查通过。

### 4.86 清理缺失作品接入共用进度（2026-11-28，前端 `20261128a`，已发布到安装版）

- **后端任务**：新增 `POST/GET /api/library/clean-missing/task`，`_MISS_TASK`
  单条状态；同批运行中重复提交接回，新批次替换旧任务。阶段 checking → cleaning
  → recycling；删记录仍一次批次事务，随后逐作品核对，清理/跳过/失败都带原因，
  每件结算后才推进 N。确认后新发现的缺失不处理；图库不可用直接 error；
  回收文件与记录清理独立计数，回收失败明确返回；旧同步接口保留。
- **前端**：预览即时显示「正在核对缺失作品」共用流动条（约 300 ms、减少动效），
  区分空清单/图库不可用/扫描不完整/请求失败且只读。确认后固定 ids/模式/数量，
  取消不建任务；执行区显示复核流动、批次/回收等待与真实计数；结果列清理、跳过、
  失败原因和回收/回收失败数。任务号存 `vixiq.miss.task`，关闭重开/刷新接回；
  串行查询、5 s 超时、短暂失败重试、迟到回包保护，其他弹窗打开即停止轮询。
- **回归**：新增 T201（总数 **192**）与 `test_missing_cleanup_progress.cjs`
  **12 项**。全量 **192/192**（191.80 s），日志
  `_dev_runtime/diagnostics/trae-20261128a-missing-cleanup-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-28 00:19:47`，前端 `20261128a`）在 7813
  隔离图库通过清理缺失 API/UI 检查，恢复配置后通过既有 **12 项**检查（日志
  `trae-20261128a-candidate-checks.log`）。回滚清单
  `_dev_runtime/backups/publish-20261128a-20261128-002302.json`（4 个旧文件、
  8 个替换项、15,232,239 字节）。dist 与安装版 exe `3de29c001322…`、base_library
  `64641d8da4f3…`、app.js `7dd2547e14e9…`、index.html `e7c8e2280fd1…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261128a-1795796387-1795695345-1795519082`、健康正常，清理缺失/以图搜图/
  副本扫描/壁纸库/回收站 UI 检查通过。

### 4.85 以图搜图文件身份核验与旧缓存迁移（2026-11-27，前端 `20261127q`，已发布到安装版）

- **内容指纹**：特征 npz 增加 `src_md5`（提取时源文件 MD5）；服务器用
  `_ls_md5_map()` 一次读出 `pages.local_path → md5` 作为可信业务指纹，任务、同步
  接口和壁纸溯源共用。判定顺序：无 `src_md5` 的旧缓存一次性补建；大小/mtime 变了
  → 过期；两项没变 → 先比登记 MD5，个别无登记的文件才现场算一次当前文件 MD5。
  正常图库无变化时不做整库原图哈希。
- **旧缓存迁移**：历史 npz 不再按源图与 npz 新旧时间升级，一律重提一次，数量通过
  `extract_plan` 进入任务分母与进度；过期特征先删后提，失败保留原因并下次重试，
  旧特征不参与检索。
- **回归**：T200 用正常入口覆盖同大小同时间 `os.replace` / `copy2` 内容替换
  （图库 MD5 已更新、TOP1 正确、盘上身份一致）、历史格式冷启动（旧时间戳也不沿用）
  与直接冷启动；保留新入库、普通换图、无变化直搜、失败重试、另一入口持锁等用例。
  全量 **191/191**（190.27 s），日志
  `_dev_runtime/diagnostics/trae-20261127q-library-similar-identity-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 23:39:55`，前端 `20261127q`）在 7813
  隔离图库通过新增身份核验/补建 API 检查与以图搜图 UI 检查，恢复配置后通过既有
  **12 项**检查（日志 `trae-20261127q-candidate-checks.log`）。回滚清单
  `_dev_runtime/backups/publish-20261127q-20261127-234447.json`（3 个旧文件、
  6 个替换项、14,464,099 字节）。dist 与安装版 exe `d4ec82ea4dd3…`、base_library
  `7cbd32499508…`、app.js `0bdca1cc1d34…`、index.html `8e5e9723146a…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127q-1795789644-1795695345-1795519082`、健康正常，以图搜图/副本扫描/
  壁纸库/回收站 UI 检查通过。安装版首次真实搜图会一次性补建旧特征缓存。

### 4.84 图库以图搜图增量索引返修（2026-11-27，前端 `20261127p`，已发布到安装版）

- **单例增量**：`/api/library/similar/task` 在 `_ls_searcher` 已存在时先
  `searcher.refresh(_cb)` 再检索；旧同步接口同样先 refresh，并把 refresh+search
  放进 `_ls_lock`。新入库和同路径换图不重启、不清特征库即可进入本次结果。
- **文件身份**：特征 npz 记录 `sig_size` / `sig_mtime_ns`（读图前 stat）；身份不一致
  即视为过期。过期 npz 先删除再重提，重提失败时旧特征不参与检索且下次仍重试；
  旧格式无身份时按图片与 npz 修改时间安全升级（未变只补身份，变化则重提）。
  `build_db` 返回是否有变化，`refresh` 仅在变化时重载内存索引。
- **回归**：T200 用正常 HTTP 入口覆盖「先搜一次 → 入库新图 → 立即搜新图」：分母 1、
  TOP1 为新图、未变化文件不重复提取；并覆盖运行中同路径换图、冷启动旧磁盘缓存、
  旧格式升级/过期、无变化直搜、另一入口持锁、临时图回收与错误重试。全量 **191/191**
  （168.40 s），日志
  `_dev_runtime/diagnostics/trae-20261127p-library-similar-incremental-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 23:16:13`，前端 `20261127p`）在 7813
  隔离图库通过新增/同路径换图 API 检查与以图搜图 UI 检查，恢复配置后通过既有
  **12 项**检查（日志 `trae-20261127p-candidate-checks.log`）。回滚清单
  `_dev_runtime/backups/publish-20261127p-20261127-231925.json`（3 个旧文件、
  6 个替换项、14,462,001 字节）。dist 与安装版 exe `ab3578eb9fff…`、base_library
  `f4369f72da83…`、app.js `0bdca1cc1d34…`、index.html `f0944c194824…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127p-1795789644-1795695345-1795519082`、健康正常，以图搜图/副本扫描/
  壁纸库/回收站 UI 检查通过。

### 4.83 图库「以图搜图」接入共用进度（2026-11-27，前端 `20261127o`，已发布到安装版）

- **核心事件**：`core.local_search` 新增 `QueryImageError` 与 `progress_cb`；
  `extract_plan` / `extract(done,total,outcome,detail)` / `load_*` / `search_*`
  均来自实际建库与检索过程；每张处理完在 finally 推进 N，成功/跳过/失败分开，
  失败带样例。小图库候选池 ≥ 特征数时 `np.argpartition` 越界改用 `np.arange`。
- **后台任务**：新增 `POST/GET /api/library/similar/task`，`_SIM_TASK` 单条状态、
  `_ls_lock` 单例复用；GET 必须带当前任务号，旧号 404。建库失败不赋值并可重试；
  壁纸深扫持锁时任务显示检查等待并复用；任务线程 finally 删除临时查询图；旧同步
  `POST /api/library/similar` 保留并共用 `_sim_items`。只读检索不登记数据域。
- **前端**：入口无任务选图、有任务接回；共用流动/计数控件、约 300 ms 阈值、
  减少动效；检查 → 提取计数（M 只含本轮）→ 加载 → 检索 → 结果/无匹配/输入图
  不可读/索引失败/检索失败；结果卡片沿用 `safeThumb`。轮询串行单链（单次 5 s
  超时，短暂失败保留进度重试）；关闭后台继续、重开/刷新凭会话任务号接回；新选图
  递增代号，旧 POST/GET/结果不能覆盖。
- **回归**：新增 T200（总数 **191**）与 `test_sim_progress.cjs` **11 项**；
  全量 **191/191**（182.23 s），日志
  `_dev_runtime/diagnostics/trae-20261127o-library-similar-progress-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 22:27:24`，前端 `20261127o`）在 7813
  以隔离图库/特征缓存通过新增以图搜图 API/UI 检查与既有 12 项检查（日志
  `trae-20261127o-candidate-checks.log`）。回滚清单
  `_dev_runtime/backups/publish-20261127o-20261127-223158.json`（4 个旧文件、
  8 个替换项、15,212,625 字节）。dist 与安装版 exe `8bdc6923d855…`、base_library
  `8613dc47d508…`、app.js `0bdca1cc1d34…`、index.html `e2c5e8dea9fe…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127o-1795789644-1795695345-1795519082`、健康正常，以图搜图/副本扫描/
  壁纸库/回收站 UI 检查通过。

### 4.82 壁纸副本扫描返修：启动归属 + 线程失败可重试（2026-11-27，前端 `20261127n`，已发布到安装版）

- **启动归属**：`wallDupesScan` 捕获 `opModalSeq()` 与 `_wallDupesStartSeq` 批次号；
  打开/关闭/重开弹窗都会递增批次。POST 的成功、失败、finally 都校验弹窗实例与批次，
  旧回包不能改新弹窗进度/结果、不能停新轮询、不能弹旧通知、不能清新一轮受理标记；
  后端任务保留，重开由 GET 接回。
- **线程失败**：`Thread.start()` 包 try/except；失败时把 `_WALL_DUPES` 收束为 error，
  返回 500 + 原因，GET 同步看到失败；不再卡在 scanning，随后可正常发起新任务。
  实际运行中的重复提交仍返回 already。
- **回归**：Node 扩到 **16 项**（旧 POST 成功/失败、新一轮挂起时旧回包结束等）；
  T199 新增 `Thread.start` 抛错、GET 终态与重试成功，保留路径不可读/任务抛错用例。
  全量 **190/190**（148.07 s），日志
  `_dev_runtime/diagnostics/trae-20261127n-wall-dupes-rework-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 21:51:12`，前端 `20261127n`）在 7813
  通过全部 12 项检查；回滚清单
  `_dev_runtime/backups/publish-20261127n-20261127-215258.json`（4 个旧文件、8 个
  替换项、15,211,694 字节）。dist 与安装版 exe `7e89dd652f49…`、base_library
  `b6d089ed3614…`、app.js `154a44d738d8…`、index.html `34545897a19d…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127n-1795787472-1795695345-1795519082`、健康正常，副本扫描/壁纸库/回收站
  UI 检查通过。

### 4.81 壁纸副本扫描接入共用进度（2026-11-27，前端 `20261127m`，已发布到安装版）

- **后端阶段**：`_WALL_DUPES` 新增 `stage`（核对壁纸夹与图库索引 → 扫描壁纸）；
  `find_wall_dupes` 在候选和图库索引准备好后先回调 `(0,total,"")`，每张**处理完**
  再推进 done。候选文件读不到指纹时计入 `failed` 总数并记原因，`errors` 只留前 20 条；
  不再把「读不到」当成「已确认非副本」。POST 启动/`already` 响应都带任务快照。
- **前端**：弹窗顶部进度区（共用流动/计数条），结果区仍保留原勾选列表。打开/开始/
  重扫均有即时反馈；准备超过约 300 ms 显示「正在核对壁纸夹与图库索引」，候选数
  确定后切「已扫描 N／M 张」。结果按「已扫描 N 张 · 发现 K 张副本 · 可释放 S」
  展示，未完成数量与原因单列。轮询为串行单链（上一请求结束/超时后才发下一次），
  关闭弹窗或切换共享弹窗后旧回包不绘制；短暂失败保留进度并重试，重开接回。
- **回归**：T131 更新进度回调顺序与 failed/errors，并覆盖坏文件；新增 **T199** 覆盖
  两阶段、重复 POST、空目录、扫描失败、启动失败、只读；新增
  `test_wall_dupes_progress.cjs` **13 项**（串行无并发、迟到回包、关闭/切换归属、
  短暂失败、重开、结果/空目录/失败）。全量 **190/190**（135.45 s），日志
  `_dev_runtime/diagnostics/trae-20261127m-wall-dupes-progress-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 20:22:52`，前端 `20261127m`）在 7813
  通过原有 10 项、壁纸库 2 项与新增副本扫描 API/UI 检查（含空目录、坏文件、420px
  窄窗与 reduced-motion）；回滚清单
  `_dev_runtime/backups/publish-20261127m-20261127-202451.json`（4 个旧文件、8 个
  替换项、15,204,494 字节）。dist 与安装版 exe `26730bff0d4e…`、base_library
  `6c8d9947138b…`、app.js `c91b54423900…`、index.html `3695c1851efd…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127m-1795782172-1795695345-1795519082`、健康正常，副本扫描/壁纸库/回收站
  UI 检查通过。

### 4.80 壁纸库生成进度返修：迟到回包 + 完整失败计数（2026-11-27，前端 `20261127l`，已发布到安装版）

- **迟到回包**：轮询改为单条串行链（当前请求结束才安排下一次）；停止、终态、新任务
  都会递增 `_wallLibGen` 并清定时器，旧请求回包按代号丢弃，不能再改按钮/进度/结果/
  在途标记或重启轮询。`loadWallLib` 在已有链或启动请求时复用，启动结束后用
  `loadWallLib(true)` 读最终状态；短暂查询失败保留最后进度继续。
- **失败计数**：`core.wallpaper_lib` 新增 `failed` 总数，每次实际复制/读落盘信息失败
  都计数；`errors` 仍只留前 20 条原因。前端用 `failed` 显示「失败 N 项」，明细原因
  取第一条，兼容无该字段时回退到明细条数。
- **回归**：Node 扩到 **13 项**（串行无并发、终态后旧 running、新任务后旧终态、
  重进单链、失败继续、完整失败数）；T133 新增 23 个失败（`failed=23`、`errors=20`）
  与部分失败（成功数=实际登记数、检查数含失败）。全量 **189/189**（166.59 s），日志
  `_dev_runtime/diagnostics/trae-20261127l-wall-lib-rework-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 19:30:23`，前端 `20261127l`）在 7813
  通过原有 10 项检查，壁纸库 API 新增只读目标复制失败场景（扫描 5 → 新增/已有/
  比例/缺失/失败各 1），UI 校验「失败 23 项」；回滚清单
  `_dev_runtime/backups/publish-20261127l-20261127-193231.json`（4 个旧文件、8 个
  替换项、15,203,000 字节）。dist 与安装版 exe `d64f78e43afb…`、base_library
  `9f951926fb6d…`、app.js `25c4130b2732…`、index.html `72829713afc7…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127l-1795779023-1795695345-1795519082`、健康正常，壁纸库与回收站 UI 检查通过。

### 4.79 壁纸库「从图库生成壁纸」接入共用进度（2026-11-27，前端 `20261127k`，已发布到安装版）

- **范围**：只接设置页「桌面与壁纸 → 壁纸库」的「从图库生成壁纸」；壁纸副本扫描等
  入口不变。
- **后端**：`generate` 在候选数确定时先回调 `(0, total, "")`，每张图片**处理完**
  （含跳过/缺失/复制失败）再推进 done；`_WALL_LIB` 新增 `stage`，依次为
  「读取图库候选 → 检查图片 → 登记壁纸」，完成/错误清空。`POST /api/wallpaper/lib/scan`
  的启动响应与 `already` 响应都带任务快照，运行中重复提交只接回原任务。
- **前端**：按钮下方新增任务区，复用共用流动/计数条；点击立即禁用并显示「正在生成…」，
  约 300 ms 后仍准备则显示「正在读取图库候选」；有总数后切「已检查 N／M 张」，100%
  后仍登记显示「正在登记壁纸」；结果按已检查/已生成/已存在/筛掉/缺失/失败展示。
  快速完成不闪进度条；轮询只有一套，短暂 GET 失败保留最后进度继续；切页返回或刷新
  进入设置页从 GET 接回任务。
- **回归**：新增 `test_wall_lib_progress.cjs` 10 项；T133 扩展回调顺序、三阶段延迟、
  `already`、空候选、复制失败（独立 DB 打桩）。全量 **189/189**（155.70 s），日志
  `_dev_runtime/diagnostics/trae-20261127k-wall-lib-progress-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 18:45:04`，前端 `20261127k`）在 7813
  通过原有 8 项检查、上一轮回收站文件核验，以及新增壁纸库 API/UI 检查（含 420px
  窄窗与 reduced-motion）；回滚清单
  `_dev_runtime/backups/publish-20261127k-20261127-185229.json`（4 个旧文件、8 个
  替换项、15,197,820 字节）。dist 与安装版 exe `f6a18b725362…`、base_library
  `abf807d74924…`、app.js `56100f032a4e…`、index.html `06bba3304d9b…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127k-1795776304-1795695345-1795519082`、健康正常，壁纸库与回收站 UI 检查通过。

### 4.78 回收站「全部恢复」文件核验返修：缺失不登记 + 实内容比对（2026-11-27，前端 `20261127j`，已发布到安装版）

- **已下载文件可用才登记**：造文件清单时逐页核对。回收站文件缺失时，图库原位也没有
  → 报「恢复文件缺失」，保留回收标记与 `trash_rel_path`，找回文件后可重试；原位有
  文件但与登记 `pages.md5` 不一致/无法核验 → 报位置或内容冲突，保留回收记录和原位
  文件；原位与登记指纹一致 → 允许登记（兼容此前回退未成功但文件已落位）。从未登记
  本地文件的页仍只恢复记录。
- **两份当前文件内容相同才清理回收副本**：目标已存在时不再凭数据库旧哈希认定相同，
  改为比对两份实际 MD5；不同内容报冲突并保留两份与回收状态。登记成功后清理回收副本
  前再复核一次，登记期间文件有变化则保留副本并写日志。
- **回归**：T198 新增两处都缺失、原位不同内容、原位与登记指纹一致、登记指纹过时但
  两份当前文件不同、正常同内容复用、多页一页缺失、从未登记本地文件的记录、登记期间
  副本变化不误删；均检查 API 结果、回收列表、DB 字段与源/目标实际字节，并对冲突和
  找回缺失文件各验一次重试。`test_trash_restore_progress.cjs` 扩到 15 项。全量
  **189/189**（161.99 s），日志
  `_dev_runtime/diagnostics/trae-20261127j-trash-restore-file-check-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 17:35:05`，前端 `20261127j`）在 7813
  通过三个接口检查、四个 UI 检查，以及新增
  `check_trash_restore_file_check_candidate.py` 七类文件核验；回滚清单
  `_dev_runtime/backups/publish-20261127j-20261127-173829.json`（4 个旧文件、8 个
  替换项、15,196,116 字节）。dist 与安装版 exe `9a6efe1619d2…`、base_library
  `92e5867bc205…`、app.js `4dafe44f2f73…`、index.html `756fbb2fbe19…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127j-1795772105-1795695345-1795519082`、健康正常，回收站恢复 UI 检查通过。

### 4.77 回收站「全部恢复」返修：失败保留重试 + 确认批次 + 文件进度（2026-11-27，前端 `20261127i`，已发布到安装版）

- **失败保留在回收站**：改为「先搬文件、后登记」逐个候选结算。只有本次文件全部安全
  落位且 DB 登记成功才计入已恢复；权限/占用/登记失败/中途目标冲突会回退已搬文件并
  保留回收标记与回收路径，失败项仍能在回收站查看和重试。同内容目标视为已就位；
  不同内容绝不覆盖。多页作品整体结算，任一页失败会把已搬页回退。回退失败在结果中
  列出实际文件位置并写日志。
- **只恢复确认批次**：点「全部恢复」时固定点击时视图并读取清单，确认文案带单位
  （N 个作品 / N 张图片）；请求携带固定 `ids`/`items`，服务端不再重取全量。确认后
  新入回收站的条目不处理；已被别处处理的候选项按实际原因跳过；同一批运行中重复
  提交返回已有任务，不重复搬文件。
- **执行进度按文件**：先流动「核对恢复位置」，再按本次文件清单推进「已处理 N／M 张」；
  失败也计入已处理。确认与结果仍按作品/图片显示，并新增「登记失败」类别。
- **回归**：T198 扩展作品/图片两视图失败项仍在回收站且可重试、多页部分失败回退、
  登记失败回退、中途目标冲突不覆盖、确认批次外新增不处理、已被别处处理、同批重复
  提交；`test_trash_restore_progress.cjs` 扩到 13 项（含确认批次、取消、切页、登记/
  回退失败文案）。全量 **189/189**（157.54 s），日志
  `_dev_runtime/diagnostics/trae-20261127i-trash-restore-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 16:47:18`，前端 `20261127i`）在 7813
  通过三个接口检查与四个 UI 检查；回滚清单
  `_dev_runtime/backups/publish-20261127i-20261127-164911.json`（4 个旧文件、8 个
  替换项、15,188,388 字节）。dist 与安装版 exe `724bf28ac4b2…`、base_library
  `de019d98e95d…`、app.js `ec8c24973a24…`、index.html `83ba174a2c1d…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127i-1795769238-1795695345-1795519082`、健康正常。

### 4.76 图库回收站「全部恢复」共用进度（2026-11-27，前端 `20261127h`，已发布到安装版）

- **范围**：只接图库回收站的「全部恢复」，作品视图与单张图片视图各一套；壁纸回收站与
  单个行内恢复保持原样。
- **后端**：新增 `GET/POST /api/library/trash/restore/task`。启动时用
  `list_trashed(kind)` 锁定候选，后台逐个恢复，作品视图按作品数、图片视图按图片数计数。
  每个候选按「恢复成功 / 冲突跳过 / 文件搬回失败」分别收口；搬文件失败不并进成功数。
  任务完成登记 artworks/pages/facets/stats/search/ops，并写操作日志。
- **前端**：回收站页新增共用任务区（流动锁定候选 → 计数 → 结果）。关闭回收站页再回来
  可接回运行中任务；运行期间「全部恢复」按钮禁用。结果明确列出恢复成功数、冲突跳过
  数及原因、文件搬回失败数及原因。
- **回归**：新增 T198：作品视图与图片视图各造 3 个候选（正常 / 原路径不同内容冲突 /
  `os.replace` 失败），核对计数、三类结果、文件去向与部件文案；新增
  `test_trash_restore_progress.cjs`（真实前端函数 9 项）。全量 **189/189**（159.27 s），
  日志 `_dev_runtime/diagnostics/trae-20261127h-trash-restore-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 15:50:54`，前端 `20261127h`）在 7813
  通过三个接口检查与四个 UI 检查；回滚清单
  `_dev_runtime/backups/publish-20261127h-20261127-155242.json`（4 个旧文件、8 个
  替换项、15,178,305 字节）。dist 与安装版 exe `0aeb8b1eae0d…`、base_library
  `f8d3271843e5…`、app.js `a19093cc209c…`、index.html `1707ff199ec7…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127h-1795765854-1795695345-1795519082`、健康正常。

### 4.75 缩略图缓存复验返修：壁纸离线保护 + 预热启动失败解锁 + 结果文案（2026-11-27，前端 `20261127g`，已发布到安装版）

- **壁纸目录离线保护**：`_thumb_roots` 先读 `paths.wallpaper_dir` 配置原值；配置了但目录
  不存在/不可读时明确把 wall 标为不完整，不再经过 `candidates_dir` 的「读不到配置就
  回退当前壁纸目录」路径。按保守口径放弃本次失效缓存清理并提示，目录重新上线后仍能
  保留有效缓存、清掉真正的失效项。配置读取失败本身也视为 wall 不可用。
- **预热启动失败解锁**：`warm_thumbs_background` 把线程创建/启动包进 try；失败时清掉
  `running`、`abort`、线程引用并把任务收束为 `error`，`/api/thumbs/task` 与旧的
  `/api/thumbs/warm` 返回 500。下一次预热可正常启动。
- **清理结果文案**：运行中计数统一为「已处理 n／N 张（个）」；只有 `total=0` 才说
  「没有失效缩略图」。有候选时结束反馈按「已处理 N：清理成功 X，未清理 Y（原因）」
  展示，清空缓存同口径；前端结果文案优先于服务端 message，不再掩盖失败原因。
  失败任务/完成状态都会解锁三个按钮。
- **回归**：T197 扩展真实配置壁纸目录离线/上线、unlink 失败与任务 message、预热线程
  启动失败后再次成功；`test_thumbs_task_ui.cjs` 扩到 9 项（失败解锁、全部/部分失败
  文案）。全量 **188/188**（148.27 s），日志
  `_dev_runtime/diagnostics/trae-20261127g-thumbs-edge-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 15:28:24`，前端 `20261127g`）在 7813
  通过三个接口检查与三个 UI 检查；回滚清单
  `_dev_runtime/backups/publish-20261127g-20261127-153039.json`（4 个旧文件、8 个
  替换项、15,176,871 字节）。dist 与安装版 exe `78d96fc4f3b8…`、base_library
  `3b21425b61e9…`、app.js `cb70c1c92668…`、index.html `77108208400f…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127g-1795764504-1795695345-1795519082`、健康正常。

### 4.74 缩略图缓存：失效范围修正 + 三操作共用进度（2026-11-27，前端 `20261127f`，已发布到安装版）

- **文案统一**：设置页「清理孤儿」改为「清理失效缩略图」，悬停说明改为「清理原图移动、
  改名或删除后留下的缩略图缓存」；统计用「失效缩略图 N 张」，确认与结果文案同步，
  失败项单列「N 张未清理」并带原因。「清除缓存」改为「清空缩略图缓存」。
- **有效缓存范围**：`_valid_thumb_names` 现在覆盖 `/api/local-img` 实际使用的
  img/input/trash/wall 四个根和全部可生成格式（含 bmp/tif/tiff），并为数据库登记过的
  图库/回收站路径保留 stable 键（源文件缺失或进回收站后仍可看历史预览）。扫描不完整
  或已启用根暂时不可用时放弃清理并返回 `skipped_roots`，不再误删有效缓存。
- **共用任务**：新增 `GET/POST /api/thumbs/task`，三个操作共用一条可重开任务状态
  （`kind/state/stage/done/total/result/message`）。清理先流动「核对缩略图」，锁定范围
  后按张数计数；清空缓存可抢占正在运行的预热，先显示「停止预热」再按文件数清理；
  预热先确定图片数，再报已检查/已生成/已存在/失败。设置卡片下方统一显示进度与结果，
  关闭设置再回来可接回，按钮在任务期间禁用。
- **回归**：新增 T197（四根/全格式/缺失 stable/不可用根/三操作任务）+
  `autotest/test_thumbs_task_ui.cjs`（真实前端函数：流动/计数/结果/接回/启动）。
  全量 **188/188**（121.17 s），日志
  `_dev_runtime/diagnostics/trae-20261127f-thumbs-cache-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 13:58:59`，前端 `20261127f`）在 7813
  通过三个接口检查、批量转 PNG 失败 UI、转 JPG 进度 UI 与
  `check_thumbs_task_ui.py`；回滚清单
  `_dev_runtime/backups/publish-20261127f-20261127-140030.json`（4 个旧文件、8 个
  替换项、15,163,701 字节）。dist 与安装版 exe `0e1e0ccb61b0…`、base_library
  `9172df217adc…`、app.js `84f5bc85a546…`、index.html `cdbc0e77cfdc…`；两处
  `config/user.json` 哈希未变。安装版 7810 前端
  `20261127f-1795759139-1795695345-1795519082`、健康正常。

### 4.73 单张 PNG 转 JPG 接入共用进度（2026-11-27，前端 `20261127e`，已发布到安装版）

- **质量预览**：质量滑块变化或首次打开时显示流动条「正在生成质量预览」，请求在途
  只显示流动条；当前质量的结果回来后收起流动条并展示大小预估。旧质量/旧请求的迟到
  应答继续按 `seq/reqSeq/quality` 丢弃，不覆盖新结果。
- **确认转换**：使用共用分段充能条，阶段为「编码 → 校验并更新登记」；提交后第一段
  进行中，成功后两段完成并关闭弹窗，失败时当前段标红、弹窗保留并提示原因。
- 沿用原有质量令牌、源文件身份、页登记、发布防覆盖、旧 PNG 回收与重试规则，不改
  后端接口。
- **回归**：新增 T196 + `autotest/test_jpg_progress_ui.cjs`（真实 `app.js` 函数）覆盖
  预览流动、成功收尾、失败标红；全量 **187/187**（123.01 s），日志
  `_dev_runtime/diagnostics/trae-20261127e-jpg-progress-smoke.log`。
- **候选/发布（仅前端）**：候选包 `_dev_runtime/packages/vixiq` 的 app.js/index.html
  更新为 `20261127e`；7813 通过三个接口检查、`check_normalize_failure_ui.py` 与
  `check_jpg_progress_ui.py`。回滚清单
  `_dev_runtime/backups/publish-20261127e-20261127-134142.json`（2 个旧文件哈希、
  4 个替换项、797,726 字节）。dist 与安装版 app.js `ba4d9380f54c…`、index.html
  `dcebef902cfd…`，exe/base_library 仍为 `20261127d` 构建；两处 `config/user.json`
  哈希未变。安装版 7810 前端 `20261127e-1795757621-1795695345-1795519082`、健康正常。

### 4.72 批量转 PNG 任务复用、弹窗归属与失败重开（2026-11-27，前端 `20261127d`，已发布到安装版）

- **任务原子登记**：`_normalize_job_claim()` 在同一临界区内完成过期清理、运行任务判断、
  登记与返回。预览重复请求接回同一任务；执行按「确认清单 + `recycle_old`」指纹复用，
  不同批次返回 409 `busy`（含运行任务号、总量与 `overlap`），不再把别批结果当成功。
- **线程启动失败**：`_normalize_job_launch()` 把任务置为 `error` 并允许下次重试；只有
  新建任务才启动线程。
- **启动等待与迟到回包**：前端新增 `_normPreviewStart` / `_normApplyStart`，重开入口
  接回同一次等待，不重复 POST；迟到回包先保存任务状态，只有仍是同一弹窗实例才认领。
- **完成收尾**：执行完成先保存 `state=done/result`，250 ms 绘制与图库刷新结束后复核
  owner 与实例号；期间换到其他弹窗则不关闭、不替换。异批 busy 保留本批并提供
  「重试本批」或「重新预览」。
- **失败终态接回**：预览/执行的启动失败与任务失败保存 `state=error`；重开入口显示上次
  原因和「重试预览 / 重试转换 / 重新预览」，不自动发新 POST。执行启动失败保留原清单与
  回收选项可原样重试；执行任务失败不复用旧令牌，只提示重新预览。
- **回归**：T195 新增真实并发预览、同批并发执行、异批 busy 与第二批可处理；
  `autotest/test_normalize_job_ownership.cjs` 用真实 `app.js` 函数跑 15 个迟到/归属/
  失败重开用例。全量 **186/186**（136.55 s），日志
  `_dev_runtime/diagnostics/trae-20261127d-normalize-job-ownership-smoke.log`。
- **候选/发布**：候选包（`built_at 2026-11-27 13:20:58`，前端 `20261127d`）在 7813
  通过三个候选检查与 `check_normalize_failure_ui.py`；回滚清单
  `_dev_runtime/backups/publish-20261127d-20261127-132940.json`（4 个旧文件、8 个
  替换项、15,153,097 字节）。dist 与安装版 exe `9cc0a8c061cd…`、app.js
  `791418984349…`，两处 `config/user.json` 哈希未变；安装版 7810 前端
  `20261127d-1795756858-1795695345-1795519082`、健康正常。

### 4.71 其他来源补登记保护 + 批量转 PNG 共用进度（2026-11-27，前端 `20261127c`，已发布到安装版）

- **其他来源补登记不覆盖当前图**：`register_foreign_file` 遇到同一 `ext_` ID 已有页时
  一律跳过，不再按「同路径才跳过」放行；冲突更新追加
  `WHERE pages.local_path = excluded.local_path`，`rowcount=0` 时清空壳作品并返回
  None。`_auto_png_cleanup_pending` 在归属复核失败、回收未完成两个分支把页原指向的
  旧 PNG 加入本轮 `blocked`，本轮补登记看不到旧图。
- **T194 新增其他来源回收切页用例**：无自然键 JPG 转 PNG 后重建原件待处理，回收替身
  内部把页切回 JPG；首次同步 `foreign_registered=0`、页的路径/指纹/大小/当前格式均
  指向 JPG、旧 PNG 未重新登记；第二次同步 JPG 未被回收、待处理状态清空。反向验证：
  临时回退 store 修复时该用例在批量补登记断言处失败。
- **批量转 PNG 接入共用进度**：新增 `/api/library/normalize-pending/preview/job`
  与 `/apply/job`（含状态查询），与同步接口共用同一套逐张处理逻辑；任务字段
  `stage/done/total/result`，重复点击复用同一任务。前端预览先显示「正在核对候选格式
  与冲突…」流动条，拿到候选数后改用「已预览 n／N 张」计数条；执行另起
  「已处理 n／N 张」，分母为本次确认数量；关闭弹窗后任务继续，从「转为 PNG」入口
  接回进度或最近结果；结果页可重新预览。
- **回归**：新增 T195 覆盖预览/执行真实计数、运行中重复点击复用、`recycle_old=False`
  保留源图、预览后源图变化的部分失败；T175 前端结果渲染改用
  `normalizeApplyShowResult` 直接验证。全量 **186/186**（108.39 s），日志
  `_dev_runtime/diagnostics/trae-20261127c-normalize-progress-smoke2.log`
  （首跑 185/186 仅 T175 旧 harness 失败，修 harness 后全绿）。
- **候选/发布**：候选包 `_dev_runtime/packages/vixiq`（`built_at 2026-11-27
  12:29:33`，前端 `20261127c`）在 7813 通过 `check_ingest_auto_png_candidate.py`、
  `check_foreign_recheck_candidate.py`、`check_normalize_progress_candidate.py`；
  回滚清单 `_dev_runtime/backups/publish-20261127c-20261127-123227.json`（4 个旧
  文件、8 个替换项、新增备份 15,139,194 字节）。dist 与安装版 exe 哈希一致
  （`db0f489ba51b…`），app.js `94a0fc682516…`，两处 `config/user.json` 哈希未变。
- **安装版验证**：7810 前端 `20261127c-1795753773-1795695345-1795519082`、
  `built_at 2026-11-27 12:29:33`、`frozen=true`、`/api/ingest` 默认
  `auto_png=false`、任务状态接口正常返回 404、健康正常。
- **同一文件的前置发布**：其他来源修复先在 `20261127b` 重打包（`built_at
  12:16:07`，回滚清单 `publish-20261127b-20261127-121818.json`，2 个旧文件、4 个
  替换项、14,358,326 字节），安装版验收后继续本项。
- GitHub：`1e4c210` 已推送；本轮提交随后推送。

### 4.70 原件回收二次收口：skip_rels 仍算磁盘存在 + 原子不覆盖退回（2026-11-27，前端 `20261127b`，已发布到安装版）

- **同步对账保留当前图**：`reconcile_output` 的 `skip_rels` 从「完全排除」改为
  「只排除登记/转换，仍算磁盘存在」：跳过文件加入 `disk_rels` 与自然键存在集合，
  缺失判定也先看登记路径是否仍在磁盘。回收期间页被切回 JPG 后，同一请求的写对账
  不再把当前页误判为丢失并重指到 PNG；下一次同步也不会把当前 JPG 当遗留原件回收。
- **退回原子不覆盖**：`_recycle_verified_file` 的 `_back` 改用
  `_publish_new_file`（目标已存在时拒绝），不再先看空闲再 `os.replace`；判定与落位
  之间若有另一操作写入同路径，后来文件保留，原件暂存同目录并报告位置，两份都能用
  SHA-256 验证。
- **失败时在手副本优先**：回收助手取走 claim 后返回错误时，不再先删安全副本再退回
  不存在的 claim；按 claim/safety 的实际存在与校验结果选择可恢复的一份，恢复成功
  才删另一份；无法恢复原路径时保留可恢复副本与位置。下次同步按同一笔失败状态续办，
  不会用「原路径缺失」代替已处理。
- **回归**：T194 新增页切回 JPG 后写对账保留 JPG、第二次同步不回收当前图；退回时
  原位置被抢的原子不覆盖（后来文件与暂存旧图双 SHA）；助手取走 claim 后报错用安全
  副本退回、下一次续办；保留同页待确认双方不登记与正常 PNG 续办。全量 **185/185**
  （113.92 s），日志 `_dev_runtime/diagnostics/trae-20261127b-recheck-smoke.log`。
  另有 T90/归档恢复各一次环境性 flake，隔离与复跑均通过。
- **候选/发布**：候选包 `_dev_runtime/packages/vixiq`（`built_at 2026-11-27
  02:52:08`，前端 `20261127b`）在 7813 通过 `check_ingest_auto_png_candidate.py`；
  回滚清单 `_dev_runtime/backups/publish-20261127b-20261127-025301.json`（3 个旧
  文件按 SHA-256 去重，6 个替换项，新增备份 14,424,519 字节）；dist 与安装版 exe /
  index.html 哈希一致（exe `1240bfc4d550…`），两处 `config/user.json` 哈希未变。
- **安装版验证**：7810 前端 `20261127b-1795693960-1795695345-1795519082`、
  `built_at 2026-11-27 02:52:08`、`frozen=true`、`/api/ingest` 默认
  `auto_png=false`、健康正常。下一步：交 Codex 复验；通过后再接手动「批量转 PNG」
  的共用进度。GitHub 未提交、未推送。

### 4.69 原件回收收口：原子取件 + 归属复核 + 无硬链接依赖（2026-11-27，前端 `20261127a`，已发布到安装版）

- **原子取件**：`_recycle_verified_file` 不再「复制/硬链接快照再删原路径」，改为
  `os.replace(src, claim)` 先把原件移出正式路径，再对实际取得的 claim 校验
  md5/大小。只回收已验证的 claim，不再对原路径做第二次删除，避免回收后再误删
  同大小换入的新文件。
- **归属绑定**：`owned` 回调在取件后、回收后各复核一次最新页归属与
  `pending_recycle*`；回收期间另一连接把页切回原 JPG 时，用回收前的安全副本把
  原件退回原路径，保留待处理状态并报告。`clear_pending_recycle_if` 增加
  `expected_local`，页归属已变时不清状态。
- **失败保全**：取件失败（占用）保留原文件与待处理状态；claim 身份不符时把后来
  文件退回原路径，原路径已被占则暂存在同目录 `.vixiq-recycle-*.tmp` 并在结果中
  给出位置；回收子进程失败同样退回 claim。开关开启/关闭都会续办，失败两轮后
  解除故障可正常清理。
- **不再依赖硬链接/inode**：取件与回收全走同盘 move + 快照校验，文件系统不支持
  `os.link` 时也能完成；文件确实被替换时保留新文件并提示。
- **回归**：T194 新增原件被占用两次（开/关各一次）、回收助手谎报成功但暂存仍在、
  取件前同大小换图、取件后写入新文件、回收期间切页（同步与页详情重试）、禁用硬链接
  下的正常/替换两条路径、`expected_local` 条件清除。全量 **185/185**（116.28 s），
  日志 `_dev_runtime/diagnostics/trae-20261127a-recycle-smoke4.log`。
- **候选/发布**：候选包 `_dev_runtime/packages/vixiq`（`built_at 2026-11-27
  01:39:15`，前端 `20261127a`）在 7813 通过 `check_ingest_auto_png_candidate.py`；
  回滚清单 `_dev_runtime/backups/publish-20261127a-20261127-013952.json`（3 个旧
  文件按 SHA-256 去重，6 个替换项，新增备份 14,421,357 字节）；dist 与安装版 exe /
  index.html 哈希一致（exe `6196d1ac1a67…`），两处 `config/user.json` 哈希未变。
- **安装版验证**：7810 前端 `20261127a-1795693960-1795695345-1795519082`、
  `built_at 2026-11-27 01:39:15`、`frozen=true`、`/api/ingest` 默认
  `auto_png=false`、健康正常。下一步：交 Codex 复验；通过后再接手动「批量转 PNG」
  的共用进度。GitHub 未提交、未推送。

### 4.68 入库自动转 PNG 四次复验返修：归档备份身份 + 待确认整轮排除 + 待回收身份（2026-11-26，前端 `20261126h`，已发布到安装版）

- **反搜归档文件保护（`core/reverse_search.py:_archive_commit`）**：动手前记录源、
  在位目标、跨格式旧页与旧登记四份身份；转换期间目标被他人换掉/新建时绝不覆盖；
  覆盖前备份旧目标并校验备份身份。登记失败只在目标仍属本次且旧登记未变时用
  `os.replace` 恢复；恢复被拒/目标被占时保留校验过的旧图备份，结果里给出可恢复
  路径，不再谎称「已保留原图与原登记」。登记成功后才删源与跨格式旧文件，并核对
  它们仍是开始时那一份，换入的新文件一律保留。新页/加副本登记走
  `_register_archived_page(expect_unregistered=True)` 条件写入，抢先登记的页与文件
  都保留。
- **同步待确认整轮排除**：同页多个未登记候选（JPG + 原生 PNG 等）用 `key_to_rels`
  把双方都加入 `blocked`，写库对账与 foreign 登记整轮跳过；发布后目标被替换时把
  源与后来目标一起排除，本轮双方都不登记。
- **待回收身份绑定**：`_recycle_verified_file` 先在同目录取硬链接/副本快照并校验，
  回收快照，再仅在源路径仍匹配时删源；同大小不同内容的替换会保留新文件并报告。
  `_recycle_files` 非零退出/无输出/成功数不足都返回失败列表，调用方不清待处理状态；
  续办与页详情重试在执行前复核最新页归属，使用 `clear_pending_recycle_if` 只清仍属于
  同一任务的 `pending_recycle*`。
- **回归**：T194 新增恢复 `os.replace` 被拒时保留旧图备份、发布后目标被他人换图再
  登记失败不覆盖来客、登记成功后源 JPG / 跨格式旧 JPG 被替换仍保留、加副本条件写入
  被抢先登记、同页 JPG+原生 PNG 双方 0 登记、发布后目标也 0 登记、待回收同大小替换、
  回收站非零退出、第二连接切页、条件清除不误清后来任务、页详情重试正常续办。全量
  **185/185**（126.24 s），日志
  `_dev_runtime/diagnostics/trae-20261126h-recheck-smoke3.log`（首跑 T171 一次环境性
  失败，隔离与复跑均通过）。
- **候选/发布**：候选包 `_dev_runtime/packages/vixiq`（`built_at 23:53:33`，前端
  `20261126h`）在 7813 通过 `check_ingest_auto_png_candidate.py`（含同页混合冲突页
  查询 404）；回滚清单 `_dev_runtime/backups/publish-20261126h-20261126-235611.json`
  （3 个旧文件按 SHA-256 去重，6 个替换项，新增备份 14,415,334 字节）；dist 与安装版
  exe / index.html 哈希与候选一致，两处 `config/user.json` 哈希未变。
- **安装版验证**：7810 前端 `20261126h-1795693960-1795695345-1795519082`、
  `built_at 23:53:33`、`frozen=true`、`/api/ingest` 默认 `auto_png=false`、健康正常。
  下一步：交 Codex 复验；通过后再做手动「批量转 PNG」的共用进度。GitHub 未提交、
  未推送。

### 4.67 入库自动转 PNG 三次复验返修：并发页保护 + 原件清理续办 + 归档回滚（2026-11-26，前端 `20261126g`，已发布到安装版）

- **同步并发页保护**：`store_page_result(expect_unregistered=True)` 用事务内条件
  `INSERT ... ON CONFLICT DO UPDATE ... WHERE local_path 为空`，另一连接先登记时本次
  不覆盖；`register_foreign_file(expect_new=True)` 用普通 INSERT + IntegrityError 回滚。
  失败只撤能证明属于本次的 PNG，源图与先到登记都保留。
- **产物身份从候选到清理全链路核对**：转换产物先在临时文件取 md5/大小，发布后核对
  仍是同一产物才登记；登记后复核页 `local_path`、在位 PNG 与源图身份，任一变化都
  不删源、报告待确认。源 JPG 替换、目标 PNG 被替换、发布前后来客均覆盖测试。
- **原件清理可续办**：PNG 已登记但原图删除失败时写 `pages.pending_recycle*`
  （复用转 JPG 的待回收状态），跨扫描/重启/开关切换保留；同步开头先续办，开启或关闭
  都会清理。`/api/library/convert-to-jpg/retry-recycle` 扩展支持自动 PNG 页；身份不符
  拒绝清理并清状态，待处理源不会再登记为新图。
- **反搜归档按逻辑页决策**：`_resolve_archive_target` 同时看磁盘最终路径与已有页登记。
  已有 p0.jpg 时开启自动 PNG 不再把 p0.png 当空位：跳过保留源与旧页，加副本分配到
  p1，覆盖在成功后清理旧 JPG。新增 `_archive_commit`：先备好并校验临时产物，覆盖前
  备份旧目标；登记失败恢复旧目标/撤新产物，编码失败保留旧图与源；p1 不会被覆盖。
- **回归**：T194 增加同页并发登记、发布后产物替换、源图替换、清理失败续办（关闭
  开关）、逻辑页 p0.jpg 的跳过/副本/覆盖、归档编码失败/登记失败回滚。
  全量 **185/185**（119.12 s），日志
  `_dev_runtime/diagnostics/trae-20261126g-ingest-auto-png-final-smoke.log`。
- **候选/发布**：候选包 `_dev_runtime/packages/vixiq`（`built_at 22:35:49`，前端
  `20261126g`）在 7813 通过 `check_ingest_auto_png_candidate.py`；回滚清单
  `_dev_runtime/backups/publish-20261126g-20261126-223652.json`（3 个旧文件按
  SHA-256 去重，6 个替换项，新增备份 14,406,666 字节）；dist 与安装版关键文件哈希
  一致，两处 `config/user.json` 哈希未变。
- **安装版验证**：7810 前端 `20261126g-1795693960-1795695345-1795519082`、
  `built_at 22:35:49`、`frozen=true`、`/api/ingest` 默认 `auto_png=false`、健康正常。
  下一步：交 Codex 复验；通过后再做手动「批量转 PNG」的共用进度。GitHub 未提交、
  未推送。

### 4.66 入库自动转 PNG 二次复验返修：提交后再删源 + 归档先探测格式 + 放大态关闭（2026-11-26，前端 `20261126f`，已发布到安装版）

- **同步改为「提交后再清理原图」**：`_auto_png_convert_new_disk_files` 生成并校验属于本次
  任务的临时 PNG，源图全程原位；发布前复核源身份、目标占用与页归属，目标被别的操作
  占用时只丢弃本次候选，绝不删除别人文件；PNG 路径与来源记录提交成功且清理前源身份
  仍一致，才删源图。写库/清理失败只报告 `auto_png_errors`，原图与旧登记保全；失败源
  通过 `skip_rels` 排除出同轮对账，下一次同步可重试。foreign 登记与转换来源改为
  `register_foreign_file(conversion=...)` 同一事务提交，并写入 md5/大小。
- **对账区分查看/诊断/入库**：`/api/library/items`、`/api/library/health`、
  `/api/library/missing-items` 改只读对账，不再顺手登记新 Pixiv JPG；手动同步才按
  开关转换。`reconcile_output(skip_rels=...)` 支持本轮暂不纳入的文件。
- **移动/改名与混合冲突**：其他来源 JPG 外部改名按 md5 指纹重指旧登记，不再重编码；
  `normalize_pending` 带 md5；同页 JPG+原生 PNG 等混合候选在确认前全部保留原格式，
  冲突写入 `conflicts`/`auto_png_errors`。`db/store.reconcile_output` 的
  `new_page_paths` 防止同轮后到文件覆盖先登记页。
- **反搜归档先探测格式再选策略**：`_resolve_archive_target` 在移动前用真实格式与
  最终输出算 canonical，再执行跳过/加副本/覆盖；关闭时伪 PNG 落 `.jpg`，开启时静态
  图落 `.png`；「加副本」按最终文件名页号登记，p1 不覆盖 p0；覆盖/复制都会同步页
  登记与当前格式。
- **放大态 ✕ 一点即关**：`.lightbox-close` 提高 `z-index: 20`，不再被放大后的
  `.lightbox-img-wrap` 盖住；关闭按钮第一下就退出，不会先还原缩略图。
- **回归**：T194 扩写同步写库异常/清理失败、目标被占、源图被替换、同页 JPG+PNG、
  移动改名、列表/健康只读、归档跳过/副本/覆盖两种开关、伪 PNG、归档 p1、放大态关
  闭 CSS；全量 **185/185**（109.59 s），日志
  `_dev_runtime/diagnostics/trae-20261126f-ingest-auto-png-recheck-smoke.log`。
- **候选/发布**：候选包 `_dev_runtime/packages/vixiq`（`built_at 20:15:45`，前端
  `20261126f`）在 7813 通过 `check_ingest_auto_png_candidate.py`（含混合格式冲突、
  伪 PNG、同步转换）；回滚清单
  `_dev_runtime/backups/publish-20261126f-20261126-201706.json`（4 个旧文件按
  SHA-256 去重，8 个替换项，新增备份 14,529,062 字节）；dist 与安装版关键文件哈希
  一致，两处 `config/user.json` 哈希未变。
- **安装版验证**：7810 前端 `20261126f-1795693960-1795695345-1795519082`、
  `built_at 20:15:45`、`frozen=true`、`/api/ingest` 默认 `auto_png=false`、健康正常；
  样式已含放大态关闭层级。下一步：交 Codex 复验；通过后再做手动「批量转 PNG」的
  共用进度。GitHub 未提交、未推送。

### 4.65 入库自动转 PNG 返修：同步事务化 + 定时只读 + 真实格式识别（2026-11-26，前端 `20261126e`，已发布到安装版）

- **磁盘同步事务化**：`web/server.py:_auto_png_convert_new_disk_files` 改为先用
  `db.reconcile_output(write=False)` 取本轮真实新增候选，复核路径/大小/mtime/页键后
  才转换。原文件先备份到应用临时目录，PNG 路径与转换来源记录一起写库成功后才删备份；
  写库失败撤 PNG、恢复原文件，重试仍能写出完整记录。原文件删除失败单独报告
  `auto_png_errors`，不留未登记 PNG，后续只有原图一条正式登记。同页多份未登记文件
  交由对账冲突规则，确认前不改写任何一张。
- **同页冲突不再顶掉登记**：`db/store.py:reconcile_output` 增加本轮 `new_page_paths`；
  同一轮同页不同内容的后到文件只进 `conflicts`，不再用 `mark_page_status` 覆盖刚登记
  的页。
- **定时扫描只读**：`_cached_reconcile(write=False)` 使用独立只读缓存（按图库根目录
  区分），`POST /api/library/sync?periodic=1` 冷/热缓存都不登记、不改写；手动同步仍
  按开关转换新图。
- **反搜入库/归档按真实格式**：`normalize_to_png` 以实际解码格式判定 PNG/动图/静态图，
  同目标路径时校验后原子替换；伪 PNG（JPEG 字节挂 .png）关闭时落 `.jpg`、开启时转
  真 PNG。反搜收件箱与归档都按真实扩展名落盘；可解码但 PNG 编码失败回退原格式并
  说明原因，源图无法解码则保留在收件箱/原处并列为失败；登记抛异常会撤掉本次副本。
- **归档「加副本」页号**：归档后用最终文件名解析实际页码，p1 使用自己的页号和来源
  记录，不再覆盖 p0。关闭自动转 PNG 时也先修正与实际格式不符的扩展名。
- **文案**：同步结果与「转为 PNG」弹窗按实际结果显示自动转换成功/失败/可手动转换，
  不再写「同步只识别并报告」；正常 JPG 用「可转为 PNG」表述。
- **回归**：T194 扩写同步写库异常/原文件处理异常/重试、同页冲突、定时冷热缓存、
  伪 PNG 文件导入与反搜入库、坏图与编码失败、归档 p1；T31/T33/T74/T100/T104/T112
  改用真实图片字节。全量 **185/185**（96.46 s），日志
  `_dev_runtime/diagnostics/trae-20261126e-ingest-auto-png-rework-smoke.log`。
- **候选/发布**：候选包 `_dev_runtime/packages/vixiq`（`built_at 19:52:40`，前端
  `20261126e`）在 7813 隔离数据通过 `check_ingest_auto_png_candidate.py`（含同页冲突、
  伪 PNG 关闭/开启）；回滚清单
  `_dev_runtime/backups/publish-20261126e-20261126-195416.json`（4 个旧文件按
  SHA-256 去重，8 个替换项，新增备份 15,107,421 字节）；dist 与安装版关键文件哈希
  一致，两处 `config/user.json` 哈希未变。
- **安装版验证**：7810 前端 `20261126e-1795693960-1795539441-1795519082`、
  `built_at 19:52:40`、`frozen=true`、`/api/ingest` 默认 `auto_png=false`、健康正常；
  页面含入库格式开关。下一步：交 Codex 复验；通过后再做手动「批量转 PNG」的共用进度。
  GitHub 未提交、未推送。

### 4.64 入库时自动转为 PNG：默认关闭 + 覆盖全部正式入库入口（2026-11-26，前端 `20261126d`，已发布到安装版）

- **设置项**：`utils/config.py` 新增 `INGEST_DEFAULTS={"auto_png": False}` 与
  `ingest_auto_png()`；`resources/default.json` 新增 `ingest.auto_png=false`。
  旧 `user.json` 缺节/缺键、全新安装都按关闭；设置页「浏览与下载 → 🖼️ 入库格式」
  开关写回 `config/user.json`，下一次入库立即生效，重启保留。接口
  `GET/POST /api/ingest`（白名单只认 `auto_png`）。
- **统一策略**：`core/image_format.apply_ingest_format()` 开启时转调
  `normalize_to_png`；关闭时只做可读性检查并按原字节复制，不重新编码、不改扩展名、
  不写转换来源；复制走目标同目录临时文件 + 原子替换，避免下载暂存与图库跨盘。
- **覆盖入口**：下载/补页/强制重下（`core/crawler._finalize_downloaded_page`）、
  网页上传与文件/文件夹导入（`web/server._ingest_image_file`）、反搜收件箱
  `/api/reverse/adopt`、反搜命中自动归档（`core/reverse_search`）、手动
  `/api/library/sync` 实际登记的新静态图。开启时 JPG/静态 WEBP 转 PNG，原生 PNG
  直入，GIF/多帧 WEBP 保留动画，转换失败保留原格式并在结果说明；关闭时用
  SHA-256 核对原始字节。`format_choice='jpg'` 的页在补图/修复/重下时保持 JPG。
- **不改写已有记录**：`periodic=1` 同步、体检只读步骤、导入预览/匹配阶段不改文件；
  手动同步只转换「路径未登记且页键不存在」的新图，切换设置后已有文件保持原样。
- **前端**：设置页新增 `#ingest-auto-png-toggle` / `#ingest-auto-png-state`；
  `loadIngestFormat()` / `toggleIngestFormat()` 接入设置页、`settings` 数据域刷新与
  首屏加载；导入确认、执行中、`_askImportMode` 文案按开关显示，删除写死的
  「静态图片统一转 PNG」。同步结果 toast 显示自动转 PNG / 转换失败计数。
- **回归**：新增 T194（配置默认/接口、关闭时上传与导入及下载的 SHA-256、开启时
  JPG/WEBP/PNG/GIF/失败、format_choice、反搜入库/归档、磁盘同步、切换不改写、前端
  接线与版本串）；T174/T175 固定按关闭运行以保留「同步只报告待转 PNG」语义。
  全量 **185/185**（109.70 s），日志
  `_dev_runtime/diagnostics/trae-20261126d-ingest-auto-png-smoke.log`。
- **候选/发布**：候选包 `_dev_runtime/packages/vixiq`（`built_at 13:29:39`，前端
  `20261126d`）在 7813 隔离数据通过 `check_ingest_auto_png_candidate.py`；回滚清单
  `_dev_runtime/backups/publish-20261126d-20261126-133035.json`（2 个旧文件按
  SHA-256 去重，4 个替换项，新增备份 14,327,412 字节）；dist 与安装版已替换
  `vixiq.exe`、`base_library.zip`；两处 `config/user.json` 哈希未变。
- **安装版验证**：7810 前端 `20261126d-1795670623-1795539441-1795519082`、
  `built_at 13:29:39`、`frozen=true`、`/api/ingest` 默认 `auto_png=false`、健康正常；
  页面含入库格式开关。下一步：交 Codex 验收；通过后再接手动「批量转 PNG」的共用
  进度。GitHub 未提交、未推送。

### 4.63 导入启动失败收尾：重开的弹窗直接显示失败（2026-11-26，前端 `20261126c`，已发布到安装版）

- **导入启动异常分支使用当前等待器实例号**：`importStartRun` 的 catch 改为
  `opModalSame(waiter.seq || seq)`，与整理入口一致；同时结束 `_importStart` 等待。
  关闭弹窗后从导入入口重开时，启动失败（普通 HTTP 500 / 断网）会直接更新重开后的
  弹窗，而不是停在「导入中…」禁用按钮。
- **失败弹窗可操作**：匹配时调用 `importShowResult(_importRun)`，标题「⚠ 导入失败」，
  显示服务端原因或网络错误，提供「重新选择」与「关闭」；失败状态保存在
  `_importRun`，从原入口重开仍可查看。跨业务弹窗仍按实例号隔离。
- **回归**：新增 T193（异常分支等待器实例号、结束等待、失败状态保存、版本串）。
  全量 **184/184**（121.71 s），日志
  `_dev_runtime/diagnostics/trae-20261126c-import-start-fail-smoke.log`。
- **浏览器实测**：`check_shared_progress_ui.py` 新增：
  - 启动 POST 挂起后返回普通 HTTP 500：关闭并重开，弹窗直接显示「导入失败」、
    「模拟启动失败」与「重新选择/关闭」；
  - 启动 POST 模拟断网（TypeError: Failed to fetch）：关闭并重开，同样直接显示
    「导入失败」与网络原因、重新选择入口。
  日志 `shared-progress-ui.log`；整理/导入既有 UI 脚本回归通过。
- **候选/发布**：候选包 `built_at 11:39:12`（前端 `20261126c`）在 7813 通过版本、
  健康、静态串与导入任务。回滚清单
  `_dev_runtime/backups/publish-20261126c-20261126-114009.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,094,045 字节**）；dist 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`；两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261126c-1795664352-1795539441-1795519082`、
  `built_at 11:39:12`、`frozen=true`、健康正常。
- 下一步：交 Codex 复验；通过后批量转 PNG 直接用这套控件与跟踪。GitHub 未提交、未推送。

### 4.62 启动等待重开 / 迟到忙响应重试 / 扫描文案与响应体超时（2026-11-26，前端 `20261126b`，已发布到安装版）

- **启动等待独立状态**：导入/整理启动回包前，`_importRun`/`_tidyRun` 带 `starting`；
  `_importStart`/`_tidyStart` 记录等待器。关闭弹窗后从原入口重开，走
  `importResumeStarting`/`tidyResumeStarting`：接回同一次启动等待并绑定新的弹窗
  实例号，不发 `undefined` 任务查询；回包只提交一次，匹配后直接进入进度/结果。
- **迟到忙响应保留本批**：迟到回包先按业务状态分类。`start.busy` 时只保存
  `_importPendingBatch` 与 `_importBusyInfo`，从入口 `importShowBusyPending()`
  显示原因并提供「重试本批」，不会把别批 `job_id` 记成自己的任务；stale/expired
  保存为可读错误。匹配弹窗时的忙响应也走同一展示函数。
- **扫描可见文案**：`opProgressPaint` 的 flow 分支只在 `o.text !== undefined`
  时更新文本，阶段 paint 不再清空「正在扫描所选…」；可见文案与读屏区域同步。
- **响应体超时**：`opFetchJson` 内层 `resp.json()` 的 `AbortError/terminated/aborted`
  统一转「任务查询超时」，不再被吞成「任务查询失败」。
- **回归**：新增 T192（starting/等待器/启动重开/忙响应保存/扫描文案/响应体超时）。
  全量 **183/183**（98.19 s），日志
  `_dev_runtime/diagnostics/trae-20261126b-starting-resume-smoke.log`。
- **浏览器实测**：`check_shared_progress_ui.py` 新增：
  - 导入启动 POST 延迟 1.5 s，关闭后从入口重开（不出现「任务不存在」、只提交一次，
    回包后直接看到结果）；整理启动同样验证；
  - 迟到 409 busy（带别批 `job_id`）后从入口显示「另有导入正在执行」，点「重试本批」
    成功，未查询别批任务；
  - 扫描可见文案 `#imp-scan-progress-text` 与读屏同步；
  - `opFetchJson` 对响应体中断抛「任务查询超时」。
  日志 `shared-progress-ui.log`；整理/导入两个既有 UI 脚本回归通过。
- **候选/发布**：候选包 `built_at 11:09:04`（前端 `20261126b`）在 7813 通过版本、
  健康、静态串与导入任务。回滚清单
  `_dev_runtime/backups/publish-20261126b-20261126-110959.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,090,895 字节**）；dist 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`；两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261126b-1795662544-1795539441-1795519082`、
  `built_at 11:09:04`、`frozen=true`、健康正常。
- 下一步：交 Codex 复验；通过后批量转 PNG 直接用这套控件与跟踪。GitHub 未提交、未推送。

### 4.61 迟到回包归属 + 扫描阶段播报（2026-11-26，前端 `20261126a`，已发布到安装版）

- **启动/预览迟到回包不覆盖当前弹窗**：`openLibModal` 每次递增并记录弹窗实例号
  `dataset.opSeq`；`opModalSeq()` / `opModalSame(seq)` 放进共用逻辑。导入启动
  (`importStartRun`)、整理启动 (`applyTidy`)、整理预览 (`openTidyPreview`) 在发起
  请求前记下 `seq`，回包先保存业务状态：有任务号就按原任务号继续跟踪，只有
  `opModalSame(seq)` 时才接管或替换弹窗。成功、拒绝、异常以及关闭后重开同一业务
  都按实例号区分，旧回包只影响发起时的界面。
- **扫描阶段播报**：导入扫描 `importScanProgressHtml` 与整理预览扫描改用
  `opProgressHtml(..., {mode:'flow'})`，带 `role=status`/`aria-live=polite` 隐藏区域，
  打开后 `opProgressPaint(..., {stage:'正在扫描所选…'/'正在扫描目录与检查冲突'})`；
  扫描、执行、终态分别由控件 live 区域与结果弹窗标题播报。
- **响应体卡死也识别为超时**：`opFetchJson` 除 `AbortError` 外，把
  `terminated/aborted` 也转成「任务查询超时」，保留继续查询入口。
- **回归**：新增 T191（实例号、`opModalSame(seq)`、两处扫描共用控件、响应体中断
  文案、版本串）。全量 **182/182**（95.85 s），日志
  `_dev_runtime/diagnostics/trae-20261126a-late-response-smoke.log`。
- **浏览器实测**：`check_shared_progress_ui.py` 新增「导入启动回包迟到」场景
  （POST 延迟 1.6 s，关闭启动弹窗并打开整理预览，回包后预览保持，任务从导入入口
  接回并完成）与扫描播报断言；原有归属、失败、查询中断、超时、淡入/减少动效场景
  继续通过。`check_tidy_progress_ui.py`、`check_import_progress_ui.py` 回归通过；
  日志 `shared-progress-ui.log`。
- **候选/发布**：候选包 `built_at 10:17:30`（前端 `20261126a`）在 7813 通过版本、
  健康、静态串与导入任务。回滚清单
  `_dev_runtime/backups/publish-20261126a-20261126-101827.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,088,677 字节**）；dist 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`；两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261126a-1795659450-1795539441-1795519082`、
  `built_at 10:17:30`、`frozen=true`、健康正常；静态 app.js 含 `opModalSame` /
  `opModalSame(seq)` / 响应体中断识别。
- 下一步：交 Codex 复验；通过后批量转 PNG 直接用这套控件与跟踪。GitHub 未提交、未推送。

### 4.60 共用进度返修：弹窗归属 / 单次查询超时 / 阶段播报（2026-11-25，前端 `20261125a`，已发布到安装版）

- **后台任务只更新自己的弹窗**：`openLibModal` 每次打开清空归属，业务调用后
  `opClaimModal(owner)` 认领（整理预览 `tidy-preview`、整理进度 `tidy:任务号`、
  整理结果 `tidy-result:任务号`、导入扫描/选择 `import-scan`/`import-choose`、
  导入进度 `import:任务号`、导入结果 `import-result:任务号`）。任务状态、结果、
  异常先写入各自任务记录；只有 `opModalMatches(owner)`（弹窗可见且归属匹配）才
  更新、关闭或替换弹窗。成功回调延时 350ms 后再次核对归属。
- **单次查询超时**：新增 `opFetchJson`（AbortController，默认 5s）供
  `opPollJobStatus` 使用；服务端长时间无响应时计入重试，连续失败进入「查询中断」
  并保留最后进度，恢复后从原任务号继续。保留 500 重试路径。
- **阶段播报与出现阈值**：进度控件带 `role=status` / `aria-live=polite` /
  `aria-atomic=true` 隐藏区域，只在阶段变化时写入（逐张计数不打断）；弹窗标题
  带 `aria-live` 播报结果。`openLibModal` 给 `.op-progress` 加约 300ms 淡入
  （`op-fade-in` → `op-shown`），短任务直接出结果；减少动效下禁用过渡与流动动画。
- **回归**：新增 T190（归属函数与两个入口认领位置、`opFetchJson`/超时文案、
  aria 属性、淡入类、版本串）。全量 **181/181**（115.72 s），日志
  `_dev_runtime/diagnostics/trae-20261125a-shared-progress-fix-smoke.log`。
- **浏览器实测**：新增 `check_shared_progress_ui.py`：
  - 导入成功时不关闭已打开的整理预览，从导入入口看到正确结果；
  - 整理失败时不覆盖已打开的导入选择弹窗，整理入口随后显示失败结果；
  - 连续 500 进入查询中断时整理预览保持，恢复后从导入入口继续查询并完成；
  - `opFetchJson` 对慢响应抛「任务查询超时」；
  - 阶段播报（`role=status`/`aria-live=polite`/「逐张导入」）与约 300ms 淡入；
    减少动效下流动动画 `animation-name=none`。
  日志 `shared-progress-ui.log`；整理/导入两个既有 UI 脚本回归通过。
- **候选/发布**：候选包 `built_at 00:57:21`（前端 `20261125a`）在 7813 通过版本、
  健康、静态串与导入任务。回滚清单
  `_dev_runtime/backups/publish-20261125a-20261125-005827.json`（5 个旧文件按 SHA-256
  去重，10 个替换项，新增备份 **15,211,873 字节**）；dist 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`、`style.css`；两处
  `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261125a-1795539441-1795539441-1795519082`、
  `built_at 00:57:21`、`frozen=true`、健康正常；静态 app.js 含 `opClaimModal` /
  `opModalMatches` / `opFetchJson` / `opRevealProgressIn` / 导入结果归属。
- 下一步：交 Codex 复验；通过后批量转 PNG 直接用这套控件与跟踪。GitHub 未提交、未推送。

### 4.59 整理与导入共用进度控件与任务跟踪（2026-11-24，前端 `20261124n`，已发布到安装版）

- **共用代码位置**（`web/static/app.js`，约 9903–10026 行）：
  - `opProgressHtml(wid, o)`：生成进度控件，支持 `flow` / `count` / `segments`
    三种显示方式；`textId`/`fillId` 可覆盖子元素 id，兼容已有入口。
  - `opProgressPaint(wid, o)`：按阶段更新显示方式、真实计数、阶段充能状态。
  - `opPollJobStatus(url, jobId, onState, opts)`：通用任务轮询；HTTP 5xx 等带
    JSON 的错误响应也计入重试（连续 5 次或超时才抛错）；任务自身报错抛出的
    Error 带 `job` 字段，业务可区分「任务失败」与「查询中断」。
  - `opTrackJob(key, jobId, opts)`：按「业务:任务号」去重，同一页面同一任务只保留
    一套轮询；查询中断后再次调用即从原任务号继续。
- **整理接入**：`tidyRunProgressHtml` 用 `opProgressHtml('tidy-run-exec'/'tidy-run-rmdir')`
  生成执行与清理两条计数控件；`tidyPaintRun` 用 `opProgressPaint` 更新计划数与
  目录数；`tidyRunWatch` 用 `opTrackJob('tidy', …)`；查询中断时保留最后进度并显示
  「继续查询」，任务失败仍走原结果弹窗。
- **导入接入**：`importRunProgressHtml` 用 `opProgressHtml('imp-run-flow'/'imp-run-count')`；
  `importPaintRun` 用 `opProgressPaint` 在扫描流动与逐张计数间切换；`importRunWatch`
  用 `opTrackJob('import', …)`；查询中断同样保留进度与继续查询。
- **回归**：新增 T189（共用函数、两个入口调用位置、旧轮询 Promise 已并入
  `_opTracks`、版本串）。全量 **180/180**（127.20 s），日志
  `_dev_runtime/diagnostics/trae-20261124n-shared-progress-smoke.log`。
- **浏览器实测**：
  - `check_tidy_progress_ui.py` 回归通过（扫描流动、执行 `／3`、空目录 `／5`、
    关闭重开接回、结果失败原因）。
  - `check_import_progress_ui.py` 回归通过，并新增：主流程轮询前 2 次任务查询返回
    500 → 自动重试后继续完成（保留进度、无中断弹窗）；阶段 10 使用 360×800 窄屏 +
    `prefers-reduced-motion: reduce`，共用控件不溢出、流动动画 `animation-name=none`；
    执行中替换仍按源文件变化跳过。
  - 关闭弹窗、切页后从原入口接回；同一页面重复打开只保留一套轮询（`_opTracks` 去重）。
- **候选/发布**：候选包 `built_at 23:56:57`（前端 `20261124n`）在 7813 通过版本、
  健康、静态串与导入任务。回滚清单
  `_dev_runtime/backups/publish-20261124n-20261124-235815.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,078,925 字节**）；dist 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`；两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124n-1795535817-1795527753-1795519082`、
  `built_at 23:56:57`、`frozen=true`、健康正常；静态 app.js 含 `opProgressHtml` /
  `opTrackJob` / 整理与导入接入 / 「继续查询」。
- 下一步：交 Codex 验收；通过后批量转 PNG 直接用这套控件与跟踪。GitHub 未提交、未推送。

### 4.58 导入执行中源图变化：逐张核对身份（2026-11-24，前端 `20261124m`，已发布到安装版）

- **逐张核对确认身份**：`_import_execute_plan(..., expected=快照)` 在每张复制前
  与复制后各核对一次源文件大小/mtime；源文件在等待或复制过程中被替换时，
  丢弃本次副本、不写图库，该项留在原处，记入 `source_changed`（`源文件已变化`）；
  源文件消失记 `源文件已消失`。其他图片继续处理。
- **分母不变**：源文件变化/消失的项仍计入本次已处理（`done` 推进），
  进度仍到 `N／N`；结果弹窗新增「源文件已变化或消失，未导入（N 张）」分区，
  列出文件名与原因，摘要行显示「源文件变化未导入 N 张」。
- **任务接线**：job 路由把预览快照 `expected` 传给执行器，确保只核对确认页清单。
- **回归**：T188 新增执行中替换（第一张 1／2 后把第二张换成 29×18 另一张有效图，
  结果只入库第一张、`source_changed` 列出第二张「源文件已变化」、进度 2／2、
  图库无替换图）与执行中消失（`源文件已消失`、进度 2／2）两个场景；并发 fake
  执行器断言收到预览快照。全量 **179/179**（103.92 s），日志
  `_dev_runtime/diagnostics/trae-20261124m-import-src-change-smoke.log`。
- **浏览器复测**：`check_import_progress_ui.py` 新增第 10 阶段（两张有效合成图，
  执行到 1／2 时替换下一张）验证结果「源文件已变化或消失，未导入（1 张）」、
  文件名与原因、第一张已入库、替换内容未入库、源文件仍在且尺寸为替换后 29×18；
  前 9 步回归（预览新增/删除、批次互斥、四类结果、按钮文案）继续通过；
  日志 `import-progress-ui.log`。
- **候选/发布**：候选包 `built_at 23:44:27`（前端 `20261124m`）在 7813 通过版本、
  健康、静态串与两轮导入任务。回滚清单
  `_dev_runtime/backups/publish-20261124m-20261124-234531.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,077,452 字节**）；dist 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`；两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124m-1795535067-1795527753-1795519082`、
  `built_at 23:44:27`、`frozen=true`、健康正常；静态 app.js 含 `source_changed` 与
  「源文件已变化或消失，未导入」。
- 下一步：交 Codex 复验；通过后再做批量转 PNG。GitHub 未提交、未推送。

### 4.57 导入进度返修：确认清单绑定 / 批次识别 / 按钮文案（2026-11-24，前端 `20261124l`，已发布到安装版）

- **确认页清单与实际执行一致**：dry-run 生成 `scan_token` 并在服务端缓存本次
  预览的文件清单（路径 + 大小 + mtime）。确认后执行只认这份清单，不再重扫目录；
  预览后新增的文件留在后续批次。原清单文件消失或变化时，job 返回
  `preview_stale` + 变化文件，前端提示重新扫描确认（本批不执行）。
- **同批重试与另一批区分**：任务记录 `batch_key`（清单令牌 + 无 ID 处理方式，或
  来源路径哈希 + 处理方式）。同批重试返回同一 `job_id`（`reused=true`）；
  不同来源或改处理方式返回 409 `busy` + 当前任务来源。前端保留本批参数，
  提供「重试本批」；进度弹窗显示「来源：…」，明确哪批正在执行。旧同步入口仍互斥 409。
- **按钮提示与行为一致**：`index.html` 导入按钮悬停改为「外部源文件保持原位
  （不会被移走或删除）」，不再声称进回收站。
- **回归**：T188 扩充（预览后新增文件不进入本批并留在原处；删除/修改清单文件
  返回 `preview_stale` 且列出原因；同批重试复用；不同来源、同预览改处理方式、
  旧同步均被挡；未知令牌 `preview_expired`）。全量 **179/179**（116.90 s），
  日志 `_dev_runtime/diagnostics/trae-20261124l-import-fix-smoke.log`。
- **浏览器复测**：`check_import_progress_ui.py` 在确认页出现后加入新文件，
  执行仍为 `／5` 且新文件未入库；再导入一批时删除一张源文件，开始导入被拒，
  弹窗列出「990702_p0.png（文件已消失）」；进度弹窗显示来源；按钮悬停无「回收站」；
  源文件全部原位。日志 `import-progress-ui.log`。
- **候选/发布**：候选包 `built_at 23:15:30`（前端 `20261124l`）在 7813 通过版本、
  健康、静态串、预览令牌新增文件与删除文件两条路径。回滚清单
  `_dev_runtime/backups/publish-20261124l-20261124-231652.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,070,126 字节**）；dist 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`；两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124l-1795533330-1795527753-1795519082`、
  `built_at 23:15:30`、`frozen=true`、健康正常；静态 app.js 含 `scan_token` /
  `importRetryPending` / `preview_stale` / `imp-run-src`，按钮文案已更新。
- 下一步：交 Codex 复验；通过后再做批量转 PNG。GitHub 未提交、未推送。

### 4.56 导入图片/文件夹进度（2026-11-24，前端 `20261124k`，已发布到安装版）

- **扫描用流动、确认后计数**：系统选择框返回后才打开导入弹窗，显示
  `.op-track.flow` + 「正在扫描所选文件夹/图片…」；dry-run 拿到总数并确认后，
  后台任务改用「已处理 X／N 张」计数条，分母固定为本次确认的计划张数。
- **单张处理推进一次**：`web/server.py` 抽出模块级 `_import_build_plan` /
  `_import_execute_plan`；执行循环内成功、复用、待确认、复制/入库失败、转换失败
  都算已处理并回调 `progress_cb(done,total)`。新增
  `POST /api/library/import-from-path/job` 与 `GET .../job/<id>`
  （`prepared/done/total/state/result`），同一图库同时只跑一个导入；重复提交返回
  同一 `job_id`（`reused=true`），旧同步入口运行中返回 409。
- **结束四类汇总**：结果弹窗列已入库（Pixiv 作品 / 自定义作品）、已复用、待确认、失败；
  转反搜与转换失败单独注明；待确认保留原因，失败保留文件名；源文件保持原位。
- **关闭/切页接回**：前端 `_importRun` 保存当前/最近任务，运行中关闭弹窗或切页后
  从原入口回来接回进度；结束后仍显示原结果，提供「再导入一批」。扫描期间用户关闭
  弹窗时迟到结果不再弹确认（`_importScanSeq`）。
- **回归**：新增 T188（固定合成 3 张：复用/入库/转反搜；回调 done 1→3 且分母恒 3；
  HTTP 任务 total=2 → done=2；并发同 job_id、旧同步 409；注入复制失败后 done 仍到
  total 且 failed 保留文件名；静态接线与 `20261124k`）。全量 **179/179**（113.58 s），
  日志 `_dev_runtime/diagnostics/trae-20261124k-import-progress-smoke.log`。
- **浏览器复测**：`check_import_progress_ui.py`（合成图库 5 张：复用/入库/待确认/
  复制失败/无ID）验证扫描流动、确认「共 5 张」、执行 `／5` 计数、执行中关闭重开接回、
  切页再回来、结果「已入库 1 · 已复用 1 · 待确认 1 · 失败 1」、关闭重开仍是结果、
  取消扫描不弹确认、源文件全部原位；日志 `import-progress-ui.log`。
- **候选/发布**：候选包 `built_at 22:44:36`（前端 `20261124k`）在 7813 通过
  （版本/健康/静态串 + 两轮导入任务）。回滚清单
  `_dev_runtime/backups/publish-20261124k-20261124-224722.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,055,929 字节**）；`dist` 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`，哈希一致；两处
  `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124k-1795531476-1795527753-1795519082`、
  `built_at 22:44:36`、`frozen=true`、健康正常；静态 app.js 已含
  `importRunWatch`/`importShowResult`/`已处理 X／N 张`。
- 下一步：第二批其余项（批量转 PNG、单张 PNG 转 JPG、以图搜图）；GitHub 未提交、未推送。

### 4.55 整理进度返修：稳定分母 / 单任务互斥 / 结果保留（2026-11-24，前端 `20261124j`，已发布到安装版）

- **分母从一开始稳定**：任务初始 `total=0`、`planned=false`；前端在分母未确认时显示
  「正在核对整理计划…」，不再先显示全部快照数。执行层回调后才以非清理计划数为分母；
  清理阶段在候选总数确定后才报 `0／N`；无清理候选时不发清理回调、前端不显示该条。
  只清空目录时执行条显示「无整理项需要执行」，清理条 `0／N → N／N`。
- **同一图库只运行一个整理**：新增 `_tidy_run_guard`，异步任务与旧同步入口共用互斥。
  重复提交返回已有 `job_id`（`reused=true`）；旧同步入口在运行中返回 409 并带 `job_id`。
- **重新进入接回原任务**：前端 `_tidyRun` 保存当前/最近任务。运行中关闭弹窗或切页后
  再点「整理文件夹」接回进度，不重新预览；任务结束后重新进入仍显示原结果，
  结果弹窗提供「重新预览」。
- **部分失败可查**：结果弹窗区分成功汇总、失败、跳过/状态变化、写库待处理；
  失败项保留具体原因，不再只留 toast 总数。失败/跳过也计入已处理计数。
- **回归**：新增 T187（两个并发任务同 job_id 且执行器只进一次、旧同步 409、任务初始
  `planned=false/total=0`、注入移动失败后其他项完成、无空目录不发清理阶段、
  只清空目录时执行分母 0/清理分母 2）。全量 **178/178**（110.77 s），日志
  `_dev_runtime/diagnostics/trae-20261124j-tidy-recheck-smoke.log`。
- **浏览器复测**：`check_tidy_progress_ui.py`（合成图库，注入 1 次移动失败）验证
  首屏无 `／8` 分母、执行 `／3` 至完成、空目录 `／5`、执行中关闭重开接回且不重新扫描、
  结果弹窗显示「失败（1 项）+ 原因」和跳过项、关闭重开仍是原结果、点「重新预览」才再次扫描；
  日志 `tidy-progress-ui.log`。
- **候选/发布**：候选包 `built_at 22:17:05`（前端 `20261124j`）在 7813 通过。回滚清单
  `_dev_runtime/backups/publish-20261124j-20261124-221757.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,052,311 字节**）；`dist` 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`，三处哈希一致；
  两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124j-1795529825-1795527753-1795519082`、
  `built_at 22:17:05`、`frozen=true`、健康正常；整理任务 GET 不存在 ID 返回 404；
  真实库 1605 作品 / 2108 图 / 433 作者，壁纸库 1180。
- 下一步：第二批导入进度；GitHub 未提交、未推送。

### 4.54 整理文件夹进度（2026-11-24，前端 `20261124i`，已发布到安装版）

- **预览扫描用流动**：`openTidyPreview()` 点击后立即打开同一弹窗，显示
  `.op-track.flow` + 「正在扫描目录与检查冲突…」；预览接口与认领扫描并行，返回后
  在同一弹窗替换成计划表格。预览仍不改任何文件。
- **确认执行按计划计数**：新增后台任务 `POST /api/library/tidy/apply/job` 与
  `GET /api/library/tidy/apply/job/<id>`；`_tidy_apply_plans` 新增 `progress_cb`，
  以本次确认且匹配到的**非空目录计划数**为分母（跳过/失败也算已处理），前端
  `tidyPollJob` 轮询并显示「已处理 12／28 项」。旧同步接口保留。
- **空目录清理单列阶段**：执行阶段结束后进入独立的「清理空目录」阶段，
  候选总数确定后才从 `0／N` 开始报计数；完成后 `rmdir_done=rmdir_total`。
- **浏览器验收**：`check_tidy_progress_ui.py`（隔离库，3 个待归位目录 + 2 个空目录，
  测试内加 0.35 s/事件延迟）验证预览流动条、计划 8 项、执行分母 3、空目录分母 5、
  执行中切页再回来进度仍在、完成后目录归位/空夹删除；日志 `tidy-progress-ui.log`。
- **回归**：新增 T186（固定合成计划：执行分母 4、跳过也计数、空目录 0/1→1/1、
  文件系统结果、前后端静态接线）。全量 **177/177**（109.58 s），日志
  `_dev_runtime/diagnostics/trae-20261124i-tidy-progress-smoke.log`。
- **候选/发布**：候选包 `built_at 22:02:15`（前端 `20261124i`）在 7813 通过版本/健康、
  壁纸 Esc 与作者排序同步。回滚清单
  `_dev_runtime/backups/publish-20261124i-20261124-220310.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **15,044,658 字节**）；`dist` 与安装版已替换
  `vixiq.exe`、`base_library.zip`、`app.js`、`index.html`，三处哈希一致；
  两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124i-1795528935-1795527753-1795519082`、
  `built_at 22:02:15`、`frozen=true`、健康正常；整理任务 GET 不存在 ID 返回 404；
  真实库 1605 作品 / 2108 图 / 433 作者，壁纸库 1180。
- 第二批剩余：导入、批量 PNG、单张 JPG、以图搜图；GitHub 未提交、未推送。

### 4.53 作者工具栏行距与窄屏方向标记（2026-11-24，前端 `20261124h`，已发布到安装版）

- **两行留白**：作者工具栏第一行继承了 `.field-row` 的上下 12 px 外边距，第二行也有 12 px，
  导致控件边缘相距 54 px。限定作者页覆盖：`.management-primary.au-row-main { margin:0 }`、
  `#authors-filters { margin:0 }`、`#tab-authors .management-extra-inner { padding-top:6px }`。
  两行控件边缘约 6 px，行内 gap 6 px、控件 29 px，总数/页码吸顶位置不变。
- **窄屏方向重复**：`#authors-sort-word` 漏了 `.au-sort-word` 类，窄屏隐藏规则没命中，
  按钮同时显示「降序」和「↓」。补类后窄屏只保留箭头，桌面仍显示升降序文字和展开折角；
  `aria-label` 继续包含字段和方向。另把窄屏排序菜单改为左对齐
  （`.au-sort-menu { left:0; right:auto; }`），避免 320 px 向左溢出。
- **复测**：1280 / 1024 / 390 / 320 px 普通态与状态+来源同时生效时两行边缘均约 6 px、
  无横向溢出；390/320 两种字段（作品数/最后更新）升降序都只留一个箭头；第 2 页改 ID 升序
  回第 1 页、作品数表头与工具栏同步、关键词+状态+来源改最后更新仍 6 位且筛选数 2、
  320 px 筛选面板在视口内、Esc 回入口。日志 `author-toolbar-ui.log`。
- **回归**：T185；全量 **176/176**（121.33 s），日志
  `_dev_runtime/diagnostics/trae-20261124h-author-menu-spacing-smoke.log`。
- **候选/发布**：候选包 `built_at 21:42:33`（前端 `20261124h`）在 7813 通过版本/健康、
  壁纸 Esc 与作者排序同步。回滚清单
  `_dev_runtime/backups/publish-20261124h-20261124-214327.json`（4 个旧文件按 SHA-256
  去重，8 个替换项，新增备份 **14,497,715 字节**）；`app.js` 内容未变，仅
  `vixiq.exe`、`base_library.zip`、`index.html`、`style.css` 替换；三处哈希一致；
  两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124h-1795520603-1795527753-1795519082`、
  `built_at 21:42:33`、`frozen=true`、健康正常；真实库 1605 作品 / 2108 图 / 433 作者，
  壁纸库 1180。
- 下一步：操作进度第二批；GitHub 未提交、未推送。

### 4.52 作者管理工具栏两行布局（2026-11-24，前端 `20261124g`，已发布到安装版）

- **桌面**：第一行搜索（占剩余宽度、带清除）、搜索按钮、排序「排序：字段 升序/降序 ▾」、收起；
  第二行状态、来源、清除筛选（有筛选才出现）、右端「查找重复作者」。
- **窄屏（≤980px）**：第一行搜索 + 更多；第二行排序（字段 + 箭头）、筛选 N ▾。
  状态/来源/清除收进筛选弹层；「查找重复作者」和收起收进「更多」。
- **排序**：字段为画师、作者 ID、作品数、最后更新；初始作品数降序；首次改选画师/ID 为升序，
  作品数/最后更新为降序；同字段可切方向。工具栏与可排序表头共用 `sortAuthors` 状态入口，
  点表头后工具栏字段/箭头同步，点工具栏后表头箭头同步；改字段/方向回第 1 页，
  查询词、状态、来源随请求保留。表头同字段点击切换升降序，新字段按该字段默认方向。
- **筛选**：状态与来源可组合，计数只算这两项；清除筛选只重置筛选，保留关键词与排序。
- **下拉**：与壁纸同一套交互——点外部/Esc 关闭并回入口，简化菜单里第一次 Esc 只关下拉、
  第二次才关简化菜单，`e.stopPropagation()` 由下拉先消费；焦点落到可见叠三角。
- **代码**：`index.html` 作者区块、`app.js` 的 `authorsNextSort / authorsFilterCount /
  authorsSyncSortUI / authorsSyncFiltersUI / authorsSetSortField / authorsSetSortOrder /
  authorsClearFilters / initAuthorsToolbarLayout`、`style.css` 的 `.au-*`；
  保留 `#authors-search-input`、`#authors-f-status`、`#authors-f-source`、`#authors-dupes-btn`、
  `.management-primary/.management-extra/.management-toggle/.management-pager` 接线。
- **回归**：新增 T185（静态结构 + Node 纯函数：首次改字段方向、同字段保持、筛选计数）。
  全量 **176/176**（121.32 s），日志
  `_dev_runtime/diagnostics/trae-20261124g-author-toolbar-smoke.log`。
- **浏览器验收**：`_dev_runtime/diagnostics/check_author_toolbar_ui.py`（隔离库，60 位作者、
  2 页）在 1280 / 1024 / 390 / 320 px 验证两行、工具栏/表头排序同步（画师升序、作品数降序/升序、
  翻页后点表头回第 1 页）、状态+来源筛选（6 位、计数 2）、切换排序保留筛选、搜索 Enter 后
  切换排序保留关键词、清除筛选恢复 60 位、排序 Esc 焦点、简化菜单两次 Esc、窄屏更多/筛选弹层
  在视口内、无横向溢出；日志 `author-toolbar-ui.log`。
- **候选/发布**：候选包 `built_at 19:43:23`（前端 `20261124g`）在 7813 验证壁纸 Esc 与
  作者页排序同步。回滚清单
  `_dev_runtime/backups/publish-20261124g-20261124-194438.json`（5 个旧文件按 SHA-256
  去重，10 个替换项，新增备份 **15,156,530 字节**）；`dist` 与安装版已替换 `vixiq.exe`、
  `base_library.zip`、`app.js`、`index.html`、`style.css`，三处哈希一致；
  两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124g-1795520603-1795520603-1795519082`、
  `built_at 19:43:23`、`frozen=true`、健康正常；真实库 1605 作品 / 2108 图 / 433 作者，
  壁纸库 1180（790 原图 + 390 壁纸夹）。
- 下一步：操作进度第二、第三批；GitHub 未提交、未推送。

### 4.51 简化菜单 Esc 顺序（2026-11-24，前端 `20261124f`，已发布到安装版）

- **现象**：简化工具栏（`main.management-compact.management-peek`）里打开排序菜单后按一次 Esc，
  排序菜单关闭的同时简化工具栏也被关闭，焦点停在一个高度/透明度都为 0 的排序按钮上。
- **原因**：`wallpaper-admin.js` 的 Esc 处理器关闭下拉并恢复焦点后继续冒泡，命中
  `management-toolbar.js` 的 document keydown，再次 `peekClose()`。
- **修复**：壁纸下拉的 Esc 分支在关闭并恢复入口后调用 `e.stopPropagation()`，由下拉先消费
  第一次 Esc；第二次 Esc 才冒泡到共享控制器关闭简化菜单。`management-toolbar.js` 的
  peek Esc 分支改为**先** `peekBtn.focus()`（focus 事件可能重新打开浮层）再 `peekClose()`，
  焦点留在可见叠三角。
- **键盘验收**：新增 `_dev_runtime/diagnostics/check_wallpaper_toolbar_esc_ui.py`（隔离库，
  Selenium 真实键盘）。1024 px 简化菜单：第一次 Esc 只关排序、`management-peek` 保持、
  焦点回可见 `#wf-sort-btn`；第二次 Esc 关闭简化菜单、`management-compact` 保持、
  焦点回 `#mgmt-peek`。普通展开态 Esc 只关下拉、不收起工具栏；320 px 筛选 Esc 路径通过。
  日志 `wallpaper-toolbar-esc-ui.log`。
- **回归**：T184 结构与纯函数保持；全量 **175/175**（115.90 s），日志
  `_dev_runtime/diagnostics/trae-20261124f-wallpaper-esc-smoke.log`。
- **候选/发布**：候选包 `built_at 19:18:02`（前端 `20261124f`）在 7813 用 Selenium 复测
  普通 Esc 与简化菜单两次 Esc 通过。回滚清单
  `_dev_runtime/backups/publish-20261124f-20261124-191855.json`（5 个旧文件按 SHA-256
  去重，10 个替换项，新增备份 **14,432,114 字节**）；`dist` 与安装版已替换 `vixiq.exe`、
  `base_library.zip`、`index.html`、`management-toolbar.js`、`wallpaper-admin.js`，
  三处哈希一致；两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124f-1795513412-1795516743-1795519082`、
  `built_at 19:18:02`、`frozen=true`、健康正常；真实库 1605 作品 / 2108 图，
  壁纸库 1180（790 原图 + 390 壁纸夹）。
- 作者管理菜单仍未开始；GitHub 未提交、未推送。

### 4.50 壁纸管理菜单返修：筛选面板与焦点恢复（2026-11-24，前端 `20261124e`，已发布到安装版）

- **筛选按钮打不开**：`#wf-filter-btn` 的 `aria-controls`、`WF_MENUS`、触发映射和
  `#wf-filters` 的 CSS 选择器原来混用 `wf-filter-menu`，实际面板 ID 是 `wf-filters`，
  `wfToggleMenu` 查不到面板直接返回。四处统一为 `wf-filters`，桌面与 320 px 的鼠标点击/
  键盘 Enter 都能打开；`aria-expanded`、`aria-controls` 同步。
- **关闭下拉恢复焦点**：`_wfMenuTrigger` 原来在 `wfCloseMenus()` 里关闭后清空，Esc 再读已空。
  现在关闭前记录入口，Esc / 点击外部 / 切换另一个下拉 / 选择筛选或排序项都恢复入口焦点；
  外部点击只在点到空白或非交互区域时回入口，点到其他输入/按钮保留用户的新焦点；
  窄屏从「更多」打开已选操作后 Esc 回到可见的 `#wf-batch-btn`。
- **回归**：T184 增加 `aria-controls="wf-filters"` 与触发映射断言；全量 **175/175**
  （112.52 s），日志 `_dev_runtime/diagnostics/trae-20261124e-wallpaper-toolbar-recheck-smoke.log`。
- **真实浏览器逐条验收**：`check_wallpaper_toolbar_ui.py` 在 1024 / 320 px 用实际鼠标点击
  和键盘 Enter 打开筛选；选择已归属后叠加作品与分组（筛选数 3、结果 1 张）；「未分组」
  叠加为 0 张；清除筛选恢复 2 张并保留关键词/排序；排序菜单 Esc、外部点击、切换筛选的
  焦点与 `aria-expanded` 均正确；320 px 面板完整落在视口内并在面板内实际点选；窄屏更多
  打开已选操作后 Esc 回 `#wf-batch-btn`。日志 `wallpaper-toolbar-ui.log`。
- **候选/发布**：候选包 `built_at 18:39:03`（前端 `20261124e`）在 7813 用 Selenium 复测
  筛选打开与 Esc 焦点通过。回滚清单 `_dev_runtime/backups/publish-20261124e-20261124-183952.json`
  （5 个旧文件按 SHA-256 去重，10 个替换项，新增备份 **14,533,701 字节**）；
  `dist` 与安装版已替换 `vixiq.exe`、`base_library.zip`、`index.html`、`style.css`、
  `wallpaper-admin.js`，三处哈希一致；两处 `config/user.json` 未变。
- **安装版验证**：7810 前端 `20261124e-1795513412-1795516743-1795516743`、
  `built_at 18:39:03`、`frozen=true`、健康正常；页面 `aria-controls="wf-filters"`；
  真实库 1605 作品 / 2108 图，壁纸库 1180（790 原图 + 390 壁纸夹）。
- 作者管理菜单仍未开始；GitHub 未提交、未推送。

### 4.49 壁纸管理工具栏两行布局（2026-11-24，前端 `20261124d`，已发布到安装版）

- **桌面**：顶部保留总数与页码。第一行：搜索框（占剩余宽度、带清除）×、范围「壁纸夹 | 壁纸库」、
  排序按钮（完整显示「排序：字段 升序/降序 ▾」）、全选本页、收起菜单；第二行：筛选（全部 /
  已归属 / 未归属 / 重复图片 / 收藏）、作品、分组、清除筛选、扫描 ▾（扫描归属/深度扫描）、
  整理 ▾（清理副本/管理分组）。壁纸夹路径、扫描状态与统计移到工具栏下方信息区。
- **窄屏（≤980px）**：第一行搜索 + 更多 ▾；第二行范围下拉、排序（字段 + 箭头）、筛选 N ▾。
  更多菜单含扫描、整理、全选与收起；有选择时出现「操作（已选 N 张）」。筛选弹层内含状态、
  作品、分组与清除筛选。
- **行为**：范围默认壁纸夹；壁纸库 = 壁纸夹文件 + 直接引用原图。切范围保留关键词、排序和作品，
  清掉状态/分组筛选；壁纸夹空结果提供「到壁纸库搜索」。状态筛选只作用于壁纸夹文件。
  排序默认文件名升序；首次改选最近修改/文件大小为降序、文件名升序；菜单可直接切方向，
  文件名自然排序，最近修改/大小同值用文件名升序做稳定次级。排序不计入筛选数。
  勾选后第二行切换为「已选 N 张 / 取消选择 / 查看信息 / 操作 ▾」，操作菜单保留原有设置、
  归属、整理与危险删除分组；全选本页只选壁纸夹文件，图库原图仍为预览卡片。
  下拉菜单点外部或 Esc 关闭并回到入口，滚轮仍滚主列表（`.lib-menu` 已列入可滚动浮层）。
- **实现**：`index.html` 壁纸区块重构；`wallpaper-admin.js` 新增 `wallFilterEntries /
  wallSortEntries / wallNextSort / wallFilterCount` 与 `initWallpaperToolbarLayout`、
  `wallSyncScopeUI / wallSyncFiltersUI / wallSyncSortUI`；隐藏的 `#wf-sort` 保留为状态载体，
  旧 ID（`#wf-search`、`#wf-work-filter`、`#wf-group-filter`、`#wf-filters`、
  `#wf-extra-tools`、`#wf-scan-btn` 等）继续接线。`style.css` 新增壁纸工具栏两行/窄屏样式。
- **回归**：新增 T184（静态结构 + Node 纯函数：范围合并、状态/作品/分组筛选、筛选计数、
  自然排序、方向与稳定次级、首次改字段规则）。全量 **175/175**（109.76 s），日志
  `_dev_runtime/diagnostics/trae-20261124d-wallpaper-toolbar-smoke.log`。
- **浏览器验收**：`_dev_runtime/diagnostics/check_wallpaper_toolbar_ui.py`（隔离临时库）在
  1280 / 1024 / 390 / 320 px 检查两行排列、无横向溢出、缩小与更多菜单、范围/作品/排序叠加、
  全选仅壁纸夹、选中行切换、收起与叠三角、切页后搜索/排序/筛选保留、安全模式遮挡；
  日志 `wallpaper-toolbar-ui.log`。
- **发布（2026-11-24 17:46）**：候选包 `_dev_runtime/packages/vixiq`
  （`built_at 17:43:32`，前端 `20261124d`）在 7813 隔离数据通过版本/健康与工具栏结构验收。
  回滚清单 `_dev_runtime/backups/publish-20261124d-20261124-174615.json`
  （6 个旧文件按 SHA-256 去重，12 个替换项，新增备份 **15,175,080 字节**）。
  `dist/vixiq` 与 `D:\Program Files\Vixiq` 已替换 `vixiq.exe`、`base_library.zip`、
  `app.js`、`index.html`、`style.css`、`wallpaper-admin.js`，部署脚本逐项校验哈希；
  两处 `config/user.json` 未变。
- **安装版验证**：7810 `/api/version` 前端 `20261124d-1795513412-1795513412-1795513412`、
  `built_at 17:43:32`、`frozen=true`，健康正常；真实库 1605 作品 / 2108 图，
  壁纸库 1180（790 图库原图 + 390 壁纸夹），页面含新工具栏结构。
- 作者管理菜单尚未开始；GitHub 未提交、未推送。

### 4.48 备份进度返修 + 壁纸库安全分级 + R18 单侧补齐（2026-11-24，前端 `20261124c`，已发布到安装版）

- **备份进度四处返修**（复验单 `trae-backup-progress-review-20261124.md`）：
  恢复失败按服务端最后真实阶段冻结，失败段静态红色、后续待执行，回滚独立动画结束即停；
  运行中任务不再被 TTL 回收（只回收已结束任务、从 `finished_at` 起算），前端轮询瞬断
  退避重试，长期不可查进入「状态未知」并保留任务身份；另存发布改为硬链接/rename
  原子无覆盖并递增后缀；另存字节条复用同一个 `.op-fill` 只更新宽度/文本。
- **壁纸库原图安全分级**（复验单第 1 节）：`_wall_library_items()` 不再按历史
  `wallpaper_usage.artwork_id` 分级，改用当前 `pages.local_path` 的有效登记
  （页未回收、作品未失效），Windows 路径反斜杠/大小写归一；无登记、多页冲突、
  分级为空一律 `None`，安全模式下缩略图与大图预览都不请求真实图片。
- **R18 历史标签单侧补齐**（复验单第 2 节）：`sync_r18_tag()` 分别检查 `tags_json`
  与关联表，只补缺失的一侧，已有 R-18/R-18G 写法沿用；两侧齐全时不写库。
- **回归**：新增 T183（壁纸库分级 8 种状态 + Node 真实卡片/预览遮挡）；T180 扩展
  「仅 JSON / 仅关联表 / 两侧都有」三种历史状态与按标签查询。全量 **174/174**
  （104.51 s），日志 `_dev_runtime/diagnostics/trae-20261124c-walllib-r18-smoke-20261124.log`。
- **候选验收（2026-11-24 16:56 打包）**：`_dev_runtime/packages/vixiq` 在 7813 隔离数据
  通过版本/健康、壁纸库分级（有效普通 0 / 旧记录指向现 R18 为 1 / 无当前页为未知）、
  R18 单侧补齐与二次保存不重复；脚本 `check_20261124c_candidate.py`。打包为本地自用模式
  （含 `to-top*` 立绘，不可公开分发）。
- **发布**：回滚清单 `_dev_runtime/backups/publish-20261124c-20261124-165746.json`
  （5 个旧文件按 SHA-256 去重，10 个替换项；新增备份 **15,135,881 字节**）。
  `dist/vixiq` 与 `D:\Program Files\Vixiq` 已替换 `vixiq.exe`、`base_library.zip`、
  `app.js`、`index.html`、`style.css`，部署脚本逐项哈希校验通过；两处 `config/user.json`
  哈希在发布前后未变（dist `da8a9c…`，安装版 `388e90…`；后者与上次发布记录不同是用户
  之后改过配置，本次未触碰）。
- **安装版验证**：7810 `/api/version` 前端 `20261124c-1795510585-1795510585-1795462925`、
  `built_at 2026-11-24 16:56:25`、`frozen=true`，`/api/health` ok；真实库 1605 作品 /
  2108 图、`schema_migrations` 至 031、R18 作品 287；壁纸库 790 条图库原图
  （716 普通 / 36 R18 / 38 未知）+ 390 壁纸夹 = 1180，与只读元数据核对一致。
- GitHub 未提交、未推送。

### 4.47 R18 字段同步标签与壁纸库原图（2026-11-24，前端 `20261124b`，已随安装版发布）

- **R18 字段 → 标签（单向，字段为准）**：`PixivDatabase.sync_r18_tag` 在
  `change_work_status` / `update_artwork`（含批量编辑）以及合并/拆分新建作品后执行：
  is_r18=1 且没有 R18/R-18/R-18G 任一标签时补 `R18`；is_r18=0 时移除这些标签；
  其他标签与 tags_json 原样保留；标签不反向改字段。`backfill_r18_tags()` 幂等补齐历史。
- **真实库补齐（2026-11-24 03:50）**：19 个 is_r18=1 但缺标签的作品（18 个原「涩图/R18」
  目录 + 新增 miku）已补 `R18`，复查缺 0、R18 关联 20；补齐前备份
  `F:\Vixiq\data\backups\r18-tag-sync-20261124-035018.db`（2,994,176 字节）。
  脚本 `_dev_runtime/diagnostics/backfill_r18_tags_20261124.py`。
- **壁纸库纳入图库原图**：`/api/wallpaper/files` 新增 `library_items`（直接引用原图的
  设置记录，按路径去重，可预览项限图库根目录内），`stats.library_refs/library_total`
  沿用「壁纸夹 + 原图引用」口径；壁纸夹未指定时也返回原图。
  前端新增「壁纸库」筛选（`data-f="library"`），并集展示壁纸夹文件与图库原图，
  原图卡片标「图库原图」、只提供预览；搜索/分页/作品筛选覆盖两者。
  排序控件 `#wf-sort` 从 extras 抽屉移到常驻筛选行（不再需要展开菜单）。
- **验收**：T180（R18 字段同步/去重/移除/backfill 幂等/编辑接口）、T181（原图列表、
  壁纸夹缺失、统计、静态接线）；全量 **172/172**（108.22 s），日志
  `_dev_runtime/diagnostics/trae-r18-walllib-smoke-20261124.log`。
  安装版实测：`/api/wallpaper/files` 返回 390 壁纸夹 + 790 图库原图（壁纸库 1180）、
  页面含「壁纸库」筛选且排序在筛选行内、`/api/library/items` 已能看到补上的 `R18` 标签。
- **状态**：两项都已包含在安装版 `20261124b`（`built_at 03:42:05`）；本轮未重新打包/部署。
  源码未提交、GitHub 未推送。

### 4.46 备份与恢复操作进度（第一批，2026-11-24，前端 `20261124b`，已发布到安装版）

- **依据**：`_dev_runtime/diagnostics/vixiq-operation-progress-design-20261124.md` 第一批，
  只改备份中心；流动/计数/分段充能三种条，300 ms 出现阈值，禁止按计时器估算百分比。
- **服务端任务**：`POST /api/library/db-backup/job` 启动内存任务，
  `GET /api/library/db-backup/job/<id>` 轮询。真实阶段：立即备份
  `生成数据库副本 → 压缩备份 → 校验并保存`；对比 `校验备份 → 核对差异`；恢复
  `校验备份 → 等待图库写入结束 → 保存恢复前快照 → 恢复并检查`；另存
  `正在复制备份（字节数）→ 正在校验副本`；旧图 `保存当前版本 → 切换所选旧图`。
  `backup_to_zip` / `preview_backup` / `restore_from_zip` 新增 `stage_cb`；
  `BackupRestoreError` 带 `rolled_back` / `rollback_error`。任务完成才 `mark_dirty`，
  任务启动/轮询本身不登记数据变化。
- **前端**：`bkProgressHTML` / `bkRenderProgress` 统一画三种条；备份按钮先变
  「备份中…」并禁用、旧图先变「切换中…」；对比弹窗先保留所选文件名和流动条；
  恢复 4 段条放在确认按钮上方，成功/回滚后提供「定位恢复前快照」；关闭弹窗任务继续，
  重开备份中心能看到上次恢复结果。`backups` 数据域只在备份中心打开且
  `_backupView.mode==='center'` 时原位刷新，不会覆盖差异预览/恢复视图。
- **验收**：T179（隔离临时库）覆盖五类任务阶段、确认失效、迁移失败 `rolled_back`；
  Selenium 无头（隔离临时库 + 1.2~1.4 s 人为延时）验证按钮即时变化、备份充能停留在
  压缩段、恢复成功/等待写入/异常回滚三条路径、390 px 窄屏无横向溢出、减少动态效果停用
  循环光带、备份中关闭弹窗再重开仍显示状态、对比期间另一次备份完成不覆盖预览、旧图切换提示。
  全量 **172/172**（118.59 s，2026-11-24 03:38 复跑；同工作区并行改动 T180/T181 一并纳管）。
  证据：`_dev_runtime/diagnostics/trae-20261124b-deploy-smoke.log`（172/172）、
  `backup-progress-ui-success.log`、`backup-progress-ui-rollback.log`，
  脚本 `check_backup_progress_ui.py`。
- **部署（2026-11-24 03:43）**：候选包 `_dev_runtime/packages/vixiq`
  （`built_at 03:42:05`，前端 `20261124b`）在 7813 隔离数据验收：版本/健康、
  备份任务 3 段、壁纸库 `library_items` 正常。回滚清单
  `_dev_runtime/backups/publish-20261124b-20261124-034310.json`（6 个旧文件按 SHA-256
  去重，新增备份 15,124,194 字节）。`dist/vixiq` 与 `D:\Program Files\Vixiq` 的
  `vixiq.exe` / `base_library.zip` / `app.js` / `style.css` / `index.html` /
  `wallpaper-admin.js` 哈希一致，两处 `config/user.json` 哈希未变；
  安装版 7810 健康、`/api/version` 前端 `20261124b`、`built_at 03:42:05`；
  真实库 1605 作品 / 2193 图、`schema_migrations` 至 031，壁纸库
  390 夹内文件 + 790 图库原图引用。GitHub 未提交。

### 4.45 第 6 节 备份与恢复（2026-11-23，前端 `20261123e`，已发布到安装版）

- **一个入口**：`💾 备份与恢复`，上半「图库数据库备份」、下半「原图旧版本」。
- **数据库备份**：列表显示时间/大小/保存位置/覆盖范围；覆盖范围一律用备份库内**实际计数**，
  清单 `backup_manifest.json` 只作历史记录（`manifest_matches=false` 时提示以库内实数为准）；
  `/api/library/db-backup/save-as` 可另存到任意绝对路径文件夹（不覆盖同名文件）。
- **清单结构校验**：`validate_backup_manifest` 要求对象、`created_at` 非空字符串、
  `artworks/pages_done/authors` 为非负整数；数组、缺字段、字符串/布尔计数一律拒绝，
  合法历史清单（多字段、缺 `db_path`）继续兼容。
- **唯一备份**：`backup_to_zip` 用 `_BACKUP_CREATE_LOCK` + `touch(exist_ok=False)` 先占唯一
  文件名（同秒追加 `_2/_3`），写同目录独立临时 zip，完整校验后 `os.replace` 发布；
  失败清理占位与半成品。恢复前快照同样走这条路径。
- **差异预览与确认令牌**：`compare_backup` 除作者/作品增减外，还返回
  `pages_added_in_backup / pages_removed_by_restore / pages_changed / artworks_changed /
  authors_changed / tags_*` 与 `other_tables`；`will_overwrite_current()` 覆盖同 ID 变更。
  `preview_backup` 签发 `preview_token = sha256(备份文件SHA256 + 当前库逐表指纹)`；
  `/restore` 必须带令牌，恢复前重新计算，预览后新增作品、改记录、替换同名 ZIP 都会
  `409 确认已失效`，原库与备份不动。
- **私有暂存与确认绑定**：`preview_backup`/`restore_from_zip` 都先把所选 ZIP 复制到
  `data/backup/.preview_*` / `.restore_*` 私有临时目录，`_inspect_backup_file` 在解出的
  `pixiv.db` 上做清单/完整性检查；令牌、差异计算与最终切换全部使用这份副本，
  **不再按原路径重开 ZIP**。确认前替换同名 ZIP → `409 确认已失效`；恢复进行中替换或删除
  ZIP 不影响已校验内容，最终库/响应/审计都是同一份备份；临时目录成功失败均清理。
- **恢复互斥（应用内 + 外部 SQLite）**：`PixivDatabase` 连接生命周期读写锁 `_DB_RW_LOCK`
  阻拦本进程其他实例；恢复还会关闭自身连接，用 `_open_exclusive_guard()`
  （`PRAGMA locking_mode=EXCLUSIVE` + `BEGIN IMMEDIATE/ROLLBACK`）取得 SQLite 文件级独占锁，
  覆盖「快照 → 用 `Connection.backup()` 把暂存库写入当前库 → 迁移 → 回滚」全程。
  外部普通 `sqlite3.connect()` 的写入会被 busy 等待或在超时后明确拒绝，已提交的数据不会
  落在被覆盖的旧库之外；拿不到独占锁返回「获取独占写入」并中止，当前库不变。
  失败回滚同样用快照库经 backup API 写回，并做 `integrity_check`。
  后台下载/导入/同步/另一条恢复仍走 `_DB_RW_LOCK`；**只关 `db.conn` 不会释放读锁，
  必须调 `db.close()`**（`core/crawler.py` 已改）。
- **原图旧版本**：以图库 `pages` 的作品/页号找**当前登记文件**（排除回收站），
  只有「唯一登记 + 同格式」才先把当前文件存成 `.bak-<时间>` 再换入所选旧版本；
  跨格式、无登记、文件名认不出作品页号一律 409 并保持磁盘/登记原样，不额外生成同页文件；
  格式更换仍走既有 PNG/JPG 转换入口。切换写失败时当前图字节不变，已建快照可在旧版本区找到。
- **验收**：T178（独立临时库）覆盖：同 ID 页/作品/标签差异；令牌三种失效（新增作品、
  改记录、替换同名 ZIP）；清单数组/缺字段/类型错误/计数不一致/合法历史清单；
  同秒连续与并发备份唯一且各自可恢复；备份写入失败清理；快照到切换之间应用内第二连接
  写入被推迟、恢复后可见；两条恢复并发一成一拒；迁移失败自动回滚；`.bak` 同格式切换/切回、
  写失败保图与快照、跨格式/无登记拒绝；确认前/恢复中替换或删除 ZIP 仍用已校验 A 并保持
  响应一致；独立 Python 进程 `sqlite3.connect()` 的 50ms 写入被明确拒绝、10s 写入被推迟到
  恢复结束后成功且快照保留旧标题。全量 **169/169**（103.80s），日志
  `_dev_runtime/diagnostics/trae-item6-backup-restore-smoke-20261123.log`。
- **部署（2026-11-23 23:14）**：候选包 `_dev_runtime/packages/vixiq`
  （`built_at 23:11:49`，前端 `20261123e`）在 7813 隔离数据验收：备份/对比/令牌失效/
  确认恢复/快照校验通过、页面版本与入口正确。回滚清单
  `_dev_runtime/backups/publish-20261123e-20261123-231409.json`（4 个旧文件按 SHA-256
  去重，新增备份 14,951,264 字节；单目标净增 29,333 字节）。
  `dist/vixiq` 与 `D:\Program Files\Vixiq` 的 `vixiq.exe` / `base_library.zip` /
  `app.js` / `index.html` 哈希一致，两处 `config/user.json` 哈希未变；
  安装版 7810 健康、`/api/version` 前端 `20261123e`、`built_at 23:11:49`；
  真实库 1605 作品 / 2193 图、`schema_migrations` 至 031，备份列表能读取
  `pixiv_backup_2026-11-20_19-26-11.zip`（valid）。GitHub 已提交并推送 `0dcfbcb`。

### 4.44 第 5 项 体检：检查与修复分开（2026-11-23，前端 `20261123d`，已发布到安装版）

- **只读检查全部**：新增 `inspect` 步骤与 `/api/library/check {"inspect": true}`：
  `reconcile_output(write=False)` 只扫描不回填/不重指/不补页，再做待补资料统计与
  `_tidy_build_plans` 只读预览；不联网、不写库、不动文件。旧
  `/api/library/self-check` 同步迁移为只读检查（兼容入口保留）。
- **四组报告**：报告按「缺失 / 损坏 / 待补资料 / 待整理目录」展示；新增条目类型
  `sync_pending`（磁盘新增/移动未写索引）、`metadata_missing`（缺标题/标签/封面等）、
  `tidy_plan`（移动/改名/归类/空夹计划，仅摘要不执行）。`suspicious/todos` 两桶
  仍保留（旧接口/落盘格式兼容），前端按类型映射到四组。
- **修复入口**：缺失 → 同步结果 / 缺失清理；损坏 → 重下修复；待补资料 → 补全资料
  （`/api/library/complement`）或逐条指定作者/反搜；待整理目录 → 整理预览。检查本身
  不触发这些动作。
- **统一状态**：`_ck_status()` 输出 `never/running/ok/issues/incomplete` +
  最近成功完成时间，图库健康条、设置页、体检面板共用同一份口径；点状态或「查看报告」
  打开同一份报告。设置页主按钮改为「🔍 检查全部」。
- **返修（2026-11-23，前端 `20261123d`）**：
  - **首次失败**：`last_run.ok=false` 或任务记录为 `interrupted` 时优先显示
    「检查未完成」，不再被「无报告」判成「尚未检查」；首次失败没有上次完成时间，
    成功后失败则保留最近一次成功完成时间，当前失败时间与原因另列。
  - **范围不完整**：`reconcile_output` 的 `scan_incomplete/unreadable_dirs` 进入状态与
    报告（新增 `scan_incomplete` 条目）；磁盘相关类型不判「已解决」，未扫范围保留原登记；
    恢复完整扫描后条目自动消失、状态回到正常。
  - **待补资料总数**：`count_works_missing_metadata()` 与列表同口径算完整总数；报告不再
    截断到 500，新增 `/api/library/check-items?type&offset&limit` 分页，末尾条目可查看。
  - **任务收尾**：`running` 改为等任务记录写完再清，避免前后两个体检线程抢写 journal。
  - **复验返修（2026-11-23）**：
    - 根目录不可用时，`last_run` 记录本次模式/`root_available`/扫描范围与原始失败原因，
      状态不再默认 `root_available=true`、`mode=""`；报告阶段直接跳过（`skip_report`），
      不再读取不存在的目录、不把第二条路径错误混进原因，上一份报告原样保留。
    - `self_check.json` 增存 `last_run`/`last_success`/`root_available`；重启后
      `check-status` 先从盘恢复，仍显示「检查未完成」、相同原因/未读范围与上次完成时间；
      后续完整成功会清除未完成状态；旧报告照常打开。
    - T177 等待条件改为「`last_run` 已更新 **且** `running` 已清空」，避免并行断言撞上
      `running=report`。
- **验收**：T176（只读、四组、状态、修复入口、旧入口）+ T177（首次失败、范围不完整
  含无/有报告与恢复、重启恢复状态、503 项总数与末尾分页、检查前后库与图片一致）；
  全量 **168/168**。
- **部署（2026-11-23 13:13）**：候选包 `_dev_runtime/packages/vixiq`
  （`built_at 13:09:19`，前端 `20261123d`）先在 7813 隔离数据验收：首次检查正常、
  根目录不可用显示未完成且保留上一份报告、候选重启后未完成状态/原因/上次完成时间仍在、
  恢复目录后完整成功清除未完成。回滚清单
  `_dev_runtime/backups/publish-20261123d-20261123-131323.json`（5 个旧文件按 SHA-256
  去重，新增备份 14,952,304 字节）。`dist/vixiq` 与 `D:\Program Files\Vixiq` 的 exe /
  app.js / index.html / base_library.zip / api-ms-win-core-synch DLL 哈希一致，两处
  `config/user.json` 哈希未变；安装版 7810 健康、`/api/version` 前端 `20261123d`、
  `built_at 13:09:19`；真实库只读体检「未发现异常」（0 项），业务库文件大小/修改时间
  未变。GitHub 未推送。

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
