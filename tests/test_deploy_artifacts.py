"""部署产物的结构约束。

------------------------------------------------------------------------------
为什么这一层非有不可
------------------------------------------------------------------------------

Dockerfile / docker-compose.yml / .env.example / xc / xc.bat / drill.sh 之间有
**跨文件的隐式契约**，而它们互相之间没有任何类型检查：compose 读一个环境变量，
.env.example 负责告诉用户要填，xc 负责把 compose 文件与 .env 传对，drill.sh 负责
用同一套路径找到容器。

任意一处漏掉，**单元测试全绿、镜像构建成功、代码审查也看不出来**——症状只在真的
``docker compose up`` 时出现。实际发生过两次：一次是 Caddyfile 引用了 compose 从未
传过的变量，配置解析失败进无限重启；一次是 ``uv sync`` 默认装 editable，构建绿灯而
镜像里 ``import xingcha`` 直接失败。

编排此前是三个 compose 文件（生产 / 局域网 / Windows）加两份 Caddyfile，现在收敛成
**一个 compose 文件、一个容器**，外加一个可选的拓扑叠加层（挂不挂本机那个 Caddy）。
其余差异全部变成 ``.env`` 里的变量，这些断言也跟着换成守新形状的。

没装 docker 的机器（Windows，或干净的 Linux）走的是**另一条路**：不打镜像，
``deploy/windows/xc.bat`` 用 uv 在宿主直接起进程
（理由见 deploy/README.md——Docker Desktop 那条路上 data 不能放宿主目录，SQLite 的
WAL 会静默降级）。两条路共用同一份 ``.env.example`` 与同一个 ``data/`` 位置，
所以这里也要守住"别悄悄变成两套配置"。

**这里只做静态结构检查**，不起容器——起容器的验证是 deploy/linux/drill.sh、CI 的
"整栈真的起得来"，与真机演练的事。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from conftest import git_file_mode, posix_only
from xingcha import contract as C

ROOT = Path(__file__).resolve().parent.parent
#: deploy/ 按角色分三个目录：linux/ 是 docker 那条路（编排 + xc + drill），
#: windows/ 是 uv 本地直跑那条（只有 xc.bat），edge/ 是网关（Caddy 单文件，
#: 两条路共用同一份 Caddyfile）。**两条路共用的留在 deploy/ 顶层**——.env.example
#: 一份、README.md 一份，那正是它们不该分家的理由；而网关那份说明跟着网关走
#: （deploy/edge/CADDY.md），它说的就是那个目录里的东西。
DEPLOY = ROOT / "deploy"
LINUX = DEPLOY / "linux"
WINDOWS = DEPLOY / "windows"

EDGE = DEPLOY / "edge"

COMPOSE = LINUX / "docker-compose.yml"
GATEWAY_COMPOSE = LINUX / "docker-compose.gateway.yml"
DOCKERFILE = ROOT / "Dockerfile"
ENV_EXAMPLE = DEPLOY / ".env.example"
DRILL_SH = LINUX / "drill.sh"
XC = LINUX / "xc"
XC_BAT = WINDOWS / "xc.bat"
CADDYFILE = EDGE / "Caddyfile"
EDGE_SH = EDGE / "edge"
EDGE_BAT = EDGE / "edge.bat"
EDGE_PS1 = EDGE / "edge.ps1"
XC_PS1 = WINDOWS / "xc.ps1"
COMMON_PS1 = DEPLOY / "_common.ps1"


def _ps1_code(path: Path) -> str:
    """``.ps1`` 里真正会执行的部分：剥掉 ``<# ... #>`` 块注释与 ``#`` 行注释。

    这些脚本的注释里会**提到** docker、``??`` 之类的字眼——那正是在解释"为什么不用
    它"。拿整份文本做断言的话，写得越清楚越容易被自己的测试抓住。
    """
    body = re.sub(r"<#.*?#>", "", path.read_text(encoding="utf-8-sig"), flags=re.S)
    return "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))


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

    #: 允许存在的 compose 文件，**闭集**：基础一份 + 一个可选的拓扑叠加层。
    #:
    #: 历史上到过三份（生产/局域网/Windows）。那三份的差异全是**参数**（端口、
    #: 地址、数据位置），参数该做成变量，所以收敛掉了。
    #:
    #: 留下的这个叠加层是**拓扑**：独立跑（明文 HTTP，端口可以开给局域网）与挂在
    #: 本机那个网关后面（端口强制只绑回环、对外 HTTPS）。它改的两项要么全有要么
    #: 全无，而 ${} 表达不了"成套出现"——只改一项的后果是安静的。
    ALLOWED: ClassVar[list[str]] = [
        "deploy/linux/docker-compose.gateway.yml",
        "deploy/linux/docker-compose.yml",
    ]

    def test_compose_files_are_a_closed_set(self):
        found = sorted(
            q.relative_to(ROOT).as_posix()
            for q in ROOT.rglob("docker-compose*.y*ml")
            if ".venv" not in q.parts and ".git" not in q.parts
        )
        assert found == self.ALLOWED, f"编排文件与闭集不符：{found}"

    def test_the_gateway_is_a_host_process_not_a_service(self):
        """网关**不进编排**：它是宿主上的一个进程（Caddy 单文件），不是容器。

        配置确实在这个仓库里（deploy/edge/Caddyfile，两条路共用），但把它做成
        compose 里的一个服务会把 docker 变成"要 HTTPS 就必须装 docker"——而这条路
        存在的全部理由就是那台机器上没有 docker，也不该为了一个反代装一层虚拟机。

        Caddyfile **只能有一份**：两份的结果一定是其中一份先过期，而两边都"能跑"，
        差别只在证书或某个头上，不到出事那天不会有人发现。
        """
        found = sorted(
            q.relative_to(ROOT).as_posix()
            for q in ROOT.rglob("Caddyfile*")
            if ".venv" not in q.parts and ".git" not in q.parts
        )
        assert found == ["deploy/edge/Caddyfile"], f"Caddyfile 不止一份或不在 deploy/edge/：{found}"
        compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        assert "caddy" not in compose["services"], "网关不该是编排里的一个服务"
        assert "caddy" not in GATEWAY_COMPOSE.read_text(encoding="utf-8")

    def test_project_name_is_pinned(self, compose: dict):
        """不写死 ``name:`` 的话 compose 用 compose 文件所在目录名，也就是
        ``deploy``——容器会叫 deploy-xingcha-1，而文档、脚本与运维记忆里全是
        xingcha-xingcha-1。"""
        assert compose.get("name") == "xingcha"

    def test_build_context_is_the_repo_root(self, compose: dict):
        """compose 里的相对路径以**compose 文件所在目录**为基准。

        断言的是"解析出来是仓库根"，不是字面的 ``../..``：编排按系统分目录之后
        这个前缀已经变过一次（deploy/ → deploy/linux/），而写死字面量的断言在那次
        移动里只会红一次，然后被人改成新的字面量——它守不住"context 必须是仓库根"
        这件真正要紧的事（context 错了的症状是 COPY 找不到源码，报错离根因很远）。
        """
        ctx = compose["services"]["xingcha"]["build"]["context"]
        assert (COMPOSE.parent / ctx).resolve() == ROOT, f"build context 不是仓库根：{ctx}"


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

    #: compose 会读、但**用户不该设**的变量：由 deploy/linux/xc 推导后导出。
    #:
    #: 它们不进 .env.example —— 列在那里等于邀请用户去设一个会被覆盖的值，
    #: 而"我明明设了却不生效"是最费时的一类困惑。
    DERIVED: ClassVar[frozenset[str]] = frozenset(
        {
            "XINGCHA_BIND_ADDR",  # 由 XINGCHA_WEB_HOST 推出来
            "XINGCHA_PUBLIC_PORT",  # 走网关时固定 8443（网关那条 docker run 的 -p）
        }
    )

    def test_env_example_documents_every_var_compose_reads(self, compose_raw: str):
        """compose 读的每个变量，``.env.example`` 里都要有（注释掉也算）。

        反向的漏项是最难发现的一类：变量有默认值所以一切正常，而用户永远不知道
        它可以调——比如"怎么开给局域网"。

        派生的那几个是例外，见 :data:`DERIVED`。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for name in set(compose_vars(compose_raw)) - self.DERIVED:
            assert re.search(rf"^#?\s*{name}=", text, re.M), f".env.example 没提到 {name}"

    def test_derived_vars_are_not_offered_to_the_user(self):
        """派生变量**不能**出现在 .env.example 里。

        列出来就是邀请人去设一个会被 deploy/linux/xc 覆盖的值——"我明明设了却不生效"
        是最费时的一类困惑，而这里没有任何提示能让人想到是被覆盖了。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for name in self.DERIVED:
            assert not re.search(rf"^#?\s*{name}=", text, re.M), (
                f"{name} 是派生值，不该出现在 .env.example 里"
            )


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

    @posix_only
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
    def test_standalone_binds_to_loopback_by_default(self, compose_raw: str):
        """独立跑时**默认只绑回环**。

        这条默认值是安全属性：映射出去的端口走 Docker 的 DOCKER-USER 链，
        **绕过 ufw**——防火墙里写的 deny 对它无效。所以"开给局域网"必须是一次
        显式选择，装上就默认对外是不可接受的。
        """
        assert compose_vars(compose_raw)["XINGCHA_BIND_ADDR"] == "127.0.0.1"

    def test_the_base_file_trusts_no_proxy(self, compose: dict):
        """独立跑时**不能**信任 X-Forwarded-*。

        那时应用是直接可达的，无条件读那个头等于让任何人左右 cookie 的 Secure 标记。
        只有叠加层配得上 ``*``——那时端口只绑回环，唯一入口是本机的网关。
        """
        assert "XINGCHA_TRUSTED_PROXIES" not in compose["services"]["xingcha"]["environment"]

    def test_only_one_service(self, compose: dict):
        assert list(compose["services"]) == ["xingcha"]

    def test_env_file_carries_the_host_env_into_the_container(self, compose: dict):
        """后台密码与各厂商的 key 变量都靠它进容器——上游页读的就是这些。

        同样按**解析后的路径**断言：必须正好是仓库根的 ``.env``。指到别处的症状是
        "我在 .env 里填了密码，容器里却没有"，而 compose 不会有任何抱怨。
        """
        env_file = compose["services"]["xingcha"]["env_file"]
        assert len(env_file) == 1, f"env_file 不止一份，会有先后覆盖：{env_file}"
        assert (COMPOSE.parent / env_file[0]).resolve() == ROOT / ".env"

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

    def test_the_gateway_overlay_only_sets_environment(self):
        """叠加层**只改 environment**，不碰 ports / networks。

        网关是宿主进程，容器照样要发布那个端口（只是绑在 127.0.0.1 上，由
        deploy/linux/xc 的 derive_bind_addr 保证）。哪天有人在这里把 ports 清成
        `!override []`，网关就再也连不上了——而症状是"容器 healthy、网关一直 502"，
        两边单独看都正常。
        """
        overlay = yaml.safe_load(GATEWAY_COMPOSE.read_text(encoding="utf-8"))
        svc = overlay["services"]["xingcha"]
        assert set(svc) == {"environment"}, f"叠加层动了别的东西：{sorted(svc)}"
        assert "networks" not in overlay, "网关不在 docker 网络里，它是宿主进程"

    def test_the_gateway_overlay_sets_both_halves_of_the_https_story(self):
        """两项必须**成套出现**，缺一项都会出安静的问题。

        - 不信任 ``X-Forwarded-Proto``：应用以为自己在 http 上，**会话 cookie 不带
          Secure**——浏览器那半段明明是 HTTPS，却少一层保护，而功能完全正常。
        - PUBLIC_URL 还是 http：后台印出的 curl 示例连不上，而错误信息指不到
          "该走网关"。

        ``*`` 在这里是安全的，前提写在叠加层的注释里：那个端口只绑 127.0.0.1，
        唯一能发这个头的就是本机上的 Caddy。宿主经发布端口连进来时容器看到的源地址
        是 docker 网桥的网关（172.x.0.1，随项目网络变），写死必然有一天对不上。
        """
        overlay = yaml.safe_load(GATEWAY_COMPOSE.read_text(encoding="utf-8"))
        env = overlay["services"]["xingcha"]["environment"]
        assert env["XINGCHA_TRUSTED_PROXIES"] == "*"
        assert env["XINGCHA_PUBLIC_URL"].startswith("https://")

    def test_the_base_public_url_is_http(self, compose: dict):
        """独立跑时展示的 curl 示例必须是 http —— 那时确实没有 TLS。

        写成 https 的话用户复制那条命令会连不上，而错误信息（连接被重置）完全指不到
        "协议写错了"。
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
        """默认是仓库根下的 ``data/``，而且必须能换成命名卷。

        默认走宿主目录，是因为那样备份就是几个能直接拷走的文件；留着命名卷这条
        退路，是因为**宿主目录不一定适合放 SQLite**：网络文件系统上 WAL 会静默
        降级——症状是零星的 database is locked，只在并发写时出现，压不出来也难复现。

        默认值同样按解析后的路径断言，理由见 test_build_context_is_the_repo_root。
        """
        default = compose_vars(compose_raw)["XINGCHA_DATA_MOUNT"]
        # 没有默认值的话它是 None（空 .env 就挂不上 data 了）——那条由
        # test_every_var_has_a_default_so_an_empty_env_still_works 单独守着，
        # 这里先断言一次，顺带让下面的路径拼接有确定的类型。
        assert default is not None, "XINGCHA_DATA_MOUNT 没有默认值"
        assert (COMPOSE.parent / default).resolve() == ROOT / "data", (
            f"data 默认挂载点不是仓库根下的 data/：{default}"
        )
        assert "xingcha_data" in compose.get("volumes", {}), (
            "命名卷没声明，设 XINGCHA_DATA_MOUNT=xingcha_data 会失败"
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
    """docker 那条路的运维脚本（``deploy/linux/xc``）不能把踩过的坑重新引进来。

    这一类跨文件契约没有任何运行时检查：脚本改坏了，全套单测照绿、镜像照建，
    只有真的去部署时才发现——而那通常是在别人的机器上。
    """

    @pytest.fixture(scope="class")
    def sh(self) -> str:
        return XC.read_text(encoding="utf-8")

    @staticmethod
    def _code(script: str) -> str:
        """去掉注释行。

        注释里出现 `10001` 或 `grep -oP` 是**正当的**——那正是在讲"为什么不要这么
        做"。下面几条断言针对的是真正执行的行，不是文档。
        """
        return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))

    def test_the_script_exists_and_is_executable(self, sh: str):
        assert sh
        # 问 git 索引而不是工作区：要保证的是"Linux 上 clone 下来能直接跑"，
        # 而 Windows 的文件系统不带执行位，stat() 在那里必然失败。见 git_file_mode。
        assert git_file_mode("deploy/linux/xc") == "100755", "deploy/linux/xc 没有执行位"

    @pytest.mark.parametrize("verb", ["start", "redeploy", "stop", "logs", "status"])
    def test_the_script_offers_every_documented_verb(self, sh: str, verb: str):
        assert f"  {verb})" in sh or f"\n  {verb})" in sh, f"deploy/linux/xc 缺 {verb}"

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

    def test_redeploy_asks_before_destroying_data(self, sh: str):
        assert "read -r answer" in sh and '"$answer" = yes' in sh

    def test_the_script_passes_the_compose_file_and_env_file_explicitly(self, sh: str):
        """不能依赖 .env 里的 COMPOSE_FILE。

        它的分隔符跟着 os.pathsep 走：Linux ``:``、Windows ``;``；而且它意味着
        "在哪个目录敲命令"会改变结果——正是让人在 deploy/ 里 restart 失败的原因。
        """
        code = self._code(sh)
        assert "deploy/linux/docker-compose.yml" in code
        assert "--env-file .env" in code

    def test_the_script_checks_the_gateway_when_one_is_configured(self, sh: str):
        """配了网关就必须在启动前检查它；没配则不该被一个不相干的容器挡住。

        不检查的症状是"容器 healthy 却什么都打不开"——那种状态最难认，因为每一层
        单独看都正常。实际踩过一次（网关一直 502，而容器 healthy）。
        """
        code = self._code(sh)
        assert "XINGCHA_GATEWAY" in code, "deploy/linux/xc 没读拓扑开关"
        # 网关是宿主进程，所以判据是"那个端口有没有人听"，不是查某个容器在不在。
        # 认端口而不认进程名，换成 nginx / Traefik 时这里才不用跟着改。
        assert "ss -tln" in code, "deploy/linux/xc 没检查网关端口在不在"

    def test_the_gateway_forces_the_port_onto_loopback(self, sh: str):
        """挂网关时绑定地址**必须被强制成 127.0.0.1**，不看 XINGCHA_WEB_HOST。

        少了这一条：有人把 WEB_HOST 填成内网 IP（那是它本来的用法——证书上的名字），
        容器那个端口就跟着绑到 0.0.0.0 上，于是 TLS 旁边多出一条明文入口，谁都能连、
        还绕过 ufw。而页面、证书、网关全都正常，没有任何现象指向这件事。

        它同时也是叠加层敢写 ``XINGCHA_TRUSTED_PROXIES=*`` 的前提。
        """
        code = self._code(sh)
        body = code[code.index("derive_bind_addr()") : code.index("export XINGCHA_BIND_ADDR")]
        assert re.search(r"\[ -z \"\$GATEWAY\" \] \|\| \{ printf '127\.0\.0\.1'", body), (
            "derive_bind_addr 没有在配了网关时强制回环"
        )

    def test_the_script_removes_orphans(self, sh: str):
        """``up`` / ``down`` 必须带 ``--remove-orphans``。

        不带的话，**从 compose 里删掉一个服务之后它的容器会永远留着**：compose 只
        管自己现在声明的服务，那个孤儿既不会被 down 掉，也不会被 up 重建，就一直
        跑着占端口。实际踩过：Caddy 从编排里去掉之后 xingcha-caddy-1 还在跑、还占着
        8443，而 ``docker compose ps`` 看起来一切正常。
        """
        assert "--remove-orphans" in self._code(sh), "up/down 没带 --remove-orphans"


