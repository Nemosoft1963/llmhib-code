"""プロジェクト構造の読み取り専用解析(標準ライブラリのみ)。

改修の前に「構造の地図」と「変更の影響範囲」を正確に把握するための機能。
このモジュールは対象ファイルを変更しない(読み取り専用)。外部依存なし
(`ast` / `pathlib` / `re` / `json` 等の標準ライブラリだけを使う)。

公開関数:
- build_index(root): プロジェクト走査 → 決定的なdict
- impact(index, target): 変更の影響範囲
- route_js_map(index): 画面JSとサーバルートの突合

設計原則:
- 「不明」を値に混ぜない。解析できなかったものは `parse_errors` /
  `unresolved_imports` / `js_unresolved` に分け、0件・空・影響なしと区別する。
- 決定的: 全リストは固定キーでソートする(setの反復順序に依存しない)。
- 広い `except` の握りつぶしをしない。読めなかったファイルは必ず
  `parse_errors` に記録し、結果全体に `complete: false` を付ける。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# 走査から除外するディレクトリ名(どの階層に現れても除外する)
EXCLUDED_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", "data", "backups", "temp"}
)

# このサイズを超えるファイルは走査対象外(巨大ファイル除外)。parse_errors には
# 入れず `skipped_large_files` に記録する(読めなかったのではなく、仕様として
# 除外したため。不明と黙殺を区別するために一覧は残す)。completeにも影響しない。
MAX_FILE_BYTES = 1024 * 1024

# FastAPI等でルートを表すデコレータのメソッド名(小文字)
ROUTE_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head", "trace"})
# methods キーワードで複数メソッドを取るデコレータ
ROUTE_MULTI_METHODS = frozenset({"route", "api_route", "add_api_route"})

# JSのAPI呼び出し検出: fetch(...) / api(...) / request(...) の呼び出し開始位置
_JS_CALL_START_RE = re.compile(r"(?:\bfetch\s*\(|\bapi\s*\(|\brequest\s*\()")
# 同一ファイル内の1行ラッパー定義:
#   const url = path => '...' + ... + path;
#   function url(path) { return '...' + ... + path; }
_JS_ARROW_WRAPPER_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"\s*=\s*(?:\(\s*([A-Za-z_$][\w$]*)\s*\)|([A-Za-z_$][\w$]*))"
    r"\s*=>\s*(.*?)\s*;?\s*$"
)
_JS_FUNCTION_WRAPPER_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"\s*\(\s*([A-Za-z_$][\w$]*)\s*\)\s*\{(.*)\}\s*;?\s*$"
)
_JS_RETURN_ONLY_RE = re.compile(r"^\s*return\s+(.+?)\s*;?\s*$")
# テンプレート ${...} → {} 正規化
_TEMPLATE_EXPR_RE = re.compile(r"\$\{[^}]*\}")
# パスパラメータ正規化用
_BRACE_PARAM_RE = re.compile(r"\{[^}]*\}")
_ANGLE_PARAM_RE = re.compile(r"<[^>]*>")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def normalize_path(path: str) -> str:
    """パスのパラメータ表記を `{}` に正規化して比較可能にする。

    `{id}` / `:id` / `<id>` / `<int:id>` / `${id}` を全て `{}` に揃える。
    """
    out = _TEMPLATE_EXPR_RE.sub("{}", path)
    out = _BRACE_PARAM_RE.sub("{}", out)
    out = _ANGLE_PARAM_RE.sub("{}", out)
    # :// (スキーム区切り)を壊さないよう、/ の後に現れる :name だけを置換する
    out = re.sub(r"(?<=/):[A-Za-z_]\w*", "{}", out)
    return out


def _strip_query(path: str) -> str:
    """クエリ文字列(`?` 以降)を取り除く。`?` が無ければそのまま。"""
    cut = path.find("?")
    return path[:cut] if cut >= 0 else path


def normalize_js_path(path: str) -> str:
    """JS側パスの突合用正規化: クエリ除去 → パラメータの `{}` 化。"""
    return normalize_path(_strip_query(path))


def _is_path_complete(normalized: str) -> bool:
    """突合に使える完全なパスか。

    部分パス(末尾セグメントが `{}` で終わる・`{}` が直結する・`/api/` で
    始まらない断片)は False。`{}` が途中にあっても末尾がリテラルなら完全。
    """
    if not normalized.startswith("/"):
        return False
    if not normalized.startswith("/api/") and normalized != "/api":
        # /api/ で始まらない断片(例: /generations、/initialize)は部分パス
        return False
    if normalized.rstrip("/").endswith("{}"):
        return False
    last = normalized.rstrip("/").rsplit("/", 1)[-1]
    if last != "{}" and "{}" in last:
        # 末尾セグメントが {} 直結(例: /lifecycle{}、/ocr/{}{}) → 部分パス
        return False
    return True


def _partial_reason(normalized: str) -> str:
    if not normalized.startswith("/"):
        return "non-rooted path fragment"
    if not normalized.startswith("/api/") and normalized != "/api":
        return "path fragment not starting with /api/"
    if normalized.endswith("{}") or "{}{}" in normalized:
        return "trailing expression (path ends with a variable)"
    last = normalized.rstrip("/").rsplit("/", 1)[-1]
    if last != "{}" and "{}" in last:
        return "trailing expression (path ends with a variable)"
    return "partial path"


def _prefix_candidates(partial_normalized: str, server_keys: set[str],
                       limit: int = 10) -> list[str]:
    """部分パスに前方一致するサーバパスの候補(上位10件、決定的順序)。"""
    base = partial_normalized
    # 末尾の {} 群を取り除いた接頭辞で前方一致を探す
    while base.endswith("{}"):
        base = base[: -len("{}")]
    base = base.rstrip("/")
    cands = sorted(k for k in server_keys if k == base or k.startswith(base + "/"))
    return cands[:limit]


def _split_js_concat(expr: str) -> list[tuple[str, str]] | None:
    """JSの連結式をトークン列に分ける。単純でなければ None(推測しない)。

    戻り値: [("str", 値) / ("arg", 引数名参照) / ("expr", その他の式)]。
    `+` 以外の演算子・条件式・関数呼び出しの入れ子等があれば None。
    """
    parts = _split_top_level(expr, "+")
    if parts is None:
        return None
    tokens: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        if not part:
            return None
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "'\"":
            val, _, closed = _read_quoted(part + " ", 0, part[0])
            if not closed:
                return None
            tokens.append(("str", val))
        elif len(part) >= 2 and part[0] == "`" and part[-1] == "`":
            inner = part[1:-1]
            if "${" in inner and not re.fullmatch(r"\$\{[A-Za-z_$][\w$]*\}", inner):
                # テンプレート内の複雑な式は対象外
                return None
            tokens.append(("str", _TEMPLATE_EXPR_RE.sub("{}", inner)))
        elif re.fullmatch(r"[A-Za-z_$][\w$]*", part):
            tokens.append(("arg", part))
        else:
            # encodeURIComponent(x) のような単純な1引数呼び出しは式として許容する
            if re.fullmatch(r"[A-Za-z_$][\w$.]*\([A-Za-z_$][\w$.]*\)", part):
                tokens.append(("expr", part))
            else:
                return None
    return tokens


def _split_top_level(expr: str, sep: str) -> list[str] | None:
    """クォート・括弧を考慮して sep で分割する。閉じ忘れがあれば None。"""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    depth = 0
    quote: str | None = None
    while i < len(expr):
        ch = expr[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(expr):
                buf.append(expr[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in "([{":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch in ")]}":
            depth -= 1
            if depth < 0:
                return None
            buf.append(ch)
            i += 1
            continue
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if quote is not None or depth != 0:
        return None
    parts.append("".join(buf))
    return parts


def _parse_js_wrapper(line: str) -> tuple[str, str, str] | None:
    """1行ラッパー定義なら (名前, 引数名, 返却式) を返す。形が単純でなければ None。

    対象の形: `「文字列の接頭辞」+式+…+引数名` の連結だけ(分岐・複数文は対象外)。
    """
    m = _JS_ARROW_WRAPPER_RE.match(line)
    if m:
        name = m.group(1)
        arg = m.group(2) or m.group(3)
        body = m.group(4).strip()
        # アロー本体がブロック `{ return ...; }` の場合も許容する
        if body.startswith("{"):
            inner = body.strip()
            if inner.startswith("{") and inner.endswith("}"):
                inner = inner[1:-1].strip()
                rm = _JS_RETURN_ONLY_RE.match(inner)
                if rm is None:
                    return None
                body = rm.group(1).strip()
        return name, arg, body
    m = _JS_FUNCTION_WRAPPER_RE.match(line)
    if m:
        name, arg = m.group(1), m.group(2)
        body = m.group(3).strip()
        # 本体は `return 式;` 1文だけ
        rm = _JS_RETURN_ONLY_RE.match(body)
        if rm is None:
            return None
        return name, arg, rm.group(1).strip()
    return None


def _wrapper_prefix(body: str, arg: str) -> tuple[str | None, str | None]:
    """返却式から接頭辞を取り出す。(prefix, None) か (None, reason)。

    条件: 式が `「文字列の接頭辞」+式+…+引数名` の形で、最後のトークンが引数名
    そのもので終わること。接頭辞は `/` で始まる文字列でなければならない。
    """
    tokens = _split_js_concat(body)
    if tokens is None:
        return None, "complex expression"
    if not tokens or tokens[0][0] != "str" or not tokens[0][1].startswith("/"):
        return None, "no static prefix"
    if tokens[-1] != ("arg", arg):
        return None, "does not end with the argument"
    # 接頭辞は「文字列の接頭辞」+ 中間の単純な式({} 化)とする。
    # 例: '/api/projects/'+encodeURIComponent(pid)+path → '/api/projects/{}'
    parts: list[str] = []
    for kind, val in tokens[:-1]:
        if kind == "str":
            parts.append(val)
        elif kind == "expr":
            if re.fullmatch(r"[A-Za-z_$][\w$.]*\([A-Za-z_$][\w$.]*\)", val):
                parts.append("{}")
            else:
                return None, "complex expression"
        elif kind == "arg" and val != arg:
            return None, "multiple arguments"
    return "".join(parts), None


def _collect_js_wrappers(lines: list[str]) -> dict[str, str]:
    """ファイル内の1行ラッパー定義 → {名前: 接頭辞}。単純なものだけ。"""
    wrappers: dict[str, str] = {}
    for line in lines:
        parsed = _parse_js_wrapper(line)
        if parsed is None:
            continue
        name, arg, body = parsed
        prefix, _ = _wrapper_prefix(body, arg)
        if prefix is not None and name not in wrappers:
            wrappers[name] = prefix
    return wrappers


def _split_call_args(arg_text: str) -> list[str] | None:
    """呼び出し引数列をトップレベルで `,` 分割する。閉じ忘れは None。"""
    return _split_top_level(arg_text, ",")


def _module_name(rel: Path) -> str:
    """相対パス(foo/bar.py) → モジュール名(foo.bar)。"""
    parts = rel.with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return "__init__"
    return ".".join(parts)


def _is_pkg_module(rel: Path) -> bool:
    return rel.name == "__init__.py"


def _resolve_relative_import(
    cur_module: str, is_pkg: bool, level: int, name: str | None
) -> str | None:
    """相対importの解決。解決不能(トップレベルより上)なら None。"""
    if is_pkg:
        package_parts = cur_module.split(".") if cur_module else []
    else:
        package_parts = cur_module.split(".")[:-1] if cur_module else []
    # level=1 は現在のパッケージ、level=2 は1つ上、...
    up = level - 1
    if up > len(package_parts):
        return None
    base = package_parts[: len(package_parts) - up] if up else package_parts
    if name:
        return ".".join(base + [name]) if base else name
    return ".".join(base) if base else None


def _decorator_name(deco: ast.expr) -> str:
    """デコレータ式の表示名(@app.get('/x') → 'app.get')。引数は落とす。"""
    node: ast.expr = deco
    if isinstance(node, ast.Call):
        node = node.func
    while isinstance(node, ast.Call):  # デコレータ工場の多重呼び出し
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _decorator_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _static_str(node: ast.expr | None) -> str | None:
    """静的な文字列式なら値を返す。動的(変数・f-stringの埋め込み等)なら None。

    不明を値に混ぜないため、評価できないものは None として呼び出し側で
    unresolved 扱いにする。
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string: 埋め込みが無ければ静的
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                return None  # FormattedValue 等がある → 動的
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_str(node.left)
        right = _static_str(node.right)
        if left is not None and right is not None:
            return left + right
        return None
    return None


