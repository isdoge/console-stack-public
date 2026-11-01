# -*- coding: utf-8 -*-
"""money.py 的 Decimal 契约回归（提案 §5.4）。纯函数，不需要数据库。"""
import pathlib
import sys
from decimal import Decimal

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import money  # noqa: E402


def test_from_float_uses_shortest_roundtrip_text_not_binary_mantissa():
    # 0.1 的二进制近似是 0.1000000000000000055511151231257827...
    # 契约要求取最短可往返十进制表示，而不是 Decimal(float) 的完整尾数。
    assert money.from_float(0.1) == Decimal("0.1")
    assert money.from_float(12.5) == Decimal("12.5")
    assert money.canonical(money.from_float(415.8)) == "415.8"


def test_from_float_rejects_nan_and_infinity():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(money.MoneyError):
            money.from_float(bad)


def test_from_float_rejects_bool():
    with pytest.raises(money.MoneyError):
        money.from_float(True)


def test_from_text_keeps_decimal_text_without_float_hop():
    assert money.from_text("415.80") == Decimal("415.80")
    assert money.from_text(" -0.50 ") == Decimal("-0.50")
    for bad in ("", "  ", "abc", "1,000", None):
        with pytest.raises(money.MoneyError):
            money.from_text(bad)


def test_from_text_rejects_nan_string():
    with pytest.raises(money.MoneyError):
        money.from_text("nan")


def test_normalize_strips_trailing_zeros_and_collapses_negative_zero():
    assert money.normalize("100.00") == Decimal(100)
    assert money.normalize("0.1000") == Decimal("0.1")
    assert money.canonical("-0") == "0"
    assert money.canonical("-0.000") == "0"
    # 数值不变，只是表示规范化
    assert money.normalize("123.4500") == Decimal("123.45")


def test_canonical_expands_exponent_form():
    # Decimal.normalize() 会产出 1E+2 这种形式；对账编码必须展开成 100。
    assert money.canonical("100") == "100"
    assert money.canonical("1E+2") == "100"
    assert money.canonical("1.5E-3") == "0.0015"


def test_quantize_cents_uses_half_even_and_only_for_output():
    assert money.quantize_cents("0.125") == Decimal("0.12")
    assert money.quantize_cents("0.135") == Decimal("0.14")
    assert money.quantize_cents("2.5") == Decimal("2.50")
    # 不指定 scale 的原始值不被改动
    assert money.as_decimal("0.125") == Decimal("0.125")


def test_json_number_roundtrips_or_raises():
    assert money.json_number("0.1") == 0.1
    assert money.json_number("415.8") == 415.8
    assert money.is_json_safe("0.1") is True
    # 超出 float 可无损往返范围 → 抛错（HTTP 400），不静默降精度
    assert money.is_json_safe("1E+400") is False
    with pytest.raises(money.MoneyError):
        money.json_number("1E+400")


def test_json_number_rejects_value_that_float_cannot_roundtrip():
    # 0.1 的最短表示可以往返；但 10**30+1 这种大整数转 float 会丢精度。
    awkward = Decimal("1000000000000000000000000000001")
    assert money.is_json_safe(awkward) is False
    with pytest.raises(money.MoneyError):
        money.json_number(awkward)


def test_sum_decimals_raises_precision_beyond_default_28():
    # 默认 context 精度 28 位下，这个累加会被截断；契约要求显式抬高。
    values = [Decimal("99999999999999999999999999.99")] * 4
    total = money.sum_decimals(values)
    assert total == Decimal("399999999999999999999999999.96")


def test_validate_range_rejects_out_of_range():
    assert money.validate_range("999") == Decimal("999")
    with pytest.raises(money.MoneyError):
        money.validate_range("-1", minimum=Decimal(0))
    with pytest.raises(money.MoneyError):
        money.validate_range("1000000000001")


def test_differs_from_binary_sum_reports_binary_drift_not_business_change():
    values = ["0.1", "0.2"]
    decimal_total, binary_total, difference = money.differs_from_binary_sum(values)
    assert decimal_total == Decimal("0.3")
    # 二进制累加确实有尾差，但它是浮点表示误差，不是要回写账本的错误
    assert binary_total != float(Decimal("0.3")) or difference == 0
    assert money.canonical(decimal_total) == "0.3"
