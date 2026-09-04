"""令牌签发与校验。

这是公网上唯一挡在一把付费 OpenRouter key 前面的东西，所以覆盖要密。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from xingcha import contract as C
from xingcha.db import migrate
from xingcha.db.engine import make_engine, make_sessionmaker
from xingcha.errors import InvalidApiKey
from xingcha.services import auth


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    db = tmp_path / "x.db"
    migrate.upgrade_to_head(db, tmp_path / "b")
    engine = make_engine(db)
    maker = make_sessionmaker(engine)
    async with maker() as s:
        yield s
        await s.rollback()
    await engine.dispose()


def bearer(tok: str) -> str:
    return f"Bearer {tok}"


class TestIssue:
    async def test_plaintext_matches_frozen_envelope(self, session: AsyncSession):
        """签发出来的东西必须符合自己冻结的契约。

        格式漂移要在开发期被抓住，而不是等某个客户端拿着一把不合法的 key 来报障。
        """
        t = await auth.issue(session, name="ci")
        assert C.TOKEN_ENVELOPE_RE.match(t.plaintext)
        assert t.plaintext.startswith(f"{C.TOKEN_PREFIX}{C.TOKEN_SCHEME_CURRENT}-")
        assert len(t.plaintext) == 68  # 6+1+1+16+1+43

    async def test_plaintext_is_never_stored(self, session: AsyncSession):
        """库里只有 hash 与 kid。明文只在签发那一刻存在。"""
        from sqlalchemy import select

        from xingcha.db.models import Token

        t = await auth.issue(session, name="ci")
        await session.flush()
        row = (await session.execute(select(Token).where(Token.kid == t.kid))).scalar_one()
        secret = t.plaintext.rsplit("-", 1)[-1]
        assert secret not in row.hash
        assert secret not in row.display_prefix
        assert secret not in (row.kdf_params or "")

    async def test_display_prefix_leaks_no_secret(self, session: AsyncSession):
        """display_prefix 会出现在 UI、日志与 token list 里。

        用「明文前 N 字符」当 prefix 的做法会把秘密本体的开头印在这些地方。
        """
        t = await auth.issue(session, name="ci")
        secret = t.plaintext.rsplit("-", 1)[-1]
        assert secret[:6] not in t.display_prefix
        assert t.display_prefix == f"sk-xc-1-{t.kid}"

    async def test_each_token_is_unique(self, session: AsyncSession):
        kids = {(await auth.issue(session, name=f"t{i}")).kid for i in range(20)}
        assert len(kids) == 20


class TestAuthenticate:
    async def test_happy_path(self, session: AsyncSession):
        t = await auth.issue(session, name="ci")
        await session.flush()
        p = await auth.authenticate(session, bearer(t.plaintext))
        assert (p.kid, p.token_id, p.user_id) == (t.kid, t.token_id, 1)

    async def test_scheme_is_case_insensitive(self, session: AsyncSession):
        t = await auth.issue(session, name="ci")
        await session.flush()
        for scheme in ("Bearer", "bearer", "BEARER"):
            assert await auth.authenticate(session, f"{scheme} {t.plaintext}")

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "sk-xc-1-aaaaaaaaaaaaaaaa-" + "x" * 43,  # 缺 Bearer
            "Basic dXNlcjpwYXNz",
            "Bearer ",
            "Bearer not-a-token",
            "Bearer sk-or-v1-someupstreamkey",  # 上游 key 不该被接受
            "Bearer sk-xc-1-SHORT-" + "x" * 43,
        ],
    )
    async def test_rejects_bad_headers(self, session: AsyncSession, header: str | None):
        with pytest.raises(InvalidApiKey):
            await auth.authenticate(session, header)

    async def test_wrong_secret_with_valid_kid(self, session: AsyncSession):
        """kid 对但 secret 错——这是最接近真实攻击的一种。"""
        t = await auth.issue(session, name="ci")
        await session.flush()
        forged = f"sk-xc-1-{t.kid}-" + "z" * 43
        with pytest.raises(InvalidApiKey):
            await auth.authenticate(session, bearer(forged))

    async def test_revoked_token_fails_immediately(self, session: AsyncSession):
        """吊销必须立刻生效，不能等缓存过期。"""
        t = await auth.issue(session, name="ci")
        await session.flush()
        assert await auth.authenticate(session, bearer(t.plaintext))

        assert await auth.revoke(session, t.kid) is True
        await session.flush()
        with pytest.raises(InvalidApiKey):
            await auth.authenticate(session, bearer(t.plaintext))

    async def test_revoke_keeps_the_row(self, session: AsyncSession):
        """置 is_active=False 而不是删行——删掉之后历史 run 就找不到归属了。"""
        from sqlalchemy import select

        from xingcha.db.models import Token

        t = await auth.issue(session, name="ci")
        await session.flush()
        await auth.revoke(session, t.kid)
        await session.flush()
        row = (await session.execute(select(Token).where(Token.kid == t.kid))).scalar_one()
        assert row.is_active is False

    async def test_expired_token_fails(self, session: AsyncSession):
        t = await auth.issue(session, name="ci", expires_at="2000-01-01T00:00:00+00:00")
        await session.flush()
        with pytest.raises(InvalidApiKey):
            await auth.authenticate(session, bearer(t.plaintext))

    async def test_future_expiry_is_fine(self, session: AsyncSession):
        t = await auth.issue(session, name="ci", expires_at=auth.parse_expiry(30))
        await session.flush()
        assert await auth.authenticate(session, bearer(t.plaintext))


class TestNoOracle:
    async def test_all_failures_look_identical_to_the_caller(self, session: AsyncSession):
        """区分无效/禁用/过期 = 给公网一个 token 有效性 oracle。

        "这个 key 存在但过期了" 是白送给攻击者的信息：它确认了 kid 有效。
        """
        valid = await auth.issue(session, name="a")
        revoked = await auth.issue(session, name="b")
        expired = await auth.issue(session, name="c", expires_at="2000-01-01T00:00:00+00:00")
        await auth.revoke(session, revoked.kid)
        await session.flush()

        bodies = []
        for tok in (
            "sk-xc-1-" + "0" * 16 + "-" + "x" * 43,  # kid 不存在
            f"sk-xc-1-{valid.kid}-" + "z" * 43,  # secret 错
            revoked.plaintext,
            expired.plaintext,
        ):
            with pytest.raises(InvalidApiKey) as exc:
                await auth.authenticate(session, bearer(tok))
            bodies.append(exc.value.to_body())

        assert all(b == bodies[0] for b in bodies), "不同失败原因对外必须完全一致"

    async def test_real_reason_still_reaches_the_log(self, session: AsyncSession):
        """对外一致，对内可查——否则排障时就抓瞎了。"""
        t = await auth.issue(session, name="ci", expires_at="2000-01-01T00:00:00+00:00")
        await session.flush()
        with pytest.raises(InvalidApiKey) as exc:
            await auth.authenticate(session, bearer(t.plaintext))
        assert "过期" in (exc.value.log_detail or "")
        assert t.kid in (exc.value.log_detail or "")


class TestSchemeEvolution:
    async def test_unknown_scheme_is_rejected(self, session: AsyncSession):
        """scheme=9 是未来的算法，本版本不认识——必须拒绝而不是当成 scheme=1。"""
        with pytest.raises(InvalidApiKey):
            await auth.authenticate(session, bearer("sk-xc-9-" + "a" * 16 + "-" + "x" * 43))

    async def test_kdf_params_column_is_available_for_future_schemes(self, session: AsyncSession):
        """scheme=1 不用盐，但列必须在——argon2id 的盐与 t/m/p 要随行走。"""
        from sqlalchemy import select

        from xingcha.db.models import Token

        t = await auth.issue(session, name="ci")
        await session.flush()
        row = (await session.execute(select(Token).where(Token.kid == t.kid))).scalar_one()
        assert row.hash_alg == "sha256"
        assert row.kdf_params is None
