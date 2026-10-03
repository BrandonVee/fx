"""本地开发的默认数据库连接；部署通过 FX_DSN 或 libpq 环境变量覆盖。"""

DEFAULT_DSN = "postgresql://fx:fx-dev-password@127.0.0.1:5436/fx"

# 首次运行（库为空）时的回填起点。
#
# 回填起点刻意不从 CFETS 最早的 2006 年、也不从 hscode 数据的 2015 年起步：
# 回填长度直接决定要发多少个请求，而计征汇率只需要「申报月的上一个月」，
# 实务上 3 年历史足够覆盖各类补报与追溯场景。
# 实测 3 年 ≈ 44 个月 ≈ 44 次请求，按 1.5 秒间隔约 1 分钟即可跑完。
# 需要更长历史时用 `fx-fetch --start YYYY-MM-DD` 显式指定。
DEFAULT_BACKFILL_START = "2023-01-01"

# 每日抓取时刻（本地时区，部署下为 Asia/Shanghai）。CFETS 约 09:15 发布当日中间价。
DEFAULT_FETCH_AT = "09:40"

# 请求间隔（秒）。按月切分后请求数很少，1.5 秒足以稳当取到数据；调大只是更保守。
DEFAULT_FETCH_DELAY = 1.5

# /api/health 的数据滞后阈值（天）。
#
# 中间价滞后**不能直接判定故障**：周末与法定节假日休市本来就不公布新价，
# 国庆长假连续 8 天没有新数据是正常的。所以中间价只给 warning（HTTP 200）。
# 市场参考汇率是境外源、每日 UTC 00:00 更新、节假日照常，它滞后才是真正
# 的信号——说明 fx-fetch 没在跑，此时 health 返回 503，容器健康检査会失败。
DEFAULT_CFETS_STALE_WARN_DAYS = 7
DEFAULT_MARKET_STALE_FAIL_DAYS = 2
