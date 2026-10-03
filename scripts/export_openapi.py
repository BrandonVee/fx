"""导出 REST API 的 OpenAPI 文档，供 Apifox / Postman 导入。

不需要启动服务——直接从 FastAPI 应用生成：

    uv run python scripts/export_openapi.py
    uv run python scripts/export_openapi.py --out docs/openapi.json

Apifox 导入：项目 → 导入 → OpenAPI/Swagger → 选本文件（或填 URL
http://127.0.0.1:8767/api/openapi.json，需 Apifox 能访问该地址）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fx.api import app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/openapi.json", help="输出文件路径")
    options = parser.parse_args()

    spec = app.openapi()
    path = Path(options.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已导出 {path}：OpenAPI {spec['openapi']}，"
          f"{len(spec['paths'])} 个端点，{len(spec.get('tags', []))} 个分组")
    for tag in spec.get("tags", []):
        count = sum(
            1
            for ops in spec["paths"].values()
            for op in ops.values()
            if tag["name"] in op.get("tags", [])
        )
        print(f"  {tag['name']}：{count} 个")


if __name__ == "__main__":
    main()
