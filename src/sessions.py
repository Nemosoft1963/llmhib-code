"""タスク実行セッション。

副作用のあるツール(write_file / run_command / 危険なMCPツール)は、承認されるまで
実行しない「承認待ち」状態でループを一時停止できる。承認結果を受け取って再開すると、
チェックポイント(gitコミット)を作成してから続きを実行する。

各セッションは1つの「プロジェクト」(tools_core.Workspace)に紐づく。プロジェクトが
異なればファイル・gitチェックポイント履歴は完全に分離される。

セッションはプロセスメモリ上(SESSIONS)に保持しつつ、同じ内容をSQLite
(session_store.py、workspace_data上の永続ボリューム)にも都度書き込む。
起動時に`load_persisted_sessions()`で読み戻すため、コンテナ再起動をまたいでも
会話(進行中の承認待ちも含む)が失われない。

実行はバックグラウンドスレッドで行う(start_session/continue_session/resume_session は
すぐに戻り、status="running"のセッションを返す)。呼び出し側(Web UI)は
`GET /agent/{id}` をポーリングして進行状況(transcript・iterations)をリアルタイムに
確認できる。互換用の一括実行API(/agent/run)だけは例外で、旧来通り完了まで
ブロックする(start_sessionのbackground=Falseで指定)。
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from . import backend_defaults, checkpoints, elaborate as elaborate_mod, mcp_tools, session_store
from .tools_core import DEFAULT_PROJECT, Workspace, get_workspace

logger = logging.getLogger("code_agent.sessions")

BUILTIN_TOOL_NAMES = {"read_file", "write_file", "edit_file", "list_directory", "run_command"}

# 副作用があり、承認が必要な組み込みツール
BUILTIN_APPROVAL_REQUIRED_TOOLS = {"write_file", "edit_file", "run_command"}

# セッション(会話の記録)を保持する期間(秒)。既定の0は「自動削除しない」で、
# 会話はSQLite(session_store.py)に永続的に残る。以前は6時間で自動削除しており、
# 開発指示や作業経緯の記録が消える問題があったため、既定を無期限に変更した。
# 環境変数SESSION_TTL_SECに正の値を設定すれば、その秒数を過ぎた会話を削除する。
SESSION_TTL_SEC = int(os.environ.get("SESSION_TTL_SEC", "0"))

SessionStatus = Literal["running", "waiting_approval", "done", "error"]


def _tool_needs_approval(name: str) -> bool:
    """このツール呼び出しに承認が必要か判定する。

    組み込みツールは固定リスト、MCPツールは名前に含まれるキーワードによる
    ヒューリスティック(mcp_tools.is_risky)で判定する。
    """
    if name in BUILTIN_APPROVAL_REQUIRED_TOOLS:
        return True
    if mcp_tools.is_mcp_tool(name):
        return mcp_tools.is_risky(name)
    return False


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    needs_approval: bool
    approved: bool | None = None  # None = 未決定
    result: str | None = None


@dataclass
class Session:
    id: str
    project: str
    backend: str
    model: str
    max_iterations: int
    require_approval: bool
    elaborate: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    status: SessionStatus = "running"
    current_turn: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    iterations: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    # このセッションでこれまでに使った(≒失敗して引き継いだ)バックエンド名の履歴。
    # 自動引き継ぎ(_try_handoff)が、同じ失敗したバックエンドへ戻らないようにするために使う。
    tried_backends: list[str] = field(default_factory=list)
    # 調査モード: 読み取り(read_file/list_directory)以外のツールはサーバー側で必ず拒否する。
    # 完了時に最終報告を analysis_out(プロジェクト内の相対パス)へ保存する。
    read_only: bool = False
    analysis_out: str | None = None


SESSIONS: dict[str, Session] = {}

# セッションの状態遷移(特に「running」への切り替え)をアトミックにするためのロック。
# 複数リクエストがほぼ同時に同じセッションへ/continueや/decisionを送った場合に、
# バックグラウンドループが二重に起動されるのを防ぐ。全セッション共通の1個で十分
# (遷移のチェック&更新自体は一瞬で終わるため、ロックの粒度を細かくする必要が無い)。
_status_lock = threading.Lock()


def _begin_running(session: Session, allowed_from: set[str]) -> None:
    """session.statusがallowed_fromに含まれていればrunningへ遷移させる。

    含まれていなければValueErrorを投げる(呼び出し元のエラーメッセージ用)。
    チェックと更新をロックで挟むことで、同じセッションに対する2つのリクエストが
    両方とも「まだrunningじゃない」と判定してバックグラウンドループを二重起動する
    競合を防ぐ。
    """
    with _status_lock:
        if session.status not in allowed_from:
            if session.status == "waiting_approval":
                raise ValueError("承認待ちの操作が残っています。先に承認/却下してください。")
            if session.status == "running":
                raise ValueError("実行中です。完了を待ってください。")
            raise ValueError(f"この操作はできません(status={session.status})")
        session.status = "running"


def _start_background(session: Session, work) -> None:
    threading.Thread(target=_safe_run, args=(session, work), daemon=True).start()


def _safe_run(session: Session, work) -> None:
    """バックグラウンドスレッドの実行本体。ここで捕まえない例外はスレッドを黙って
    落とすだけ(誰にも通知されない)になってしまうため、必ず捕まえてセッションの
    エラー状態として記録する。"""
    try:
        work()
    except Exception as e:
        logger.exception("バックグラウンド実行中にエラーが発生しました: session=%s", session.id)
        session.status = "error"
        session.error = _error_diagnosis(e)
        _persist(session)


def _persist(session: Session) -> None:
    """現在のセッション状態をSQLiteへ保存する。呼び出し側の処理は失敗しても止めない。"""
    session_store.save(
        session.id, session.project, session.created_at, dataclasses.asdict(session)
    )


def _session_from_dict(d: dict[str, Any]) -> Session:
    current_turn = [ToolCall(**c) for c in d.get("current_turn", [])]
    return Session(
        id=d["id"],
        project=d["project"],
        backend=d["backend"],
        model=d["model"],
        max_iterations=d["max_iterations"],
        require_approval=d["require_approval"],
        elaborate=d.get("elaborate", False),
        read_only=d.get("read_only", False),
        analysis_out=d.get("analysis_out"),
        messages=d.get("messages", []),
        transcript=d.get("transcript", []),
        status=d.get("status", "done"),
        current_turn=current_turn,
        final_text=d.get("final_text", ""),
        iterations=d.get("iterations", 0),
        error=d.get("error"),
        created_at=d.get("created_at", time.time()),
        tried_backends=d.get("tried_backends", []),
    )


def load_persisted_sessions() -> None:
    """起動時に呼ぶ。SQLiteに保存済みの会話をSESSIONSへ読み戻す。

    コンテナ再起動をまたいでも、進行中の承認待ちセッションを含めて会話が
    続けられるようにするための処理。個々のセッションの復元に失敗しても
    他のセッションの復元は続行する。
    """
    restored = 0
    for data in session_store.load_all():
        try:
            session = _session_from_dict(data)
        except Exception:
            logger.exception("セッションの復元に失敗しました(スキップ): %s", data.get("id"))
            continue
        SESSIONS[session.id] = session
        restored += 1
    if restored:
        logger.info("永続化されたセッションを%d件復元しました", restored)
    _cleanup_old_sessions()


def _cleanup_old_sessions() -> None:
    if SESSION_TTL_SEC <= 0:
        return  # 自動削除しない(記録を永続的に残す)
    cutoff = time.time() - SESSION_TTL_SEC
    stale = [sid for sid, s in SESSIONS.items() if s.created_at < cutoff]
    for sid in stale:
        del SESSIONS[sid]
        session_store.delete(sid)


READ_ONLY_TOOLS = {"read_file", "list_directory"}


def _execute(
    workspace: Workspace, name: str, args: dict[str, Any], read_only: bool = False
) -> str:
    if read_only and name not in READ_ONLY_TOOLS:
        return (
            f"エラー: 調査モード(読み取り専用)のため {name} は実行できません。"
            "read_file と list_directory だけを使って調査し、結果は最終報告としてテキストで出力してください。"
        )
    if mcp_tools.is_mcp_tool(name):
        return mcp_tools.call_tool(name, args)

    if name not in BUILTIN_TOOL_NAMES:
        return f"エラー: 未知のツールです: {name}"
    fn = getattr(workspace, name)
    try:
        return str(fn(**args))
    except TypeError as e:
        return f"エラー: 引数が不正です: {e}"
    except Exception as e:  # ツール実行中の予期しない例外もモデルに返して継続させる
        logger.exception("ツール実行中にエラー: %s", name)
        return f"エラー: ツール実行中に例外が発生しました: {e}"


def _extract_text(assistant_msg: dict[str, Any]) -> str:
    content = assistant_msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if btype == "text":
                text = b.get("text") if isinstance(b, dict) else getattr(b, "text", "")
                parts.append(text or "")
        return "".join(parts)
    return ""


def _backend_module(name: str):
    """バックエンド名から、call_model/init_messages/build_tool_result_messages を持つ
    モジュールを返す。call_model系だけ import が重い(clientの生成等)ため遅延import する。
    """
    from .backends import claude_step, gemini_step, grok_step, meta_step, ollama_step, openai_step

    modules = {
        "claude": claude_step,
        "ollama": ollama_step,
        "openai": openai_step,
        "gemini": gemini_step,
        "grok": grok_step,
        "meta": meta_step,
    }
    module = modules.get(name)
    if module is None:
        raise ValueError(f"未知のbackendです: {name} (ollama, claude, openai, gemini, grok, meta のいずれか)")
    return module


def _finalize_turn(session: Session, workspace: Workspace) -> None:
    """current_turnの全呼び出しが承認/実行済みになったら、結果をmessagesにまとめて追加する。

    実行後のプロジェクトを毎回git commitしにいき、実際に差分があった場合だけ
    チェックポイントとして記録する(何が変更されたかをツール名で判定する必要がない
    ため、組み込みツールに限らずMCPツール(例: sqlite書き込み)の変更も拾える)。
    """
    results = [
        {"id": c.id, "name": c.name, "content": c.result or ""} for c in session.current_turn
    ]

    backend = _backend_module(session.backend)
    session.messages.extend(backend.build_tool_result_messages(results))

    summary = "; ".join(
        f"{c.name}({', '.join(f'{k}={v!r}' for k, v in c.args.items())})"
        for c in session.current_turn
        if c.approved
    )
    if summary:
        sha = checkpoints.commit_checkpoint(workspace.root, f"checkpoint: {summary[:200]}")
        if sha:
            session.transcript.append(
                {"type": "checkpoint", "sha": sha, "message": summary[:200]}
            )

    session.current_turn = []


def _run_loop(session: Session) -> None:
    """LLMを呼び、承認待ちが発生するか完了するまでループを進める。

    現在のバックエンドが続けられなくなった場合(トークン/レート制限/残量不足等の
    APIエラー、またはmax_iterations到達)、_try_handoffで別のバックエンドへ自動的に
    切り替えて続行を試みる(backend_defaults.FAILOVER_PRIORITY参照)。すべて試して
    ダメだった場合のみ、実際にエラー/打ち切りとして終了する。
    """
    workspace = get_workspace(session.project)

    while True:
        if session.iterations >= session.max_iterations:
            if _try_handoff(session, "ツール呼び出し回数の上限(max_iterations)に達しました"):
                continue
            session.status = "done"
            session.error = "max_iterationsに到達したため打ち切りました"
            _save_analysis(session, workspace, partial=True)
            _persist(session)
            return

        session.iterations += 1
        step_fn = _backend_module(session.backend).call_model
        try:
            assistant_msgs, tool_calls_raw = step_fn(session.messages, session.model)
        except Exception as e:
            if _is_failover_worthy(e) and _try_handoff(session, _error_diagnosis(e)):
                continue
            raise

        session.messages.extend(assistant_msgs)
        text = "".join(_extract_text(m) for m in assistant_msgs)
        if text:
            session.transcript.append({"type": "text", "text": text})

        if not tool_calls_raw:
            session.status = "done"
            session.final_text = text
            _save_analysis(session, workspace)
            _persist(session)
            return

        current_turn: list[ToolCall] = []
        for raw in tool_calls_raw:
            needs_approval = session.require_approval and _tool_needs_approval(raw["name"])
            call = ToolCall(
                id=raw["id"], name=raw["name"], args=raw["args"], needs_approval=needs_approval
            )
            current_turn.append(call)
            session.transcript.append(
                {
                    "type": "tool_use",
                    "name": call.name,
                    "input": call.args,
                    "needs_approval": needs_approval,
                }
            )

        session.current_turn = current_turn

        if any(c.needs_approval for c in current_turn):
            # 1件でも承認が必要な呼び出しがあれば、ターン全体を保留する。
            # (同一ターン内の呼び出しは依存関係がありうる — 例: create_table→write_query→read_query —
            #  ので、承認不要なものだけ先に実行すると順序が崩れて誤った結果を返しかねない)
            session.status = "waiting_approval"
            _persist(session)
            return

        # 承認不要な呼び出しだけのターン: 元の順序どおりそのまま実行する
        for call in current_turn:
            call.result = _execute(workspace, call.name, call.args, session.read_only)
            call.approved = True

        _finalize_turn(session, workspace)


def _save_analysis(session: Session, workspace: Workspace, partial: bool = False) -> None:
    """調査モードの最終報告を、プロジェクト内の docs/analysis/ に保存してチェックポイントを残す。"""
    if not session.read_only or not session.analysis_out:
        return
    text = session.final_text or next(
        (t["text"] for t in reversed(session.transcript) if t.get("type") == "text"), ""
    )
    if not text.strip():
        return
    header = (
        f"<!-- 調査モード(読み取り専用) backend={session.backend} model={session.model} "
        f"session={session.id} {'(途中打ち切り)' if partial else ''} -->\n\n"
    )
    target = workspace._resolve(session.analysis_out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(header + text, encoding="utf-8")
    checkpoints.commit_checkpoint(workspace.root, f"analysis: {session.analysis_out}")


def _build_context_summary(
    session: Session, max_chars: int = 3000, exclude_last: bool = True
) -> str:
    """これまでの会話の流れの要約。具体化ステップと自動引き継ぎ(handoff)の両方で使う。

    「進めて」のような短い継続指示だけを文脈無しでフロントAIに見せると、
    無関係な新しいタスクを捏造することが実際に確認されている(例: VRM表示アプリの
    続きを頼んだのに、Djangoの文字列反転関数を作れという全く別の指示にすり替わった)。
    それを防ぐため、直近のユーザー発話・具体化結果・担当AIの発言を要約して渡す。

    exclude_last=True(具体化ステップ用)の場合、session.transcriptの末尾は今回の
    (これから具体化する)ユーザー発話なので除外する。exclude_last=False(引き継ぎ用)の
    場合はそのまま全件を対象にする。
    """
    items = session.transcript[:-1] if exclude_last else session.transcript
    parts: list[str] = []
    for item in items:
        t = item.get("type")
        if t == "user_message":
            parts.append(f"[ユーザー] {item.get('text', '')}")
        elif t == "elaboration" and item.get("ok"):
            parts.append(f"[具体化された指示] {item.get('elaborated', '')}")
        elif t == "text":
            parts.append(f"[担当AIの発言] {item.get('text', '')}")
        elif t == "handoff":
            parts.append(
                f"[担当AI引き継ぎ] {item.get('from_backend')} → {item.get('to_backend')}"
                f"(理由: {item.get('reason')})"
            )
    joined = "\n".join(parts)
    if len(joined) > max_chars:
        joined = "…(前略)…\n" + joined[-max_chars:]
    return joined


# (キーワード, 判定用の小文字断片群, 日本語の分かりやすい説明+対処)。
# 上から順に最初に一致したものを採用する。「別のバックエンドでなら続けられる
# 可能性がある」エラーは全てfailover対象(_is_failover_worthyがTrueを返す)。
_ERROR_PATTERNS: list[tuple[tuple[str, ...], str]] = [
    (
        ("credit balance", "insufficient_quota", "insufficient quota", "purchase credits", "billing"),
        "APIの利用残高(クレジット)が不足しています。管理画面で支払い情報・残高を確認してください。",
    ),
    (
        ("rate limit", "rate_limit", "429", "too many requests"),
        "レート制限に達しました(短時間のリクエストが多すぎます)。しばらく待つか、"
        "他のAIへの自動切り替えをお待ちください。",
    ),
    (
        ("resource_exhausted", "quota"),
        "APIの利用枠(クォータ)の上限に達しました。",
    ),
    (
        ("context_length", "context length", "maximum context", "too long", "token limit"),
        "会話が長くなりすぎて、このAIの上限(コンテキスト長)を超えました。",
    ),
    (
        ("invalid api key", "invalid_api_key", "unauthorized", "authenticationerror"),
        "APIキーが無効です。.envの設定(該当するAPIキー)を確認してください。",
    ),
    (
        ("overloaded", "unavailable", "connection", "timeout", "connect"),
        "APIサーバーへの接続に失敗しました(ネットワークまたはサーバー側の一時的な問題の可能性)。",
    ),
    (
        ("model", "not found", "does not exist"),
        "指定されたモデル名が見つかりません。.envのモデル設定を確認してください。",
    ),
]


def _match_error_pattern(e: Exception) -> str | None:
    name = type(e).__name__.lower()
    msg = str(e).lower()
    haystack = f"{name} {msg}"
    for keywords, explanation in _ERROR_PATTERNS:
        if any(k in haystack for k in keywords):
            return explanation
    return None


def _is_failover_worthy(e: Exception) -> bool:
    """トークン/レート制限/残量不足/コンテキスト長超過など、「今のバックエンドでは
    続けられないが、別のバックエンドでなら続けられる可能性がある」エラーかどうかを
    判定する。_ERROR_PATTERNS(上記)に該当すれば対象とする。
    """
    return _match_error_pattern(e) is not None


def _error_diagnosis(e: Exception) -> str:
    """例外を人間にわかりやすい日本語の診断+対処に変換する(該当パターンが無ければ
    元のクラス名+メッセージをそのまま使う)。handoffの理由表示・最終エラー表示の
    両方で使う共通のフォーマット。
    """
    explanation = _match_error_pattern(e)
    detail = f"{type(e).__name__}: {e}"
    return f"{explanation}({detail})" if explanation else detail


def _try_handoff(session: Session, reason: str) -> bool:
    """フロントAIの制御の下、現在のバックエンドが続けられなくなった場合に、
    優先順リスト(backend_defaults.FAILOVER_PRIORITY)の中からまだ試していない
    利用可能なバックエンドへ切り替える。

    バックエンドごとにメッセージ形式が異なる(role/content形式のものとGeminiの
    steps形式)ため、生の会話履歴はそのまま引き継がない。代わりにこれまでの
    作業内容を要約し、新しいバックエンドへの「引き継ぎタスク」として渡し、
    会話をそこから再構築する。

    切り替えられたらTrue(呼び出し側は同じターンをやり直す)、
    これ以上候補が無ければFalseを返す。
    """
    for candidate in backend_defaults.FAILOVER_PRIORITY:
        if candidate in session.tried_backends:
            continue
        if not backend_defaults.is_backend_available(candidate):
            continue

        old_backend, old_model = session.backend, session.model
        _, new_model = backend_defaults.resolve_backend_and_model(candidate)
        context = _build_context_summary(session, exclude_last=False)
        handoff_task = (
            "これまでの作業の続きです。以下の内容を踏まえて、この時点から実装を"
            f"引き継いで進めてください(直前の担当AI({old_backend})から、次の理由で"
            f"引き継がれました: {reason})。\n\n### これまでの作業内容\n{context}"
        )

        session.backend = candidate
        session.model = new_model
        session.tried_backends.append(candidate)
        session.iterations = 0
        session.messages = _backend_module(candidate).init_messages(handoff_task)
        session.transcript.append(
            {
                "type": "handoff",
                "from_backend": old_backend,
                "from_model": old_model,
                "to_backend": candidate,
                "to_model": new_model,
                "reason": reason,
            }
        )
        _persist(session)
        logger.warning(
            "バックエンドを自動的に切り替えました: %s(%s) -> %s(%s) 理由: %s",
            old_backend, old_model, candidate, new_model, reason,
        )
        return True
    return False


def _maybe_elaborate(session: Session, task: str) -> str:
    """session.elaborate が有効なら、ローカルLLMで作業指示を具体化してから返す。

    これまでの会話の流れを背景情報として渡すことで、「進めて」のような短い継続指示を
    正しく解釈させる(渡さないと無関係な新規タスクを捏造することがある)。
    具体化前後の内容は transcript に "elaboration" として必ず記録する(ユーザーから
    見えない書き換えをしない)。ローカルLLMの呼び出しに失敗した場合は、その旨を
    記録した上で元の指示にフォールバックする(このステップの失敗でタスク全体を
    止めない)。
    """
    if not session.elaborate:
        return task

    context = _build_context_summary(session)
    try:
        elaborated = elaborate_mod.elaborate(task, context=context or None)
    except elaborate_mod.ElaborationError as e:
        logger.warning("作業指示の具体化に失敗したため、元の指示を使用します: %s", e)
        session.transcript.append(
            {
                "type": "elaboration",
                "original": task,
                "elaborated": None,
                "ok": False,
                "error": str(e),
            }
        )
        return task

    session.transcript.append(
        {
            "type": "elaboration",
            "original": task,
            "elaborated": elaborated,
            "ok": True,
        }
    )
    return elaborated


def start_session(
    task: str,
    backend: str,
    model: str,
    project: str = DEFAULT_PROJECT,
    max_iterations: int = 20,
    require_approval: bool = True,
    elaborate: bool = False,
    background: bool = True,
    read_only: bool = False,
    analysis_out: str | None = None,
) -> Session:
    """新しいセッションを開始する。

    background=True(既定、対話UI用)の場合、セッション作成後すぐに戻り、実行は
    バックグラウンドスレッドで進む。呼び出し側は `GET /agent/{id}` をポーリングして
    進行状況を確認する。background=Falseの場合は完了(またはエラー)までブロックする
    (互換用の一括実行API `/agent/run` 専用。例外はそのまま呼び出し元へ伝播する)。
    """
    _cleanup_old_sessions()

    workspace = get_workspace(project)  # 不正なプロジェクト名はここで例外になる
    checkpoints.ensure_repo(workspace.root)

    session = Session(
        id=uuid.uuid4().hex,
        project=project,
        backend=backend,
        model=model,
        max_iterations=max_iterations,
        require_approval=False if read_only else require_approval,
        elaborate=elaborate,
        # 調査モードは指定バックエンドだけで実施する(自動引き継ぎで別のAIへ渡さない)
        tried_backends=(
            ["claude", "ollama", "openai", "gemini", "grok", "meta"] if read_only else [backend]
        ),
        read_only=read_only,
        analysis_out=analysis_out,
    )
    session.transcript.append({"type": "user_message", "text": task})
    SESSIONS[session.id] = session
    _persist(session)  # running状態を即座に保存(ポーリングですぐ見えるように)

    def _work() -> None:
        effective_task = _maybe_elaborate(session, task)
        session.messages = _backend_module(backend).init_messages(effective_task)
        _run_loop(session)

    if background:
        _start_background(session, _work)
    else:
        _work()
    return session


def continue_session(
    session_id: str, task: str, max_iterations: int | None = None
) -> Session:
    """既存セッションに新しい指示(ユーザー発話)を追加し、会話を継続する。

    これまでの会話履歴(session.messages)は保持したまま、チャットの次のターンとして
    続きを実行する。対話形式のUIで「次の指示」を送るときに使う。すぐに戻り、実行は
    バックグラウンドスレッドで進む(`GET /agent/{id}` でポーリングする)。
    """
    session = SESSIONS.get(session_id)
    if session is None:
        raise KeyError(session_id)

    _begin_running(session, allowed_from={"done", "error"})

    session.transcript.append({"type": "user_message", "text": task})
    if max_iterations is not None:
        session.max_iterations = max_iterations
    session.iterations = 0
    session.final_text = ""
    session.error = None
    _persist(session)

    def _work() -> None:
        effective_task = _maybe_elaborate(session, task)
        session.messages.append(
            _backend_module(session.backend).build_user_message(effective_task)
        )
        _run_loop(session)

    _start_background(session, _work)
    return session


def resume_session(session_id: str, decisions: dict[str, bool]) -> Session:
    """decisions: {tool_call_id: approved} で承認/却下を反映し、ループを再開する。

    承認要否の判定に関わらず、current_turn内の全呼び出しは"実行はまだしていない"状態で
    保留されている。決定が出そろったら、元の順序どおりに実行する(承認不要だったものは
    ここで初めて実行される)。同一ターン内の呼び出しの依存関係(例: create_table→
    write_query→read_query)を壊さないための設計。全員分の決定が揃うまでは
    waiting_approvalのまま即座に返す。揃ったら、実行・再開はバックグラウンドスレッドで進む。
    """
    session = SESSIONS.get(session_id)
    if session is None:
        raise KeyError(session_id)
    if session.status != "waiting_approval":
        raise ValueError(f"セッションは承認待ちではありません(status={session.status})")

    for call in session.current_turn:
        if not call.needs_approval or call.approved is not None:
            continue
        if call.id not in decisions:
            continue
        call.approved = decisions[call.id]

    if any(c.needs_approval and c.approved is None for c in session.current_turn):
        _persist(session)
        return session  # まだ一部未決定 -> waiting_approvalのまま

    _begin_running(session, allowed_from={"waiting_approval"})
    _persist(session)

    def _work() -> None:
        workspace = get_workspace(session.project)
        for call in session.current_turn:
            if call.approved is False:
                call.result = "ユーザーがこの操作を却下しました。"
            else:
                call.result = _execute(workspace, call.name, call.args, session.read_only)
                call.approved = True
        _finalize_turn(session, workspace)
        _run_loop(session)

    _start_background(session, _work)
    return session


def get_session(session_id: str) -> Session:
    session = SESSIONS.get(session_id)
    if session is None:
        raise KeyError(session_id)
    return session


def to_public_dict(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "project": session.project,
        "backend": session.backend,
        "model": session.model,
        "elaborate": session.elaborate,
        "status": session.status,
        "iterations": session.iterations,
        "max_iterations": session.max_iterations,
        "final_text": session.final_text,
        "error": session.error,
        "transcript": session.transcript,
        "pending": [
            {"id": c.id, "name": c.name, "args": c.args}
            for c in session.current_turn
            if c.needs_approval and c.approved is None
        ],
    }
