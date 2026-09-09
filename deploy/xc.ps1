<#
星槎的 Windows 操作入口。与 deploy/xc（bash）一一对应：

    .\deploy\xc.ps1 start      重新构建代码并启动，数据不动
    .\deploy\xc.ps1 update     拉代码 + 重新构建启动
    .\deploy\xc.ps1 redeploy   连数据一起清空，从零开始
    .\deploy\xc.ps1 stop       停止，数据保留
    .\deploy\xc.ps1 logs       跟随日志
    .\deploy\xc.ps1 status     容器状态 + 后台账号状态

与 Linux 版的唯一实质差别：**数据要放命名卷，不放宿主目录。**

Docker Desktop 经 9p/virtiofs 把 Windows 目录挂进虚拟机，那是网络文件系统，
**SQLite 的 WAL 在上面会静默降级**——症状是零星的 database is locked，只在并发写
时出现，压不出来也难复现。所以 Windows 上要在 .env 里写：

    XINGCHA_DATA_MOUNT=xingcha_data

顺带也就没有 chown 的事了（命名卷由 Docker 建，属主直接是容器里的 UID；
Windows 上本来也 chown 不了）。redeploy 删的是那个卷，不是目录。
#>

[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet('start', 'update', 'redeploy', 'stop', 'logs', 'status')]
  [string]$Action,

  [Parameter(Position = 1)]
  [int]$Tail = 50
)

$ErrorActionPreference = 'Stop'

# 仓库根 = 脚本所在目录的上一级。这样在任何工作目录下调用都对。
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $Root

$Files = @('-f', 'deploy/docker-compose.yml', '--env-file', '.env')

function Require-Gateway {
  # 网关（..\edge）是**硬依赖**：xingcha 一个宿主端口都不发布，对外只经它。
  # 不检查的话症状是"容器 healthy 却什么都打不开"，而每一层单独看都正常。
  docker network inspect edge 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Die '共享网络 edge 不存在。先起网关：cd ..\edge; .\edge start（见 ..\edge\README.md）'
  }
  $running = docker inspect -f '{{.State.Running}}' edge-caddy-1 2>$null
  if ($running -ne 'true') { Die '网关容器 edge-caddy-1 没在跑。先：cd ..\edge; .\edge start' }
  Ok '网关就绪（edge-caddy-1）'
}

function Say  { param($m) Write-Host "→ $m" -ForegroundColor Cyan }
function Ok   { param($m) Write-Host "✓ $m" -ForegroundColor Green }
function Die  { param($m) Write-Host "✗ $m" -ForegroundColor Red; exit 1 }

function Compose { docker compose @Files @args }

# up / down 一律带 --remove-orphans：不带的话，**从 compose 里删掉一个服务之后它的
# 容器会永远留着**——compose 只管现在声明的服务，那个孤儿既不会被 down 掉也不会被
# up 重建，就一直跑着占端口。实际踩过（Caddy 去掉之后 xingcha-caddy-1 还占着端口）。
function Up   { Compose up -d --build --remove-orphans }
function Down { Compose down --remove-orphans }

function Need-Env {
  # 与 bash 版一致：没有 .env 就生成一份并停下来，而不是让人先去读文档。
  # （此前这里让用户去填 XINGCHA_DOMAIN —— 那一项随 Caddy 一起删掉了，
  #   照着做只会得到一个没有效果的配置项。）
  if (-not (Test-Path '.env')) {
    Say '.env 不存在，从模板生成一份'
    Copy-Item 'deploy/.env.example' '.env'
    Ok '已生成 .env'
    Write-Host '  Windows 上必须加一行 XINGCHA_DATA_MOUNT=xingcha_data（见文件头的说明）。'
    Write-Host '  XINGCHA_WEB_HOST / XINGCHA_WEB_PORT 填**网关**的地址与端口。'
    exit 0
  }
}

function From-Env {
  param($Key)
  $line = Select-String -Path '.env' -Pattern "^$Key=(.*)$" -ErrorAction SilentlyContinue |
          Select-Object -First 1
  if ($line) { $line.Matches[0].Groups[1].Value.Trim() } else { $null }
}

