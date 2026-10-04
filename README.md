# 海关计征汇率 MCP

PostgreSQL + 中国货币网（CFETS）人民币汇率中间价，提供**海关计征汇率查询**与**外币折人民币**两个核心能力。

回答的是一个具体问题：**这批货在这一次申报里，该用哪一天的汇率折人民币？**

## 为什么值得单独做一个服务

报关场景里有两类问题必须连起来用：HS 编码回答「这是什么」，汇率回答「值多少钱」。
查完编码确定完税价格之后，下一步就是折人民币算税。但**计征汇率不是市场汇率**，
也不是银行挂牌价——它有一条专门的规则，而且规则在 2025 年换过。

公开数据源都不提供「海关计征汇率」这个口径：汇率网站给的是即期市场价，
银行给的是挂牌价，模型凭记忆给出的多半是错的。这正是这个服务的价值：
把规则落成代码，让结果**可查、可复算、有依据**。

## 规则（海关总署令第 272 号）

《中华人民共和国海关进出口货物征税管理办法》（2025-01-14 公布）

- **第十三条** 进出口货物的价格及有关费用以外币计价的，按照计征汇率折合为人民币计算
  计税价格，采用四舍五入法计算至分。海关每月使用的计征汇率为**上一个月第三个星期三**
  中国人民银行授权**中国外汇交易中心**公布的**人民币汇率中间价**；第三个星期三
  **非银行间外汇市场交易日**的，**顺延采用下一个交易日**公布的人民币汇率中间价。
  如果上述汇率发生重大波动，海关总署认为必要时，可以另行规定计征汇率，并且对外公布。
- **第十四条** 进出口货物应当适用纳税人、扣缴义务人**完成申报之日**实施的税率和计征汇率。

### ⚠️ 新旧口径不同，网上资料多为旧版

| 维度 | 旧（海关总署令第 124 号，已废止） | 现（海关总署令第 272 号） |
| --- | --- | --- |
| 汇率口径 | 基准汇率；非基准币种用**中国银行现汇买入价与卖出价的中间值** | **CFETS 人民币汇率中间价** |
| 顺延条件 | 第三个星期三为**法定节假日** → 顺延第四个星期三 | 第三个星期三**非银行间外汇市场交易日** → 顺延下一个交易日 |

两处都不同，算出来的数不一样。服务返回里带 `legacy_caveat`，会显式提示这一点。

### 顺延怎么实现

不维护交易日历。CFETS 只在银行间外汇市场交易日公布中间价，所以**库里存在的日期本身就是
交易日**：取「≥ 第三个星期三 的最早一条记录」即可同时覆盖「当天是交易日」和
「当天不是、顺延到下一个交易日」两种情况。周末永远开不了市，法定节假日调休的坑也一并规避。

## 快速开始

```sh
cp .env.example .env
# 默认对外端口 8766，避免与 hscode 的 8765 冲突
docker compose up -d --build

docker compose logs -f fetcher     # 首次会自动全量回填
```

默认 MCP 地址：`http://127.0.0.1:8766/mcp/fx`，Streamable HTTP，无状态模式。

### 内网网关接入

网关容器可通过宿主机内网 IP 和映射端口访问 fx，无需加入 fx 的 Docker 网络。
Compose 的 MCP 端口映射为：

```yaml
ports:
  - "${FX_BIND_HOST:-127.0.0.1}:${FX_PORT:-8766}:8765"
  - "172.17.0.1:${FX_PORT:-8766}:8765"
```

如果网关与 fx 在同一台 Linux 宿主机的不同容器中，且宿主机 Docker 网桥地址为
`172.17.0.1`，网关可使用 `http://172.17.0.1:8766/mcp/fx`，协议选
**Streamable HTTP**，目标地址白名单添加 `172.17.0.1`。
`172.17.0.1` 必须是宿主机实际拥有的地址；Docker Desktop 或不同网桥地址的环境
需先调整这条映射。

