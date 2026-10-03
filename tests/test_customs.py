"""规则层单测：海关计征汇率的日期推导、顺延、折算、币种归一。

规则算错直接导致税算错，所以这一层必须钉死。用例按「法条怎么说的」组织：

    第十三条  计征汇率 = 上一个月第三个星期三 CFETS 中间价；
              该日非银行间外汇市场交易日的，顺延下一个交易日；折算四舍五入至分。
    第十四条  适用「完成申报之日」的计征汇率。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from fx.customs import (
    CENTS,
    convert_to_cny,
    currency_rate,
    customs_rate,
    next_month,
    normalize_currency,
    normalized_quote,
    previous_month,
    quote_style_label,
    reference_date,
    resolve_rate_date,
    third_wednesday,
)
from fx.customs import FxDataError


# --------------------------------------------------------------------------- #
# 第三个星期三
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "year, month, expected",
    [
        (2026, 9, date(2026, 9, 16)),   # 1 号周二，第一个周三 9-02 → 第三个 9-16
        (2026, 2, date(2026, 2, 18)),   # 春节月，该日休市（真实发生过顺延）
        (2026, 1, date(2026, 1, 21)),
        (2025, 12, date(2025, 12, 17)),
        (2024, 2, date(2024, 2, 21)),   # 闰年 2 月
    ],
)
def test_third_wednesday(year: int, month: int, expected: date) -> None:
    assert third_wednesday(year, month) == expected
    assert expected.weekday() == 2  # 恒为周三


def test_third_wednesday_always_in_same_month() -> None:
    for year in range(2020, 2031):
        for month in range(1, 13):
            assert third_wednesday(year, month).month == month


# --------------------------------------------------------------------------- #
# 上一个月（跨年）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "given, expected",
    [
        (date(2026, 1, 5), (2025, 12)),   # 1 月申报 → 上一个月是上一年的 12 月
        (date(2026, 2, 1), (2026, 1)),
        (date(2026, 12, 31), (2026, 11)),
    ],
)
def test_previous_month(given: date, expected: tuple[int, int]) -> None:
    assert previous_month(given) == expected


def test_next_month_rolls_over() -> None:
    assert next_month(2026, 12) == (2027, 1)
    assert next_month(2026, 8) == (2026, 9)


@pytest.mark.parametrize(
    "declared, expected",
    [
        (date(2026, 1, 5), date(2025, 12, 17)),   # 跨年：1 月申报用上一年 12 月
        (date(2026, 2, 10), date(2026, 1, 21)),
        (date(2026, 10, 3), date(2026, 9, 16)),   # README 里的示例
    ],
)
def test_reference_date(declared: date, expected: date) -> None:
    assert reference_date(declared) == expected


# --------------------------------------------------------------------------- #
# 顺延：不维护交易日历，取「>= 第三个星期三 的最早一条记录」
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, row: dict) -> None:
        self._row = row

    def fetchone(self) -> dict:
        return self._row


class _StubConn:
    """只实现 resolve_rate_date 用到的两个查询的最小替身。

    库里存在的日期本身就是交易日——这是顺延实现的前提，所以只需给一组日期。
    """

    def __init__(self, trading_days: list[date]) -> None:
        self.days = sorted(trading_days)

    def execute(self, sql: str, params: tuple | None = None) -> _Result:
        if "min(rate_date)" in sql:
            ref = params[0]
            return _Result({"rate_date": next((d for d in self.days if d >= ref), None)})
        start, end = params
        return _Result({"n": len([d for d in self.days if start <= d <= end])})


def _month_days(year: int, month: int) -> list[date]:
    """构造一个正常的交易月：跳过周末，约 20 个交易日。"""
    days: list[date] = []
    day = date(year, month, 1)
    while day.month == month:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def test_resolve_rate_date_no_deferral_when_trading_day() -> None:
    conn = _StubConn(_month_days(2026, 9))
    result = resolve_rate_date(conn, date(2026, 10, 3))
    assert result["rate_date"] == "2026-09-16"
    assert result["deferred"] is False
    assert result["gap_days"] == 0


def test_resolve_rate_date_defers_to_next_trading_day() -> None:
    # 2026-02-18 起连休（春节），下一个交易日是 2026-02-24
    days = [d for d in _month_days(2026, 2) if d < date(2026, 2, 18) or d >= date(2026, 2, 24)]
    conn = _StubConn(days)
    result = resolve_rate_date(conn, date(2026, 3, 15))
    assert result["reference_date"] == "2026-02-18"
    assert result["rate_date"] == "2026-02-24"
    assert result["deferred"] is True
    assert result["gap_days"] == 6


def test_resolve_rate_date_warns_when_month_incomplete() -> None:
    # 参考月只有 3 个交易日：此时 min(rate_date) >= ref 会跳到很晚，必须告警
    conn = _StubConn([date(2026, 9, 16), date(2026, 9, 28), date(2026, 10, 20)])
    result = resolve_rate_date(conn, date(2026, 10, 3))
    assert result["data_incomplete"] is True
    assert "数据明显不完整" in result["warning"]


def test_resolve_rate_date_raises_when_no_data() -> None:
    conn = _StubConn([])
    with pytest.raises(FxDataError, match="请先运行 fx-fetch"):
        resolve_rate_date(conn, date(2026, 10, 3))


# --------------------------------------------------------------------------- #
# 折算：四舍五入至分
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "amount, rate, expected",
    [
        ("12345.67", "7.7762", "96002.40"),   # README 示例
        ("1", "6.7351", "6.74"),
        ("0.5", "1", "0.50"),                 # 保留两位
        ("2.345", "1", "2.35"),               # HALF_UP，不是银行家的四舍六入
        ("2.344", "1", "2.34"),
        ("100", "0.042727", "4.27"),          # 日元：1 单位而非 100 单位
    ],
)
def test_convert_to_cny(amount: str, rate: str, expected: str) -> None:
    assert convert_to_cny(Decimal(amount), Decimal(rate)) == Decimal(expected)


def test_convert_to_cny_uses_half_up_not_half_even() -> None:
    # 2.345 用 ROUND_HALF_EVEN 会得到 2.34，法条要求四舍五入，必须是 2.35
    assert convert_to_cny(Decimal("2.345"), Decimal("1")) == Decimal("2.35")
    assert CENTS == Decimal("0.01")


# --------------------------------------------------------------------------- #
# 币种归一
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("USD", "USD"),
        ("usd", "USD"),
        ("美元", "USD"),
        ("美金", "USD"),
        ("JPY", "JPY"),
        ("日元", "JPY"),
        ("CNY", "CNY"),           # 本币：由调用方拒绝
        ("人民币", "CNY"),
    ],
)
def test_normalize_currency_unique(raw: str, expected: str) -> None:
    assert normalize_currency(raw) == (expected, [])


@pytest.mark.parametrize(
    "raw, candidates",
    [
        ("克朗", ["DKK", "NOK", "SEK"]),   # 刻意保留的歧义，要求调用方明确
        ("澳币", ["AUD", "MOP"]),
    ],
)
def test_normalize_currency_ambiguous(raw: str, candidates: list[str]) -> None:
    assert normalize_currency(raw) == (None, candidates)


@pytest.mark.parametrize("raw", ["", "XYZ", "火星币", "比特币"])
def test_normalize_currency_unknown(raw: str) -> None:
    assert normalize_currency(raw) == (None, [])


# --------------------------------------------------------------------------- #
# 报价方向：原始保留、归一可用
# --------------------------------------------------------------------------- #


def test_quote_style_label() -> None:
    assert "直接标价" in quote_style_label("D")
    assert "间接标价" in quote_style_label("I")
    assert quote_style_label(None) is None


def test_normalized_quote_flips_indirect() -> None:
    # 间接标价 CNY/MYR 0.6067 → 归一后 MYR/CNY ≈ 1.6483
    assert normalized_quote("MYR", Decimal("1.648261084556")) == {
        "pair": "MYR/CNY",
        "unit": 1,
        "cny_per_unit": "1.648261084556",
    }


# --------------------------------------------------------------------------- #
# 人工覆盖优先于 CFETS 数据
# --------------------------------------------------------------------------- #


class _OverrideConn:
    """先命中 override 表，命中则不再查中间价表。"""

    def __init__(self, override: dict | None) -> None:
        self.override = override
        self.queried_parity = False

    def execute(self, sql: str, params: tuple | None = None) -> _Result:
        if "fx_rate_override" in sql:
            hit = self.override if self.override and params[0] == self.override["rate_date"] else None
            return _Result(hit)
        self.queried_parity = True
        return _Result(None)


def test_override_takes_precedence() -> None:
    conn = _OverrideConn({"rate_date": date(2026, 9, 16), "cny_per_unit": Decimal("7.1"),
                          "note": "海关总署另行规定"})
    rate = currency_rate(conn, date(2026, 9, 16), "USD")
    assert rate["source"] == "override"
    assert rate["cny_per_unit"] == "7.1"
    assert conn.queried_parity is False  # 命中覆盖后不应再读中间价


def test_customs_rate_missing_currency_raises() -> None:
    class _EmptyConn:
        def execute(self, sql: str, params: tuple | None = None) -> _Result:
            if "min(rate_date)" in sql:
                return _Result({"rate_date": date(2026, 9, 16)})
            if "count(DISTINCT rate_date)" in sql:
                return _Result({"n": 20})
            return _Result(None)

    with pytest.raises(FxDataError, match="没有 USD"):
        customs_rate(_EmptyConn(), "USD", date(2026, 10, 3))
