"""セバス指示パッケージの取込みアダプターの試験(標準ライブラリのみ。外部通信なし)。"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import sebas_bridge as sb  # noqa: E402

PID = "abc123def456abc123def456abc123de"
FORBIDDEN = [
    ".env を読まない・書かない・転送しない",
    "/data 配下を読まない・書かない・転送しない",
    "docker-compose*.yml を変更しない",
    "docker/proxy/ 配下を変更しない",
    "既存の安全ゲート (承認・検証・公開境界) を弱体化しない",
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _package(base_ref="app-content-0123456789abcdef-files2", **overrides):
    instruction = {
        "schema": sb.SCHEMA_INSTRUCTION, "proposal_id": PID, "base_ref": base_ref,
        "required_capability_id": "proposal.example", "change_forbidden": FORBIDDEN,
        "acceptance_tests": ["架空の受入テスト"],
    }
    instruction.update(overrides.pop("instruction", {}))
    instruction_text = json.dumps(instruction, ensure_ascii=False)
    request_md = overrides.pop("request_md", "# 架空の依頼\n架空の機能を追加する。\n")
    manifest = {"schema": sb.SCHEMA_MANIFEST, "proposal_id": PID,
                "files": {"instruction.json": _sha(instruction_text), "REQUEST.md": _sha(request_md)}}
    manifest.update(overrides.pop("manifest", {}))
    return instruction_text, request_md, json.dumps(manifest, ensure_ascii=False)


def _make_source(root: Path, project="架空本体"):
    src = root / "projects" / project
    (src / "app").mkdir(parents=True)
    (src / "app" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (src / "app" / "b.py").write_text("OTHER = 2\n", encoding="utf-8")
    (src / "tests").mkdir()
    (src / "tests" / "test_a.py").write_text(
        "from app import a\n\n\ndef test_a():\n    assert a.VALUE == 1\n", encoding="utf-8")
    (src / ".env").write_text("SECRET_TOKEN=dummy-not-real\n", encoding="utf-8")
    (src / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (src / "data").mkdir()
    (src / "data" / "x.sqlite3").write_text("dummy", encoding="utf-8")
    return src


def _store(root: Path, base_ref):
    texts = _package(base_ref=base_ref)
    return sb.store_package(root, *texts), texts


# ---- 検査 ----------------------------------------------------------------

def test_verify_ok_and_tamper_detection():
    ins, req, man = _package()
    assert sb.verify_package(ins, req, man)["ok"] is True
    bad = sb.verify_package(ins, req + "追記", man)
    assert bad["ok"] is False and "SHA-256" in bad["reason"]
    bad = sb.verify_package(ins.replace("架空の受入テスト", "別のテスト"), req, man)
    assert bad["ok"] is False


@pytest.mark.parametrize("mutate", [
    {"instruction": {"schema": "x"}},
    {"instruction": {"proposal_id": "../evil"}},
    {"instruction": {"change_forbidden": []}},
    {"instruction": {"change_forbidden": [".env を読まない"]}},      # 必須の境界が欠けている
    {"instruction": {"acceptance_tests": []}},
    {"instruction": {"base_ref": ""}},
])
def test_verify_rejects_incomplete_instructions(mutate):
    ins, req, man = _package(**mutate)
    assert sb.verify_package(ins, req, man)["ok"] is False


def test_verify_rejects_garbage_and_empty():
    assert sb.verify_package("", "x", "{}")["ok"] is False
    assert sb.verify_package("{", "x", "{")["ok"] is False
    ins, req, man = _package()
    assert sb.verify_package(ins, req, man.replace(sb.SCHEMA_MANIFEST, "other"))["ok"] is False
    broken = json.loads(man)
    broken["proposal_id"] = "different"
    assert sb.verify_package(ins, req, json.dumps(broken))["ok"] is False
    broken = json.loads(man)
    broken["files"]["REQUEST.md"] = "zz"
    assert sb.verify_package(ins, req, json.dumps(broken))["ok"] is False


# ---- 保存 ----------------------------------------------------------------

def test_store_is_idempotent_and_refuses_different_content(tmp_path):
    ins, req, man = _package()
    first = sb.store_package(tmp_path, ins, req, man)
    assert first["ok"] and first["stored"] and not first["idempotent"]
    again = sb.store_package(tmp_path, ins, req, man)
    assert again["ok"] and again["idempotent"]
    other = _package(request_md="# 別内容\n")
    refused = sb.store_package(tmp_path, *other)
    assert refused["ok"] is False and "上書きしません" in refused["reason"]
    assert (tmp_path / "sebas_inbox" / PID / "REQUEST.md").read_text(encoding="utf-8") == req


def test_load_detects_tampering_after_store(tmp_path):
    ins, req, man = _package()
    sb.store_package(tmp_path, ins, req, man)
    assert sb.load_package(tmp_path, PID)["ok"] is True
    (tmp_path / "sebas_inbox" / PID / "REQUEST.md").write_text("改ざん", encoding="utf-8")
    assert sb.load_package(tmp_path, PID)["ok"] is False
    assert sb.load_package(tmp_path, "../x")["ok"] is False
    assert sb.load_package(tmp_path, "notexist")["ok"] is False


# ---- 基準版・作業コピー ---------------------------------------------------

def test_base_ref_matches_sebas_rule_and_unknown_when_unreadable(tmp_path):
    src = _make_source(tmp_path)
    ref = sb.base_ref_of(src)
    assert ref.startswith("app-content-") and ref.endswith("-files2")
    assert sb.base_ref_of(tmp_path / "nothing") == "unknown"
    (src / "app" / "a.py").write_text("VALUE = 9\n", encoding="utf-8")
    assert sb.base_ref_of(src) != ref


def test_prepare_refuses_when_base_ref_differs(tmp_path):
    _make_source(tmp_path)
    stored, _ = _store(tmp_path, "app-content-deadbeefdeadbeef-files2")
    assert stored["ok"]
    res = sb.prepare_workcopy(tmp_path, "架空本体", PID)
    assert res["ok"] is False and "再同期" in res["reason"]
    assert not (tmp_path / "projects" / sb.workcopy_name("架空本体", PID)).exists()


def test_prepare_never_copies_secrets_and_is_idempotent(tmp_path):
    src = _make_source(tmp_path)
    _store(tmp_path, sb.base_ref_of(src))
    res = sb.prepare_workcopy(tmp_path, "架空本体", PID)
    assert res["ok"] and not res["idempotent"]
    work = tmp_path / "projects" / res["workcopy"]
    assert (work / "app" / "a.py").exists()
    assert not (work / ".env").exists()
    assert not (work / "data").exists()
    assert not list(work.rglob("*.sqlite3"))
    info = json.loads((work / ".sebas_intake.json").read_text(encoding="utf-8"))
    assert info["proposal_id"] == PID and len(info["base_commit"]) == 40
    again = sb.prepare_workcopy(tmp_path, "架空本体", PID)
    assert again["ok"] and again["idempotent"] and again["base_commit"] == info["base_commit"]
    # コピー元は変わらない
    assert (src / ".env").read_text(encoding="utf-8").startswith("SECRET_TOKEN")


def test_prepare_refuses_to_overwrite_foreign_workcopy(tmp_path):
    src = _make_source(tmp_path)
    _store(tmp_path, sb.base_ref_of(src))
    name = sb.workcopy_name("架空本体", PID)
    (tmp_path / "projects" / name).mkdir()
    res = sb.prepare_workcopy(tmp_path, "架空本体", PID)
    assert res["ok"] is False


# ---- 納品データ -----------------------------------------------------------

def test_build_delivery_matches_sebas_hash_rule_and_forbidden_areas(tmp_path):
    src = _make_source(tmp_path)
    _store(tmp_path, sb.base_ref_of(src))
    prepared = sb.prepare_workcopy(tmp_path, "架空本体", PID)
    work = tmp_path / "projects" / prepared["workcopy"]
    # 変更なし → 納品しない
    assert sb.build_delivery(tmp_path, PID, run=False)["ok"] is False
    (work / "app" / "c.py").write_text("NEW = 3\n", encoding="utf-8")
    res = sb.build_delivery(tmp_path, PID, run=False)
    assert res["ok"] is True
    d = res["delivery"]
    assert d["proposal_id"] == PID and d["base_commit"] == sb.base_ref_of(src)
    assert len(d["result_commit"]) == 40 and "app/c.py" in d["diff_text"]
    # セバスの正規形と同じハッシュ
    material = {"diff_text": d["diff_text"], "artifact_text": "", "files": {}}
    expected = hashlib.sha256((json.dumps(material, ensure_ascii=False, sort_keys=True) + "\n")
                              .encode("utf-8")).hexdigest()
    assert d["artifact_sha256"] == expected
    assert d["capability_ids"] == ["proposal.example"]
    assert res["tests_passed"] is False   # run=False ではテスト未実行


def test_build_delivery_refuses_forbidden_changes(tmp_path):
    src = _make_source(tmp_path)
    _store(tmp_path, sb.base_ref_of(src))
    prepared = sb.prepare_workcopy(tmp_path, "架空本体", PID)
    work = tmp_path / "projects" / prepared["workcopy"]
    (work / "docker-compose.yml").write_text("services: {changed: 1}\n", encoding="utf-8")
    res = sb.build_delivery(tmp_path, PID, run=False)
    assert res["ok"] is False and "禁止" in res["reason"]
    assert "docker-compose.yml" in res["banned"]


def test_run_tests_real_pytest_and_summary_parsing(tmp_path):
    src = _make_source(tmp_path)
    _store(tmp_path, sb.base_ref_of(src))
    prepared = sb.prepare_workcopy(tmp_path, "架空本体", PID)
    work = tmp_path / "projects" / prepared["workcopy"]
    (work / "app" / "c.py").write_text("NEW = 3\n", encoding="utf-8")
    res = sb.build_delivery(tmp_path, PID, test_args=["tests/test_a.py"], run=True)
    assert res["ok"] is True
    tests = res["delivery"]["test_results"]
    assert tests["total"] == 1 and tests["failed"] == 0 and tests["passed"] is True
    # 失敗するテストは passed=False、failure_tail が付く
    (work / "tests" / "test_b.py").write_text("def test_b():\n    assert False\n", encoding="utf-8")
    res2 = sb.build_delivery(tmp_path, PID, test_args=["tests/test_b.py"], run=True)
    assert res2["ok"] is True
    t2 = res2["delivery"]["test_results"]
    assert t2["passed"] is False and t2["failed"] == 1 and t2["failure_tail"]
    assert res2["tests_passed"] is False


def test_parse_pytest_summary_unreadable_is_zero_total():
    assert sb.parse_pytest_summary("")["total"] == 0
    assert sb.parse_pytest_summary("garbage")["total"] == 0
    parsed = sb.parse_pytest_summary("..\n3 passed, 1 skipped in 0.5s")
    assert parsed["total"] == 3 and parsed["failed"] == 0 and parsed["skipped"] == 1
    parsed = sb.parse_pytest_summary("1 failed, 2 passed in 1.2s")
    assert parsed["total"] == 3 and parsed["failed"] == 1


def test_no_network_or_credentials_code():
    text = Path(sb.__file__).read_text(encoding="utf-8")
    for needle in ("requests", "httpx", "urllib", "socket", "http.client"):
        assert needle not in text
    assert "import subprocess" in text   # git / pytest の実行だけ
    assert subprocess is not None