def _is_apirouter_call(func: ast.expr) -> bool:
    """APIRouter(...) の呼び出しか(修飾名 fastapi.APIRouter も可)。"""
    if isinstance(func, ast.Name):
        return func.id == "APIRouter"
    if isinstance(func, ast.Attribute):
        return func.attr == "APIRouter"
    return False


def _dotted_name(node: ast.expr) -> str | None:
    """Name / Attribute の連鎖を 'a.b.c' にする。それ以外は None。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


class _ModuleVisitor(ast.NodeVisitor):
    """1モジュール分の定義・import・呼び出し参照を集める。"""

    def __init__(self, module: str, internal_modules: frozenset[str]) -> None:
        self.module = module
        self.internal_modules = internal_modules
        self.is_pkg = False
        self.functions: list[dict] = []
        self.classes: list[dict] = []
        self.internal_imports: list[dict] = []
        self.external_imports: list[dict] = []
        self.unresolved_imports: list[dict] = []
        self.routes: list[dict] = []
        self.calls: list[dict] = []
        self.router_defs: list[dict] = []  # APIRouter(prefix=...) の定義
        self.includes: list[dict] = []  # include_router(...) の呼び出し
        self.import_aliases: list[dict] = []  # クロスファイル解決用
        self._func_stack: list[str] = []
        self.defined_names: dict[str, list[int]] = {}  # 名前 → 定義行(複数可)

    # -- import ---------------------------------------------------------
    def _is_known_internal(self, resolved: str) -> bool:
        """解決名が内部モジュール集合のいずれかと一致する、またはその配下/上位にある。"""
        return resolved in self.internal_modules or any(
            m == resolved or m.startswith(resolved + ".") or resolved.startswith(m + ".")
            for m in self.internal_modules
        )

    def _top_in_project(self, resolved: str) -> bool:
        """importのトップレベル名がプロジェクト内に存在する(配下に内部モジュールがある)。"""
        top = resolved.split(".")[0]
        return any(m == top or m.startswith(top + ".") for m in self.internal_modules)

    def _record_import(
        self, resolved: str | None, names: list[str], line: int, raw: str,
        relative: bool,
    ) -> None:
        if resolved is None:
            self.unresolved_imports.append(
                {"line": line, "raw": raw, "reason": "relative import beyond top level"}
            )
            return
        if not resolved:
            self.unresolved_imports.append(
                {"line": line, "raw": raw, "reason": "empty module name"}
            )
            return
        if self._is_known_internal(resolved):
            self.internal_imports.append(
                {"module": resolved, "names": sorted(names), "line": line, "raw": raw}
            )
            return
        if relative or self._top_in_project(resolved):
            # 相対importの解決先が無い、またはトップレベルは内部にあるのに
            # 対象が無い → 不明として unresolved に分け、external に混ぜない。
            self.unresolved_imports.append(
                {"line": line, "raw": raw,
                 "reason": f"module not found in project: {resolved}"}
            )
            return
        self.external_imports.append(
            {"name": resolved.split(".")[0], "raw": raw, "line": line})

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            raw = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
            self._record_import(alias.name, [alias.asname or alias.name.split(".")[0]],
                                node.lineno, raw, relative=False)
            # クロスファイル解決用: import a.b [as c] → c/a.b は a.b を指す
            local = alias.asname or alias.name.split(".")[0]
            full = alias.name if alias.asname else alias.name.split(".")[0]
            if alias.asname:
                full = alias.name
            self.import_aliases.append({"local": local, "target": full})
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = [a.asname or a.name for a in node.names]
        raw_names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "")
                              for a in node.names)
        raw_mod = node.module or ""
        if node.level and node.level > 0:
            resolved = _resolve_relative_import(
                self.module, self.is_pkg, node.level, node.module)
            raw = f"from {'.' * node.level}{raw_mod} import {raw_names}"
            if resolved is None:
                self.unresolved_imports.append(
                    {"line": node.lineno, "raw": raw,
                     "reason": "relative import beyond top level"})
                self.generic_visit(node)
                return
            if node.module is None:
                # from . import x → 解決したパッケージ + 各名前が1モジュール
                for alias in node.names:
                    full = f"{resolved}.{alias.name}" if resolved else alias.name
                    self._record_import(
                        full, [alias.asname or alias.name], node.lineno, raw,
                        relative=True)
                    self.import_aliases.append(
                        {"local": alias.asname or alias.name, "target": full})
                self.generic_visit(node)
                return
            # from .mod import x → 解決モジュール + 各名前(ローカル名→完全名)
            for alias in node.names:
                full = f"{resolved}.{alias.name}" if resolved else alias.name
                self.import_aliases.append(
                    {"local": alias.asname or alias.name, "target": full})
        else:
            resolved = node.module or ""
            raw = f"from {raw_mod} import {raw_names}"
            if not resolved:
                self.unresolved_imports.append(
                    {"line": node.lineno, "raw": raw, "reason": "empty module name"})
                self.generic_visit(node)
                return
            # from mod import x → ローカル名 x は mod.x を指す
            for alias in node.names:
                self.import_aliases.append(
                    {"local": alias.asname or alias.name,
                     "target": f"{resolved}.{alias.name}"})
        self._record_import(resolved, names, node.lineno, raw,
                            relative=bool(node.level and node.level > 0))
        self.generic_visit(node)

    # -- 定義 -----------------------------------------------------------
    def _def_entry(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> dict:
        return {
            "name": node.name,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", None) or node.lineno,
            "decorators": sorted(
                d for d in (_decorator_name(d) for d in node.decorator_list) if d
            ),
        }

    def _route_path(self, deco: ast.Call) -> tuple[str | None, bool]:
        """デコレータのパス引数を返す。(path, is_static)。

        静的な文字列式なら (値, True)。変数などで動的なら (None, False)。
        引数が無ければ (None, True)(=パス指定なし、未指定扱い)。
        """
        path_node: ast.expr | None = deco.args[0] if deco.args else None
        if path_node is None:
            for kw in deco.keywords:
                if kw.arg in ("path", "route", "url"):
                    path_node = kw.value
                    break
        if path_node is None:
            return None, True
        value = _static_str(path_node)
        if value is None:
            return None, False
        return value, True

    def _check_route(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            if not isinstance(func, ast.Attribute):
                continue
            attr = func.attr.lower()
            if attr not in ROUTE_METHODS and attr not in ROUTE_MULTI_METHODS:
                continue  # ルート以外のデコレータは対象外
            receiver = _dotted_name(func.value)
            path, is_static = self._route_path(deco)
            if path is None and is_static:
                continue  # パス指定なしのデコレータはルートにしない
            if not is_static:
                # パスが動的で静的に取れない → unresolved ルートとして記録する
                # (空文字にせず、不明と黙殺を区別する)
                self.routes.append(
                    {
                        "method": attr.upper() if attr in ROUTE_METHODS else "*",
                        "path": None,
                        "normalized_path": None,
                        "path_resolved": False,
                        "unresolved_reason": "dynamic route path",
                        "receiver": receiver,
                        "function": node.name,
                        "module": self.module,
                        "line": node.lineno,
                    }
                )
                continue
            assert path is not None
            if attr in ROUTE_METHODS:
                methods = [attr.upper()]
            elif attr in ROUTE_MULTI_METHODS:
                methods = []
                for kw in deco.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        for elt in kw.value.elts:
                            s = _static_str(elt)
                            if s is not None:
                                methods.append(s.upper())
                if not methods:
                    methods = ["*"]
            else:
                continue
            for method in sorted(set(methods)):
                self.routes.append(
                    {
                        "method": method,
                        "path": path,
                        "normalized_path": normalize_path(path),
                        "path_resolved": True,
                        "receiver": receiver,
                        "function": node.name,
                        "module": self.module,
                        "line": node.lineno,
                    }
                )

    def _check_router_def(self, node: ast.Assign | ast.AnnAssign) -> None:
        """X = APIRouter(prefix="...") の収集。静的に取れなければ理由付きで記録。"""
        value = node.value if isinstance(node, ast.Assign) else node.value
        if value is None or not isinstance(value, ast.Call):
            return
        if not _is_apirouter_call(value.func):
            return
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                name = _dotted_name(t)
                if name is not None:
                    targets.append(name)
        else:
            name = _dotted_name(node.target)
            if name is not None:
                targets.append(name)
        if not targets:
            return
        prefix: str | None = None
        prefix_static = True
        for kw in value.keywords:
            if kw.arg == "prefix":
                prefix = _static_str(kw.value)
                prefix_static = prefix is not None
                break
        entry = {
            "line": node.lineno,
            "targets": sorted(targets),
            "prefix": prefix if prefix_static else None,
            "prefix_resolved": prefix_static,
        }
        if not prefix_static:
            entry["unresolved_reason"] = "dynamic APIRouter prefix"
        self.router_defs.append(entry)

    def _check_include(self, node: ast.Expr) -> None:
        """app.include_router(router, prefix="...") の収集。"""
        call = node.value
        if not isinstance(call, ast.Call):
            return
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "include_router"):
            return
        host = _dotted_name(func.value)
        if not call.args and not call.keywords:
            return
        router_expr = call.args[0] if call.args else None
        for kw in call.keywords:
            if kw.arg == "router":
                router_expr = kw.value
                break
        router = _dotted_name(router_expr) if router_expr is not None else None
        prefix: str | None = None
        prefix_static = True
        for kw in call.keywords:
            if kw.arg == "prefix":
                prefix = _static_str(kw.value)
                prefix_static = prefix is not None
                break
        self.includes.append(
            {
                "line": node.lineno,
                "host": host,
                "router": router,
                "prefix": prefix if prefix_static else None,
                "prefix_resolved": prefix_static,
                **({} if prefix_static else {"unresolved_reason": "dynamic include_router prefix"}),
            }
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_router_def(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_router_def(node)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        self._check_include(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_def(node)

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not self._func_stack:
            # トップレベルの関数だけを functions に載せる
            self.functions.append(self._def_entry(node))
        self.defined_names.setdefault(node.name, []).append(node.lineno)
        self._check_route(node)
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if not self._func_stack:
            self.classes.append(self._def_entry(node))
        self.defined_names.setdefault(node.name, []).append(node.lineno)
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    # -- 呼び出し参照 ---------------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.calls.append(
                {
                    "name": node.id,
                    "line": node.lineno,
                    "enclosing": self._func_stack[-1] if self._func_stack else None,
                }
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.calls.append(
                {
                    "name": node.attr,
                    "line": node.lineno,
                    "enclosing": self._func_stack[-1] if self._func_stack else None,
                }
            )
        self.generic_visit(node)


def _parse_python(
    rel: Path, text: str, internal_modules: frozenset[str]
) -> tuple[dict | None, list[dict], list[dict], list[dict], list[dict], str | None,
           list[dict], list[dict], list[dict]]:
    """(module_entry, routes, calls, unresolved, defined, error, router_defs, includes, aliases)を返す。"""
    module = _module_name(rel)
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return None, [], [], [], [], f"SyntaxError: {e}", [], [], []
    except ValueError as e:  # null bytes 等
        return None, [], [], [], [], f"ValueError: {e}", [], [], []
    visitor = _ModuleVisitor(module, internal_modules)
    visitor.is_pkg = _is_pkg_module(rel)
    visitor.visit(tree)
    entry = {
        "name": module,
        "path": rel.as_posix(),
        "lines": len(text.splitlines()),
        "functions": sorted(visitor.functions, key=lambda d: (d["line"], d["name"])),
        "classes": sorted(visitor.classes, key=lambda d: (d["line"], d["name"])),
        "internal_imports": sorted(
            visitor.internal_imports, key=lambda d: (d["line"], d["module"])),
        "external_imports": sorted(
            visitor.external_imports, key=lambda d: (d["line"], d["name"])),
    }
    routes = [
        {**r, "file": rel.as_posix()}
        for r in sorted(visitor.routes, key=lambda d: (d["line"], str(d["method"]), str(d["path"])))
    ]
    calls = [
        {"file": rel.as_posix(), "module": module, **c}
        for c in sorted(visitor.calls, key=lambda d: (d["line"], d["name"]))
    ]
    defined = [
        {"module": module, "name": name, "lines": sorted(lines)}
        for name, lines in sorted(visitor.defined_names.items())
    ]
    return (entry, routes, calls, visitor.unresolved_imports, defined, None,
            visitor.router_defs, visitor.includes, visitor.import_aliases)


def _scan_js_line(
    rel_posix: str, lineno: int, line: str, wrappers: dict[str, str],
    fixed: list[dict], partial: list[dict], unresolved: list[dict],
) -> None:
    """1行中の fetch/api/request 呼び出しを走査する。

    連結の `+式+` とテンプレートリテラルの `${式}` はどちらも `{}` に正規化して
    `/api/projects/{}/mission` の形で解決する。同一ファイル内の1行ラッパー経由の
    呼び出しは接頭辞を適用して解決する。クエリ文字列(`?` 以降)は突合前に取り除く。
    末尾が変数で終わるもの・`/api/` で始まらない断片は部分パス(`partial`)に分け、
    `js_only` に混ぜない。先頭が式のものだけ `unresolved` に残す。
    """
    for m in _JS_CALL_START_RE.finditer(line):
        _scan_js_call(rel_posix, lineno, line, m, wrappers, fixed, partial, unresolved)


def _parse_js_call_args(
    line: str, arg_start: int,
) -> tuple[list[tuple[str, str]] | None, str, bool]:
    """第1引数の式をトークン列として読む。

    戻り値: (tokens | None, raw, ok)。
    tokens は [("str", 値) / ("expr", 原文)]。
    """
    i = arg_start
    while i < len(line) and line[i] in " \t":
        i += 1
    tokens: list[tuple[str, str]] = []
    pos = i
    ok = True
    while True:
        while pos < len(line) and line[pos] in " \t":
            pos += 1
        if pos >= len(line):
            ok = False
            break
        ch = line[pos]
        if ch in "'\"":
            val, pos2, closed = _read_quoted(line, pos, ch)
            if not closed:
                ok = False
                break
            tokens.append(("str", val))
            pos = pos2
        elif ch == "`":
            val, pos2, closed = _read_template(line, pos)
            if not closed:
                ok = False
                break
            tokens.append(("str", val))
            pos = pos2
        else:
            # 式(変数・関数呼び出し・数値等): トップレベルの + , ; ) まで読む。
            # クォート内・括弧内は読み飛ばす(例: url('/mission') 全体を1式とする)。
            j = pos
            depth = 0
            quote: str | None = None
            while j < len(line):
                c = line[j]
                if quote is not None:
                    if c == "\\" and j + 1 < len(line):
                        j += 2
                        continue
                    if c == quote:
                        quote = None
                    j += 1
                    continue
                if c in "'\"`":
                    quote = c
                    j += 1
                    continue
                if c == "(":
                    depth += 1
                elif c == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif c == "+" and depth == 0:
                    break
                elif c == "," and depth == 0:
                    break
                elif c == ";" and depth == 0:
                    break
                j += 1
            if quote is not None:
                ok = False
                break
            expr = line[pos:j].strip()
            if not expr:
                ok = False
                break
            tokens.append(("expr", expr))
            pos = j
        while pos < len(line) and line[pos] in " \t":
            pos += 1
        if pos < len(line) and line[pos] == "+":
            pos += 1
            continue
        break
    raw_call = line[arg_start:pos].strip()
    if len(raw_call) > 200:
        raw_call = raw_call[:200]
    if not ok or not tokens:
        return None, raw_call or line.strip()[:200], False
    return tokens, raw_call, True


def _tokens_to_literal(tokens: list[tuple[str, str]]) -> str:
    """トークン列を結合し、式は `{}` に正規化する。"""
    parts: list[str] = []
    for kind, val in tokens:
        if kind == "str":
            parts.append(val)
        else:
            parts.append("{}")
    return "".join(parts)


def _emit_js_path(
    rel_posix: str, lineno: int, raw: str, literal: str,
    fixed: list[dict], partial: list[dict], unresolved: list[dict],
    via_wrapper: str | None = None,
) -> None:
    if not literal.startswith("/"):
        unresolved.append(
            {"file": rel_posix, "line": lineno, "raw": raw,
             "reason": "non-rooted or relative path",
             **({"via_wrapper": via_wrapper} if via_wrapper else {})})
        return
    normalized = normalize_js_path(literal)
    base_entry = {
        "file": rel_posix, "line": lineno,
        "path": _strip_query(literal), "normalized_path": normalized,
        "raw": raw,
        **({"via_wrapper": via_wrapper} if via_wrapper else {}),
    }
    if _is_path_complete(normalized):
        fixed.append(base_entry)
    else:
        partial.append({**base_entry, "reason": _partial_reason(normalized)})


def _scan_js_call(
    rel_posix: str, lineno: int, line: str, match: re.Match,
    wrappers: dict[str, str],
    fixed: list[dict], partial: list[dict], unresolved: list[dict],
) -> None:
    tokens, raw_call, ok = _parse_js_call_args(line, match.end())
    if not ok or tokens is None:
        unresolved.append(
            {"file": rel_posix, "line": lineno, "raw": raw_call,
             "reason": "unparsable argument"})
        return
    # ラッパー経由の呼び出し: 第1引数の式全体が wrapper(...) の形か確認する
    if len(tokens) == 1 and tokens[0][0] == "expr":
        resolved = _resolve_wrapper_call(tokens[0][1], wrappers)
        if resolved is not None:
            kind, payload = resolved
            if kind == "full":
                literal, wrapper_name = payload
                _emit_js_path(rel_posix, lineno, raw_call, literal,
                              fixed, partial, unresolved,
                              via_wrapper=wrapper_name)
            elif kind == "partial":
                _emit_js_path(rel_posix, lineno, raw_call, payload["literal"],
                              fixed, partial, unresolved,
                              via_wrapper=payload.get("wrapper"))
            else:  # "unresolved"
                unresolved.append(
                    {"file": rel_posix, "line": lineno, "raw": raw_call,
                     "reason": payload})
            return
        # ラッパーでない通常の動的引数 → 解決不能
        unresolved.append(
            {"file": rel_posix, "line": lineno, "raw": raw_call,
             "reason": "dynamic argument"})
        return
    # 先頭が式(静的なリテラルで始まらない) → 解決不能
    if tokens[0][0] != "str":
        unresolved.append(
            {"file": rel_posix, "line": lineno, "raw": raw_call,
             "reason": "dynamic argument"})
        return
    _emit_js_path(rel_posix, lineno, raw_call, _tokens_to_literal(tokens),
                  fixed, partial, unresolved)


def _resolve_wrapper_call(
    expr: str, wrappers: dict[str, str],
) -> tuple[str, object] | None:
    """wrapper(...) 形の式を解決する。ラッパー呼び出しでなければ None。

    戻り値: ("full", (literal, wrapper名)) /
    ("partial", {"literal","reason","wrapper"}) / ("unresolved", reason)。
    引数が文字列リテラルなら完全なパスに、動的なら部分パスにする。
    ラッパーの形が単純でない・未定義の名前は解決せず unresolved のまま(推測しない)。
    """
    text = expr.strip()
    if not text.endswith(")"):
        return None
    depth = 0
    open_at = -1
    quote: str | None = None
    for idx, ch in enumerate(text):
        if quote is not None:
            if ch == "\\":
                continue
            if ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "(":
            if depth == 0:
                open_at = idx
            depth += 1
        elif ch == ")":
            depth -= 1
    if depth != 0 or open_at < 0:
        return None
    callee = text[:open_at].strip()
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", callee):
        return None
    if callee not in wrappers:
        return None
    arg_text = text[open_at + 1: -1]
    args = _split_call_args(arg_text)
    if args is None or len(args) != 1:
        return ("unresolved", "wrapper call with multiple arguments")
    arg = args[0].strip()
    prefix = wrappers[callee]
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "'\"":
        val, _, closed = _read_quoted(arg + " ", 0, arg[0])
        if not closed:
            return ("unresolved", "unparsable wrapper argument")
        return ("full", (_join_prefix(prefix, val), callee))
    if len(arg) >= 2 and arg[0] == "`" and arg[-1] == "`":
        inner = arg[1:-1]
        if "${" in inner and not re.fullmatch(r"\$\{[A-Za-z_$][\w$]*\}", inner.strip()):
            return ("unresolved", "unparsable wrapper argument")
        return ("full", (_join_prefix(prefix, _TEMPLATE_EXPR_RE.sub("{}", inner)), callee))
    if not arg:
        return ("unresolved", "empty wrapper argument")
    # 動的な引数 → 部分パス
    return ("partial", {"literal": _join_prefix(prefix, "{}"),
                        "reason": "dynamic wrapper argument",
                        "wrapper": callee})


def _read_quoted(line: str, pos: int, quote: str) -> tuple[str, int, bool]:
    """クォート文字列を読む。(値, 終端の次位置, 閉じたか)。"""
    buf: list[str] = []
    i = pos + 1
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(line[i + 1])
            i += 2
            continue
        if ch == quote:
            return "".join(buf), i + 1, True
        buf.append(ch)
        i += 1
    return "".join(buf), i, False


def _read_template(line: str, pos: int) -> tuple[str, int, bool]:
    """テンプレートリテラルを読む。`${...}` は `{}` に正規化する。

    (値, 終端の次位置, 閉じたか)。`${` に対応する `}` が無い場合のみ失敗。
    """
    buf: list[str] = []
    i = pos + 1
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(line[i + 1])
            i += 2
            continue
        if ch == "`":
            return "".join(buf), i + 1, True
        if ch == "$" and i + 1 < len(line) and line[i + 1] == "{":
            j = line.find("}", i + 2)
            if j < 0:
                return "".join(buf), len(line), False
            buf.append("{}")
            i = j + 1
            continue
        buf.append(ch)
        i += 1
    return "".join(buf), i, False


def _join_prefix(prefix: str, path: str) -> str:
    """プレフィックスとパスを結合する(スラッシュの重複・欠落を正規化)。"""
    if not prefix:
        return path
    if not path:
        return prefix
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _resolve_router_ref(
    ref: str, cur_module: str, aliases_by_module: dict[str, dict[str, str]],
    module_files: dict[str, str],
) -> tuple[tuple[str, str] | None, str | None]:
    """router参照(X / mod.X)→((file, name), None) か (None, reason)。

    解決できない場合は (None, reason) を返す(不明を混ぜない)。
    """
    if not ref:
        return None, "empty router reference"
    aliases = aliases_by_module.get(cur_module, {})
    if "." not in ref:
        # 同一モジュール内の変数 or from-import された名前
        target = aliases.get(ref)
        if target is not None and "." in target:
            mod_name, _, attr = target.rpartition(".")
            if mod_name in module_files:
                return (module_files[mod_name], attr), None
            return None, f"router target module not in project: {mod_name}"
        cur_file = module_files.get(cur_module)
        if cur_file is None:
            return None, f"router module not in project: {cur_module}"
        return (cur_file, ref), None  # 同一モジュール内の変数とみなす
    # mod.X 形式: mod を解決する
    mod_part, _, attr = ref.rpartition(".")
    mod_target = aliases.get(mod_part, mod_part)
    if mod_target in module_files:
        return (module_files[mod_target], attr), None
    return None, f"router module not in project: {mod_target}"


def _apply_router_prefixes(
    routes: list[dict],
    router_defs: list[dict], includes: list[dict],
    aliases_by_module: dict[str, dict[str, str]] | None = None,
    module_files: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """APIRouter(prefix)/include_router(prefix)を解決して完全なパスにする。

    戻り値: (解決済みルート一覧, ルータ診断一覧)。
    プレフィックスが動的で取れないルートは空文字にせず `path_resolved: false`
    と理由を付ける。
    """
    aliases_by_module = aliases_by_module or {}
    module_files = module_files or {}
    # 1. (file, router変数名) → APIRouter の prefix
    file_prefix: dict[tuple[str, str], tuple[str | None, bool, str | None]] = {}
    declared: set[tuple[str, str]] = set()
    for rd in router_defs:
        for target in rd["targets"]:
            key = (rd["file"], target)
            declared.add(key)
            file_prefix[key] = (rd["prefix"], rd["prefix_resolved"],
                                rd.get("unresolved_reason"))
    # 2. include の解決: host 側の追加prefix と、router 実体への prefix
    include_extra: dict[tuple[str, str], list[tuple[str | None, bool, str | None]]] = {}
    router_included: set[tuple[str, str]] = set()
    router_prefix_from_include: dict[tuple[str, str],
                                     list[tuple[str | None, bool, str | None]]] = {}
    include_unresolved: list[dict] = []
    for inc in includes:
        host_file = inc["file"]
        try:
            host_mod = _module_name(Path(host_file))
        except (ValueError, TypeError):
            host_mod = ""
        resolved = None
        reason = None
        if inc.get("router"):
            resolved, reason = _resolve_router_ref(
                inc["router"], host_mod, aliases_by_module, module_files)
        if resolved is not None:
            router_included.add(resolved)
            router_prefix_from_include.setdefault(resolved, []).append(
                (inc["prefix"], inc["prefix_resolved"], inc.get("unresolved_reason")))
        else:
            include_unresolved.append({
                "file": host_file, "line": inc.get("line"),
                "host": inc.get("host"), "router": inc.get("router"),
                "prefix": inc.get("prefix"),
                "prefix_resolved": inc.get("prefix_resolved", True),
                "unresolved_reason": reason or "cannot resolve router reference",
            })
        if inc.get("host"):
            key = (host_file, inc["host"])
            include_extra.setdefault(key, []).append(
                (inc["prefix"], inc["prefix_resolved"], inc.get("unresolved_reason")))
    resolved_routes: list[dict] = []
    for r in routes:
        if not r.get("path_resolved", True):
            resolved_routes.append(r)  # 動的パス: そのまま unresolved
            continue
        receiver = r.get("receiver")
        if receiver is None:
            resolved_routes.append(r)
            continue
        key = (r["file"], receiver)
        own_prefix, own_ok, own_reason = file_prefix.get(key, ("", True, None))
        # 自身への include prefix(同一ファイルのhost) + 実体への include prefix
        extras = list(include_extra.get(key, []))
        extras.extend(router_prefix_from_include.get(key, []))
        if not own_ok:
            resolved_routes.append({
                **r,
                "path_resolved": False,
                "unresolved_reason": f"APIRouter prefix not static: {own_reason}",
            })
            continue
        bad = [reason for _, ok, reason in extras if not ok]
        if bad:
            resolved_routes.append({
                **r,
                "path_resolved": False,
                "unresolved_reason": f"include_router prefix not static: {bad[0]}",
            })
            continue
        full = _join_prefix(own_prefix or "", r["path"] or "")
        for extra_prefix, _, _ in extras:
            full = _join_prefix(extra_prefix or "", full)
        resolved_routes.append({
            **r,
            "path": full,
            "normalized_path": normalize_path(full),
        })
    # 診断: 宣言された router の解決状況
    diagnostics: list[dict] = []
    for (fname, target) in sorted(declared):
        prefix, ok, reason = file_prefix[(fname, target)]
        diagnostics.append({
            "file": fname, "router": target,
            "prefix": prefix, "prefix_resolved": ok,
            **({} if ok else {"unresolved_reason": reason}),
            "included": (fname, target) in router_included,
        })
    diagnostics.sort(key=lambda d: (d["file"], str(d["router"])))
    diagnostics.extend(
        sorted(include_unresolved, key=lambda d: (str(d["file"]), d.get("line") or 0)))
    return resolved_routes, diagnostics


def _is_test_file(rel: Path) -> bool:
    return (
        rel.suffix == ".py"
        and rel.name.startswith("test_")
        and "tests" in rel.parts[:-1]
    )


def _walk_files(root_path: Path, parse_errors: list[dict]) -> list[Path]:
    """除外枝刈り・非追随で .py / .js ファイルを集める(決定的な順序)。"""
    found: list[Path] = []
    entries = sorted(root_path.iterdir(), key=lambda p: p.name)
    stack: list[Path] = entries[::-1]
    while stack:
        p = stack.pop()
        try:
            if p.is_symlink():
                continue  # シンボリックリンクは辿らない
        except OSError:
            continue
        name = p.name
        try:
            is_dir = p.is_dir()
        except OSError:
            continue
        if is_dir:
            if name in EXCLUDED_DIRS:
                continue  # 除外ディレクトリは枝刈り
            try:
                children = sorted(p.iterdir(), key=lambda c: c.name)
            except OSError:
                parse_errors.append(
                    {"path": p.relative_to(root_path).as_posix()
                     if _is_within(p, root_path) else name,
                     "error": "list failed"})
                continue
            stack.extend(children[::-1])
            continue
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if p.suffix in (".py", ".js"):
            found.append(p)
    return found


def build_index(root: str | Path) -> dict:
    """プロジェクトのルート以下を走査し、決定的な解析結果を返す(読み取り専用)。

    除外: `.git`、`.venv`、`node_modules`、`__pycache__`、`data`、`backups`、
    `temp` 配下、シンボリックリンク、1MB超の巨大ファイル。
    """
    root_path = Path(root).resolve()
    modules: list[dict] = []
    routes: list[dict] = []
    calls: list[dict] = []
    js_api_calls: list[dict] = []
    js_partial: list[dict] = []
    js_unresolved: list[dict] = []
    unresolved_imports: list[dict] = []
    parse_errors: list[dict] = []
    skipped_large_files: list[dict] = []
    defined: list[dict] = []
    router_defs: list[dict] = []
    includes: list[dict] = []
    aliases_by_module: dict[str, dict[str, str]] = {}
    py_files: list[Path] = []
    js_files: list[Path] = []

    try:
        files = _walk_files(root_path, parse_errors)
    except OSError as e:
        return {
            "root": str(root_path), "complete": False,
            "modules": [], "routes": [], "js_api_calls": [], "js_partial": [],
            "js_unresolved": [],
            "tests": [], "unresolved_imports": [], "skipped_large_files": [],
            "parse_errors": [{"path": ".", "error": f"walk failed: {e}"}],
            "calls": [], "defined": [],
        }

    for p in files:
        rel = p.relative_to(root_path)
        try:
            size = p.stat().st_size
        except OSError:
            parse_errors.append({"path": rel.as_posix(), "error": "stat failed"})
            continue
        if size > MAX_FILE_BYTES:
            # 巨大ファイルは仕様として除外するが、黙って捨てず記録する
            skipped_large_files.append(
                {"path": rel.as_posix(), "bytes": size,
                 "reason": f"exceeds {MAX_FILE_BYTES} bytes"})
            continue
        if p.suffix == ".py":
            py_files.append(p)
        elif p.suffix == ".js":
            js_files.append(p)

    # 内部モジュール集合を先に確定させる(内外判定用)
    rels = []
    for p in py_files:
        try:
            rels.append(p.relative_to(root_path))
        except ValueError:
            continue
    internal_modules = frozenset(_module_name(r) for r in rels)

    for p in py_files:
        rel = p.relative_to(root_path)
        posix = rel.as_posix()
        try:
            text = p.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            parse_errors.append({"path": posix, "error": "UnicodeDecodeError: not utf-8 text"})
            continue
        except OSError as e:
            parse_errors.append({"path": posix, "error": f"read failed: {e}"})
            continue
        entry, rts, cls, unres, defs, err, rdefs, incs, aliases = _parse_python(
            rel, text, internal_modules)
        if err is not None:
            parse_errors.append({"path": posix, "error": err})
            continue
        assert entry is not None
        mod_name = entry["name"]
        modules.append(entry)
        routes.extend(rts)
        calls.extend(cls)
        defined.extend(defs)
        for rd in rdefs:
            router_defs.append({"file": posix, "module": mod_name, **rd})
        for inc in incs:
            includes.append({"file": posix, "module": mod_name, **inc})
        for al in aliases:
            aliases_by_module.setdefault(mod_name, {})[al["local"]] = al["target"]
        for u in sorted(unres, key=lambda d: (d["line"], d["raw"])):
            unresolved_imports.append({"path": posix, **u})

    for p in js_files:
        rel = p.relative_to(root_path)
        posix = rel.as_posix()
        try:
            text = p.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            parse_errors.append({"path": posix, "error": "UnicodeDecodeError: not utf-8 text"})
            continue
        except OSError as e:
            parse_errors.append({"path": posix, "error": f"read failed: {e}"})
            continue
        lines = text.splitlines()
        wrappers = _collect_js_wrappers(lines)
        for lineno, line in enumerate(lines, start=1):
            _scan_js_line(posix, lineno, line, wrappers,
                          js_api_calls, js_partial, js_unresolved)

    module_files = {m["name"]: m["path"] for m in modules}
    routes, router_diagnostics = _apply_router_prefixes(
        routes, router_defs, includes, aliases_by_module, module_files)
    router_diagnostics.sort(
        key=lambda d: (str(d.get("file", "")), str(d.get("router", "")),
                       str(d.get("host", "")), d.get("line") or 0))

    modules.sort(key=lambda d: d["name"])
    routes.sort(key=lambda d: (d["file"], d["line"], str(d["method"]), str(d["path"])))
    calls.sort(key=lambda d: (d["file"], d["line"], d["name"]))
    js_api_calls.sort(key=lambda d: (d["file"], d["line"], d["path"]))
    js_partial.sort(key=lambda d: (d["file"], d["line"], d["path"]))
    js_unresolved.sort(key=lambda d: (d["file"], d["line"], d["raw"]))
    unresolved_imports.sort(key=lambda d: (d["path"], d["line"], d["raw"]))
    skipped_large_files.sort(key=lambda d: d["path"])
    parse_errors.sort(key=lambda d: d["path"])
    defined.sort(key=lambda d: (d["module"], d["name"]))

    by_name = {m["name"]: m for m in modules}
    tests = []
    for m in modules:
        rel = Path(m["path"])
        if _is_test_file(rel):
            imports = sorted({imp["module"] for imp in m["internal_imports"]})
            tests.append({"file": m["path"], "module": m["name"], "imports": imports})
    tests.sort(key=lambda d: d["file"])
    _ = by_name

    return {
        "root": str(root_path),
        "complete": len(parse_errors) == 0,
        "modules": modules,
        "routes": routes,
        "router_diagnostics": router_diagnostics,
        "js_api_calls": js_api_calls,
        "js_partial": js_partial,
        "js_unresolved": js_unresolved,
        "tests": tests,
        "unresolved_imports": unresolved_imports,
        "skipped_large_files": skipped_large_files,
        "parse_errors": parse_errors,
        "calls": calls,
        "defined": defined,
    }


def summarize_index(index: dict, max_examples: int = 5) -> dict:
    """インデックスの要約(件数・complete・エラーと未解決の上位)。

    CLIの既定出力用。全体の一覧は含めない。
    """
    def _head(items: list, key) -> list:
        return sorted(items, key=key)[:max_examples]

    return {
        "root": index.get("root"),
        "complete": index.get("complete"),
        "counts": {
            "modules": len(index.get("modules", [])),
            "routes": len(index.get("routes", [])),
            "routes_unresolved": sum(
                1 for r in index.get("routes", []) if not r.get("path_resolved", True)),
            "js_api_calls": len(index.get("js_api_calls", [])),
            "js_partial": len(index.get("js_partial", [])),
            "js_unresolved": len(index.get("js_unresolved", [])),
            "tests": len(index.get("tests", [])),
            "unresolved_imports": len(index.get("unresolved_imports", [])),
            "skipped_large_files": len(index.get("skipped_large_files", [])),
            "parse_errors": len(index.get("parse_errors", [])),
            "calls": len(index.get("calls", [])),
            "defined": len(index.get("defined", [])),
        },
        "parse_errors": _head(index.get("parse_errors", []), lambda d: d.get("path", "")),
        "routes_unresolved": _head(
            [r for r in index.get("routes", []) if not r.get("path_resolved", True)],
            lambda d: (d.get("file", ""), d.get("line", 0))),
        "js_unresolved": _head(
            index.get("js_unresolved", []),
            lambda d: (d.get("file", ""), d.get("line", 0), d.get("raw", ""))),
        "unresolved_imports": _head(
            index.get("unresolved_imports", []),
            lambda d: (d.get("path", ""), d.get("line", 0), d.get("raw", ""))),
    }


def _resolve_target(index: dict, target: str) -> tuple[dict | None, str | None]:
    """target → ({"module":..., "function":...|None, "path":...}, None) か (None, reason)。"""
    text = str(target or "").replace("\\", "/").strip().lstrip("./")
    if not text:
        return None, "empty target"
    modules = {m["name"]: m for m in index.get("modules", [])}
    paths = {m["path"]: m for m in index.get("modules", [])}
    if text.endswith(".py"):
        norm = text.lstrip("/")
        entry = paths.get(norm)
        if entry is None:
            # 先頭のディレクトリなし指定(src省略等)にも寛容に: 末尾一致を試す
            cands = [m for p, m in sorted(paths.items()) if p.endswith("/" + norm)]
            if len(cands) == 1:
                entry = cands[0]
            elif len(cands) > 1:
                return None, (
                    "multiple modules match path "
                    f"'{target}': {', '.join(sorted(m['name'] for m in cands))}")
            else:
                return None, f"no such module file: '{target}'"
        return {"module": entry["name"], "function": None, "path": entry["path"]}, None
    # ドット記法: 最長prefixでモジュール一致を探す
    parts = text.split(".")
    for i in range(len(parts), 0, -1):
        mod = ".".join(parts[:i])
        if mod in modules:
            rest = parts[i:]
            if not rest:
                m = modules[mod]
                return {"module": mod, "function": None, "path": m["path"]}, None
            if len(rest) == 1:
                m = modules[mod]
                names = {f["name"] for f in m.get("functions", [])}
                names |= {c["name"] for c in m.get("classes", [])}
                if rest[0] not in names:
                    return None, (
                        f"module '{mod}' has no function/class '{rest[0]}'")
                return {"module": mod, "function": rest[0], "path": m["path"]}, None
            return None, (
                f"cannot resolve '{target}': too many parts after module '{mod}'")
    return None, f"no such module: '{target}'"


def _import_graph(index: dict) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for m in index.get("modules", []):
        deps = {imp["module"] for imp in m.get("internal_imports", [])}
        graph[m["name"]] = deps
    return graph


def _reverse_graph(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    rev: dict[str, set[str]] = {m: set() for m in graph}
    for mod, deps in graph.items():
        for dep in deps:
            if dep in rev:
                rev[dep].add(mod)
            # import先がファイルに対応しない名前(例: パッケージ名のみ)の場合、
            # その名前のモジュールが無ければ逆引きに載せない(不明を混ぜない)
    return rev


def impact(index: dict, target: str) -> dict:
    """target(ファイルパス or app.foo.関数名)への変更の影響範囲を返す(読み取り専用)。"""
    resolved, reason = _resolve_target(index, target)
    if resolved is None:
        # 空の結果ではなく found:false(「影響なし」と区別する)
        return {"found": False, "target": str(target), "reason": reason}
    mod0: str = resolved["module"]
    func: str | None = resolved["function"]

    graph = _import_graph(index)
    rev = _reverse_graph(graph)

    # 推移的な依存元(BFS、深さ付き)
    dependents: list[dict] = []
    depth: dict[str, int] = {mod0: 0}
    via: dict[str, str | None] = {mod0: None}
    frontier = [mod0]
    d = 0
    while frontier:
        d += 1
        nxt: list[str] = []
        for cur in sorted(frontier):
            for dep_by in sorted(rev.get(cur, ())):
                if dep_by not in depth:
                    depth[dep_by] = d
                    via[dep_by] = cur
                    nxt.append(dep_by)
        frontier = nxt
    for name in sorted(depth, key=lambda n: (depth[n], n)):
        if name == mod0:
            continue
        mod_entry = next((m for m in index.get("modules", []) if m["name"] == name), None)
        dependents.append({
            "module": name,
            "depth": depth[name],
            "via": via[name],
            "path": mod_entry["path"] if mod_entry else None,
        })

    affected = set(depth)  # mod0 + 全依存元

    # 呼び出し箇所(関数指定時のみ)
    callers: list[dict] = []
    ambiguous = False
    if func is not None:
        definers = sorted(
            {d["module"] for d in index.get("defined", []) if d["name"] == func})
        ambiguous = len(definers) > 1
        def_lines = {
            (d["module"], line)
            for d in index.get("defined", [])
            if d["name"] == func
            for line in d.get("lines", [])
        }
        for c in index.get("calls", []):
            if c.get("name") != func:
                continue
            if (c.get("module"), c.get("line")) in def_lines:
                continue  # 定義行そのものは呼び出しではない
            callers.append({
                "file": c["file"], "module": c["module"], "line": c["line"],
                "enclosing": c.get("enclosing"), "ambiguous": ambiguous,
            })
        callers.sort(key=lambda c: (c["file"], c["line"]))

    # テスト: 直接または推移的にそのモジュールを使うもの
    tests_out: list[dict] = []
    for t in index.get("tests", []):
        closure = set(t.get("imports", []))
        stack = list(closure)
        while stack:
            cur = stack.pop()
            for dep in graph.get(cur, ()):
                if dep not in closure:
                    closure.add(dep)
                    stack.append(dep)
        if mod0 in closure:
            direct = mod0 in set(t.get("imports", []))
            tests_out.append({
                "file": t["file"], "module": t["module"], "direct": direct})
    tests_out.sort(key=lambda t: t["file"])

    routes_affected = [
        r for r in index.get("routes", []) if r.get("module") in affected
    ]
    routes_affected.sort(
        key=lambda r: (r["file"], r["line"], str(r["method"]), str(r["path"])))
    affected_paths = {r.get("normalized_path") for r in routes_affected
                      if r.get("normalized_path")}
    js_affected = [
        j for j in index.get("js_api_calls", [])
        if j.get("normalized_path") in affected_paths
    ]
    js_affected.sort(key=lambda j: (j["file"], j["line"], j["path"]))

    return {
        "found": True,
        "target": {"input": str(target), **resolved},
        "dependents": dependents,
        "callers": callers,
        "tests": tests_out,
        "routes_affected": routes_affected,
        "js_api_calls_affected": js_affected,
    }


def route_js_map(index: dict) -> dict:
    """画面JSが呼ぶAPIパスとサーバルートの突合(正規化して比較)。

    区分の意味:
    - matched: JSの呼び出しとサーバのルートが一致。
    - js_only: 完全に解決できたJSのパスなのに、サーバにルートが無い
      (=本物の不整合の候補)。
    - server_only: どのJSからも呼ばれていないルート。ただし unresolved/partial
      が残る間は「呼ばれていない」とは断定できない(server_only_caveat参照)。
    - partial: 末尾が変数で終わる・`/api/` で始まらない断片などの部分パス。
      js_only には混ぜない。前方一致するサーバルートの候補(candidates)を付ける。
    - unresolved: 動的に組み立てられたJSパス。js_only に混ぜない。
    - server_unresolved: パスが未解決のサーバルート(path_resolved: false)。
      server_only に混ぜない。

    サーバ側のパラメータ(`{project_id}` など)も `{}` に正規化して比較する。
    JS側のクエリ文字列(`?` 以降)は突合の前に取り除く。
    """
    server: dict[str, dict] = {}
    server_unresolved: list[dict] = []
    for r in index.get("routes", []):
        if not r.get("path_resolved", True) or not r.get("normalized_path"):
            server_unresolved.append(r)
            continue
        key = r.get("normalized_path", "")
        slot = server.setdefault(key, {"path": key, "methods": set(), "routes": []})
        slot["methods"].add(r.get("method", ""))
        slot["routes"].append(r)
    js: dict[str, list[dict]] = {}
    for j in index.get("js_api_calls", []):
        js.setdefault(j.get("normalized_path", ""), []).append(j)
    partial_groups: dict[str, list[dict]] = {}
    for j in index.get("js_partial", []):
        partial_groups.setdefault(j.get("normalized_path", ""), []).append(j)

    matched = []
    for key in sorted(set(server) & set(js)):
        matched.append({
            "path": key,
            "methods": sorted(server[key]["methods"]),
            "server": sorted(server[key]["routes"],
                             key=lambda r: (r["file"], r["line"], str(r["method"]))),
            "js": sorted(js[key], key=lambda j: (j["file"], j["line"])),
        })
    matched_keys = set(server) & set(js)
    server_keys = set(server)
    partial = []
    partial_matched_extra: list[dict] = []
    for key in sorted(partial_groups):
        if key in server_keys:
            # 部分パスでもサーバのルートと完全一致すれば正しい呼び出しとして
            # matched に昇格させる(js要素の reason フィールドで由来は区別可能)。
            # 例: JS `/api/items/${id}` → `/api/items/{}` と
            # サーバ `/api/items/{id}` → `/api/items/{}` の突合。
            partial_matched_extra.append(key)
            continue
        partial.append({
            "path": key,
            "js": sorted(partial_groups[key], key=lambda j: (j["file"], j["line"])),
            "reason": partial_groups[key][0].get("reason", "partial path"),
            "candidates": _prefix_candidates(key, server_keys),
        })
    for key in partial_matched_extra:
        matched.append({
            "path": key,
            "methods": sorted(server[key]["methods"]),
            "server": sorted(server[key]["routes"],
                             key=lambda r: (r["file"], r["line"], str(r["method"]))),
            "js": sorted(partial_groups[key], key=lambda j: (j["file"], j["line"])),
            "partial_match": True,
        })
        matched_keys.add(key)
    matched.sort(key=lambda e: e["path"])
    js_only = [
        {"path": key, "js": sorted(js[key], key=lambda j: (j["file"], j["line"]))}
        for key in sorted(set(js) - matched_keys)
    ]
    server_only = [
        {"path": key, "methods": sorted(server[key]["methods"]),
         "server": sorted(server[key]["routes"],
                           key=lambda r: (r["file"], r["line"], str(r["method"])))}
        for key in sorted(set(server) - set(js) - set(partial_matched_extra))
    ]
    unresolved = sorted(index.get("js_unresolved", []),
                        key=lambda j: (j["file"], j["line"], j["raw"]))
    server_unresolved_sorted = sorted(
        server_unresolved, key=lambda r: (r["file"], r["line"], str(r["method"])))
    caveat = None
    if unresolved or partial_groups:
        caveat = (
            "unresolved または partial のJS呼び出しが残っているため、"
            "server_only のルートが「どのJSからも呼ばれていない」とは断定できない。"
            f"(unresolved: {len(unresolved)}件, partial: {len(partial)}件)"
        )
    return {
        "meanings": {
            "matched": "JSの呼び出しとサーバのルートが一致",
            "js_only": "完全に解決できたJSのパスなのに、サーバにルートが無い(=本物の不整合の候補)",
            "server_only": "どのJSからも呼ばれていないルート",
            "partial": "末尾が変数で終わる・/api/ で始まらない断片などの部分パス(js_onlyには混ぜない)",
            "unresolved": "動的に組み立てられたJSパス(js_onlyには混ぜない)",
            "server_unresolved": "パスが未解決のサーバルート(server_onlyには混ぜない)",
        },
        "matched": matched,
        "js_only": js_only,
        "server_only": server_only,
        "server_only_caveat": caveat,
        "partial": partial,
        "unresolved": unresolved,
        "server_unresolved": server_unresolved_sorted,
    }
