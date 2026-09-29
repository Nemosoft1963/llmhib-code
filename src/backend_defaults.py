"""バックエンド名からモデル名を解決する共通ロジック、および自動引き継ぎ(failover)の設定。

main.py(API層、ユーザーが明示的に選んだbackendの解決)と sessions.py(トークン切れ等が
起きたときの自動引き継ぎ先の解決)の両方から使う。ここに一本化しないと、モデルを
変更したときに片方だけ更新し忘れる事故につながる。
"""

from __future__ import annotations

import os

DEFAULT_BACKEND = os.environ.get("LLM_BACKEND", "ollama")
DEFAULT_CLAUDE_MODEL = "claude-opus-5"
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
DEFAULT_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
DEFAULT_XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4.6")
# Meta Llama API(OpenAI互換)。モデル名はLlama APIの提供モデルに合わせて.envで変更する。
DEFAULT_META_MODEL = os.environ.get("LLAMA_MODEL", "Llama-4-Maverick-17B-128E-Instruct-FP8")

# 自動引き継ぎ(failover)で次に試す際の優先順。現在のバックエンドと、この会話で
# 既に試して失敗したバックエンドは除外される(sessions.pyの_try_handoff参照)。
# claudeは最も安定して動く分、他のバックエンドが軒並み失敗したときの最後の
# 引き継ぎ先として温存する(ユーザー指定により2026-09-11に末尾へ変更)。
FAILOVER_PRIORITY = ["gemini", "openai", "grok", "ollama", "claude"]

# 各バックエンドが「使える状態か」を判定するためのAPIキー環境変数名。
API_KEY_ENV = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "grok": "XAI_API_KEY",
    "meta": "LLAMA_API_KEY",
}


def resolve_backend_and_model(backend: str | None) -> tuple[str, str]:
    selected = (backend or DEFAULT_BACKEND).lower()
    if selected == "claude":
        return "claude", DEFAULT_CLAUDE_MODEL
    if selected == "ollama":
        return "ollama", DEFAULT_OLLAMA_MODEL
    if selected == "openai":
        return "openai", DEFAULT_OPENAI_MODEL
    if selected == "gemini":
        return "gemini", DEFAULT_GEMINI_MODEL
    if selected == "grok":
        return "grok", DEFAULT_XAI_MODEL
    if selected == "meta":
        return "meta", DEFAULT_META_MODEL
    raise ValueError(
        f"未知のbackendです: {selected} (ollama, claude, openai, gemini, grok, meta のいずれかを指定してください)"
    )


def is_backend_available(name: str) -> bool:
    """APIキー等が設定されていて、実際に呼び出せそうか判定する(ollamaは常にTrue)。"""
    if name == "ollama":
        return True
    env_name = API_KEY_ENV.get(name)
    return bool(env_name and os.environ.get(env_name))
