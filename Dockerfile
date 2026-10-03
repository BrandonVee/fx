FROM python:3.13-slim-bookworm

ARG PYPI_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HTTP_PATH=/mcp/fx \
    TZ=Asia/Shanghai

WORKDIR /app

# psycopg[binary] 自带 libpq，无需 apt 安装数据库客户端库。
RUN useradd --create-home --uid 10001 fx

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY data/ ./data/

# 先装构建工具再用 --no-build-isolation，避免构建隔离阶段重复下载 setuptools。
RUN pip install --no-cache-dir --index-url "$PYPI_INDEX_URL" --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --index-url "$PYPI_INDEX_URL" --no-build-isolation . \
    && rm -rf /root/.cache

USER fx

# 8765 MCP（容器内端口）；8767 REST API
EXPOSE 8765 8767

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "fx.healthcheck"]

CMD ["fx", "--http", "8765", "--host", "0.0.0.0", "--stateless"]
