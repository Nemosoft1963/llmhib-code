"""用途別モデル選択の試験(標準ライブラリ+pytestのみ。外部通信なし)。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import model_profiles as mp  # noqa: E402
from src import sessions  # noqa: E402


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("MODEL_PROFILES_PATH", raising=False)


def test_classify_heavy_light_standard():
    assert mp.classify_task("認証まわりの設計を見直して、全体をリファクタする")[0] == "heavy"
    assert mp.classify_task("この関数の場所を教えて")[0] == "light"
    assert mp.classify_task("ログイン画面にボタンを追加して")[0] == "standard"
    assert mp.classify_task("x" * 2000)[0] == "heavy"
    # 軽い語でも長い依頼は軽くしない
    assert mp.classify_task("調査して" + "あ" * 400)[0] == "standard"


def test_resolve_known_and_unknown_backend():
    model, note = mp.resolve_model("gemini", "heavy")
    assert model == "gemini-3.1-pro-preview" and note == ""
    # 対応表に無いバックエンドは既定モデルへ戻り、理由が残る
    model, note = mp.resolve_model("ollama", "heavy")
    assert model and "既定モデル" in note
    model, note = mp.resolve_model("gemini", "bogus")
    assert "確認できない" in note


def test_override_file_valid_invalid_and_missing(tmp_path, monkeypatch):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"gemini": {"heavy": "gemini-x-1", "light": "bad name!", "zzz": "m"}}),
                 encoding="utf-8")
    monkeypatch.setenv("MODEL_PROFILES_PATH", str(f))
    table, warnings = mp.load_table()
    assert table["gemini"]["heavy"] == "gemini-x-1"
    assert table["gemini"]["light"] == mp.DEFAULT_TABLE["gemini"]["light"]  # 不正値は既定
    assert len(warnings) == 2
    f.write_text("{broken", encoding="utf-8")
    table, warnings = mp.load_table()
    assert table["gemini"] == mp.DEFAULT_TABLE["gemini"] and warnings
    monkeypatch.setenv("MODEL_PROFILES_PATH", str(tmp_path / "none.json"))
    table, warnings = mp.load_table()
    assert table["gemini"] == mp.DEFAULT_TABLE["gemini"] and warnings


def test_resolve_tier_request():
    assert mp.resolve_tier_request(None, "t") == ("", "")
    assert mp.resolve_tier_request("", "t") == ("", "")
    assert mp.resolve_tier_request("heavy", "t") == ("heavy", "指定")
    assert mp.resolve_tier_request("AUTO", "設計を見直す")[0] == "heavy"
    with pytest.raises(ValueError):
        mp.resolve_tier_request("ultra", "t")


def test_next_tier():
    assert mp.next_tier("light") == "standard"
    assert mp.next_tier("heavy") is None
    assert mp.next_tier("x") is None


def _session(**kw):
    base = dict(id="s", project="p", backend="gemini", model="gemini-3.8-flash",
                max_iterations=5, require_approval=False, tier="standard", auto_escalate=True)
    base.update(kw)
    return sessions.Session(**base)


def _errs(n=1):
    return [{"id": str(i), "name": "run_command", "content": "エラー: 失敗"} for i in range(n)]


def test_escalates_once_after_consecutive_errors():
    s = _session()
    for _ in range(sessions.ESCALATE_AFTER_ERRORS - 1):
        sessions._maybe_escalate_tier(s, _errs())
    assert s.model == "gemini-3.8-flash" and s.escalations == 0
    sessions._maybe_escalate_tier(s, _errs())
    assert s.model == "gemini-3.1-pro-preview" and s.tier == "heavy" and s.escalations == 1
    assert s.transcript[-1]["type"] == "tier_escalation"
    # 2回目は上げない(最上位でもあり、1回まで)
    for _ in range(5):
        sessions._maybe_escalate_tier(s, _errs())
    assert s.escalations == 1


def test_success_resets_streak_and_manual_tier_never_escalates():
    s = _session()
    sessions._maybe_escalate_tier(s, _errs())
    sessions._maybe_escalate_tier(s, [{"id": "1", "name": "read_file", "content": "ok"}])
    assert s.error_streak == 0
    m = _session(auto_escalate=False)
    for _ in range(10):
        sessions._maybe_escalate_tier(m, _errs())
    assert m.model == "gemini-3.8-flash" and m.escalations == 0
    # 一部だけエラーのターンは数えない
    p = _session()
    for _ in range(10):
        sessions._maybe_escalate_tier(p, _errs() + [{"id": "z", "name": "x", "content": "ok"}])
    assert p.escalations == 0


def test_session_roundtrip_keeps_tier_fields():
    import dataclasses
    s = _session(tier="light", tier_reason="指定", escalations=1, error_streak=2)
    r = sessions._session_from_dict(dataclasses.asdict(s))
    assert (r.tier, r.tier_reason, r.auto_escalate, r.escalations, r.error_streak) == (
        "light", "指定", True, 1, 2)
    # tier 項目が無い旧データも読める
    d = dataclasses.asdict(s)
    for k in ("tier", "tier_reason", "auto_escalate", "escalations", "error_streak"):
        d.pop(k)
    assert sessions._session_from_dict(d).tier == ""


def test_api_model_profiles_and_bad_tier():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from src import main

    client = TestClient(main.app)
    r = client.get("/model-profiles")
    assert r.status_code == 200
    body = r.json()
    assert body["tiers"] == ["light", "standard", "heavy"] and "gemini" in body["table"]
    bad = client.post("/agent/start", json={"task": "x", "backend": "gemini", "tier": "ultra"})
    assert bad.status_code == 400


def test_model_name_validation_and_direct_model_in_api():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from src import main

    assert mp.is_valid_model_name("qwen3-coder:30b") and mp.is_valid_model_name("a/b.c_d-1")
    for bad in ["", "x y", "a;b", "../x", "a" * 101, None, 5]:
        assert mp.is_valid_model_name(bad) is False
    client = TestClient(main.app)
    r = client.post("/agent/start", json={"task": "x", "backend": "ollama", "model": "bad name;"})
    assert r.status_code == 400
