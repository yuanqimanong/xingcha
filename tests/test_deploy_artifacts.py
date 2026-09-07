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


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


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
