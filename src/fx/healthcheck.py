"""使用 MCP initialize 请求验证容器 HTTP 服务。"""
import json
import os
from urllib.request import Request, urlopen


def main() -> None:
    path = "/" + os.environ.get("MCP_HTTP_PATH", "/mcp/fx").lstrip("/")
    body = json.dumps({
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "healthcheck", "version": "1.0"}},
    }).encode()
    request = Request("http://127.0.0.1:8765" + path, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
    })
    with urlopen(request, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError(f"MCP 初始化失败：HTTP {response.status}")
        result = response.read().decode()
        if '"result"' not in result or '"error"' in result:
            raise RuntimeError("MCP 初始化未返回成功结果")


if __name__ == "__main__":
    main()
