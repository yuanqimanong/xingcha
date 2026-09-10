@echo off
chcp 65001 >nul

rem ===========================================================================
rem 星槎 · Windows 本地直跑。**双击这个文件就起来。**
rem
rem Linux 那台仍然是 docker compose + Caddy，那一套一个字都没动。这里换掉的只是
rem 「怎么把进程跑起来」：uv 直接在宿主上起 uvicorn，不打镜像、不要 docker。
rem
rem 为什么 Windows 上不走 docker —— 两条，第二条比第一条重要：
rem
rem   1. 这台机器没装 Docker Desktop，为了一个 Python 进程装一层虚拟机不划算；
rem   2. 就算装了，data 也**不能**放宿主目录：Docker Desktop 经 9p/virtiofs 把
rem      Windows 目录挂进虚拟机，那是网络文件系统，SQLite 的 WAL 在上面会静默
rem      降级 —— 症状是零星的 database is locked，只在并发写时出现，压不出来也
rem      难复现。绕开它只能改用命名卷，于是数据又跑进虚拟机里，备份、体检、
rem      恢复演练全都得进容器做。
rem
rem   本地直跑没有这两条：data 就是仓库根下的 data\，NTFS 上 WAL 是正常的，
rem   备份就是几个能直接拷走的文件。
rem
rem HTTPS 仍然复用 Linux 那台 Caddy —— 它按「IP:端口」反代到这台机器的 8720，
rem 浏览器里出现的始终是网关地址、网关的证书，根证书还是只装那一次。
rem 怎么配见 deploy\CADDY.md 的「跨机器接入」。**代价必须说清楚**：Caddy 到这里
rem 这一跳变成了跨网络的明文，别以为 TLS 是端到端的。
rem
rem 别的动作没有包装，因为它们本来就不长、也没有坑：
rem
rem     uv run xingcha doctor                  一次性体检
rem     uv run xingcha admin status            后台账号状态
rem     uv run xingcha admin reset-password    忘了密码
rem     uv run xingcha db backup               崩溃一致的备份
rem     uv run xingcha db verify               体检备份
rem
rem deploy\linux\xc（bash）存在的理由是那条 docker 命令太长、每一段都是踩过的坑。
rem 这里没有那个问题，所以这个脚本只做一件事：把服务起起来。
rem ===========================================================================

rem 仓库根 = 脚本所在目录的**上两级**（脚本在 deploy\windows\ 下）。
rem 双击时 cmd 的工作目录是脚本目录，而 .env 与 data\ 都在仓库根 —— 少上一级
rem 就会在 deploy\ 下面又建一个 data\，症状是「我明明起来了，后台却是空的」，
rem 而两个 data 目录都真实存在，看不出哪个是对的。
cd /d "%~dp0..\.."

rem ---------------------------------------------------------------------------
rem uv
rem ---------------------------------------------------------------------------
where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo [x] 找不到 uv。装一次就够了，两种随便挑：
    echo.
    echo         winget install --id astral-sh.uv
    echo         powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo.
    echo     装完**重开一个窗口**再双击 —— PATH 是进程启动时读的，
    echo     当前这个窗口拿不到刚装上的东西。
    goto :fail
)

rem ---------------------------------------------------------------------------
rem .env —— 和 Linux 用**同一份模板**，不另起一份
rem
rem 另起一份的代价是它们会各自漂移，而症状是「我在 Windows 上跑的那套和 Linux
rem 上不是一个东西」：两边都能用，但配置来源不同。模板最后一节是 Windows 专用的，
rem 那几项在 Linux 上会被 compose 的 environment: 整个覆盖掉，留着也无害。
rem ---------------------------------------------------------------------------
if not exist ".env" (
    echo → .env 不存在，从模板生成一份
    copy /y "deploy\.env.example" ".env" >nul
    if errorlevel 1 goto :fail
    echo ✓ 已生成 .env
    echo.
    echo   一项都不改也能起来，但**只绑回环**：只有这台机器自己能打开。
    echo   要让 Linux 那台 Caddy 反代过来，翻到 .env 最后一节，把这三行放开：
    echo.
    echo         XINGCHA_HOST=0.0.0.0
    echo         XINGCHA_PUBLIC_URL=https://那台Linux的IP:8443
    echo         XINGCHA_TRUSTED_PROXIES=那台Linux的IP
    echo.
)

rem ---------------------------------------------------------------------------
rem 依赖
rem
rem --frozen：严格照 uv.lock 装，不许就地改锁文件。部署机上「顺手升了个依赖」
rem 是最难查的一类差异 —— 代码一个字没动，行为变了。
rem --no-dev：dev 组里有 playwright，几百 MB，跑服务用不上。
rem ---------------------------------------------------------------------------
echo → 同步依赖（第一次要下 Python 和依赖包，几分钟；之后是秒级）
uv sync --frozen --no-dev
if errorlevel 1 (
    echo.
    echo [x] uv sync 失败，原因在上面。
    goto :fail
)

rem ---------------------------------------------------------------------------
rem 读 .env 只为了**打印一个能点开的地址**。真正生效的是应用自己读 .env，
rem 不是这里 —— 所以这几行读错了最多是显示不对，不会让服务跑成另一个样子。
rem
rem eol=# 跳过注释行；tokens=1,* delims== 把 KEY=VALUE 拆成两半。
rem ---------------------------------------------------------------------------
set "XC_PORT=8720"
set "XC_BIND=127.0.0.1"
set "XC_PUBLIC="
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="XINGCHA_PORT"            set "XC_PORT=%%b"
    if /i "%%a"=="XINGCHA_HOST"            set "XC_BIND=%%b"
    if /i "%%a"=="XINGCHA_PUBLIC_URL"      set "XC_PUBLIC=%%b"
)

echo.
if defined XC_PUBLIC (
    echo   %XC_PUBLIC%
    echo   经 Linux 那台 Caddy 反代过来。浏览器还拦证书的话，是这台设备还没装
    echo   网关的根证书 —— 在 Linux 那台跑 .\edge ca，它会印出 Windows 的装法。
) else (
    echo   http://127.0.0.1:%XC_PORT%
    echo   **明文 HTTP，且只绑回环**：只有这台机器能打开。
    echo   想让别人也能用、并且走 HTTPS：见 .env 最后一节与 deploy\CADDY.md。
)

if not "%XC_BIND%"=="127.0.0.1" (
    echo.
    echo   ! 绑在 %XC_BIND% 上，端口是真的开在局域网里的。
    echo     第一次跑 Windows 防火墙会弹窗问要不要放行 python.exe：
    echo     **必须点允许，而且要勾上「专用网络」**。点了取消的话，这台机器自己
    echo     完全正常，而 Caddy 那边一直 502 —— 那个现象指不到防火墙。
)

echo.
echo → 启动。**关掉这个窗口就是停止**，data 不动。
echo.

rem 监听地址、端口、数据目录一律由 .env 决定，这里不传 --host / --port。
rem 传了就是第二个来源，而两个来源必然有一天不一致。
uv run --frozen --no-dev xingcha serve
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
pause
exit /b 1
