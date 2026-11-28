<#
.SYNOPSIS
    Signs the current worker release (worker_template.py) with your release key
    and pushes the signature, so workers can verify the update.

.DESCRIPTION
    Run it from your clone of the repository (double-click sign_worker.bat), after
    a change to worker_template.py has been merged into main. It:
      1. pulls the latest main;
      2. makes sure worker_template.py has the exact bytes the manager serves
         (Unix line endings);
      3. signs it with ssh-keygen (asks for your key's passphrase);
      4. checks the signature against the key(s) built into the worker;
      5. commits worker_template.py.sig and pushes it.
    Then pull on the manager as usual.

.PARAMETER KeyFile
    Your private signing key (the file without .pub). By default the script
    looks for "FractumEncodes" in your .ssh folder, next to this script, in
    Documents and in Downloads, and asks if it finds none.

.PARAMETER NoPush
    Sign and commit, but do not push.

.EXAMPLE
    .\sign_worker.ps1
    .\sign_worker.ps1 -KeyFile D:\keys\FractumEncodes
#>
param(
    [string]$KeyFile,
    [switch]$NoPush
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Namespace = 'fractum-worker'
$Script = 'worker_template.py'
$Sig = "$Script.sig"

function Say($msg)  { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Good($msg) { Write-Host "[+] $msg" -ForegroundColor Green }
function Fail($msg) {
    Write-Host "[!] $msg" -ForegroundColor Red
    exit 1
}

# --- tools ------------------------------------------------------------------
foreach ($tool in 'git', 'ssh-keygen') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail "$tool was not found. Install Git for Windows (it includes both), or on Windows 10/11 add the 'OpenSSH Client' optional feature for ssh-keygen."
    }
}
if (-not (Test-Path -LiteralPath $Script)) {
    Fail "No $Script here. Put this script in your clone of the DistributedEncodes repository."
}

# --- key ----------------------------------------------------------------------
$homeDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
if (-not $KeyFile) {
    $candidates = @(
        (Join-Path $homeDir '.ssh/FractumEncodes'),
        (Join-Path $PSScriptRoot 'FractumEncodes'),
        (Join-Path $homeDir 'Documents/FractumEncodes'),
        (Join-Path $homeDir 'Downloads/FractumEncodes')
    )
    $KeyFile = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $KeyFile) {
    $KeyFile = (Read-Host "Path to your private signing key (the file without .pub)").Trim('"', ' ')
}
if (-not (Test-Path -LiteralPath $KeyFile)) { Fail "Key file not found: $KeyFile" }
if ($KeyFile.EndsWith('.pub')) { Fail "$KeyFile is the public key; give the private one (same name without .pub)." }
$KeyFile = (Resolve-Path -LiteralPath $KeyFile).Path
Say "Signing key: $KeyFile"

# Windows OpenSSH refuses a private key that other accounts can read
# ("UNPROTECTED PRIVATE KEY FILE"); limit it to you.
if ($IsWindows -or $env:OS -eq 'Windows_NT') {
    & icacls $KeyFile /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
}

# --- latest main ------------------------------------------------------------
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'main') { Fail "This clone is on branch '$branch'. Switch to main first (git checkout main)." }
Say "Getting the latest main..."
git pull --ff-only
if ($LASTEXITCODE -ne 0) { Fail "git pull failed (local changes?). Fix that and run this again." }

# --- exact bytes (Unix line endings) ------------------------------------------
function Has-CRLF { ([IO.File]::ReadAllText((Join-Path $PSScriptRoot $Script))).Contains("`r`n") }
if (Has-CRLF) {
    Say "$Script has Windows line endings in this clone; checking it out again with Unix ones..."
    Remove-Item -LiteralPath $Script
    git checkout -- $Script
    if (Has-CRLF) {
        Fail "$Script still has Windows line endings. Run: git config core.autocrlf false; then delete it and run 'git checkout -- $Script'."
    }
}
$text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot $Script))
$version = [regex]::Match($text, '(?m)^WORKER_VERSION = "([^"]+)"').Groups[1].Value
if (-not $version) { Fail "Could not read WORKER_VERSION from $Script." }
$trusted = [regex]::Matches($text, '(?m)^\s*"(AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/=]+)"') | ForEach-Object { $_.Groups[1].Value }
if (-not $trusted) { Fail "Could not find the trusted keys (UPDATE_SIGNING_KEYS) in $Script." }

# --- sign ---------------------------------------------------------------------
Say "Signing worker $version (ssh-keygen asks for your key's passphrase)..."
if (Test-Path -LiteralPath $Sig) { Remove-Item -LiteralPath $Sig }
& ssh-keygen -Y sign -f $KeyFile -n $Namespace $Script
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Sig)) { Fail "Signing failed (wrong passphrase?)." }

# --- verify against the keys the workers trust ----------------------------------
$allowed = [IO.Path]::GetTempFileName()
try {
    $trusted | ForEach-Object { "fractum ssh-ed25519 $_" } | Set-Content -LiteralPath $allowed -Encoding ascii
    # Feed the file's raw bytes on stdin (a PowerShell pipe would re-encode it).
    $out = [IO.Path]::GetTempFileName()
    $p = Start-Process -FilePath (Get-Command ssh-keygen).Source -NoNewWindow -Wait -PassThru `
        -ArgumentList @('-Y', 'verify', '-f', "`"$allowed`"", '-I', 'fractum', '-n', $Namespace, '-s', "`"$Sig`"") `
        -RedirectStandardInput (Join-Path $PSScriptRoot $Script) -RedirectStandardOutput $out
    $msg = (Get-Content -LiteralPath $out -Raw)
    Remove-Item -LiteralPath $out -ErrorAction SilentlyContinue
    # ssh-keygen prints 'Good "fractum-worker" signature for ...' on success
    # (Windows PowerShell 5.1 can lose the exit code of a redirected process).
    $verified = ($msg -match '(?m)^Good "') -and ($p.ExitCode -eq 0 -or $null -eq $p.ExitCode)
    if (-not $verified) {
        Remove-Item -LiteralPath $Sig
        Fail "The signature does not verify with the key(s) built into the worker. Is this the right key? ($msg)"
    }
    Good "Signature verified with the worker's built-in key."
} finally {
    Remove-Item -LiteralPath $allowed -ErrorAction SilentlyContinue
}

# --- commit and push --------------------------------------------------------------
git add -- $Sig
git diff --cached --quiet -- $Sig
if ($LASTEXITCODE -eq 0) {
    Good "The signature in the repository is already current; nothing to commit."
    exit 0
}
git commit -m "Sign worker $version" -- $Sig
if ($LASTEXITCODE -ne 0) { Fail "git commit failed." }
if ($NoPush) {
    Good "Committed. Push it yourself with: git push"
    exit 0
}
git push
if ($LASTEXITCODE -ne 0) { Fail "git push failed. If Git asks you to sign in, run 'gh auth setup-git' once, then 'git push'." }
Good "Worker $version is signed and pushed. Now pull on the manager."
