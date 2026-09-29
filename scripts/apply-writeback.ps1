<#
ジェンキンスで改修した内容を、元のフォルダへ書き戻す。

  # 1) まず確認だけ(何も変更しない)
  scripts\apply-writeback.ps1 -Project LOCALSAPORTER -Target "Z:\AI_Automater\LOCALSAPORTER"
  # 2) 問題なければ反映
  scripts\apply-writeback.ps1 -Project LOCALSAPORTER -Target "Z:\AI_Automater\LOCALSAPORTER" -Apply

安全策:
- 既定はドライラン。-Apply を付けたときだけ書き込む。
- 取り込み後に元フォルダ側のファイルが変わっていたら(競合)、何も書かずに終了する。
- 変更・削除する元ファイルは、書き込み前に <Target>\.jenkins_backup\<日時>\ へ退避する。
- 削除は -AllowDelete を付けたときだけ行う。
- 書き込み後、SHA-256 で内容を検証する。
#>
param(
  [Parameter(Mandatory = $true)][string]$Project,
  [Parameter(Mandatory = $true)][string]$Target,
  [switch]$Apply,
  [switch]$AllowDelete,
  [string]$Container = "llmhib-code-agent",
  [string]$Api = "http://localhost:8012"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Target -PathType Container)) { throw "書き戻し先が見つかりません: $Target" }
$Target = (Resolve-Path -LiteralPath $Target).Path.TrimEnd('\')
$tmp = Join-Path $env:TEMP ("jenkins-writeback-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  $url = "$Api/projects/" + [uri]::EscapeDataString($Project) + "/writeback/bundle"
  $bytes = [System.Text.Encoding]::UTF8.GetBytes("{}")
  $resp = Invoke-RestMethod -Method Post -Uri $url -ContentType "application/json; charset=utf-8" -Body $bytes
  docker cp "${Container}:$($resp.bundle_path)/." "$tmp"
  if ($LASTEXITCODE -ne 0) { throw "バンドルの取得に失敗しました" }
  $manifest = Get-Content -LiteralPath (Join-Path $tmp "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json

  $srcLeaf = (($manifest.import_source.Replace('\', '/')).TrimEnd('/') -split '/')[-1]
  $dstLeaf = Split-Path -Leaf $Target
  if ($srcLeaf -and ($srcLeaf -ne $dstLeaf)) { throw "書き戻し先の名前が取り込み元と異なります(元: $srcLeaf / 指定: $dstLeaf)" }

  function Get-Hash($p) { (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower() }
  $plan = @()
  foreach ($c in $manifest.changes) {
    $dest = Join-Path $Target ($c.path -replace '/', '\')
    $exists = Test-Path -LiteralPath $dest -PathType Leaf
    $action = ""; $note = ""
    switch ($c.status) {
      "A" {
        if (-not $exists) { $action = "add" }
        elseif ((Get-Hash $dest) -eq $c.new_sha256) { $action = "same"; $note = "既に同じ内容" }
        else { $action = "conflict"; $note = "元に同名の別内容のファイルがある" }
      }
      "M" {
        if (-not $exists) { $action = "conflict"; $note = "元に存在しない" }
        else {
          $h = Get-Hash $dest
          if ($h -eq $c.new_sha256) { $action = "same"; $note = "既に同じ内容" }
          elseif ($h -ne $c.base_sha256) { $action = "conflict"; $note = "取り込み後に元が変更されている" }
          else { $action = "modify" }
        }
      }
      "D" {
        if (-not $exists) { $action = "same"; $note = "既に無い" }
        elseif ((Get-Hash $dest) -ne $c.base_sha256) { $action = "conflict"; $note = "取り込み後に元が変更されている" }
        elseif ($AllowDelete) { $action = "delete" }
        else { $action = "skip-delete"; $note = "-AllowDelete が無いため削除しない" }
      }
    }
    $plan += [pscustomobject]@{ Action = $action; Path = $c.path; Note = $note; Status = $c.status; New = $c.new_sha256 }
  }
  $plan | ForEach-Object { "{0,-12} {1}  {2}" -f $_.Action, $_.Path, $_.Note } | Write-Host
  if ($manifest.blocked_secret_files.Count -gt 0) {
    Write-Host "注意: 秘密情報の形を含むため除外したファイル: $(($manifest.blocked_secret_files | ForEach-Object { $_.path }) -join ', ')"
  }
  $conflicts = @($plan | Where-Object { $_.Action -eq "conflict" })
  if ($conflicts.Count -gt 0) { Write-Host "競合が $($conflicts.Count) 件あります。何も書き込まずに終了します。"; exit 2 }
  $todo = @($plan | Where-Object { $_.Action -in @("add", "modify", "delete") })
  if (-not $Apply) { Write-Host "ドライラン: 反映対象 $($todo.Count) 件。反映するには -Apply を付けてください。"; exit 0 }
  if ($todo.Count -eq 0) { Write-Host "反映する変更はありません。"; exit 0 }

  $ts = Get-Date -Format "yyyyMMdd-HHmmss"
  $backup = Join-Path $Target ".jenkins_backup\$ts"
  foreach ($p in $todo) {
    $dest = Join-Path $Target ($p.Path -replace '/', '\')
    if ($p.Action -in @("modify", "delete")) {
      $b = Join-Path $backup ($p.Path -replace '/', '\')
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $b) | Out-Null
      Copy-Item -LiteralPath $dest -Destination $b -Force
    }
  }
  foreach ($p in $todo) {
    $dest = Join-Path $Target ($p.Path -replace '/', '\')
    if ($p.Action -eq "delete") { Remove-Item -LiteralPath $dest -Force; continue }
    $src = Join-Path (Join-Path $tmp "files") ($p.Path -replace '/', '\')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
    Copy-Item -LiteralPath $src -Destination $dest -Force
    if ((Get-Hash $dest) -ne $p.New) { throw "書き込み後の検証に失敗しました: $($p.Path)" }
  }
  if (Test-Path -LiteralPath $backup) { Copy-Item -LiteralPath (Join-Path $tmp "manifest.json") -Destination (Join-Path $backup "manifest.json") }
  Write-Host "反映しました: $($todo.Count) 件(内容をSHA-256で検証済み)。バックアップ: $backup"
} finally {
  Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
