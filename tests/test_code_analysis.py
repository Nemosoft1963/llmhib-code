"""読み取り専用コード解析(build_index/impact/route_js_mapとツールの受入試験)。外部通信なし。標準ライブラリのみ。"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import code_analysis  # noqa: E402
from src import sessions  # noqa: E402
from src import tools_core  # noqa: E402


FILES = {
    # 内部import(絶対・from・相対・遅延)と外部importの混在
    "app/__init__.py": "",
    "app/base.py": "X = 1\n\n\ndef helper():\n    return X\n",
    "app/util.py": (
        "import os\n"
        "import json\n"
        "import app.base\n"
        "from app.base import helper\n"
        "from . import base as base2\n"
        "\n\n"
        "def run():\n"
        "    import app.lazy\n"
        "    return helper()\n"
    ),
    "app/lazy.py": "Y = 2\n",
    "app/mid.py": "from app.util import run\n\n\ndef mid_fn():\n    return run()\n",
    "app/top.py": "import app.mid\n",
    # ルート(複数メソッド・パラメータ付き)
    "app/web.py": (
        "from fastapi import APIRouter\n"
        "import app.base\n"
        "\n"
        "router = APIRouter()\n"
        "\n\n"
        "@router.get('/items/{id}')\n"
        "def get_item():\n"
        "    return {}\n"
        "\n\n"
        "@router.route('/multi', methods=['GET', 'POST'])\n"
        "def multi():\n"
        "    return {}\n"
        "\n\n"
        "@router.delete('/gone')\n"
        "def gone():\n"
        "    return {}\n"
    ),
    # 同名関数(ambiguous用)
    "app/dup_a.py": "def same():\n    return 1\n",
    "app/dup_b.py": "def same():\n    return 2\n",
    "app/user.py": "from app.dup_a import same\n\n\ndef go():\n    return same()\n",
    # テスト
    "tests/__init__.py": "",
    "tests/test_mid.py": "import app.mid\n\n\ndef test_x():\n    assert True\n",
    "tests/test_top.py": "import app.top\n",
    "tests/test_web.py": "import app.web\n",
    # JS(固定パス・テンプレート・動的・他パス)
    "static/app.js": (
        "fetch('/api/items/1');\n"
        "fetch(`/api/items/${id}`);\n"
        "api('/api/multi');\n"
        "fetch('/api/nowhere');\n"
        "fetch(url);\n"
        "fetch('/api/' + id);\n"
    ),
    # 除外対象
    "data/skip.py": "import app.base\n",
    ".venv/lib.py": "import app.base\n",
    "node_modules/x.js": "fetch('/api/items/1');\n",
    "__pycache__/c.py": "import app.base\n",
}


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    for rel, content in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def _snapshot(root):
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def test_imports_relative_and_lazy_and_external_split(proj):
    index = code_analysis.build_index(proj)
    util = next(m for m in index["modules"] if m["name"] == "app.util")
    internal = {i["module"] for i in util["internal_imports"]}
    assert {"app.base", "app.lazy"} <= internal
    external = {e["name"] for e in util["external_imports"]}
    assert {"os", "json"} <= external
    assert "app" not in external  # 内部がexternalに混ざらない
    assert index["unresolved_imports"] == []
    # 除外ディレクトリは走査されない
    names = {m["name"] for m in index["modules"]}
    assert "data.skip" not in names and "lib" not in names
    assert index["complete"] is True


def test_routes_multi_method_and_params(proj):
    index = code_analysis.build_index(proj)
    by_func = {r["function"]: r for r in index["routes"] if r["module"] == "app.web"}
    assert by_func["get_item"]["method"] == "GET"
    assert by_func["get_item"]["path"] == "/items/{id}"
    assert by_func["get_item"]["normalized_path"] == "/items/{}"
    multi = sorted(r["method"] for r in index["routes"] if r["function"] == "multi")
    assert multi == ["GET", "POST"]
    assert by_func["gone"]["method"] == "DELETE"


def test_js_fixed_paths_and_normalization_and_route_js_map(proj):
    index = code_analysis.build_index(proj)
    paths = sorted(j["normalized_path"] for j in index["js_api_calls"])
    partial_paths = sorted(j["normalized_path"] for j in index["js_partial"])
    assert "/api/items/{}" in partial_paths  # ${id} は部分パス側に正規化
    assert "/api/multi" in paths
    assert "/api/nowhere" in paths
    assert "/api/{}" in partial_paths  # '/api/' + id の連結も {} に正規化
    mapping = code_analysis.route_js_map(index)
    matched_paths = sorted(m["path"] for m in mapping["matched"])
    assert matched_paths == []  # /api/* とサーバの /items・/multi は一致しない
    # サーバの /items/{} とJSの /api/items/{} は一致しない → 両方に現れる
    js_only_paths = sorted(e["path"] for e in mapping["js_only"])
    server_only_paths = sorted(e["path"] for e in mapping["server_only"])
    assert "/api/nowhere" in js_only_paths
    assert "/items/{}" in server_only_paths
    assert "/multi" in server_only_paths
    # 末尾 {} の部分パスは partial 側に出て、js_only に混ざらない
    partial_paths_map = sorted(e["path"] for e in mapping["partial"])
    assert "/api/items/{}" in partial_paths_map
    assert "/api/items/{}" not in js_only_paths
    # 先頭が式の動的パス(fetch(url))は unresolved に分離され、js_only に混ざらない
    raw_unresolved = " ".join(u["raw"] for u in mapping["unresolved"])
    assert "url" in raw_unresolved
    for e in mapping["js_only"]:
        for j in e["js"]:
            assert "url" not in j["raw"] or "/api" in j["raw"]


def test_route_js_map_matched_case(tmp_path):
    root = tmp_path / "m"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "api.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n\n"
        "@app.get('/api/items/{id}')\nasync def get_item():\n    return {}\n",
        encoding="utf-8")
    (root / "static").mkdir()
    (root / "static" / "x.js").write_text("fetch(`/api/items/${id}`);\n", encoding="utf-8")
    index = code_analysis.build_index(root)
    mapping = code_analysis.route_js_map(index)
    assert [m["path"] for m in mapping["matched"]] == ["/api/items/{}"]
    assert mapping["js_only"] == [] and mapping["server_only"] == []


def test_impact_transitive_dependents_tests_routes_js(tmp_path):
    root = tmp_path / "m"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "app" / "mid.py").write_text(
        "import app.core\n\n\ndef g():\n    return 1\n", encoding="utf-8")
    (root / "app" / "top.py").write_text("import app.mid\n", encoding="utf-8")
    (root / "app" / "web.py").write_text(
        "from fastapi import FastAPI\nimport app.core\napp = FastAPI()\n\n\n"
        "@app.get('/api/core')\ndef r():\n    return {}\n",
        encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_top.py").write_text("import app.top\n", encoding="utf-8")
    (root / "static").mkdir()
    (root / "static" / "x.js").write_text("fetch('/api/core');\n", encoding="utf-8")
    index = code_analysis.build_index(root)
    res = code_analysis.impact(index, "app/core.py")
    assert res["found"] is True
    deps = {d["module"]: d["depth"] for d in res["dependents"]}
    assert deps == {"app.mid": 1, "app.top": 2, "app.web": 1, "tests.test_top": 3}
    assert [t["file"] for t in res["tests"]] == ["tests/test_top.py"]
    assert [r["function"] for r in res["routes_affected"]] == ["r"]
    assert [j["path"] for j in res["js_api_calls_affected"]] == ["/api/core"]


def test_impact_ambiguous_and_not_found(proj):
    index = code_analysis.build_index(proj)
    res = code_analysis.impact(index, "app.user.go")
    assert res["found"] is True  # go は user 内の関数
    res2 = code_analysis.impact(index, "app.dup_a.same")
    callers = [c for c in res2["callers"] if c["file"] == "app/user.py"]
    assert callers and all(c["ambiguous"] is True for c in callers)
    missing = code_analysis.impact(index, "app.nope")
    assert missing["found"] is False and "reason" in missing
    missing2 = code_analysis.impact(index, "no/such.py")
    assert missing2["found"] is False  # 「影響なし」と区別(空リストではない)


def test_parse_errors_and_complete_false(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "ok.py").write_text("X = 1\n", encoding="utf-8")
    (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    index = code_analysis.build_index(root)
    assert index["complete"] is False
    assert [e["path"] for e in index["parse_errors"]] == ["bad.py"]
    assert "bad" not in {m["name"] for m in index["modules"]}  # 黙って消えず一覧に出る


def test_deterministic_and_symlink_and_outside(tmp_path):
    root = tmp_path / "d"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "a.py").write_text("import os\nX = 1\n", encoding="utf-8")
    (root / "outside.py").write_text("import app.a\n", encoding="utf-8")
    target_dir = tmp_path / "ext"
    target_dir.mkdir()
    (target_dir / "evil.py").write_text("X = 9\n", encoding="utf-8")
    link = root / "app" / "link.py"
    try:
        link.symlink_to(target_dir / "evil.py")
    except OSError:
        pytest.skip("symlink not supported")
    first = code_analysis.build_index(root)
    names = {m["name"] for m in first["modules"]}
    assert "app.link" not in names and "evil" not in names  # シンボリックリンクを辿らない
    second = code_analysis.build_index(root)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "12345"
    import subprocess
    code = (
        "import json,sys; sys.path.insert(0, '.');"
        "from src import code_analysis;"
        f"print(json.dumps(code_analysis.build_index({str(root)!r}), sort_keys=True))"
    )
    r1 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        cwd=str(Path(__file__).resolve().parents[1]), env=env, timeout=60)
    env["PYTHONHASHSEED"] = "999"
    r2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        cwd=str(Path(__file__).resolve().parents[1]), env=env, timeout=60)
    assert r1.returncode == 0 and r2.returncode == 0
    assert r1.stdout == r2.stdout  # PYTHONHASHSEED を変えても同じ


def _workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_core, "PROJECTS_ROOT", tmp_path)
    ws = tools_core.Workspace("proj")
    for rel, content in FILES.items():
        p = ws.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (ws.root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    return ws


def test_tools_callable_readonly_no_approval_and_investigation_mode(monkeypatch, tmp_path):
    ws = _workspace(monkeypatch, tmp_path)
    before = _snapshot(ws.root)
    assert "analyze_structure" in sessions.BUILTIN_TOOL_NAMES
    assert "analyze_impact" in sessions.BUILTIN_TOOL_NAMES
    assert "analyze_structure" in sessions.READ_ONLY_TOOLS
    assert "analyze_impact" in sessions.READ_ONLY_TOOLS
    # 承認は要求されない
    assert sessions._tool_needs_approval("analyze_structure") is False
    assert sessions._tool_needs_approval("analyze_impact") is False
    # ツールとして呼べる
    assert "analyze_structure" in dir(ws) and "analyze_impact" in dir(ws)
    assert getattr(ws, "analyze_structure") is not None
    out = json.loads(ws.analyze_structure())
    assert out["module_count"] > 0 and out["complete"] is False
    assert [e["path"] for e in out["parse_errors"]] == ["bad.py"]
    detail = json.loads(ws.analyze_structure(module="app.util"))
    assert detail["found"] is True
    missing = json.loads(ws.analyze_structure(module="app.nope"))
    assert missing["found"] is False
    imp = json.loads(ws.analyze_impact(target="app/mid.py"))
    assert imp["found"] is True
    imp_missing = json.loads(ws.analyze_impact(target="app/nope.py"))
    assert imp_missing["found"] is False
    # 調査モード(読み取り専用)で使える
    assert sessions._execute(ws, "analyze_structure", {}, read_only=True).startswith("{")
    assert sessions._execute(ws, "analyze_impact", {"target": "app/mid.py"},
                             read_only=True).startswith("{")
    denied = sessions._execute(ws, "write_file", {"path": "x", "content": "y"},
                               read_only=True)
    assert "読み取り専用" in denied
    # 読み取り専用: 解析の前後でファイルが変わらない
    assert _snapshot(ws.root) == before


def test_tools_truncate_large_output(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_core, "PROJECTS_ROOT", tmp_path)
    ws = tools_core.Workspace("big")
    for i in range(250):
        (ws.root / f"m{i:03d}.py").write_text(f"X{i} = {i}\n", encoding="utf-8")
    out = json.loads(ws.analyze_structure())
    assert out["truncated"] is True
    assert out["truncated_counts"]["modules"] == 250
    assert len(out["modules"]) == 200


def test_new_tools_exposed_in_all_backend_conversions():
    from src import tools_schema

    names_anthropic = {t["name"] for t in tools_schema.as_anthropic_tools()}
    assert {"analyze_structure", "analyze_impact"} <= names_anthropic
    for conv in (tools_schema.as_ollama_tools, tools_schema.as_openai_tools):
        names = {t["function"]["name"] for t in conv()}
        assert {"analyze_structure", "analyze_impact"} <= names
    names_gemini = {t["name"] for t in tools_schema.as_gemini_tools()}
    assert {"analyze_structure", "analyze_impact"} <= names_gemini


def _write(root, rel, content, encoding="utf-8", raw_bytes=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if raw_bytes is not None:
        p.write_bytes(raw_bytes)
    else:
        p.write_text(content, encoding=encoding)


def test_bom_files_readable_and_complete_true(tmp_path):
    root = tmp_path / "bom"
    root.mkdir()
    # BOM付きの正しいPythonファイル(BOMがあっても無くても読める)
    _write(root, "app/__init__.py", "X = 1\n",
           raw_bytes=b"\xef\xbb\xbfX = 1\n")
    _write(root, "app/mod.py", "import app\n\n\ndef f():\n    return 1\n",
           raw_bytes=b"\xef\xbb\xbfimport app\n\n\ndef f():\n    return 1\n")
    _write(root, "static/x.js", "fetch('/api/a');\n",
           raw_bytes=b"\xef\xbb\xbffetch('/api/a');\n")
    index = code_analysis.build_index(root)
    assert index["complete"] is True
    assert index["parse_errors"] == []
    assert {"app", "app.mod"} <= {m["name"] for m in index["modules"]}
    assert [j["path"] for j in index["js_api_calls"]] == ["/api/a"]
    # 本当の構文エラーのファイルは parse_errors に出る
    _write(root, "app/bad.py", "def broken(:\n")
    index2 = code_analysis.build_index(root)
    assert index2["complete"] is False
    assert [e["path"] for e in index2["parse_errors"]] == ["app/bad.py"]


def test_js_concat_and_template_normalized_and_matched(tmp_path):
    root = tmp_path / "js"
    _write(root, "app/api.py", (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/projects')\n"
        "\n\n"
        "@router.get('/{project_id}/mission')\n"
        "def get_mission():\n"
        "    return {}\n"))
    _write(root, "static/x.js", (
        "fetch('/api/projects/'+encodeURIComponent(pid)+'/mission');\n"
        "fetch(`/api/projects/${pid}/mission`);\n"
        "fetch('/api/projects/'+pid+'/x');\n"
        "fetch(url(file.path));\n"))
    index = code_analysis.build_index(root)
    full_paths = sorted(j["normalized_path"] for j in index["js_api_calls"])
    partial_paths = sorted(j["normalized_path"] for j in index["js_partial"])
    # {} が途中でも末尾がリテラルなら完全パス側に入る
    assert full_paths.count("/api/projects/{}/mission") == 2  # 連結・テンプレート両方
    assert "/api/projects/{}/x" in full_paths
    # 式が先頭(url(...))のものだけ unresolved に残る
    assert len(index["js_unresolved"]) == 1
    assert "url" in index["js_unresolved"][0]["raw"]
    mapping = code_analysis.route_js_map(index)
    # サーバと完全一致する結合パスは matched に入る
    assert [m["path"] for m in mapping["matched"]] == ["/api/projects/{}/mission"]
    # 末尾リテラルの完全パスは js_only 側に出る
    assert [e["path"] for e in mapping["js_only"]] == ["/api/projects/{}/x"]
    assert mapping["partial"] == []
    # unresolved は js_only に混ざらない
    for e in mapping["js_only"]:
        for j in e["js"]:
            assert "url(" not in j["raw"]


def test_router_prefix_and_include_prefix_resolved(tmp_path):
    root = tmp_path / "router"
    _write(root, "app/sub.py", (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/inner')\n"
        "\n\n"
        "@router.get('/items/{id}')\n"
        "def get_item():\n"
        "    return {}\n"))
    _write(root, "app/main.py", (
        "from fastapi import FastAPI\n"
        "from app.sub import router\n"
        "app = FastAPI()\n"
        "app.include_router(router, prefix='/api')\n"))
    index = code_analysis.build_index(root)
    by_func = {r["function"]: r for r in index["routes"]}
    assert by_func["get_item"]["path"] == "/api/inner/items/{id}"
    assert by_func["get_item"]["normalized_path"] == "/api/inner/items/{}"
    assert by_func["get_item"].get("path_resolved", True) is True


def test_dynamic_prefix_route_is_unresolved(tmp_path):
    root = tmp_path / "dyn"
    _write(root, "app/dyn.py", (
        "from fastapi import APIRouter\n"
        "PREFIX = '/dyn'\n"
        "router = APIRouter(prefix=PREFIX)\n"
        "\n\n"
        "@router.get('/items')\n"
        "def get_items():\n"
        "    return {}\n"))
    index = code_analysis.build_index(root)
    by_func = {r["function"]: r for r in index["routes"]}
    assert by_func["get_items"]["path_resolved"] is False
    assert "unresolved_reason" in by_func["get_items"]
    mapping = code_analysis.route_js_map(index)
    # server_only に混ぜず unresolved 側に出す
    assert mapping["server_only"] == []
    assert [r["function"] for r in mapping["server_unresolved"]] == ["get_items"]


def test_cli_default_is_summary_and_full_is_whole(tmp_path):
    import subprocess

    root = tmp_path / "cli"
    _write(root, "app/a.py", "X = 1\n")
    _write(root, "app/b.py", "import app.a\nY = 2\n")
    _write(root, "static/x.js", "fetch('/api/a');\n")
    base = str(Path(__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, "scripts/code_map.py", str(root)],
                       capture_output=True, text=True, cwd=base, timeout=60)
    assert r.returncode == 0
    summary = json.loads(r.stdout)
    assert summary["complete"] is True
    assert summary["counts"]["modules"] == 2
    assert "modules" not in summary and "calls" not in summary  # 要約は小さい
    assert len(r.stdout) < 5000
    r2 = subprocess.run([sys.executable, "scripts/code_map.py", str(root), "--full"],
                        capture_output=True, text=True, cwd=base, timeout=60)
    assert r2.returncode == 0
    full = json.loads(r2.stdout)
    assert len(full["modules"]) == 2 and "calls" in full and "defined" in full
    assert len(r2.stdout) > len(r.stdout)


def test_query_string_stripped_before_match(tmp_path):
    root = tmp_path / "q"
    _write(root, "app/api.py", (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n\n"
        "@app.get('/api/questions')\n"
        "def get_questions():\n"
        "    return {}\n"))
    _write(root, "static/x.js", (
        "fetch('/api/questions?limit=5');\n"
        "fetch(`/api/questions?offset=${n}`);\n"))
    index = code_analysis.build_index(root)
    assert sorted(j["normalized_path"] for j in index["js_api_calls"]) == [
        "/api/questions", "/api/questions"]
    mapping = code_analysis.route_js_map(index)
    assert [m["path"] for m in mapping["matched"]] == ["/api/questions"]
    assert mapping["js_only"] == []


def test_partial_paths_with_candidates_not_in_js_only(tmp_path):
    root = tmp_path / "partial"
    _write(root, "app/api.py", (
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/projects')\n"
        "\n\n"
        "@router.get('/{project_id}/lifecycle/log')\n"
        "def get_log():\n"
        "    return {}\n"
        "\n\n"
        "@router.get('/{project_id}/ocr/{job_id}')\n"
        "def get_ocr():\n"
        "    return {}\n"))
    _write(root, "static/x.js", (
        "fetch('/api/projects/'+pid+'/lifecycle'+path);\n"
        "fetch('/api/projects/'+pid+'/ocr/'+jid+rest);\n"
        "fetch('/generations');\n"
        "fetch('/api/nowhere-at-all');\n"))
    index = code_analysis.build_index(root)
    mapping = code_analysis.route_js_map(index)
    partial_paths = sorted(e["path"] for e in mapping["partial"])
    assert "/api/projects/{}/lifecycle{}" in partial_paths  # 末尾が変数
    assert "/api/projects/{}/ocr/{}{}" in partial_paths
    assert "/generations" in partial_paths  # /api/ で始まらない断片
    # 部分パスは js_only に混ざらない
    assert mapping["js_only"] == [] or all(
        e["path"] not in partial_paths for e in mapping["js_only"])
    assert [e["path"] for e in mapping["js_only"]] == ["/api/nowhere-at-all"]
    # 前方一致の候補が付く
    by_path = {e["path"]: e for e in mapping["partial"]}
    assert "/api/projects/{}/lifecycle/log" in by_path["/api/projects/{}/lifecycle{}"]["candidates"]
    assert "/api/projects/{}/ocr/{}" in by_path["/api/projects/{}/ocr/{}{}"]["candidates"]
    assert by_path["/generations"]["candidates"] == []


def test_wrapper_prefix_resolves_literal_and_partial(tmp_path):
    wrapper_cases = [
        ("arrow", "const url = path => '/api/projects/'+encodeURIComponent(pid)+path;\n"),
        ("func", "function url(path){return '/api/projects/'+encodeURIComponent(pid)+path;}\n"),
    ]
    for dirname, wrapper_line in wrapper_cases:
        root = tmp_path / dirname
        _write(root, "app/api.py", (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "\n\n"
            "@app.get('/api/projects/{pid}/mission')\n"
            "def get_mission():\n"
            "    return {}\n"))
        _write(root, "static/x.js", wrapper_line + (
            "fetch(url('/mission'));\n"
            "fetch(url(sub));\n"))
        index = code_analysis.build_index(root)
        mapping = code_analysis.route_js_map(index)
        # リテラル引数は完全なパスとして matched に入る
        assert [m["path"] for m in mapping["matched"]] == ["/api/projects/{}/mission"]
        matched_js = mapping["matched"][0]["js"]
        assert any(j.get("via_wrapper") == "url" for j in matched_js)
        # 動的な引数は partial になる
        partial_paths = [e["path"] for e in mapping["partial"]]
        assert "/api/projects/{}/{}" in partial_paths
        assert mapping["js_only"] == []


def test_complex_wrapper_stays_unresolved(tmp_path):
    root = tmp_path / "cplx"
    _write(root, "static/x.js", (
        "const url = path => cond ? '/a/'+path : '/b/'+path;\n"
        "fetch(url('/mission'));\n"))
    index = code_analysis.build_index(root)
    assert index["js_api_calls"] == [] and index["js_partial"] == []
    assert len(index["js_unresolved"]) == 1


def test_only_truly_missing_full_paths_are_js_only(tmp_path):
    root = tmp_path / "only"
    _write(root, "app/api.py", (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n\n"
        "@app.get('/api/real')\n"
        "def get_real():\n"
        "    return {}\n"))
    _write(root, "static/x.js", (
        "fetch('/api/real');\n"
        "fetch('/api/ghost');\n"))
    index = code_analysis.build_index(root)
    mapping = code_analysis.route_js_map(index)
    assert [m["path"] for m in mapping["matched"]] == ["/api/real"]
    assert [e["path"] for e in mapping["js_only"]] == ["/api/ghost"]
    assert mapping["partial"] == []


def test_server_only_caveat_depends_on_leftovers(tmp_path):
    root = tmp_path / "cav"
    _write(root, "app/api.py", (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n\n"
        "@app.get('/api/a')\n"
        "def get_a():\n"
        "    return {}\n"))
    _write(root, "static/x.js", "fetch('/api/a');\n")
    mapping = code_analysis.route_js_map(code_analysis.build_index(root))
    assert mapping["server_only"] == []
    assert mapping["server_only_caveat"] is None
    # unresolved が残れば caveat が出る
    _write(root, "static/y.js", "fetch(url);\n")
    mapping2 = code_analysis.route_js_map(code_analysis.build_index(root))
    assert mapping2["server_only_caveat"] is not None
    assert "unresolved" in mapping2["server_only_caveat"]
    # 区分の説明が含まれる
    assert set(mapping2["meanings"]) >= {
        "matched", "js_only", "server_only", "partial", "unresolved"}
