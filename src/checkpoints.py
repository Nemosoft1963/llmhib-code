"""git を使った、プロジェクトごとのworkspaceのチェックポイント(スナップショット)管理。

承認されたツール呼び出し(write_file / run_command / 副作用のあるMCPツール)が
プロジェクトのファイルを変更するたびに自動コミットし、あとから一覧・差分確認・
復元(ロールバック)できるようにする。すべての関数はプロジェクトのルートディレクトリ
(Path)を明示的に受け取り、プロジェクトをまたいで状態を共有しない。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SEC = 30


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SEC,
    )


def ensure_repo(root: Path) -> None:
    """プロジェクトのrootがgitリポジトリでなければ初期化する。"""
    if (root / ".git").exists():
        return
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "code-agent@localhost")
    _git(root, "config", "user.name", "code-agent")
    _git(root, "add", "-A")
    _git(root, "commit", "--allow-empty", "-q", "-m", "checkpoint: 初期状態")


def commit_checkpoint(root: Path, message: str) -> str | None:
    """変更があればチェックポイントとしてコミットし、そのSHAを返す。変更が無ければNone。"""
    ensure_repo(root)
    _git(root, "add", "-A")
    status = _git(root, "status", "--porcelain")
    if not status.stdout.strip():
        return None
    result = _git(root, "commit", "-q", "-m", message or "checkpoint")
    if result.returncode != 0:
        return None
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def list_checkpoints(root: Path, limit: int = 50) -> list[dict[str, Any]]:
    """新しい順にチェックポイント一覧を返す。"""
    ensure_repo(root)
    result = _git(root, "log", f"-{limit}", "--pretty=format:%H%x1f%ct%x1f%s")
    checkpoints: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, ts, msg = parts
        checkpoints.append({"sha": sha, "timestamp": int(ts), "message": msg})
    return checkpoints


def diff_checkpoint(root: Path, sha: str) -> str:
    """指定したチェックポイントの差分(そのコミット自体の変更内容)を返す。"""
    ensure_repo(root)
    result = _git(root, "show", sha, "--stat", "-p")
    if result.returncode != 0:
        return f"エラー: 差分の取得に失敗しました: {result.stderr}"
    return result.stdout


def restore_checkpoint(root: Path, sha: str) -> dict[str, Any]:
    """指定したチェックポイントへ復元する(git reset --hard)。

    復元前の状態も自動的にコミットして残すため、復元操作自体もあとから取り消せる。
    """
    ensure_repo(root)
    before_sha = commit_checkpoint(root, f"checkpoint: {sha[:8]}への復元前の自動保存")
    result = _git(root, "reset", "--hard", sha)
    if result.returncode != 0:
        raise RuntimeError(f"復元に失敗しました: {result.stderr}")
    return {"restored_to": sha, "auto_saved_before": before_sha}
