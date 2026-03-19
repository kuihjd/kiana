import pytest


@pytest.mark.parametrize(
    ("code", "expected_market"),
    [
        ("600000", "sh"),
        ("000001", "sz"),
        ("430476", "bj"),
        ("920029", "bj"),
    ],
)
def test_infer_stock_market_returns_expected_market(code: str, expected_market: str):
    from src.plugins.fund.market_rules import infer_stock_market

    assert infer_stock_market(code) == expected_market


def test_stock_market_detectors_distinguish_sh_sz_bj():
    from src.plugins.fund.market_rules import (
        is_beijing_stock,
        is_shanghai_stock,
        is_shenzhen_stock,
    )

    assert is_shanghai_stock("600000") is True
    assert is_shanghai_stock("000001") is False
    assert is_shanghai_stock("920029") is False

    assert is_shenzhen_stock("000001") is True
    assert is_shenzhen_stock("600000") is False
    assert is_shenzhen_stock("920029") is False

    assert is_beijing_stock("430476") is True
    assert is_beijing_stock("920029") is True
    assert is_beijing_stock("600000") is False
    assert is_beijing_stock("43123456") is False


def test_index_market_detectors_distinguish_sh_sz():
    from src.plugins.fund.market_rules import is_shanghai_index, is_shenzhen_index

    assert is_shanghai_index("000001") is True
    assert is_shanghai_index("399001") is False

    assert is_shenzhen_index("399001") is True
    assert is_shenzhen_index("000001") is False


@pytest.mark.parametrize(
    ("code", "market"),
    [
        ("600000", "sh"),
        ("000001", "sz"),
        ("430476", "bj"),
        ("920029", "bj"),
    ],
)
def test_validate_market_code_accepts_matching_market(code: str, market: str):
    from src.plugins.fund.market_rules import validate_market_code

    is_valid, error_msg = validate_market_code(code, market)

    assert is_valid is True
    assert error_msg is None


@pytest.mark.parametrize(
    ("code", "market", "expected_error"),
    [
        ("600000", "sz", "深圳市场"),
        ("000001", "sh", "上海市场"),
        ("920029", "sh", "上海市场"),
        ("920029", "sz", "深圳市场"),
        ("600000", "bj", "北京市场"),
    ],
)
def test_validate_market_code_rejects_mismatched_market(
    code: str, market: str, expected_error: str
):
    from src.plugins.fund.market_rules import validate_market_code

    is_valid, error_msg = validate_market_code(code, market)

    assert is_valid is False
    assert error_msg is not None
    assert expected_error in error_msg
