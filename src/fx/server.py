#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""fx MCP Server
=================

海关计征汇率服务：把「某次申报该用哪一天的汇率、怎么折人民币」这件事，
从规则到数据做成可查、可复算的 MCP 工具。

为什么单独做一个服务
--------------------
HS 编码回答的是「这是什么」，汇率回答的是「值多少钱」。报关场景里这是两个问题，
但必须连起来用：查完编码 → 确定完税价格 → 折人民币 → 算税。所以汇率是贸易工具链的
底座件，而不是编码库的一个字段。

口径与规则
----------
数据是**中国外汇交易中心（CFETS）的人民币汇率中间价**，来自中国货币网。
适用规则是《中华人民共和国海关进出口货物征税管理办法》（海关总署令第 272 号）：
海关每月使用的计征汇率为**上一个月第三个星期三**中国人民银行授权中国外汇交易中心
公布的中间价；该日非银行间外汇市场交易日的顺延下一个交易日；进出口货物适用
**完成申报之日**的计征汇率；折人民币四舍五入至分。

⚠️ 网上多数资料仍是旧口径（海关总署令第 124 号）：用「基准汇率 / 中国银行现汇
买入价与卖出价中间值」，且顺延条件是「法定节假日」。新旧口径算出的数不一样。

工具：
    get_customs_fx_rate      某次申报适用的计征汇率（含日期推导与依据）
    convert_customs_value    按计征汇率折人民币（四舍五入至分）
    get_fx_rate              指定交易日的中间价（非报关场景）
    get_fx_rate_history      中间价历史序列
    list_fx_currencies       可用币种与数据覆盖区间

资源：fx://dataset-info
提示词：customs_value_workflow

数据准备（一次性）
------------------
    uv sync
    uv run fx-fetch --full        # 回填中间价（表为空时自动触发）

启动
----
    uv run fx                     # stdio（本地 MCP 客户端默认）
    uv run fx --http 8766         # Streamable HTTP
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from .db import connect
from .service import (
    convert_payload,
    currencies_payload,
    dataset_json,
    history_payload,
    latest_payload,
    market_payload,
    parse_amount,
    parse_date,
    parse_limit,
    rate_payload,
    resolve_currency,
    resolve_market_currency,
    spot_payload,
)

SERVER_NAME = "fx"
SERVER_VERSION = "1.0.0"

mcp = MCPServer(
    SERVER_NAME,
    instructions=(
        "海关计征汇率查询与折算服务，用于进出口货物完税价格折合人民币。"
        "计征汇率不是当天的市场汇率：按海关总署令第 272 号，它是**上一个月第三个星期三**"
        "中国外汇交易中心公布的人民币汇率中间价（该日非交易日的顺延下一个交易日），"
        "并且适用**完成申报之日**。"
        "涉及报关折算时必须用 get_customs_fx_rate / convert_customs_value，"
        "不要用 get_fx_rate 或任何市场汇率代替。"
    ),
    version=SERVER_VERSION,
)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


@mcp.tool()
def get_customs_fx_rate(currency: str, declaration_date: str) -> dict[str, Any]:
    """查询某次申报适用的海关计征汇率。

    按海关总署令第 272 号第十三条、第十四条：进出口货物适用**完成申报之日**的
    计征汇率，而计征汇率为**上一个月第三个星期三**中国外汇交易中心公布的人民币
    汇率中间价；该日非银行间外汇市场交易日的，顺延采用下一个交易日公布的中间价。

    返回里带上完整推导：申报日对应的上一个月、第三个星期三的日期、实际采用的
    rate_date、是否发生过顺延，以及原始货币对和标价法，便于核对。

    注意：这不是市场汇率，也不能用查询当天的汇率代替；旧的 124 号令口径
    （基准汇率 / 中国银行现汇中间值 / 法定节假日顺延）已废止。

    Args:
        currency: 外币币种，代码或中文名均可，如 "USD"、"美元"、"JPY"、"日元"。
        declaration_date: 完成申报之日，YYYY-MM-DD。

    Returns:
        含 rate_date、cny_per_unit（1 单位外币折多少人民币）、deferred、
        rate_date_basis、quote 与规则依据的字典。
    """
    code = resolve_currency(currency)
    when = parse_date(declaration_date, "declaration_date")
    with connect() as conn:
        return rate_payload(conn, code, when)


