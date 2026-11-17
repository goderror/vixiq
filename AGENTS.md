# AGENTS.md · 接手须知

> 给后续接手本项目的 Codex / 其他代理看。最后更新 2026-11-18（当前前端版本 `20261118j`）。
> 产品面向普通用户的说明在 [README.md](README.md)，历史排障在 [DEBUG_HISTORY.md](DEBUG_HISTORY.md)，
> 表结构在 [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)，版本记录在 [CHANGELOG.md](CHANGELOG.md)。思考要用中文。

---

## 0. 先读这三条

1. **一次只改一项**。用户明确要求「我一项项跟你说」，不要一次实现多个需求。
   上一轮一口气改了 7 项，被整体回退（见 §4.1）。有歧义先问，不要自行扩大范围。
2. **改完必须验证**：`py_compile` + 全量冒烟（当前 146 用例，必须全过，2026-11-18 已 146/146 通过），同步相应程序/资源并验收安装版；仅更新交接文档按 §2.2 核对，不重启或部署程序。
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
| `autotest/smoke_all.py` | 全量冒烟，146 个用例；新增功能必须加断言 |

---

## 2. 运行、验证、打包、部署

### 2.1 验证（每次改完都要做）

```powershell
.venv\Scripts\python.exe -m py_compile web\server.py core\wallpaper_index.py
.venv\Scripts\python.exe -m autotest.smoke_all        # 146 用例必须全过
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
改前端后一起升（当前源码 `20261118j`，格式 `yyyyMMdd+字母`）。漏一处冒烟必失败。
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
- A2 待做：`涩图`→`R18`、`X图`→`普通` 目录改名；按 R18 规则重排 82+37 张；
  分类管理 UI（改名/规则编辑）与图库分类筛选。

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
