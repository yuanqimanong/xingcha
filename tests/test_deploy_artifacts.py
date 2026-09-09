"""部署产物的结构约束。

------------------------------------------------------------------------------
为什么这一层非有不可
------------------------------------------------------------------------------

Dockerfile / docker-compose.yml / .env.example / xc / drill.sh 之间有**跨文件的
隐式契约**，而它们互相之间没有任何类型检查：compose 读一个环境变量，.env.example
负责告诉用户要填，xc 负责把 compose 文件与 .env 传对，drill.sh 负责用同一套路径
找到容器。

任意一处漏掉，**单元测试全绿、镜像构建成功、代码审查也看不出来**——症状只在真的
``docker compose up`` 时出现。实际发生过两次：一次是 Caddyfile 引用了 compose 从未
传过的变量，配置解析失败进无限重启；一次是 ``uv sync`` 默认装 editable，构建绿灯而
镜像里 ``import xingcha`` 直接失败。

编排此前是三个 compose 文件（生产 / 局域网 / Windows）加两份 Caddyfile，现在收敛成
**一个 compose 文件、一个容器、明文 HTTP**。差异全部变成 ``.env`` 里的变量。
这些断言也跟着换成守新形状的。

**这里只做静态结构检查**，不起容器——起容器的验证是 deploy/drill.sh、CI 的
"整栈真的起得来"，与真机演练的事。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from xingcha import contract as C

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
DRILL_SH = ROOT / "deploy" / "drill.sh"
XC = ROOT / "deploy" / "xc"
XC_PS1 = ROOT / "deploy" / "xc.ps1"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose_raw() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def compose_vars(raw: str) -> dict[str, str | None]:
    """compose 里引用的 ``${VAR}``，映射到它的默认值（没有默认值则 None）。"""
    out: dict[str, str | None] = {}
    for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-([^}]*))?\}", raw):
        out.setdefault(m.group(1), m.group(3))
    return out


# =============================================================================
# 只有一份编排
# =============================================================================


class TestSingleFile:
    """收敛成一个文件之后，**多出来的那份就是漂移的开始**。

    此前三份 compose 靠 ``.env`` 里的 COMPOSE_FILE 拼起来，代价是：在哪个目录敲
    命令会改变结果（在 deploy/ 里 restart 直接失败），而 Windows 上那行的分隔符
    根本不对（``:`` vs ``;``），表现为"端口没开、也没有任何报错"。
    """

    #: 允许存在的 compose 文件，**闭集**。
    #:
    #: 基础那一份 + 一个可选的拓扑叠加层。多出任何一份都要经过一次决定：
    #: 参数（端口、地址、数据位置）必须做成变量，只有**拓扑**（独立跑 vs 挂在
    #: 共享网关后面）才配得上一个文件——因为 `networks` 这类字段没法靠 ${} 条件
    #: 出现。此前三份文件（生产/局域网/Windows）全是参数，已经收敛掉了。
    ALLOWED: ClassVar[list[str]] = [
        "deploy/docker-compose.edge.yml",
        "deploy/docker-compose.yml",
    ]

    def test_compose_files_are_a_closed_set(self):
        found = sorted(
            q.relative_to(ROOT).as_posix()
            for q in ROOT.rglob("docker-compose*.y*ml")
            if ".venv" not in q.parts and ".git" not in q.parts
        )
        assert found == self.ALLOWED, f"编排文件与闭集不符：{found}"

    def test_caddy_is_really_gone(self):
        """Caddy 去掉了就要**彻底**去掉。

        留下一份 Caddyfile 或一处引用，下一个读到它的人（包括三个月后的自己）会
        以为前面还有反代，于是在 cookie、HSTS、X-Forwarded-Proto 上做出错误假设。
        """
        assert not list(ROOT.glob("Caddyfile*")), "根目录还有 Caddyfile"
        assert not list((ROOT / "deploy").glob("Caddyfile*")), "deploy/ 还有 Caddyfile"
        for path in (COMPOSE, XC, XC_PS1, DRILL_SH):
            # 只看真正执行的行。注释里解释"为什么没有 Caddy 了"是正当的——
            # 那条信息恰恰能拦住下一个人做出"前面有反代"的错误假设。
            code = "\n".join(
                ln
                for ln in path.read_text(encoding="utf-8").splitlines()
                if not ln.lstrip().startswith(("#", "//"))
            )
            assert "caddy" not in code.lower(), f"{path.name} 的可执行部分还引用 caddy"

    def test_project_name_is_pinned(self, compose: dict):
        """不写死 ``name:`` 的话 compose 用 compose 文件所在目录名，也就是
        ``deploy``——容器会叫 deploy-xingcha-1，而文档、脚本与运维记忆里全是
        xingcha-xingcha-1。"""
        assert compose.get("name") == "xingcha"

    def test_build_context_is_the_repo_root(self, compose: dict):
        """compose 里的相对路径以**文件所在目录**为基准，这里是 deploy/。"""
        assert compose["services"]["xingcha"]["build"]["context"] == ".."


# =============================================================================
# 跨文件的变量契约
# =============================================================================


class TestEnvVarContract:
    def test_every_var_has_a_default_so_an_empty_env_still_works(self, compose_raw: str):
        """**空 .env 必须能起来。**

        没有默认值的变量（``${VAR}``）会静默变成空串；写成 ``${VAR:?...}`` 则让
        ``up`` 直接失败。两种都不要：这个产品的第一次启动不该需要先读一遍文档。
        默认值把"能跑"和"配得好"分开——先跑起来，再去后台配。
        """
        assert "${" in compose_raw, "一个变量都没有？这条断言失效了"
        assert ":?" not in compose_raw, "不该有必填变量：空 .env 也要能起来"
        missing = [k for k, v in compose_vars(compose_raw).items() if v is None]
        assert not missing, f"这些变量没有默认值，会静默变成空串：{missing}"

    def test_env_example_documents_every_var_compose_reads(self, compose_raw: str):
        """compose 读的每个变量，``.env.example`` 里都要有（注释掉也算）。

        反向的漏项是最难发现的一类：变量有默认值所以一切正常，而用户永远不知道
        它可以调——比如"怎么开给局域网"。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for name in compose_vars(compose_raw):
            assert re.search(rf"^#?\s*{name}=", text, re.M), f".env.example 没提到 {name}"


