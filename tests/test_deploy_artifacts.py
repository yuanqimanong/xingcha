"""部署产物的结构约束。

------------------------------------------------------------------------------
为什么这一层非有不可
------------------------------------------------------------------------------

Dockerfile / docker-compose.yml / Caddyfile / deploy.sh 之间有**跨文件的隐式契约**，
而它们互相之间没有任何类型检查：Caddyfile 引用一个环境变量，compose 负责传进去，
deploy.sh 负责校验它非空，.env.example 负责告诉用户要填。

这四处任意一处漏掉，**单元测试全绿、镜像构建成功、代码审查也看不出来**——症状只在
真的 `docker compose up` 时出现，而那通常是在一台刚买的 VPS 上、半夜、DNS 刚生效的
时候。实际发生过：Caddyfile 里写了 ``email {$ACME_EMAIL}``，compose 从来没传过这个
变量，于是容器里它是空的，``email`` 指令零参数 → 配置解析失败 → 无限重启循环。
那套编排从来没有真正起来过，而全套测试是绿的。

所以这些约束必须变成会红的断言。**这里只做静态结构检查**，不起容器——起容器的验证
是 deploy/drill.sh 与真机演练的事。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from xingcha import contract as C

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
CADDYFILE = ROOT / "Caddyfile"
DOCKERFILE = ROOT / "Dockerfile"
DEPLOY_SH = ROOT / "deploy" / "deploy.sh"
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
DRILL_SH = ROOT / "deploy" / "drill.sh"
COMPOSE_LAN = ROOT / "docker-compose.lan.yml"
CADDYFILE_LAN = ROOT / "deploy" / "Caddyfile.lan"


class _ComposeLoader(yaml.SafeLoader):
    """认得 compose 自己的 YAML 标签（``!override`` / ``!reset``）。

    ``safe_load`` 会对它们抛 ConstructorError——那不是配置错，是 compose 的扩展
    语法。这里把标签丢掉、只保留值，因为测试关心的是"有没有这个 key、值是什么"，
    而标签本身另有一条测试（读原文断言 ``ports: !override`` 在）。
    """


def _drop_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_ComposeLoader.add_multi_constructor("!", _drop_tag)


def _load_compose(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)


@pytest.fixture(scope="module")
def compose() -> dict:
    return _load_compose(COMPOSE)


def caddy_env_refs() -> set[str]:
    """Caddyfile 里引用的所有环境变量名。"""
    return set(re.findall(r"\{\$([A-Z_][A-Z0-9_]*)", CADDYFILE.read_text(encoding="utf-8")))


# =============================================================================
# 跨文件的变量契约
# =============================================================================


class TestEnvVarContract:
    def test_every_caddyfile_var_is_passed_by_compose(self, compose: dict):
        """**Caddyfile 引用的每个变量，compose 都必须传给 caddy 容器。**

        漏传的后果不是"用默认值"，是容器里那个变量为空。而 Caddy 的 ``email``
        指令不接受空值——配置解析直接失败，容器进无限重启循环，日志里那句
        "wrong argument count" 离根因很远。

        这条就是真踩过的那个 bug。
        """
        passed = set(compose["services"]["caddy"].get("environment", {}))
        missing = caddy_env_refs() - passed
        assert not missing, (
            f"Caddyfile 用了 {sorted(missing)}，但 docker-compose.yml 没传给 caddy 容器。"
            f"容器里它们会是空字符串。"
        )

    def test_required_vars_fail_at_up_not_in_a_restart_loop(self, compose: dict):
        """必填变量要用 ``${VAR:?说明}``，不能用裸 ``${VAR}``。

        裸写法在变量缺失时**静默变成空串**，然后失败推迟到容器里发生——用户看到的是
        崩溃循环。``:?`` 让 compose 在 up 那一刻就带着说明失败，那才是能修的报错。
        """
        raw = COMPOSE.read_text(encoding="utf-8")
        for var in ("XINGCHA_DOMAIN", "ACME_EMAIL"):
            bare = re.findall(rf"\$\{{{var}\}}", raw)
            assert not bare, f"{var} 有裸 ${{}} 引用，应当写成 ${{{var}:?说明}}"
            assert f"${{{var}:?" in raw, f"{var} 没有 :? 守卫"

    def test_env_example_documents_every_required_var(self):
        """必填项必须在 .env.example 里、且带一个非空的示例值。

        留空的示例值等于把"这项可以不填"写进了文档——而 ACME_EMAIL 留空会让
        Caddy 起不来。
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for var in ("XINGCHA_DOMAIN", "ACME_EMAIL"):
            m = re.search(rf"^{var}=(.*)$", text, re.M)
            assert m, f".env.example 里没有 {var}"
            assert m.group(1).strip(), f".env.example 里 {var} 的示例值是空的"

    def test_deploy_sh_validates_every_required_var(self):
        """deploy.sh 要在 up 之前把必填项挡下来。

        compose 的 ``:?`` 是最后一道；deploy.sh 这道能给出更贴近用户的指引
        （"去 .env 里填什么"而不是一句 interpolation 错误）。
        """
        text = DEPLOY_SH.read_text(encoding="utf-8")
        for var in ("XINGCHA_DOMAIN", "ACME_EMAIL"):
            assert re.search(rf'\[\[ -n "\$\{{{var}:-\}}" \]\]', text), (
                f"deploy.sh 没有校验 {var} 非空"
            )


