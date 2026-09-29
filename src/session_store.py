"""セッション(会話)のSQLiteによる永続化。

`sessions.py`のSESSIONSはプロセスメモリ上の辞書なので、コンテナ再起動で消える
(実際にこれまで何度も発生し、UIから見ると「エージェントが停止した」ように見えていた)。
ここでは同じ内容を、workspace_data(永続named volume)上のSQLiteファイルにも
書き込み、起動時に読み戻すことで会話がコンテナ再起動をまたいで残るようにする。

Session/ToolCallはdataclassで、バックエンドごとにmessagesの形が異なる(role/content
形式のもの、Geminiのsteps形式のもの等)ため、relationalに正規化する意味が薄い。
1セッション1行、内容はJSONの1カラムとして保存する。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .tools_core import WORKSPACE_ROOT

logger = logging.getLogger("code_agent.session_store")

DB_PATH = WORKSPACE_ROOT / "sessions.db"

# sqlite3のConnectionはスレッド間で共有できないため、呼び出しのたびに接続を開き、
# 単一のロックで直列化する(このアプリの利用規模ではオーバーヘッドは無視できる)。
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            data TEXT NOT NULL
        )
        """
    )
    return conn


def save(session_id: str, project: str, created_at: float, data: dict[str, Any]) -> None:
    """セッション1件をupsertする。失敗しても例外は投げない(呼び出し側の処理は継続させる)。

    json.dumps自体も(バックエンドが将来JSON化できないオブジェクトを紛れ込ませた場合に
    備えて)try節の中に含める。ここで例外を外へ漏らすと、セッションの永続化という
    副次的な処理の失敗で本来のリクエスト(チャット応答)自体が500エラーになってしまう。
    """
    try:
        payload = json.dumps(data, ensure_ascii=False)
        with _lock, _connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, project, created_at, updated_at, data)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    data = excluded.data
                """,
                (session_id, project, created_at, time.time(), payload),
            )
    except Exception:
        logger.exception("セッションの永続化に失敗しました(処理は継続します): %s", session_id)


def load_all() -> list[dict[str, Any]]:
    """保存済みの全セッションを、新しい順ではなく登録順のまま返す。読み込みに失敗した行はスキップする。"""
    try:
        with _lock, _connect() as conn:
            rows = conn.execute("SELECT id, data FROM sessions").fetchall()
    except Exception:
        logger.exception("セッションDBの読み込みに失敗しました(空の状態で起動します)")
        return []

    results: list[dict[str, Any]] = []
    for session_id, raw in rows:
        try:
            results.append(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("セッションデータのJSON解析に失敗したためスキップします: %s", session_id)
    return results


def delete(session_id: str) -> None:
    try:
        with _lock, _connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    except Exception:
        logger.exception("セッションの削除に失敗しました: %s", session_id)
