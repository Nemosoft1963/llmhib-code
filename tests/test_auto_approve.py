"""作業コピー内の自動承認 (D-2) の試験。範囲外は必ず人の承認待ちになる。外部通信なし。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import sessions  # noqa: E402


class _WS:
    def __init__(self, root):
        self.root = root


def _session(auto=True, read_only=False):
    return sessions.Session(id="s", project="p", backend="meta", model="m", max_iterations=5,
                            require_approval=True, auto_approve=auto, read_only=read_only)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".sebas_intake.json").write_text("{}", encoding="utf-8")
    return _WS(tmp_path)


@pytest.mark.parametrize("path", ["app/a.py", "tests/test_x.py", "docs/n.md", "./app/b.py"])
def test_edits_inside_workcopy_are_auto_approved(ws, path):
    ok, why = sessions.auto_approvable(_session(), ws, "write_file", {"path": path})
    assert ok is True and why


@pytest.mark.parametrize("path", [
    ".env", ".env.example", "docker-compose.yml", "docker-compose.override.yml",
    "docker/proxy/nginx.conf", "data/x.db", ".git/config", ".venv/bin/python",
    "../outside.py", "/etc/passwd", "C:/Windows/x", "app/../../x", "", ".sebas_intake.json",
    "backups/a.tar"])
def test_forbidden_or_outside_paths_need_human(ws, path):
    ok, why = sessions.auto_approvable(_session(), ws, "edit_file", {"path": path})
    assert ok is False and why


@pytest.mark.parametrize("cmd", [
    "pytest -q", "python -m pytest -q tests/test_a.py", ".venv/bin/python -m pytest -q",
    "python scripts/fail_open_lint.py --json --changed app/a.py", "git status", "git diff HEAD",
    "git log --oneline"])
def test_allowed_commands(ws, cmd):
    ok, _ = sessions.auto_approvable(_session(), ws, "run_command", {"command": cmd})
    assert ok is True


@pytest.mark.parametrize("cmd", [
    "pip install requests", "curl http://x", "rm -rf .", "git push", "git commit -am x",
    "git checkout .", "pytest -q; rm -rf /", "pytest && ls", "pytest | cat", "pytest > out.txt",
    "python -c 'print(1)'", "python /tmp/x.py", "git -C / status", "git diff --output=x",
    "pytest $(id)", "python -m pytest ../other", "node x.js", "", "ls"])
def test_other_commands_need_human(ws, cmd):
    ok, _ = sessions.auto_approvable(_session(), ws, "run_command", {"command": cmd})
    assert ok is False


def test_disabled_by_default_and_for_read_only_and_non_workcopy(tmp_path, ws):
    assert sessions.auto_approvable(_session(auto=False), ws, "write_file", {"path": "app/a.py"})[0] is False
    assert sessions.auto_approvable(_session(read_only=True), ws, "write_file", {"path": "app/a.py"})[0] is False
    plain = _WS(tmp_path / "other")
    (tmp_path / "other").mkdir()
    ok, why = sessions.auto_approvable(_session(), plain, "write_file", {"path": "app/a.py"})
    assert ok is False and "作業コピーではありません" in why
    # MCPなど対象外のツールは自動承認しない
    assert sessions.auto_approvable(_session(), ws, "mcp__sqlite__write_query", {})[0] is False


def test_session_roundtrip_keeps_auto_approve():
    import dataclasses
    s = _session()
    assert sessions._session_from_dict(dataclasses.asdict(s)).auto_approve is True
    d = dataclasses.asdict(s)
    d.pop("auto_approve")
    assert sessions._session_from_dict(d).auto_approve is False


def _loop_session(tmp_path, monkeypatch, auto):
    """偽のバックエンド(1回目に書込み+禁止領域の書込み、2回目に終了)で、実際のループを回す。"""
    from src import tools_core

    monkeypatch.setattr(tools_core, "PROJECTS_ROOT", tmp_path)
    real_ws = tools_core.Workspace("proj")
    root = real_ws.root
    (root / ".sebas_intake.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sessions, "get_workspace", lambda project: real_ws)
    monkeypatch.setattr(sessions.checkpoints, "commit_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "_persist", lambda session: None)

    class Fake:
        calls = 0

        @staticmethod
        def init_messages(task):
            return [{"role": "user", "content": task}]

        @staticmethod
        def build_tool_result_messages(results):
            return [{"role": "user", "content": str(results)}]

        @classmethod
        def call_model(cls, messages, model):
            cls.calls += 1
            if cls.calls == 1:
                return ([{"role": "assistant", "content": "w"}], [
                    {"id": "1", "name": "write_file", "args": {"path": "app/ok.py", "content": "X = 1\n"}},
                    {"id": "2", "name": "write_file", "args": {"path": ".env", "content": "A=1\n"}},
                ])
            return ([{"role": "assistant", "content": "done"}], [])

    monkeypatch.setattr(sessions, "_backend_module", lambda name: Fake)
    s = sessions.Session(id="s", project="p", backend="meta", model="m", max_iterations=5,
                         require_approval=True, auto_approve=auto)
    s.messages = Fake.init_messages("t")
    return s, root


def test_loop_auto_approves_in_scope_but_holds_turn_when_any_call_is_out_of_scope(tmp_path, monkeypatch):
    s, root = _loop_session(tmp_path, monkeypatch, auto=True)
    sessions._run_loop(s)
    # 同一ターンに範囲外(.env)があるので、ターン全体が人の承認待ちになり、何も実行されない
    assert s.status == "waiting_approval"
    assert not (root / "app" / "ok.py").exists() and not (root / ".env").exists()
    pending = [c for c in s.current_turn if c.needs_approval and c.approved is None]
    assert [c.args["path"] for c in pending] == [".env"]
    assert any(t["type"] == "auto_approve_declined" for t in s.transcript)


def test_loop_without_auto_approve_waits_for_human(tmp_path, monkeypatch):
    s, root = _loop_session(tmp_path, monkeypatch, auto=False)
    sessions._run_loop(s)
    assert s.status == "waiting_approval"
    assert len([c for c in s.current_turn if c.needs_approval]) == 2
    assert not any(t["type"] in ("auto_approved", "auto_approve_declined") for t in s.transcript)


def test_loop_runs_automatically_when_all_calls_are_in_scope(tmp_path, monkeypatch):
    s, root = _loop_session(tmp_path, monkeypatch, auto=True)
    # 範囲内だけのターンにする
    orig = sessions._backend_module(None)

    def only_ok(messages, model, _o=orig):
        if _o.calls == 0:
            _o.calls = 1
            return ([{"role": "assistant", "content": "w"}], [
                {"id": "1", "name": "write_file", "args": {"path": "app/ok.py", "content": "X = 1\n"}}])
        return ([{"role": "assistant", "content": "done"}], [])

    monkeypatch.setattr(orig, "call_model", staticmethod(only_ok))
    sessions._run_loop(s)
    assert s.status == "done"
    assert (root / "app" / "ok.py").read_text(encoding="utf-8") == "X = 1\n"
    assert any(t["type"] == "auto_approved" for t in s.transcript)