function Wait-Healthy {
  # `Select-Object -First 1`：compose 可能吐多行（或空），直接 .Trim() 在数组上会炸
  $id = Compose ps -q xingcha | Select-Object -First 1
  if (-not $id) { Die "xingcha 容器没起来。看 .\deploy\xc.ps1 logs" }
  Say '等健康检查…'
  foreach ($i in 1..60) {
    $state = docker inspect -f '{{.State.Health.Status}}' $id 2>$null
    if ($state -eq 'healthy') { Ok 'healthy'; return }
    # 起不来的时候**立刻把日志摊开**，而不是让人等满两分钟再自己去找
    if ((docker inspect -f '{{.State.Restarting}}' $id 2>$null) -eq 'true') {
      Write-Host ''; Compose logs --tail 20 xingcha
      Die '容器在重启循环里（日志见上）'
    }
    Start-Sleep -Seconds 2
  }
  Write-Host ''; Compose logs --tail 30 xingcha
  Die '两分钟没等到 healthy（日志见上）'
}

function Show-Url {
  $host_ = From-Env 'XINGCHA_WEB_HOST'; if (-not $host_) { $host_ = 'localhost' }
  $port  = From-Env 'XINGCHA_WEB_PORT'; if (-not $port)  { $port  = '8443' }
  Write-Host ''
  Write-Host "  https://${host_}:${port}" -ForegroundColor White
  Write-Host '  经共享网关。xingcha 自己零宿主端口，没有别的入口。'
  Write-Host '  浏览器还拦的话，说明这台设备还没装网关的根证书：cd ..\edge; .\edge ca'
  Write-Host ''
}

switch ($Action) {
  'start' {
    Need-Env; Require-Gateway
    Say '构建并启动（数据保留）'
    Up
    Wait-Healthy
    Show-Url
  }

  'redeploy' {
    Need-Env
    Write-Host '这会删掉全部数据：数据库、密钥环、备份。' -ForegroundColor Yellow
    Write-Host '上游 key、后台密码、所有 Agent 与调用记录都会没有。'
    $answer = Read-Host '输入 yes 继续'
    if ($answer -ne 'yes') { Die '已取消，什么都没动。' }

    # `down -v` 删掉本项目声明的命名卷（xingcha_data 与 caddy 的两个）。
    # 数据在卷里而不是宿主目录里，所以**不存在** Linux 版那个"容器还在跑时删目录、
    # 进程握着已删除的 inode 继续写"的陷阱——down 一定先于卷被删除。
    Require-Gateway
    Say '停止容器并删除数据卷'
    Compose down -v --remove-orphans
    Say '构建并启动'
    Up
    Wait-Healthy
    Ok '全新实例。第一次打开 /admin 会引导设定密码（或按 .env 里的 XINGCHA_ADMIN_PASSWORD）'
    Show-Url
  }

  'update' {
    Need-Env; Require-Gateway
    # `pull --ff-only` 而不是 `reset --hard`：这个脚本也会在开发机上被跑，
    # 而那里 reset --hard 会不声不响地毁掉未提交的工作。
    git diff --quiet; $dirty = $LASTEXITCODE -ne 0
    if ($dirty) { Die '工作区有未提交的改动。先 commit 或 stash，再 update。' }
    Say '拉代码'
    git pull --ff-only
    Say '构建并启动'
    Up
    Wait-Healthy
    Show-Url
  }

  'stop' { Down; Ok '已停止（数据卷保留）' }

  'logs' { Compose logs -f --tail $Tail xingcha }

  'status' {
    Compose ps
    Write-Host ''
    try { Compose exec -T xingcha xingcha admin status } catch { }
  }

  default {
    @'
星槎

  .\deploy\xc.ps1 start        重新构建代码并启动，数据不动（日常用这个）
  .\deploy\xc.ps1 update       拉代码 + 重新构建启动
  .\deploy\xc.ps1 redeploy     清空数据从零开始（会问一次 yes）
  .\deploy\xc.ps1 stop         停止，数据保留
  .\deploy\xc.ps1 logs [n]     跟随日志
  .\deploy\xc.ps1 status       容器状态 + 后台账号状态
'@ | Write-Host
    exit 1
  }
}
