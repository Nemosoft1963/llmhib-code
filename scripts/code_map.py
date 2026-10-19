"""プロジェクト構造の解析結果をJSONで標準出力に出す(人が手で確認できるように)。

使い方:
    python scripts/code_map.py <root> [--impact <target>] [--route-js] [--full]

- 既定: 要約(件数・complete・parse_errors と未解決の上位)をJSONで出す。
- --full: 全体のインデックスをJSONで出す。
- --impact <target>: impact(target) の結果を出す。
- --route-js: route_js_map の結果を出す。
- --impact と --route-js の両方を指定したら、{"impact": ..., "route_js_map": ...} を出す。
  (--impact/--route-js 指定時は要約/全体にはならない)

読み取り専用: 対象ファイルを変更しない。標準ライブラリのみ。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import code_analysis


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="code analysis map (read-only)")
    parser.add_argument("root", help="解析するプロジェクトのルート")
    parser.add_argument("--impact", default=None, help="影響範囲を見る対象(ファイルパス or module.func)")
    parser.add_argument("--route-js", action="store_true", help="JSとサーバルートの突合も出す")
    parser.add_argument("--full", action="store_true", help="全体のインデックスを出す(既定は要約)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(json.dumps({"error": f"not a directory: {args.root}"},
                         ensure_ascii=False, indent=2))
        return 1
    index = code_analysis.build_index(root)
    if args.impact is None and not args.route_js:
        if args.full:
            print(json.dumps(index, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(code_analysis.summarize_index(index),
                             ensure_ascii=False, indent=2))
        return 0
    out: dict = {}
    if args.impact is not None:
        out["impact"] = code_analysis.impact(index, args.impact)
    if args.route_js:
        out["route_js_map"] = code_analysis.route_js_map(index)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
