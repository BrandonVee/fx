"""查询层：MCP 工具与 REST API 共用的入参校验与结果组装。

把入参校验、日期/币种归一、结果组装集中在这里，是为了让 MCP 工具与 REST API
**共用同一份 SQL 与同一套规则**：两个接口只是暴露方式不同，口径必须一致，
否则「MCP 查到 6.7628、API 查到另一个数」这种事迟早会发生。

规则本身（第三个星期三、顺延、折算）在 customs.py，本模块只负责编排与组装。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from .customs import (
    AUTHORITY,
    CURRENCY_NAMES,
    FxDataError,
    LEGACY_NOTE,
    RULE_NOTE,
    convert_to_cny,
    customs_rate,
    describe_currencies,
    normalize_currency,
    normalized_quote,
    quote_style_label,
)

DATA_NOTE = (
    "数据来源：中国货币网（中国外汇交易中心 CFETS）人民币汇率中间价，约 25 个币种，"
    "由 fx-fetch 每日抓取落库。规则依据：《中华人民共和国海关进出口货物征税管理办法》"
    "（海关总署令第 272 号）。本服务只提供汇率与折算，不含税率、完税价格审定与归类判断。"
)

MARKET_NOTE = (
    "市场参考汇率（exchangerate-api.com），**不是** CFETS 人民币汇率中间价，"
    "也**不是计征汇率**，不可用于报关折算、不可用于申报。"
    "来源站每日 UTC 00:00 更新一次（周末与节假日照常更新，这是它相对中间价的价值），"
    "覆盖约 160 个币种，含 CFETS 不公布的冷门币种。"
)

HISTORY_LIMIT_MAX = 500

_SPAN_SQL = (
    "SELECT min(rate_date) AS first_date, max(rate_date) AS last_date, "
    "count(*) AS rows, count(DISTINCT currency) AS currencies, "
    "count(DISTINCT rate_date) AS trading_days FROM fx_central_parity"
)


# --------------------------------------------------------------------------- #
# 入参归一
# --------------------------------------------------------------------------- #


def resolve_currency(raw: str) -> str:
    """把用户输入（代码或中文名）归一为币种代码；无法唯一确定时抛 ValueError。"""
    code, candidates = normalize_currency(raw)
    if code == "CNY":
        raise ValueError(
            "人民币（CNY）是本币。计征汇率用于把外币折合人民币，"
            "若货物本身以人民币计价则无需折算。"
        )
    if code:
        return code
    if candidates:
        listing = "、".join(f"{c}（{CURRENCY_NAMES.get(c, '')}）" for c in candidates)
        raise ValueError(f"「{raw}」可能对应多个币种，请明确指定币种代码：{listing}")
    supported = "、".join(f"{c}（{CURRENCY_NAMES[c]}）" for c in sorted(CURRENCY_NAMES))
    raise ValueError(
        f"无法识别币种「{raw}」。CFETS 中间价当前覆盖以下币种：{supported}。"
        "冷门币种没有中间价，需另行确认适用汇率；"
        "若只是要一个参考价，可用市场参考汇率接口（不可用于申报）。"
    )


def resolve_market_currency(conn: Any, raw: str) -> str:
    """市场参考汇率的币种归一：接受库里存在的 ISO 代码，以及 CFETS 币种的中文名/别名。"""
    text = (raw or "").strip()
    code = text.upper().replace(" ", "")
    if len(code) == 3 and code.isalpha():
        if code == "CNY":
            raise ValueError("人民币（CNY）是本币，无需折算。")
        hit = conn.execute(
            "SELECT 1 FROM fx_market_rate WHERE currency = %s LIMIT 1", (code,)
        ).fetchone()
        if hit:
            return code
    resolved, _ = normalize_currency(raw)
    if resolved and resolved != "CNY":
        return resolved
    raise ValueError(
        f"市场参考汇率中没有「{raw}」。不带 currency 参数可查看全部可用币种。"
    )


def parse_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field} 需要 YYYY-MM-DD 格式的日期，收到 {raw!r}") from None


def parse_amount(raw: Any) -> Decimal:
    """金额归一为 Decimal：先经 str 转换，避免 float 二进制误差参与四舍五入。"""
    try:
        amount = Decimal(str(raw).strip() if isinstance(raw, str) else str(raw))
    except Exception:
        raise ValueError(f"amount 需为数字，收到 {raw!r}") from None
    if amount.is_nan() or amount.is_infinite():
        raise ValueError(f"amount 需为有限数字，收到 {raw!r}")
    if amount < 0:
        raise ValueError("amount 不能为负数。")
    return amount


def parse_limit(raw: Any) -> int:
    try:
        return max(1, min(int(raw), HISTORY_LIMIT_MAX))
    except (TypeError, ValueError):
        raise ValueError(
            f"limit 需为 1-{HISTORY_LIMIT_MAX} 的整数，收到 {raw!r}"
        ) from None


# --------------------------------------------------------------------------- #
# 结果组装
# --------------------------------------------------------------------------- #


def market_payload(conn: Any, currency: str | None) -> dict[str, Any]:
    """最近一份市场参考汇率。currency 为空时返回该日全部币种。

    与中间价**分表**存放，这里也只是读数：市场价永远不会出现在计征汇率的结果里。
    """
    latest = conn.execute("SELECT max(rate_date) AS d FROM fx_market_rate").fetchone()["d"]
    if latest is None:
        raise FxDataError(
            "库中还没有市场参考汇率。需配置 FX_EXCHANGERATE_API_KEY 后运行 fx-fetch 抓取。"
        )

    columns = (
        "SELECT rate_date, currency, pair, quote_style, unit, quoted_rate, cny_per_unit, source "
    )
    if currency:
        row = conn.execute(
            columns + "FROM fx_market_rate WHERE currency = %s ORDER BY rate_date DESC LIMIT 1",
            (currency,),
        ).fetchone()
        if row is None:
            raise FxDataError(
                f"市场参考汇率中没有 {currency}。不带 currency 参数可查看全部可用币种。"
            )
        rows = [row]
        rate_date = row["rate_date"]
    else:
        rate_date = latest
        rows = conn.execute(
            columns + "FROM fx_market_rate WHERE rate_date = %s ORDER BY currency",
            (rate_date,),
        ).fetchall()

    today = date.today()
    result: dict[str, Any] = {
        "rate_date": rate_date.isoformat(),
        "as_of": today.isoformat(),
        "stale_days": (today - rate_date).days,
        "currency": currency,
        "currency_name": CURRENCY_NAMES.get(currency) if currency else None,
        "count": len(rows),
        "rates": [
            {
                "currency": r["currency"],
                "currency_name": CURRENCY_NAMES.get(r["currency"]),
                "rate_date": r["rate_date"].isoformat(),
                "cny_per_unit": str(r["cny_per_unit"]),
                "quote": {
                    "pair": r["pair"],
                    "style": r["quote_style"],
                    "style_label": quote_style_label(r["quote_style"]),
                    "unit": r["unit"],
                    "quoted_rate": str(r["quoted_rate"]),
                    "normalized": normalized_quote(r["currency"], r["cny_per_unit"]),
                },
                "source": r["source"],
            }
            for r in rows
        ],
        "latest_market_date": latest.isoformat(),
        "source": "MARKET",
        "note": MARKET_NOTE,
    }
    if currency:
        result["cny_per_unit"] = str(rows[0]["cny_per_unit"])
        result["cfets_reference"] = _cfets_reference(conn, currency, rows[0]["cny_per_unit"])
    return result


def _cfets_reference(conn: Any, currency: str, market_rate: Any) -> dict[str, Any] | None:
    """同币种的最新 CFETS 中间价，以及与市场价的偏离。仅供核对。"""
    row = conn.execute(
        "SELECT rate_date, cny_per_unit FROM fx_central_parity WHERE currency = %s "
        "ORDER BY rate_date DESC LIMIT 1",
        (currency,),
    ).fetchone()
    if row is None:
        return None
    central = Decimal(row["cny_per_unit"])
    diff = (Decimal(market_rate) - central) / central * 100
    return {
        "rate_date": row["rate_date"].isoformat(),
        "cny_per_unit": str(central),
        "market_vs_central_pct": f"{diff:.4f}",
        "note": "中间价与市场价的偏离，仅用于判断市场价是否异常；两者口径不同，不可互换。",
    }


def _market_hint(conn: Any, currency: str) -> str:
    """计征路径缺数据时，附一句市场参考价——只作核对，不改变「无计征汇率」的结论。"""
    row = conn.execute(
        "SELECT rate_date, cny_per_unit FROM fx_market_rate WHERE currency = %s "
        "ORDER BY rate_date DESC LIMIT 1",
        (currency,),
    ).fetchone()
    if row is None:
        return ""
    return (
        f"（市场参考价约 1 {currency} = {row['cny_per_unit']} CNY，日期 "
        f"{row['rate_date'].isoformat()}，仅供核对，不可用于申报。）"
    )


def rate_payload(conn: Any, currency: str, declaration_date: date) -> dict[str, Any]:
    """计征汇率结论 + 规则说明，供报关类接口共用。"""
    try:
        result = customs_rate(conn, currency, declaration_date)
    except FxDataError as exc:
        # 缺数据仍然报错——不能用市场价顶上；只在消息里给一个可核对的参考值。
        raise FxDataError(f"{exc} {_market_hint(conn, currency)}".strip()) from None
    result["rate_date_basis"] = (
        f"{result['reference_date']} 不是银行间外汇市场交易日，"
        f"顺延采用下一个交易日 {result['rate_date']} 公布的中间价"
        if result["deferred"]
        else f"第三个星期三 {result['reference_date']} 本身即为交易日，采用当日中间价"
    )
    result["rule"] = RULE_NOTE
    result["authority"] = AUTHORITY
    result["legacy_caveat"] = LEGACY_NOTE
    return result


def convert_payload(
    conn: Any, amount: Decimal, currency: str, declaration_date: date
) -> dict[str, Any]:
    """按计征汇率折人民币（四舍五入至分），附可复核的算式。"""
    result = rate_payload(conn, currency, declaration_date)
    cny_amount = convert_to_cny(amount, Decimal(result["cny_per_unit"]))
    return {
        "currency": result["currency"],
        "currency_name": result["currency_name"],
        "amount": str(amount),
        "declaration_date": result["declaration_date"],
        "rate_date": result["rate_date"],
        "deferred": result["deferred"],
        "cny_per_unit": result["cny_per_unit"],
        "cny_amount": str(cny_amount),
        "calculation": f"{amount} × {result['cny_per_unit']} = {cny_amount}",
        "rounding": "四舍五入至分（海关总署令第 272 号第十三条）",
        "rate_date_basis": result["rate_date_basis"],
        "source": result["source"],
        "source_note": result["source_note"],
        "override_note": result.get("override_note"),
        "authority": AUTHORITY,
        "legacy_caveat": LEGACY_NOTE,
    }


def spot_payload(conn: Any, currency: str, trade_date: date) -> dict[str, Any]:
    """指定交易日的原始中间价；无数据时不猜值，返回前后最近可查日期。"""
    row = conn.execute(
        "SELECT rate_date, pair, quote_style, unit, quoted_rate, cny_per_unit, source "
        "FROM fx_central_parity WHERE rate_date = %s AND currency = %s",
        (trade_date, currency),
    ).fetchone()
    if row:
        return {
            "currency": currency,
            "currency_name": CURRENCY_NAMES.get(currency),
            "date": trade_date.isoformat(),
            "available": True,
            "cny_per_unit": str(row["cny_per_unit"]),
            "quote": {
                "pair": row["pair"],
                "style": row["quote_style"],
                "style_label": quote_style_label(row["quote_style"]),
                "unit": row["unit"],
                "quoted_rate": str(row["quoted_rate"]),
                "normalized": normalized_quote(currency, row["cny_per_unit"]),
            },
            "source": row["source"],
            "source_note": "中国货币网（中国外汇交易中心）人民币汇率中间价",
        }

    previous = conn.execute(
        "SELECT rate_date FROM fx_central_parity "
        "WHERE currency = %s AND rate_date < %s ORDER BY rate_date DESC LIMIT 1",
        (currency, trade_date),
    ).fetchone()
    following = conn.execute(
        "SELECT rate_date FROM fx_central_parity "
        "WHERE currency = %s AND rate_date > %s ORDER BY rate_date LIMIT 1",
        (currency, trade_date),
    ).fetchone()
    return {
        "currency": currency,
        "currency_name": CURRENCY_NAMES.get(currency),
        "date": trade_date.isoformat(),
        "available": False,
        "message": (
            f"{trade_date.isoformat()} 没有 {currency} 的中间价，"
            "该日可能是周末或法定节假日（银行间外汇市场不开市）。"
        ),
        "nearest_before": previous["rate_date"].isoformat() if previous else None,
        "nearest_after": following["rate_date"].isoformat() if following else None,
    }


def latest_payload(conn: Any, currency: str | None) -> dict[str, Any]:
    """最近一个交易日公布的中间价（「当前价」）。

    currency 为空时返回该日的全部币种。刻意不带数据就报错而不是猜值：
    中间价每个交易日只公布一次，休市日不公布，所以「最新」本身就是个日期问题。
    """
    latest = conn.execute(
        "SELECT max(rate_date) AS d FROM fx_central_parity"
    ).fetchone()["d"]
    if latest is None:
        raise FxDataError("库中还没有中间价数据，请先运行 fx-fetch 抓取。")

    if currency:
        row = conn.execute(
            "SELECT rate_date, pair, quote_style, unit, quoted_rate, cny_per_unit, source "
            "FROM fx_central_parity WHERE currency = %s ORDER BY rate_date DESC LIMIT 1",
            (currency,),
        ).fetchone()
        if row is None:
            raise FxDataError(
                f"库中没有 {currency}（{CURRENCY_NAMES.get(currency, '未知币种')}）"
                "的中间价记录。请用 /api/currencies 查看可用币种。"
            )
        rows = [dict(row, currency=currency)]
        rate_date = row["rate_date"]
    else:
        rate_date = latest
        rows = conn.execute(
            "SELECT rate_date, currency, pair, quote_style, unit, quoted_rate, cny_per_unit, source "
            "FROM fx_central_parity WHERE rate_date = %s ORDER BY currency",
            (rate_date,),
        ).fetchall()

    today = date.today()
    stale_days = (today - rate_date).days
    result: dict[str, Any] = {
        "rate_date": rate_date.isoformat(),
        "as_of": today.isoformat(),
        "stale_days": stale_days,
        "currency": currency,
        "currency_name": CURRENCY_NAMES.get(currency) if currency else None,
        "count": len(rows),
        "rates": [
            {
                "currency": r["currency"],
                "currency_name": CURRENCY_NAMES.get(r["currency"]),
                "rate_date": r["rate_date"].isoformat(),
                "cny_per_unit": str(r["cny_per_unit"]),
                "quote": {
                    "pair": r["pair"],
                    "style": r["quote_style"],
                    "style_label": quote_style_label(r["quote_style"]),
                    "unit": r["unit"],
                    "quoted_rate": str(r["quoted_rate"]),
                    "normalized": normalized_quote(r["currency"], r["cny_per_unit"]),
                },
                "source": r["source"],
            }
            for r in rows
        ],
        "latest_rate_date": latest.isoformat(),
        "source_note": "中国货币网（中国外汇交易中心）人民币汇率中间价",
        "note": (
            "人民币汇率中间价由 CFETS 每个交易日约 09:15 公布一次，当日不再变动；"
            "周末与法定节假日休市，不公布新价。stale_days 为距今天的天数，"
            "数值偏大通常是休市或抓取尚未跟上。这是中间价而非实时行情，"
            "也不能用于报关折算——报关请用计征汇率接口。"
        ),
    }
    if currency:
        result["cny_per_unit"] = str(rows[0]["cny_per_unit"])
    if rate_date != latest:
        result["warning"] = (
            f"{currency} 的最新中间价日期为 {rate_date.isoformat()}，"
            f"早于库中最新日期 {latest.isoformat()}，该币种可能已停止公布。"
        )
    return result


def history_payload(
    conn: Any, currency: str, start: date, end: date, limit: int
) -> dict[str, Any]:
    """区间内的中间价序列（按日期倒序），附区间统计。"""
    if start > end:
        raise ValueError("start_date 不能晚于 end_date。")
    rows = conn.execute(
        "SELECT rate_date, pair, unit, quoted_rate, cny_per_unit "
        "FROM fx_central_parity "
        "WHERE currency = %s AND rate_date BETWEEN %s AND %s "
        "ORDER BY rate_date DESC LIMIT %s",
        (currency, start, end, limit),
    ).fetchall()
    total = conn.execute(
        "SELECT count(*) AS n FROM fx_central_parity "
        "WHERE currency = %s AND rate_date BETWEEN %s AND %s",
        (currency, start, end),
    ).fetchone()["n"]

    values = [r["cny_per_unit"] for r in rows]
    return {
        "currency": currency,
        "currency_name": CURRENCY_NAMES.get(currency),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "count": len(rows),
        "total_in_range": total,
        "summary": (
            {
                "min_cny_per_unit": str(min(values)),
                "max_cny_per_unit": str(max(values)),
                "latest_cny_per_unit": str(values[0]),
            }
            if values
            else None
        ),
        "records": [
            {
                "date": r["rate_date"].isoformat(),
                "pair": r["pair"],
                "unit": r["unit"],
                "quoted_rate": str(r["quoted_rate"]),
                "cny_per_unit": str(r["cny_per_unit"]),
                "normalized": normalized_quote(currency, r["cny_per_unit"]),
            }
            for r in rows
        ],
        "note": "中间价，非报关计征汇率。报关折算请用计征汇率接口。",
    }


def currencies_payload(conn: Any) -> dict[str, Any]:
    """可用币种清单与数据覆盖区间。"""
    span = conn.execute(_SPAN_SQL).fetchone()
    present = {
        r["currency"]
        for r in conn.execute("SELECT DISTINCT currency FROM fx_central_parity").fetchall()
    }
    overrides = conn.execute("SELECT count(*) AS n FROM fx_rate_override").fetchone()["n"]
    return {
        "currencies": [
            {**item, "in_db": item["currency"] in present}
            for item in describe_currencies()
        ],
        "coverage": {
            "first_rate_date": span["first_date"].isoformat() if span["first_date"] else None,
            "last_rate_date": span["last_date"].isoformat() if span["last_date"] else None,
            "trading_days": span["trading_days"],
            "records": span["rows"],
            "currency_count": span["currencies"],
        },
        "override_rows": overrides,
        "note": (
            "覆盖区间由最新的抓取结果决定。申报日适用的汇率取自「上一个月」，"
            "因此可计算的申报日最早约为 first_rate_date 往后两个月。"
        ),
        "legacy_caveat": LEGACY_NOTE,
    }


def dataset_payload(conn: Any) -> dict[str, Any]:
    """数据集说明（对应 MCP 资源 fx://dataset-info）。"""
    span = conn.execute(_SPAN_SQL).fetchone()
    return {
        "source": {
            "name": "人民币汇率中间价",
            "publisher": "中国人民银行授权 中国外汇交易中心（CFETS）公布",
            "url": "https://www.chinamoney.com.cn/chinese/bkccpr/",
            "pairs": "25 个货币对，D 直接标价 / I 间接标价混用",
            "fetched_by": "fx-fetch（每日定时抓取，幂等覆盖）",
        },
        "coverage": {
            "first_rate_date": span["first_date"].isoformat() if span["first_date"] else None,
            "last_rate_date": span["last_date"].isoformat() if span["last_date"] else None,
            "trading_days": span["trading_days"],
            "records": span["rows"],
            "currencies": span["currencies"],
        },
        "rule": {
            "authority": AUTHORITY,
            "summary": RULE_NOTE,
            "legacy_caveat": LEGACY_NOTE,
        },
        "scope": {
            "provides": ["计征汇率", "上一个月第三个星期三的推导与顺延", "外币折人民币（至分）"],
            "excludes": ["税率", "完税价格审定", "商品归类", "关税税额计算"],
        },
        "data_note": DATA_NOTE,
    }


def dataset_json(conn: Any) -> str:
    return json.dumps(dataset_payload(conn), ensure_ascii=False, indent=2)
