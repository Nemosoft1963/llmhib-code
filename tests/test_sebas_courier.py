"""運搬役(scripts/sebas_courier.py)の試験。HTTPは注入した偽関数。外部通信なし。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sebas_courier as sc  # noqa: E402

PKG = {"ok": True, "proposal_id": "p1", "status": "export_ready", "base_ref": "ref",
       "files": {"instruction.json": "{}", "REQUEST.md": "# r", "manifest.json": "{}"}}
GATE = [{"name": "full_tests", "passed": True, "detail": "x"}]


def _http(script):
    calls = []

    def http(method, url, body=None, timeout=60):
        calls.append((method, url, body))
        for key, resp in script:
            if key in url:
                return resp
        return 404, {"detail": "no route"}
    http.calls = calls
    return http


def _push(http, **kw):
    base = dict(start=False, auto_approve=False, source_project="LOCALSAPORTER", dry_run=False)
    base.update(kw)
    return sc.push("http://127.0.0.1:8099", "http://127.0.0.1:8012", "pj", "p1", "山田", http=http, **base)


def test_push_moves_files_in_order_and_records_submitted():
    http = _http([("/package", (200, PKG)), ("/sebas/intake/p1/prepare", (200, {"ok": True, "workcopy": "w__p1"})),
                  ("/sebas/intake", (200, {"ok": True, "proposal_id": "p1"})),
                  ("/submitted", (200, {"ok": True}))])
    out = _push(http, start=True, auto_approve=True)
    assert out["ok"] and out["workcopy"] == "w__p1"
    methods = [(m, u.rsplit("/", 1)[-1]) for m, u, _ in http.calls]
    assert methods == [("GET", "package"), ("POST", "intake"), ("POST", "prepare"), ("POST", "submitted")]
    prep = http.calls[2][2]
    assert prep["auto_approve"] is True and prep["start"] is True
    assert "files" not in json.dumps(out)  # 中身を結果に出さない


def test_push_stops_on_unexpected_response_and_never_records_submitted():
    for bad in [(409, {"detail": "状態が違います"}), (200, {"ok": True, "files": {"instruction.json": "x"}}),
                (200, {"ok": True, "files": {"instruction.json": "", "REQUEST.md": "a", "manifest.json": "b"}})]:
        http = _http([("/package", bad)])
        with pytest.raises(sc.CourierError):
            _push(http)
        assert not any(u.endswith("/submitted") for _, u, _ in http.calls)
    # 取込みが失敗したら「渡した」を記録しない
    http = _http([("/package", (200, PKG)), ("/sebas/intake", (422, {"detail": {"reason": "改ざん"}}))])
    with pytest.raises(sc.CourierError):
        _push(http)
    assert not any(u.endswith("/submitted") for _, u, _ in http.calls)


def test_push_dry_run_writes_nothing():
    http = _http([("/package", (200, PKG))])
    out = _push(http, dry_run=True)
    assert out["dry_run"] is True and [m for m, _, _ in http.calls] == ["GET"]


def test_deliver_requires_gate_and_records_delivery():
    built = {"ok": True, "delivery": {"proposal_id": "p1", "diff_text": "d"}, "gate": GATE,
             "changed_files": ["a"]}
    http = _http([("/delivery", (200, built))])
    # 同じ "/delivery" を両方が使うため、Jenkins と Sebas で分岐する偽関数にする
    seen = []

    def route(method, url, body=None, timeout=60):
        seen.append(url)
        if ":8012" in url:
            return 200, built
        return 200, {"ok": True, "verdict": "delivered"}
    out = sc.deliver("http://127.0.0.1:8099", "http://127.0.0.1:8012", "pj", "p1", "山田", dry_run=False, http=route)
    assert out["ok"] and out["verdict"] == "delivered" and any(g["passed"] for g in out["gate"])
    assert seen[0].startswith("http://127.0.0.1:8012") and seen[1].startswith("http://127.0.0.1:8099")


def test_deliver_refuses_when_gate_missing_or_failed_and_sends_nothing():
    sent = []

    def route_factory(built, status=200):
        def route(method, url, body=None, timeout=60):
            sent.append(url)
            return status, built
        return route
    # ゲートを通らない(422)
    r = route_factory({"detail": {"ok": False, "reason": "納品ゲートを通りません(納品しません)",
                                  "gate": [{"name": "fail_open_lint", "passed": False}]}}, 422)
    with pytest.raises(sc.CourierError) as e:
        sc.deliver("http://127.0.0.1:8099", "http://127.0.0.1:8012", "pj", "p1", "山田", dry_run=False, http=r)
    assert "fail_open_lint" in str(e.value)
    assert all(":8012" in u for u in sent)  # セバスへは送っていない
    # ゲートの結果が無い(None)納品は拒否
    sent.clear()
    r2 = route_factory({"ok": True, "delivery": {"a": 1}, "gate": None})
    with pytest.raises(sc.CourierError):
        sc.deliver("http://127.0.0.1:8099", "http://127.0.0.1:8012", "pj", "p1", "山田", dry_run=False, http=r2)
    assert all(":8012" in u for u in sent)


def test_only_local_hosts_allowed_and_actor_required():
    assert sc.check_local("http://127.0.0.1:8099/") == "http://127.0.0.1:8099"
    assert sc.check_local("http://localhost:8012") == "http://localhost:8012"
    for url in ["http://192.168.11.9:8099", "https://example.com", "http://evil.localhost.example:1", "ftp://127.0.0.1"]:
        with pytest.raises(sc.CourierError):
            sc.check_local(url)
    assert sc.main(["push", "--project", "a", "--proposal", "b", "--actor", " "]) == 2
    assert sc.main(["push", "--project", "a", "--proposal", "b", "--actor", "x",
                    "--sebas", "http://10.0.0.5:8099"]) == 2
