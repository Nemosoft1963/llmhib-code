"""既存プロジェクト(ホスト側フォルダ)を、新しいプロジェクトとして取り込む。

- ホスト側の元フォルダ(NASなどDockerから直接見えない場所を含む)は、ホスト側の
  scripts/stage-import.ps1 が除外つきで IMPORT_ROOT(コンテナ内のステージング領域)へ
  コピーする。このモジュールはそのステージング領域からのみ読む。元フォルダは変更しない。
- 機密になり得るものは自動で除外する(外部AIへ渡る可能性があるため)。
  * 名前で判定: .env / 鍵・証明書 / credential・secret を含む名前 など(上書き不可)
  * 中身で判定: APIキー・トークン・秘密鍵の形をした文字列を含むテキスト(上書き不可)
  * 生成物・巨大ディレクトリ(.venv, node_modules, logs, backups 等)は既定で除外。
    こちらは include_dirs で明示的に含めることができる。
- 何を除外したかは必ず結果に含め、画面で確認できるようにする(値そのものは出さない)。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import checkpoints
from .tools_core import PROJECTS_ROOT, get_project_meta, get_workspace, set_project_meta

IMPORT_ROOT = Path(os.environ.get("IMPORT_ROOT", "/app/workspace/_import"))

MAX_FILE_BYTES = 1_000_000
MAX_FILES = 5000
MAX_TOTAL_BYTES = 60_000_000

# 既定で除外する生成物・巨大ディレクトリ(include_dirs で個別に含められる)
SOFT_EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode", ".codex",
    "dist", "build", "backups", "logs", "temp", "tmp", "data", "workspace", ".next",
}
SOFT_EXCLUDE_DIR_PATTERNS = ("*.egg-info",)

# 名前だけで機密とみなすファイル(上書き不可)
SECRET_NAME_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "id_rsa*", "id_ed25519*",
    ".netrc", "*credential*", "*secret*", "*.token", "*.keystore",
)
SECRET_NAME_ALLOW = (".env.example", ".env.sample", ".env.template")

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".7z",
    ".exe", ".dll", ".so", ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".mp3", ".mp4",
    ".wav", ".onnx", ".pt", ".bin", ".woff", ".woff2", ".ttf", ".lock",
}

# 中身が機密の形をしているか(高確信のものだけ。誤検出で取り込みが壊れないよう限定する)
SECRET_CONTENT_PATTERNS = {
    "OpenAI/互換キー": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "xAIキー": re.compile(r"xai-[A-Za-z0-9]{20,}"),
    "GitHubトークン": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    "Googleキー": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "Llamaキー": re.compile(r"LLM\|\d{8,}\|[A-Za-z0-9_-]{16,}"),
    "AWSアクセスキー": re.compile(r"AKIA[0-9A-Z]{16}"),
    "Slackトークン": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "秘密鍵": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}


class ImportError_(ValueError):
    pass


def resolve_source(source_path: str) -> Path:
    """ユーザー入力(ホスト側パスまたはステージング名)から、ステージング領域内のフォルダを解決する。

    ステージング名 = 元フォルダ名(パスの最後の要素)。ステージング領域の外は指せない。
    """
    raw = (source_path or "").strip().strip('"')
    if not raw:
        raise ImportError_("取り込み元のパスが空です")
    name = raw.replace("\\", "/").rstrip("/").split("/")[-1]
    if not name or name in {".", ".."}:
        raise ImportError_(f"取り込み元の指定が不正です: {source_path}")
    target = (IMPORT_ROOT / name).resolve()
    if not target.is_relative_to(IMPORT_ROOT.resolve()):
        raise ImportError_("取り込み元が許可範囲の外です")
    if not target.is_dir():
        raise ImportError_(
            f"ステージングされていません: {name}。先にホストで "
            f"scripts/stage-import.ps1 -Source \"{raw}\" を実行してください"
        )
    return target


def _is_secret_name(name: str) -> bool:
    low = name.lower()
    if low in SECRET_NAME_ALLOW:
        return False
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_NAME_PATTERNS)


def _secret_kinds_in(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return [kind for kind, rx in SECRET_CONTENT_PATTERNS.items() if rx.search(text)]


def scan_source(source: Path, include_dirs: set[str] | None = None) -> dict[str, Any]:
    """取り込み対象を走査し、含めるファイルと除外理由を返す(コピーはしない)。"""
    include_dirs = {d.lower() for d in (include_dirs or set())}
    included: list[tuple[Path, str, int]] = []
    excluded: dict[str, list[str]] = {
        "機密の名前": [], "機密の内容": [], "除外ディレクトリ": [], "バイナリ・ロック": [],
        "巨大ファイル": [], "シンボリックリンク": [],
    }
    total = 0
    for dirpath, dirnames, filenames in os.walk(source):
        rel_dir = Path(dirpath).relative_to(source)
        keep = []
        for d in sorted(dirnames):
            low = d.lower()
            full = Path(dirpath) / d
            soft = low in SOFT_EXCLUDE_DIRS or any(
                fnmatch.fnmatch(low, p) for p in SOFT_EXCLUDE_DIR_PATTERNS
            )
            rel = (rel_dir / d).as_posix() if str(rel_dir) != "." else d
            if full.is_symlink():
                excluded["シンボリックリンク"].append(rel + "/")
            elif soft and low not in include_dirs:
                excluded["除外ディレクトリ"].append(rel + "/")
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            rel = (rel_dir / fn).as_posix() if str(rel_dir) != "." else fn
            if full.is_symlink():
                excluded["シンボリックリンク"].append(rel)
            elif _is_secret_name(fn):
                excluded["機密の名前"].append(rel)
            elif full.suffix.lower() in BINARY_EXTENSIONS:
                excluded["バイナリ・ロック"].append(rel)
            else:
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                if size > MAX_FILE_BYTES:
                    excluded["巨大ファイル"].append(f"{rel} ({size // 1024}KB)")
                    continue
                kinds = _secret_kinds_in(full)
                if kinds:
                    excluded["機密の内容"].append(f"{rel} ({'/'.join(kinds)})")
                    continue
                included.append((full, rel, size))
                total += size
    return {
        "source": str(source),
        "included": included,
        "included_count": len(included),
        "included_bytes": total,
        "excluded": {k: v for k, v in excluded.items() if v},
        "within_limits": len(included) <= MAX_FILES and total <= MAX_TOTAL_BYTES,
    }


def preview(source_path: str, include_dirs: list[str] | None = None) -> dict[str, Any]:
    source = resolve_source(source_path)
    scan = scan_source(source, set(include_dirs or []))
    scan["sample_files"] = [rel for _p, rel, _s in scan["included"][:40]]
    del scan["included"]
    scan["limits"] = {"max_files": MAX_FILES, "max_bytes": MAX_TOTAL_BYTES}
    return scan


def import_project(name: str, source_path: str, include_dirs: list[str] | None = None) -> dict[str, Any]:
    source = resolve_source(source_path)
    workspace = get_workspace(name)  # 名前検証・作成
    if any(p for p in workspace.root.iterdir() if p.name != ".git"):
        raise ImportError_(f"プロジェクト {name!r} は既に存在し空ではありません。別の名前にしてください")
    scan = scan_source(source, set(include_dirs or []))
    if not scan["within_limits"]:
        raise ImportError_(
            f"取り込み対象が大きすぎます({scan['included_count']}ファイル / "
            f"{scan['included_bytes'] // 1_000_000}MB)。除外設定を見直してください"
        )
    for src, rel, _size in scan["included"]:
        dest = workspace.root / PurePosixPath(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
    checkpoints.ensure_repo(workspace.root)
    baseline = checkpoints.commit_checkpoint(workspace.root, "import: 取り込み時点(元フォルダの状態)")
    if baseline is None:
        baseline = checkpoints._git(workspace.root, "rev-parse", "HEAD").stdout.strip()
    summary = {k: len(v) for k, v in scan["excluded"].items()}
    set_project_meta(
        name,
        import_source=str(source_path),
        imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        import_baseline_sha=baseline,
        import_excluded=scan["excluded"],
    )
    import shutil

    shutil.rmtree(source, ignore_errors=True)  # ステージングのコピーは取り込み後に消す
    return {
        "project": name,
        "included_count": scan["included_count"],
        "included_bytes": scan["included_bytes"],
        "excluded": scan["excluded"],
        "excluded_summary": summary,
        "baseline_sha": baseline,
    }
