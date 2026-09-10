@echo off
chcp 65001 >nul
setlocal

rem ===========================================================================
rem 网关（Caddy 单文件）· Windows。**双击这个文件就起来。**
rem
rem 一个 caddy.exe + 一份 Caddyfile，没有 docker、没有服务注册。窗口开着就是跑着,
rem 关掉就是停止 —— 和 deploy\windows\xc.bat 同一个心智。
rem
rem     双击                   前台跑（关窗口 = 停）
rem     edge.bat get           下载 caddy.exe 到这个目录（一次就够）
rem     edge.bat trust         把根证书装进本机信任库（**要管理员**）
rem     edge.bat ca            导出根证书 + 印出别的设备怎么装
rem     edge.bat stop          停掉后台跑着的那个
rem
rem 这个脚本几乎不做事，真正的活是 caddy 自己的子命令干的。它只负责三件忘了就出
rem 问题、而症状指不到这里的事：
rem
rem   1. 认准本目录的 caddy.exe（优先于 PATH 里的）—— 两个版本同时在，
rem      「我明明升级了」却跑着旧的那个，是最费时的一类困惑；
rem   2. 把 EDGE_HOST 传进去 —— Caddyfile 里的 {$VAR} 读的是进程环境变量，
rem      空的话站点地址退化成 https://:8443，证书签不出来而报错离根因很远。
rem      来源是仓库根 .env 的 XINGCHA_WEB_HOST，**不再单独设一个变量**；
rem   3. cd 到脚本自己的目录 —— 认的是这一份 Caddyfile，不是「你在哪儿敲命令」。
rem ===========================================================================

cd /d "%~dp0"

set "VERB=%~1"
if "%VERB%"=="" set "VERB=run"

rem ---------------------------------------------------------------------------
rem 二进制
rem ---------------------------------------------------------------------------
set "CADDY=caddy.exe"
if exist "%~dp0caddy.exe" set "CADDY=%~dp0caddy.exe"

if /i "%VERB%"=="get" goto :get

if not exist "%~dp0caddy.exe" (
    where caddy.exe >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [x] 找不到 caddy.exe。下载一次就够（约 45 MB，不进版本库）：
        echo.
        echo         edge.bat get
        echo.
        echo     或自己拿：https://caddyserver.com/download
        echo     存成 %~dp0caddy.exe
        goto :fail
    )
)

rem ---------------------------------------------------------------------------
rem 浏览器里敲的那个地址 —— 同时是证书上的名字与 default_sni 的值。
rem 填错的症状是「证书警告点不过去」，不是「打不开」。
rem
rem eol=# 跳过注释行；tokens=1,* delims== 把 KEY=VALUE 拆成两半。
rem ---------------------------------------------------------------------------
set "EDGE_HOST="
if exist "..\..\.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in ("..\..\.env") do (
        if /i "%%a"=="XINGCHA_WEB_HOST" set "EDGE_HOST=%%b"
    )
)
if "%EDGE_HOST%"=="" (
    set "EDGE_HOST=localhost"
    echo   ! .env 里没有 XINGCHA_WEB_HOST，按 localhost 签证书 —— 别的设备打不开，
    echo     而这台机器自己一切正常。要给别人用就把它填成本机内网 IP。
)
if "%EDGE_HOST%"=="0.0.0.0" (
    echo [x] XINGCHA_WEB_HOST=0.0.0.0 是监听地址，不是能敲的主机名。
    echo     在仓库根的 .env 里改成这台机器的内网 IP。
    goto :fail
)

if /i "%VERB%"=="run"    goto :run
if /i "%VERB%"=="start"  goto :start
if /i "%VERB%"=="stop"   goto :stop
if /i "%VERB%"=="reload" goto :reload
if /i "%VERB%"=="trust"  goto :trust
if /i "%VERB%"=="ca"     goto :ca
if /i "%VERB%"=="status" goto :status
echo [x] 不认识的动作：%VERB%（get / run / start / stop / reload / trust / ca / status）
goto :fail

:get
if exist "%~dp0caddy.exe" (
    echo ✓ 已经有了：%~dp0caddy.exe（要换版本先删掉它）
    goto :done
)
echo → 下载 caddy.exe
curl -fsSL "https://caddyserver.com/api/download?os=windows&arch=amd64" -o "%~dp0caddy.exe"
if errorlevel 1 (
    echo [x] 下载失败。到 https://caddyserver.com/download 手动拿一个，
    echo     存成 %~dp0caddy.exe
    goto :fail
)
echo ✓ %~dp0caddy.exe
echo   接着：双击 edge.bat 就起来了。
goto :done

:run
echo.
echo   https://%EDGE_HOST%:8443  →  127.0.0.1:8720
echo   **关掉这个窗口就是停止。** 星槎自己要另外起（deploy\windows\xc.bat）。
echo.
echo   第一次跑 Windows 防火墙会弹窗（它要监听 8443）：**要允许，勾「专用网络」**。
echo   点了取消的话本机 https://127.0.0.1:8443 完全正常，而别的机器一直连不上。
echo.
echo   浏览器拦证书 = 这台设备还没装根证书：以管理员身份跑 edge.bat trust
echo.
"%CADDY%" run --config Caddyfile
if errorlevel 1 goto :fail
goto :done

:start
"%CADDY%" start --config Caddyfile
if errorlevel 1 goto :fail
echo ✓ https://%EDGE_HOST%:8443 → 127.0.0.1:8720（后台跑着，停止用 edge.bat stop）
goto :done

:stop
"%CADDY%" stop
goto :done

:reload
"%CADDY%" reload --config Caddyfile
if errorlevel 1 goto :fail
echo ✓ 已重载（配置验不过会保留旧的，不会把服务弄下线）
goto :done

:trust
"%CADDY%" trust
if errorlevel 1 (
    echo.
    echo [x] 装不进去，多半是**没用管理员身份**跑：右键这个 .bat → 以管理员身份运行。
    goto :fail
)
echo ✓ 已装进本机信任库。**要重启浏览器。**
goto :done

:ca
set "CRT=%AppData%\Caddy\pki\authorities\local\root.crt"
if not exist "%CRT%" (
    echo [x] 还没有根证书 —— 先起一次网关：双击 edge.bat
    goto :fail
)
copy /y "%CRT%" "%~dp0root.crt" >nul
echo ✓ 已导出 %~dp0root.crt（公钥，随便传）
echo.
echo   别的 Windows（管理员）：
echo       certutil -addstore -f ROOT root.crt
echo.
echo   Linux（curl / wget）：
echo       sudo cp root.crt /usr/local/share/ca-certificates/edge-root.crt
echo       sudo update-ca-certificates
echo.
echo   Linux 上的 Chrome / Chromium 另有一套信任库：
echo       certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n edge-root -i root.crt
echo.
echo   装完**要重启浏览器**。
goto :done

:status
netstat -ano | findstr ":8443" >nul
if errorlevel 1 (
    echo   ! 8443 没人听 —— 网关没起。双击 edge.bat
) else (
    echo ✓ 网关在跑：https://%EDGE_HOST%:8443
)
goto :done

:done
endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
