"""中国货币网（CFETS）人民币汇率中间价抓取。

两个接口，分工不同
------------------
1. **最新值**（轻量，不限额）
       GET https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json
       返回 data.lastDate（最新发布日期）+ 25 条记录，字段 vrtEName（货币对）/ price（报价）。
       每日增量只调这一个，一次请求搞定。

2. **历史区间**（重）
       GET https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew
           ?startDate=&endDate=&currency=&pageNum=&pageSize=
       currency 传空串返回全部币种；`records[].values` 与 `data.head` 按下标对齐。
       仅在回填或补缺口时使用，按月切分。

⚠️ 最需要防的坑：records 的层级
------------------------------
两个接口的响应形状不同，历史接口尤其容易读错——`records` 是**顶层键**，
与 `data` 平级；只有币种列表 `head` 在 `data` 里面：

    {"head":    {"rep_code": "200", ...},
     "data":    {"head": ["USD/CNY", ...], "total": 21, "pageTotal": 1, ...},
     "records": [{"date": "2026-09-30", "values": ["6.7351", ...]}, ...]}

读成 `payload["data"]["records"]` 会**永远得到空列表且不报错**，表现为
「抓取成功但一条都没写入」，日志上看不出任何异常。（最新值接口 ccpr.json 的
`records` 同样在顶层，那个反而一开始就读对了。）

`_validate_history()` 在此基础上再加一道防线：声明 `total > 0` 却没给记录时
宁可报错，也不要当成「该区间无数据」而静默漏抓。注意它的取值路径必须与
`parse_history` 保持一致，否则会在每次正常响应上误报——这个误报曾把上面的
层级 bug 掩盖成「IP 限流」。

实测 pageSize 上限在 50 与 100 之间：100 被直接拒绝（返回 403 HTML），50 正常。
单月最多约 23 个交易日，50 足够一次取完。

币种方向
--------
25 个货币对混用两种标价法，方向弄反结果会差出数量级：

    D 直接标价  1 或 100 单位外币 = ? 人民币    USD/CNY、100JPY/CNY …
    I 间接标价  1 人民币 = ? 外币              CNY/MYR、CNY/KRW …

两者都归一成 cny_per_unit（1 单位外币 = ? 人民币）后入库，换算只认这一列。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .customs import month_bounds, next_month

LATEST_API = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
HISTORY_API = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
REFERER = "https://www.chinamoney.com.cn/chinese/bkccpr/"

# 市场参考汇率（境外源，与 CFETS 中间价分表存放，不参与计征）
# key 只从环境变量读取，绝不写进代码或文档。
MARKET_API = "https://v6.exchangerate-api.com/v6/{key}/latest/CNY"
MARKET_SOURCE = "MARKET"
MARKET_KEY_ENV = "FX_EXCHANGERATE_API_KEY"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 实测上限：100 直接被拒（403 HTML），50 稳定。
PAGE_SIZE = 50

# 异常响应（声明有数据却无记录）时的退避起点与上限（秒）。
# 这不是实测到的稳定行为——详见本模块开头关于 records 层级的说明。
THROTTLE_DELAY = 30.0
THROTTLE_MAX = 300.0

_MISSING = {"", "-", "--", "---", "None", "null", "N/A"}
_PAIR_RE = re.compile(r"^(?P<unit>\d{1,3})?(?P<base>[A-Z]{3})/(?P<quote>[A-Z]{3})$")

Logger = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class ThrottledError(RuntimeError):
    """响应声明有数据，却没给任何记录。

    宁可报错也不当成「该区间无数据」——后者会静默漏抓且入库成功。

    说明：这**不是**一个「已知会发生」的场景。本项目曾把 `records` 的层级读错
    （见 parse_history），又让校验函数复用了同一个错误路径，于是每次正常响应都
    被误判成限流，把解析 bug 掩盖成了「接口反爬」。保留本异常只是为了不把真正
    的异常响应错当空数据，不应据此推断接口有稳定的限流行为。
    """


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def parse_pair(pair: str) -> tuple[str, str, int]:
    """货币对 -> (币种代码, 标价法, 报价单位)。

    >>> parse_pair("100JPY/CNY")
    ('JPY', 'D', 100)
    >>> parse_pair("CNY/MYR")
    ('MYR', 'I', 1)
    """
    m = _PAIR_RE.match((pair or "").strip().upper())
    if not m:
        raise ValueError(f"无法识别的货币对：{pair!r}")
    base, quote = m["base"], m["quote"]
    if quote == "CNY" and base != "CNY":
        return base, "D", int(m["unit"] or 1)
    if base == "CNY" and quote != "CNY":
        return quote, "I", 1
    raise ValueError(f"货币对不以人民币为其中一方：{pair!r}")


def normalize_value(pair: str, raw: Any) -> tuple[str, str, int, Decimal, Decimal] | None:
    """单个报价 -> (币种, 标价法, 单位, 原始报价, 折算率)；无效值返回 None。"""
    text = str(raw).strip() if raw is not None else ""
    if text in _MISSING:
        return None
    try:
        quoted = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if quoted <= 0:
        return None

    currency, style, unit = parse_pair(pair)
    cny_per_unit = quoted / unit if style == "D" else Decimal(1) / quoted
    return currency, style, unit, quoted, cny_per_unit


def _row(
    rate_date: date, pair: str, raw: Any
) -> dict[str, Any] | None:
    parsed = normalize_value(pair, raw)
    if parsed is None:
        return None
    currency, style, unit, quoted, cny_per_unit = parsed
    return {
        "rate_date": rate_date,
        "currency": currency,
        "pair": pair,
        "quote_style": style,
        "unit": unit,
        "quoted_rate": quoted,
        "cny_per_unit": cny_per_unit,
    }


def parse_latest(payload: dict[str, Any]) -> tuple[date, list[dict[str, Any]]]:
    """解析最新值接口 -> (最新发布日期, 记录列表)。"""
    data = payload.get("data") or {}
    stamp = str(data.get("lastDate") or "").strip()
    if not stamp:
        raise RuntimeError("最新值接口未返回 lastDate，无法确定数据日期。")
    try:
        rate_date = date.fromisoformat(stamp.split()[0])
    except ValueError:
        raise RuntimeError(f"最新值接口的 lastDate 无法解析：{stamp!r}") from None

    records = payload.get("records") or []
    if not records:
        raise ThrottledError("最新值接口返回 0 条记录。")

    rows: list[dict[str, Any]] = []
    for rec in records:
        row = _row(rate_date, rec.get("vrtEName") or "", rec.get("price"))
        if row:
            rows.append(row)
    if not rows:
        raise RuntimeError("最新值接口返回了记录，但没有一条能解析成有效的货币对。")
    return rate_date, rows


def parse_history(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """解析历史区间接口 -> 记录列表。

    ⚠️ 这个接口的 JSON 结构很容易读错：`records` 是**顶层键**，与 `data` 平级，
    只有币种列表 `head` 在 `data` 里面：

        {"head": {...},
         "data":    {"head": ["USD/CNY", ...], "total": 21, "pageTotal": 1, ...},
         "records": [{"date": "2026-09-30", "values": ["6.7351", ...]}, ...]}

    读成 `payload["data"]["records"]` 会永远拿到空列表，而且不报错——
    表现为「抓取成功但一条都没写入」。
    """
    data = payload.get("data") or {}
    head = data.get("head") or []
    rows: list[dict[str, Any]] = []
    for record in payload.get("records") or []:
        try:
            rate_date = date.fromisoformat(str(record.get("date", "")).strip())
        except ValueError:
            continue
        for pair, raw in zip(head, record.get("values") or []):
            row = _row(rate_date, pair, raw)
            if row:
                rows.append(row)
    return rows


def page_total(payload: dict[str, Any]) -> int:
    data = payload.get("data") or {}
    try:
        return max(1, int(data.get("pageTotal") or 1))
    except (TypeError, ValueError):
        return 1


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #


def _request(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    # 参数越界（如 pageSize=100）时返回的是 403 HTML 页，这里会抛 JSONDecodeError。
    return json.loads(body)


def _validate_history(payload: dict[str, Any]) -> None:
    """防漏抓：声明有数据却没给记录时，宁可报错也不要当成「无数据」。

    注意这里读的是**顶层** `records`（不是 `data.records`）。读错路径会让本函数
    在每次正常响应上都误报，把好数据挡在门外——正是这个误报曾让我把「解析 bug」
    错判成「IP 限流」。所以路径与 parse_history 必须保持一致。
    """
    head = payload.get("head") or {}
    code = str(head.get("rep_code") or "").strip()
    if code and code != "200":
        raise RuntimeError(f"接口返回 rep_code={code}：{head.get('rep_message')}")
    data = payload.get("data") or {}
    try:
        total = int(data.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    if total > 0 and not (payload.get("records") or []):
        raise ThrottledError(
            f"接口声明 total={total} 但未返回任何 records，判定为异常响应（疑似限流），"
            "已按「不要当成无数据」处理。"
        )


def _get_with_retry(
    url: str, *, delay: float, retries: int, timeout: float,
    throttle_delay: float, logger: Logger, validate: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            if isinstance(last, ThrottledError):
                wait = min(throttle_delay * (2 ** (attempt - 1)), THROTTLE_MAX)
            else:
                wait = min(delay * (2 ** attempt), THROTTLE_MAX)
            logger(f"    第 {attempt}/{retries} 次重试，等待 {wait:.1f}s：{last}")
            time.sleep(wait)
        try:
            payload = _request(url, timeout)
            if validate:
                validate(payload)
            return payload
        except ThrottledError as exc:
            last = exc
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
    raise RuntimeError(f"请求失败，已重试 {retries} 次：{url} | 最后错误：{last}") from last


def _history_url(start: date, end: date, page: int, size: int) -> str:
    return f"{HISTORY_API}?" + urlencode({
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "currency": "",
        "pageNum": page,
        "pageSize": size,
    })


def fetch_latest(
    *,
    delay: float = 1.5,
    retries: int = 3,
    timeout: float = 30.0,
    throttle_delay: float = THROTTLE_DELAY,
    logger: Logger | None = None,
) -> tuple[date, list[dict[str, Any]]]:
    """取最新一个交易日公布的全部中间价。日常增量只需这一次请求。"""
    log = logger or _noop
    payload = _get_with_retry(
        LATEST_API, delay=delay, retries=retries, timeout=timeout,
        throttle_delay=throttle_delay, logger=log, validate=None,
    )
    rate_date, rows = parse_latest(payload)
    log(f"  最新公布日期 {rate_date}，{len(rows)} 个币种")
    return rate_date, rows


def fetch_range(
    start: date,
    end: date,
    *,
    delay: float = 1.5,
    retries: int = 3,
    timeout: float = 30.0,
    throttle_delay: float = THROTTLE_DELAY,
    logger: Logger | None = None,
    on_month: Callable[[date, list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    """抓取 [start, end] 的全部中间价，按月切分请求。仅用于回填或补缺口。

    on_month(month_end, rows) 在每个月抓取成功后立即回调。回填动辄上百次请求，
    中途失败会丢掉尚未落库的部分；调用方应在这里落库并推进进度，
    而不是攒到最后一次性写。
    """
    log = logger or _noop
    if start > end:
        return []

    rows: list[dict[str, Any]] = []
    cursor = date(start.year, start.month, 1)
    first_request = True

    while cursor <= end:
        m_start, m_end = month_bounds(cursor.year, cursor.month)
        m_start = max(m_start, start)
        m_end = min(m_end, end)

        if not first_request:
            time.sleep(delay)
        first_request = False

        payload = _get_with_retry(
            _history_url(m_start, m_end, 1, PAGE_SIZE),
            delay=delay, retries=retries, timeout=timeout,
            throttle_delay=throttle_delay, logger=log, validate=_validate_history,
        )
        month_rows = parse_history(payload)
        for page in range(2, page_total(payload) + 1):
            time.sleep(delay)
            payload = _get_with_retry(
                _history_url(m_start, m_end, page, PAGE_SIZE),
                delay=delay, retries=retries, timeout=timeout,
                throttle_delay=throttle_delay, logger=log, validate=_validate_history,
            )
            month_rows.extend(parse_history(payload))

        days = len({r["rate_date"] for r in month_rows})
        log(f"  {cursor.year}-{cursor.month:02d}：{days} 个交易日 / {len(month_rows)} 条")
        rows.extend(month_rows)
        if on_month is not None:
            on_month(m_end, month_rows)

        year, month = next_month(cursor.year, cursor.month)
        cursor = date(year, month, 1)

    return rows


def upsert_market_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    """写入市场参考汇率，按 (rate_date, currency) 幂等覆盖。

    与中间价**分表**：市场价一旦进了 fx_central_parity，就会被「上一个月第三个
    星期三」的规则当成计征汇率取用，那是错的。
    """
    if not rows:
        return 0
    sql = """
        INSERT INTO fx_market_rate
            (rate_date, currency, pair, quote_style, unit, quoted_rate, cny_per_unit, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'MARKET')
        ON CONFLICT (rate_date, currency) DO UPDATE SET
            pair         = EXCLUDED.pair,
            quote_style  = EXCLUDED.quote_style,
            unit         = EXCLUDED.unit,
            quoted_rate  = EXCLUDED.quoted_rate,
            cny_per_unit = EXCLUDED.cny_per_unit,
            source       = EXCLUDED.source,
            fetched_at   = now()
    """
    params = [
        (
            r["rate_date"], r["currency"], r["pair"], r["quote_style"],
            r["unit"], r["quoted_rate"], r["cny_per_unit"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
    conn.commit()
    return len(params)


# --------------------------------------------------------------------------- #
# 市场参考汇率（exchangerate-api）
# --------------------------------------------------------------------------- #


def market_api_key() -> str | None:
    """市场汇率接口的 key。只从环境变量读，缺失时返回 None 由调用方决定是否跳过。"""
    return os.environ.get(MARKET_KEY_ENV, "").strip() or None


def parse_market(payload: dict[str, Any]) -> tuple[date, list[dict[str, Any]]]:
    """解析市场汇率接口 -> (汇率日期, 记录列表)。

    该接口以 CNY 为基准报价（1 人民币 = ? 外币），取倒数得到 cny_per_unit，
    与中间价表保持一致：换算只认 cny_per_unit 一列。
    """
    if payload.get("result") != "success":
        raise RuntimeError(
            "市场汇率接口返回失败："
            f"{payload.get('error_type') or payload.get('result')}"
        )
    rates = payload.get("conversion_rates")
    if not rates:
        raise RuntimeError("市场汇率接口未返回 conversion_rates。")
    stamp = payload.get("time_last_update_unix")
    if not stamp:
        raise RuntimeError("市场汇率接口未返回 time_last_update_unix，无法确定数据日期。")
    # 源站每天 UTC 00:00 更新；按 UTC+8 归到当天（中国无夏令时，固定偏移即可）。
    rate_date = (
        datetime.fromtimestamp(int(stamp), tz=timezone.utc) + timedelta(hours=8)
    ).date()

    rows: list[dict[str, Any]] = []
    for currency, raw in rates.items():
        if not currency or currency == "CNY":
            continue
        try:
            per_cny = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if per_cny <= 0:
            continue
        rows.append({
            "rate_date": rate_date,
            "currency": str(currency),
            "pair": f"CNY/{currency}",
            "quote_style": "I",
            "unit": 1,
            "quoted_rate": per_cny,
            "cny_per_unit": (Decimal(1) / per_cny).quantize(Decimal("0.000000000001")),
        })
    if not rows:
        raise RuntimeError("市场汇率接口返回了 conversion_rates，但没有一条能解析成有效汇率。")
    return rate_date, rows


def fetch_market_latest(
    *, timeout: float = 30.0, logger: Logger | None = None
) -> tuple[date, list[dict[str, Any]]]:
    """取最新一份市场参考汇率。境外源，失败不影响中间价主链路。"""
    log = logger or _noop
    key = market_api_key()
    if not key:
        raise RuntimeError(f"未配置 {MARKET_KEY_ENV}，跳过市场参考汇率抓取。")
    payload = _request(MARKET_API.format(key=key), timeout)
    rate_date, rows = parse_market(payload)
    log(f"  市场参考汇率 {rate_date}，{len(rows)} 个币种")
    return rate_date, rows


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    """写入中间价，按 (rate_date, currency) 幂等覆盖。"""
    if not rows:
        return 0
    sql = """
        INSERT INTO fx_central_parity
            (rate_date, currency, pair, quote_style, unit, quoted_rate, cny_per_unit, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'CFETS')
        ON CONFLICT (rate_date, currency) DO UPDATE SET
            pair         = EXCLUDED.pair,
            quote_style  = EXCLUDED.quote_style,
            unit         = EXCLUDED.unit,
            quoted_rate  = EXCLUDED.quoted_rate,
            cny_per_unit = EXCLUDED.cny_per_unit,
            source       = EXCLUDED.source,
            fetched_at   = now()
    """
    params = [
        (
            r["rate_date"], r["currency"], r["pair"], r["quote_style"],
            r["unit"], r["quoted_rate"], r["cny_per_unit"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
    conn.commit()
    return len(params)
