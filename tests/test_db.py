from decimal import Decimal

from sqlalchemy import select

from short_trading_bot.persistence.db import (
    create_engine,
    init_models,
    session_factory,
    session_scope,
)
from short_trading_bot.persistence.models import AuditLog, Position


async def test_create_insert_query(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await init_models(engine)
    sf = session_factory(engine)

    async with session_scope(sf) as s:
        s.add(
            Position(
                lot_id="lot1",
                ticker="005930",
                state="WATCHING",
                strategy_id="trend_long_v1",
                qty_target=Decimal("10"),
            )
        )
        s.add(AuditLog(event_type="lot.created", lot_id="lot1", payload_json={"k": "v"}))

    async with session_scope(sf) as s:
        pos = (
            await s.execute(select(Position).where(Position.lot_id == "lot1"))
        ).scalar_one()
        assert pos.ticker == "005930"
        assert pos.qty_target == Decimal("10")
        assert pos.market == "KRX"  # default
        assert pos.realized_pnl == Decimal("0")

        audit = (await s.execute(select(AuditLog))).scalar_one()
        assert audit.event_type == "lot.created"
        assert audit.payload_json == {"k": "v"}
        assert audit.seq == 1  # autoincrement

    await engine.dispose()
