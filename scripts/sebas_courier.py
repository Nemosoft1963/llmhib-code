#!/usr/bin/env python3
"""セバス⇄ジェンキンスの運搬役(人が起動するコマンド。常駐しない)。

人がこのコマンドを実行したときだけ、ローカルの2つのAPIの間でファイルを運ぶ。
ジェンキンスからセバスへは接続しない(このスクリプトを実行する人が両方を呼ぶ)。

  push     セバスの指示パッケージを取得し、ジェンキンスへ取り込み、作業コピーを作り、
           セバスに「渡した」と記録する。--start で承認つきの作業セッションも開始する。
  deliver  ジェンキンスの納品ゲート(fail-open検査・全体テスト・ハッシュ順序を変えた再実行)を
           通った納品データを、セバスの delivery に記録する。ゲートを通らなければ何も送らない。

守ること:
  - 接続先はローカルホスト(127.0.0.1 / localhost / ::1)だけ。それ以外は拒否する。
  - 想定外の応答・読めない応答は、リトライせず中止して非0で終わる(成功扱いにしない)。
  - ファイルの中身・秘密値を画面に出さない(IDと状態とハッシュだけ)。
  - 記録だけで能力は有効にならない(セバス側の再判定 verify は、人が別に行う)。

使い方:
  python scripts/sebas_courier.py push    --project <PJ_ID> --proposal <提案ID> --actor 山田 [--start] [--auto-approve]
  python scripts/sebas_courier.py deliver --project <PJ_ID> --proposal <提案ID> --actor 山田
  (--sebas http://127.0.0.1:8099 --jenkins http://127.0.0.1:8012 は既定値。--dry-run は何も書かない)
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
DEFAULT_SEBAS = "http://127.0.0.1:8099"
DEFAULT_JENKINS = "http://127.0.0.1:8012"
HTTP_TIMEOUT_SEC = 60
DELIVERY_TIMEOUT_SEC = 3600  # 納品ゲートは全体テストを3回回すため長い


class CourierError(Exception):
    """運搬を中止する理由(日本語。内容・秘密を含めない)。"""


def check_local(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in {h.strip("[]") for h in LOCAL_HOSTS}:
        raise CourierError("接続先はローカルホストだけです: " + (host or "不明"))
    return url.rstrip("/")


def default_http(method: str, url: str, body: dict | None = None,
                 timeout: int = HTTP_TIMEOUT_SEC) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="strict")
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CourierError("接続できません: " + type(exc).__name__) from exc
    try:
        return status, json.loads(raw)
    except ValueError as exc:
        raise CourierError(f"応答を読めません(HTTP {status})") from exc


def _expect_ok(status: int, payload: Any, what: str) -> dict:
    if status != 200 or not isinstance(payload, dict):
        reason = ""
        if isinstance(payload, dict):
            detail = payload.get("detail", payload.get("reason", ""))
            reason = str(detail if not isinstance(detail, (dict, list)) else
                         (detail.get("reason") if isinstance(detail, dict) else ""))[:160]
        raise CourierError(f"{what}に失敗しました(HTTP {status}): {reason}")
    return payload


def push(sebas: str, jenkins: str, project: str, proposal: str, actor: str, *,
         start: bool, auto_approve: bool, source_project: str, dry_run: bool,
         backend: str | None = None, http: Callable = default_http) -> dict:
    base = f"{sebas}/api/projects/{urllib.parse.quote(project)}/capability-gaps/proposals/{urllib.parse.quote(proposal)}"
    status, pkg = http("GET", base + "/package")
    pkg = _expect_ok(status, pkg, "指示パッケージの取得")
    files = pkg.get("files")
    if not isinstance(files, dict) or set(files) != {"instruction.json", "REQUEST.md", "manifest.json"} \
            or not all(isinstance(v, str) and v for v in files.values()):
        raise CourierError("指示パッケージの形式を確認できません")
    if dry_run:
        return {"ok": True, "dry_run": True, "proposal_id": proposal, "status": pkg.get("status"),
                "base_ref": pkg.get("base_ref"), "would": ["取込み", "作業コピー作成", "セバスへ「渡した」記録"]}
    body = {"instruction_json": files["instruction.json"], "request_md": files["REQUEST.md"],
            "manifest_json": files["manifest.json"]}
    status, intake = http("POST", f"{jenkins}/sebas/intake", body)
    intake = _expect_ok(status, intake, "ジェンキンスへの取込み")
    if not intake.get("ok", True) or str(intake.get("proposal_id") or proposal) != proposal:
        raise CourierError("取込みの結果が提案と一致しません")
    prep_body = {"source_project": source_project, "start": bool(start),
                 "auto_approve": bool(auto_approve)}
    if backend:
        prep_body["backend"] = backend
    status, prepared = http("POST", f"{jenkins}/sebas/intake/{urllib.parse.quote(proposal)}/prepare", prep_body)
    prepared = _expect_ok(status, prepared, "作業コピーの作成")
    workcopy = str(prepared.get("workcopy") or "")
    if not workcopy:
        raise CourierError("作業コピーの名前を確認できません")
    status, sub = http("POST", base + "/submitted",
                       {"actor": actor, "memo": f"運搬役が取り込みました(作業コピー {workcopy})"})
    _expect_ok(status, sub, "セバスへの「渡した」記録")
    return {"ok": True, "proposal_id": proposal, "workcopy": workcopy,
            "session_started": bool(prepared.get("session")), "auto_approve": bool(auto_approve),
            "next": "ジェンキンスの作業が終わったら deliver を実行してください"}


def deliver(sebas: str, jenkins: str, project: str, proposal: str, actor: str, *,
            dry_run: bool, http: Callable = default_http) -> dict:
    if dry_run:
        return {"ok": True, "dry_run": True, "proposal_id": proposal,
                "would": ["納品ゲート(長時間)", "セバスの delivery に記録"]}
    status, built = http("POST", f"{jenkins}/sebas/intake/{urllib.parse.quote(proposal)}/delivery",
                         {"test_args": [], "run": True}, timeout=DELIVERY_TIMEOUT_SEC)
    if status != 200 or not isinstance(built, dict) or not built.get("ok"):
        detail = built.get("detail", built) if isinstance(built, dict) else {}
        gate = detail.get("gate") if isinstance(detail, dict) else None
        failed = [str(c.get("name")) for c in (gate or []) if isinstance(c, dict) and not c.get("passed")]
        reason = (detail.get("reason") if isinstance(detail, dict) else "") or "納品データを作れません"
        raise CourierError(f"納品しません: {str(reason)[:120]}"
                           + (f" 未通過: {', '.join(failed[:6])}" if failed else ""))
    if built.get("gate") is None:
        raise CourierError("納品ゲートの結果がありません(確認できず)。納品しません")
    delivery = built.get("delivery")
    if not isinstance(delivery, dict) or not delivery:
        raise CourierError("納品データを確認できません")
    url = (f"{sebas}/api/projects/{urllib.parse.quote(project)}/capability-gaps/proposals/"
           f"{urllib.parse.quote(proposal)}/delivery")
    status, rec = http("POST", url, {"actor": actor, "delivery": delivery})
    rec = _expect_ok(status, rec, "セバスへの納品の記録")
    return {"ok": True, "proposal_id": proposal, "verdict": rec.get("verdict"),
            "changed_files": len(built.get("changed_files") or []),
            "gate": [{"name": c.get("name"), "passed": c.get("passed")} for c in built["gate"]
                     if isinstance(c, dict)],
            "next": "セバスの画面で再判定(verify)を行ってください。能力はまだ有効ではありません"}


def main(argv: list[str] | None = None, http: Callable = default_http) -> int:
    ap = argparse.ArgumentParser(description="セバス⇄ジェンキンスの運搬役(人が起動する)")
    ap.add_argument("command", choices=("push", "deliver"))
    ap.add_argument("--project", required=True, help="セバスのPJ ID")
    ap.add_argument("--proposal", required=True, help="提案ID")
    ap.add_argument("--actor", required=True, help="担当者名(記録用)")
    ap.add_argument("--sebas", default=DEFAULT_SEBAS)
    ap.add_argument("--jenkins", default=DEFAULT_JENKINS)
    ap.add_argument("--source-project", default="LOCALSAPORTER", help="コピー元のジェンキンスのプロジェクト名")
    ap.add_argument("--start", action="store_true", help="作業セッションも開始する(承認つき)")
    ap.add_argument("--auto-approve", action="store_true",
                    help="作業コピーの中だけ、許可リストを自動承認する(範囲外は人の承認待ち)")
    ap.add_argument("--backend", default=None, help="作業セッションのバックエンド(--start のとき。例: meta)")
    ap.add_argument("--dry-run", action="store_true", help="何も書かず、予定だけを表示する")
    args = ap.parse_args(argv)
    try:
        if not args.actor.strip():
            raise CourierError("担当者名が必要です")
        sebas = check_local(args.sebas)
        jenkins = check_local(args.jenkins)
        if args.command == "push":
            result = push(sebas, jenkins, args.project, args.proposal, args.actor.strip(),
                          start=args.start, auto_approve=args.auto_approve,
                          source_project=args.source_project, dry_run=args.dry_run,
                          backend=args.backend, http=http)
        else:
            result = deliver(sebas, jenkins, args.project, args.proposal, args.actor.strip(),
                             dry_run=args.dry_run, http=http)
    except CourierError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
