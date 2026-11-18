<#
Build the per-user installer jev-setup-<version>.exe, on Windows.

    scripts\build-helper.ps1                         unsigned
    scripts\build-helper.ps1 -Thumbprint <sha1>      signed with a code-signing certificate in your store

The installer carries jev.exe and the perception's model files. Installing it needs no further
command: it lands in %LOCALAPPDATA%\Programs\jev and shows the MCP config. Building runs nothing
on the screen and does not start the helper. It does download the model files.
#>
param(
    [string]$Thumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [string]$Out = "dist"
)
$ErrorActionPreference = "Stop"
# Resolve-Path .Path is a PowerShell provider path (FileSystem::\\server\share) when the repo
# lives on a UNC path, which Python and ISCC cannot open. ProviderPath is the Win32 path.
function WinPath([string]$Path) {
    return (Resolve-Path -LiteralPath $Path).ProviderPath
}
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $version = (uv run python -c "from importlib.metadata import version; print(version('typesafe-computer-use'))").Trim()
    Push-Location packaging\windows
    try {
        uv run --with "pyinstaller>=6.11,<7" pyinstaller --noconfirm --clean `
            --distpath "$root\build\helper\dist" --workpath "$root\build\helper\work" jev.spec
        if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
    } finally { Pop-Location }

    $folder = "$root\build\helper\dist\jev"
    if ($Thumbprint) {
        signtool sign /sha1 $Thumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 "$folder\jev.exe"
        if ($LASTEXITCODE -ne 0) { throw "signtool failed" }
    }
    Set-Content -Path "$folder\version.txt" -Value $version -NoNewline -Encoding ascii

    # The hub takes a Windows file lock in its cache. That lock fails on a UNC path, which is
    # what a WSL checkout looks like from Windows, so the download lands on the local disk and
    # is copied into the installer tree. Symlinks are disabled: the installer needs real files.
    $models = "$root\build\helper\models"
    $cache = Join-Path $env:LOCALAPPDATA "jev-build\hf-cache"
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $env:HF_HUB_CACHE = $cache
    $env:HF_HUB_DISABLE_SYMLINKS = "1"
    $env:HF_HUB_OFFLINE = "0"
    $env:TRANSFORMERS_OFFLINE = "0"
    uv run clicker-models fetch
    if ($LASTEXITCODE -ne 0) { throw "clicker-models fetch failed" }
    if (Test-Path -LiteralPath $models) { Remove-Item -LiteralPath $models -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $models | Out-Null
    & robocopy $cache (WinPath $models) /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying the model cache failed ($LASTEXITCODE)" }
    $links = @(Get-ChildItem -LiteralPath $models -Recurse -Force | Where-Object {
        $_.LinkType -or ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
    })
    if ($links.Count -gt 0) {
        throw "the model cache contains symlinks, and the installer needs real files: $($links.FullName -join ', ')"
    }

    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) was not found" }
    New-Item -ItemType Directory -Force -Path $Out | Out-Null
    $outDir = WinPath $Out
    # A normalized absolute path. A relative one that still contains ".." is longer, and the
    # longest file then exceeds the 260-character limit before Windows collapses the "..".
    $distArg = WinPath $folder
    $modelsArg = WinPath $models
    & $iscc "$root\packaging\windows\jev.iss" "/DVersion=$version" "/DDistDir=$distArg" "/DModelsDir=$modelsArg" "/O$outDir"
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

    $pieces = @(Get-ChildItem -LiteralPath $outDir -Filter "jev-setup-$version*")
    if ($pieces.Count -eq 0) { throw "ISCC produced no jev-setup-$version file" }
    if ($Thumbprint) {
        foreach ($piece in ($pieces | Where-Object { $_.Extension -eq ".exe" })) {
            signtool sign /sha1 $Thumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $piece.FullName
            if ($LASTEXITCODE -ne 0) { throw "signtool failed on $($piece.Name)" }
        }
    }
    foreach ($piece in $pieces) {
        $sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $piece.FullName).Hash.ToLower()
        Write-Output "$($piece.FullName)"
        Write-Output "sha256: $sha"
    }
} finally { Pop-Location }
