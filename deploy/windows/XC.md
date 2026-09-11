# Windows 本地直跑

双击 `deploy\windows\xc.bat` 就起来。要 HTTPS / 局域网访问，再双击
`deploy\edge\edge.bat` 起网关（见 [../edge/CADDY.md](../edge/CADDY.md)）。两个窗口
各开着，**关掉窗口就是停止**，`data\` 不动。

> **`xc.bat` 只是个启动器**，真正的逻辑与中文输出在同名的 `xc.ps1` 里，这份文档
> 是从原来 `xc.bat` 的注释里搬出来的。
>
> 为什么拆：cmd.exe 在 `chcp 65001` 下按字节偏移回溯文件位置，会从一个汉字中间接着
> 读，**后半行被当成一条新命令执行**——`rem uv run xingcha admin reset-password  忘了密码`
> 这一行真的被跑过一次，"忘了密码"成了命令行参数。而且时有时无，只在文件不在页缓存
> 里时才撞上。完整说明见 [../edge/CADDY.md](../edge/CADDY.md)。
>
> 规矩：`.bat` 一个非 ASCII 字节都不许有；`.ps1` 反过来必须带 UTF-8 BOM。都有测试盯着。

---

## 为什么 Windows 上不走 docker

两条，第二条比第一条重要：

1. 这台机器没装 Docker Desktop，为了一个 Python 进程装一层虚拟机不划算；
2. 就算装了，`data` 也**不能**放宿主目录：Docker Desktop 经 9p/virtiofs 把 Windows
   目录挂进虚拟机，那是网络文件系统，**SQLite 的 WAL 在上面会静默降级**——症状是零星
   的 `database is locked`，只在并发写时出现，压不出来也难复现。绕开它只能改用命名卷，
   于是数据又跑进虚拟机里，备份、体检、恢复演练全都得进容器做。

本地直跑没有这两条：`data` 就是仓库根下的 `data\`，NTFS 上 WAL 是正常的，备份就是几个
能直接拷走的文件。

Linux 那台仍然是 docker compose + Caddy，那一套一个字都没动。这里换掉的只是「怎么把
进程跑起来」：uv 直接在宿主上起 uvicorn，不打镜像、不要 docker。

## 为什么 Caddy 要在**这台**机器上

TLS 在本机终止，「Caddy 到应用」那一跳走回环、不出这台机器。

此前是让另一台机器上的 Caddy 按「IP:端口」反代过来，已经去掉：那会把这一跳变成跨网络
的**明文**，而浏览器里看着是 HTTPS——最容易被误当成端到端加密的拓扑。

## 脚本做的三件事

| | 为什么 |
|---|---|
| `cd /d "%~dp0..\.."` | 仓库根是脚本所在目录的**上两级**。少上一级就会在 `deploy\` 下面又建一个 `data\`，症状是「我明明起来了，后台却是空的」，而两个 `data` 都真实存在，看不出哪个是对的 |
| `uv sync --frozen --no-dev` | `--frozen` 严格照 `uv.lock` 装，不许就地改锁文件——部署机上「顺手升了个依赖」是最难查的一类差异：代码一个字没动，行为变了。`--no-dev` 跳过 dev 组（playwright 几百 MB，跑服务用不上） |
| `uv run xingcha serve` | 监听地址、端口、数据目录**一律由 `.env` 决定**，不传 `--host` / `--port`。传了就是第二个来源，而两个来源必然有一天不一致 |

> `--no-dev` 会把 dev 依赖从 `.venv` 里**删掉**（pytest / ruff / pyright / playwright
> 都在 dev 组）。之后要跑测试，先 `uv sync --frozen` 补回来。

## .env

和 Linux 用**同一份模板**（`deploy\.env.example`），不另起一份——另起一份的代价是它们
会各自漂移，而症状是「我在 Windows 上跑的那套和 Linux 上不是一个东西」：两边都能用，
但配置来源不同。模板最后一节是 Windows 专用的，那几项在 Linux 上会被 compose 的
`environment:` 整个覆盖掉，留着无害。

一项都不改也能起来，但**明文 HTTP 且只绑回环**：只有这台机器能打开。要 HTTPS 就放开
最后一节的两行：

```ini
XINGCHA_TRUSTED_PROXIES=127.0.0.1
XINGCHA_PUBLIC_URL=https://本机内网IP:8443
```

第二项只影响后台页面上印的示例地址。第一项是实质的：Caddy 用明文 HTTP 连回环，
星槎看到的协议是 `http`，**会话 cookie 就不带 `Secure`**——功能完全正常，只是少一层
保护，没人会注意到。配了它 uvicorn 才会读 Caddy 发来的 `X-Forwarded-Proto`。
只填 `127.0.0.1` 不填 `*`：那个头能伪造，只信回环等于只有已经在这台机器上的进程
才伪造得了。

## 绑到 0.0.0.0 的那条警告

`XINGCHA_HOST` 不是 `127.0.0.1` 时，端口是**真的开在局域网里的**：一条明文入口，
谁都能直连，绕过 Caddy 那层 TLS。要 HTTPS 的话不用改这一项——Caddy 就在本机，走回环
连 `127.0.0.1:8720` 就够了。

另外第一次跑 Windows 防火墙会弹窗问要不要放行 `python.exe`：**必须点允许，而且要勾
「专用网络」**，否则别的机器一直连不上，而这台机器自己完全正常——那个现象指不到防火墙。

## 别的动作没有包装

它们本来就不长、也没有坑：

```cmd
uv run xingcha doctor                  一次性体检
uv run xingcha admin status            后台账号状态
uv run xingcha admin reset-password    忘了密码时重置
uv run xingcha db backup               崩溃一致的备份
uv run xingcha db verify               体检备份
```

`deploy\linux\xc`（bash）存在的理由是那条 docker 命令太长、每一段都是踩过的坑。
这里没有那个问题，所以 `xc.bat` 只做一件事：把服务起起来。