@mcp.tool()
def convert_customs_value(
    amount: float, currency: str, declaration_date: str
) -> dict[str, Any]:
    """按海关计征汇率把外币金额折合人民币，四舍五入至分。

    适用场景：以成交价格（或其他估价方法确定的价格）为基础算完税价格时，
    有关费用以外币计价的，需按计征汇率折合为人民币，采用四舍五入法计算至分
    （海关总署令第 272 号第十三条）。

    工具只做折算，不涉及税率与税额：完税价格与税额请另行计算。

    Args:
        amount: 外币金额。
        currency: 外币币种，代码或中文名均可。
        declaration_date: 完成申报之日，YYYY-MM-DD。

    Returns:
        含 rate_date、cny_per_unit、cny_amount（折合人民币，保留 2 位小数）
        和 calculation（可复核的算式）的字典。
    """
    code = resolve_currency(currency)
    when = parse_date(declaration_date, "declaration_date")
    value = parse_amount(amount)

    with connect() as conn:
        return convert_payload(conn, value, code, when)


@mcp.tool()
def get_fx_rate(currency: str, trade_date: str) -> dict[str, Any]:
    """查询指定交易日公布的人民币汇率中间价（非报关场景）。

    这里是**原始中间价查询**，不做任何「上一个月第三个星期三」推算。
    报关折算请改用 get_customs_fx_rate。若该日没有中间价（周末、法定节假日），
    会返回该日期前后最近的可查日期，而不是猜一个值。

    Args:
        currency: 外币币种，代码或中文名均可。
        trade_date: 交易日期，YYYY-MM-DD。

    Returns:
        含 quoted_rate、cny_per_unit、quote 与可用性说明的字典。
    """
    code = resolve_currency(currency)
    target = parse_date(trade_date, "trade_date")
    with connect() as conn:
        return spot_payload(conn, code, target)


@mcp.tool()
def get_latest_fx_rate(currency: str = "") -> dict[str, Any]:
    """查询最近一个交易日公布的人民币汇率中间价（当前价）。

    中间价由 CFETS 每个交易日约 09:15 公布一次，当日不再变动；周末与法定节假日
    休市，不公布新价。因此「当前价」= 最近一个交易日公布的中间价，返回里带
    rate_date 与 stale_days（距今天数）：数值偏大通常是休市或抓取没跟上。

    不传 currency 时返回该日全部币种。这是中间价而非实时行情，也**不能用于报关
    折算**——报关请用 get_customs_fx_rate。

    Args:
        currency: 外币币种，代码或中文名均可；留空返回全部币种。

    Returns:
        含 rate_date、as_of、stale_days、rates（各币种 cny_per_unit）的字典。
    """
    code = resolve_currency(currency) if (currency or "").strip() else None
    with connect() as conn:
        return latest_payload(conn, code)


@mcp.tool()
def get_market_fx_rate(currency: str = "") -> dict[str, Any]:
    """查询市场参考汇率（非计征汇率，不可用于报关折算）。

    与 get_latest_fx_rate 是两个口径，不要混用：
      - get_latest_fx_rate：CFETS 人民币汇率中间价，每个交易日约 09:15 公布一次，休市不更新；
      - **本工具**：境外源的市场汇率，每日 UTC 00:00 更新，周末与节假日照常更新，
        覆盖约 160 个币种（含 CFETS 不公布的冷门币种，如 VND、INR）。

    用途：休市期间看价格走向、冷门币种取一个参考值、与中间价交叉核对。
    **不能**用于申报：计征汇率必须是「上一个月第三个星期三 CFETS 中间价」，
    市场价与之有偏离（今日 USD 约 0.3%），用来算税就是错的。

    Args:
        currency: 外币币种代码（如 VND）或 CFETS 币种的中文名；留空返回全部币种。

    Returns:
        含 rate_date、rates、cfets_reference（同币种中间价对照与偏离）的字典。
    """
    with connect() as conn:
        code = resolve_market_currency(conn, currency) if (currency or "").strip() else None
        return market_payload(conn, code)


