"""MCPサーバー接続。

`mcp_servers.json`(または MCP_CONFIG_PATH で指定したファイル)に列挙された
MCPサーバーに接続し、そのツールをエージェントのツール集合に追加する。

MCP Python SDKは非同期(asyncio)前提だが、このプロジェクトの他の部分は同期コードで
書かれているため、専用のイベントループを持つバックグラウンドスレッドを1つ起動し、
そこでMCPセッションを維持する。同期コードからは start() / get_tool_schemas() /
call_tool() だけを使えばよい。

MCP_SERVERS環境変数、または設定ファイルが存在しない/空の場合は何もしない
(既存の動作に影響を与えない)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("code_agent.mcp")

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "mcp_servers.json"

# MCPツール名にこれらの語(部分一致・大文字小文字無視)が含まれる場合、
# 副作用がある可能性が高いとみなし承認を必須にする。ヒューリスティックであり
# 完全ではないため、リスクの高いMCPサーバーを追加する場合は注意すること。
RISKY_KEYWORDS = (
    "write",
    "delete",
    "drop",
    "insert",
    "update",
    "create",
    "exec",
    "remove",
    "modify",
    "alter",
    "append",
)

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_exit_stack: AsyncExitStack | None = None
_sessions: dict[str, Any] = {}  # server_name -> ClientSession
_tool_map: dict[str, tuple[str, str]] = {}  # 公開ツール名 -> (server_name, mcp側のツール名)
_tool_schemas: dict[str, dict[str, Any]] = {}  # 公開ツール名 -> {name, description, parameters}


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str] | None = None


def _load_config() -> list[MCPServerConfig]:
    path_str = os.environ.get("MCP_CONFIG_PATH", "").strip()
    path = Path(path_str) if path_str else DEFAULT_CONFIG_PATH
    if not path.exists():
        logger.info("MCP設定ファイルが見つかりません(%s)。MCP連携は無効です", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("MCP設定ファイルの読み込みに失敗しました(%s): %s", path, e)
        return []
    return [
        MCPServerConfig(
            name=entry["name"],
            command=entry["command"],
            args=entry.get("args", []),
            env=entry.get("env"),
        )
        for entry in data
    ]


def _run_loop_forever(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def _connect_server(config: MCPServerConfig) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    assert _exit_stack is not None
    env = {**os.environ, **(config.env or {})} if config.env else None
    params = StdioServerParameters(command=config.command, args=config.args, env=env)
    read, write = await _exit_stack.enter_async_context(stdio_client(params))
    session = await _exit_stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    tools_result = await session.list_tools()
    _sessions[config.name] = session
    for t in tools_result.tools:
        public_name = f"mcp_{config.name}_{t.name}"
        _tool_map[public_name] = (config.name, t.name)
        _tool_schemas[public_name] = {
            "name": public_name,
            "description": (t.description or "")[:1000],
            "parameters": t.input_schema or {"type": "object", "properties": {}},
        }
    logger.info(
        "MCPサーバー接続完了: %s (%d ツール: %s)",
        config.name,
        len(tools_result.tools),
        ", ".join(t.name for t in tools_result.tools),
    )


async def _setup_all(configs: list[MCPServerConfig]) -> None:
    global _exit_stack
    _exit_stack = AsyncExitStack()
    for config in configs:
        try:
            await _connect_server(config)
        except Exception:
            logger.exception("MCPサーバーへの接続に失敗しました: %s", config.name)


def start() -> None:
    """バックグラウンドスレッドでイベントループを起動し、設定済みMCPサーバーに接続する。

    アプリ起動時に一度だけ呼ぶ。設定が無ければ何もしない。
    """
    global _loop, _thread
    if _loop is not None:
        return

    configs = _load_config()
    if not configs:
        return

    _loop = asyncio.new_event_loop()
    _thread = threading.Thread(target=_run_loop_forever, args=(_loop,), daemon=True)
    _thread.start()

    fut = asyncio.run_coroutine_threadsafe(_setup_all(configs), _loop)
    try:
        fut.result(timeout=120)
    except Exception:
        logger.exception("MCPサーバーの初期化中にエラーが発生しました")


def get_tool_schemas() -> list[dict[str, Any]]:
    """接続済み全MCPサーバーのツールスキーマ(tools_schema.TOOL_SCHEMAS互換)を返す。"""
    return list(_tool_schemas.values())


def is_mcp_tool(name: str) -> bool:
    return name in _tool_map


def is_risky(name: str) -> bool:
    """このMCPツールが副作用を持つ可能性が高いか(ヒューリスティック)。"""
    _, mcp_tool_name = _tool_map.get(name, ("", name))
    lname = mcp_tool_name.lower()
    return any(kw in lname for kw in RISKY_KEYWORDS)


def call_tool(name: str, args: dict[str, Any]) -> str:
    """公開ツール名(mcp_<server>_<tool>)でMCPツールを実行する。"""
    mapping = _tool_map.get(name)
    if mapping is None:
        return f"エラー: 未知のMCPツールです: {name}"
    server_name, mcp_tool_name = mapping

    session = _sessions.get(server_name)
    if session is None or _loop is None:
        return f"エラー: MCPサーバーに接続されていません: {server_name}"

    async def _do_call() -> str:
        result = await session.call_tool(mcp_tool_name, args)
        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            parts.append(text if text is not None else str(block))
        text = "\n".join(parts)
        if getattr(result, "isError", False):
            return f"エラー: {text}"
        return text

    fut = asyncio.run_coroutine_threadsafe(_do_call(), _loop)
    try:
        return fut.result(timeout=60)
    except Exception as e:
        logger.exception("MCPツール実行中にエラー: %s.%s", server_name, mcp_tool_name)
        return f"エラー: MCPツール実行中に例外が発生しました: {e}"
