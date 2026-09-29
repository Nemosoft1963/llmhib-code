"""「デプロイ&動作テスト」機能(固定手順、LLMのツールとしては公開しない)。

Web UIの明示的なボタン操作からのみ呼ばれる、決め打ちの手順:
  1. コピー   : プロジェクトのワークスペース → デプロイ用ステージングディレクトリ
               (必ずビルドの直前に実行し、ビルド対象を編集中のワークスペースと分離する)
  2. ビルド   : docker build でステージングディレクトリからイメージを作成
  3. テスト   : tests/ があれば、ステージングディレクトリ一式(research.md等の
               テスト用データ含む)を使い捨てコンテナへ丸ごとコピーして pytest を実行。
               (本番イメージ自体は最小構成のためtests/を含まない想定であり、ビルド済み
               イメージの中で直接pytestを走らせると「無いのが正常」なのに落ちてしまう)
               失敗したら、ここで中止して本番コンテナには一切手を触れない。
  4. 入れ替え : 既存の本番コンテナを止めて、新しいイメージで起動し直す
  5. 動作確認 : 起動したコンテナ自身の中から、自分自身のポートへHTTPアクセスして
               実際に応答が返ることを確認する(コンテナが起動しただけでは「動作した」と
               見なさない)

コンテナ内から `docker` コマンドを呼べるのは、docker-compose.yml で
`/var/run/docker.sock` をマウントしているため(ホストのDockerデーモンを操作する)。
これはこの固定パイプライン専用であり、LLMがチャット経由で任意のdocker操作を
行えるようにするものではない(run_commandのアローリストにdockerは含めない)。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from pathlib import Path

from .tools_core import PROJECTS_ROOT, get_project_meta, get_workspace, set_project_meta

DEPLOY_STAGING_ROOT = Path("/app/deploy_staging")

BUILD_TIMEOUT_SEC = 600
TEST_TIMEOUT_SEC = 180
RUN_TIMEOUT_SEC = 60
SMOKE_TEST_TIMEOUT_SEC = 15
SMOKE_TEST_RETRIES = 10
SMOKE_TEST_INTERVAL_SEC = 1.5

# コンテナ内で動作確認(HTTPアクセス)に使うコマンドの候補。イメージに何が
# 入っているか事前には分からないため、上から順に試して最初に成功したものを使う。
_SMOKE_TEST_COMMANDS = [
    ["curl", "-sf", "-o", "/dev/null", "-w", "%{{http_code}}", "http://localhost:{port}/"],
    ["wget", "-qO-", "-T", "5", "http://localhost:{port}/"],
    [
        "python",
        "-c",
        "import urllib.request;"
        "print(urllib.request.urlopen('http://localhost:{port}/', timeout=5).status)",
    ],
    [
        "python3",
        "-c",
        "import urllib.request;"
        "print(urllib.request.urlopen('http://localhost:{port}/', timeout=5).status)",
    ],
]


def _slug_for(project: str) -> str:
    """プロジェクト名(日本語含む)から、docker のコンテナ/イメージ名に使える
    安定したASCIIスラッグを作り、プロジェクトのメタデータに保存する。"""
    meta = get_project_meta(project)
    slug = meta.get("deploy_slug")
    if not slug:
        slug = "deploy-" + hashlib.sha1(project.encode("utf-8")).hexdigest()[:10]
        set_project_meta(project, deploy_slug=slug)
    return slug


def _bind_ip_for(staging_dir: Path) -> str | None:
    """docker-compose.ymlのports指定に "127.0.0.1:8088:8080" のようにバインド先IPがあれば返す。

    これを無視して -p 8088:8080 で起動すると、compose側で「127.0.0.1のみ公開」と決めて
    いても0.0.0.0(LAN全体)に公開されてしまう。実際に、認証なしのUIを127.0.0.1限定にする
    仕様のプロジェクトがLANから見える状態でデプロイされた事故があったため対応した。
    """
    compose_path = staging_dir / "docker-compose.yml"
    if not compose_path.is_file():
        return None
    text = compose_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"[\"']?(\d{1,3}(?:\.\d{1,3}){3}):\d{2,5}\s*:\s*\d{2,5}[\"']?", text)
    return m.group(1) if m else None


def _named_volumes_for(staging_dir: Path) -> list[tuple[str, str]]:
    """docker-compose.ymlのvolumesに書かれた名前付きボリューム("name:/path")を返す。

    これを無視して docker run すると、コンテナを入れ替える(再デプロイ)たびにSQLite等の
    データが消える。バインドマウント("./x:/y" や "/abs:/y")は対象外(名前が英字始まりで
    パス区切りを含まないものだけ拾う)。
    """
    compose_path = staging_dir / "docker-compose.yml"
    if not compose_path.is_file():
        return []
    text = compose_path.read_text(encoding="utf-8", errors="ignore")
    pattern = r"^\s*-\s*[\"']?([A-Za-z][\w.-]*):(/[^\s\"':]+)(?::\w+)?[\"']?\s*$"
    return [(m.group(1), m.group(2)) for m in re.finditer(pattern, text, re.MULTILINE)]


def _env_file_for(project: str) -> Path | None:
    """プロジェクトごとの実行時環境変数ファイル(PROJECTS_ROOT/.<名前>.deploy.env)。

    連絡先メール等、gitやプロジェクトのファイルに残したくない設定をコンテナへ渡す。
    プロジェクトのフォルダの外に置くため、gitの履歴やエクスポートには含まれない。
    """
    path = PROJECTS_ROOT / f".{project}.deploy.env"
    return path if path.is_file() else None


def _port_for(project: str, staging_dir: Path) -> tuple[int, int]:
    """(ホスト側ポート, コンテナ側ポート) を決める。

    優先順位: 1) プロジェクト自身のdocker-compose.ymlの`ports:`指定
              2) 過去のデプロイで自動割り当て済みのポート(メタデータに保存済み)
              3) 新規に8100番台から自動割り当て(以後そのプロジェクトに固定)
    コンテナ側ポートはDockerfileの`EXPOSE`から読む(無ければ8000)。
    """
    container_port = 8000
    dockerfile_path = staging_dir / "Dockerfile"
    if dockerfile_path.is_file():
        m = re.search(
            r"^\s*EXPOSE\s+(\d+)",
            dockerfile_path.read_text(encoding="utf-8", errors="ignore"),
            re.MULTILINE,
        )
        if m:
            container_port = int(m.group(1))

    compose_path = staging_dir / "docker-compose.yml"
    if compose_path.is_file():
        text = compose_path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"[\"']?(\d{2,5})\s*:\s*(\d{2,5})[\"']?", text)
        if m:
            return int(m.group(1)), int(m.group(2))

    meta = get_project_meta(project)
    host_port = meta.get("deploy_port")
    if not host_port:
        h = int(hashlib.sha1(project.encode("utf-8")).hexdigest(), 16)
        host_port = 8100 + (h % 100)
        set_project_meta(project, deploy_port=host_port)
    return int(host_port), container_port


def _base_image_for(staging_dir: Path) -> str:
    """テスト実行用の使い捨てコンテナに使うベースイメージ。

    プロジェクトのDockerfileの`FROM`がPython系イメージなら、それに合わせることで
    実行時のPythonバージョン差異等によるテスト結果のズレを防げる。ただし本番用の
    Dockerfileは(nginx・node等)Python自体を含まないことが多い(実際に、静的サイトを
    nginx:alpineで配信するプロジェクトでpipが無く`pytest`のインストール自体に失敗した
    ケースを確認済み)。そのため、FROMがPython系だと判断できる場合のみそれを使い、
    それ以外は常にpython:3.12-slim(pip入り)にフォールバックする。
    """
    dockerfile_path = staging_dir / "Dockerfile"
    if dockerfile_path.is_file():
        m = re.search(
            r"^\s*FROM\s+(\S+)",
            dockerfile_path.read_text(encoding="utf-8", errors="ignore"),
            re.MULTILINE,
        )
        if m and "python" in m.group(1).lower():
            return m.group(1)
    return "python:3.12-slim"


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _truncate(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "…(省略)…\n" + text[-limit:]


def deploy_and_test(project: str) -> dict:
    """コピー→ビルド→テスト→起動→動作確認を、この順番で必ず実行する。"""
    workspace = get_workspace(project)  # 存在確認・名前検証(不正ならInvalidProjectNameError)

    dockerfile = workspace.root / "Dockerfile"
    if not dockerfile.is_file():
        raise ValueError(
            "このプロジェクトには Dockerfile がありません。"
            "「デプロイ&動作テスト」はDockerfileを含むプロジェクト(Webアプリ等の成果物)のみ対象です。"
        )

    slug = _slug_for(project)
    staging_dir = DEPLOY_STAGING_ROOT / slug
    tag = f"{slug}:{int(time.time())}"

    steps: list[dict] = []
    result: dict = {"project": project, "steps": steps, "ok": False}

    # --- 1. コピー(必ずビルドの直前に実行する) ---
    step = {"step": "copy", "label": "ワークスペース → デプロイ用ステージングへコピー"}
    try:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        shutil.copytree(
            workspace.root,
            staging_dir,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
        )
        step["ok"] = True
        step["detail"] = f"{workspace.root} → {staging_dir}"
    except OSError as e:
        step["ok"] = False
        step["detail"] = str(e)
        steps.append(step)
        result["error"] = "コピーに失敗しました"
        return result
    steps.append(step)

    host_port, container_port = _port_for(project, staging_dir)
    bind_ip = _bind_ip_for(staging_dir)
    publish = f"{bind_ip}:{host_port}:{container_port}" if bind_ip else f"{host_port}:{container_port}"

    # --- 2. ビルド ---
    step = {"step": "build", "label": f"docker build -t {tag} ."}
    try:
        proc = _run(["docker", "build", "-t", tag, str(staging_dir)], timeout=BUILD_TIMEOUT_SEC)
        step["ok"] = proc.returncode == 0
        step["detail"] = _truncate((proc.stdout or "") + "\n" + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        step["ok"] = False
        step["detail"] = f"タイムアウトしました({BUILD_TIMEOUT_SEC}秒)"
    except FileNotFoundError:
        step["ok"] = False
        step["detail"] = (
            "dockerコマンドが見つかりません。"
            "コンテナにDocker CLIが入っていないか、docker-compose.ymlで"
            "/var/run/docker.sockがマウントされていない可能性があります。"
        )
    steps.append(step)
    if not step["ok"]:
        result["error"] = "ビルドに失敗しました"
        return result

    # --- 3. テスト(tests/ があるときだけ。失敗したら本番へは反映しない) ---
    # 本番イメージ(tag)自体は最小構成のため tests/ や research.md 等の開発用ファイルを
    # 含まない前提。よって「ビルド済みイメージの中でpytestを直接実行」はしない
    # (含まれていないのが正常な状態でも「テスト失敗」に見えてしまうため)。
    # 代わりに、ステージングディレクトリ一式をコピーした使い捨てコンテナの中で実行する。
    has_tests = (staging_dir / "tests").is_dir()
    test_container = f"{slug}-test"
    if has_tests:
        base_image = _base_image_for(staging_dir)
        step = {
            "step": "test",
            "label": f"使い捨てコンテナ({base_image})にソース一式をコピーして pytest -q を実行",
        }
        try:
            _run(["docker", "rm", "-f", test_container], timeout=30)  # 前回の残骸があれば掃除
            create_proc = _run(
                ["docker", "run", "-d", "--name", test_container, "--entrypoint", "sleep", base_image, "900"],
                timeout=120,
            )
            if create_proc.returncode != 0:
                step["ok"] = False
                step["detail"] = "テスト用コンテナの起動に失敗しました:\n" + _truncate(
                    (create_proc.stdout or "") + "\n" + (create_proc.stderr or "")
                )
            else:
                cp_proc = _run(
                    ["docker", "cp", f"{staging_dir}/.", f"{test_container}:/app"], timeout=60
                )
                if cp_proc.returncode != 0:
                    step["ok"] = False
                    step["detail"] = "ソースのコピーに失敗しました:\n" + _truncate(
                        (cp_proc.stdout or "") + "\n" + (cp_proc.stderr or "")
                    )
                else:
                    test_cmd = (
                        "cd /app && "
                        "(test -f requirements.txt && pip install --quiet -r requirements.txt || true) && "
                        "pip install --quiet pytest && pytest -q"
                    )
                    proc = _run(
                        ["docker", "exec", test_container, "sh", "-c", test_cmd],
                        timeout=TEST_TIMEOUT_SEC,
                    )
                    step["ok"] = proc.returncode == 0
                    step["detail"] = _truncate((proc.stdout or "") + "\n" + (proc.stderr or ""))
        except subprocess.TimeoutExpired:
            step["ok"] = False
            step["detail"] = f"タイムアウトしました({TEST_TIMEOUT_SEC}秒)"
        finally:
            _run(["docker", "rm", "-f", test_container], timeout=30)
        steps.append(step)
        if not step["ok"]:
            result["error"] = "テストが失敗したため、本番コンテナへの反映を中止しました"
            _run(["docker", "rmi", "-f", tag], timeout=30)
            return result
    else:
        steps.append(
            {
                "step": "test",
                "label": "pytest",
                "ok": None,
                "detail": "tests/ ディレクトリが無いためスキップしました",
            }
        )

    # --- 4. 既存コンテナを新イメージへ入れ替え ---
    step = {"step": "up", "label": f"docker run -d --name {slug} -p {publish} <新イメージ>"}
    try:
        _run(["docker", "rm", "-f", slug], timeout=30)  # 既存が無ければ黙って失敗するだけで問題ない
        run_cmd = ["docker", "run", "-d", "--name", slug, "-p", publish, "--restart", "unless-stopped"]
        for vol_name, vol_path in _named_volumes_for(staging_dir):
            run_cmd += ["-v", f"{slug}_{vol_name}:{vol_path}"]  # slugで接頭辞を付け、他プロジェクトと衝突させない
        env_file = _env_file_for(project)
        if env_file is not None:
            run_cmd += ["--env-file", str(env_file)]
            # 設定ファイル内の注釈行 "# deploy-network: <ネットワーク名>" で、参加させる
            # Dockerネットワークを指定できる(docker自身は#行を無視する)。例: ホストの別のOllama
            # ではなく、llmhib-ollamaへ名前で直接つなぐために llmhibcode_default に参加させる。
            m = re.search(r"^#\s*deploy-network:\s*(\S+)\s*$", env_file.read_text(encoding="utf-8"), re.MULTILINE)
            if m:
                run_cmd += ["--network", m.group(1)]
        run_cmd.append(tag)
        proc = _run(
            run_cmd,
            timeout=RUN_TIMEOUT_SEC,
        )
        step["ok"] = proc.returncode == 0
        step["detail"] = _truncate((proc.stdout or "") + "\n" + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        step["ok"] = False
        step["detail"] = f"タイムアウトしました({RUN_TIMEOUT_SEC}秒)"
    steps.append(step)
    if not step["ok"]:
        result["error"] = "コンテナの起動に失敗しました"
        return result

    # --- 5. 動作確認(スモークテスト): 「起動した」で終わらせず、実際にHTTP応答を確認する ---
    step = {"step": "smoke_test", "label": f"コンテナ内から http://localhost:{container_port}/ へアクセス"}
    ok = False
    detail = "応答を確認できませんでした"
    for _attempt in range(SMOKE_TEST_RETRIES):
        found_client = False
        for template in _SMOKE_TEST_COMMANDS:
            cmd = [part.format(port=container_port) for part in template]
            try:
                proc = _run(["docker", "exec", slug, *cmd], timeout=SMOKE_TEST_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                detail = "タイムアウトしました"
                continue
            # returncode 126/127 系は「そのコマンド自体がコンテナに無い」ことが多いので次の候補へ。
            if proc.returncode in (126, 127) or "not found" in (proc.stderr or "").lower():
                continue
            found_client = True
            output = (proc.stdout or "").strip()
            if proc.returncode == 0 and (output.startswith("2") or "200" in output):
                ok = True
                detail = f"応答を確認しました: {output or 'HTTP 200'}"
                break
            detail = _truncate((output + "\n" + (proc.stderr or "")).strip() or f"終了コード {proc.returncode}")
        if ok:
            break
        if not found_client:
            detail = "コンテナ内にcurl/wget/pythonが見つからず、内部からの動作確認ができませんでした"
            break
        time.sleep(SMOKE_TEST_INTERVAL_SEC)
    step["ok"] = ok
    step["detail"] = detail
    steps.append(step)

    result["ok"] = ok
    result["host_port"] = host_port
    result["container_name"] = slug
    result["url"] = f"http://localhost:{host_port}/"
    if not ok:
        result["error"] = "コンテナは起動しましたが、動作確認(HTTPアクセス)には失敗しました"

    return result
