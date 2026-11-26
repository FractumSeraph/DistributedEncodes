<#
.SYNOPSIS
    Fractum Worker Launcher (Production)
    - Silent startup.
    - No debug logs.
    - Launches worker in its own window.
#>

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName Microsoft.VisualBasic
Add-Type -AssemblyName System.Windows.Forms

# --- Configuration ---
$ManagerUrl = "https://encode.fractumseraph.net/"
$WorkerUrl  = "$($ManagerUrl)dl/worker"
$ConfigFile = "worker_config.json"
$Config     = $null

function Show-Error($msg) {
    [System.Windows.Forms.MessageBox]::Show($msg, "Fractum Error", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
}

# --- 1. Load or Create Config ---
if (Test-Path $ConfigFile) {
    try {
        $rawJson = Get-Content $ConfigFile -Raw
        if ([string]::IsNullOrWhiteSpace($rawJson)) { throw "Empty File" }
        $Config = $rawJson | ConvertFrom-Json
    } catch {
        Remove-Item $ConfigFile -ErrorAction SilentlyContinue
    }
}

if (-not $Config) {
    $defUser = "Anonymous"
    $uPrompt = "Please enter the USERNAME of the person running the program.`n(e.g. 'FractumSeraph')`n`n[Click Cancel to Quit]"
    $uInput  = [Microsoft.VisualBasic.Interaction]::InputBox($uPrompt, "Fractum Setup (1/2)", $defUser)
    if ($uInput -eq "") { exit } 

    $defWorker = "Node-" + (Get-Random -Minimum 1000 -Maximum 9999)
    $wPrompt   = "Please enter a name for THIS COMPUTER.`n(e.g. 'LivingRoom-PC')`n`n[Click Cancel to Quit]"
    $wInput    = [Microsoft.VisualBasic.Interaction]::InputBox($wPrompt, "Fractum Setup (2/2)", $defWorker)
    if ($wInput -eq "") { exit }

    $Config = @{ username = $uInput; workername = $wInput }
    
    try {
        $jsonContent = $Config | ConvertTo-Json -Depth 2
        $fullPath = Join-Path (Get-Location) $ConfigFile
        [System.IO.File]::WriteAllText($fullPath, $jsonContent)
    } catch {
        Show-Error "Failed to save configuration file."
        exit
    }
}

# --- 2. Check Python ---
$pythonExists = $false
try {
    $ver = python --version 2>&1
    if ($ver -match "Python 3") { $pythonExists = $true }
} catch {}

if (-not $pythonExists) {
    [System.Windows.Forms.MessageBox]::Show("Python is missing.`n`nClick OK to install it automatically.`n(This may take 1-2 minutes.)", "Fractum Setup", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
    try {
        $installer = "$env:TEMP\python_installer.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $installer -UseBasicParsing
        Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    } catch {
        Show-Error "Failed to install Python."
        exit
    }
}

# --- 3. Dependencies ---
try { pip install requests --disable-pip-version-check | Out-Null } catch { python -m pip install requests | Out-Null }

# --- 4. Download Worker ---
# A worker already here decides whether the download may replace it: workers
# 3.17.0+ only accept a script signed by the project's key. (An older worker
# does not know --install-update; argparse then exits with 2 and the download
# is used as before.)
try {
    if (Test-Path "worker.py") {
        Invoke-WebRequest $WorkerUrl -OutFile "worker.py.new" -UseBasicParsing
        python worker.py --install-update worker.py.new --manager $ManagerUrl
        if ($LASTEXITCODE -eq 2 -and (Test-Path "worker.py.new")) {
            Move-Item -Force "worker.py.new" "worker.py"
        }
        if (Test-Path "worker.py.new") { Remove-Item -Force "worker.py.new" }
    } else {
        Invoke-WebRequest $WorkerUrl -OutFile "worker.py" -UseBasicParsing
    }
} catch {
    if (-not (Test-Path "worker.py")) {
        Show-Error "Could not download worker script.`nCheck internet connection."
        exit
    }
}

# --- 5. Launch Worker ---
# This opens the worker in a standard console window and closes the launcher.
$CurrentDir = Get-Location
Start-Process python -ArgumentList "worker.py --manager $ManagerUrl --jobs 1" -WorkingDirectory $CurrentDir
