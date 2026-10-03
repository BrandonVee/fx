"""REST API：与 MCP 工具同源的海关计征汇率查询接口。

MCP 面向模型调用，REST 面向脚本与内部系统集成——两者共用 `service.py`，
口径、入参校验、返回结构完全一致，不各写一份 SQL。
新增能力时改 service.py 即可同时落到两个接口上。

启动
----
    uv run fx-api --port 8767
    docker compose up -d api      # 容器内监听 8767，宿主机端口见 FX_API_PORT

示例
----
    curl 'http://127.0.0.1:8767/api/customs-rate?currency=USD&declaration_date=2026-10-03'
    curl 'http://127.0.0.1:8767/api/convert?amount=12345.67&currency=EUR&declaration_date=2026-10-03'

错误约定
--------
    400 invalid_input        入参问题（币种无法识别、日期格式不对、金额非法）
    404 data_unavailable     数据缺失（该日/该币种没有中间价，或库里还没抓到）
    503 db_unavailable       连不上库
均返回 {"error": {"code": ..., "message": ...}}，message 可直接展示给用户。
"""

from __future__ import annotations

import argparse
import os
from datetime import date

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

from .config import DEFAULT_CFETS_STALE_WARN_DAYS, DEFAULT_MARKET_STALE_FAIL_DAYS
from .customs import FxDataError
from .db import connect
from .service import (
    convert_payload,
    currencies_payload,
    dataset_payload,
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

API_VERSION = "1.0.0"

# 分组：导出 OpenAPI 导入 Apifox / Postman 后按此分组展示。
TAGS_METADATA = [
    {"name": "计征汇率",
     "description": "报关折算专用。依据海关总署令第 272 号：上一个月第三个星期三 CFETS 中间价。"},
    {"name": "中间价",
     "description": "CFETS 人民币汇率中间价查询。非报关场景；报关请用「计征汇率」分组。"},
    {"name": "市场参考汇率",
     "description": "市场汇率，**非计征汇率**，不可用于报关折算。休市期与冷门币种的参考价。"},
    {"name": "元数据与健康", "description": "币种清单、数据集说明、健康检査。"},
]

# 错误契约：所有数据接口一致，写进 OpenAPI 后导入 Apifox 能看到示例。
_ERROR_EXAMPLE = {
    "application/json": {"example": {"error": {"code": "", "message": ""}}}
}
ERROR_RESPONSES = {
    400: {"description": "入参问题：币种无法识别或有歧义、日期格式不对、金额非法",
          "content": {"application/json": {"example":
              {"error": {"code": "invalid_input", "message": "无法识别币种「火星币」……"}}}}},
    404: {"description": "数据缺失：该日期或币种没有中间价，库中尚未抓到",
          "content": {"application/json": {"example":
              {"error": {"code": "data_unavailable", "message": "库中没有 2029-12-19 及之后……"}}}}},
    503: {"description": "数据库不可用，或抓取链路已停（见 /api/health）",
          "content": {"application/json": {"example":
              {"error": {"code": "db_unavailable", "message": "could not connect……"}}}}},
}

app = FastAPI(
    title="海关计征汇率 API",
    version=API_VERSION,
    description=(
        "中国海关计征汇率（海关总署令第 272 号）与 CFETS 人民币汇率中间价查询。"
        "报关折算请用「计征汇率」分组的接口：计征汇率是「上一个月第三个星期三」"
        "的中间价，不是查询当天的市场汇率，也不是中国银行挂牌价。"
    ),
    openapi_tags=TAGS_METADATA,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #


@app.exception_handler(ValueError)
async def _invalid_input(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "invalid_input", "message": str(exc)}},
    )


@app.exception_handler(FxDataError)
async def _data_unavailable(request: Request, exc: FxDataError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "data_unavailable", "message": str(exc)}},
    )


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": str(exc)}},
    )


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/api/docs")


@app.get("/api", include_in_schema=False)
def api_index() -> dict:
    """端点清单，方便手工排查时先看这里。"""
    return {
        "service": "fx-api",
        "version": API_VERSION,
        "endpoints": {
            "/api/health": "服务与数据可用性",
            "/api/customs-rate": "申报日适用的计征汇率（报关用）",
            "/api/convert": "按计征汇率折人民币，四舍五入至分（报关用）",
            "/api/rate": "指定交易日的中间价（非报关场景）",
            "/api/latest": "最近一个交易日公布的中间价（当前价，非实时行情）",
            "/api/market-rate": "市场参考汇率（非计征汇率，不可报关，覆盖约 160 币种）",
            "/api/history": "中间价历史序列",
            "/api/currencies": "可用币种与数据覆盖区间",
            "/api/dataset-info": "数据集说明",
        },
        "docs": "/api/docs",
    }


