# rebuild.ps1 — Vixiq 打包版一键同步 / 重建
#
# 用法 (双击根目录的 rebuild.bat 效果相同):
#   .\rebuild.ps1             自动判断: 只改了前端(web/static) → 秒级快同步
#                                        改了 .py / resources / build.spec → 全量重打包
#   .\rebuild.ps1 -Fast       强制快同步 (只把 web/static 拷进 dist)
#   .\rebuild.ps1 -Full       强制全量重打包
#   .\rebuild.ps1 -Release    发布模式 (排除版权立绘 to-top*.png, 产物可公开分发)
#   .\rebuild.ps1 -NoLaunch   重建完不自动启动 exe
#
# ⚠ 别把它的输出接进管道截断 (例如 `| Select-Object -First 20`): 管道一关, 脚本会被
#   直接掐死在半路 (可能正好停在"dist 已删、配置还没还原"那一步)。要看输出就整段跑完。
# ⚠ 本文件必须以 **UTF-8 with BOM** 保存, 否则 Windows PowerShell 5.1 会按 GBK 读,
#   中文注释/字符串会乱码到语法报错。改完记得确认首三字节是 EF BB BF。
#
# 为什么需要它: PyInstaller 打包版是把代码/资源**拷进** dist\vixiq\ 之后才生效的,
# 源码改了 exe 不会自己变 —— 于是"我测的是源码版、你测的是 exe"就会出现两边不一致。
# 全量重打包要完整跑一遍 PyInstaller (较慢); 而绝大多数前端改动只涉及 web/static
# 下那几个文件, 那种情况直接把文件拷进 dist\vixiq\_internal\web\static\ 即可, 1 秒完成。