# =============================================================================
# 容器 UID：四处引用，一处定义
# =============================================================================


class TestContainerUid:
    """UID 在 Dockerfile、deploy.sh、drill.sh、以及数据目录报错文案里都出现。

    写四遍的结果是改了一处忘三处，而症状只在真部署时出现——容器起来就是
    Permission denied，而那句报错离根因很远。
    """

    def test_dockerfile_uses_the_contract_uid(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert f"--uid {C.CONTAINER_UID}" in text, (
            f"Dockerfile 的 useradd 没有用 contract 里的 {C.CONTAINER_UID}"
        )

    def test_deploy_and_drill_use_the_same_uid(self):
        for path in (DEPLOY_SH, DRILL_SH):
            text = path.read_text(encoding="utf-8")
            assert str(C.CONTAINER_UID) in text, f"{path.name} 里没有 {C.CONTAINER_UID}"

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
    def test_xingcha_publishes_no_host_ports(self, compose: dict):
        """**xingcha 绝不能映射宿主端口。**

        加一行 ``ports:`` 会让应用直接暴露在公网，而且 Docker 的 DOCKER-USER 链
        会绕过 ufw——防火墙写了 deny，映射出去的端口照样可达。对外只经 Caddy。
        """
        assert "ports" not in compose["services"]["xingcha"], (
            "xingcha 服务出现了 ports:，这会绕过 Caddy 并且绕过 ufw"
        )

    def test_only_caddy_publishes_ports(self, compose: dict):
        publishers = [n for n, s in compose["services"].items() if s.get("ports")]
        assert publishers == ["caddy"], f"只有 caddy 该发布端口，实际是 {publishers}"

    def test_logging_is_capped_on_every_service(self, compose: dict):
        """A8：日志不限大小会涨满磁盘，而整个产品就是一个 SQLite 文件——
        磁盘一满就是写失败 + 迁移失败 + 无法启动，根因在监控里完全看不见。"""
        for name, svc in compose["services"].items():
            opts = (svc.get("logging") or {}).get("options") or {}
            assert opts.get("max-size"), f"{name} 没有 max-size，日志会涨满磁盘"

    def test_caddy_does_not_buffer_streams(self):
        """``flush_interval -1`` 少一行，流式就变成"等全部生成完再一次性吐出"。

        客户端那边表现为"卡很久然后突然全出来"——功能上没报错，体验上流式完全没了。
        """
        assert "flush_interval -1" in CADDYFILE.read_text(encoding="utf-8")

    def test_caddy_forwards_the_real_client_ip(self):
        """调用记录与限流都靠它。抹掉就永久失去来源 IP 取证能力。"""
        assert "X-Real-IP" in CADDYFILE.read_text(encoding="utf-8")

    def test_body_limit_leaves_room_for_the_app_to_answer(self):
        """Caddy 的上限要**略大于**应用的上限。

        相等或更小的话，超限请求会被 Caddy 静默截断，调用方拿不到星槎自己那个
        写明原因的 413。
        """
        m = re.search(r"max_size\s+(\d+)MB", CADDYFILE.read_text(encoding="utf-8"))
        assert m, "Caddyfile 里没有 request_body max_size"
        assert int(m.group(1)) * 1024 * 1024 > C.MAX_BODY_BYTES

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


class TestLanOverride:
    """局域网叠加层。

    ------------------------------------------------------------------------
    它为什么存在
    ------------------------------------------------------------------------

    生产的 Caddyfile 走 ACME，需要真域名——所以局域网/本机测试此前只能手搓
    ``docker run``。后果是**照文档敲的 `docker compose restart` 会失败**：
    用户在 deploy/ 下执行，撞上 `XINGCHA_DOMAIN is missing a value`，而在跑的容器
    压根不是 compose 起的、compose 管不到。正规路径不覆盖真实用法，那就是文档在骗人。
    """

    @pytest.fixture(scope="class")
    def lan(self) -> dict:
        return _load_compose(COMPOSE_LAN)

    def test_the_override_exists_and_parses(self, lan: dict):
        assert set(lan["services"]) <= {"xingcha", "caddy"}

    def test_ports_are_overridden_not_appended(self, lan: dict):
        """``ports`` 必须用 ``!override``。

        compose 对列表默认是**追加**，所以不覆盖的话生产那份的 80/443 会继续被占——
        本机很可能已经有别的东西在用它们，而"我明明只想开 8443"却把 443 占掉是一个
        说不清的副作用。实测踩过。
        """
        raw = COMPOSE_LAN.read_text(encoding="utf-8")
        assert "ports: !override" in raw, "ports 没有 !override，80/443 会被一起占用"
        assert "volumes: !override" in raw, "volumes 没有 !override，会挂两份 Caddyfile"

    def test_caddy_volume_is_separate_from_production(self, lan: dict):
        """证书状态卷要与生产分开。

        内部 CA 与 ACME 的状态混在同一个卷里，切换模式时会带着上一次的配置，
        表现为"端口上没人监听"——一个查不到原因的现象。
        """
        mounts = " ".join(lan["services"]["caddy"]["volumes"])
        assert "caddy_lan_data" in mounts
        assert "caddy_data:" not in mounts

    def test_env_file_is_passed_into_the_container(self, lan: dict):
        """**这是「上游可切换」在 Docker 下能用的前提。**

        容器不继承宿主环境（Docker 的隔离语义），所以 DEEPSEEK_API_KEY 一类不透传
        进来就扫不到，后台的「环境里扫到的」那一栏是空的。顺带也带进
        XINGCHA_ADMIN_PASSWORD——那正是用户"我 .env 里设了怎么不生效"的根因：
        我此前用 docker run 手工列举环境变量，根本没传它。
        """
        entries = lan["services"]["xingcha"]["env_file"]
        assert any((e if isinstance(e, str) else e.get("path")) == ".env" for e in entries), (
            "没有把 .env 注入容器"
        )

    def test_lan_caddyfile_uses_the_internal_ca(self):
        """局域网 IP 与 localhost 拿不到公网证书。"""
        raw = CADDYFILE_LAN.read_text(encoding="utf-8")
        assert "tls internal" in raw
        assert "acme_ca" not in raw, "局域网配置不该走 ACME"

    def test_lan_site_address_has_no_hostname(self):
        """站点地址**只写端口**。

        写成 ``https://<IP>:8443`` 会撞上 IP 证书的固有限制：SNI 里不允许放 IP
        （RFC 6066），于是 curl 发出的 SNI 与站点地址对不上，Caddy 直接拒绝握手
        （``TLS alert internal error``）——而证书本身是对的，SAN 里确实有那个 IP。
        实测踩过：openssl 能连、curl 不能，差别就在 SNI。
        """
        raw = CADDYFILE_LAN.read_text(encoding="utf-8")
        assert re.search(r"^:\{\$XINGCHA_LAN_PORT", raw, re.M), "站点地址不该带主机名"
        assert not re.search(r"^https://", raw, re.M)

    def test_lan_caddy_reaches_xingcha_by_service_name(self):
        """按 compose 服务名寻址，所以 xingcha **不需要**发布宿主端口——与生产一致。"""
        assert "reverse_proxy xingcha:8720" in CADDYFILE_LAN.read_text(encoding="utf-8")

    def test_override_does_not_publish_xingcha_ports(self, lan: dict):
        """叠加层也不能给 xingcha 开宿主端口。

        开了就绕过 Caddy，而且 Docker 的 DOCKER-USER 链会绕过 ufw。
        """
        assert "ports" not in lan["services"].get("xingcha", {})

    def test_lan_keeps_the_streaming_flush(self):
        """少这一行，流式就变成"等全部生成完再一次性吐出"。

        叠加层是**测试用的**，所以它更不能与生产在这类行为上分叉——分叉了就测不出
        真实体验。
        """
        assert "flush_interval -1" in CADDYFILE_LAN.read_text(encoding="utf-8")
