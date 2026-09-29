"""ツールのJSON Schema定義(バックエンド非依存)。

ここを唯一の定義元とし、Claude / Ollama / OpenAI それぞれの形式へ変換する。
実行ロジックは tools_core、承認要否や実行ディスパッチは sessions.py が持つ。
"""

from __future__ import annotations

from typing import Any

from . import mcp_tools

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "workspace内のファイルをテキストとして読み込みます。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "workspaceルートからの相対パス。"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "workspace内にファイルを新規作成、または上書きします。"
            "親ディレクトリが無ければ自動作成します。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "workspaceルートからの相対パス。"},
                "content": {
                    "type": "string",
                    "description": "書き込むファイルの内容(全体を置き換えます)。",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "既存ファイルの一部だけを置換します(ファイル全体は書き換えません)。"
            "長いファイルの修正や、改行コードを保ちたい場合は write_file ではなくこちらを使ってください。"
            "old_string はファイル内で一意に一致する必要があります(複数一致はエラー)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "workspaceルートからの相対パス。"},
                "old_string": {
                    "type": "string",
                    "description": "置換前の文字列。空白・改行・インデントまで正確に一致させる。",
                },
                "new_string": {"type": "string", "description": "置換後の文字列。"},
                "replace_all": {
                    "type": "boolean",
                    "description": "true なら一致するすべてを置換する(既定 false)。",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_directory",
        "description": "workspace内のディレクトリ内容を一覧表示します。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "workspaceルートからの相対パス。省略時はworkspaceルート直下。",
                }
            },
            "required": [],
        },
    },
    {
        "name": "run_command",
        "description": (
            "workspace内で、許可されたコマンド(python, python3, pip, pip3, pytest, "
            "node, npm, npx, git, ls, mkdir)のみを実行します。シェル演算子"
            "(&&, ||, |, ;, バッククォート, $())は使用できません。タイムアウトは60秒です。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": '実行するコマンド全体(例: "pytest -q")。',
                }
            },
            "required": ["command"],
        },
    },
]


def _all_schemas() -> list[dict[str, Any]]:
    """組み込みツール + 接続済みMCPサーバーのツールをまとめて返す。"""
    return TOOL_SCHEMAS + mcp_tools.get_tool_schemas()


def as_anthropic_tools() -> list[dict[str, Any]]:
    """Claude Messages API の tools= 形式に変換する。"""
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in _all_schemas()
    ]


def _as_openai_style_tools() -> list[dict[str, Any]]:
    """OpenAI互換のtools= 形式({"type": "function", "function": {...}})に変換する。

    OllamaのAPIもこのOpenAI互換形式を採用しているため、Ollama/OpenAI両バックエンドで共用する。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in _all_schemas()
    ]


def as_ollama_tools() -> list[dict[str, Any]]:
    """Ollama chat API の tools= 形式(OpenAI互換)に変換する。"""
    return _as_openai_style_tools()


def as_openai_tools() -> list[dict[str, Any]]:
    """OpenAI Chat Completions API の tools= 形式に変換する。"""
    return _as_openai_style_tools()


def as_gemini_tools() -> list[dict[str, Any]]:
    """Gemini Interactions API の tools= 形式(フラットなfunction定義)に変換する。"""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        }
        for t in _all_schemas()
    ]
