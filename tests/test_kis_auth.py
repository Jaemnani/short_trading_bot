from datetime import UTC, datetime, timedelta

from short_trading_bot.infra.config import KisEnvCreds
from short_trading_bot.infra.kis_auth import KisAuth


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _creds() -> KisEnvCreds:
    return KisEnvCreds(app_key="k", app_secret="s", account_no="1-01")


async def test_access_token_is_cached_until_expiry() -> None:
    calls = {"n": 0}

    async def fetch() -> tuple[str, int]:
        calls["n"] += 1
        return f"tok{calls['n']}", 86400

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    auth = KisAuth(_creds(), "https://x:9443", token_fetcher=fetch, clock=clock)

    assert await auth.access_token() == "tok1"
    assert await auth.access_token() == "tok1"  # cached
    assert calls["n"] == 1


async def test_access_token_refetched_after_expiry() -> None:
    calls = {"n": 0}

    async def fetch() -> tuple[str, int]:
        calls["n"] += 1
        return f"tok{calls['n']}", 86400

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    auth = KisAuth(_creds(), "https://x:9443", token_fetcher=fetch, clock=clock)

    await auth.access_token()
    clock.now += timedelta(seconds=86400)  # reach expiry (within skew window)
    assert await auth.access_token() == "tok2"
    assert calls["n"] == 2


async def test_invalidate_forces_refetch() -> None:
    calls = {"n": 0}

    async def fetch() -> tuple[str, int]:
        calls["n"] += 1
        return "tok", 86400

    auth = KisAuth(_creds(), "https://x:9443", token_fetcher=fetch)
    await auth.access_token()
    auth.invalidate()
    await auth.access_token()
    assert calls["n"] == 2


async def test_approval_key_cached() -> None:
    calls = {"n": 0}

    async def afetch() -> str:
        calls["n"] += 1
        return "approval-123"

    auth = KisAuth(_creds(), "https://x:9443", approval_fetcher=afetch)
    assert await auth.approval_key() == "approval-123"
    assert await auth.approval_key() == "approval-123"
    assert calls["n"] == 1
