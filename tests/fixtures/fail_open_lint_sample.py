"""A-3 fail-open 静的検査 (検査専用・製品の実行時には読み込まれない)。

目的: 不明・失敗・読取不能を「成功」「通過」扱いにする書き方 (fail-open) を
AST (Python) / 正規表現 (JS) で検出し、新しい違反が入るとテストで落とす。
製品コード (`app/`) からは import しないこと。`tests/` からだけ使う。

検出の型:
  P1 握りつぶし: 広い except (裸 / Exception / BaseException) の本体が
      pass / continue / 定数だけの return (None/False/0/""/[]/{}/()) のみ。
      contextlib.suppress(Exception/BaseException) も同様。
  P2 失敗を肯定で返す: 広い except の本体が return True / 肯定的な既定値
      ("ok" 等の文字列定数 / {"ok": True} 系の dict)。
  P3 既定値での欠落の補完: `x or True` / `or 1` (BoolOp Or の右辺が肯定の定数)。
      `.get(k, 0)` は対象外 (誤検出が多いため。報告書に理由を記載)。
  P4(JS): app/static/*.js の .catch(()=>({})) / .catch(()=>[]) /
      空の catch / 本体が return だけの catch (正規表現)。

識別子は「ファイル・外側の関数名・型・正規化テキストのハッシュ」で作り、
行番号に依存しない。PYTHONHASHSEED が変わっても安定 (sort 済み・hash()不使用)。

D-3 再利用: lint_changed_files(paths) は変更行を含む関数だけ検査し、
ベースラインに無い新規違反だけ返す (ベースライン照合つき)。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
STATIC_DIR = APP_DIR / "static"
BASELINE_PATH = ROOT / "tests" / "fail_open_baseline.json"

# --- 判定基準はコードの1か所に置く -------------------------------------------
# 安全の中核モジュール (承認・完了ゲート・外部送信・削除・RAG登録・OCR・
# 公開・事前照合・自走・停止分類・証拠・漏斗を扱うファイル)。
CORE_MODULE_FILES: dict[str, str] = {
    # 承認
    "app/goal_review.py": "承認・外部評価",
    "app/goal_review_queue.py": "承認・外部評価",
    "app/review_queue.py": "承認・外部評価",
    "app/plan_review_loop.py": "承認・外部評価",
    "app/project_manager.py": "承認・適用",
    # 完了ゲート
    "app/completion_gate.py": "完了ゲート",
    "app/completion_replay.py": "完了ゲート",
    "app/goal_completion_store.py": "完了ゲート",
    "app/goal_completion_flag.py": "完了ゲート",
    "app/goal_completion_hooks.py": "完了ゲート",
    # 外部送信
    "app/external_ai.py": "外部送信",
    "app/goal_contract.py": "外部送信・契約",
    # 削除
    "app/project_delete.py": "削除",
    # RAG登録
    "app/experience_memory.py": "RAG登録",
    "app/experience_store.py": "RAG登録",
    "app/experience_cli.py": "RAG登録",
    # OCR
    "app/ocr_runtime.py": "OCR",
    "app/ocr_review.py": "OCR",
    "app/ocr_artifacts.py": "OCR",
    "app/ocr_client.py": "OCR",
    "app/ocr_fallback.py": "OCR",
    "app/ocr_store.py": "OCR",
    "app/ocr_schema.py": "OCR",
    "app/ocr_usami_adapter.py": "OCR",
    "app/ocr_invoice_adapter.py": "OCR",
    # 公開
    "app/landing_page.py": "公開",
    "app/social_premarketing.py": "公開",
    "app/premarketing.py": "公開",
    "app/premarketing_monitor.py": "公開",
    "app/google_premarketing.py": "公開",
    "app/public_web_research.py": "公開",
    "app/canva_connect.py": "公開",
    # 事前照合
    "app/capability_gap.py": "事前照合",
    "app/capability_delivery.py": "事前照合",
    "app/capability_export.py": "事前照合",
    "app/capability_registry.py": "事前照合",
    # 自走
    "app/autopilot.py": "自走",
    "app/safe_auto_resume.py": "自走",
    # 停止分類
    "app/stop_classifier.py": "停止分類",
    "app/workflow_readiness.py": "停止分類",
    "app/resolution_coordinator.py": "停止分類",
    # 証拠
    "app/evidence_graph.py": "証拠",
    "app/claim_evidence.py": "証拠",
    "app/campaign_evidence.py": "証拠",
    # 漏斗
    "app/execution_funnel.py": "漏斗",
}

WIDE_NAMES = frozenset({"Exception", "BaseException"})

# P2/P3 の「肯定的な定数」の判定表 (1か所)。P2 仕様の例示
# ("ok","active","passed","conforms","allowed") を含む。
AFFIRMATIVE_STRINGS = frozenset({
    "ok", "active", "passed", "pass", "conforms", "allowed", "allow",
    "success", "successful", "valid", "approved", "completed", "complete",
    "achieved", "healthy", "ready", "available", "enabled",
})

# 検査対象 (製品の実行コード)。tests / scripts(検査自体) / .bak は含めない。
TARGET_PY_GLOB = "app/**/*.py"
TARGET_JS_GLOB = "app/static/*.js"


def is_core_module(rel_posix: str) -> bool:
    return rel_posix in CORE_MODULE_FILES


def default_classification(rel_posix: str) -> str:
    return "high_risk" if is_core_module(rel_posix) else "review"


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


def _hash12(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:12]


def make_id(rel: str, func: str, typ: str, snippet_norm: str) -> str:
    return f"{rel}:{func}:{typ}:{_hash12(snippet_norm)}"


# --- Python AST 判定 ----------------------------------------------------------

def _is_wide_handler(handler: ast.ExceptHandler) -> bool:
    """裸の except / Exception / BaseException (タプル内も含む) だけ広い扱い。

    ValueError 等の狭い例外は対象外。KeyboardInterrupt 等の派生名も対象外
    (仕様どおり Exception / BaseException の名前に限定)。
    """
    typ = handler.type
    if typ is None:
        return True
    if isinstance(typ, ast.Name):
        return typ.id in WIDE_NAMES
    if isinstance(typ, ast.Tuple):
        for elt in typ.elts:
            if isinstance(elt, ast.Name) and elt.id in WIDE_NAMES:
                return True
        return False
    return False


def _is_empty_const(value: ast.expr | None) -> bool:
    """P1 の「定数だけの return」の値か。True / 肯定文字は含めない (P2 側)。"""
    if value is None:
        return True
    if isinstance(value, ast.Constant):
        v = value.value
        if v is None or v is False:
            return True
        if type(v) is int and v == 0:
            return True
        if type(v) is float and v == 0.0:
            return True
        if v == "":
            return True
        if isinstance(v, bytes) and v == b"":
            return True
        return False
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return len(value.elts) == 0
    if isinstance(value, ast.Dict):
        return len(value.keys) == 0
    return False


def _handler_is_swallow(handler: ast.ExceptHandler) -> bool:
    """本体が pass / continue / 定数 return (docstring は無視) のみか。"""
    body = list(handler.body or [])
    if not body:
        return False
    seen = False
    for stmt in body:
        if isinstance(stmt, (ast.Pass, ast.Continue)):
            seen = True
            continue
        if isinstance(stmt, ast.Return) and _is_empty_const(stmt.value):
            seen = True
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                and isinstance(stmt.value.value, str):
            continue  # docstring は無視
        return False
    return seen


def _is_affirmative_return(ret: ast.Return) -> bool:
    """P2: return True / 肯定文字列 / {"ok": True} 系の dict か。"""
    val = ret.value
    if val is None:
        return False
    if isinstance(val, ast.Constant):
        if val.value is True:
            return True
        if isinstance(val.value, str) and val.value.strip().lower() \
                in AFFIRMATIVE_STRINGS:
            return True
        return False
    if isinstance(val, ast.Dict):
        for k, v in zip(val.keys, val.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                if k.value.strip().lower() in AFFIRMATIVE_STRINGS:
                    if isinstance(v, ast.Constant) and v.value is True:
                        return True
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                if v.value.strip().lower() in AFFIRMATIVE_STRINGS:
                    return True
        return False
    return False


def _iter_returns_no_nested(stmts: list[ast.stmt]):
    """handler 本体配下の return を列挙 (nested def/class には入らない)。"""
    stack = list(stmts)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Return):
            yield node
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt) or isinstance(child, ast.expr):
                stack.append(child)


def _is_affirmative_or_value(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        if node.value is True:
            return True
        if type(node.value) is int and node.value == 1:
            return True
        if isinstance(node.value, str) and node.value.strip().lower() \
                in AFFIRMATIVE_STRINGS:
            return True
    return False


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        self.source = source
        self.func_stack: list[str] = []
        self.findings: list[dict] = []
        self.func_ranges: list[dict] = []

    def _func(self) -> str:
        return self.func_stack[-1] if self.func_stack else "<module>"

    def _push(self, rel: str, func: str, typ: str, node: ast.AST) -> None:
        try:
            text = ast.unparse(node)
        except Exception:
            text = type(node).__name__
        norm = _norm(text)
        lineno = int(getattr(node, "lineno", 0) or 0)
        fid = make_id(rel, func, typ, norm)
        self.findings.append({
            "id": fid, "file": rel, "function": func, "type": typ,
            "snippet": norm[:200], "_lineno": lineno,
        })

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.func_stack.append(node.name)
        self.func_ranges.append({
            "name": node.name,
            "start": int(node.lineno or 0),
            "end": int(getattr(node, "end_lineno", node.lineno) or 0),
        })
        self.generic_visit(node)
        self.func_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.func_stack.append(node.name)
        self.func_ranges.append({
            "name": node.name,
            "start": int(node.lineno or 0),
            "end": int(getattr(node, "end_lineno", node.lineno) or 0),
        })
        self.generic_visit(node)
        self.func_stack.pop()

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        func = self._func()
        if _is_wide_handler(node):
            if _handler_is_swallow(node):
                self._push(self.rel, func, "P1", node)
            else:
                for ret in _iter_returns_no_nested(list(node.body or [])):
                    if _is_affirmative_return(ret):
                        self._push(self.rel, func, "P2", ret)
                        break
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        func = self._func()
        for item in node.items:
            expr = item.optional_vars  # noqa: F841 (unused, for clarity)
            call = item.context_expr
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            is_suppress = (
                (isinstance(fn, ast.Name) and fn.id == "suppress")
                or (isinstance(fn, ast.Attribute) and fn.attr == "suppress")
            )
            if not is_suppress:
                continue
            wide = False
            for arg in list(call.args or []):
                if isinstance(arg, ast.Name) and arg.id in WIDE_NAMES:
                    wide = True
            for kw in list(call.keywords or []):
                v = kw.value
                if isinstance(v, ast.Name) and v.id in WIDE_NAMES:
                    wide = True
            if wide:
                self._push(self.rel, func, "P1", node)
                break
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        if isinstance(node.op, ast.Or):
            vals = list(node.values or [])
            if len(vals) >= 2 and any(
                    _is_affirmative_or_value(v) for v in vals[1:]):
                self._push(self.rel, self._func(), "P3", node)
        self.generic_visit(node)


def lint_python_text(rel: str, text: str) -> tuple[list[dict], list[dict]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [], []
    vis = _PyVisitor(rel, text)
    vis.visit(tree)
    return vis.findings, vis.func_ranges


# --- JS (正規表現) --------------------------------------------------------------
# P4 の対象は仕様どおり空のフォールバック/空catch/returnだけのcatch に絞る。
# Epoch ガード付きの catch (if(ticket!==epoch)return 等)・メッセージ表示付きは
# 意図的な表示処理であり対象外 (誤検出を避け、ベースラインを正直に保つ)。
_JS_CATCH_ARROW = re.compile(
    r"\.catch\s*\(\s*\(?[^)]*\)?\s*=>\s*"
    r"(\(\s*\{\s*\}\s*\)|\(\s*\[\s*\]\s*\)|\[\s*\]|\{\s*\})")
_JS_EMPTY_CATCH = re.compile(
    r"\bcatch\s*\(\s*(?:_err|_ignored|_)\s*\)\s*\{\s*\}"
    r"|\bcatch\s*\(\s*e\s*\)\s*\{\s*\}"
    r"|\bcatch\s*\(\s*function\s*\(\s*\)\s*\{\s*\}"
    r"|\bcatch\s*\([^)]*\)\s*\{\s*\}")
_JS_CATCH_BLOCK = re.compile(r"\bcatch\s*\([^)]*\)\s*\{([^{}]*)\}")
_JS_FUNC = re.compile(r"function\s+([A-Za-z_$][\w$]*)")


def _js_func_before(text: str, pos: int) -> str:
    seg = text[:pos]
    ms = list(_JS_FUNC.finditer(seg))
    if not ms:
        return "(anonymous)"
    return ms[-1].group(1)


def _js_is_guarded(body: str) -> bool:
    """Epoch/選択のガード・メッセージ表示がある catch は対象外。

    注意: `return value` / `return String(value)` だけの catch は表示用で
    あっても P4 の検出対象にする (仕様の「catch の本体が return だけ」)。
    棚卸し側で allow/review を判断する。ここで除外するのは非同期の世代
    ガード・状態表示など「失敗を握りつぶしていない」ことが文面で分かる
    ものだけに絞る。
    """
    low = body.lower()
    markers = (
        "epoch", "ticket", "iscurrent", "selected()", "getepoch()",
        "textcontent", "message", "status", "render", "replacechildren",
        "disabled",
    )
    return any(m in low for m in markers)


def lint_js_text(rel: str, text: str) -> list[dict]:
    out: list[dict] = []
    for m in _JS_CATCH_ARROW.finditer(text):
        func = _js_func_before(text, m.start())
        norm = _norm(m.group(0))
        out.append({
            "id": make_id(rel, func, "P4", norm),
            "file": rel, "function": func, "type": "P4",
            "snippet": norm[:200], "_lineno": text.count("\n", 0, m.start()) + 1,
        })
    for m in _JS_EMPTY_CATCH.finditer(text):
        body = m.group(0)
        # ガード・表示付きの大きい catch は _JS_CATCH_BLOCK 側で除外するため、
        # ここでは真に空のものだけ残す (念のため長さで絞る)。
        if len(_norm(body)) > 60:
            continue
        func = _js_func_before(text, m.start())
        norm = _norm(m.group(0))
        fid = make_id(rel, func, "P4", norm)
        if not any(f["id"] == fid for f in out):
            out.append({
                "id": fid, "file": rel, "function": func, "type": "P4",
                "snippet": norm[:200],
                "_lineno": text.count("\n", 0, m.start()) + 1,
            })
    for m in _JS_CATCH_BLOCK.finditer(text):
        body = m.group(1) or ""
        if body.strip() == "":
            continue
        if _js_is_guarded(body):
            continue
        if "return" in body and "throw" not in body:
            func = _js_func_before(text, m.start())
            norm = _norm(m.group(0))
            fid = make_id(rel, func, "P4", norm)
            if not any(f["id"] == fid for f in out):
                out.append({
                    "id": fid, "file": rel, "function": func, "type": "P4",
                    "snippet": norm[:200],
                    "_lineno": text.count("\n", 0, m.start()) + 1,
                })
    return out


# --- 全体検査 -------------------------------------------------------------------

def _iter_target_files(root: Path) -> tuple[list[Path], list[Path]]:
    py = sorted((root).glob(TARGET_PY_GLOB))
    js = sorted((root).glob(TARGET_JS_GLOB))
    py = [p for p in py if "__pycache__" not in p.parts
          and not p.name.endswith((".bak_20261001_chromadb",
                                   ".bak_20261001_dependencies",
                                   ".bak_20261001_pytest9"))]
    # .bak_ で終わる退避ファイルは対象外。
    py = [p for p in py if ".bak_" not in p.name]
    return py, js


def lint_project(root: Path | str = ROOT) -> list[dict]:
    root = Path(root)
    findings: list[dict] = []
    py_files, js_files = _iter_target_files(root)
    for path in py_files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        items, _ranges = lint_python_text(rel, text)
        findings.extend(items)
    for path in js_files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        findings.extend(lint_js_text(rel, text))
    # 重複 id の安定化 (同関数・同型・同文が複数ある場合だけ #n を付ける)。
    by_id: dict[str, list[dict]] = {}
    for f in findings:
        by_id.setdefault(f["id"], []).append(f)
    stable: list[dict] = []
    for fid in sorted(by_id):
        group = sorted(by_id[fid], key=lambda x: int(x.pop("_lineno", 0) or 0))
        if len(group) == 1:
            item = dict(group[0])
            item.pop("_lineno", None)
            stable.append(item)
        else:
            for n, item in enumerate(group, 1):
                c = dict(item)
                c.pop("_lineno", None)
                c["id"] = f"{fid}#{n}"
                stable.append(c)
    stable.sort(key=lambda x: (x["file"], x["function"], x["type"], x["id"]))
    return stable


def load_baseline(path: Path | str = BASELINE_PATH) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("findings"), list):
        items = raw["findings"]
        meta = {k: v for k, v in raw.items() if k != "findings"}
    elif isinstance(raw, list):
        items = raw
        meta = {}
    else:
        raise ValueError("baseline format error")
    by_id = {str(x.get("id")): dict(x) for x in items if isinstance(x, dict)}
    return {"items": items, "by_id": by_id, "meta": meta}


def check_against_baseline(current: list[dict],
                           baseline_by_id: dict) -> tuple[list[dict], list[dict]]:
    cur_ids = {c["id"] for c in current}
    base_ids = set(baseline_by_id)
    new_items = [c for c in current if c["id"] not in base_ids]
    stale_ids = sorted(base_ids - cur_ids)
    stale_items = [baseline_by_id[i] for i in stale_ids]
    return new_items, stale_items


# --- D-3 再利用: 差分だけの検査 ---------------------------------------------------

def _changed_lines(root: Path, paths: list[str]) -> dict[str, list[tuple[int, int]]]:
    """git diff (HEAD に対する作業コピー) から新ファイル側の変更行範囲を得る。"""
    rels = []
    for p in paths or []:
        pp = Path(p)
        try:
            rel = pp.relative_to(root).as_posix() if pp.is_absolute() \
                else Path(p).as_posix()
        except ValueError:
            rel = Path(p).as_posix()
        rels.append(rel)
    if not rels:
        return {}
    try:
        proc = subprocess.run(
            ["git", "diff", "--unified=0", "HEAD", "--", *rels],
            cwd=str(root), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    out: dict[str, list[tuple[int, int]]] = {}
    cur = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("+++ b/"):
            cur = line[len("+++ b/"):].strip()
            continue
        m = re.match(r"^@@ .* \+(\d+)(?:,(\d+))? @@", line)
        if m and cur:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count > 0:
                out.setdefault(cur, []).append((start, start + count - 1))
    return out


def lint_changed_files(paths: list[str], root: Path | str = ROOT,
                       baseline_path: Path | str = BASELINE_PATH) -> list[dict]:
    """差分(変更行を含む関数)だけ検査し、ベースラインに無い新規違反を返す。

    D-3 (納品ゲート) で再利用する。git diff が読めない/変更が無い場合は
    指定パスの全体検査にフォールバックする。戻り値は current と同じ形。
    """
    root = Path(root)
    rels = []
    for p in paths or []:
        pp = Path(p)
        rel = pp.relative_to(root).as_posix() if pp.is_absolute() else Path(p).as_posix()
        rels.append(rel)
    rels = sorted(set(rels))
    changed = _changed_lines(root, rels)
    current = lint_project(root)
    by_file: dict[str, list[dict]] = {}
    for f in current:
        by_file.setdefault(f["file"], []).append(f)
    if not changed:
        scoped = [f for f in current if f["file"] in set(rels)]
    else:
        scoped = []
        for rel in rels:
            items = by_file.get(rel, [])
            if rel not in changed:
                scoped.extend(items)
                continue
            # 変更行を含む関数だけに絞る (関数範囲は AST から再取得)。
            ranges: list[dict] = []
            src = root / rel
            if src.suffix == ".py" and src.exists():
                try:
                    _, ranges = lint_python_text(
                        rel, src.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    ranges = []
            spans = changed.get(rel, [])
            if not ranges:
                scoped.extend(items)
                continue
            hit_funcs = set()
            for r in ranges:
                for s, e in spans:
                    if not (e < int(r["start"]) or s > int(r["end"])):
                        hit_funcs.add(r["name"])
            if not hit_funcs:
                continue
            scoped.extend([x for x in items if x["function"] in hit_funcs])
    try:
        base = load_baseline(baseline_path)["by_id"]
    except (OSError, ValueError):
        base = {}
    new_items, _ = check_against_baseline(scoped, base)
    new_items.sort(key=lambda x: (x["file"], x["function"], x["type"], x["id"]))
    return new_items


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="fail-open lint (A-3)")
    ap.add_argument("--json", action="store_true",
                    help="検出を JSON で標準出力する")
    ap.add_argument("--changed", nargs="*", default=None,
                    help="差分検査するパスの一覧")
    args = ap.parse_args()
    if args.changed is not None:
        items = lint_changed_files(args.changed)
    else:
        items = lint_project(ROOT)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"findings: {len(items)}")
        for f in items:
            print(f"{f['id']} [{f['type']}] {f['snippet'][:120]}")