# =============================================================================
# 容器里的 UID
# =============================================================================


class TestContainerUid:
    """UID 在 Dockerfile、drill.sh、xc、以及数据目录报错文案里都出现。

    写四遍的结果是改了一处忘三处，而症状只在真部署时出现——容器起来就是
    Permission denied，而那句报错离根因很远。
    """

    def test_dockerfile_uses_the_contract_uid(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert f"--uid {C.CONTAINER_UID}" in text, (
            f"Dockerfile 的 useradd 没有用 contract 里的 {C.CONTAINER_UID}"
        )

    def test_drill_uses_the_same_uid(self):
        assert str(C.CONTAINER_UID) in DRILL_SH.read_text(encoding="utf-8")

    def test_error_message_names_the_uid_and_the_command(self, tmp_path: Path):
        """数据目录不可写时，报错要给出**可以直接粘贴执行**的命令。

        默认的 ``PermissionError: /data/backups`` 没人会从它想到"去宿主上 chown"。
        """
        import os

        from xingcha.config import DataDirNotWritable, Settings

        if os.geteuid() == 0:
            pytest.skip("root 写得进任何目录，造不出这个失败")

        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            with pytest.raises(DataDirNotWritable) as e:
                Settings(data_dir=locked / "data").ensure_data_dir()
        finally:
            locked.chmod(0o700)

        msg = str(e.value)
        assert str(C.CONTAINER_UID) in msg
        assert "chown" in msg


# =============================================================================
# 编排本身的安全形状
# =============================================================================


class TestOrchestrationShape:
    def test_the_default_bind_is_loopback_only(self, compose_raw: str):
        """**默认只绑回环。这是一条安全属性，不是风格。**

        映射出去的端口走 Docker 的 DOCKER-USER 链，**会绕过 ufw**——你在防火墙里
        写的 deny 对它无效。所以"开给局域网"必须是 .env 里的一次显式选择，
        装上就默认对外是不可接受的。

        （Caddy 在的时候 xingcha 一个宿主端口都不发布，那条更强的性质随 Caddy 一起
        没了；能保留的最强形式就是这条默认值。）
        """
        assert compose_vars(compose_raw)["XINGCHA_BIND_ADDR"] == "127.0.0.1"

    def test_only_one_service(self, compose: dict):
        assert list(compose["services"]) == ["xingcha"]

    def test_env_file_carries_the_host_env_into_the_container(self, compose: dict):
        """后台密码与各厂商的 key 变量都靠它进容器——上游页读的就是这些。"""
        assert compose["services"]["xingcha"]["env_file"] == ["../.env"]

    def test_orchestration_owned_settings_are_not_left_to_the_env_file(self, compose: dict):
        """这几项由编排决定，必须写在 ``environment:`` 里。

        compose 的 ``environment`` 永远覆盖 ``env_file``，所以写在这里就意味着
        ".env 里不小心留了一行 XINGCHA_PORT" 不会把容器内的监听端口改掉——
        而那种改动的症状是健康检查一直失败，看不出跟 .env 有关。
        """
        env = compose["services"]["xingcha"]["environment"]
        for key in ("XINGCHA_DATA_DIR", "XINGCHA_HOST", "XINGCHA_PORT"):
            assert key in env, f"{key} 应该由编排固定，而不是留给 .env"
        assert env["XINGCHA_DATA_DIR"] == "/data"
        assert env["XINGCHA_HOST"] == "0.0.0.0"

    def test_public_url_is_http_now_that_tls_is_gone(self, compose: dict):
        """后台展示的 curl 示例必须与真实协议一致。

        写成 https 的话用户复制那条命令会直接连不上，而错误信息（连接被重置）
        完全指不到"协议写错了"。
        """
        assert compose["services"]["xingcha"]["environment"]["XINGCHA_PUBLIC_URL"].startswith(
            "http://"
        )

    def test_logging_is_capped(self, compose: dict):
        """A8：日志不限大小会涨满磁盘，而整个产品就是一个 SQLite 文件——
        磁盘一满就是写失败 + 迁移失败 + 无法启动，根因在监控里完全看不见。"""
        for name, svc in compose["services"].items():
            opts = (svc.get("logging") or {}).get("options") or {}
            assert opts.get("max-size"), f"{name} 没有 max-size，日志会涨满磁盘"

    def test_data_mount_defaults_to_the_host_dir_and_can_be_a_named_volume(
        self, compose: dict, compose_raw: str
    ):
        """Windows 上必须能换成命名卷。

        Docker Desktop 经 9p/virtiofs 把 Windows 目录挂进虚拟机，那是网络文件系统，
        **SQLite 的 WAL 在上面会静默降级**——症状是零星的 database is locked，
        只在并发写时出现，压不出来也难复现。
        """
        assert compose_vars(compose_raw)["XINGCHA_DATA_MOUNT"] == "../data"
        assert "xingcha_data" in compose.get("volumes", {}), (
            "命名卷没声明，Windows 上设 XINGCHA_DATA_MOUNT=xingcha_data 会失败"
        )

    def test_healthcheck_hits_the_app_not_the_port(self, compose: dict):
        """探活要打 ``/healthz``。只探端口的话，一个卡在迁移里的进程也算健康。"""
        test = compose["services"]["xingcha"]["healthcheck"]["test"]
        assert any("/healthz" in str(x) for x in test)

    def test_image_build_is_locked(self):
        """镜像必须按锁文件装（A12）。

        ``--no-editable`` 尤其不能丢：uv sync 默认装 editable、指向构建阶段的
        /build/src，而那个目录在 runtime 阶段不存在——构建照样成功，镜像里
        ``import xingcha`` 直接失败。
        """
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "uv sync" in text and "--frozen" in text
        assert "--no-editable" in text
        assert (ROOT / "uv.lock").exists(), "uv.lock 必须入库，否则 --frozen 无从谈起"


# =============================================================================
# 运维脚本
# =============================================================================


class TestOpsScripts:
    """两个脚本必须真的等价，且不能把踩过的坑重新引进来。

    这一类跨文件契约没有任何运行时检查：脚本改坏了，全套单测照绿、镜像照建，
    只有真的去部署时才发现——而那通常是在别人的机器上。
    """

    @pytest.fixture(scope="class")
    def sh(self) -> str:
        return (ROOT / "deploy" / "xc").read_text(encoding="utf-8")

    @staticmethod
    def _code(script: str) -> str:
        """去掉注释行。

        注释里出现 `10001` 或 `grep -oP` 是**正当的**——那正是在讲"为什么不要这么
        做"。下面几条断言针对的是真正执行的行，不是文档。
        """
        return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))

    @pytest.fixture(scope="class")
    def ps1(self) -> str:
        return (ROOT / "deploy" / "xc.ps1").read_text(encoding="utf-8")

    def test_both_scripts_exist_and_bash_one_is_executable(self, sh: str, ps1: str):
        assert sh and ps1
        assert (ROOT / "deploy" / "xc").stat().st_mode & 0o111, "deploy/xc 没有执行位"

    @pytest.mark.parametrize("verb", ["start", "redeploy", "stop", "logs", "status"])
    def test_the_two_scripts_offer_the_same_verbs(self, sh: str, ps1: str, verb: str):
        assert f"  {verb})" in sh or f"\n  {verb})" in sh, f"deploy/xc 缺 {verb}"
        assert f"'{verb}'" in ps1, f"deploy/xc.ps1 缺 {verb}"

    def test_bash_script_reads_the_uid_from_the_contract(self, sh: str):
        """UID 不能写死。写死的那一刻它就开始和 Dockerfile 漂移，而症状是
        PermissionError + 无限重启，看不出跟这个数字有关。"""
        assert "from_contract CONTAINER_UID" in sh
        code = self._code(sh)
        assert str(C.CONTAINER_UID) not in code, f"UID {C.CONTAINER_UID} 被写死在脚本里"

    def test_bash_script_does_not_use_grep_dash_capital_p(self, sh: str):
        """不许用 `grep -oP ... \\K`。

        这台机器上 `grep` 是 ugrep 的 shim，PCRE 的 \\K 不生效、返回 1，
        配上 `set -e` 就是**整个脚本静默退出**：没有任何输出，看起来像脚本坏了。
        """
        assert "-oP" not in self._code(sh), "换成 sed —— grep -oP 在 ugrep 上不可靠"

    def test_redeploy_stops_before_it_deletes(self, sh: str):
        """**先 down 再删。** 反过来的话进程还握着已删除的 inode 继续写，
        表现为"我删了库，密码却还在"——实际踩出来的。"""
        block = sh[sh.index("  redeploy)") : sh.index("  stop)")]
        # 匹配的是 `down`（脚本里的包装函数，会带上 --remove-orphans），不是 `c down`
        assert re.search(r"^\s*down\s*$", block, re.M), "redeploy 里没有停容器这一步"
        assert block.index("down") < block.index("rm -rf"), "redeploy 在 down 之前就删了 data"

    def test_redeploy_asks_before_destroying_data(self, sh: str, ps1: str):
        assert "read -r answer" in sh and '"$answer" = yes' in sh
        assert "Read-Host" in ps1 and "-ne 'yes'" in ps1

    def test_powershell_tells_windows_users_to_use_a_named_volume(self, ps1: str):
        """Windows 上 /data **不能**走宿主目录：Docker Desktop 经 9p/virtiofs 挂进
        虚拟机，那是网络文件系统，SQLite 的 WAL 在上面会静默降级。

        编排本身只有一份（没有 Windows 专用叠加层了），所以这件事只能靠 .env 里的
        ``XINGCHA_DATA_MOUNT=xingcha_data``——而**脚本必须把它说出来**，否则
        Windows 用户按默认值跑起来，几周后开始遇到无法复现的 database is locked。
        """
        assert "XINGCHA_DATA_MOUNT=xingcha_data" in ps1
        assert "WAL" in ps1, "没解释为什么必须换成命名卷，下一个人会把它改回去"

    def test_powershell_passes_the_compose_file_and_env_file_explicitly(self, ps1: str):
        """不能依赖 .env 里的 COMPOSE_FILE。

        它的分隔符跟着 os.pathsep 走：Linux ``:``、Windows ``;``；而且它意味着
        "在哪个目录敲命令"会改变结果——正是让人在 deploy/ 里 restart 失败的原因。
        """
        assert "'deploy/docker-compose.yml'" in ps1
        assert "'--env-file', '.env'" in ps1

    def test_bash_and_powershell_agree_on_the_compose_invocation(self, sh: str, ps1: str):
        """两个脚本必须用**同一份** compose 文件与同一个 .env。

        分开写的话它们会各自漂移，而症状是"我在 Windows 上跑起来的那套和 Linux
        上不是一个东西"——两边都"能用"，但配置来源不同。
        """
        for token in ("deploy/docker-compose.yml", "--env-file"):
            assert token in sh, f"deploy/xc 里没有 {token}"
            assert token in ps1, f"deploy/xc.ps1 里没有 {token}"

    def test_powershell_avoids_the_reserved_host_variable(self, ps1: str):
        """``$Host`` 是 PowerShell 的自动变量，赋值会报错。"""
        assert "$host =" not in ps1 and "$Host =" not in ps1

    def test_both_scripts_remove_orphans(self, sh: str, ps1: str):
        """``up`` / ``down`` 必须带 ``--remove-orphans``。

        不带的话，**从 compose 里删掉一个服务之后它的容器会永远留着**：compose 只
        管自己现在声明的服务，那个孤儿既不会被 down 掉，也不会被 up 重建，就一直
        跑着占端口。实际踩过：Caddy 从编排里去掉之后 xingcha-caddy-1 还在跑、还占着
        8443，而 ``docker compose ps`` 看起来一切正常。
        """
        for name, text in (("deploy/xc", sh), ("deploy/xc.ps1", ps1)):
            assert "--remove-orphans" in text, f"{name} 的 up/down 没带 --remove-orphans"


