FROM python:3.14-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# チェックポイント(git)機能に必要
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# 「デプロイ&動作テスト」機能用のDocker CLI(静的バイナリ)。
# 本体のDockerデーモンには接続しない(docker-compose.ymlでホストの
# /var/run/docker.sockをマウントして初めて使えるようになる)。
# 注意: これはLLMのツールとしては公開しない(run_commandのALLOWLISTにdockerは無い)。
# あくまでWeb UIの固定エンドポイント(/projects/{name}/deploy)専用。
ARG DOCKER_CLI_VERSION=27.3.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
    | tar xz --strip-components=1 -C /usr/local/bin docker/docker

# 依存パッケージのインストール(レイヤーキャッシュを効かせるため先にコピー)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# MCPサーバー(duckduckgo-mcp-server, mcp-server-sqlite など)を `uvx` で
# オンデマンド起動するために使う
RUN pip install --no-cache-dir uv

# アプリケーション本体
COPY src ./src
COPY mcp_servers.json .

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