param(
    [switch]$Fast,
    [switch]$Full,
    [switch]$Release,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

$Exe       = Join-Path $Root 'dist\vixiq\vixiq.exe'
$DistDir   = Join-Path $Root 'dist\vixiq'
$SrcStatic = Join-Path $Root 'web\static'
$DstStatic = Join-Path $DistDir '_internal\web\static'
$CfgDst    = Join-Path $DistDir 'config\user.json'
$CfgSrc    = Join-Path $Root 'config\user.json'
$CfgBak    = Join-Path $Root 'build\_dist_user_config.json'
$Python    = Join-Path $Root '.venv\Scripts\python.exe'

$isRelease = $Release -or ($env:VIXIQ_RELEASE -eq '1')

function Write-Step($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  警告 $msg" -ForegroundColor Yellow }

function Test-VixiqRunning { return [bool](Get-Process -Name 'vixiq' -ErrorAction SilentlyContinue) }

# exe 已在运行就别再 Start-Process 了: 它有单实例互斥锁, 新进程会立刻自己退出
# (只发个 /api/focus 去激活已有窗口), 白弹一个 splash。静态文件是每次请求现读磁盘的,
# 所以快同步后**不需要**重启, 浏览器 Ctrl+F5 就能看到新前端。
function Start-VixiqExe {
    if ($NoLaunch) { Write-Host "      (-NoLaunch, 跳过启动)"; return }
    if (Test-VixiqRunning) {
        Write-Host "      exe 已在运行 → 前端改动已生效, 浏览器按 Ctrl+F5 刷新即可 (无需重启)" -ForegroundColor Yellow
        return
    }
    Start-Process -FilePath $Exe
    Write-Ok "已启动 $Exe"
}

# ── 0. 自愈: 补回被中断的构建弄丢的配置 ───────────────────────────────────────
# --noconfirm 会先删掉整个 dist, 配置是"构建后才还原"的 —— 万一脚本在中间被中断
# (最典型: 输出被 `| Select-Object -First N` 截断, 管道关闭会把脚本一起掐死),
# dist\config\user.json 就永久丢了 → exe 静默回落端口 7790 + dist 内空库。
# 每次启动先自愈一次, 重跑即恢复。
if (-not (Test-Path $CfgDst)) {
    $cands0 = @()
    if (Test-Path $CfgBak) { $cands0 += Get-Item $CfgBak }
    if (Test-Path $CfgSrc) { $cands0 += Get-Item $CfgSrc }
    if ($cands0.Count -gt 0) {
        $cfgDir = Split-Path -Parent $CfgDst
        if (-not (Test-Path $cfgDir)) { New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null }
        $pick0 = $cands0 | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        Copy-Item $pick0.FullName $CfgDst -Force
        Write-Host "[自愈] dist 里缺 config\user.json, 已从 $($pick0.FullName) 补回" -ForegroundColor Yellow
    }
}

# ── 1. 判断要不要全量重打包 ────────────────────────────────────────────────────
# 触发全量重打包 = 这些"进包"的东西里有比 exe 更新的:
#   main.py / build.spec / requirements*.txt / core|db|utils|resources 全部 / web 下的 .py
# **刻意排除 web\static** —— 那部分走快同步, 否则改个 css 也要重打包。
function Get-PackStamp {
    $t = [datetime]::MinValue
    $files = @('main.py', 'build.spec', 'requirements.txt', 'requirements-dev.txt')
    foreach ($f in $files) {
        $p = Join-Path $Root $f
        if (Test-Path $p) { $x = (Get-Item $p).LastWriteTime; if ($x -gt $t) { $t = $x } }
    }
    foreach ($d in @('core', 'db', 'utils', 'resources')) {
        $p = Join-Path $Root $d
        if (Test-Path $p) {
            $x = Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
                 Where-Object { $_.FullName -notmatch '__pycache__' } |
                 Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($x -and $x.LastWriteTime -gt $t) { $t = $x.LastWriteTime }
        }
    }
    $p = Join-Path $Root 'web'
    if (Test-Path $p) {
        $x = Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -notmatch '__pycache__' -and $_.FullName -notmatch '\\web\\static\\' } |
             Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($x -and $x.LastWriteTime -gt $t) { $t = $x.LastWriteTime }
    }
    return $t
}

$needFull = $false
$why = ''
if ($isRelease) {
    # 发布模式必须全量: 快同步不负责"从产物里剔除"版权立绘
    $needFull = $true; $why = '发布模式'
} elseif ($Full) {
    $needFull = $true; $why = '指定 -Full'
} elseif (-not (Test-Path $Exe)) {
    $needFull = $true; $why = 'dist 里还没有 exe'
} else {
    $stamp = Get-PackStamp
    $exeTime = (Get-Item $Exe).LastWriteTime
    if ($stamp -gt $exeTime) {
        $needFull = $true
        $why = "源码/资源比 exe 新 (最新改动 $($stamp.ToString('MM-dd HH:mm:ss')), exe 打包于 $($exeTime.ToString('MM-dd HH:mm:ss')))"
    }
}
if ($Fast -and -not $isRelease) { $needFull = $false; $why = '指定 -Fast' }

Write-Host ("=" * 68)
Write-Host " Vixiq 打包版同步" -ForegroundColor Magenta
Write-Host (" 模式: " + $(if ($isRelease) { '发布版 (不含版权立绘)' } else { '本地自用版 (含初音立绘)' }))
Write-Host (" 方式: " + $(if ($needFull) { "全量重打包 — $why" } else { '前端快同步 (只拷 web/static)' }))
Write-Host ("=" * 68)

# ── 2. 快同步: 只把 web/static 覆盖进 dist ────────────────────────────────────
if (-not $needFull) {
    if (-not (Test-Path $DstStatic)) {
        Write-Warn2 "dist 里没有 _internal\web\static, 改走全量重打包"
        $needFull = $true
    }
}

if (-not $needFull) {
    Write-Step "[1/2] 同步 web/static → dist"
    $n = 0
    Get-ChildItem $SrcStatic -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($SrcStatic.Length).TrimStart('\')
        $dst = Join-Path $DstStatic $rel
        $dd = Split-Path -Parent $dst
        if (-not (Test-Path $dd)) { New-Item -ItemType Directory -Path $dd -Force | Out-Null }
        Copy-Item $_.FullName $dst -Force
        Write-Host ("      · " + $rel)
        $n++
    }
    # 源里已删除的文件同步删掉, 免得 exe 还在伺服一个已经不存在的资源
    Get-ChildItem $DstStatic -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($DstStatic.Length).TrimStart('\')
        if (-not (Test-Path (Join-Path $SrcStatic $rel))) {
            Remove-Item $_.FullName -Force
            Write-Host ("      · 删除 " + $rel) -ForegroundColor DarkGray
        }
    }
    Write-Ok "$n 个文件已同步"

    Write-Step "[2/2] 启动 exe"
    Start-VixiqExe
    Write-Host ""
    exit 0
}

# ── 3. 全量重打包 ─────────────────────────────────────────────────────────────
# 3.1 先杀掉占用 dist 的进程, 否则 --noconfirm 删 dist 会报一堆 "Access is denied"
function Stop-BlockingProcesses {
    $killed = 0
    Get-Process -Name 'vixiq' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host ("      · 关闭打包版 vixiq.exe (PID {0})" -f $_.Id)
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue; $killed++
    }
    try {
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'main\.py' -and $_.CommandLine -match 'pixiv-toolkit' } |
            ForEach-Object {
                Write-Host ("      · 关闭源码版 python (PID {0})" -f $_.ProcessId)
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed++
            }
        # 孤儿 msedgedriver 会锁住 dist\_internal\resources\msedgedriver.exe
        Get-CimInstance Win32_Process -Filter "Name='msedgedriver.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and ($_.CommandLine -match 'pixiv-toolkit' -or $_.CommandLine -match 'vixiq') } |
            ForEach-Object {
                Write-Host ("      · 清理孤儿 msedgedriver 进程树 (PID {0})" -f $_.ProcessId)
                & taskkill /F /T /PID $_.ProcessId 2>$null | Out-Null; $killed++
            }
    } catch {
        Write-Warn2 "进程枚举失败 (不影响继续): $_"
    }
    if ($killed -gt 0) { Start-Sleep -Milliseconds 1500 }
    return $killed
}