# =============================================================================
# Windows 那条路（不走 docker）
# =============================================================================


class TestWindowsEntry:
    r"""``deploy/windows/xc.bat``：uv 在宿主直接起进程。

    它守的东西和 docker 那条路完全不同，所以单独一类。**每一条都是"改坏了不会有
    任何报错、只会在那台机器上表现成别的毛病"的那种**：

    · 不 cd 回仓库根 → 在 deploy\ 下面又建一个 data\，症状是"起来了但后台是空的"；
    · 丢了 --frozen  → 部署机上顺手升了依赖，代码一字未动而行为变了；
    · 另起一份模板   → 两份 .env.example 各自漂移，两边"都能用"但不是一个东西；
    · LF 换行        → cmd.exe 的 goto 按字节偏移找标签，跳飞了也不报错。
    """

    @pytest.fixture(scope="class")
    def bat(self) -> str:
        return XC_BAT.read_text(encoding="utf-8")

    @staticmethod
    def _code(script: str) -> str:
        """去掉 ``rem`` 注释行——下面几条针对真正执行的行，不是文档。"""
        return "\n".join(
            ln for ln in script.splitlines() if not ln.lstrip().lower().startswith("rem")
        )

    def test_it_exists_and_is_the_only_windows_entry_point(self, bat: str):
        """Windows 入口是**闭集**：一个 .bat，没有别的。

        此前是 xc.ps1（docker 版）。留着的话双击哪一个会得到完全不同的部署，
        而两个都"能用"。
        """
        assert bat
        found = sorted(q.name for q in DEPLOY.rglob("*") if q.suffix.lower() in (".bat", ".ps1"))
        assert found == ["_common.ps1", "edge.bat", "edge.ps1", "xc.bat", "xc.ps1"], (
            f"deploy/ 下的 Windows 入口与闭集不符：{found}"
        )

    def test_it_uses_crlf_and_no_bom(self):
        r"""必须 CRLF、不带 BOM。

        cmd.exe 对 LF-only 批处理有历史遗留的坑：``goto`` 按字节偏移找标签，
        行尾少一个字节就可能跳飞；而 UTF-8 BOM 会让第一行变成一条不认识的命令。
        两种都不会给出像样的报错。``.gitattributes`` 钉了 eol=crlf，这里守住结果。
        """
        raw = XC_BAT.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "xc.bat 带了 UTF-8 BOM"
        assert b"\r\n" in raw, "xc.bat 不是 CRLF —— cmd 的 goto 会跳飞"
        assert b"\n" not in raw.replace(b"\r\n", b""), "xc.bat 里混了裸 LF"

    def test_it_returns_to_the_repo_root(self, bat: str):
        r"""双击时 cmd 的工作目录是脚本所在目录。

        不 cd 的话 ``.env`` 找不到、``data\`` 会建在 ``deploy\`` 下面——症状是
        "服务起来了，后台却是空的"，而两处 data 目录都真实存在。
        """
        # 真正 cd 的是 .ps1（.bat 只把活交出去），所以断言落在那边。
        assert "Join-Path $Here" in XC_PS1.read_text(encoding="utf-8-sig")

    def test_it_does_not_reach_for_docker(self, bat: str):
        """这条路的**全部意义**就是不装 docker。

        哪天有人"顺手"把 compose 调用加回来，这台机器就又需要 Docker Desktop 了，
        而那正是 data 不能放宿主目录的那条路。
        """
        # 只看真正会执行的行。注释里解释"为什么这条路不用 docker"是正当的，
        # 那正是这个脚本存在的理由，不该被自己抓住。
        code = (self._code(bat) + _ps1_code(XC_PS1)).lower()
        assert "docker" not in code, "Windows 这条路上出现了 docker 调用"

    def test_dependencies_are_locked(self, bat: str):
        """``uv sync`` 与 ``uv run`` 都要带 ``--frozen``。

        少了它 uv 会就地更新 uv.lock：代码一个字没动，跑的却是另一组依赖版本——
        和镜像那边 A12 是同一条理由（见 test_image_build_is_locked）。
        """
        code = XC_PS1.read_text(encoding="utf-8-sig")
        assert "uv sync --frozen" in code
        assert "uv run --frozen" in code

    def test_it_shares_the_one_env_template(self, bat: str):
        r"""Windows 不另起一份模板。

        两份必然各自漂移，而症状是"我在 Windows 上跑的那套和 Linux 上不是一个
        东西"：两边都能用，但配置来源不同。
        """
        assert ".env.example" in XC_PS1.read_text(encoding="utf-8-sig")
        strays = sorted(q.name for q in DEPLOY.rglob(".env*") if q.name != ".env.example")
        assert not strays, f"deploy/ 下出现了第二份 env 模板：{strays}"

    def test_listen_address_comes_only_from_the_env_file(self, bat: str):
        """不能给 ``serve`` 传 ``--host`` / ``--port``。

        传了就是第二个来源，而两个来源必然有一天不一致——症状是"我在 .env 里改了
        端口却不生效"，而两处配置单看都对。
        """
        code = XC_PS1.read_text(encoding="utf-8-sig")
        assert "xingcha serve" in code
        serve_line = next(ln for ln in code.splitlines() if "xingcha serve" in ln)
        assert "--host" not in serve_line and "--port" not in serve_line

    def test_failures_stop_the_window(self, bat: str):
        """失败路径必须 ``pause``。

        双击起来的窗口在脚本结束时立刻消失——不 pause 的话用户看到的是"闪一下就
        没了"，报错一个字都读不到。
        """
        # pause 在**启动器**里（它才是那个窗口的主人），失败路径在 .ps1 里。
        assert "pause" in self._code(bat), "xc.bat 失败时不 pause，窗口会闪一下就没"
        assert "exit 1" in XC_PS1.read_text(encoding="utf-8-sig"), (
            "xc.ps1 没有非零退出，启动器判断不出失败"
        )

    def test_it_warns_about_the_windows_firewall(self, bat: str):
        """绑 0.0.0.0 之后第一次启动会弹防火墙窗。

        点了取消的话：本机一切正常，网关那边一直 502——那个现象指不到防火墙，
        所以脚本必须在启动前把这句话说出来。
        """
        assert "防火墙" in XC_PS1.read_text(encoding="utf-8-sig")

    def test_the_address_settings_are_derived_not_asked_for(self):
        """这四项**不该再出现在模板里让用户填**——它们是推出来的。

        以前要用户自己填 XINGCHA_HOST / PORT / PUBLIC_URL / TRUSTED_PROXIES，
        代价有两个，都很安静：

        · ``XINGCHA_WEB_HOST`` 与 ``XINGCHA_HOST`` 名字像、作用完全不同（**应用根本
          不读前者**）。想"让别人能访问"而只改了前者，是个空操作，而页面上印的地址
          看起来完全正确。
        · "挂了网关"这件事要在四个地方分别表达一遍。只改一处的后果同样安静——
          PUBLIC_URL 写了 https 而 TRUSTED_PROXIES 没配，于是会话 cookie 不带
          Secure，功能全正常。

        现在三项输入（GATEWAY / WEB_HOST / WEB_PORT）推出这四项，推导在
        ``deploy/_common.ps1``（Windows）与 ``deploy/linux/xc``（docker）。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for name in ("XINGCHA_HOST", "XINGCHA_PORT", "XINGCHA_PUBLIC_URL"):
            assert not re.search(rf"^#?\s*{name}=", text, re.M), (
                f".env.example 又把 {name} 列成待填项了。它是推出来的，见 deploy/_common.ps1"
            )
        # 但必须讲清楚它们去哪了，否则读者只会以为功能少了。
        for name in ("XINGCHA_HOST", "XINGCHA_PUBLIC_URL", "XINGCHA_TRUSTED_PROXIES"):
            assert name in text, f".env.example 没解释 {name} 是怎么来的"

    def test_both_paths_derive_the_bind_address_the_same_way(self):
        """两条路的推导规则必须逐条对齐。

        对不上的症状是"我在 Windows 上跑的和 Linux 上不是一个东西"：两边都能用，
        但绑的地址、印的地址不一样，而没有任何一处会报错。
        """
        ps = COMMON_PS1.read_text(encoding="utf-8-sig")
        sh = XC.read_text(encoding="utf-8")
        for token in ("127.0.0.1", "0.0.0.0", "localhost"):
            assert token in ps and token in sh, f"{token} 只在一条路里出现"
        assert "8443" in ps, "_common.ps1 里没有网关端口"
        assert "XINGCHA_WEB_HOST" in ps and "XINGCHA_GATEWAY" in ps

    def test_the_env_template_does_not_hand_out_a_wildcard_proxy(self):
        """``XINGCHA_TRUSTED_PROXIES`` 在这条路上**不能**示范 ``*``。

        容器那边敢信任所有来源，前提是端口只绑回环、唯一入口就是本机的网关；
        这条路上星槎是宿主上的一个普通进程，填 ``*`` 等于让任何能连到它的人伪造
        X-Forwarded-Proto。``127.0.0.1`` 就够了——Caddy 就在本机。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert not re.search(r"^#?\s*XINGCHA_TRUSTED_PROXIES=\s*\*", text, re.M)


