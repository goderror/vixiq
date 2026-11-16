# AGENTS.md · 接手须知

> 给后续接手本项目的 Codex / 其他代理看。最后更新 2026-11-16，当前 HEAD `e3f1ea4`。
> 产品面向普通用户的说明在 [README.md](README.md)，历史排障在 [DEBUG_HISTORY.md](DEBUG_HISTORY.md)，
> 表结构在 [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)，版本记录在 [CHANGELOG.md](CHANGELOG.md)。

---

## 0. 先读这三条

1. **一次只改一项**。用户明确要求「我一项项跟你说」，不要一次实现多个需求。
   上一轮一口气改了 7 项，被整体回退（见 §4）。有歧义先问，不要自行扩大范围。
2. **改完必须验证**：`py_compile` + 全量冒烟（当前 130 用例，必须全过）+ 实际部署到安装版。
3. **不碰安全红线**（§7）：绝不自动删文件、R18 不加载真实像素、不 force push。

---

## 1. 项目与环境（本机）

- 工作区：`d:\__workspace__\项目\vixiq-dev`
- 解释器：`.venv\Scripts\python.exe`（Python 3.14）
- 技术栈：Flask + 原生 JS（`web/static`，无构建步骤）+ SQLite + Selenium（Edge）
- 本机配置 `config/user.json`：
  - 端口 **7810**
  - 数据目录 `F:\pixiv-toolkit-data`（图库 `img/`、SQLite、日志、cookies）
  - 临时目录 `F:\pixiv-toolkit-temp`
  - 壁纸夹 `C:\Users\Administrator\Documents\【壁纸】`
  - 安全模式 **已开启**
- 安装版（用户日常使用）：`D:\Program Files\Vixiq\vixiq.exe`，数据同 `F:\pixiv-toolkit-data`
- 源码运行：`.venv\Scripts\python.exe main.py`（本机同样读上面的 user.json，端口 7810）

### 关键目录

| 路径 | 作用 |
|---|---|
| `main.py` | 启动器：Flask 服务、托盘、桌面小控件、心跳守护、孤儿进程清理 |
| `web/server.py` | 全部 HTTP 接口（约 1 万行，按功能分块注释） |
| `web/static/{index.html,app.js,style.css}` | 前端全部代码；无打包，改完升版本串即可 |
| `core/wallpaper_trace.py` | 壁纸溯源（只读）：当前壁纸、上一张/下一张、MD5 索引 |
| `core/wallpaper_index.py` | 壁纸夹归属索引（`wallpaper_files` 表）：扫描、列表、重复组 |
| `core/wallpaper_lib.py` | 从图库按尺寸/比例生成壁纸 |
| `db/store.py` | 数据访问层；`db/migrations/NNN_*.sql` 迁移 |
| `utils/proc.py` | **所有子进程统一入口**（隐藏控制台窗口），见 §6.1 |
| `autotest/smoke_all.py` | 全量冒烟，130 个用例；新增功能必须加断言 |

---

## 2. 运行、验证、打包、部署

### 2.1 验证（每次改完都要做）

```powershell
.venv\Scripts\python.exe -m py_compile web\server.py core\wallpaper_index.py
.venv\Scripts\python.exe -m autotest.smoke_all        # 130 用例必须全过
```

- 冒烟无参数、跑全量约 50 秒；输出报告在 `autotest/SMOKE_REPORT.md`。
- 只想快速验证某个用例，可直接 import 调用：

```powershell
.venv\Scripts\python.exe -c "from autotest.smoke_all import t137_wallpaper_index_and_admin_page as t; t(); print('OK')"
```

- 改了前端（`web/static/*`）**必须升版本串**（§3.1），否则冒烟会失败。

### 2.2 打包与部署到安装版

```powershell
# 1) 先让安装版退出（页面全关后约 90 秒也会自动退，但显式关更稳）
Invoke-WebRequest -Uri 'http://127.0.0.1:7810/api/app/quit' -Method Post

# 2) 全量打包（改了 .py / resources / build.spec 必须用 -Full；只改前端可用 -Fast）
.\rebuild.ps1 -Full -NoLaunch

# 3) 替换安装版（旧 _internal 留作 _internal_old，下轮升级时删）
$dst='D:\Program Files\Vixiq'
Remove-Item "$dst\_internal_old" -Recurse -Force -ErrorAction SilentlyContinue
Move-Item "$dst\_internal" "$dst\_internal_old"
Copy-Item 'dist\vixiq\_internal' "$dst\_internal" -Recurse
Copy-Item 'dist\vixiq\vixiq.exe' "$dst\vixiq.exe" -Force

# 4) 启动并验证
Start-Process "$dst\vixiq.exe"
```