@app.get("/api/health", tags=["元数据与健康"], summary="健康检査：连库状态与数据滞后",
          responses=ERROR_RESPONSES)
def health() -> Any:
    """健康检查：连得上库、且抓取链路还在跑，才算 ok。

    中间价滞后**不直接判故障**：周末与法定节假日休市本来就不公布新价。
    所以中间价只给 warning（HTTP 200）；真正判故障看市场参考汇率——
    那是境外源、每日更新、节假日照常，它滞后说明 fx-fetch 没在跑（HTTP 503）。
    """
    cfets_warn = int(os.environ.get("FX_STALE_WARN_DAYS", DEFAULT_CFETS_STALE_WARN_DAYS))
    market_fail = int(
        os.environ.get("FX_MARKET_STALE_DAYS", DEFAULT_MARKET_STALE_FAIL_DAYS)
    )
    try:
        with connect() as conn:
            parity = conn.execute(
                "SELECT max(rate_date) AS last_date, count(DISTINCT rate_date) AS days "
                "FROM fx_central_parity"
            ).fetchone()
            market = conn.execute(
                "SELECT max(rate_date) AS last_date FROM fx_market_rate"
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - 健康接口要把任何故障都说清楚
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": {"code": "db_unavailable", "message": str(exc)},
            },
        )

    today = date.today()
    last = parity["last_date"]
    market_last = market["last_date"]
    stale = (today - last).days if last else None
    market_stale = (today - market_last).days if market_last else None

    status = "ok"
    warnings: list[str] = []
    if last is None:
        status = "degraded"
        warnings.append("库中还没有中间价数据，请先运行 fx-fetch 抓取。")
    elif stale > cfets_warn:
        warnings.append(
            f"CFETS 中间价最新为 {last.isoformat()}（滞后 {stale} 天）。"
            "周末与法定节假日休市属于正常；休市结束后仍未更新，请检查 fx-fetch。"
        )

    if market_last is None:
        warnings.append(
            "未配置市场参考汇率（FX_EXCHANGERATE_API_KEY），"
            "无法用它交叉校验抓取链路是否仍在运行。"
        )
    elif market_stale > market_fail:
        status = "degraded"
        warnings.append(
            f"市场参考汇率最新为 {market_last.isoformat()}（滞后 {market_stale} 天）。"
            "该源每日更新且节假日照常，滞后说明 fx-fetch 可能已停止运行。"
        )

    payload: dict[str, Any] = {
        "status": status,
        "service": "fx-api",
        "version": API_VERSION,
        "last_rate_date": last.isoformat() if last else None,
        "stale_days": stale,
        "trading_days": parity["days"],
        "market_last_date": market_last.isoformat() if market_last else None,
        "market_stale_days": market_stale,
        "thresholds": {
            "cfets_stale_warn_days": cfets_warn,
            "market_stale_fail_days": market_fail,
        },
        "checked_at": today.isoformat(),
    }
    if warnings:
        payload["warnings"] = warnings
    if status == "degraded":
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/api/customs-rate", tags=["计征汇率"], summary="申报日适用的海关计征汇率",
          responses=ERROR_RESPONSES)
def customs_rate(
    currency: str = Query(..., description="外币币种，代码或中文名，如 USD / 美元"),
    declaration_date: str = Query(..., description="完成申报之日，YYYY-MM-DD"),
) -> dict:
    """某次申报适用的海关计征汇率（含日期推导与依据）。

    按「上一个月第三个星期三」取值，非交易日的顺延下一个交易日。
    """
    code = resolve_currency(currency)
    when = parse_date(declaration_date, "declaration_date")
    with connect() as conn:
        return rate_payload(conn, code, when)


@app.get("/api/convert", tags=["计征汇率"], summary="按计征汇率折人民币（四舍五入至分）",
          responses=ERROR_RESPONSES)
