"""국내주식 현금주문 body — KIS 공식 샘플과 키 집합 일치 검증.

2026-08-11 사고: CNDT_PRIC(조건가격) 누락으로 매도 주문이 IGW00007 "MCA 전문바디 구성 중
오류" 로 거부됐다. 게이트웨이가 고정 포맷 전문을 조립하므로 선택 필드도 빠지면 안 된다.
매수는 통과해 **실주문 전환(08-03) 이후 매수만 되고 매도 100% 실패 = 손절 불능**이었다.

출처: koreainvestment/open-trading-api examples_llm/domestic_stock/order_cash/order_cash.py
"""

from decimal import Decimal

import pytest

from short_trading_bot.domain.enums import Market, Mode, Side
from short_trading_bot.execution.broker.kis import KisBrokerAdapter
from short_trading_bot.execution.types import OrderRequest
from short_trading_bot.infra.config import KisEnvCreds
from short_trading_bot.infra.kis_auth import KisAuth

# 공식 샘플이 보내는 키 전부 (하나라도 빠지면 전문 구성이 깨진다)
OFFICIAL_KEYS = {
    "CANO", "ACNT_PRDT_CD", "PDNO", "ORD_DVSN", "ORD_QTY",
    "ORD_UNPR", "EXCG_ID_DVSN_CD", "SLL_TYPE", "CNDT_PRIC",
}


def _adapter() -> KisBrokerAdapter:
    creds = KisEnvCreds(
        app_key="k", app_secret="s", account_no="50196843", account_product_code="01"
    )
    return KisBrokerAdapter(
        KisAuth(creds, "https://example.test"), creds, "https://example.test", Mode.PAPER
    )


def _req(side: Side) -> OrderRequest:
    return OrderRequest(
        client_order_id="c1", lot_id="lot1", ticker="122630", market=Market.KRX,
        side=side, qty=Decimal("52"), price=Decimal("88460"), ord_dvsn="00",
    )


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_body_has_exactly_official_keys(side: Side) -> None:
    body = _adapter().build_order_body(_req(side))
    assert set(body) == OFFICIAL_KEYS, "공식 샘플과 키 집합이 달라지면 IGW00007 재발"


def test_cndt_pric_always_present() -> None:
    """누락 시 IGW00007 — 매수·매도 모두 공란으로라도 실어야 한다."""
    for side in (Side.BUY, Side.SELL):
        assert _adapter().build_order_body(_req(side))["CNDT_PRIC"] == ""


def test_sll_type_is_blank_even_for_sell() -> None:
    """공식 매도 예시(chk_order_cash.py)가 sll_type 을 넘기지 않아 ""로 나간다.
    "01"(일반매도)을 넣으면 IGW00007 로 거부된다 — 실주문 매도 100% 실패의 원인."""
    for side in (Side.BUY, Side.SELL):
        assert _adapter().build_order_body(_req(side))["SLL_TYPE"] == ""


def test_values_are_strings() -> None:
    """KIS 는 수량·단가를 문자열로 요구한다 (숫자로 보내면 거부)."""
    body = _adapter().build_order_body(_req(Side.SELL))
    assert body["ORD_QTY"] == "52" and body["ORD_UNPR"] == "88460"
    assert all(isinstance(v, str) for v in body.values())
