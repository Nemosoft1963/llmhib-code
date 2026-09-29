# ジェンキンス — 汎用コード作成エージェント (LLMHIB_CODE)

[![build-check](https://github.com/Nemosoft1963/llmhib-code/actions/workflows/ci.yml/badge.svg)](https://github.com/Nemosoft1963/llmhib-code/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Docker上で動く汎用コード作成エージェントです。タスクを投げると、エージェントが
**プロジェクト**(独立した作業フォルダ)配下にコードを作成・編集し、必要なら `pytest` や
`pip install` 等で動作確認まで行います。Cline に近い体験を目指し、**write_file /
run_command(および副作用のあるMCPツール)は承認制**、**変更はプロジェクトごとに
gitチェックポイントとして自動記録・差分確認・ロールバック可能**です。**MCPサーバー**
(Web検索・SQLite)にも標準で接続しています。

**既定バックエンドはローカルLLM(Ollama + `qwen3:8b`、GPU利用)です。** Claude API
(`claude-opus-5`)・OpenAI API(既定 `gpt-4o`)・Gemini API(既定 `gemini-3.8-flash`)・
xAI Grok API(既定 `grok-4.6`)にもリクエスト単位で切り替え可能です(モデルはそれぞれ
`OPENAI_MODEL`/`GEMINI_MODEL`/`XAI_MODEL`で変更可)。Meta Llama API(`"backend": "meta"`、`LLAMA_API_KEY`と`LLAMA_MODEL`を.envに設定)にも
切り替え可能です(OpenAI互換エンドポイント経由。自動引き継ぎの候補には含めていません)。

## 目次

- [構成](#構成)
- [モデル選定について(重要な検証結果)](#モデル選定について重要な検証結果)
- [セットアップ](#セットアップ)
- [プロジェクト](#プロジェクト)
- [Web UI(チャット形式・承認・チェックポイント対応)](#web-uiチャット形式承認チェックポイント対応)
- [API](#api)
- [安全上の制約](#安全上の制約)
- [MCPサーバー連携](#mcpサーバー連携)
- [ログ・デバッグ](#ログデバッグ)
- [貢献・ライセンス](#貢献ライセンス)

## 構成

- `Dockerfile` / `docker-compose.yml` — 実行環境(Docker/Docker Compose)。`ollama` と
  `code-agent` の2サービス構成。
- `src/main.py` — FastAPI エントリーポイント(セッションAPI・チェックポイントAPI・Web UI配信)
- `src/sessions.py` — セッション管理。承認が必要なツール呼び出しでループを一時停止し、
  承認/却下を受けて再開する状態機械。
- `src/checkpoints.py` — git を使ったworkspaceのチェックポイント(スナップショット)管理。
  一覧・差分・ロールバック。
- `src/backends/claude_step.py` / `src/backends/ollama_step.py` / `src/backends/openai_step.py` /
  `src/backends/gemini_step.py` / `src/backends/grok_step.py` — 各バックエンドで
  「モデルを1ターン分呼んでtool_callsを取り出す」処理(手動ループの1ステップ)。
  `sessions.py`がバックエンド名からモジュールを解決する(`ollama`/`claude`/`openai`/
  `gemini`/`grok`)。Geminiのみ会話履歴の表現が「role/content」ではなく「steps」形式
  (Interactions API)なので、`call_model`はターンごとに複数stepを返せる設計。Grok(xAI)は
  APIがOpenAI互換のため、`grok_step.py`は`openai_step.py`と同じ実装をbase_url違いで使う。
- `src/backend_defaults.py` — バックエンド名→既定モデル名の解決、自動引き継ぎ(failover)の
  優先順リストとAPIキー有無の判定(`main.py`と`sessions.py`の両方から使う共通ロジック)
- `src/tools_schema.py` — ツールのJSON Schema定義(組み込み + MCP。各API形式へ変換)
- `src/tools_core.py` — 組み込みツールの実装本体(`Workspace`クラス。プロジェクトごとに
  read_file / write_file / list_directory / run_command / プロジェクト一覧・作成を提供)
- `src/mcp_tools.py` — MCPサーバークライアント。`mcp_servers.json` に列挙したサーバーに接続し、
  そのツールを組み込みツールと同じ枠組みに統合する。
- `mcp_servers.json` — 接続するMCPサーバーの設定(下記参照)
- `src/static/index.html` — Web UI(`/` で配信、下記参照)
- `workspace/projects/<プロジェクト名>/` — 各プロジェクトの作業ディレクトリ(下記「プロジェクト」参照)

> **注意(ネットワークドライブ上の制約):** 筆者の環境では、このプロジェクトを社内NAS共有の
> マップドライブ上に置いて運用しています。検証の結果、
> Docker Desktopはこのパスへの **host bind mount への書き込みを実体のNAS共有に反映できません**
> (コンテナ内から書いても消える)。そのため `docker-compose.yml` では `workspace/` および
> Ollamaのモデルデータをホストへbind mountせず、Docker管理の named volume
> (`workspace_data` / `ollama_data`)に永続化しています。
> - コード変更(`src/`)を反映するには `docker compose up -d --build` で再ビルドしてください
>   (hot reloadは無効です)。
> - エージェントが生成したファイルをこのフォルダの `workspace/` に取り出したい場合は、下記の
>   `docker compose cp` コマンドを使ってください。

## モデル選定について(重要な検証結果)

複数のローカルモデルを検証し、既定を **`qwen3:8b`** としています。

- `qwen2.5-coder:7b`: Ollama上でtool callingの応答形式(`<tool_call>...</tool_call>`タグ)を
  安定して出力せず、**ツールを呼び出したふりをして結果を捏造する**ことを確認(temperature=0や
  明示的なプロンプトでも改善せず)。不採用。
- `qwen3:14b`: プロトコルレベルのtool callingは正常だが、実際のマルチファイル実装タスクでは
  ツールを一切呼ばずに`ls -R`の出力を丸ごと捏造し、全ステップ完了したかのような虚偽報告をした。
  `qwen3:8b`より信頼性が低く不採用。
- `devstral:24b`(コーディングエージェント向けモデル): テンプレート上はtool calling対応だが、
  実際にはツールを一切呼ばず「自分では作れないので手順を案内します」とテキストで回答するのみ
  (複数回再現)。不採用。

これらの検証結果から、**ローカルLLMを大きくする方向はこのエージェントの信頼性を改善しない**
というのが結論です。複雑な多ファイル実装や事実確認が必要なタスクには、後述の
`"backend": "claude"` / `"openai"` / `"gemini"` / `"grok"` のいずれかを使ってください
(claude/openai/geminiは実タスクで実際にファイル作成・実行・承認フローが動作することを
確認済みです。grokは追加直後で同等の実タスク検証はまだ行っていません)。

## セットアップ

1. `.env.example` を参考に `.env` を用意してください(既に用意済み)。
   `LLM_BACKEND`(既定 `ollama`)、`OLLAMA_MODEL`(既定 `qwen3:8b`)、および
   Claude backendを使う場合のみ `ANTHROPIC_API_KEY` を設定します。

2. コンテナをビルド・起動します。

   ```bash
   docker compose up -d --build
   ```

3. モデルをpull(初回のみ、約5GB):

   ```bash
   docker exec llmhib-ollama ollama pull qwen3:8b
   ```

4. 起動確認:

   ```bash
   docker compose ps
   curl http://localhost:8012/health
   ```

### GPU / VRAMについて

`ollama` サービスは `docker-compose.yml` でNVIDIA GPUを予約しています(RTX 4070 Ti SUPER,
16GB)。このマシンはComfyUI・VOICEVOX・アバターワーカーなど他のGPUコンテナも動いており、
それらの負荷次第でVRAMが逼迫すると `qwen3:8b`(約5-6GBのVRAMを使用)のロードに失敗する
ことがあります。その場合は他のGPUコンテナを一時停止するか、`docker compose logs ollama`
でエラー内容を確認してください。

## プロジェクト

エージェントが扱う対象は「プロジェクト」という単位に分かれています。プロジェクトごとに
ファイル・gitチェックポイント履歴が完全に分離されます(`workspace/projects/<名前>/`)。
既定は `default` です。

- 新しいプロジェクトを作るには `POST /projects`(またはWeb UIの「＋ 新規作成」)。
  名前は**日本語などUnicode文字も使用可**(100文字以内)。禁止されるのはパス区切り文字
  (`/` `\`)、`.`/`..`そのもの、前後の空白、`< > : " | ? *` などOS上使えない記号、制御文字、
  Windowsの予約デバイス名(`CON`等)のみ(パストラバーサル対策)。
- タスクは `project` パラメータで対象プロジェクトを指定します(省略時 `default`)。
- **既存データの移行:** この機能を導入する前(単一workspaceだった頃)にファイルがあった
  場合、コンテナの初回起動時に自動的に `default` プロジェクトへ移行されます
  (`.git`履歴も含めて丸ごと移動、ログに移行件数が出ます)。
- **既知の制約:** MCPツール(Web検索・SQLite)は現状プロジェクトに依存しません。
  Web検索はどのプロジェクトからでも同じように使えますが、SQLite(`mcp_sqlite_*`)は
  `projects/default/agent.db` に固定されており、`default` 以外のプロジェクトで使っても
  同じデータベースを共有します(プロジェクトごとのDB分離は未対応)。

### 成果物のエクスポート先を自由に設定する

コンテナ自身はホスト(Windows)側の任意フォルダに直接アクセスできない
(このプロジェクトが載っているNAS共有では、以前検証した通りDocker Desktopの
bind mountも信頼できません)ため、「エクスポート先」はプロジェクトごとに**設定を保存**し、
実際のコピーは**ホスト側で1コマンド実行する**方式にしています。

```bash
# 1. エクスポート先を設定する(NAS・ローカルドライブ、どちらの任意パスでもOK)
curl -X PUT http://localhost:8012/projects/<プロジェクト名>/export-path \
  -H "Content-Type: application/json" \
  -d '{"export_path": "C:\\Users\\<ユーザー名>\\Desktop\\myapp-output"}'

# 2. コピー用コマンドを取得する
curl http://localhost:8012/projects/<プロジェクト名>/export-command
# -> {"command": "docker compose cp \"code-agent:/app/workspace/projects/<name>/.\" \"<export_path>\""}

# 3. 取得したコマンドを、このリポジトリのディレクトリ(docker-compose.ymlがある場所)で実行する
docker compose cp "code-agent:/app/workspace/projects/<name>/." "<export_path>"
```

Web UIからも同様に設定・コマンド取得ができます(プロジェクトバー付近)。手元で実行する
代わりに、Claude Codeにこのコマンドの実行を依頼することもできます。

**より高度な選択肢(ローカルドライブ限定):** `C:\` 等の本当のローカルドライブ配下の
フォルダであれば、Docker Desktopのbind mountが正しく機能することを確認済みです
(NASのマップドライブ `Z:\` 等では機能しません)。特定プロジェクトを常時ホストの
ローカルフォルダへライブ同期したい場合は、`docker-compose.yml` にそのフォルダを
bind mountとして追加し再起動する必要があります(実行中のアプリからは動的に追加
できないDockerの仕様上の制約です)。希望のプロジェクト名とローカルフォルダを
指定してもらえれば、この設定を行います。

## Web UI(チャット形式・承認・チェックポイント対応)

ブラウザで **http://localhost:8012/** を開くと、専用のWeb UIが使えます(FastAPIが配信、
追加コンテナ不要)。**チャット形式(対話形式)**になっており、指示を送る→実行状況/結果が
返る→続けて次の指示を送る、という流れで使えます。

- 画面上部の「プロジェクト」でプロジェクトを選択・新規作成できます。ファイル一覧・
  チェックポイント・チャットは、選択中のプロジェクトに対して行われます。
- 下部の入力欄に指示を書いて送信すると、ユーザーの発言が吹き出しとして表示され、
  エージェントの応答(テキスト・ツール呼び出し・チェックポイント)がその下に積み上がります。
- **会話の文脈は保持されます**: 一度のタスクが完了した後、続けて「さっき作ったファイルに
  ○○を追加して」のように前のやり取りを踏まえた指示を送れます(`/agent/{id}/continue` API)。
  「↺ 新しい会話」ボタンで文脈をリセットして最初からやり直せます。
- **実行状況はリアルタイムに確認できます**: `/agent/start` `/agent/{id}/continue`
  `/agent/{id}/decision` はいずれも即座に`status: "running"`を返し、実際の処理は
  バックグラウンドスレッドで進みます。Web UIはその間 `GET /agent/{id}` を約1.2秒間隔で
  自動ポーリングし、チャットログと「実行中です…(3/20ターン目) 🔧 write_file を実行中」の
  ような進捗表示をリアルタイムに更新します。長時間かかるローカルLLM呼び出しでも、
  完了まで無反応になることはありません。互換用の一括実行API(`/agent/run`)のみ、
  旧来通り完了までブロックします。
- 「⚙ 実行設定を表示」でバックエンド(ollama/claude)・最大ループ回数・承認要否を
  会話開始前に設定できます(会話継続中は最初に決めた設定が使われます)。
- **実装担当AIは常にチャット上部に表示されます**(例:「🤖 コード生成担当AI: grok
  (grok-4.6)」)。「フロントAI」(指示の具体化・下記)とは別の存在であることが常に分かります。
- **自動引き継ぎ(failover)**: 実装担当のバックエンドがトークン/レート制限・APIの残量不足・
  コンテキスト長超過などで続けられなくなった場合、または`max_iterations`の上限に達した場合、
  固定の優先順リスト(`gemini → openai → grok → ollama → claude`、
  `src/backend_defaults.py`の`FAILOVER_PRIORITY`。claudeは最も安定して動くため、
  他が軒並みダメだったときの最後の引き継ぎ先として温存している)の中から、まだこの会話で試していない
  利用可能なバックエンドへ自動的に切り替えて続行します。切り替えの際は、生の会話履歴
  (バックエンドごとに形式が異なる)をそのまま引き継ぐのではなく、これまでの作業内容を
  要約して新しい担当AIへの引き継ぎタスクとして渡します。切り替えはチャット上に
  「🔄 担当AIを自動的に切り替えました: gemini(...) → openai(...) 理由: ...」として必ず
  表示されます。候補をすべて試しても続けられない場合のみ、正直にエラーとして報告します。
- **「指示をローカルLLMで具体化してから実装担当のAIへ渡す」**(既定OFF): 曖昧な一行の
  依頼(例:「検索を再実行するボタンをUIに追加」)をそのまま実装担当のLLMに渡すと、
  ラベルだけそれらしく作って中身が伴わない実装になりやすいことが実際に確認されている
  (「モデル選定について」節参照)。これをONにすると、
  ユーザーの指示は先にローカルLLM(「フロントAI」、既定`mistral-small3.2:24b-instruct-2506-q4_K_M`、
  `ELABORATE_MODEL`で変更可。実装担当として使う`ollama` backend自体のモデル`OLLAMA_MODEL`とは
  別の設定)へ渡され、「実装者が迷わず実装できる具体的な指示文」に書き直されてから、選択中の
  バックエンド(claude/gemini/openai/grok/ollama)へ渡されます。
  具体化前後の内容はチャット上に「📝 ローカルLLMが指示を具体化しました」として必ず表示され、
  ユーザーから見えない書き換えは行いません。ローカルLLM呼び出しに失敗した場合は、その旨を
  表示した上で元の指示にフォールバックします(このステップの失敗でタスク全体は止めません)。
  会話継続中は最初に決めた設定が使われます。
- 「📤 エクスポート」でプロジェクトごとのエクスポート先を設定・コマンド取得できます
  (前述「成果物のエクスポート先を自由に設定する」参照)。
- **「write_file / run_command の実行前に承認を求める」** はデフォルトON。
  該当する呼び出しが発生すると、内容(ファイルパス・書き込む内容・実行コマンド)を
  表示したまま一時停止し、「すべて承認して続行」または「すべて却下」を選べます。
  却下すると、モデルはその操作を実行せず代替案を提示します。
- 承認・自動実行された変更は都度gitコミットされ、「チェックポイント」パネルに
  一覧表示されます。各チェックポイントは「差分を見る」「この状態に戻す」が可能です
  (`git reset --hard` で復元。復元前の状態も自動保存されるため、復元操作自体も
  取り消せます)。
- 右側の「プロジェクトのファイル」で、選択中プロジェクトの生成・編集済みファイル一覧と
  プレビューができます。
- ローカルLLMの場合、モデル呼び出し1回ごとに数秒〜数十秒(初回はモデルロードも含めて
  数十秒〜数分)かかることがあります。
- **「🚀 デプロイ&動作テスト」**(Dockerfileを含むプロジェクトのみ): エージェントによる
  修正が完了すると、その発言の下に「🚀 この内容をデプロイして動作確認する」ボタンが
  自動的に表示されます(押さない限り何も実行されない)。プロジェクト上部のボタンから
  いつでも手動実行することもできます。詳細は「デプロイ&動作テストAPI」節を参照。

## API

### プロジェクトAPI

```bash
curl http://localhost:8012/projects
# -> {"projects": ["default", "myapp"]}

curl -X POST http://localhost:8012/projects \
  -H "Content-Type: application/json" \
  -d '{"name": "myapp"}'

curl http://localhost:8012/projects/myapp
# -> {"project": "myapp", "export_path": null}
```

エクスポート先の設定は「成果物のエクスポート先を自由に設定する」節を参照してください。

### セッションAPI(承認フロー、推奨)

```bash
# 1. タスクを開始する(project省略時は default)。すぐに status: "running" で戻る
#    (実際の処理はバックグラウンドスレッドで進む)。
curl -X POST http://localhost:8012/agent/start \
  -H "Content-Type: application/json" \
  -d '{"task": "fizzbuzz.pyを作成して実行して", "project": "myapp", "require_approval": true}'
# -> {"session_id": "...", "project": "myapp", "status": "running", ...}

# 2. GET /agent/<session_id> を1秒程度の間隔でポーリングし、"waiting_approval" または
#    "done"/"error" になるのを待つ(Web UIはこれを自動で行う)。
curl http://localhost:8012/agent/<session_id>
# -> {"status": "waiting_approval", "pending": [...], "iterations": 2, "max_iterations": 20, ...}

# 3. 保留中の呼び出しを承認/却下して続行する。これも即座に戻り、続きはバックグラウンドで進む。
curl -X POST http://localhost:8012/agent/<session_id>/decision \
  -H "Content-Type: application/json" \
  -d '{"approve_all": true}'
# 個別に決めたい場合: {"decisions": {"<call_id>": true, "<call_id2>": false}}

# 4. 完了(done/error)したセッションに、続きの指示を送って会話を継続する(チャットUI用)。
#    それまでの会話履歴(文脈)は保持されたまま次のターンが実行される(これも即座に戻る)。
curl -X POST http://localhost:8012/agent/<session_id>/continue \
  -H "Content-Type: application/json" \
  -d '{"task": "さっき作ったファイルにテストを追加して"}'
```

`elaborate: true` を指定すると、`task` をまずローカルLLM(Ollama)で具体化してから
実装担当のbackendへ渡します(既定false)。詳細は「Web UI」節の「指示をローカルLLMで
具体化してから実装担当のAIへ渡す」を参照。

`status` は `running` / `waiting_approval` / `done` / `error` のいずれか。`start`/`continue`/
`decision` はすべて即座にレスポンスを返す(実行はバックグラウンドスレッド)。`GET /agent/{id}`
だけを繰り返し呼んで進行状況(`iterations`/`max_iterations`/`transcript`の伸び)を確認する。
`backend` は省略時 `LLM_BACKEND` 環境変数(既定 `ollama`)、`"claude"`(`ANTHROPIC_API_KEY`必要)・
`"openai"`(`OPENAI_API_KEY`必要)・`"gemini"`(`GEMINI_API_KEY`必要)・`"grok"`(`XAI_API_KEY`必要)
のいずれかを指定して切り替え可能。`/continue` はセッション作成時の
backend/project/require_approval をそのまま引き継ぐ(変更したい場合は新しいセッションを開始する)。

### チェックポイントAPI

`project` クエリパラメータで対象プロジェクトを指定します(省略時 `default`)。

```bash
curl "http://localhost:8012/checkpoints?project=myapp"
curl "http://localhost:8012/checkpoints/<sha>/diff?project=myapp"
curl -X POST "http://localhost:8012/checkpoints/<sha>/restore?project=myapp"
```

### 互換API: 承認なしの一括実行

`/agent/run` は承認を求めず、完了まで一括で自動実行する互換エンドポイントです
(旧バージョンのスクリプトや単純なcurl利用向け)。`project` は省略時 `default`。

```bash
curl -X POST http://localhost:8012/agent/run \
  -H "Content-Type: application/json" \
  -d '{"task": "fizzbuzz.pyを作成して実行して", "project": "myapp"}'
```

### デプロイ&動作テストAPI

エージェントが行った修正が「コーディングエージェント内部のワークスペース」に反映されて
いても、それだけでは**本番相当のコンテナ(例: `docker compose -p ... up`で個別に立てた
Webアプリ)には自動的に反映されません**。両者は別のファイルツリー・別のDockerイメージだからです。
この差を埋めるための、決め打ちの固定パイプラインです(LLMのチャット経由では呼び出せません
— `run_command`のアローリストに`docker`は含めていません。呼べるのはこのAPI/UIボタンのみ)。

```bash
curl -X POST http://localhost:8012/projects/myapp/deploy
```

必ず次の順番で実行され、途中で失敗したら以降には進みません(古い本番コンテナは残ります):

1. **コピー**: `workspace/projects/<name>/` → コンテナ内のステージング領域へコピー
   (編集中のワークスペースとビルド対象を分離するため、ビルドの直前に必ず実行)
2. **ビルド**: `docker build`
3. **テスト**: `tests/` があれば、ビルド直後の新イメージに対して`pytest -q`を実行。
   失敗したら中止し、本番コンテナには一切手を触れない
4. **入れ替え**: 既存の同名コンテナを停止・削除し、新イメージで起動し直す
5. **動作確認**: 「起動できた」では終わらせず、**起動したコンテナ自身の内部から**
   自分自身のポートへ実際にHTTPアクセスして応答を確認する

ポートは、プロジェクトの`docker-compose.yml`に`ports:`指定があればそれを使用し、無ければ
`Dockerfile`の`EXPOSE`(コンテナ側)+ 8100番台の自動割り当て(ホスト側、以後そのプロジェクトに
固定してメタデータに保存)を使います。固定したい場合はプロジェクトの`docker-compose.yml`に
明示的な`ports:`を書いてください。

これを実現するため、`code-agent`コンテナにはホストの`/var/run/docker.sock`をマウントし、
コンテナ内にDocker CLIを同梱しています(`docker-compose.yml`/`Dockerfile`参照)。これは
コンテナに強い権限(ホストのDocker操作権限)を与えるトレードオフを理解した上で有効化しています。

### 生成物をこのフォルダに取り出す

```bash
docker compose cp code-agent:/app/workspace/. ./workspace
```

このコマンドはDockerクライアント(このPC上のユーザーセッション)経由でファイルを
コピーするため、bind mountとは異なりNASのマップドライブ上でも問題なく動作します。
(`.git/` もコピーされますが、チェックポイント履歴なので削除しないでください。)

## 安全上の制約

- ファイル操作・コマンド実行はすべて対象プロジェクト(`workspace/projects/<名前>/`)配下に
  限定されます(パストラバーサル対策込み。プロジェクト名自体も英数字・`-`・`_`のみに制限)。
- `run_command` はアローリスト方式で、`python`, `pip`, `pytest`, `node`, `npm`, `npx`, `git`,
  `ls`, `mkdir` のみ実行可能です。シェル演算子(`&&` `|` `;` など)は拒否されます。`docker`は
  このアローリストに**含めていません**— LLMがチャット経由で任意のDocker操作を行うことは
  できません。「デプロイ&動作テスト」機能(`POST /projects/{name}/deploy`)は、これとは別の
  固定パイプライン(コピー→ビルド→テスト→入れ替え→動作確認のみ、決め打ちの手順)として
  独立に実装されています(詳細は「デプロイ&動作テストAPI」節)。
- **write_file / run_command、および副作用のありそうなMCPツールは既定で承認制**です
  (`require_approval: false` または `/agent/run` で無効化可能ですが、非対話的な自動実行に
  なるため注意してください)。同一ターン内に承認が必要な呼び出しが1つでもあれば、
  ターン全体(承認不要な呼び出しも含む)を保留し、承認確定後に元の順序で実行します
  (例: `create_table`→`write_query`→`read_query` の依存関係を壊さないため)。
- 承認・自動実行された変更はすべてgitチェックポイントとして記録され、いつでも
  `git reset --hard` 相当のロールバックができます(復元前の状態も自動保存)。
- 1タスクあたりのツール呼び出しループ回数には上限(既定20回、`max_iterations`で変更可)があります。
- セッション(会話)状態はプロセスメモリ上に保持しつつ、`src/session_store.py`が
  `workspace_data`(永続named volume)上のSQLite(`sessions.db`)へも都度書き込みます。
  起動時に読み戻すため、コンテナ再起動をまたいでも会話(承認待ち状態も含む)は消えません
  (2026-09-10以前は消えていました。単一ユーザーのローカル開発ツールという前提です)。
  会話の記録は**自動削除されず、永続的に残ります**(以前は6時間で削除していたが、開発指示や
  作業経緯の記録が失われるため廃止)。環境変数`SESSION_TTL_SEC`に正の秒数を設定した場合のみ、
  その期間を過ぎた会話が削除されます。`workspace_data`ボリュームを削除しない限り残ります。
- ローカルLLM(Ollama)は無料・オフラインで使えますが、コーディング精度・指示追従は
  Claude/OpenAI/Gemini/Grokより劣ります。特に**複雑な多ファイル実装や事実確認が必要なタスクでは、
  ツールを呼ばずに完了したふりをする(結果を捏造する)ことを複数モデルで確認済み**です
  (上記「モデル選定について」参照)。品質重視のタスクは `"backend": "claude"` / `"openai"` /
  `"gemini"` / `"grok"` のいずれかを使ってください。

## MCPサーバー連携

`mcp_servers.json` に列挙したMCPサーバーへ、コンテナ起動時に自動接続します。既定では
以下の2つが有効です(いずれもAPIキー不要):

| サーバー | ツール | 用途 |
|---|---|---|
| `duckduckgo`(`duckduckgo-mcp-server`) | `search`, `fetch_content` | Web検索・ページ内容取得。読み取り専用のため承認不要で自動実行されます。 |
| `sqlite`(`mcp-server-sqlite`) | `read_query`, `write_query`, `create_table`, `list_tables`, `describe_table`, `append_insight` | `workspace/projects/default/agent.db` へのクエリ実行(**default プロジェクト固定**、上記「プロジェクト」節参照)。`write_query`/`create_table`/`append_insight` はツール名に書き込み系のキーワードを含むため承認制、`read_query`/`list_tables`/`describe_table` は自動実行されます。 |

MCPツールは `mcp_<サーバー名>_<ツール名>`(例: `mcp_duckduckgo_search`)という名前で
組み込みツールと同列に扱われ、`GET /mcp/tools` で一覧を確認できます。承認要否は
「ツール名に write/delete/drop/insert/update/create/exec/remove/modify/alter/append の
いずれかが含まれるか」で機械的に判定しています(ヒューリスティックです)。

**サーバーを追加・変更する場合** は `mcp_servers.json` を編集して再ビルドしてください
(`[{"name": "...", "command": "uvx", "args": [...]}]` の配列)。多くのMCPサーバーは
`uvx <パッケージ名>` で起動できますが、パッケージによっては最新の `mcp` SDK
(このプロジェクトは `mcp>=1.2.0` を使用、実際は2.x系が入ります)に未対応な場合があります
(`mcp-server-sqlite` がまさにそうでした — 対処として
`"args": ["--with", "mcp<2", "mcp-server-sqlite", ...]` のように `uvx --with` で
そのサーバー専用の依存バージョンを固定しています)。接続に失敗したサーバーは
`docker compose logs code-agent` にエラーが出ますが、他のサーバーやアプリ本体の起動には
影響しません。

## ログ・デバッグ

```bash
docker compose logs -f
docker compose logs -f ollama
```

## 貢献・ライセンス

- 不具合報告・機能要望は [Issue](https://github.com/Nemosoft1963/llmhib-code/issues) から。
  開発手順・PRの出し方は [CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。
- 参加にあたっては [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) に同意してください。
- 脆弱性は公開Issueにせず、[Security Advisory](https://github.com/Nemosoft1963/llmhib-code/security/advisories/new)
  から報告してください。信頼境界・秘密情報の扱いは [SECURITY.md](SECURITY.md) を参照。
- ライセンスは [Apache License 2.0](LICENSE) です。
