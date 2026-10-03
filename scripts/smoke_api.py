"""验证 REST API 各端点（含错误路径）。

先跑通抓取（`uv run fx-fetch`），再执行本脚本：

    uv run python scripts/smoke_api.py
    uv run python scripts/smoke_api.py --base http://127.0.0.1:8767
"""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request


def get(base: str, path: str, **params: object) -> tuple[int, dict]:
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def expect_ok(base: str, path: str, **params: object) -> dict:
    status, payload = get(base, path, **params)
    assert status == 200, (path, status, payload)
    print(f"通过：GET {path} {params or ''}")
    return payload


def expect_error(base: str, path: str, code: str, **params: object) -> None:
    status, payload = get(base, path, **params)
    assert payload.get("error", {}).get("code") == code, (path, status, payload)
    print(f"通过：GET {path} {params or ''} → {status} {code}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8767")
    parser.add_argument("--declaration-date", default="2026-10-03")
    options = parser.parse_args()
    base = options.base.rstrip("/")
    when = options.declaration_date

    health = expect_ok(base, "/api/health")
    assert health["status"] == "ok", health
    print(f"  → 最新中间价 {health['last_rate_date']}（滞后 {health['stale_days']} 天），"
          f"{health['trading_days']} 个交易日；市场价 {health['market_last_date']}"
          f"（滞后 {health['market_stale_days']} 天）")
    for warning in health.get("warnings", []):
        print(f"  ⚠ {warning}")

    expect_ok(base, "/api/currencies")

    rate = expect_ok(base, "/api/customs-rate", currency="美元", declaration_date=when)
    print(f"  → {rate['currency']} 适用 {rate['rate_date']}"
          f"（基准日 {rate['reference_date']}，顺延={rate['deferred']}）"
          f" 1 单位 = {rate['cny_per_unit']} CNY")

    converted = expect_ok(base, "/api/convert", amount="12345.67", currency="EUR",
                          declaration_date=when)
    print(f"  → {converted['calculation']}")

    expect_ok(base, "/api/rate", currency="JPY", trade_date=when)

    latest = expect_ok(base, "/api/latest", currency="美元")
    print(f"  → 最新中间价 {latest['rate_date']}（距今 {latest['stale_days']} 天）"
          f" 1 USD = {latest['cny_per_unit']} CNY")
    all_latest = expect_ok(base, "/api/latest")
    assert all_latest["count"] > 1, all_latest
    print(f"  → 全量 {all_latest['count']} 个币种，公布日 {all_latest['rate_date']}")


    expect_ok(base, "/api/history", currency="USD", start_date="2026-01-01",
              end_date=when, limit=10)
    expect_ok(base, "/api/dataset-info")

    # 市场参考汇率：未配置 FX_EXCHANGERATE_API_KEY 时库里没有数据，跳过而不是失败
    status, market = get(base, "/api/market-rate", currency="USD")
    if status == 404 and market.get("error", {}).get("code") == "data_unavailable":
        print("跳过：/api/market-rate（未配置 FX_EXCHANGERATE_API_KEY，库里暂无市场价）")
    else:
        assert status == 200, (status, market)
        print(f"通过：GET /api/market-rate USD → {market['rate_date']}"
              f" 1 USD = {market['cny_per_unit']} CNY")
        if market.get("cfets_reference"):
            print(f"  → 中间价 {market['cfets_reference']['rate_date']} "
                  f"{market['cfets_reference']['cny_per_unit']}，"
                  f"偏离 {market['cfets_reference']['market_vs_central_pct']}%")
        exotic = expect_ok(base, "/api/market-rate", currency="VND")
        print(f"  → 冷门币种 VND 参考价 1 VND = {exotic['cny_per_unit']} CNY")



    # 错误路径：币种无法识别 → 400；日期格式不对 → 400；库里还没抓到的日期 → 404
    expect_error(base, "/api/customs-rate", "invalid_input",
                 currency="火星币", declaration_date=when)
    expect_error(base, "/api/customs-rate", "invalid_input",
                 currency="USD", declaration_date="2026/10/03")
    expect_error(base, "/api/customs-rate", "data_unavailable",
                 currency="USD", declaration_date="2030-01-01")

    print("REST API 端点验证通过")


if __name__ == "__main__":
    main()
