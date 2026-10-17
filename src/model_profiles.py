"""用途(軽い/標準/重い)に応じてモデルを選ぶための対応表と自動判定。

- バックエンドごとに tier(light / standard / heavy)→モデル名の対応表を持つ。
  既定値はこのファイルの DEFAULT_TABLE。環境変数 MODEL_PROFILES_PATH のJSONで差し替えられる。
- 対応表に無い組み合わせ・不正な設定値は黙って使わず、backend_defaults の既定モデルに
  戻し、理由(note)を返す(呼び出し側がセッション記録に残す)。
- tier="auto" のときは、依頼文からヒューリスティックで tier を決める(classify_task)。
  自動判定は「迷ったら standard」。重い判定は費用が増えるため、根拠の語を記録に残す。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from . import backend_defaults

logger = logging.getLogger("code_agent.model_profiles")

TIERS = ("light", "standard", "heavy")
AUTO = "auto"

# 既定の対応表。載っていないバックエンドは、tierによらず backend_defaults の既定モデル。
DEFAULT_TABLE: dict[str, dict[str, str]] = {
    "gemini": {
        "light": "gemini-3.1-flash-lite",
        "standard": "gemini-4-argon",
        "heavy": "gemini-3.1-pro-preview",
    },
    "claude": {
        "light": "claude-haiku-5-5",
        "standard": "claude-sonnet-5-5",
        "heavy": "claude-opus-5",
    },
}

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")

# 自動判定の語。重い=設計や横断的な変更など、軽い=読み取り・確認中心。
_HEAVY_WORDS = (
    "設計", "アーキテクチャ", "リファクタ", "セキュリティ", "移行", "マイグレーション",
    "並行", "競合", "根本原因", "全体", "横断", "fail-open", "キルスイッチ", "回路遮断",
    "refactor", "architecture", "migration", "security",
)
_LIGHT_WORDS = (
    "調査", "確認", "一覧", "読んで", "説明", "誤字", "typo", "コメント", "ドキュメント",
    "ログを見", "場所を教え", "どこにある",
)
_HEAVY_LEN = 1500
_LIGHT_LEN = 200


def is_valid_model_name(name: object) -> bool:
    """モデル名として許す形式か(英数字と . _ : / - のみ、100字以内)。"""
    return isinstance(name, str) and bool(_MODEL_NAME_RE.match(name))


def _profiles_path() -> Path | None:
    raw = os.environ.get("MODEL_PROFILES_PATH", "").strip()
    return Path(raw) if raw else None


def load_table() -> tuple[dict[str, dict[str, str]], list[str]]:
    """有効な対応表と、読み込み時の警告(日本語)を返す。

    上書きファイルが無い→既定のみ(警告なし)。読めない/壊れている→既定のみ+警告。
    個々の不正な値は無視し、その分は既定にフォールバックする(警告に残す)。
    """
    table = {b: dict(t) for b, t in DEFAULT_TABLE.items()}
    warnings: list[str] = []
    path = _profiles_path()
    if path is None:
        return table, warnings
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return table, [f"モデル対応表ファイルが見つかりません。既定を使います: {path.name}"]
    except (OSError, ValueError) as exc:
        return table, [f"モデル対応表を読めません。既定を使います: {type(exc).__name__}"]
    if not isinstance(raw, dict):
        return table, ["モデル対応表の形式が不正です。既定を使います"]
    for backend, tiers in raw.items():
        if not isinstance(backend, str) or not isinstance(tiers, dict):
            warnings.append("モデル対応表の一部の形式が不正なため無視しました")
            continue
        for tier, model in tiers.items():
            if tier not in TIERS:
                warnings.append(f"未知の用途段階を無視しました: {backend}/{str(tier)[:20]}")
                continue
            if not isinstance(model, str) or not _MODEL_NAME_RE.match(model):
                warnings.append(f"不正なモデル名を無視しました: {backend}/{tier}")
                continue
            table.setdefault(backend, {})[tier] = model
    return table, warnings


def classify_task(task: str) -> tuple[str, str]:
    """依頼文から tier を決める。(tier, 根拠) を返す。迷ったら standard。"""
    text = str(task or "")
    lowered = text.lower()
    heavy_hits = [w for w in _HEAVY_WORDS if w.lower() in lowered]
    light_hits = [w for w in _LIGHT_WORDS if w.lower() in lowered]
    if heavy_hits or len(text) >= _HEAVY_LEN:
        reason = "語: " + ",".join(heavy_hits[:3]) if heavy_hits else f"依頼文が長い({len(text)}字)"
        return "heavy", reason
    if light_hits and len(text) <= _LIGHT_LEN:
        return "light", "語: " + ",".join(light_hits[:3])
    return "standard", "既定"


def next_tier(tier: str) -> str | None:
    """1段階上の tier。最上位なら None。"""
    if tier not in TIERS:
        return None
    idx = TIERS.index(tier)
    return TIERS[idx + 1] if idx + 1 < len(TIERS) else None


def resolve_model(backend: str, tier: str) -> tuple[str, str]:
    """(モデル名, 注記) を返す。対応表に無い場合は既定モデルへ戻し、注記に理由を入れる。"""
    _, default_model = backend_defaults.resolve_backend_and_model(backend)
    if tier not in TIERS:
        return default_model, f"用途段階を確認できないため既定モデルを使います: {str(tier)[:20]}"
    table, warnings = load_table()
    note = "; ".join(warnings)
    model = table.get(backend, {}).get(tier)
    if not model:
        return default_model, (note + "; " if note else "") + "対応表に無いため既定モデルを使います"
    return model, note


def resolve_tier_request(requested: str | None, task: str) -> tuple[str, str]:
    """リクエストの tier 指定(None/"auto"/具体的な段階)を、具体的な (tier, 根拠) にする。

    None は従来どおり「段階を使わない」=呼び出し側が既定モデルを使う。ここでは "standard"
    ではなく ("", "") を返す。不正な値は ValueError。
    """
    if requested is None or str(requested).strip() == "":
        return "", ""
    value = str(requested).strip().lower()
    if value == AUTO:
        return classify_task(task)
    if value in TIERS:
        return value, "指定"
    raise ValueError(f"tier は {', '.join(TIERS)}, auto のいずれかを指定してください: {value[:20]}")
