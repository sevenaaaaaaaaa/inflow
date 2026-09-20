#!/usr/bin/env python3
"""导出或核对 OpenAPI 快照。

  python scripts/export_openapi.py --write   # 更新 contracts/openapi.json
  python scripts/export_openapi.py --check   # CI：不一致则 exit 1
"""

from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("INSFLOW_DISABLE_SCHEDULER", "1")
os.environ.setdefault("INSFLOW_MASTER_KEY", "openapi-export")
os.environ.setdefault("INSFLOW_MCP_AUTH", "0")


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAPI 契约快照")
    parser.add_argument("--write", action="store_true", help="回写 contracts/openapi.json")
    parser.add_argument("--check", action="store_true", help="与快照 diff，不一致则失败")
    args = parser.parse_args()

    from insflow.core.openapi_contract import check_snapshot, dump_spec, write_snapshot

    if args.check:
        report = check_snapshot()
        if report["ok"]:
            print(f"OpenAPI 契约一致: {report['path']}")
            return 0
        print(report["reason"], file=sys.stderr)
        print(report.get("hint", ""), file=sys.stderr)
        return 1
    if args.write:
        path = write_snapshot()
        print(f"已写入 {path}")
        return 0
    sys.stdout.write(dump_spec())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
