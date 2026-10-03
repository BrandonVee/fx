"""数据库连接与建表。

DSN 解析优先级：显式参数 > 环境变量 FX_DSN > config.DEFAULT_DSN。
psycopg 自身也会读取 PGUSER / PGPASSWORD / PGHOST 等 libpq 变量补齐连接串缺失部分。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import DEFAULT_DSN

SCHEMA_FILENAME = "001_schema.sql"


def _schema_candidates() -> list[Path]:
    """按优先级返回可能的 schema 文件位置。

    开发布局与安装布局的目录深度不同，镜像里也与源码树不一致，
    因此不写死一个路径，而是依次探测。
    """
    candidates: list[Path] = []
    override = os.environ.get("FX_SCHEMA_PATH")
    if override:
        candidates.append(Path(override))
    here = Path(__file__).resolve()
    # 开发布局：<repo>/src/fx/db.py -> <repo>/data/001_schema.sql
    candidates.append(here.parent.parent.parent / "data" / SCHEMA_FILENAME)
    # 容器内：镜像把 data/ 放在 /app/data
    candidates.append(Path("/app/data") / SCHEMA_FILENAME)
    candidates.append(Path.cwd() / "data" / SCHEMA_FILENAME)
    return candidates


def dsn(override: str | None = None) -> str:
    """解析实际使用的连接串。"""
    return override or os.environ.get("FX_DSN") or DEFAULT_DSN


def connect(override: str | None = None) -> psycopg.Connection:
    """连接到库（返回 dict 行）。"""
    return psycopg.connect(dsn(override), row_factory=dict_row)


def ensure_schema(conn: psycopg.Connection, schema_path: Path | None = None) -> None:
    """幂等建表。

    docker compose 下由 db 容器的 initdb 完成；本机开发或 schema 文件缺失时
    由本函数兜底，保证 fx-fetch 在任何布局下都能跑起来。
    """
    path = schema_path
    if path is None:
        path = next((p for p in _schema_candidates() if p.exists()), None)
    sql = path.read_text(encoding="utf-8") if path else _FALLBACK_DDL
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


_FALLBACK_DDL = """
CREATE TABLE IF NOT EXISTS fx_central_parity (
    rate_date    date           NOT NULL,
    currency     text           NOT NULL,
    pair         text           NOT NULL,
    quote_style  char(1)        NOT NULL CHECK (quote_style IN ('D', 'I')),
    unit         integer        NOT NULL CHECK (unit > 0),
    quoted_rate  numeric(20, 8) NOT NULL CHECK (quoted_rate > 0),
    cny_per_unit numeric(24, 12) NOT NULL CHECK (cny_per_unit > 0),
    source       text           NOT NULL DEFAULT 'CFETS',
    fetched_at   timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, currency)
);
CREATE INDEX IF NOT EXISTS fx_central_parity_currency_date_idx
    ON fx_central_parity (currency, rate_date DESC);
CREATE TABLE IF NOT EXISTS fx_market_rate (
    rate_date    date           NOT NULL,
    currency     text           NOT NULL,
    pair         text           NOT NULL,
    quote_style  char(1)        NOT NULL DEFAULT 'I' CHECK (quote_style IN ('D', 'I')),
    unit         integer        NOT NULL DEFAULT 1 CHECK (unit > 0),
    quoted_rate  numeric(20, 8) NOT NULL CHECK (quoted_rate > 0),
    cny_per_unit numeric(24, 12) NOT NULL CHECK (cny_per_unit > 0),
    source       text           NOT NULL DEFAULT 'MARKET',
    fetched_at   timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, currency)
);
CREATE INDEX IF NOT EXISTS fx_market_rate_currency_date_idx
    ON fx_market_rate (currency, rate_date DESC);
CREATE TABLE IF NOT EXISTS fx_rate_override (
    rate_date    date           NOT NULL,
    currency     text           NOT NULL,
    cny_per_unit numeric(24, 12) NOT NULL CHECK (cny_per_unit > 0),
    note         text,
    created_at   timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, currency)
);
CREATE TABLE IF NOT EXISTS fx_fetch_state (
    key        text        PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""
