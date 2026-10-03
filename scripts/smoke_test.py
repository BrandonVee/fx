"""通过 MCP HTTP 调用各工具及资源，验证部署后的真实服务。

先跑通抓取（`uv run fx-fetch`），再执行本脚本：

    uv run python scripts/smoke_test.py
    uv run python scripts/smoke_test.py --url http://127.0.0.1:8766/mcp/fx
"""
import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "get_customs_fx_rate",
    "convert_customs_value",
    "get_fx_rate",
    "get_latest_fx_rate",
    "get_market_fx_rate",
    "get_fx_rate_history",
    "list_fx_currencies",
}


async def verify(url: str, declaration_date: str) -> None:
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == EXPECTED_TOOLS, names

            cases = [
                ("list_fx_currencies", {}),
                ("get_customs_fx_rate", {"currency": "美元", "declaration_date": declaration_date}),
                ("convert_customs_value",
                 {"amount": 12345.67, "currency": "EUR", "declaration_date": declaration_date}),
                ("get_fx_rate", {"currency": "JPY", "trade_date": declaration_date}),
                ("get_latest_fx_rate", {"currency": "USD"}),
                ("get_latest_fx_rate", {}),
                ("get_market_fx_rate", {"currency": "USD"}),
                ("get_fx_rate_history",
                 {"currency": "USD", "start_date": "2026-01-01", "end_date": declaration_date,
                  "limit": 10}),
            ]
            for name, arguments in cases:
                result = await session.call_tool(name, arguments)
                if result.is_error and name == "get_market_fx_rate":
                    print("跳过：get_market_fx_rate（库里暂无市场参考汇率，"
                          "需配置 FX_EXCHANGERATE_API_KEY 后抓取）")
                    continue
                assert not result.is_error, result
                payload = result.structured_content
                if payload is None:
                    payload = json.loads(
                        next(b.text for b in result.content if b.type == "text")
                    )
                print(f"通过：{name} {arguments}")
                if name == "get_customs_fx_rate":
                    print(f"  → {payload['currency']} 适用 {payload['rate_date']}"
                          f"（基准日 {payload['reference_date']}，顺延={payload['deferred']}）"
                          f" 1 单位 = {payload['cny_per_unit']} CNY")
                if name == "convert_customs_value":
                    print(f"  → {payload['calculation']}")
                if name == "get_latest_fx_rate":
                    print(f"  → 最新中间价 {payload['rate_date']}"
                          f"（距今 {payload['stale_days']} 天），{payload['count']} 个币种")

            resource = await session.read_resource("fx://dataset-info")
            assert resource.contents
            print("MCP HTTP 工具及资源验证通过")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766/mcp/fx")
    parser.add_argument("--declaration-date", default="2026-10-03",
                        help="用于验证的完成申报之日")
    options = parser.parse_args()
    asyncio.run(verify(options.url, options.declaration_date))