- 只改前端时：`.\rebuild.ps1 -Fast -NoLaunch` 同步 `dist`，再把 `web/static/{app.js,style.css,index.html}`
  复制到 `D:\Program Files\Vixiq\_internal\web\static\`，浏览器 Ctrl+F5 即可，**不用重启**。
- 打包产物在 `dist\vixiq\`；`-Release` 会剔除版权立绘，**本地自用版不要加**。
- 校验三处一致性（源码 / dist / 安装版）：

```powershell
Get-FileHash web\static\app.js, dist\vixiq\_internal\web\static\app.js, 'D:\Program Files\Vixiq\_internal\web\static\app.js' -Algorithm MD5
```

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
改前端后一起升（当前 `20261116c`，格式 `yyyyMMdd+字母`）。漏一处冒烟必失败。

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

### 4.1 已回退

用户说「一口气说太多你做不好」，要求在「还得改进」之前重来。
`dab2e19` 已用 `git revert` 撤销了下面两个提交（**代码状态 = `f52054b`**）：

- `067a631` 壁纸查重改同图口径 + 壁纸管理界面完善 + 图库工具栏分组/顶部固定
- `3e5e453` 上述改动的 bug 修复

被撤销的需求清单（用户后续会**一项项**重新提）：

1. 重复检测口径：同作品不同页不算重复，只算「同作品同页」或「md5 相同」
2. 设壁纸查重提示里提供「选择已有壁纸作为当前壁纸」（缩略图选择弹窗）
3. 壁纸管理界面完善（搜索/排序/单张操作：设为壁纸、在库中查看、删除）
4. 图库工具栏按性质分组、去掉「批量」、下载+修复合并为「重新下载 ▾（补完作品/修复原图）」
5. 分页栏显示「共 X 个作品 · Y 张图片」，原位置显示「已选 X 个作品 · Y 张图片」
6. 三个管理页顶部菜单固定 + 下滑收起（注意 §6.2 的闪烁坑）
7. 导航顺序：图库 → 壁纸 → 作者（当前是 图库 → 作者 → 壁纸）

回退前的未提交改动与新测试已备份在 `build\revert_backup_20261116\`（`working.diff` +
`test_wallpaper_management.py`），可作参考，**不要直接套用**。

### 4.2 当前保留的功能

- 壁纸管理页（`cdc01d0` 引入）：tab 在「作者管理」之后、「统计」之前
  - 归属索引表 `wallpaper_files`（迁移 `021_wallpaper_files.sql`）
  - 快扫（文件名作品 ID / MD5）+ 深度扫描（ORB，复用 `_wall_orb_candidates` 单例）
  - 重复组按 **artwork_id** 聚合（这就是用户认为不准的地方，见 §4.1 第 1 条）
  - 删除只勾选、只进 Windows 回收站
- 大图查看器「设为壁纸」是左下角圆形图标按钮，点击先关闭查看器再进裁剪
- 导航（从左到右）：图库管理 → 作者管理 → 壁纸管理 → 统计 → 设置 → 回收站（最右角落，仅图标）
- 子进程全部隐藏控制台窗口（`e3f1ea4`）

### 4.3 壁纸相关接口速查

| 接口 | 说明 |
|---|---|
| `GET /api/wallpaper/files` | 归属清单 + 统计 + 重复组（读 `wallpaper_files`） |
| `POST /api/wallpaper/files/scan` | 后台扫描，body `{deep: bool}`；进度看 `GET .../scan/status` |
| `POST /api/wallpaper/files/recycle` | 勾选文件进回收站，只收壁纸夹内相对名 |
| `GET /api/wallpaper/dupes/check?artwork_id=&page_num=` | 设为壁纸前的查重（当前按作品匹配） |
| `POST /api/wallpaper/apply` | 从图库页设壁纸，支持 `crop` 与 `force` |
| `POST /api/wallpaper/use` | **当前不存在**（被回退）；重新实现时用于「用已有壁纸」 |
| `GET /api/wallpaper/current` | 当前桌面壁纸溯源（只读） |

数据流：扫描 → 写 `wallpaper_files` → 列表/查重都读这张表 → 秒回。
`match_kind`：`md5` / `name` / `visual` / `manual`；`page_num` 可能为 NULL（文件名无页号）。

---

## 5. 用户的工作方式

- 一次一项，做完 → 验证 → 部署 → 汇报，再等下一项。
- 汇报用大白话，不堆术语；说清楚改了什么、验证了什么、还有哪些没做。
- 用户很在意「四处同步」：本地源码、`dist` 打包版、`D:\Program Files\Vixiq` 安装版、GitHub 仓库。
  每次汇报都说明这四处状态。
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

壁纸夹 1000+ 张，一次性塞 DOM 会明显卡顿。分批渲染（每批 ~120）+ `IntersectionObserver`
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
  「避免违规图片」。需要验证 UI 时优先用接口 + 静态断言。
- 检查 R18 相关内容时，先把安全模式确认开启（`GET /api/safemode`）。

### 6.8 其他

- `_wall_dupes_for` 已改为优先读 `wallpaper_files` 索引，整表为空才退回现场反推；
  索引非空时不要再现场跑 ORB（很慢）。
- SQLite 连接绑定线程：后台线程必须用 `db_factory()` 新建连接，不要跨线程复用。
- 文件删除永远走 `_recycle_files`（Windows 回收站），不要 `os.remove` / `unlink`。

---

## 7. 安全红线（无条件遵守）

1. **绝不自动删除壁纸/图片**。删除必须用户勾选确认，且只进 Windows 回收站（可还原）。
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