第一条映射默认绑定 `127.0.0.1`，网关无法通过宿主机内网 IP 访问它。
参见 [Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/#publishing-ports)。

在运行 fx 的服务器上，修改 `.env`（将示例 IP 替换为服务器实际内网 IP）：

```dotenv
FX_BIND_HOST=192.168.1.100
FX_PORT=8766
```

重新创建应用容器，使端口绑定生效：

```sh
docker compose up -d --no-deps --force-recreate app
docker compose port app 8765
```

网关后台的 MCP 地址填写 `http://192.168.1.100:8766/mcp/fx`。
如果网关有目标地址白名单，将该内网 IP 加入白名单。
网关需要能路由到这个地址，服务器防火墙需允许网关访问 TCP 8766。
此配置将第一条 MCP 端口映射绑定到指定内网地址，同时保留 `172.17.0.1` 映射，
无需为它配置公网端口映射。

### 手动回填

```sh
# 手动回填历史区间
docker compose run --rm backfill
# 或指定区间
docker compose run --rm backfill fx-fetch --start 2024-01-01 --end 2024-12-31
```

### 本地开发

```sh
uv sync
uv run fx-fetch                     # 默认连 127.0.0.1:5436/fx，可用 FX_DSN 覆盖
uv run fx                           # stdio
uv run fx --http 8766               # HTTP
uv run python scripts/smoke_test.py
```

## 工具

| 工具 | 用途 |
| --- | --- |
| `get_customs_fx_rate` | 某次申报适用的计征汇率，含日期推导与依据 |
| `convert_customs_value` | 按计征汇率折人民币，四舍五入至分 |
| `get_fx_rate` | 指定交易日的中间价（非报关场景） |
| `get_latest_fx_rate` | 最近一个交易日公布的中间价（当前价，非实时行情） |
| `get_market_fx_rate` | 市场参考汇率（**非计征汇率**，覆盖约 160 币种） |
| `get_fx_rate_history` | 中间价历史序列 |
| `list_fx_currencies` | 可用币种与数据覆盖区间 |

资源：`fx://dataset-info`　提示词：`customs_value_workflow`

## REST API

不想走 MCP 协议时（脚本、内部系统、curl）用同一套能力的 HTTP 接口。**与 MCP 工具同源**：
共用 `src/fx/service.py`，口径与返回结构一致，不会两边算出不同的数。

```sh
docker compose up -d api          # 默认 http://127.0.0.1:8767/api
uv run fx-api --port 8767         # 本地开发
```

| 端点 | 对应工具 | 说明 |
| --- | --- | --- |
| `GET /api/health` | — | 连库状态与最新中间价日期 |
| `GET /api/customs-rate` | `get_customs_fx_rate` | 申报日适用的计征汇率 |
| `GET /api/convert` | `convert_customs_value` | 折人民币，四舍五入至分 |
| `GET /api/rate` | `get_fx_rate` | 指定交易日的中间价（非报关） |
| `GET /api/latest` | `get_latest_fx_rate` | 最近一个交易日公布的中间价（当前价） |
| `GET /api/market-rate` | `get_market_fx_rate` | 市场参考汇率（**非计征汇率**） |
| `GET /api/history` | `get_fx_rate_history` | 中间价历史序列 |
| `GET /api/currencies` | `list_fx_currencies` | 可用币种与覆盖区间 |
| `GET /api/dataset-info` | `fx://dataset-info` | 数据集说明 |

```sh
curl 'http://127.0.0.1:8767/api/customs-rate?currency=USD&declaration_date=2026-10-03'
curl 'http://127.0.0.1:8767/api/convert?amount=12345.67&currency=EUR&declaration_date=2026-10-03'
```

交互式文档：`/api/docs`（OpenAPI：`/api/openapi.json`）。

### 导入 Apifox / Postman

```sh
uv run python scripts/export_openapi.py     # 生成 docs/openapi.json（不需要启动服务）
```

Apifox：项目 → 导入 → **OpenAPI/Swagger** → 选 `docs/openapi.json`。
也可以直接填 URL `http://127.0.0.1:8767/api/openapi.json`——但服务默认只监听
`127.0.0.1`，Apifox 不在同一台机器时要用文件导入，或先改 `FX_API_BIND_HOST`。

导进去是 4 个分组、9 个端点，带中文摘要、参数说明与错误示例（400 / 404 / 503）。
改接口后重新跑一次导出再导入即可覆盖。

> **MCP 工具不在这次导出里**：MCP 是 JSON-RPC（`POST /mcp/fx`），不是 REST，
> OpenAPI 描述不了。要在 Apifox 里调 MCP，需自己建一个 POST 接口，
> body 形如 `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{...}}`；
> 日常验证用 `scripts/smoke_test.py` 更省事。

金额用**字符串**传（`amount=12345.67`），避免 JSON 浮点误差影响「四舍五入至分」。

### 「当前价」怎么用

`GET /api/latest` 返回最近一个交易日公布的中间价（不传 `currency` 则返回全部 25 个币种）：

```sh
curl 'http://127.0.0.1:8767/api/latest?currency=USD'
```

```json
{"rate_date": "2026-09-30", "as_of": "2026-10-03", "stale_days": 3,
 "currency": "USD", "cny_per_unit": "6.7351", "count": 1, "rates": [...]}
```

中间价每个交易日约 09:15 公布一次、**当日不再变动**，周末与法定节假日休市不公布。
所以「当前价」实际是「最近一个交易日的价格」：`stale_days` 偏大通常是休市（如国庆
10/1–10/8），不是抓取失败。它是中间价而非实时行情，也**不能用于报关折算**。

## 第二个数据源：市场参考汇率

CFETS 中间价有两个硬限制：**休市不更新**（国庆 10/1–10/8 中间价一动不动）、
**只覆盖 25 个币种**（VND、INR 等没有中间价）。所以另接一个境外源
exchangerate-api.com 补这两块：

| 维度 | CFETS 中间价 | 市场参考汇率 |
| --- | --- | --- |
| 更新 | 每个交易日约 09:15，休市不更新 | 每日 UTC 00:00，**周末节假日照常更新** |
| 覆盖 | 25 个币种 | 约 160 个币种 |
| 口径 | 官方中间价 | 市场汇率，与中间价有偏离（今日 USD 约 0.3%） |
| 能否报关 | ✅ 计征汇率取这个 | ❌ **不能** |

```sh
curl 'http://127.0.0.1:8767/api/market-rate?currency=USD'
curl 'http://127.0.0.1:8767/api/market-rate?currency=VND'   # CFETS 没有的冷门币种
```

返回里带 `cfets_reference`（同币种最新中间价与偏离百分比），便于交叉核对。

**为什么分表**：市场价存在独立的 `fx_market_rate`，**不并入** `fx_central_parity`。
计征规则是「取上一个月第三个星期三库里那条」，一旦市场价进了中间价表，就会被当成
计征汇率取用——那是错的。分表让它物理上进不了计征路径；缺数据时计征接口仍然报错，
只在错误信息里附一句市场参考价供核对。

配置（可选，不配就只抓中间价）：

```env
FX_EXCHANGERATE_API_KEY=...
```

`fx-fetch` 每轮最后抓一次，失败只记日志、不影响中间价主链路；`--no-market` 可跳过。



错误一律返回 `{"error": {"code": ..., "message": ...}}`，message 可直接展示：

| 状态码 | code | 触发 |
| --- | --- | --- |
| 400 | `invalid_input` | 币种无法识别或有歧义、日期格式不对、金额非法 |
| 404 | `data_unavailable` | 该日期/币种没有中间价，或库里尚未抓到 |
| 503 | `db_unavailable` | `/api/health` 连不上库 |

> 端口与绑定地址：`FX_API_PORT`（默认 8767）、`FX_API_BIND_HOST`（默认 127.0.0.1）。
> 服务本身不鉴权，与 MCP 端点一样靠网络边界隔离。

`get_customs_fx_rate` 返回示例（申报日 2026-10-03，美元）：

```json
{
  "currency": "USD",
  "declaration_date": "2026-10-03",
  "reference_month": "2026-09",
  "reference_date": "2026-09-16",
  "rate_date": "2026-09-16",
  "deferred": false,
  "rate_date_basis": "第三个星期三 2026-09-16 本身即为交易日，采用当日中间价",
  "cny_per_unit": "6.7628",
  "quote": {"pair": "USD/CNY", "style": "D", "unit": 1, "quoted_rate": "6.7628"},
  "source": "CFETS"
}
```

## 数据源与抓取策略

数据来自中国货币网，两个接口分工使用：

| 接口 | 用途 | 特点 |
| --- | --- | --- |
| `r/cms/www/chinamoney/data/fx/ccpr.json` | 每日增量 | 一次请求返回最新公布日的 25 个币种 |
| `ags/ms/cm-u-bk-ccpr/CcprHisNew` | 回填 / 补缺口 | 支持日期区间；`pageSize` 上限在 80–100 之间（100 会被直接拒绝，返回 403） |

`fx-fetch` 的顺序是：先调轻量接口拿最新公布日（日常到这一步就结束），只有发现库中最新日期
落后于公布日期时，才动历史接口补中间缺口。

### 首次回填

3 年 ≈ 44 个月 ≈ 44 次请求，按默认 1.5 秒间隔约 1 分钟：

```sh
docker compose run --rm backfill
```

回填加了两处防呆，都是踩过坑才加的：

1. **进度落库**（`fx_fetch_state.backfill_floor`）。不能按「库中最新日期 + 1」推断起点——
   一旦首次回填中途失败而较新的日期已经写入，`max(rate_date) + 1` 会直接跳过中间缺口，
   缺口就永久补不上了。所以只以落盘进度为准。
2. **逐月落库**。长回填若攒到最后一次性写入，中途失败会丢掉全部进度；
   现在每抓完一个月立即写库并推进进度。

因此重跑同一条命令即可续传，不需要手工清理。

默认起点取 **2023-01-01**：回填长度直接决定请求数，而计征汇率只需要「申报月的上一个月」，
3 年足够覆盖补报与追溯场景；要更长历史用 `--start` 显式指定。

### ⚠️ 最容易踩的坑：records 的层级

两个接口的响应形状**不一样**，历史接口尤其容易读错——`records` 是**顶层键**，
与 `data` 平级；只有币种列表 `head` 在 `data` 里面：

```json
{"head":    {"rep_code": "200"},
 "data":    {"head": ["USD/CNY", ...], "total": 21, "pageTotal": 1},
 "records": [{"date": "2026-09-30", "values": ["6.7351", ...]}, ...]}
```

读成 `payload["data"]["records"]` 会**永远拿到空列表，而且不抛异常**，表现为
「抓取成功但一条都没写入」，日志上完全看不出问题。本项目第一版就栽在这里，
而且因为不报错，一度被误判成「接口反爬限流」。

`_validate_history()` 是事后补的第二道防线：声明 `total > 0` 却没给记录时宁可报错，
也不要当成「该区间无数据」静默漏抓。但它的取值路径必须与 `parse_history` 保持一致——
读错路径会让它在每次正常响应上误报，反而把真正的解析 bug 掩盖掉。

### 币种方向

25 个货币对混用两种标价法，方向弄反结果会差出数量级：

- **直接标价** `1` 或 `100` 单位外币 = ? 人民币 —— `USD/CNY`、`100JPY/CNY` 等 10 个
- **间接标价** `1` 人民币 = ? 外币 —— `CNY/MYR`、`CNY/KRW` 等 15 个

两者都归一成 `cny_per_unit`（1 单位外币 = ? 人民币）后入库，**换算只认这一列**，
`quoted_rate` 原样保留以便核对——报关口径要能回中国货币网对数，所以不改写原始值。

因此接口返回里两种都有（`quote` 是原始、`normalized` 是归一后的）：

```json
"quote":      {"pair": "CNY/MYR", "style": "I", "unit": 1, "quoted_rate": "0.60670000",
               "style_label": "间接标价法（1 人民币 = ? 外币）",
               "normalized": {"pair": "MYR/CNY", "unit": 1, "cny_per_unit": "1.648261084556"}}
```

`style` 的 `D` / `I` 容易看懵，一律看 `normalized.pair`（恒为 `XXX/CNY`）与
`cny_per_unit` 即可。另注意 JPY 原始 `unit = 100`（`100JPY/CNY`），
归一后 `unit = 1`。

## 测试

```sh
uv run pytest tests -q              # 规则层单测（纯逻辑，不连库）
uv run python scripts/smoke_test.py # MCP 工具与资源
uv run python scripts/smoke_api.py  # REST 端点与错误路径
```

规则层（第三个星期三、跨年、顺延、四舍五入至分、币种归一、人工覆盖优先级）
由单测钉死——这里算错直接导致税算错。单测不连数据库，抓取与接口靠冒烟脚本。

## 维护命令

```sh
docker compose logs -f fetcher         # 跟踪定时抓取
docker compose logs -f app
curl -s http://127.0.0.1:8767/api/health | python -m json.tool   # 健康与滞后
docker compose down                    # 停止并保留数据卷

# 海关总署另行规定计征汇率时的人工覆盖（优先于 CFETS 数据）
docker compose exec db psql -U fx -d fx -c \
  "INSERT INTO fx_rate_override (rate_date, currency, cny_per_unit, note)
   VALUES ('2026-09-16','USD',7.1000,'海关总署另行规定') 
   ON CONFLICT (rate_date, currency) DO UPDATE SET cny_per_unit = EXCLUDED.cny_per_unit;"
```

### 健康检査怎么判故障

中间价滞后**不能直接判故障**：周末与法定节假日休市本来就不公布新价，国庆长假
连续 8 天没有新数据是正常的。所以 `/api/health` 分两级：

| 信号 | 阈值 | 结果 |
| --- | --- | --- |
| CFETS 中间价滞后 | `FX_STALE_WARN_DAYS`（默认 7） | 只给 `warnings`，HTTP 200 |
| 市场参考汇率滞后 | `FX_MARKET_STALE_DAYS`（默认 2） | `status=degraded`，**HTTP 503** |
| 连不上库 | — | `db_unavailable`，**HTTP 503** |

市场参考汇率是境外源、每日更新、**节假日照常**，它滞后才说明 `fx-fetch` 没在跑——
用第二个数据源交叉验证抓取链路，比只看中间价可靠得多。503 会让 api 容器的健康
检査失败，`docker compose ps` 里能直接看到。

## 接入 WorkBuddy

```json
{
  "mcpServers": {
    "fx": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp/fx"
    }
  }
}
```

> 新加入的 MCP 不会自动生效：到连接器管理页右上角的自定义连接器入口，点「信任」即可启用。

## 目录

```text
src/fx/customs.py       规则层：第三个星期三、顺延、折算、币种归一、数据完整度自检（纯逻辑，可单测）
src/fx/service.py       查询层：MCP 与 REST 共用的入参校验与结果组装（口径只有一份）
src/fx/server.py        MCP 服务与工具
src/fx/api.py           REST API（FastAPI，独立进程 / 独立端口）
src/fx/sources.py       抓取层：两个 CFETS 接口、响应结构校验、解析与入库
src/fx/fetch_rates.py   fx-fetch CLI：增量 / 回填续传 / 常驻定时
data/001_schema.sql     建表 DDL（db 容器 initdb 使用）
compose.yaml            db / fetcher / app / api / backfill 五个服务
```

MCP 与 REST 共用 `service.py`：新增能力改这一处，两个接口同时生效。

数据库四张表：`fx_central_parity`（中间价）、`fx_market_rate`（市场参考汇率，
与中间价分表）、`fx_rate_override`（人工覆盖）、`fx_fetch_state`（回填进度）。

## 边界

- 只提供**汇率与折算**：不含税率、完税价格审定、商品归类、税额计算。
- 中间价覆盖约 25 个币种，冷门币种没有中间价，服务会明确报错而不是猜一个值。
- 抓取依赖中国货币网的公开接口（站点前端自用的 JSON 端点，非官方文档化 API），
  接口结构变动会导致抓取失效；解析层已做结构校验，宁可报错也不会静默写入错误数据。
- 数据自行抓取维护，仅供辅助核算。**正式申报前请与报关行或海关确认。**
