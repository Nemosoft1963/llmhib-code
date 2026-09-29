"""既存プロジェクトの調査(読み取り専用モード)を開始する。

調査は指定した1つのバックエンドで、read_file / list_directory だけを使って行う
(それ以外のツールはサーバー側で拒否される)。最終報告は
docs/analysis/<バックエンド>-<日時>.md としてプロジェクト内に保存され、git に記録される。
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import backend_defaults, sessions

ANALYSIS_PROMPT = """あなたは既存コードを精密に調査する担当です。このセッションは【読み取り専用】です。
ファイルの作成・変更・コマンド実行はできません(試みても拒否されます)。read_file と list_directory だけを使ってください。

目的: このプロジェクト全体を、あとで安全に改修するために正確に把握し、調査報告を作成する。

進め方:
1. まず list_directory でルートを確認し、README / AGENTS / 設計・運用ドキュメント / 依存定義 / 設定 / 起動方法を読む。
2. 次に主要なソース(エントリポイント、中心となるモジュール、データの保存、外部連携)と、tests/ の内容を実際に読む。
   ファイル一覧だけで推測しない。読んでいないファイルについて断定しない。
3. inputs/ にある TRIZ 収集データ(triz-problems-export.json とメタファイル)の形式を読み、
   このシステムの既存の入力口(問題定義・事例の追加処理)へどう接続できるかを具体的に調べる。

報告は日本語のMarkdownで、次の見出しをこの順で書くこと:
# 1. 概要(目的・主な機能・技術スタック)
# 2. ディレクトリ構成と各モジュールの責務(ファイルパス付き)
# 3. 起動方法と実行の流れ(入口 → 主要処理)
# 4. データと永続化(保存先・形式・スキーマ)
# 5. 外部連携・設定・環境変数(値は書かず、名前と用途だけ)
# 6. テストの状況(何がテストされ、何が無いか)
# 7. リスクと技術的負債(ファイルパスと根拠付き。重大度: 高/中/低)
# 8. 改修する際の注意点(壊れやすい箇所、依存関係、触る順序の提案)
# 9. TRIZ収集データの取り込み案(既存の入力口、必要な変換、検証方法)
# 10. 確認できなかったこと(読めなかった/判断できなかった点を、推測と明確に区別して列挙)

規則:
- docs/analysis/ 配下は、他のAIが作成した分析報告なので【絶対に読まない】こと。独立した分析にするため。
  (docs/ のそれ以外の文書、および docs/CHANGELOG_* は読んでよい)
- 各主張には、根拠となるファイルパスを添える。行番号が分かれば添える。
- 事実(読んで確認したこと)と推測を区別する。推測には「推測:」と明記する。
- 認証情報の値・個人情報は報告に書かない。
- 最後に、読んだファイルの一覧(パス)を付ける。
"""


def start_analysis(project: str, backend: str | None = None, max_iterations: int = 80):
    resolved_backend, model = backend_defaults.resolve_backend_and_model(backend or "claude")
    if not backend_defaults.is_backend_available(resolved_backend):
        raise ValueError(f"{resolved_backend} のAPIキーが設定されていません")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = f"docs/analysis/{resolved_backend}-{stamp}.md"
    return sessions.start_session(
        task=ANALYSIS_PROMPT,
        backend=resolved_backend,
        model=model,
        project=project,
        max_iterations=max_iterations,
        require_approval=False,
        elaborate=False,
        read_only=True,
        analysis_out=out,
    )
