<#
既存プロジェクトフォルダを、ジェンキンス(コード作成エージェント)へ取り込む準備をする。

  scripts\stage-import.ps1 -Source "Z:\AI_Automater\LOCALSAPORTER"

- 元フォルダは変更しない(読み取りのみ)。
- 機密・生成物を除外してローカル一時フォルダへコピー → コンテナ内のステージング領域へ転送。
- その後 UI の「既存プロジェクトの取り込み」またはAPI(POST /projects/import)で取り込む。
#>
param(
  [Parameter(Mandatory = $true)][string]$Source,
  [string]$Container = "llmhib-code-agent"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Source -PathType Container)) { throw "フォルダが見つかりません: $Source" }
$name = Split-Path -Leaf ($Source.TrimEnd('\', '/'))
$tmp = Join-Path $env:TEMP ("jenkins-stage-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  $xd = @(".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
          ".ruff_cache", ".tox", ".idea", ".vscode", ".codex", "dist", "build", "backups", "logs", "temp",
          "tmp", "data", "workspace", ".next", "*.egg-info")
  $xf = @(".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "id_rsa*", "id_ed25519*", ".netrc",
          "*credential*", "*secret*", "*.token", "*.keystore", "*.pyc", "*.db", "*.sqlite", "*.sqlite3")
  # .env.example 等は残したいので、除外後に個別に拾い直す
  $null = robocopy $Source $tmp /E /XJ /MAX:1000000 /R:1 /W:1 /NFL /NDL /NJH /NJS /NP /XD @xd /XF @xf
  if ($LASTEXITCODE -ge 8) { throw "robocopy が失敗しました (code $LASTEXITCODE)" }
  foreach ($f in @(".env.example", ".env.sample", ".env.template")) {
    $p = Join-Path $Source $f
    if (Test-Path -LiteralPath $p) { Copy-Item -LiteralPath $p -Destination $tmp -Force }
  }
  docker exec $Container sh -c "rm -rf /app/workspace/_import/'$name' && mkdir -p /app/workspace/_import"
  docker cp "$tmp" "${Container}:/app/workspace/_import/$name"
  if ($LASTEXITCODE -ne 0) { throw "docker cp が失敗しました" }
  $n = (Get-ChildItem -LiteralPath $tmp -Recurse -File).Count
  Write-Host "ステージング完了: $name ($n ファイル)。次に取り込みを実行してください。"
} finally {
  Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
