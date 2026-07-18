from short_trading_bot.market.kis_ranking import KisVolumeRank


def test_parse_volume_rank_rows() -> None:
    resp = {
        "output": [
            {
                "mksc_shrn_iscd": "123450", "hts_kor_isnm": "급등주", "stck_prpr": "15000",
                "prdy_ctrt": "12.34", "acml_vol": "5000000", "acml_tr_pbmn": "75000000000",
                "vol_inrt": "850.5",
            },
            {"mksc_shrn_iscd": "", "hts_kor_isnm": "코드없음"},  # skipped
            {"mksc_shrn_iscd": "999990", "prdy_ctrt": "bad"},  # 필드 오염 → 0.0
        ]
    }
    rows = KisVolumeRank.parse(resp)
    assert len(rows) == 2
    assert rows[0].ticker == "123450" and rows[0].change_pct == 12.34
    assert rows[0].vol_surge == 850.5 and rows[0].value_traded == 75_000_000_000.0
    assert rows[1].change_pct == 0.0