Write-Step "[1/4] 关闭占用 dist 的进程"
$k = Stop-BlockingProcesses
if ($k -eq 0) { Write-Host "      (没有进程占用)" }

Write-Step "[2/4] 备份 dist\config\user.json"
# 这个文件不在版本库里, --noconfirm 会连 dist 整个删掉 → 丢了 exe 会静默回落
# 端口 7790 + dist\vixiq\data\ 空库, 真实图库完全不可见。
if (Test-Path $CfgDst) {
    $bakDir = Split-Path -Parent $CfgBak
    if (-not (Test-Path $bakDir)) { New-Item -ItemType Directory -Path $bakDir -Force | Out-Null }
    Copy-Item $CfgDst $CfgBak -Force
    Write-Ok "已备份到 $CfgBak"
} else {
    Write-Warn2 "dist 里没有 user.json (将从 config\user.json 补)"
}

Write-Step "[3/4] PyInstaller 重打包 (这一步最慢, 请等)"
if ($isRelease) { $env:VIXIQ_RELEASE = '1' } else { Remove-Item Env:\VIXIQ_RELEASE -ErrorAction SilentlyContinue }
& $Python -m PyInstaller build.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n打包失败 (exit $LASTEXITCODE), dist 可能已被删除。" -ForegroundColor Red
    Write-Host "配置备份仍在: $CfgBak" -ForegroundColor Red
    exit 1
}

Write-Step "[4/4] 还原配置 + 校验"
if (-not (Test-Path (Split-Path -Parent $CfgDst))) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $CfgDst) -Force | Out-Null
}
# 备份与源码 config 里挑**较新的**那个: 用户可能在源码 config 里改了端口/数据目录
$cands = @()
if (Test-Path $CfgBak) { $cands += Get-Item $CfgBak }
if (Test-Path $CfgSrc) { $cands += Get-Item $CfgSrc }
if ($cands.Count -gt 0) {
    $pick = $cands | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Copy-Item $pick.FullName $CfgDst -Force
    Write-Ok "user.json 已还原 (取自 $($pick.FullName))"
} else {
    Write-Warn2 "没找到任何 user.json, exe 会按默认配置启动 (端口 7790 / dist 内空库)!"
}

$fail = @()
if (-not (Test-Path $Exe))      { $fail += 'exe 不存在' }
if (-not (Test-Path $CfgDst))   { $fail += 'config\user.json 不存在' }
if (-not (Test-Path $DstStatic)){ $fail += '_internal\web\static 不存在' }
if ($isRelease -and (Test-Path (Join-Path $DstStatic 'to-top.png'))) { $fail += '发布模式产物里仍有 to-top.png' }

if ($fail.Count -gt 0) {
    Write-Host "`n校验未通过: $($fail -join ' / ')" -ForegroundColor Red
    exit 1
}
$exeItem = Get-Item $Exe
Write-Ok ("exe: {0}  ({1:N1} MB, {2})" -f $exeItem.FullName, ($exeItem.Length / 1MB), $exeItem.LastWriteTime.ToString('MM-dd HH:mm:ss'))
Write-Ok ("静态资源: {0} 个文件" -f (Get-ChildItem $DstStatic -Recurse -File).Count)
Write-Ok ("配置: {0} B" -f (Get-Item $CfgDst).Length)

Write-Host ""
Start-VixiqExe
Write-Host ""
exit 0
