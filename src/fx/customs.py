"""海关计征汇率的规则层。

只做两件事：**决定用哪一天的汇率**，以及**按那一天折算**。数据本身来自
fx_central_parity（CFETS 人民币汇率中间价），本模块不改写数据。

法律依据
--------
《中华人民共和国海关进出口货物征税管理办法》（海关总署令第 272 号，2025-01-14 公布）

    第十三条  进出口货物的价格及有关费用以外币计价的，按照计征汇率折合为人民币
              计算计税价格，采用四舍五入法计算至分。海关每月使用的计征汇率为上一个
              月第三个星期三中国人民银行授权中国外汇交易中心公布的人民币汇率中间价，
              第三个星期三非银行间外汇市场交易日的，顺延采用下一个交易日公布的人民币
              汇率中间价。如果上述汇率发生重大波动，海关总署认为必要时，可以另行规定
              计征汇率，并且对外公布。
    第十四条  进出口货物应当适用纳税人、扣缴义务人完成申报之日实施的税率和计征汇率。

版本变更提醒
------------
旧口径（海关总署令第 124 号，已废止）与现行口径**两处不同**，网上资料多为旧版：

    | 维度     | 旧（124 号令）                          | 现（272 号令）                    |
    |----------|-----------------------------------------|-----------------------------------|
    | 汇率口径 | 基准汇率 / 中行现汇买入价卖出价中间值   | CFETS 人民币汇率中间价            |
    | 顺延条件 | 法定节假日 → 顺延第四个星期三           | 非银行间外汇市场交易日 → 下一交易日 |

「顺延」的实现
--------------
不维护交易日历：CFETS 只在银行间外汇市场交易日公布中间价，因此**库里存在的日期
本身就是交易日**。取「>= 第三个星期三 的最早一条记录」即可同时覆盖两种情况——
星期三当天是交易日就用当天，不是就自动落到下一个交易日。这也天然规避了法定节假日
调休的坑（周末永远不是银行间外汇市场交易日）。
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

CENTS = Decimal("0.01")

AUTHORITY = "《中华人民共和国海关进出口货物征税管理办法》（海关总署令第 272 号）"

RULE_NOTE = (
    "第十三条：海关每月使用的计征汇率为上一个月第三个星期三中国人民银行授权中国外汇交易中心"
    "公布的人民币汇率中间价，第三个星期三非银行间外汇市场交易日的，顺延采用下一个交易日公布的"
    "人民币汇率中间价；第十四条：进出口货物应当适用纳税人、扣缴义务人完成申报之日实施的税率和计征汇率。"
)

LEGACY_NOTE = (
    "旧口径（海关总署令第 124 号，已废止）：采用上一个月第三个星期三中国人民银行公布的外币对人民币"
    "基准汇率；非基准汇率币种采用同一时间中国银行公布的现汇买入价和现汇卖出价的中间值；遇法定节假日"
    "顺延第四个星期三。与现行口径在「汇率口径」和「顺延条件」两处都不同，不可混用。"
)

# 顺延跨度超过该天数时视为数据可能不连续，向调用方提示。
GAP_WARN_DAYS = 10

# 参考月份的交易日数低于该值时，判定数据明显不完整（正常月份约 20-22 个交易日）。
MIN_MONTH_TRADING_DAYS = 10

CURRENCY_NAMES: dict[str, str] = {
    "USD": "美元", "EUR": "欧元", "JPY": "日元", "HKD": "港元", "GBP": "英镑",
    "AUD": "澳大利亚元", "NZD": "新西兰元", "SGD": "新加坡元", "CHF": "瑞士法郎",
    "CAD": "加拿大元", "MOP": "澳门元", "MYR": "马来西亚林吉特", "RUB": "俄罗斯卢布",
    "ZAR": "南非兰特", "KRW": "韩元", "AED": "阿联酋迪拉姆", "SAR": "沙特里亚尔",
    "HUF": "匈牙利福林", "PLN": "波兰兹罗提", "DKK": "丹麦克朗", "SEK": "瑞典克朗",
    "NOK": "挪威克朗", "TRY": "土耳其里拉", "MXN": "墨西哥比索", "THB": "泰铢",
}

# 中文别名 -> 币种代码（同一别名对应多个币种时视为歧义，要求调用方明确指定）
_ALIAS_SOURCES: dict[str, list[str]] = {
    "USD": ["美元", "美金", "美圆", "美刀"],
    "EUR": ["欧元", "欧圆"],
    "JPY": ["日元", "日圆", "日币"],
    "HKD": ["港元", "港币", "香港元"],
    "GBP": ["英镑", "英磅"],
    "AUD": ["澳大利亚元", "澳洲元", "澳元"],
    "NZD": ["新西兰元", "纽元", "纽西兰元"],
    "SGD": ["新加坡元", "新元", "星元"],
    "CHF": ["瑞士法郎", "瑞郎", "瑞士法朗"],
    "CAD": ["加拿大元", "加元"],
    "MOP": ["澳门元", "澳门币", "澳币", "澳门圆"],
    "MYR": ["马来西亚林吉特", "林吉特", "马币"],
    "RUB": ["俄罗斯卢布", "卢布"],
    "ZAR": ["南非兰特", "兰特"],
    "KRW": ["韩元", "韩国元", "韩币"],
    "AED": ["阿联酋迪拉姆", "迪拉姆"],
    "SAR": ["沙特里亚尔", "里亚尔"],
    "HUF": ["匈牙利福林", "福林"],
    "PLN": ["波兰兹罗提", "兹罗提"],
    "DKK": ["丹麦克朗"],
    "SEK": ["瑞典克朗"],
    "NOK": ["挪威克朗"],
    "TRY": ["土耳其里拉"],
    "MXN": ["墨西哥比索", "比索"],
    "THB": ["泰铢", "泰国铢"],
}

# 刻意保留的歧义项：口语里的简称无法唯一落到某个币种，报候选而不是猜。
_AMBIGUOUS: dict[str, list[str]] = {
    "克朗": ["DKK", "SEK", "NOK"],
    "澳币": ["AUD", "MOP"],
    "元": sorted(CURRENCY_NAMES),
}

_ALIAS_LOOKUP: dict[str, set[str]] = {}
for _code, _names in _ALIAS_SOURCES.items():
    for _name in _names:
        _ALIAS_LOOKUP.setdefault(_name, set()).add(_code)
for _name, _codes in _AMBIGUOUS.items():
    for _code in _codes:
        _ALIAS_LOOKUP.setdefault(_name, set()).add(_code)


class FxDataError(RuntimeError):
    """数据缺失或不满足规则。消息面向调用方，可直接展示。"""


# --------------------------------------------------------------------------- #
# 日期规则
# --------------------------------------------------------------------------- #


QUOTE_STYLE_LABELS = {
    "D": "直接标价法（1 或 100 单位外币 = ? 人民币）",
    "I": "间接标价法（1 人民币 = ? 外币）",
}


def quote_style_label(style: str | None) -> str | None:
    """标价法的中文说明，避免调用方去猜 D / I 的含义。"""
    return QUOTE_STYLE_LABELS.get(style) if style else None


def normalized_quote(currency: str, cny_per_unit: Any) -> dict[str, Any]:
    """统一方向的报价：1 单位外币 = ? 人民币。

    原始报价两种标价法混用，且 JPY 以 100 为单位——直接拿 quoted_rate 算会差出
    数量级。这里给出可直接使用的归一值；原始报价仍保留在 quote 里，
    用于对账（报关口径要能回中国货币网核对，所以不改写原始值）。
    """
    return {"pair": f"{currency}/CNY", "unit": 1, "cny_per_unit": str(cny_per_unit)}


def third_wednesday(year: int, month: int) -> date:
    """指定月份的第三个星期三。"""
    first = date(year, month, 1)
    offset = (2 - first.weekday()) % 7  # date.weekday(): 周一=0，周三=2
    return first + timedelta(days=offset + 14)


def previous_month(d: date) -> tuple[int, int]:
    """d 所在月份的上一个月。"""
    return (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)


def reference_date(declaration_date: date) -> date:
    """完成申报之日所对应的「上一个月第三个星期三」。纯日期计算，不查库。"""
    year, month = previous_month(declaration_date)
    return third_wednesday(year, month)


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


# --------------------------------------------------------------------------- #
# 查库
# --------------------------------------------------------------------------- #


def resolve_rate_date(conn: psycopg.Connection, declaration_date: date) -> dict[str, Any]:
    """定位申报日适用的计征汇率日期（含顺延），并做数据完整度自检。"""
    ref = reference_date(declaration_date)
    row = conn.execute(
        "SELECT min(rate_date) AS rate_date FROM fx_central_parity WHERE rate_date >= %s",
        (ref,),
    ).fetchone()
    rate_date = row["rate_date"] if row else None
    if rate_date is None:
        y, m = previous_month(declaration_date)
        raise FxDataError(
            f"库中没有 {ref.isoformat()} 及之后的中间价数据，无法确定"
            f"{declaration_date.isoformat()} 的计征汇率。"
            f"该申报日适用 {y:04d}-{m:02d} 月第三个星期三（{ref.isoformat()}）的中间价，"
            "请先运行 fx-fetch 抓取数据。"
        )

    gap_days = (rate_date - ref).days
    # 参考月份已有的交易日数：正常月份约 20-22 天。数值过低说明库中数据有洞，
    # 此时「min(rate_date) >= ref」会跳到一个偏晚的日期，结果看似正常却是错的。
    month_start, month_end = month_bounds(ref.year, ref.month)
    month_days = conn.execute(
        "SELECT count(DISTINCT rate_date) AS n FROM fx_central_parity "
        "WHERE rate_date BETWEEN %s AND %s",
        (month_start, month_end),
    ).fetchone()["n"]

    warnings: list[str] = []
    if month_days < MIN_MONTH_TRADING_DAYS:
        warnings.append(
            f"参考月份 {ref.year:04d}-{ref.month:02d} 在库中只有 {month_days} 个交易日"
            f"（正常约 20-22 个），数据明显不完整，本次结果可能取到了偏晚的日期，"
            "请先运行 fx-fetch 补齐后再使用。"
        )
    if gap_days >= GAP_WARN_DAYS:
        warnings.append(
            f"顺延跨度为 {gap_days} 天，超出正常的 1-7 天，可能是数据缺口而非节假日顺延。"
        )

    result: dict[str, Any] = {
        "reference_date": ref.isoformat(),
        "reference_month": f"{ref.year:04d}-{ref.month:02d}",
        "reference_month_trading_days": month_days,
        "rate_date": rate_date.isoformat(),
        "deferred": rate_date != ref,
        "gap_days": gap_days,
    }
    if warnings:
        result["warning"] = " ".join(warnings)
        result["data_incomplete"] = month_days < MIN_MONTH_TRADING_DAYS
    return result


def currency_rate(
    conn: psycopg.Connection, rate_date: date, currency: str
) -> dict[str, Any] | None:
    """取某日某币种的计征汇率；人工覆盖优先于 CFETS 数据。"""
    override = conn.execute(
        "SELECT cny_per_unit, note FROM fx_rate_override "
        "WHERE rate_date = %s AND currency = %s",
        (rate_date, currency),
    ).fetchone()
    if override:
        return {
            "source": "override",
            "source_note": "人工录入 / 海关总署另行规定的计征汇率",
            "currency": currency,
            "currency_name": CURRENCY_NAMES.get(currency),
            "rate_date": rate_date.isoformat(),
            "pair": None,
            "quote_style": None,
            "unit": None,
            "quoted_rate": None,
            "cny_per_unit": str(override["cny_per_unit"]),
            "override_note": override["note"],
        }

    row = conn.execute(
        "SELECT pair, quote_style, unit, quoted_rate, cny_per_unit, source "
        "FROM fx_central_parity WHERE rate_date = %s AND currency = %s",
        (rate_date, currency),
    ).fetchone()
    if not row:
        return None
    return {
        "source": row["source"],
        "source_note": "中国货币网（中国外汇交易中心）人民币汇率中间价",
        "currency": currency,
        "currency_name": CURRENCY_NAMES.get(currency),
        "rate_date": rate_date.isoformat(),
        "pair": row["pair"],
        "quote_style": row["quote_style"],
        "quote_style_label": quote_style_label(row["quote_style"]),
        "unit": row["unit"],
        "quoted_rate": str(row["quoted_rate"]),
        "cny_per_unit": str(row["cny_per_unit"]),
        "normalized": normalized_quote(currency, row["cny_per_unit"]),
    }


def customs_rate(
    conn: psycopg.Connection, currency: str, declaration_date: date
) -> dict[str, Any]:
    """申报日 + 币种 -> 完整计征汇率结论。"""
    when = resolve_rate_date(conn, declaration_date)
    rate = currency_rate(conn, date.fromisoformat(when["rate_date"]), currency)
    if rate is None:
        raise FxDataError(
            f"{when['rate_date']} 没有 {currency}（{CURRENCY_NAMES.get(currency, '未知币种')}）"
            "的中间价记录。CFETS 中间价覆盖约 25 个币种，冷门币种不在其中；"
            "请用 list_fx_currencies 查看可用币种。"
        )
    return {
        "currency": currency,
        "currency_name": CURRENCY_NAMES.get(currency),
        "declaration_date": declaration_date.isoformat(),
        "reference_month": when["reference_month"],
        "reference_date": when["reference_date"],
        "reference_month_trading_days": when["reference_month_trading_days"],
        "rate_date": when["rate_date"],
        "deferred": when["deferred"],
        "gap_days": when["gap_days"],
        **{k: when[k] for k in ("warning", "data_incomplete") if k in when},
        "cny_per_unit": rate["cny_per_unit"],
        "quote": {
            "pair": rate["pair"],
            "style": rate["quote_style"],
            "style_label": rate.get("quote_style_label"),
            "unit": rate["unit"],
            "quoted_rate": rate["quoted_rate"],
            "normalized": rate.get("normalized"),
        },
        "source": rate["source"],
        "source_note": rate["source_note"],
        "override_note": rate.get("override_note"),
    }


# --------------------------------------------------------------------------- #
# 折算
# --------------------------------------------------------------------------- #


def convert_to_cny(amount: Decimal, cny_per_unit: Decimal) -> Decimal:
    """按计征汇率折合人民币，四舍五入至分（272 号令第十三条）。"""
    return (amount * cny_per_unit).quantize(CENTS, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# 币种归一
# --------------------------------------------------------------------------- #


def normalize_currency(raw: str) -> tuple[str | None, list[str]]:
    """把用户输入归一为币种代码。

    返回 (code, candidates)：
      - (代码, [])      唯一命中
      - (None, [候选…]) 歧义，候选按代码排序
      - (None, [])      无法识别
    """
    text = (raw or "").strip()
    if not text:
        return None, []

    upper = text.upper().replace(" ", "")
    if upper in CURRENCY_NAMES:
        return upper, []
    if upper in {"CNY", "RMB", "CNH"} or text in {"人民币", "元人民币", "人币"}:
        return "CNY", []  # 由调用方处理：本币无需折算

    hits = _ALIAS_LOOKUP.get(text) or _ALIAS_LOOKUP.get(text.replace(" ", ""))
    if not hits:
        return None, []
    if len(hits) == 1:
        return next(iter(hits)), []
    return None, sorted(hits)


def describe_currencies() -> list[dict[str, str]]:
    """可用币种清单（含中文名与别名），供资源与提示词使用。"""
    return [
        {
            "currency": code,
            "name": name,
            "aliases": "、".join(sorted(_ALIAS_SOURCES.get(code, []))),
        }
        for code, name in sorted(CURRENCY_NAMES.items())
    ]
