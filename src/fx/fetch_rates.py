"""fx-fetch：抓取中国货币网（CFETS）人民币汇率中间价并入库。

策略
----
分两步，尽量只用轻量接口：

1. 先用**最新值接口**取最新公布日的全部币种——单次请求即可完成日常增量。
2. 只有当库中仍有历史缺口时，才用**历史区间接口**按月补齐，
   并且**每个月抓完立即落库、立即推进进度**。
3. 最后抓一份**市场参考汇率**（境外源，可选，需 FX_EXCHANGERATE_API_KEY），
   **分表存放**，只作参考、不参与计征；失败不影响前两步。

进度为什么必须落库
------------------
历史接口一次回填动辄上百次请求，中途失败会丢掉未落库的部分。如果按「库中最新日期 + 1」
推断起点，一旦回填中途失败而较新的日期已写入，`max(rate_date) + 1` 会直接跳过中间缺口，
**缺口永久补不上**。所以进度存在 fx_fetch_state 表的 `backfill_floor` 键里：
只以它为准，逐月推进，中断后重跑自动续传。

同理，长回填不能攒到最后一次性写库——中途失败会丢掉全部进度。见 sources.fetch_range
的 on_month 回调。

用法
----
    fx-fetch                       # 增量：最新值 + 续跑未完成的历史回填
    fx-fetch --full                # 从默认回填起点（2015-01-01）重抓，幂等覆盖
    fx-fetch --start 2025-01-01    # 指定起点（同时重置回填进度）
    fx-fetch --watch --at 09:40    # 常驻，每天 09:40（本地时区）抓一次
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

from .config import DEFAULT_BACKFILL_START, DEFAULT_FETCH_AT, DEFAULT_FETCH_DELAY
from .db import connect, ensure_schema
from .sources import (
    fetch_latest,
    fetch_market_latest,
    fetch_range,
    upsert_market_rows,
    upsert_rows,
)

PROG = "fx-fetch"
BACKFILL_KEY = "backfill_floor"


def log(message: str) -> None:
    print(f"[{PROG}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# 库操作
# --------------------------------------------------------------------------- #


def latest_stored(conn: Any) -> date | None:
    row = conn.execute("SELECT max(rate_date) AS d FROM fx_central_parity").fetchone()
    return row["d"] if row else None


def get_state(conn: Any, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM fx_fetch_state WHERE key = %s", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_state(conn: Any, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO fx_fetch_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, value),
    )
    conn.commit()


def read_floor(conn: Any) -> date:
    """仍需抓取的最早日期。"""
    raw = get_state(conn, BACKFILL_KEY)
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            log(f"回填进度值无法解析（{raw!r}），回退到 {DEFAULT_BACKFILL_START}")
    return date.fromisoformat(DEFAULT_BACKFILL_START)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def run_once(args: argparse.Namespace) -> int:
    with connect(args.dsn) as conn:
        ensure_schema(conn)
        stored = latest_stored(conn)
        written = 0

        # ---- 1) 最新值：轻量接口，单次请求 --------------------------------------
        published: date | None = None
        latest_rows: list[dict[str, Any]] = []
        try:
            published, latest_rows = fetch_latest(
                delay=args.delay, retries=args.retries, logger=log
            )
        except Exception as exc:  # noqa: BLE001 - 轻量接口失败不致命，可退回历史接口
            log(f"最新值接口未取到数据：{exc}")

        ceiling = published or (
            date.fromisoformat(args.end) if args.end else date.today()
        )
        if latest_rows and (stored is None or published > stored):
            written += upsert_rows(conn, latest_rows)

        # ---- 2) 历史缺口：以落盘进度为准，逐月落库 ------------------------------
        floor = date.fromisoformat(args.start) if args.start else read_floor(conn)
        heavy_end = published - timedelta(days=1) if published else ceiling

        if floor <= heavy_end:
            log(f"补历史缺口 {floor} ~ {heavy_end}（历史接口，按月切分，逐月落库）")

            def on_month(month_end: date, month_rows: list[dict[str, Any]]) -> None:
                nonlocal written
                written += upsert_rows(conn, month_rows)
                set_state(conn, BACKFILL_KEY, (month_end + timedelta(days=1)).isoformat())

            fetch_range(
                floor, heavy_end, delay=args.delay, retries=args.retries,
                logger=log, on_month=on_month,
            )
        elif not args.start:
            # 回填已追平公布日期，把进度标记同步到最新，下次不再重复判断。
            set_state(conn, BACKFILL_KEY, (heavy_end + timedelta(days=1)).isoformat())

        # ---- 3) 市场参考汇率：独立数据源，分表存放，不参与计征 ------------------
        # 失败不影响主链路（境外源、可能需要 key），只记日志。
        if not args.no_market:
            try:
                market_date, market_rows = fetch_market_latest(logger=log)
                written_market = upsert_market_rows(conn, market_rows)
                log(f"  市场参考汇率 {market_date} 写入 {written_market} 条（仅参考，不参与计征）")
            except Exception as exc:  # noqa: BLE001 - 参考价缺失不阻断计征数据抓取
                log(f"市场参考汇率未更新：{exc}")
        else:
            log("已跳过市场参考汇率（--no-market）")

        if written == 0:
            log(f"已是最新：库中最后日期 {stored}，最新公布日期 {published}")
        else:
            days = conn.execute(
                "SELECT count(DISTINCT rate_date) AS n FROM fx_central_parity"
            ).fetchone()["n"]
            log(f"完成：本次写入 {written} 条，库中共 {days} 个交易日")
        return written


def seconds_until(hhmm: str) -> float:
    """距离下一个 HH:MM 的秒数（本地时区）。"""
    hour, minute = (int(part) for part in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main() -> None:
    parser = argparse.ArgumentParser(prog=PROG, description="抓取 CFETS 人民币汇率中间价")
    parser.add_argument("--dsn", help="PostgreSQL 连接串；缺省用 FX_DSN 环境变量")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD（同时重置回填进度）")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD；缺省今天")
    parser.add_argument("--full", action="store_true",
                        help=f"从默认回填起点 {DEFAULT_BACKFILL_START} 重抓（幂等覆盖）")
    parser.add_argument("--delay", type=float,
                        default=float(os.environ.get("FX_FETCH_DELAY", DEFAULT_FETCH_DELAY)),
                        help="历史接口的请求间隔秒数；低于 1 秒容易触发 403")
    parser.add_argument("--retries", type=int, default=3, help="单次请求失败重试次数")
    parser.add_argument("--watch", action="store_true", help="常驻，每天定时抓取")
    parser.add_argument("--at", default=os.environ.get("FX_FETCH_AT", DEFAULT_FETCH_AT),
                        help="每日抓取时刻 HH:MM（本地时区）")
    parser.add_argument("--no-market", action="store_true",
                        help="跳过市场参考汇率（exchangerate-api）抓取")
    args = parser.parse_args()

    if args.full and not args.start:
        args.start = DEFAULT_BACKFILL_START

    if not args.watch:
        try:
            run_once(args)
        except Exception as exc:  # noqa: BLE001 - CLI 顶层需要打印可读错误
            log(f"本轮未跑完（进度已保存，重跑会从断点续传）：{exc}")
            sys.exit(1)
        return

    log(f"常驻模式：每天 {args.at} 抓取一次（请求间隔 {args.delay}s）")
    first = True
    while True:
        if not first:
            # 首轮之后不再接受显式起点，改由库中进度决定，避免每天重复全量回填。
            args.start = None
            args.full = False
        first = False
        try:
            run_once(args)
        except Exception as exc:  # noqa: BLE001 - 常驻进程不能因单次失败退出
            log(f"本轮未跑完（不影响下一轮，进度已保存）：{exc}")
        wait = seconds_until(args.at)
        log(f"下次运行在 {wait / 3600:.1f} 小时后（{args.at}）")
        time.sleep(wait)


if __name__ == "__main__":
    main()