def convert(
    amount: str = Query(..., description="外币金额，字符串形式以避免浮点误差"),
    currency: str = Query(..., description="外币币种，代码或中文名"),
    declaration_date: str = Query(..., description="完成申报之日，YYYY-MM-DD"),
) -> dict:
    """按计征汇率把外币金额折合人民币，四舍五入至分。"""
    code = resolve_currency(currency)
    when = parse_date(declaration_date, "declaration_date")
    value = parse_amount(amount)
    with connect() as conn:
        return convert_payload(conn, value, code, when)


@app.get("/api/rate", tags=["中间价"], summary="指定交易日的人民币汇率中间价",
          responses=ERROR_RESPONSES)
def fx_rate(
    currency: str = Query(..., description="外币币种，代码或中文名"),
    trade_date: str = Query(..., description="交易日期，YYYY-MM-DD"),
) -> dict:
    """指定交易日的人民币汇率中间价（非报关场景）。

    该日没有中间价时不猜值，返回 available=false 与前后最近的可查日期。
    """
    code = resolve_currency(currency)
    target = parse_date(trade_date, "trade_date")
    with connect() as conn:
        return spot_payload(conn, code, target)


@app.get("/api/latest", tags=["中间价"], summary="最近一个交易日公布的中间价（当前价）",
          responses=ERROR_RESPONSES)
def latest(
    currency: str = Query(None, description="外币币种，代码或中文名；留空返回全部币种"),
) -> dict:
    """最近一个交易日公布的中间价（「当前价」）。

    中间价每个交易日约 09:15 公布一次，休市日不更新——所以返回里带 rate_date
    与 stale_days（距今天数）。这是中间价而非实时行情，也不能用于报关折算。
    """
    code = resolve_currency(currency) if (currency or "").strip() else None
    with connect() as conn:
        return latest_payload(conn, code)


@app.get("/api/market-rate", tags=["市场参考汇率"], summary="市场参考汇率（非计征汇率，不可报关）",
          responses=ERROR_RESPONSES)
def market_rate(
    currency: str = Query(None, description="外币币种代码（如 VND）或中文名；留空返回全部"),
) -> dict:
    """市场参考汇率（**非计征汇率，不可用于报关折算**）。

    境外源，每日 UTC 00:00 更新，周末与节假日照常更新，覆盖约 160 个币种——
    包括 CFETS 不公布的冷门币种。与 /api/latest（CFETS 中间价）是两个口径，
    返回里的 cfets_reference 给出同币种中间价与偏离，便于交叉核对。
    """
    with connect() as conn:
        code = resolve_market_currency(conn, currency) if (currency or "").strip() else None
        return market_payload(conn, code)


@app.get("/api/history", tags=["中间价"], summary="中间价历史序列",
          responses=ERROR_RESPONSES)
def history(
    currency: str = Query(..., description="外币币种，代码或中文名"),
    start_date: str = Query(..., description="起始日期，YYYY-MM-DD"),
    end_date: str = Query(..., description="结束日期，YYYY-MM-DD"),
    limit: int = Query(60, description="最多返回条数，1-500，按日期倒序"),
) -> dict:
    """区间内的中间价历史序列（按日期倒序），附区间统计。"""
    code = resolve_currency(currency)
    start = parse_date(start_date, "start_date")
    end = parse_date(end_date, "end_date")
    capped = parse_limit(limit)
    with connect() as conn:
        return history_payload(conn, code, start, end, capped)


@app.get("/api/currencies", tags=["元数据与健康"], summary="可用币种与数据覆盖区间",
          responses=ERROR_RESPONSES)
def currencies() -> dict:
    """可用币种与数据覆盖区间。"""
    with connect() as conn:
        return currencies_payload(conn)


@app.get("/api/dataset-info", tags=["元数据与健康"], summary="数据集说明：来源、覆盖区间、规则与边界",
          responses=ERROR_RESPONSES)
def dataset_info() -> dict:
    """数据集说明：来源、覆盖区间、适用规则与口径提醒。"""
    with connect() as conn:
        return dataset_payload(conn)


def main() -> None:
    parser = argparse.ArgumentParser(description="海关计征汇率 REST API")
    parser.add_argument("--host", default=os.environ.get("FX_API_HOST", "127.0.0.1"),
                        help="监听地址")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("FX_API_PORT", "8767")),
                        help="监听端口")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
