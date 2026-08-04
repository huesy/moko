# Moko installer for Windows — bring Moko home. 🌸
#
#   irm https://mokomoko.ai/install.ps1 | iex
#
# What it does, and nothing more:
#   1. Installs `uv` (Astral's Python tool manager) if you don't have it.
#   2. Installs the `moko` command — uv fetches an isolated, managed
#      Python 3.12 for it, so you need nothing pre-installed.
#   3. Tells you to run `moko setup`.
#
# It does not require admin rights or touch system files. Everything lands in
# your user profile. Moko's mind never leaves your machine.

$ErrorActionPreference = 'Stop'

# The repo is public, so anonymous HTTPS works for everyone; we probe SSH first
# only so contributors with keys use them, and fall back to HTTPS otherwise. Set
# MOKO_SLUG for a fork, MOKO_REPO_SSH / MOKO_REPO_HTTPS to override a URL, or
# MOKO_REPO to force one exact URL.
$Slug      = if ($env:MOKO_SLUG)       { $env:MOKO_SLUG }       else { 'huesy/moko' }
$RepoSsh   = if ($env:MOKO_REPO_SSH)   { $env:MOKO_REPO_SSH }   else { "ssh://git@github.com/$Slug.git" }
$RepoHttps = if ($env:MOKO_REPO_HTTPS) { $env:MOKO_REPO_HTTPS } else { "https://github.com/$Slug.git" }
$RepoForce = $env:MOKO_REPO   # force one exact URL, if set
$Python    = if ($env:MOKO_PYTHON)     { $env:MOKO_PYTHON }     else { '3.12' }
# Default: latest published release. Pin with MOKO_VERSION=v1.2.3 (or MOKO_REF).
$Ref       = if ($env:MOKO_REF)        { $env:MOKO_REF }        elseif ($env:MOKO_VERSION) { $env:MOKO_VERSION } else { '' }

function Step($m) { Write-Host "🌸 $m" -ForegroundColor Magenta }
function Note($m) { Write-Host "   $m" -ForegroundColor DarkGray }

Write-Host ''
Write-Host '  Moko' -ForegroundColor Magenta -NoNewline
Write-Host ' — your warm, curious, fluffy companion'
Write-Host ''

# ---- 1. ensure uv -----------------------------------------------------------
if (Get-Command uv -ErrorAction SilentlyContinue) {
  Step 'uv is already installed.'
} else {
  Step "Installing uv (Moko's Python toolchain)…"
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# ---- 2. pick the version to install ----------------------------------------
if (-not $Ref) {
  try { $Ref = (Invoke-RestMethod "https://api.github.com/repos/$Slug/releases/latest").tag_name } catch { $Ref = '' }
}
if (-not $Ref) {
  $Ref = 'main'
  Note 'No published release yet — installing the latest development build (main).'
} else {
  Note "Installing release $Ref."
}

# ---- 3. install the moko command -------------------------------------------
Step "Installing Moko…"

# Transport env for any git fetch (source build): SSH-first, fail-fast, no prompt.
$env:GIT_SSH_COMMAND = 'ssh -o BatchMode=yes -o ConnectTimeout=5'
$env:GIT_TERMINAL_PROMPT = '0'

function Install-FromSource {
  # The dashboard UI is a build artifact, not in the source tree — a source build
  # ships without it (build it with scripts/build-monitor.sh from a clone).
  if ($RepoForce) {
    $url = $RepoForce
  } elseif ((Get-Command git -ErrorAction SilentlyContinue)) {
    git ls-remote $RepoSsh HEAD *> $null
    if ($LASTEXITCODE -eq 0) { $url = $RepoSsh; Note 'building from source over SSH' }
    else { $url = $RepoHttps; Note 'building from source over HTTPS' }
  } else {
    $url = $RepoHttps; Note 'building from source over HTTPS'
  }
  uv tool install --force --python $Python --from "git+$url@$Ref#subdirectory=apps/agent" moko
  return ($LASTEXITCODE -eq 0)
}

$installed = $false
# A real release? Install its prebuilt WHEEL (carries the built dashboard, no Node).
# Try gh first (handles auth / API rate limits); else a plain HTTPS download.
if ($Ref -ne 'main') {
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("moko-" + [System.Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp -Force | Out-Null
  if (Get-Command gh -ErrorAction SilentlyContinue) {
    gh release download $Ref --repo $Slug --pattern '*.whl' --dir $tmp *> $null
    if ($LASTEXITCODE -eq 0) {
      $whl = Get-ChildItem -Path $tmp -Filter *.whl | Select-Object -First 1
      if ($whl) {
        Note "installing the $Ref release wheel (dashboard included)"
        uv tool install --force --python $Python "$($whl.FullName)"
        if ($LASTEXITCODE -eq 0) { $installed = $true }
      }
    }
  }
  if (-not $installed) {
    try {
      $rel = Invoke-RestMethod "https://api.github.com/repos/$Slug/releases/tags/$Ref" -ErrorAction Stop
      $asset = $rel.assets | Where-Object { $_.name -like '*.whl' } | Select-Object -First 1
      if ($asset) {
        $dest = Join-Path $tmp 'moko.whl'
        Invoke-WebRequest $asset.browser_download_url -OutFile $dest -ErrorAction Stop
        Note "installing the $Ref release wheel (dashboard included)"
        uv tool install --force --python $Python "$dest"
        if ($LASTEXITCODE -eq 0) { $installed = $true }
      }
    } catch { }
  }
  if (-not $installed) { Note "couldn't fetch a release wheel for $Ref — building from source" }
}

if (-not $installed -and -not (Install-FromSource)) {
  Write-Host ''
  Write-Host "error: Moko install failed — couldn't reach github.com/$Slug. Check your network and try again, or set MOKO_REPO to an exact clone URL." -ForegroundColor Red
  exit 1
}
uv tool update-shell *> $null

# ---- 4. done ----------------------------------------------------------------
Write-Host ''
Write-Host '  Moko is installed.' -ForegroundColor Magenta
Write-Host ''
Note 'Next, get set up (timezone, dream hours, your LLM provider):'
Write-Host ''
Write-Host '      moko setup' -ForegroundColor Magenta
Write-Host ''
Note 'Everything runs on your machine. Open a new terminal if `moko` is not found.'
