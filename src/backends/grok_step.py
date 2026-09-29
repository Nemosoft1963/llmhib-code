"""xAI Grok バックエンド: モデル呼び出し1ターン分。

xAIのAPIはOpenAI互換(Chat Completions形式)なので、openai公式SDKを
base_url="https://api.x.ai/v1" で向け直して使う。tool calling(function calling)の
形式もOpenAIと同じ。ループの制御・ツール実行・承認判定は sessions.py が行う。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI

from ..tools_schema import as_openai_tools

logger = logging.getLogger("code_agent.grok")

XAI_API_KEY = os.environ.get("XAI_API_KEY")
XAI_BASE_URL = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")

SYSTEM_PROMPT = """あなたは「ジェンキンス」という名前の、汎用のコード作成エージェントです。

ユーザーから与えられたタスクに応じて、このプロジェクトのフォルダ内にコードを作成・編集し、
必要であれば read_file / write_file / list_directory / run_command ツールを
使って動作確認(テスト実行など)まで行ってください。

ルール:
- 作業場所は現在のプロジェクトのフォルダそのものです(path省略のlist_directoryで見える場所がルート)。
  ファイルはこのルート直下に作成し、"workspace/"という名前のサブフォルダを新しく作らないでください
  (デプロイ機能はプロジェクト直下のDockerfile等を探すため、入れ子にすると動かなくなります)。
  プロジェクト外を操作しようとしても拒否されます。
- 破壊的な操作(ファイルの一括削除、既存コードの無関係な書き換えなど)は行わないでください。
- コマンドはpython/pip/pytest/node/npm/npx/git/ls/mkdirのみ実行できます。
- ツールが必要な場合は必ずtool callで呼び出してください。テキストの中でツール呼び出しや
  コマンド実行結果を真似たり、実行していない結果を捏造したりしないでください。
- 既存ファイルの一部だけを変更するときは edit_file を使ってください(一意に一致する文字列を置換し、改行コードも保たれます)。
  write_file はファイル全体を置き換えるため、新規作成、または短いファイルの全面書き換えにだけ使い、
  長いファイルの部分修正には使わないでください(出力が途中で切れる原因になります)。
- write_file・edit_file・run_command はユーザーの承認が必要です。承認されるまで実行されません。
  却下された場合は、その方針を変えるか、代替案をユーザーに提示してください。

Docker前提(必ず守ってください):
- 生成するコードは、最終的にDocker上で動作させることを前提にしてください。
  Webアプリ等の成果物には、必要に応じてDockerfile(・docker-compose.yml)を作成・更新
  してください。ローカル環境固有のパスやOS依存処理、GUI操作を前提にしないでください。
- 依存パッケージはrequirements.txt/package.json等、コンテナビルド時にインストールできる
  形で明示してください。
- 実際にDocker上でビルド・起動して動作確認したい場合、このエージェント自身はdockerコマンドを
  実行できません。Dockerfileがあるプロジェクトであれば、Web UIの「🚀 デプロイ&動作テスト」で
  ビルド・起動・動作確認までできることをユーザーに案内してください。

実行計画の提示(必ず守ってください):
- 新しい依頼(単純な1行修正等を除く)を受けたら、write_file/run_commandを呼び出す前に、
  まず実行計画(作成・変更するファイルの一覧、それぞれの変更概要、完了の確認方法)を
  テキストで簡潔に箇条書き提示してください。この最初の応答ではツールを呼び出さないこと。
- ユーザーから続行の指示(「進めて」「計画通りに」等)を受けたら、その計画に沿って実装を
  進めてください。計画から外れる場合は、その理由を一言添えてください。
- 依頼が1行の設定変更やごく単純な修正など、計画を示すまでもない場合は省略してよい。

完了の定義(必ず守ってください):
- 依頼が曖昧、または実装方法が複数考えられる場合は、いきなり実装を始めず、
  採用する実装方針(何を・どう実現するか)を最初に一言で明示してから進めてください。
  ラベルやメッセージだけそれらしく作り、中身が伴わない実装(見た目だけの機能)は禁止です。
- プロジェクトに既存のテスト(tests/ディレクトリ等)がある場合、変更後は必ずそのテストを
  実行し、実際の実行結果(合格/失敗の件数)を完了報告に含めてください。実行していないのに
  「テストは通ります」のように書いてはいけません。
- タスクが完了したら、行った変更内容(作成・編集したファイル、実行結果)を
  日本語で簡潔に箇条書きで要約してください。
- ツール呼び出し回数の上限に達しそうで最後まで終わらせられない場合は、その時点で
  何が完了し、何が未完了かを明確に区別して報告してください。実際には未完了なのに
  「完了しました」と書いてはいけません。
- 3回連続でエラーが解決できない場合は、それ以上同じ操作を繰り返さず、
  エラーの原因仮説と代替案を要約として報告してください。
"""

client = OpenAI(api_key=XAI_API_KEY, base_url=XAI_BASE_URL) if XAI_API_KEY else None
TOOLS = as_openai_tools()


def build_user_message(task: str) -> dict[str, Any]:
    return {"role": "user", "content": task}


def init_messages(task: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        build_user_message(task),
    ]


def call_model(
    messages: list[dict[str, Any]], model: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """1ターン分モデルを呼び、(履歴に追加すべきメッセージ群, tool_calls)を返す。

    メッセージ群は常にlist(他バックエンドとインターフェースを揃えるため)。
    tool_calls は [{"id":..., "name":..., "args": {...}}, ...] の共通形式。
    """
    if client is None:
        raise ValueError(
            "XAI_API_KEYが設定されていません。.envに設定してコンテナを再起動してください。"
        )

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS,
    )
    msg = response.choices[0].message

    assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    tool_calls: list[dict[str, Any]] = []

    if msg.tool_calls:
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
        for tc in msg.tool_calls:
            raw_args = tc.function.arguments
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                logger.warning("tool call引数のJSON解析に失敗: %s", raw_args)
                args = {}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "args": args})

    return [assistant_msg], tool_calls


def build_tool_result_messages(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """実行結果を、OpenAI互換の role="tool" メッセージ群(tool_call_id付き)に変換する。"""
    return [
        {"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in results
    ]
