-- 海关计征汇率服务：基础结构
--
-- 数据来源：中国货币网（中国外汇交易中心 CFETS）人民币汇率中间价。
-- 只存中间价原始值与归一化后的折算率，不做任何衍生推算；
-- 「哪一天适用哪一条」由服务层的规则代码决定（见 src/fx/customs.py）。

CREATE TABLE IF NOT EXISTS fx_central_parity (
    rate_date    date           NOT NULL,
    currency     text           NOT NULL,
    pair         text           NOT NULL,
    -- D = 直接标价法（1 或 100 单位外币 = ? 人民币）
    -- I = 间接标价法（1 人民币 = ? 外币），见 CFETS 对林吉特/卢布等 15 个币种的报价
    quote_style  char(1)        NOT NULL CHECK (quote_style IN ('D', 'I')),
    -- 报价单位：JPY 为 100，其余为 1
    unit         integer        NOT NULL CHECK (unit > 0),
    -- CFETS 原始报价，原样保留，便于比对
    quoted_rate  numeric(20, 8) NOT NULL CHECK (quoted_rate > 0),
    -- 归一化折算率：1 单位外币 = ? 人民币。换算只用这一列。
    cny_per_unit numeric(24, 12) NOT NULL CHECK (cny_per_unit > 0),
    source       text           NOT NULL DEFAULT 'CFETS',
    fetched_at   timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, currency)
);

CREATE INDEX IF NOT EXISTS fx_central_parity_currency_date_idx
    ON fx_central_parity (currency, rate_date DESC);

-- 人工覆盖：海关总署在汇率发生重大波动时「可以另行规定计征汇率，并且对外公布」。
-- 命中 (rate_date, currency) 时优先于 CFETS 数据。
CREATE TABLE IF NOT EXISTS fx_rate_override (
    rate_date    date           NOT NULL,
    currency     text           NOT NULL,
    cny_per_unit numeric(24, 12) NOT NULL CHECK (cny_per_unit > 0),
    note         text,
    created_at   timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, currency)
);

-- 市场参考汇率（exchangerate-api.com），与中间价**分表存放、口径隔离**。
--
-- 用途：休市期间中间价不更新时给一个持续更新的市场参考价，以及 CFETS 25 个币种
--       之外（如 VND、INR）的冷门币种参考。
-- ⚠️ 这是市场汇率，**不是计征汇率**，绝不参与 customs 链路的推算：
--    272 号令要求的是「上一个月第三个星期三 CFETS 人民币汇率中间价」。
--    分表而不是并入 fx_central_parity，就是为了让它物理上进不了计征路径。
CREATE TABLE IF NOT EXISTS fx_market_rate (
    rate_date    date           NOT NULL,
    currency     text           NOT NULL,
    pair         text           NOT NULL,
    -- 该源统一以 CNY 为基准报价（1 人民币 = ? 外币），即间接标价
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

-- 抓取进度。回填动辄上百次请求，中途失败会丢掉未落库的部分，进度必须落盘；
-- 否则「库中最新日期 + 1」会把中间缺口永久跳过。
-- key = 'backfill_floor'，value = 仍需抓取的最早日期（YYYY-MM-DD）。
CREATE TABLE IF NOT EXISTS fx_fetch_state (
    key        text        PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