# =============================================================================
# 共享网关的叠加层
# =============================================================================


class TestEdgeOverlay:
    """接进共享网关（../edge）的可选叠加层。

    为什么这一份第二个 compose 文件是正当的，而此前那三份不是：**它是拓扑，不是
    参数。** 独立跑（自己发布端口、明文 HTTP）与挂在网关后面（零宿主端口、HTTPS）
    是两种结构；`networks` 这类字段没法靠 ``${}`` 条件出现，硬塞进一份文件只会
    得到一堆互相排斥的变量。
    """

    @pytest.fixture(scope="class")
    def edge(self) -> dict:
        return yaml.safe_load(
            (ROOT / "deploy" / "docker-compose.edge.yml")
            .read_text(encoding="utf-8")
            .replace("!override", "")
        )

    def test_it_publishes_no_host_ports(self, edge: dict):
        """挂在网关后面时**必须零宿主端口**。

        留着直连口等于同时存在两条入口，而两条的 TLS 状态完全不同：一条经网关是
        HTTPS，一条直连是明文——而且直连那条走 DOCKER-USER 链，绕过 ufw。
        """
        assert edge["services"]["xingcha"]["ports"] == []

    def test_ports_are_overridden_not_appended(self):
        """compose 对 ports 默认是**追加**。少了 `!override`，基础那份的端口映射
        会继续存在，上面那条性质就没了——而配置看起来完全正常。"""
        raw = (ROOT / "deploy" / "docker-compose.edge.yml").read_text(encoding="utf-8")
        assert "ports: !override" in raw

    def test_it_joins_the_shared_external_network(self, edge: dict):
        """网络必须是 external。让 compose 自建的话名字会带项目名前缀
        （xingcha_edge），网关根本不在那个网络里——症状是网关一直 502
        而两边容器都"在跑"。"""
        assert edge["networks"]["default"] == {"name": "edge", "external": True}

    def test_it_trusts_the_gateway_for_forwarded_proto(self, edge: dict):
        """不信任 ``X-Forwarded-Proto`` 的话**会话 cookie 不带 Secure**。

        网关到应用这一跳是 http，应用看到的 scheme 就是 http。浏览器那半段明明是
        HTTPS，却少了一层保护——而功能完全正常，没人会注意到。
        """
        assert edge["services"]["xingcha"]["environment"]["XINGCHA_TRUSTED_PROXIES"] == "*"

    def test_the_base_deployment_trusts_nobody(self, compose: dict):
        """默认部署里**不能**有这一项。

        无条件信任 X-Forwarded-* 意味着任何能直连应用的人都能左右它，而基础部署
        恰恰是要发布宿主端口的。
        """
        assert "XINGCHA_TRUSTED_PROXIES" not in compose["services"]["xingcha"]["environment"]

    def test_public_url_switches_to_https(self, edge: dict):
        assert edge["services"]["xingcha"]["environment"]["XINGCHA_PUBLIC_URL"].startswith(
            "https://"
        )


