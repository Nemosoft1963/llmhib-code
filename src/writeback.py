"""取り込んだプロジェクトの変更を、元フォルダへ書き戻すための差分バンドルを作る。

- 比較の基準は、取り込み時点(import_baseline_sha)のコミット。そこからの 追加(A)・変更(M)・削除(D) を対象にする。
- 各ファイルについて、新しい内容のSHA-256と、取り込み時点(=元フォルダの内容)のSHA-256を記録する。
  ホスト側の適用スクリプトは、後者と元フォルダの現在の内容を突き合わせ、取り込み後に元が
  変更されていれば(競合)そのファイルを書き戻さない。
- このモジュールは元フォルダに一切触れない。バンドルを作るだけ(適用は scripts/apply-writeback.ps1)。
- 秘密情報の形をした内容を含むファイルは、バンドルから除外して報告する。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import checkpoints
from .importer import _secret_kinds_in
from .tools_core import WORKSPACE_ROOT, get_project_meta, get_workspace

BUNDLE_ROOT = WORKSPACE_ROOT / "_writeback"


class WritebackError(ValueError):
    pass


def _git_bytes(root: Path, *args: str) -> bytes:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if r.returncode != 0:
        raise WritebackError(r.stderr.decode("utf-8", "replace").strip() or "git error")
    return r.stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_changes(project: str) -> dict:
    workspace = get_workspace(project)
    meta = get_project_meta(project)
    baseline = meta.get("import_baseline_sha")
    if not baseline:
        raise WritebackError(f"プロジェクト {project!r} は取り込みプロジェクトではありません(基準がありません)")
    root = workspace.root
    checkpoints.commit_checkpoint(root, "checkpoint: 書き戻し前の状態")  # 未コミットの変更も対象にする
    raw = _git_bytes(root, "-c", "core.quotepath=off", "diff", "--name-status", "-z", "--no-renames", baseline, "HEAD")
    parts = raw.decode("utf-8").split("\0")
    entries, secrets = [], []
    i = 0
    while i + 1 < len(parts):
        status, path = parts[i], parts[i + 1]
        i += 2
        if not status:
            continue
        entry = {"path": path, "status": status[0], "base_sha256": None, "new_sha256": None, "size": 0}
        if status[0] in ("M", "D"):
            entry["base_sha256"] = _sha(_git_bytes(root, "show", f"{baseline}:{path}"))
        if status[0] in ("A", "M"):
            data = (root / path).read_bytes()
            entry["new_sha256"], entry["size"] = _sha(data), len(data)
            kinds = _secret_kinds_in(root / path)
            if kinds:
                secrets.append({"path": path, "kinds": kinds})
                continue
        entries.append(entry)
    return {
        "project": project,
        "import_source": meta.get("import_source"),
        "baseline_sha": baseline,
        "head_sha": _git_bytes(root, "rev-parse", "HEAD").decode().strip(),
        "changes": entries,
        "blocked_secret_files": secrets,
        "summary": {s: sum(1 for e in entries if e["status"] == s) for s in ("A", "M", "D")},
    }


def build_bundle(project: str, include: list[str] | None = None) -> dict:
    info = compute_changes(project)
    root = get_workspace(project).root
    changes = info["changes"]
    if include is not None:
        wanted = set(include)
        unknown = wanted - {c["path"] for c in changes}
        if unknown:
            raise WritebackError(f"変更一覧にないパスです: {sorted(unknown)[:5]}")
        changes = [c for c in changes if c["path"] in wanted]
    if not changes:
        raise WritebackError("書き戻す変更がありません")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = BUNDLE_ROOT / project / stamp
    if out.parent.exists():
        shutil.rmtree(out.parent, ignore_errors=True)  # 古いバンドルは残さない(最新のみ)
    (out / "files").mkdir(parents=True)
    for c in changes:
        if c["status"] in ("A", "M"):
            dest = out / "files" / c["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((root / c["path"]).read_bytes())
    manifest = {
        "project": project,
        "import_source": info["import_source"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline_sha": info["baseline_sha"],
        "head_sha": info["head_sha"],
        "changes": changes,
        "blocked_secret_files": info["blocked_secret_files"],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["bundle_path"] = str(out)
    return manifest
