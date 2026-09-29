"""TRIZ情報収集エージェントの承認済みエクスポートを、プロジェクトの inputs/ へ取り込む。

- 取得元は収集アプリの GET /api/export(人間が承認した設計ネタのみを返す仕様)。
- 保存物: inputs/triz-problems-export.json(そのままのJSON)と inputs/triz-problems-export.meta.json
  (取得元URL・取得時刻・SHA-256・件数)。ファイル内容は加工しない。
- 取得元は環境変数 TRIZ_COLLECTOR_URL(既定: http://host.docker.internal:8088)。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import httpx

from . import checkpoints
from .tools_core import get_workspace

COLLECTOR_URL = os.environ.get("TRIZ_COLLECTOR_URL", "http://host.docker.internal:8088").rstrip("/")


def attach_triz_export(project: str) -> dict:
    workspace = get_workspace(project)
    url = f"{COLLECTOR_URL}/api/export"
    try:
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ValueError(f"TRIZ収集アプリから取得できません({url}): {e}") from e
    raw = resp.content
    payload = json.loads(raw)
    items = (payload.get("export") or {}).get("items")
    if not isinstance(items, list):
        raise ValueError("想定外の形式です(export.items が見つかりません)")
    inputs = workspace.root / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "triz-problems-export.json").write_bytes(raw)
    meta = {
        "source_url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "item_count": len(items),
        "format": (payload.get("export") or {}).get("format"),
        "note": "人間が承認済みの設計ネタのみ。各itemのevidenceに出典URL・取得時刻・SHA-256・引用を含む。",
    }
    (inputs / "triz-problems-export.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sha = checkpoints.commit_checkpoint(workspace.root, f"inputs: TRIZ収集データ {len(items)}件を取り込み")
    return {"project": project, "item_count": len(items), "sha256": meta["sha256"], "checkpoint": sha}