# =============================================================================
# 编排变量与应用设置的对齐
# =============================================================================


class TestEnvNameAlignment:
    """``.env`` 里每个 ``XINGCHA_*`` 都必须被应用**认识**。

    ``env_file`` 把整份 ``.env`` 注进容器，而应用会对不认识的 ``XINGCHA_*``
    告警"拼错了？"。于是编排层自己的变量（端口、绑定地址、挂载点）会被误报——
    用户配得完全正确却被告知拼错。

    这条假警报的真正代价是**它会训练人忽略这类警告**，而那条警告存在的理由恰恰是
    "你以为设了 XINGCHA_MAX_CONCURENCY（少一个 R），实际跑的是默认值"。
    误报一次，真报就没人看了。

    实际发生过：收敛部署时新增了 XINGCHA_BIND_ADDR / WEB_PORT / WEB_HOST，
    于是每次启动打三条"拼错了？"。
    """

    @staticmethod
    def _referenced() -> set[str]:
        """部署产物里出现的所有 ``XINGCHA_*`` 名字（只看可执行部分）。"""
        names: set[str] = set()
        for name in ("docker-compose.yml", "docker-compose.edge.yml", "xc", "xc.ps1"):
            text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
            code = "\n".join(
                ln for ln in text.splitlines() if not ln.lstrip().startswith(("#", "//", "<#"))
            )
            names.update(re.findall(r"\bXINGCHA_[A-Z_]+\b", code))
        return names

    def test_every_referenced_var_is_known_to_the_app(self):
        from xingcha.config import _KNOWN_ENV_NAMES

        unknown = sorted(self._referenced() - _KNOWN_ENV_NAMES)
        assert not unknown, (
            f"这些变量会在每次启动时被误报成「拼错了？」：{unknown}。"
            "要么它是应用设置（加到 Settings），要么是编排层的"
            "（登记到 contract.ORCHESTRATION_ENV_NAMES）。"
        )

    def test_the_orchestration_set_has_no_dead_entries(self):
        """登记了却没人用的名字要清掉——否则它会掩盖真正的拼写错误。"""
        referenced = self._referenced()
        dead = sorted(n for n in C.ORCHESTRATION_ENV_NAMES if n not in referenced)
        assert not dead, f"ORCHESTRATION_ENV_NAMES 里这些已经没人引用：{dead}"

    def test_a_real_typo_is_still_reported(self):
        """放行编排变量**不能**顺手把真错误也放过。"""
        from xingcha.config import _KNOWN_ENV_NAMES

        assert "XINGCHA_MAX_CONCURENCY" not in _KNOWN_ENV_NAMES

    def test_the_env_example_only_documents_known_names(self):
        text = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
        from xingcha.config import _KNOWN_ENV_NAMES

        documented = set(re.findall(r"^#?\s*(XINGCHA_[A-Z_]+)=", text, re.M))
        unknown = sorted(documented - _KNOWN_ENV_NAMES)
        assert not unknown, f".env.example 里这些名字应用不认识：{unknown}"