# =============================================================================
# 网关（deploy/edge）
# =============================================================================


class TestEdgeGateway:
    """网关是**宿主上的一个 Caddy 单文件**，两条部署路径共用同一份 Caddyfile。

    它和这个仓库之间有三条隐式契约，全都是"改坏了不报错、只在真访问时才表现成
    别的毛病"的那种：

    · 反代目标端口 ≠ 星槎发布的端口 → 网关一直 502，而两边单独看都在跑；
    · 站点端口 ≠ xc 印出来的端口     → 后台印一个打不开的地址，服务本身好着；
    · EDGE_HOST 没传进去             → 站点地址退化成 https://:8443，证书签不出来。
    """

    @pytest.fixture(scope="class")
    def caddyfile(self) -> str:
        return CADDYFILE.read_text(encoding="utf-8")

    def test_it_reverse_proxies_the_port_xingcha_publishes(self, caddyfile: str, compose_raw: str):
        """反代目标必须是**星槎发布的那个宿主端口**，而且只在回环上。

        对不上的症状是网关一直 502，而 `xc status` 显示容器 healthy——每一层单独看
        都正常，最难认的那类。
        """
        m = re.search(r"^\s*reverse_proxy\s+127\.0\.0\.1:(\d+)", caddyfile, re.M)
        assert m, "Caddyfile 里没有反代 127.0.0.1 的那一行"
        assert m.group(1) == compose_vars(compose_raw)["XINGCHA_WEB_PORT"], (
            "Caddyfile 的反代目标和 compose 发布的端口对不上"
        )

    def test_the_site_port_matches_what_xc_prints(self, caddyfile: str):
        """站点端口与 ``deploy/linux/xc`` 走网关时用的端口必须相等。

        两处各写一遍是有意的取舍（xc 不去解析 Caddyfile），代价由这条断言兜着——
        不一致的症状是后台印出一个打不开的地址，而服务本身完全正常。
        """
        site = re.search(r"^https://\{\$EDGE_HOST\}:(\d+)", caddyfile, re.M)
        assert site, "Caddyfile 里没有 https://{$EDGE_HOST}:<端口> 这样的站点地址"
        xc = re.search(
            r"if \[ -n \"\$GATEWAY\" \]; then\n\s*p=(\d+)", XC.read_text(encoding="utf-8")
        )
        assert xc, "deploy/linux/xc 的 public_port 里读不出网关端口"
        assert site.group(1) == xc.group(1), "Caddyfile 的站点端口和 xc 印的端口对不上"

    #: 少一行各有各的坑法，表在 deploy/edge/CADDY.md。
    REQUIRED_LINES: ClassVar[list[str]] = [
        "default_sni",  # 按 IP 访问不发 SNI，Caddy 一张证书都选不出来
        "bind 0.0.0.0",  # 否则绑到字面 IP 上，DHCP 换地址就起不来
        "tls internal",  # 公网 CA 不给私网 IP 签
        "flush_interval -1",  # 否则 SSE 被缓冲，"回答要等生成完才一次性蹦出来"
    ]

    @pytest.mark.parametrize("line", REQUIRED_LINES)
    def test_the_lines_that_cannot_be_dropped(self, caddyfile: str, line: str):
        assert line in caddyfile, f"Caddyfile 少了 {line}，症状见 deploy/edge/CADDY.md"

    def test_no_hsts_on_an_internal_ca(self, caddyfile: str):
        """内网别发 HSTS。

        零收益（没有可降级的 http 入口），代价却很实：它让浏览器**拒绝**你点
        "继续前往"，而新设备在装根证书之前必然撞证书警告——"点一下继续"变成
        "这个站点你今天进不去了"，且没有任何提示说原因是一个响应头。
        """
        assert "Strict-Transport-Security" not in caddyfile

    def test_both_scripts_pass_the_host_in_from_the_one_source(self):
        """``EDGE_HOST`` 必须由脚本传进去，且来源只有仓库根 .env 的 WEB_HOST。

        Caddyfile 里的 ``{$VAR}`` 读的是**进程环境变量**，不是文件里的什么插值；
        空的话站点地址退化成 ``https://:8443``，证书签不出来而报错离根因很远。
        另设一个变量则是第二个来源——两个来源必然有一天不一致，而那时页面上印的
        地址和证书上的名字对不上。
        """
        for path in (EDGE_SH, EDGE_PS1):
            text = path.read_text(encoding="utf-8-sig")
            assert "EDGE_HOST" in text, f"{path.name} 没传 EDGE_HOST"
            assert "XINGCHA_WEB_HOST" in text, f"{path.name} 没从 .env 读 XINGCHA_WEB_HOST"

    def test_the_shell_script_is_executable_with_a_shebang(self):
        assert git_file_mode("deploy/edge/edge") == "100755", "deploy/edge/edge 没有可执行位"
        assert EDGE_SH.read_text(encoding="utf-8").startswith("#!"), "缺 shebang"

    def test_the_shell_script_does_not_use_grep_dash_capital_p(self):
        """和 xc 同一条理由：这台机器上 ``grep`` 是 ugrep 的 shim，PCRE 的 ``\\K``
        不生效、返回 1，配上 ``set -e`` 就是整个脚本静默退出。"""
        code = "\n".join(
            ln
            for ln in EDGE_SH.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "-oP" not in code

    def test_the_bat_uses_crlf_and_no_bom(self):
        r"""cmd.exe 的 ``goto`` 按字节偏移找标签，行尾少一个字节就可能跳飞；
        UTF-8 BOM 会让第一行变成一条不认识的命令——实测 ``'﻿@echo' 不是命令``，
        而且 ``@echo off`` 因此失效，之后每一行都回显。两种都不会给出像样的报错。"""
        for path in (EDGE_BAT, XC_BAT):
            raw = path.read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 带了 UTF-8 BOM"
            assert b"\r\n" in raw, f"{path.name} 不是 CRLF —— cmd 的 goto 会跳飞"
            assert b"\n" not in raw.replace(b"\r\n", b""), f"{path.name} 里混了裸 LF"

    @pytest.mark.parametrize("path", [EDGE_BAT, XC_BAT], ids=lambda p: p.name)
    def test_the_bat_is_pure_ascii(self, path: Path):
        """``.bat`` 里**一个非 ASCII 字节都不许有**，注释里也不行。

        cmd.exe 在 ``chcp 65001`` 下按**字节偏移**回溯文件位置，而偏移记账按字符算。
        于是它会从一个多字节字符**中间**接着读：前半截字节留在上一行，后半截落单
        （控制台显示成两个替换符），而**后半行被当成一条新命令执行**。

        这不是显示问题。``rem uv run xingcha admin reset-password  忘了密码`` 那一行
        真的被执行过一次，"忘了密码"成了命令行参数。注释里要是记着一条破坏性命令，
        后果就不只是难看。

        更糟的是它**时有时无**：只在文件不在系统页缓存里（刚改过、刚开机）时才容易
        撞上，下一次跑又"好了"——是那种偶发一次、复现不了的故障。

        所以逻辑与中文全在同名的 ``.ps1`` 里，``.bat`` 只剩一个纯 ASCII 的启动器。
        """
        raw = path.read_bytes()
        bad = [i for i, b in enumerate(raw) if b > 127]
        assert not bad, (
            f"{path.name} 里有 {len(bad)} 个非 ASCII 字节（首个在偏移 {bad[0]}）。"
            f"中文放进同名 .ps1，或放进旁边的 .md。"
        )

    @pytest.mark.parametrize("path", [EDGE_PS1, XC_PS1], ids=lambda p: p.name)
    def test_the_ps1_has_a_bom(self, path: Path):
        """``.ps1`` **必须**带 UTF-8 BOM。**和 .bat 正好相反。**

        Windows PowerShell 5.1（Windows 自带的那个）读无 BOM 的 ``.ps1`` 会按系统
        ANSI 代码页解码——中文全成乱码，而脚本照常运行，所以只有输出是坏的，
        看起来像"终端编码没设对"，指不到文件本身。
        """
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), (
            f"{path.name} 没有 UTF-8 BOM —— PowerShell 5.1 会按 ANSI 读，中文乱码"
        )

    @pytest.mark.parametrize("path", [EDGE_PS1, XC_PS1], ids=lambda p: p.name)
    def test_the_ps1_avoids_powershell7_only_syntax(self, path: Path):
        """只用 Windows PowerShell 5.1 认的语法。

        ``?.``、``??``、三元 ``? :`` 都是 PowerShell 7 才有的。5.1 上它们是**解析
        错误**——整个脚本一行都不跑，而 5.1 正是 Windows 自带的那个版本，也就是
        双击时真正执行这些脚本的那个。
        """
        code = _ps1_code(path)
        for token, why in ((")?.", "null-conditional ?."), ("??", "null-coalescing ??")):
            assert token not in code, f"{path.name} 用了 {why}，PowerShell 5.1 不认"

    def test_the_bat_delegates_to_the_ps1(self):
        """启动器要真的把活交出去，而不是自己又长回去。

        没有这一条的话，"顺手在 .bat 里加一行"是很自然的动作，而加的那一行迟早会
        带中文——于是绕一圈回到上面那个 bug。
        """
        for bat, ps1 in ((EDGE_BAT, "edge.ps1"), (XC_BAT, "xc.ps1")):
            text = bat.read_text(encoding="ascii")
            assert ps1 in text, f"{bat.name} 没有指向 {ps1}"
            assert "-ExecutionPolicy Bypass" in text, (
                f"{bat.name} 没传 -ExecutionPolicy Bypass —— 默认策略会挡下未签名的 "
                f".ps1，而那个报错读起来像安全课，不像「双击我」"
            )
            code = [
                ln
                for ln in text.splitlines()
                if ln.strip() and not ln.strip().lower().startswith(("rem", "@echo", ":"))
            ]
            assert len(code) <= 12, (
                f"{bat.name} 有 {len(code)} 行实际代码 —— 启动器应当很薄，逻辑归 {ps1}"
            )

    def test_the_binary_is_not_committed(self):
        """caddy 二进制**不进版本库**：45 MB，而且每台机器的平台/架构不同。

        进了库的后果不只是仓库变大——它会开始漂移：谁也不知道那个文件是哪个版本、
        哪个平台的，而 `edge get` 现取一个是几秒钟的事。
        """
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for name in ("deploy/edge/caddy", "deploy/edge/caddy.exe"):
            assert name in ignored, f".gitignore 里没有 {name}"
        assert not (EDGE / "caddy").exists(), "deploy/edge/caddy 不该在仓库里"


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
        for path in (COMPOSE, GATEWAY_COMPOSE, XC, XC_BAT, EDGE_SH, EDGE_BAT):
            text = path.read_text(encoding="utf-8")
            code = "\n".join(
                ln
                for ln in text.splitlines()
                if not ln.lstrip().startswith(("#", "//"))
                and not ln.lstrip().lower().startswith("rem")
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
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        from xingcha.config import _KNOWN_ENV_NAMES

        documented = set(re.findall(r"^#?\s*(XINGCHA_[A-Z_]+)=", text, re.M))
        unknown = sorted(documented - _KNOWN_ENV_NAMES)
        assert not unknown, f".env.example 里这些名字应用不认识：{unknown}"


# =============================================================================
# 文档不重复
# =============================================================================


class TestDocsHaveOneOwner:
    """网关的部署说明**只有一份**，在 deploy/edge/CADDY.md——和它说的那些文件同一个
    目录。

    用户明确要求过"其他地方不要有重复的部分"。这不是洁癖：两处各写一份的结果一定是
    其中一份先过期，而读到过期那份的人会照着做——然后撞上一个已经不存在的步骤
    （这个项目已经发生过：deploy.sh 在 Caddy 去掉之后还在校验 XINGCHA_DOMAIN）。
    """

    #: 只该出现在 CADDY.md 里的字眼。都是网关的实现细节，不是 xingcha 的部署步骤。
    GATEWAY_ONLY: ClassVar[list[str]] = ["edge trust", "root.crt", "default_sni", "内部 CA"]

    def test_the_gateway_doc_exists_and_is_linked(self):
        caddy = EDGE / "CADDY.md"
        assert caddy.exists(), "deploy/edge/CADDY.md 不见了"
        assert not (DEPLOY / "CADDY.md").exists(), "deploy/CADDY.md 又冒出来一份"
        assert "CADDY.md" in (DEPLOY / "README.md").read_text(encoding="utf-8")
        assert "CADDY.md" in (ROOT / "README.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize("phrase", GATEWAY_ONLY)
    def test_gateway_details_live_in_one_place(self, phrase: str):
        others = [
            q.relative_to(ROOT).as_posix()
            for q in [ROOT / "README.md", DEPLOY / "README.md"]
            if phrase in q.read_text(encoding="utf-8")
        ]
        assert not others, (
            f"「{phrase}」是网关的细节，只该在 deploy/edge/CADDY.md 里，却出现在 {others}"
        )


# =============================================================================
# 默认值的分工
# =============================================================================


class TestEnvDefaults:
    """**非敏感项必须有默认值；敏感项一律不能有。**

    两条规则的理由不同，所以要分开断言：

    - 非敏感项（端口、地址、日志级别、数据位置）有默认值，是为了让**一个空
      ``.env`` 也能起来**。装上先跑起来看看是最常见的第一步，而"必填三项才能启动"
      会把那一步变成读文档。
    - 敏感项（各种 key、后台密码）**不能**有默认值。一个内置的默认 key 是假的
      凭据，会让"配好了"这件事变得看不出来：调用一路走到上游才失败，而错误信息
      指向上游而不是"你还没配 key"。空 = 没设置 = 登录后再加。
    """

    #: 敏感项。这些在 Settings 里必须是 None，在 .env.example 里必须留空。
    SECRETS: ClassVar[list[str]] = [
        "api_key",
        "admin_password",
    ]

    #: 非敏感项，必须有一个能直接用的默认值。
    WITH_DEFAULTS: ClassVar[list[str]] = [
        "host",
        "port",
        "log_level",
        "session_ttl_hours",
        "data_dir",
    ]

    @pytest.fixture(scope="class")
    def fields(self) -> dict[str, object]:
        from xingcha.config import Settings

        return {name: f.get_default() for name, f in Settings.model_fields.items()}

    @pytest.mark.parametrize("name", SECRETS)
    def test_secrets_have_no_default(self, fields: dict[str, object], name: str):
        assert name in fields, f"Settings 里没有 {name} 这个字段"
        assert fields[name] is None, f"{name} 有默认值 {fields[name]!r} —— 敏感项不能有"

    @pytest.mark.parametrize("name", WITH_DEFAULTS)
    def test_non_secrets_have_a_usable_default(self, fields: dict[str, object], name: str):
        assert name in fields, f"Settings 里没有 {name} 这个字段"
        got = fields[name]
        assert got is not None and got != "", f"{name} 没有默认值，空 .env 起不来"

    def test_the_env_example_leaves_every_secret_blank(self):
        """``.env.example`` 里敏感项必须是 ``KEY=``（等号后面什么都没有）。

        写一个占位值（``sk-xxx``）的代价：用户 cp 之后忘了改，服务带着一个假 key
        起来，第一次调用才失败——而错误信息指向上游，不指向"你还没配"。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if any(w in name.upper() for w in ("KEY", "PASSWORD", "SECRET", "TOKEN")):
                assert value.strip() == "", f"{name} 在 .env.example 里带了值：{value!r}"

    def test_an_empty_env_still_produces_a_valid_compose_config(self):
        """空 ``.env`` 必须能解析出完整编排。

        这条是上面那些默认值的**端到端**证明：``${VAR}`` 少一个 ``:-default``，
        这里就会红——而症状在真部署时是"某个值静默变成空串"。
        """
        raw = COMPOSE.read_text(encoding="utf-8")
        missing = [k for k, v in compose_vars(raw).items() if v is None]
        assert not missing, f"这些变量没有默认值：{missing}"
