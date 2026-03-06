from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 15, tzinfo=tz)


@pytest.mark.asyncio
async def test_get_fund_holdings_falls_back_to_previous_year_data():
    """当前年份无持仓时，应回退到上一年数据。"""
    from src.plugins.fund.data_fetcher import get_fund_holdings

    empty_df = pd.DataFrame(columns=["季度", "股票代码", "股票名称", "占净值比例"])
    previous_year_df = pd.DataFrame(
        {
            "季度": ["2025年4季度"],
            "股票代码": ["600000"],
            "股票名称": ["浦发银行"],
            "占净值比例": [8.5],
        }
    )

    with (
        patch("src.plugins.fund.data_fetcher.datetime", _FixedDatetime),
        patch(
            "src.plugins.fund.data_fetcher.ak.fund_portfolio_hold_em",
            side_effect=[empty_df, previous_year_df],
        ) as mock_holdings,
    ):
        result = await get_fund_holdings("002170")

    assert result["success"] is True
    assert result["holdings"].equals(previous_year_df)
    assert mock_holdings.call_count == 2
    assert mock_holdings.call_args_list[0].kwargs["date"] == "2026"
    assert mock_holdings.call_args_list[1].kwargs["date"] == "2025"
