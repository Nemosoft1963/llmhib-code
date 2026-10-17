"""セバスの「Jenkins指示パッケージ」を取り込み、納品データ(delivery.json)を作る橋渡し。

セバス本番へは**接続しない・送信しない**(ファイルの受取りと、ファイルの作成だけ)。
パッケージは、人がセバス側から取得して、ここ(ジェンキンス)へ渡す。納品データも、
人がセバス側へ渡す。認証情報・接続先は扱わない。標準ライブラリだけで動く(テスト容易性)。

流れ:
  1. verify_package        指示パッケージ(instruction.json / REQUEST.md / manifest.json)の
                           完全性(SHA-256)・スキーマ・変更禁止境界・proposal_id を検査する。
  2. store_package         検査に通ったものだけ inbox に保存する(冪等。別内容の再投入は拒否)。
  3. prepare_workcopy      基準版(base_ref)が一致するときだけ、本番コピーから**隔離した作業コピー**を作る。
  4. build_delivery        作業コピーの差分・テスト結果から、セバスの `record_delivery` が受け取る
                           形式の delivery.json を作る。

fail-closed: 検査できない・読めない・不一致は、すべて拒否(ok=False と理由)とし、通過扱いにしない。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

SCHEMA_INSTRUCTION = "sebas.development-instruction/v1"
SCHEMA_MANIFEST = "sebas.development-manifest/v1"

#: proposal_id は安全な文字種・長さに制限する(セバス側と同じ。パストラバーサル拒否)。
PROPOSAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 変更禁止境界として、指示に**必ず含まれていなければならない**語(欠けた指示は拒否する)。
REQUIRED_FORBIDDEN_MARKERS = (".env", "/data", "docker-compose", "docker/proxy", "安全ゲート")

#: 作業コピーに入れないもの(秘密・環境・巨大物)。
COPY_EXCLUDE_NAMES = {
    ".env", ".git", ".venv", "__pycache__", ".pytest_cache", "node_modules",
    "data", "backups", "logs", ".codex_update_backups", ".jenkins_backup",
    "workspace", "temp", "experience_memory", "experience_lessons",
}
# 例外として作業コピーに入れる環境の見本(秘密を含まない。リポジトリに含まれる雛形)。
# 実際の .env や .env.local などは入れない。納品の差分でこれらを変更すると禁止領域として拒否される。
COPY_ALLOW_ENV_NAMES = {".env.example"}
COPY_EXCLUDE_SUFFIXES = (".pyc", ".sqlite3", ".db", ".db-wal", ".db-shm")

MAX_FILE_BYTES = 2_000_000
MAX_DIFF_CHARS = 400_000
TEST_TIMEOUT_SEC = 1500


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail(reason: str, **extra: Any) -> dict:
    out = {"ok": False, "reason": reason}
    out.update(extra)
    return out


# ---------------------------------------------------------------- 基準版

def base_ref_of(root: str | Path) -> str:
    """セバスの `capability_export.get_base_ref` と同じ規則で、app/*.py の内容から基準版を作る。

    読めるファイルが1つも無ければ "unknown"(不明は一致とみなさない)。
    """
    app_dir = Path(root) / "app"
    digest = hashlib.sha256()
    try:
        files = sorted(p for p in app_dir.glob("*.py") if p.is_file())
    except OSError:
        files = []
    count = 0
    for path in files[:200]:
        try:
            h = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(b":")
        digest.update(h.encode("utf-8"))
        digest.update(b"\n")
        count += 1
    if count == 0:
        return "unknown"
    return f"app-content-{digest.hexdigest()[:16]}-files{count}"


# ---------------------------------------------------------------- 1. 検査

def verify_package(instruction_text: str, request_md: str, manifest_text: str) -> dict:
    """指示パッケージの完全性と内容を検査する(副作用なし)。"""
    for name, value in (("instruction.json", instruction_text),
                        ("REQUEST.md", request_md),
                        ("manifest.json", manifest_text)):
        if not isinstance(value, str) or not value.strip():
            return _fail(f"{name} が空です")
        if len(value.encode("utf-8")) > MAX_FILE_BYTES:
            return _fail(f"{name} が大きすぎます")
    try:
        manifest = json.loads(manifest_text)
        instruction = json.loads(instruction_text)
    except (ValueError, TypeError) as exc:
        return _fail(f"JSONを読めません: {type(exc).__name__}")
    if not isinstance(manifest, dict) or not isinstance(instruction, dict):
        return _fail("manifest / instruction の形式が不正です")
    if manifest.get("schema") != SCHEMA_MANIFEST:
        return _fail("manifest のスキーマが未対応です")
    if instruction.get("schema") != SCHEMA_INSTRUCTION:
        return _fail("instruction のスキーマが未対応です")
    proposal_id = str(instruction.get("proposal_id") or "")
    if not PROPOSAL_ID_RE.match(proposal_id):
        return _fail("proposal_id が不正です")
    if str(manifest.get("proposal_id") or "") != proposal_id:
        return _fail("manifest と instruction の proposal_id が一致しません")
    files = manifest.get("files")
    if not isinstance(files, dict):
        return _fail("manifest に files がありません")
    expected = {"instruction.json": instruction_text.encode("utf-8"),
                "REQUEST.md": request_md.encode("utf-8")}
    for name, data in expected.items():
        declared = str(files.get(name) or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", declared):
            return _fail(f"manifest の {name} のハッシュが不正です")
        if declared != _sha256_bytes(data):
            return _fail(f"{name} のSHA-256が manifest と一致しません(改ざんまたは破損)")
    forbidden = instruction.get("change_forbidden")
    if not isinstance(forbidden, list) or not forbidden:
        return _fail("変更禁止境界(change_forbidden)がありません")
    joined = "\n".join(str(x) for x in forbidden)
    missing = [m for m in REQUIRED_FORBIDDEN_MARKERS if m not in joined]
    if missing:
        return _fail("変更禁止境界に必須の項目が欠けています", missing=missing)
    base_ref = str(instruction.get("base_ref") or "").strip()
    if not base_ref:
        return _fail("基準版(base_ref)がありません")
    tests = instruction.get("acceptance_tests")
    if not isinstance(tests, list) or not [t for t in tests if str(t).strip()]:
        return _fail("受入テストがありません")
    return {"ok": True, "proposal_id": proposal_id, "base_ref": base_ref,
            "required_capability_id": str(instruction.get("required_capability_id") or ""),
            "manifest_sha256": _sha256_bytes(manifest_text.encode("utf-8")),
            "acceptance_tests": [str(t) for t in tests],
            "forbidden": [str(x) for x in forbidden]}


# ---------------------------------------------------------------- 2. 保存

def inbox_dir(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / "sebas_inbox"


def store_package(workspace_root: str | Path, instruction_text: str, request_md: str,
                  manifest_text: str) -> dict:
    """検査に通ったパッケージだけを inbox に保存する。同一内容は冪等、別内容は拒否する。"""
    checked = verify_package(instruction_text, request_md, manifest_text)
    if not checked.get("ok"):
        return checked
    pid = checked["proposal_id"]
    target = inbox_dir(workspace_root) / pid
    if target.exists():
        try:
            same = ((target / "manifest.json").read_text(encoding="utf-8") == manifest_text
                    and (target / "instruction.json").read_text(encoding="utf-8") == instruction_text
                    and (target / "REQUEST.md").read_text(encoding="utf-8") == request_md)
        except OSError as exc:
            return _fail(f"既存の保存を確認できません: {type(exc).__name__}", proposal_id=pid)
        if same:
            return {**checked, "stored": True, "idempotent": True}
        return _fail("同じ proposal_id で別内容のパッケージが既にあります(上書きしません)",
                     proposal_id=pid)
    tmp = inbox_dir(workspace_root) / f".{pid}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        tmp.mkdir(parents=True, exist_ok=False)
        (tmp / "instruction.json").write_text(instruction_text, encoding="utf-8")
        (tmp / "REQUEST.md").write_text(request_md, encoding="utf-8")
        (tmp / "manifest.json").write_text(manifest_text, encoding="utf-8")
        tmp.rename(target)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return _fail(f"保存に失敗しました: {type(exc).__name__}", proposal_id=pid)
    return {**checked, "stored": True, "idempotent": False}


def load_package(workspace_root: str | Path, proposal_id: str) -> dict:
    """inbox から読み直して、再検査する(保存後の改ざんも検出する)。"""
    if not PROPOSAL_ID_RE.match(str(proposal_id or "")):
        return _fail("proposal_id が不正です")
    base = inbox_dir(workspace_root) / str(proposal_id)
    try:
        instruction_text = (base / "instruction.json").read_text(encoding="utf-8")
        request_md = (base / "REQUEST.md").read_text(encoding="utf-8")
        manifest_text = (base / "manifest.json").read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"パッケージを読めません: {type(exc).__name__}", proposal_id=str(proposal_id))
    checked = verify_package(instruction_text, request_md, manifest_text)
    if not checked.get("ok"):
        return checked
    return {**checked, "instruction": json.loads(instruction_text), "request_md": request_md}


# ---------------------------------------------------------------- 3. 作業コピー

def _git(cwd: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "sebas-bridge", "GIT_AUTHOR_EMAIL": "bridge@example.invalid",
                "GIT_COMMITTER_NAME": "sebas-bridge", "GIT_COMMITTER_EMAIL": "bridge@example.invalid"})
    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=timeout, env=env)


def workcopy_name(source_project: str, proposal_id: str) -> str:
    return f"{source_project}__{proposal_id[:12]}"


def _copy_filtered(src: Path, dst: Path) -> int:
    count = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in COPY_EXCLUDE_NAMES]
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            if name in COPY_EXCLUDE_NAMES or name.endswith(COPY_EXCLUDE_SUFFIXES):
                continue
            if name.startswith(".env") and name not in COPY_ALLOW_ENV_NAMES:
                continue
            source = Path(dirpath) / name
            if source.is_symlink():
                continue
            shutil.copy2(source, dst / rel / name)
            count += 1
    return count


def prepare_workcopy(workspace_root: str | Path, source_project: str, proposal_id: str) -> dict:
    """基準版が一致するときだけ、隔離した作業コピーを作る(冪等)。

    - パッケージを inbox から再検査する(改ざん検出)。
    - コピー元の基準版(app/*.py のハッシュ)が指示の base_ref と一致しなければ拒否する
      (セバス本番の版と、ここにあるコピーがずれているので、先に再同期が必要)。
    - .env・データ・.git・.venv などはコピーしない。.venv は読み取り用にシンボリックリンクする。
    """
    pkg = load_package(workspace_root, proposal_id)
    if not pkg.get("ok"):
        return pkg
    projects = Path(workspace_root) / "projects"
    source = projects / source_project
    if not source.is_dir():
        return _fail("コピー元のプロジェクトがありません", source_project=source_project)
    actual = base_ref_of(source)
    wanted = pkg["base_ref"]
    if actual == "unknown" or actual != wanted:
        return _fail("コピー元の基準版が指示と一致しません。先に本番との再同期が必要です",
                     expected_base_ref=wanted, actual_base_ref=actual)
    name = workcopy_name(source_project, proposal_id)
    target = projects / name
    marker = target / ".sebas_intake.json"
    if target.exists():
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _fail("既存の作業コピーの記録を読めません(上書きしません)", workcopy=name)
        if info.get("proposal_id") != proposal_id:
            return _fail("同名の別の作業コピーがあります(上書きしません)", workcopy=name)
        return {"ok": True, "idempotent": True, "workcopy": name, "base_ref": wanted,
                "base_commit": info.get("base_commit", ""), "proposal_id": proposal_id}
    tmp = projects / f".{name}.{os.getpid()}.tmp"
    try:
        tmp.mkdir(parents=True, exist_ok=False)
        copied = _copy_filtered(source, tmp)
        venv = source / ".venv"
        if venv.exists():
            (tmp / ".venv").symlink_to(venv.resolve(), target_is_directory=True)
        init = _git(tmp, "init", "-q")
        if init.returncode != 0:
            raise RuntimeError("git init に失敗")
        gi = tmp / ".gitignore"
        gi.write_text(".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n.sebas_intake.json\n", encoding="utf-8")
        _git(tmp, "add", "-A")
        commit = _git(tmp, "commit", "-q", "-m", f"baseline for {proposal_id}")
        if commit.returncode != 0:
            raise RuntimeError("baseline commit に失敗")
        head = _git(tmp, "rev-parse", "HEAD")
        base_commit = head.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
            raise RuntimeError("baseline commit を取得できません")
        (tmp / ".sebas_intake.json").write_text(json.dumps({
            "proposal_id": proposal_id, "base_ref": wanted, "base_commit": base_commit,
            "manifest_sha256": pkg["manifest_sha256"], "source_project": source_project,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(target)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return _fail(f"作業コピーの作成に失敗しました: {type(exc).__name__}", workcopy=name)
    return {"ok": True, "idempotent": False, "workcopy": name, "base_ref": wanted,
            "base_commit": base_commit, "proposal_id": proposal_id, "copied_files": copied}


def build_task(pkg: dict) -> str:
    """作業コピーで動かすジェンキンス用の依頼文(REQUEST.md に、固定の作業規則を添える)。"""
    forbidden = "\n".join(f"- {x}" for x in pkg.get("forbidden", []))
    tests = "\n".join(f"- {x}" for x in pkg.get("acceptance_tests", []))
    return (
        "以下は、セバスが作成した開発指示です。この作業コピーだけで実装してください。\n"
        "調査用の使い捨てスクリプトは作らず、完了前に一時ファイルを全削除してください。\n"
        "エラー・不明・読取不能を『成功』『適合』『通過』扱いにしないでください(fail-closed)。\n"
        "既存テストを緩めない・期待値を書き換えて通さないでください。実案件・本番には触れないでください。\n\n"
        f"## 変更禁止境界\n{forbidden}\n\n## 受入テスト\n{tests}\n\n"
        f"## 依頼文(REQUEST.md)\n{pkg.get('request_md', '')}\n"
    )


# ---------------------------------------------------------------- 4. 納品データ

def canonical_artifact_bytes(diff_text: str, artifact_text: str = "", files: dict | None = None) -> bytes:
    """セバスの `_canonical_delivery_content` と同じ正規形(提出内容だけから作る)。"""
    material = {"diff_text": str(diff_text or "")[:MAX_DIFF_CHARS],
                "artifact_text": str(artifact_text or "")[:MAX_DIFF_CHARS],
                "files": {}}
    for key in sorted((files or {}).keys())[:50]:
        material["files"][str(key)[:120]] = str((files or {})[key])[:200_000]
    return (json.dumps(material, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


_PYTEST_SUMMARY = re.compile(r"(?:(\d+) failed)?[ ,]*(?:(\d+) passed)?[ ,]*(?:(\d+) skipped)?[ ,]*(?:(\d+) error)?")


def parse_pytest_summary(output: str) -> dict:
    """pytest -q の最終行から件数を読む。読めなければ total=0(納品側で needs_evidence になる)。"""
    last = ""
    for line in reversed(str(output or "").splitlines()):
        if re.search(r"\b(passed|failed|error)\b", line) and " in " in line:
            last = line
            break
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for number, word in re.findall(r"(\d+) (passed|failed|skipped|errors?)", last):
        counts["error" if word.startswith("error") else word] += int(number)
    total = counts["passed"] + counts["failed"] + counts["error"]
    return {"total": total, "passed_count": counts["passed"],
            "failed": counts["failed"] + counts["error"], "skipped": counts["skipped"],
            "summary_line": last[:200]}


def run_tests(workcopy_dir: Path, test_args: list[str], seed: str = "0") -> dict:
    py = workcopy_dir / ".venv" / "bin" / "python"
    exe = str(py) if py.exists() else "python"
    cmd = [exe, "-m", "pytest", "-q", "-p", "no:cacheprovider", *test_args]
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    try:
        proc = subprocess.run(cmd, cwd=str(workcopy_dir), capture_output=True, text=True,
                              timeout=TEST_TIMEOUT_SEC, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"total": 0, "failed": 0, "error": f"テストを実行できません: {type(exc).__name__}",
                "command": " ".join(["pytest", "-q", *test_args])}
    parsed = parse_pytest_summary(proc.stdout + "\n" + proc.stderr)
    parsed["command"] = " ".join(["pytest", "-q", *test_args])
    parsed["returncode"] = proc.returncode
    parsed["passed"] = bool(proc.returncode == 0 and parsed["total"] > 0 and parsed["failed"] == 0)
    if not parsed["passed"]:
        parsed["failure_tail"] = (proc.stdout + proc.stderr)[-1500:]
    return parsed


# --- 納品ゲート (D-3) ---------------------------------------------------------
# 納品の前に必ず通す: (a) fail-open検査を差分に適用 (b) 全体テスト (c) ハッシュ順序を
# 変えた再実行 (d) 変更禁止領域・差分の大きさ・テストの有無。1つでも通らない・確認できない
# なら納品しない (不明は不合格)。
GATE_RERUN_SEEDS = ("3", "8")
MAX_CHANGED_FILES = 80


def _gate_check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": str(detail)[:400]}


def _gate_lint(work: Path, changed: list[str]) -> dict:
    """作業コピー自身の scripts/fail_open_lint.py を差分に適用する。無い・読めない=不合格。"""
    script = work / "scripts" / "fail_open_lint.py"
    if not script.is_file():
        return _gate_check("fail_open_lint", False, "検査スクリプトが作業コピーにありません(確認できず)")
    py = work / ".venv" / "bin" / "python"
    exe = str(py) if py.exists() else "python"
    targets = [n for n in changed if n.endswith((".py", ".js"))]
    if not targets:
        return _gate_check("fail_open_lint", True, "検査対象(.py/.js)の変更なし")
    try:
        proc = subprocess.run([exe, "scripts/fail_open_lint.py", "--json", "--changed", *targets],
                              cwd=str(work), capture_output=True, text=True, timeout=300)
        found = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return _gate_check("fail_open_lint", False, f"検査を実行できません: {type(exc).__name__}")
    if proc.returncode != 0 or not isinstance(found, list):
        return _gate_check("fail_open_lint", False, "検査の結果を読めません(確認できず)")
    if found:
        sample = "; ".join(f"{x.get('file')}:{x.get('function')}[{x.get('type')}]" for x in found[:5])
        return _gate_check("fail_open_lint", False, f"新しいfail-openの検出が{len(found)}件: {sample}")
    return _gate_check("fail_open_lint", True, "新しい検出なし")


def run_delivery_gate(work: Path, changed: list[str], *, run_tests_fn=None) -> dict:
    """納品ゲートを実行し {passed, checks, tests} を返す。run_tests_fn はテスト用の差し替え。"""
    runner = run_tests_fn or run_tests
    checks: list[dict] = []
    checks.append(_gate_check(
        "changed_files_count", len(changed) <= MAX_CHANGED_FILES,
        f"変更ファイル{len(changed)}件(上限{MAX_CHANGED_FILES})"))
    code_changed = [n for n in changed if n.startswith("app/") and n.endswith(".py")]
    tests_changed = [n for n in changed if n.startswith("tests/")]
    checks.append(_gate_check(
        "tests_accompany_code", not code_changed or bool(tests_changed),
        "コードの変更にテストの追加・更新が伴っています" if (not code_changed or tests_changed)
        else f"app/ のコード{len(code_changed)}件の変更に、tests/ の変更がありません"))
    checks.append(_gate_lint(work, changed))
    first = runner(work, [], seed="0")
    first_ok = bool(first.get("passed")) and int(first.get("total") or 0) > 0         and int(first.get("failed") or 0) == 0
    checks.append(_gate_check(
        "full_tests", first_ok,
        f"全体テスト {first.get('summary_line') or first.get('error') or '結果を読めません'}"))
    reruns_ok = first_ok
    for seed in GATE_RERUN_SEEDS:
        if not first_ok:
            break
        again = runner(work, [], seed=seed)
        ok = bool(again.get("passed")) and int(again.get("total") or 0) == int(first.get("total") or 0)             and int(again.get("failed") or 0) == 0
        reruns_ok = reruns_ok and ok
        checks.append(_gate_check(
            f"rerun_hashseed_{seed}", ok,
            f"PYTHONHASHSEED={seed}: {again.get('summary_line') or again.get('error') or '結果を読めません'}"))
    if not first_ok:
        checks.append(_gate_check("rerun_hashseed", False, "全体テストが通らないため再実行しません"))
    passed = all(c["passed"] for c in checks) and reruns_ok
    return {"passed": bool(passed), "checks": checks, "tests": first}


def build_delivery(workspace_root: str | Path, proposal_id: str, *, test_args: list[str] | None = None,
                   run: bool = True, gate: bool = True) -> dict:
    """作業コピーの差分とテスト結果から、セバスの record_delivery 用の delivery を作る。

    base_commit にはセバスが比較する基準版(base_ref)を入れる。result_commit は作業コピーの
    git の最終コミット。禁止領域(.env・docker-compose*.yml・docker/proxy/)への変更があれば拒否する。
    """
    pkg = load_package(workspace_root, proposal_id)
    if not pkg.get("ok"):
        return pkg
    projects = Path(workspace_root) / "projects"
    candidates = [p for p in projects.glob(f"*__{proposal_id[:12]}") if p.is_dir()]
    if len(candidates) != 1:
        return _fail("作業コピーが見つからない、または複数あります", proposal_id=proposal_id)
    work = candidates[0]
    try:
        info = json.loads((work / ".sebas_intake.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fail("作業コピーの記録を読めません", workcopy=work.name)
    if info.get("proposal_id") != proposal_id or info.get("base_ref") != pkg["base_ref"]:
        return _fail("作業コピーの記録が指示と一致しません", workcopy=work.name)
    base_commit = str(info.get("base_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        return _fail("基準コミットが不正です", workcopy=work.name)
    add = _git(work, "add", "-A")
    if add.returncode != 0:
        return _fail("差分を取得できません(git add)", workcopy=work.name)
    names = _git(work, "diff", "--cached", "--name-only", base_commit)
    if names.returncode != 0:
        return _fail("差分を取得できません(git diff)", workcopy=work.name)
    changed = [n for n in names.stdout.splitlines() if n.strip()]
    if not changed:
        return _fail("変更がありません(納品する差分がありません)", workcopy=work.name)
    banned = [n for n in changed if n == ".env" or n.startswith(".env.")
              or re.match(r"^docker-compose.*\.ya?ml$", n) or n.startswith("docker/proxy/")
              or n.startswith("data/")]
    if banned:
        return _fail("変更禁止境界の領域に変更があります(納品しません)", banned=banned[:10])
    diff = _git(work, "diff", "--cached", "--no-color", base_commit)
    if diff.returncode != 0:
        return _fail("差分を取得できません(git diff)", workcopy=work.name)
    diff_text = diff.stdout
    if len(diff_text) > MAX_DIFF_CHARS:
        return _fail("差分が大きすぎます。分割して納品してください", chars=len(diff_text))
    commit = _git(work, "commit", "-q", "-m", f"delivery for {proposal_id}")
    head = _git(work, "rev-parse", "HEAD")
    result_commit = head.stdout.strip() if head.returncode == 0 else ""
    if commit.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", result_commit):
        return _fail("成果コミットを作成できません", workcopy=work.name)
    artifact_sha256 = _sha256_bytes(canonical_artifact_bytes(diff_text))
    gate_report = None
    if gate and run:
        # D-3: 納品ゲート。通らなければ納品データを作らない(不明は不合格)。
        gate_report = run_delivery_gate(work, changed)
        tests = gate_report["tests"]
        if not gate_report["passed"]:
            return _fail("納品ゲートを通りません(納品しません)", workcopy=work.name,
                         gate=gate_report["checks"], changed_files=changed[:200])
    elif run:
        tests = run_tests(work, list(test_args or []))
    else:
        tests = {"total": 0, "failed": 0, "note": "テスト未実行(run=false)"}
    delivery = {
        "proposal_id": proposal_id,
        "base_commit": pkg["base_ref"],
        "result_commit": result_commit,
        "diff_text": diff_text,
        "artifact_sha256": artifact_sha256,
        "test_results": tests,
        "capability_ids": [pkg["required_capability_id"]] if pkg.get("required_capability_id") else [],
        "failure_logs": str(tests.get("failure_tail") or "")[:1500],
    }
    return {"ok": True, "delivery": delivery, "workcopy": work.name, "changed_files": changed[:200],
            "artifact_sha256": artifact_sha256,
            "gate": (gate_report["checks"] if gate_report else None),
            "tests_passed": bool(tests.get("passed"))}
