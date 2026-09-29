"""作業指示の「具体化」ステップ(ローカルLLMで下書きし、実装担当のAIへ渡す)。

背景: 曖昧な一行の依頼(例:「検索を再実行するボタンをUIに追加」)を実装担当のLLMへ
そのまま渡すと、ラベルやメッセージだけそれらしく作って中身が伴わない実装になりやすい
ことが実際に確認されている(README「モデル選定について」参照)。ここでは、実装に入る前に
ローカルLLM(Ollama)へ「実装者が迷わず実装できる具体的な指示」に書き直させ、その結果を
ユーザーが選んだ実装担当のバックエンド(claude/gemini/openai/ollama)へ渡す。

このステップ自体はツール呼び出しを行わない、1回だけのプレーンなテキスト生成。
失敗した場合は例外を投げるだけで、フォールバック(元の指示をそのまま使う)は
呼び出し側(sessions.py)の責務とする。
"""

from __future__ import annotations

import logging
import os

import ollama

logger = logging.getLogger("code_agent.elaborate")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
# 「フロントAI」(具体化担当)のモデル。実装担当として使う"ollama"backend自体のモデル
# (OLLAMA_MODEL)とは役割が異なるため、専用の環境変数として分離している。
# 未設定時はOLLAMA_MODELにフォールバックする(後方互換)。
ELABORATE_MODEL = os.environ.get("ELABORATE_MODEL") or os.environ.get("OLLAMA_MODEL", "qwen3:8b")
# 大きめのモデル(20B超)の初回ロードには300秒でも不足することを確認済みのため延長
# (ollama_step.pyのREQUEST_TIMEOUT_SECと同じ理由)。
REQUEST_TIMEOUT_SEC = 900

ELABORATION_SYSTEM_PROMPT = """あなたはソフトウェア開発の要件定義を行う担当者です。
渡された「作業指示」を、別の実装担当者(別のAI)がそのまま迷わず実装できるように、
具体的な指示文に書き直してください。

必ず守ること:
- 出力は書き直した指示文だけにする(前置き・後書き・説明・見出しは書かない)
- 元の依頼の意図を変えない。依頼されていない機能を勝手に追加しない
- 曖昧な部分は妥当な前提を自分で補い、その前提を指示文の中に明記する
- 可能な限り、何をもって完成とするか(成功/失敗の判定基準)を含める
- 対象プロジェクトに既存のテストがありそうな場合は、変更後にテストを実行して
  全件合格を確認することを指示に含める
- 実装方法を1つに決め打ちしすぎない(実装担当が読んで迷わない程度の具体性に留める)
- 元の指示が既に十分に具体的な場合は、大きく変えず、そのまま(必要な微修正のみ)返す
- 出力は日本語にする

「これまでの会話の流れ」が渡された場合の注意(重要・必ず守ること):
- それはこの会話でこれまでに起きたやり取りの背景情報であり、具体化の対象ではない。
  具体化すべきなのは、あくまで「今回のユーザー指示」だけである。
- 「進めて」「続けて」「必要なコードを生成」「はい」のような短い継続指示は、
  必ず背景情報に書かれている直前の計画・要件の続きとして解釈すること。
  背景情報と無関係な、別の新しいタスク(全く違うアプリ・機能の例など)を
  勝手に作り出してはならない。これは重大な誤りである。
- 継続指示の意味が背景情報からも本当に読み取れない場合は、無関係な具体例を
  でっち上げるのではなく、「直前の計画に沿って実装を進めてください」のように、
  背景情報に書かれた直前の話題を保ったまま一般的な継続の指示として書き直すこと。
"""


class ElaborationError(Exception):
    """具体化に失敗した場合の例外。呼び出し側は元の指示へのフォールバックを想定する。"""


def elaborate(task: str, context: str | None = None) -> str:
    """ローカルLLMで作業指示を具体化して返す。失敗時は ElaborationError を送出する。

    context: これまでの会話の流れの要約(あれば)。「進めて」のような短い継続指示を
    文脈込みで正しく解釈させるために渡す。渡さないと、無関係な新規タスクを
    捏造することがある(実際に確認された不具合)。
    """
    client = ollama.Client(host=OLLAMA_BASE_URL, timeout=REQUEST_TIMEOUT_SEC)
    user_content = task
    if context:
        user_content = (
            "### これまでの会話の流れ(背景情報。具体化の対象ではない)\n"
            f"{context}\n\n"
            "### 今回具体化すべき、直近のユーザー指示\n"
            f"{task}"
        )
    try:
        response = client.chat(
            model=ELABORATE_MODEL,
            messages=[
                {"role": "system", "content": ELABORATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as e:  # ollama.ResponseError / httpx系タイムアウト等をまとめて捕捉
        raise ElaborationError(f"ローカルLLMの呼び出しに失敗しました: {e}") from e

    message = response.get("message") if isinstance(response, dict) else getattr(response, "message", None)
    content = ""
    if isinstance(message, dict):
        content = message.get("content", "") or ""
    elif message is not None:
        content = getattr(message, "content", "") or ""

    content = content.strip()
    if not content:
        raise ElaborationError("ローカルLLMから空の応答が返りました")
    return content
