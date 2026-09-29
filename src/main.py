"""FastAPI エントリーポイント。

プロジェクトAPI(/projects)、承認フロー対応のセッションAPI(/agent/start,
/agent/{id}/decision, /agent/{id})、チェックポイントAPI(/checkpoints, .../diff,
.../restore)、互換用の一括実行API(/agent/run, 承認なしですべて自動実行)を提供する。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import anthropic
import httpx
import ollama
import openai
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field

from . import analysis, backend_defaults, checkpoints, deploy, importer, mcp_tools, sessions, triz_inputs, writeback
from .tools_core import DEFAULT_PROJECT, PROJECTS_ROOT, WORKSPACE_ROOT
from .tools_core import get_project_meta as _get_project_meta
from .tools_core import get_workspace as _get_workspace
from .tools_core import list_projects as _list_projects
from .tools_core import set_project_meta as _set_project_meta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("code_agent.api")

app = FastAPI(title="ジェンキンス(汎用コード作成エージェント)", version="0.3.0")

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.on_event("startup")
def _migrate_legacy_workspace() -> None:
    """プロジェクト機能導入前(workspace直下が単一の作業場所だった頃)のデータを
    `projects/default/` へ一度だけ移行する。既にprojects/があれば何もしない。"""
    if PROJECTS_ROOT.exists():
        return
    legacy_entries = [p for p in WORKSPACE_ROOT.iterdir() if p.name != "projects"]
    default_dir = PROJECTS_ROOT / DEFAULT_PROJECT
    default_dir.mkdir(parents=True, exist_ok=True)
    if not legacy_entries:
        return
    for entry in legacy_entries:
        shutil.move(str(entry), str(default_dir / entry.name))
    logger.info(
        "プロジェクト機能導入前のworkspaceを default プロジェクトへ移行しました(%d件)",
        len(legacy_entries),
    )


@app.on_event("startup")
def _init_checkpoints_repo() -> None:
    """起動時に必ず default プロジェクトのgitリポジトリを初期化する。

    書き込みが起きた"後"に遅延初期化すると、その書き込みが「初期状態」コミットに
    巻き込まれてチェックポイントとして記録されない事故が起きるため、起動時に固定する。
    (他のプロジェクトは start_session 時に遅延初期化される)
    """
    checkpoints.ensure_repo(_get_workspace(DEFAULT_PROJECT).root)


@app.on_event("startup")
def _load_persisted_sessions() -> None:
    """起動時にSQLite(session_store.py)から会話セッションを読み戻す。

    コンテナ再起動でチャットの会話状態が消えて「エージェントが停止した」ように
    見えていた問題への対処。workspace_data(永続named volume)上のsessions.dbに
    保存されているため、再起動後もそのまま会話を継続できる。
    """
    sessions.load_persisted_sessions()


@app.on_event("startup")
def _init_mcp() -> None:
    """起動時にMCPサーバー(mcp_servers.json)へ接続する。初回はパッケージのダウンロードで
    時間がかかることがある。接続に失敗しても組み込みツールだけでアプリは起動を続ける。

    注意: 現状MCPツール(Web検索・SQLite)はプロジェクトに依存しない。SQLiteは
    `projects/default/agent.db` に固定されている(README参照)。
    """
    mcp_tools.start()


_resolve_backend_and_model = backend_defaults.resolve_backend_and_model


class ProjectCreateRequest(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=100, description="プロジェクト名(日本語可、パス区切り文字等は不可)"
    )


class ImportPreviewRequest(BaseModel):
    source_path: str = Field(..., min_length=1, max_length=1000, description="取り込み元(ホスト側の既存フォルダ)")
    include_dirs: list[str] = Field(
        default_factory=list, description="既定で除外する生成物ディレクトリのうち、含めたいディレクトリ名"
    )


class ImportRequest(ImportPreviewRequest):
    name: str = Field(..., min_length=1, max_length=100, description="作成するプロジェクト名")


class ExportPathRequest(BaseModel):
    export_path: str = Field(
        ..., min_length=1, max_length=1000, description="成果物のエクスポート先ホストパス(自由記述)"
    )


class StartRequest(BaseModel):
    task: str = Field(..., min_length=1, description="エージェントに依頼するコーディングタスク")
    project: str = Field(DEFAULT_PROJECT, description="対象プロジェクト名")
    max_iterations: int = Field(20, ge=1, le=50, description="ツール呼び出しループの上限")
    backend: str | None = Field(
        None, description='"ollama" / "claude" / "openai" / "gemini" / "grok" / "meta"。省略時は LLM_BACKEND 環境変数(既定 ollama)'
    )
    require_approval: bool = Field(
        True, description="write_file / edit_file / run_command の実行前にユーザー承認を求めるか"
    )
    elaborate: bool = Field(
        False,
        description=(
            "作業指示をまずローカルLLM(Ollama)で具体化してから、実装担当のbackendへ渡すか。"
            "曖昧な一行の依頼が、ラベルだけそれらしい実装になるのを防ぐための前処理。"
        ),
    )


class DecisionRequest(BaseModel):
    approve_all: bool | None = Field(
        None, description="trueならpending全件を承認、falseなら全件却下"
    )
    decisions: dict[str, bool] | None = Field(
        None, description="tool_call_id -> 承認(true)/却下(false) の個別指定"
    )


class ContinueRequest(BaseModel):
    task: str = Field(..., min_length=1, description="会話の続きとして送る次の指示")
    max_iterations: int | None = Field(
        None, ge=1, le=50, description="このターンのループ上限(省略時は前回の設定を維持)"
    )


class TaskRequest(BaseModel):
    """/agent/run (承認なしの一括実行、互換用) のリクエスト。"""

    task: str = Field(..., min_length=1, description="エージェントに依頼するコーディングタスク")
    project: str = Field(DEFAULT_PROJECT, description="対象プロジェクト名")
    max_iterations: int = Field(20, ge=1, le=50, description="ツール呼び出しループの上限")
    backend: str | None = Field(
        None, description='"ollama" / "claude" / "openai" / "gemini" / "grok" / "meta"。省略時は LLM_BACKEND 環境変数(既定 ollama)'
    )


def _handle_backend_errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"見つかりません: {e}") from e
    # --- Claude backend ---
    except anthropic.AuthenticationError:
        logger.exception("認証エラー")
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEYが無効です")
    except anthropic.RateLimitError as e:
        logger.exception("レートリミット")
        raise HTTPException(
            status_code=429, detail="レートリミットに達しました。しばらくしてから再試行してください"
        ) from e
    except anthropic.APIStatusError as e:
        logger.exception("Claude APIエラー")
        raise HTTPException(status_code=502, detail=f"Claude APIエラー: {e.message}") from e
    except anthropic.APIConnectionError as e:
        logger.exception("Claude接続エラー")
        raise HTTPException(status_code=502, detail="Claude APIへの接続に失敗しました") from e
    # --- Ollama backend ---
    except ollama.ResponseError as e:
        logger.exception("Ollamaエラー")
        detail = f"Ollamaエラー: {e.error}"
        if "not found" in (e.error or "").lower():
            detail += " (`docker exec llmhib-ollama ollama pull <model>` でモデルを取得してください)"
        raise HTTPException(status_code=502, detail=detail) from e
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        logger.exception("Ollama接続エラー")
        raise HTTPException(
            status_code=502, detail="Ollamaサーバーに接続できません。コンテナが起動しているか確認してください"
        ) from e
    # --- OpenAI / Grok backend ---
    # xAI(Grok)はOpenAI互換API(openai SDKをbase_url差し替えで使用)のため、
    # 例外クラスはOpenAIと共通。どちらのbackendで発生したかはここでは区別できないため、
    # メッセージには両方のキー名を併記する。
    except openai.AuthenticationError:
        logger.exception("OpenAI/Grok認証エラー")
        raise HTTPException(
            status_code=500, detail="OPENAI_API_KEY / XAI_API_KEY / LLAMA_API_KEY のいずれか(使用中のバックエンド)が無効です"
        )
    except openai.RateLimitError as e:
        logger.exception("OpenAI/Grokレートリミット")
        raise HTTPException(
            status_code=429,
            detail="OpenAI/Grokのレートリミットに達しました。しばらくしてから再試行してください",
        ) from e
    except openai.NotFoundError as e:
        logger.exception("OpenAI/Grokモデルエラー")
        raise HTTPException(
            status_code=502, detail=f"OpenAI/Grok APIエラー(モデル未検出の可能性): {e}"
        ) from e
    except openai.APIStatusError as e:
        logger.exception("OpenAI/Grok APIエラー")
        raise HTTPException(status_code=502, detail=f"OpenAI/Grok APIエラー: {e}") from e
    except openai.APIConnectionError as e:
        logger.exception("OpenAI/Grok接続エラー")
        raise HTTPException(status_code=502, detail="OpenAI/Grok APIへの接続に失敗しました") from e
    # --- Gemini backend ---
    # google-genai SDKは詳細な例外クラスを分けておらず、APIError(code, message)に
    # まとめられているため、HTTPステータス相当のcodeで分岐する。
    except genai_errors.APIError as e:
        code = getattr(e, "code", None)
        logger.exception("Gemini APIエラー(code=%s)", code)
        if code in (401, 403):
            raise HTTPException(status_code=500, detail="GEMINI_API_KEYが無効です") from e
        if code == 429:
            raise HTTPException(
                status_code=429, detail="Geminiのレートリミットに達しました。しばらくしてから再試行してください"
            ) from e
        raise HTTPException(status_code=502, detail=f"Gemini APIエラー: {e}") from e


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    """Web UI(プロジェクト選択・タスク投入・承認・チェックポイント・ファイル閲覧)を返す。"""
    return INDEX_HTML


@app.get("/mcp/tools")
def mcp_tools_list() -> dict:
    """接続済みMCPサーバーのツール一覧(スキーマ)を返す。"""
    return {"tools": mcp_tools.get_tool_schemas()}


@app.get("/health")
def health() -> dict:
    return {"status": "healthy"}


# --- プロジェクト ---


@app.get("/projects")
def projects_list() -> dict[str, Any]:
    """既存プロジェクト名の一覧を返す(無ければ default のみ)。"""
    return {"projects": _list_projects()}


@app.post("/projects")
def projects_create(req: ProjectCreateRequest) -> dict[str, Any]:
    """新しいプロジェクト(空のディレクトリ + gitリポジトリ)を作成する。既存名なら何もしない。"""

    def _run():
        workspace = _get_workspace(req.name)
        checkpoints.ensure_repo(workspace.root)
        return {"project": workspace.project}

    return _handle_backend_errors(_run)


@app.post("/projects/import/preview")
def projects_import_preview(req: ImportPreviewRequest) -> dict[str, Any]:
    """取り込み前の確認: 含まれるファイル数と、自動除外されるものの一覧(コピーはしない)。"""
    return _handle_backend_errors(lambda: importer.preview(req.source_path, req.include_dirs))


@app.post("/projects/import")
def projects_import(req: ImportRequest) -> dict[str, Any]:
    """既存フォルダを新しいプロジェクトへコピー(元は変更しない)。機密は自動除外する。"""
    return _handle_backend_errors(
        lambda: importer.import_project(req.name, req.source_path, req.include_dirs)
    )


@app.post("/projects/{name}/inputs/triz")
def project_attach_triz(name: str) -> dict[str, Any]:
    """TRIZ情報収集エージェントの承認済みエクスポートを、プロジェクトの inputs/ へ保存する。"""
    return _handle_backend_errors(lambda: triz_inputs.attach_triz_export(name))


class AnalyzeRequest(BaseModel):
    backend: str | None = Field(None, description='調査を行うバックエンド。省略時は "claude"')
    max_iterations: int = Field(80, ge=5, le=300)


@app.post("/projects/{name}/analyze")
def project_analyze(name: str, req: AnalyzeRequest) -> dict[str, Any]:
    """読み取り専用の調査を開始する。進行は GET /agent/{session_id} で確認し、
    完了すると docs/analysis/ に報告が保存される。"""

    def _run():
        session = analysis.start_analysis(name, req.backend, req.max_iterations)
        return {"session_id": session.id, "backend": session.backend, "report_path": session.analysis_out}

    return _handle_backend_errors(_run)


class WritebackBundleRequest(BaseModel):
    include: list[str] | None = Field(None, description="書き戻すパスを絞る場合に指定(省略時は全変更)")


@app.get("/projects/{name}/writeback")
def project_writeback_preview(name: str) -> dict[str, Any]:
    """取り込み時点からの変更一覧(追加/変更/削除)。元フォルダには触れない。"""
    return _handle_backend_errors(lambda: writeback.compute_changes(name))


@app.post("/projects/{name}/writeback/bundle")
def project_writeback_bundle(name: str, req: WritebackBundleRequest) -> dict[str, Any]:
    """書き戻し用バンドル(変更ファイル + manifest)を作る。適用はホスト側の scripts/apply-writeback.ps1。"""
    return _handle_backend_errors(lambda: writeback.build_bundle(name, req.include))


@app.get("/projects/{name}")
def project_get(name: str) -> dict[str, Any]:
    """プロジェクトの情報(現在のエクスポート先設定など)を返す。"""

    def _run():
        workspace = _get_workspace(name)  # 存在確認・名前検証
        meta = _get_project_meta(name)
        return {"project": workspace.project, "export_path": meta.get("export_path")}

    return _handle_backend_errors(_run)


@app.put("/projects/{name}/export-path")
def project_set_export_path(name: str, req: ExportPathRequest) -> dict[str, Any]:
    """成果物のエクスポート先(任意のホストパス、NAS/ローカルどちらでも可)を保存する。

    このパスはラベルとして保存するだけで、コンテナ内からは検証も書き込みもしない
    (コンテナはホストの任意パスに直接アクセスできないため)。実際のコピーは
    GET /projects/{name}/export-command が返すコマンドをホスト側で実行する。
    """

    def _run():
        _get_workspace(name)  # 存在確認・名前検証
        meta = _set_project_meta(name, export_path=req.export_path)
        return {"project": name, "export_path": meta.get("export_path")}

    return _handle_backend_errors(_run)


@app.get("/projects/{name}/export-command")
def project_export_command(name: str) -> dict[str, Any]:
    """設定済みのエクスポート先へコピーするための、ホスト側で実行するコマンドを返す。

    コンテナ自身はホストの任意フォルダへ書き込めないため、実行は利用者自身
    (このPCのターミナル)またはClaude Codeに依頼して行う想定。
    """

    def _run():
        _get_workspace(name)  # 存在確認・名前検証
        meta = _get_project_meta(name)
        export_path = meta.get("export_path")
        if not export_path:
            raise ValueError(
                "エクスポート先が未設定です。先に PUT /projects/{name}/export-path で設定してください。"
            )
        command = f'docker compose cp "code-agent:/app/workspace/projects/{name}/." "{export_path}"'
        return {"project": name, "export_path": export_path, "command": command}

    return _handle_backend_errors(_run)


# --- ファイル閲覧 ---


@app.get("/files")
def list_files(project: str = DEFAULT_PROJECT) -> dict:
    """プロジェクト配下のファイル一覧(相対パス)を返す。UIのファイルツリー用。"""

    def _run():
        return {"files": _get_workspace(project).list_files_recursive()}

    return _handle_backend_errors(_run)


@app.get("/files/content")
def file_content(path: str, project: str = DEFAULT_PROJECT) -> dict:
    """プロジェクト配下のファイル内容を返す。pathはプロジェクト外にアクセスできない。"""

    def _run():
        return {"path": path, "content": _get_workspace(project).read_file(path)}

    return _handle_backend_errors(_run)


# --- セッションAPI(承認フロー) ---


@app.post("/agent/start")
def agent_start(req: StartRequest) -> dict[str, Any]:
    """新しいタスクセッションを開始する。承認待ちになるか完了するまで進める。"""

    def _run():
        backend, model = _resolve_backend_and_model(req.backend)
        session = sessions.start_session(
            task=req.task,
            backend=backend,
            model=model,
            project=req.project,
            max_iterations=req.max_iterations,
            require_approval=req.require_approval,
            elaborate=req.elaborate,
        )
        return sessions.to_public_dict(session)

    return _handle_backend_errors(_run)


@app.get("/agent/{session_id}")
def agent_get(session_id: str) -> dict[str, Any]:
    """セッションの現在の状態を返す(ポーリング用)。"""

    def _run():
        session = sessions.get_session(session_id)
        return sessions.to_public_dict(session)

    return _handle_backend_errors(_run)


@app.post("/agent/{session_id}/decision")
def agent_decision(session_id: str, req: DecisionRequest) -> dict[str, Any]:
    """保留中のツール呼び出しを承認/却下し、ループを再開する。"""

    def _run():
        session = sessions.get_session(session_id)
        if req.approve_all is not None:
            decisions = {c.id: req.approve_all for c in session.current_turn}
        else:
            decisions = req.decisions or {}
        updated = sessions.resume_session(session_id, decisions)
        return sessions.to_public_dict(updated)

    return _handle_backend_errors(_run)


@app.post("/agent/{session_id}/continue")
def agent_continue(session_id: str, req: ContinueRequest) -> dict[str, Any]:
    """完了済みセッションに次の指示を送り、会話として継続する(チャットUI用)。"""

    def _run():
        session = sessions.continue_session(session_id, req.task, req.max_iterations)
        return sessions.to_public_dict(session)

    return _handle_backend_errors(_run)


# --- チェックポイントAPI ---


@app.get("/checkpoints")
def checkpoints_list(project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    def _run():
        return {"checkpoints": checkpoints.list_checkpoints(_get_workspace(project).root)}

    return _handle_backend_errors(_run)


@app.get("/checkpoints/{sha}/diff")
def checkpoints_diff(sha: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    def _run():
        return {"sha": sha, "diff": checkpoints.diff_checkpoint(_get_workspace(project).root, sha)}

    return _handle_backend_errors(_run)


@app.post("/checkpoints/{sha}/restore")
def checkpoints_restore(sha: str, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    def _run():
        return checkpoints.restore_checkpoint(_get_workspace(project).root, sha)

    return _handle_backend_errors(_run)


# --- デプロイ&動作テスト(固定手順、LLMのツールとしては公開しない) ---


@app.post("/projects/{name}/deploy")
def project_deploy(name: str) -> dict[str, Any]:
    """プロジェクトを実際にビルドして動作テストし、本番相当のコンテナへ反映する。

    Web UIの明示的な操作(ボタン)からのみ呼ばれる決め打ちの手順:
    1) ワークスペース→デプロイ用ステージングへコピー(必ずビルドの前に実行する)
    2) docker build
    3) tests/ があれば、ビルド直後のイメージに対してpytestを実行(失敗時は本番へ反映しない)
    4) 既存コンテナを新イメージへ入れ替え
    5) コンテナ内から自分自身のポートへHTTPアクセスして動作確認

    チャット経由でLLMがこれを呼び出すことはできない(run_commandのアローリストに
    dockerは含めていない)。あくまでこのエンドポイントだけが持つ固定パイプライン。
    """

    def _run():
        return deploy.deploy_and_test(name)

    return _handle_backend_errors(_run)


# --- 互換API: 承認なしの一括実行 ---


@app.post("/agent/run")
def agent_run(req: TaskRequest) -> dict[str, Any]:
    """(互換用) 承認を求めず、タスクが完了するまで一括で自動実行する。"""

    def _run():
        backend, model = _resolve_backend_and_model(req.backend)
        session = sessions.start_session(
            task=req.task,
            backend=backend,
            model=model,
            project=req.project,
            max_iterations=req.max_iterations,
            require_approval=False,
            background=False,  # このAPIは互換用に「完了まで待つ」旧来の挙動を維持する
        )
        result = sessions.to_public_dict(session)
        result["truncated"] = session.status != "done" or bool(session.error)
        result["stop_reason"] = "end_turn" if session.status == "done" else session.status
        return result

    return _handle_backend_errors(_run)
