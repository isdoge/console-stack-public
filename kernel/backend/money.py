# -*- coding: utf-8 -*-
"""金额的 Decimal 契约（提案 §5.4，Q3 已确认；契约版本 `pg00-1`）。

设计要点，逐条对应提案：

- `renewals.cost` / `ledger_tx.amount` 是**不指定 scale 的 NUMERIC**，所以这里不预设两位小数。
- 浮点读入一律走 `Decimal(str(value))`（最短可往返十进制表示），**不得** `Decimal(value)`
  引入二进制尾数，也不得 `round(..., 2)` 改历史值。
- 汇总/运算显式抬高精度，不沿用默认 28 位——大额或多行汇总会在那里悄悄舍入。
- 只对**响应字段**做 `quantize(0.01, ROUND_HALF_EVEN)`，绝不回写数据库。
- JSON 仍是 number（不改成字符串），但转 float 前必须能十进制往返，否则抛 `MoneyError` → 400。
"""
import math
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

CENT = Decimal("0.01")
ZERO = Decimal(0)

# 与 ledger.py 现有手工记账上限一致（1e12）；续费等其它入口按自己的范围另加约束。
MAX_AMOUNT = Decimal("1000000000000")

# 汇总/运算精度：默认 28 位对大额多行汇总不够，显式抬高。
CALC_PRECISION = 60


class MoneyError(ValueError):
    """金额非法：HTTP 层映射为 400，绝不静默舍入、截断或改成 null。"""


def from_float(value):
    """SQLite REAL / JSON number 读入：走最短可往返十进制表示。"""
    if isinstance(value, Decimal):
        return _ensure_finite(value)
    if isinstance(value, bool):
        raise MoneyError("金额不能是布尔值")
    if isinstance(value, float) and not math.isfinite(value):
        raise MoneyError("金额必须是有限数值，不接受 NaN/Infinity")
    try:
        return _ensure_finite(Decimal(str(value)))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"金额无法解析: {value!r}") from exc


def from_text(value):
    """CSV/XLSX/PDF 解析后的十进制文本读入；不做 float 中转。"""
    if isinstance(value, Decimal):
        return _ensure_finite(value)
    text = "" if value is None else str(value).strip()
    if not text:
        raise MoneyError("金额不能为空")
    try:
        return _ensure_finite(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"金额格式无效: {text!r}") from exc


def as_decimal(value):
    """统一入口：Decimal 直通，float 走 from_float，其余走 from_text。"""
    if isinstance(value, Decimal):
        return _ensure_finite(value)
    if isinstance(value, float):
        return from_float(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return from_text(value)


def _ensure_finite(value):
    if not value.is_finite():
        raise MoneyError("金额必须是有限数值，不接受 NaN/Infinity")
    return value


def normalize(value):
    """规范十进制**数学值**：去无意义尾零、展开指数、负零归零。数值不变。

    不能用 `Decimal.normalize()`：它受 context 精度影响，`Decimal("10**30+1").normalize()`
    在默认 28 位下会被**舍入**成 `1E+30`——那是静默改历史金额。这里按 `as_tuple()`
    手工剥尾零，指数相应进位，全程精确。
    """
    decimal_value = as_decimal(value)
    if decimal_value == 0:
        return ZERO
    sign, digits, exponent = decimal_value.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return Decimal((sign, tuple(digits), exponent))


def canonical(value):
    """规范十进制文本：迁移逐行 hash 与独立对账使用**同一编码**。"""
    return format(normalize(value), "f")


def quantize_cents(value):
    """响应字段的两位舍入（ROUND_HALF_EVEN）。**只用于输出，不写库。**"""
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        return as_decimal(value).quantize(CENT, rounding=ROUND_HALF_EVEN)


def is_json_safe(value):
    """转 float 后能否用十进制文本无损往返。"""
    decimal_value = normalize(value)
    if not decimal_value.is_finite():
        return False
    number = float(decimal_value)
    if not math.isfinite(number):
        return False
    try:
        return Decimal(str(number)) == decimal_value
    except InvalidOperation:  # pragma: no cover - str(float) 不会产生非法 Decimal
        return False


def json_number(value):
    """序列化前把 Decimal 转成 JSON number；不能往返就抛错（400），不降精度。"""
    decimal_value = normalize(value)
    if not is_json_safe(decimal_value):
        raise MoneyError(
            f"金额 {canonical(decimal_value)} 超出 JSON number 可无损往返范围"
        )
    return float(decimal_value)


def validate_range(value, minimum=None, maximum=MAX_AMOUNT):
    """范围校验：超界拒绝，不依赖 NUMERIC 无限精度代替应用约束。"""
    decimal_value = as_decimal(value)
    if minimum is not None and decimal_value < minimum:
        raise MoneyError(f"金额不能小于 {canonical(minimum)}")
    if maximum is not None and decimal_value > maximum:
        raise MoneyError(f"金额不能大于 {canonical(maximum)}")
    return decimal_value


def sum_decimals(values):
    """高精度汇总；空序列返回 Decimal(0)。"""
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        total = ZERO
        for value in values:
            total = total + as_decimal(value)
        return total + ZERO


def differs_from_binary_sum(values):
    """旧 SQLite 浮点累加与 Decimal 汇总的差额（只用于报告，不回写源账本）。

    返回 `(decimal_total, binary_total, difference)`；差额非零说明二进制累加误差存在，
    它**不是**需要修正的业务金额。
    """
    decimal_total = sum_decimals(values)
    binary_total = 0.0
    for value in values:
        binary_total += float(as_decimal(value))
    return decimal_total, binary_total, as_decimal(binary_total) - decimal_total