@mcp.tool()
def get_fx_rate_history(
    currency: str, start_date: str, end_date: str, limit: int = 60
) -> dict[str, Any]:
    """查询一段区间内人民币汇率中间价的历史序列。

    用于看走势、做区间核对。报关折算请用 get_customs_fx_rate。

    Args:
        currency: 外币币种，代码或中文名均可。
        start_date: 起始日期，YYYY-MM-DD。
        end_date: 结束日期，YYYY-MM-DD。
        limit: 最多返回条数，1-500，默认 60（按日期倒序取最近的部分）。

    Returns:
        含 records（日期、原始报价、折算率）与区间统计的字典。
    """
    code = resolve_currency(currency)
    start = parse_date(start_date, "start_date")
    end = parse_date(end_date, "end_date")
    limit = parse_limit(limit)

    with connect() as conn:
        return history_payload(conn, code, start, end, limit)


@mcp.tool()
def list_fx_currencies() -> dict[str, Any]:
    """列出可查询的币种，以及中间价的数据覆盖区间。

    在查询前先看这个，避免用冷门币种去查一个不存在的中间价：
    CFETS 中间价只覆盖有限币种，且库中数据的起止日期决定了哪些申报日可算。

    Returns:
        含 currencies（代码、中文名、别名）、数据起止日期与记录数的字典。
    """
    with connect() as conn:
        return currencies_payload(conn)


# --------------------------------------------------------------------------- #
# 资源与提示词
# --------------------------------------------------------------------------- #


@mcp.resource("fx://dataset-info")
def dataset_info() -> str:
    """数据集说明：来源、覆盖区间、适用规则与口径提醒。"""
    with connect() as conn:
        return dataset_json(conn)


@mcp.prompt()
def customs_value_workflow(amount: str, currency: str, declaration_date: str) -> str:
    """报关折算工作流提示词（确认口径 → 取汇率 → 折算 → 提示边界）。"""
    return (
        f"请按中国海关口径，把 {amount} {currency} 折合人民币，"
        f"完成申报之日为 {declaration_date}。\n\n"
        "执行步骤：\n"
        "1. 用 list_fx_currencies 确认该币种有中间价、且申报日在数据覆盖区间内；\n"
        "2. 用 get_customs_fx_rate 取得该申报日适用的计征汇率，"
        "并说明它取自哪一个「上一个月第三个星期三」、是否发生了顺延；\n"
        "3. 用 convert_customs_value 折算，四舍五入至分；\n"
        "4. 在回答中明确：计征汇率不是查询当天的市场汇率，"
        "也不是中国银行挂牌的现汇价；旧口径（海关总署令第 124 号）已废止；\n"
        "5. 指出边界：本结果只解决折算，不含税率、完税价格审定与税额计算；"
        "正式申报前请与报关行或海关确认。\n\n"
        "货币对方向提醒：CFETS 对部分币种采用间接标价法（1 人民币 = ? 外币），"
        "工具返回的 cny_per_unit 已归一为「1 单位外币 = ? 人民币」，"
        "换算一律只用该字段。"
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{SERVER_NAME} MCP server")
    parser.add_argument("--http", type=int, metavar="PORT",
                        help="以 Streamable HTTP 方式运行并监听端口（默认 stdio）")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--path", default=os.environ.get("MCP_HTTP_PATH", "/mcp/fx"),
                        help="HTTP 端点路径")
    parser.add_argument("--stateless", action="store_true",
                        default=os.environ.get("MCP_STATELESS", "").lower() in {"1", "true"},
                        help="HTTP 无状态模式：请求不依赖 Mcp-Session-Id")
    args = parser.parse_args()

    if args.http:
        path = args.path if args.path.startswith("/") else "/" + args.path
        print(f"[{SERVER_NAME}] HTTP 模式：http://{args.host}:{args.http}{path}"
              f"（{'stateless' if args.stateless else 'stateful'}）", file=sys.stderr)
        mcp.run(transport="streamable-http", host=args.host, port=args.http,
                streamable_http_path=path, stateless_http=args.stateless)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
