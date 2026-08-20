<#
.SYNOPSIS
  打包当前 git 仓库的改动文件为 zip（供其他电脑解压覆盖）。

.DESCRIPTION
  从 git 解析「修改 + 新增（含未跟踪）」文件清单，用 7z 压缩为 zip。
  默认排除删除项（zip 无法表达删除动作）。目标电脑解压到仓库根目录覆盖即可。

  清单来源：
    - git diff --name-only --diff-filter=ACMRT HEAD  （已跟踪文件的非删除改动）
    - git ls-files --others --exclude-standard      （未跟踪文件，遵守 .gitignore）

.PARAMETER Out
  输出 zip 路径。相对路径按仓库根目录解析。默认 changes.zip。

.PARAMETER IncludeDeleted
  包含删除项（仅会列出文件名，实际无文件可打包，一般无用）。

.EXAMPLE
  .\scripts\pack_changes.ps1
  .\scripts\pack_changes.ps1 -Out D:\share\mfabd2-changes.zip
#>
param(
    [string]$Out = "changes.zip",
    [switch]$IncludeDeleted
)

$ErrorActionPreference = "Stop"
$listFile = $null

# --- 定位仓库根 ---
$repoRoot = git rev-parse --show-toplevel 2>$null
if (-not $repoRoot) {
    Write-Error "当前不在 git 仓库内，无法定位仓库根目录。"
    exit 1
}

Push-Location $repoRoot
try {
    # --- 检测 7z ---
    $sevenZip = (Get-Command 7z.exe -ErrorAction SilentlyContinue).Source
    if (-not $sevenZip) {
        $candidates = @(
            "C:\Program Files\7-Zip\7z.exe",
            "C:\Program Files (x86)\7-Zip\7z.exe"
        )
        $sevenZip = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $sevenZip) {
        Write-Error "未找到 7z。请安装 7-Zip 或将其加入 PATH。"
        exit 1
    }
    Write-Host "7z      : $sevenZip"
    Write-Host "repo    : $repoRoot"

    # --- 生成文件清单 ---
    $diffFilter = if ($IncludeDeleted) { "ACDMRT" } else { "ACMRT" }
    $tracked = git diff --name-only --diff-filter=$diffFilter HEAD
    $untracked = git ls-files --others --exclude-standard

    $files = @($tracked) + @($untracked) |
        Where-Object { $_ } |
        ForEach-Object { ($_ -replace '/', '\').Trim('"') } |
        Sort-Object -Unique

    if ($files.Count -eq 0) {
        Write-Host "没有改动文件可打包。" -ForegroundColor Yellow
        exit 0
    }

    Write-Host "files   : $($files.Count) 项"
    $files | ForEach-Object { Write-Host "  $_" }

    # --- 写无 BOM UTF-8 listfile（避免 BOM 被当成文件名）---
    $listFile = [IO.Path]::GetTempFileName()
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [IO.File]::WriteAllLines($listFile, $files, $utf8NoBom)

    # --- 解析输出路径 ---
    $outPath = $Out
    if (-not [IO.Path]::IsPathRooted($outPath)) {
        $outPath = Join-Path $repoRoot $outPath
    }
    $outDir = Split-Path $outPath -Parent
    if ($outDir -and -not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }
    if (Test-Path $outPath) { Remove-Item $outPath -Force }

    # --- 7z 打包（-scsUTF-8 让 7z 按 UTF-8 解读清单，兼容中文路径）---
    Write-Host "packing : $outPath"
    & $sevenZip a -tzip -mx=9 -scsUTF-8 $outPath "@$listFile"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "7z 打包失败，退出码 $LASTEXITCODE。"
        exit $LASTEXITCODE
    }

    # --- 摘要 ---
    $sizeKB = [math]::Round((Get-Item $outPath).Length / 1KB, 1)
    Write-Host ""
    Write-Host "done    : $outPath ($sizeKB KB)" -ForegroundColor Green
    Write-Host "解压到仓库根目录覆盖即可: $repoRoot"
}
finally {
    if ($listFile -and (Test-Path $listFile)) {
        Remove-Item $listFile -Force -ErrorAction SilentlyContinue
    }
    Pop-Location
}
