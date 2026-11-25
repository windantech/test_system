# One-time setup for Code Replay on a new Windows machine.
# Run it by double-clicking SETUP.bat. Windows will ask for administrator permission
# a few times (Docker, WSL); click Yes each time.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Install-App($id, $name, $check) {
    if (& $check) { Write-Host "  [ok]      $name already installed"; return }
    Write-Host "  [install] $name ..."
    winget install --id $id -e --accept-source-agreements --accept-package-agreements --silent | Out-Null
    Update-SessionPath
    if (& $check) { Write-Host "  [ok]      $name installed" }
    else { Write-Host "  [!!]      $name may need a restart or a manual install" -ForegroundColor Yellow }
}

Write-Host "`n=== 1. Installing programs ===" -ForegroundColor Cyan
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Host "winget is missing. Install 'App Installer' from the Microsoft Store, then run SETUP again." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}
Update-SessionPath
Install-App "Python.Python.3.12"        "Python 3.12"     { Test-Path "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" }
Install-App "Git.Git"                   "Git (Git Bash)"  { Test-Path "C:\Program Files\Git\bin\bash.exe" }
Install-App "Microsoft.VisualStudioCode" "VS Code"        { [bool](Get-Command code -ErrorAction SilentlyContinue) }
Install-App "PHP.PHP.NTS.8.2"           "PHP 8.2"         { [bool](Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\PHP.PHP.NTS.8.2*\php.exe" -ErrorAction SilentlyContinue) }
Install-App "Docker.DockerDesktop"      "Docker Desktop"  { Test-Path "C:\Program Files\Docker\Docker\Docker Desktop.exe" }
Install-App "Google.Chrome"             "Google Chrome"   { (Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe") -or (Test-Path "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") }

Write-Host "`n=== 2. Configuring PHP extensions ===" -ForegroundColor Cyan
$php = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\PHP.PHP.NTS.8.2*\php.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($php) {
    $dir = $php.DirectoryName
    if (-not (Test-Path "$dir\php.ini")) {
        $ini = Get-Content "$dir\php.ini-development" -Raw
        $ini = $ini -replace '(?m)^;extension_dir = "ext"', 'extension_dir = "ext"'
        foreach ($e in 'curl','fileinfo','mbstring','openssl','pdo_mysql','sodium','intl','zip') {
            $ini = $ini -replace "(?m)^;extension=$e\s*$", "extension=$e"
        }
        $ini = $ini -replace '(?m)^;date.timezone =.*$', 'date.timezone = Africa/Blantyre'
        Set-Content -Path "$dir\php.ini" -Value $ini -Encoding ascii
    }
    & $php.FullName -m | Select-String -Pattern '^(curl|fileinfo|mbstring|openssl|pdo_mysql)$' | ForEach-Object { Write-Host "  [ok]      php extension $_" }
} else { Write-Host "  [!!]      PHP not found" -ForegroundColor Yellow }

Write-Host "`n=== 3. WSL (needed by Docker) ===" -ForegroundColor Cyan
wsl --status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [install] WSL - click Yes on the permission prompt"
    Start-Process wsl.exe -ArgumentList '--install','--no-distribution' -Verb RunAs -Wait
    $needRestart = $true
} else { Write-Host "  [ok]      WSL present" }

Write-Host "`n=== 4. Python environment for the tool ===" -ForegroundColor Cyan
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if ($py -and -not (Test-Path ".venv\Scripts\python.exe")) { & $py -m venv .venv }
if (Test-Path ".venv\Scripts\python.exe") {
    & .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
    & .\.venv\Scripts\python.exe -c "import pynput; print('  [ok]      pynput')"
} else { Write-Host "  [!!]      Could not create the Python environment" -ForegroundColor Yellow }

Write-Host "`n=== 5. Checks ===" -ForegroundColor Cyan
$virt = (Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled
docker info *> $null
if ($LASTEXITCODE -eq 0) { Write-Host "  [ok]      Docker engine running" }
elseif (-not $virt) {
    Write-Host "  [!!]      CPU virtualization looks OFF. Turn on 'Intel Virtualization Technology' / 'SVM'" -ForegroundColor Yellow
    Write-Host "            in the BIOS, then start Docker Desktop. (Only needed for the full test suite.)" -ForegroundColor Yellow
} else { Write-Host "  [..]      Start Docker Desktop from the Start menu and accept its terms." }

Write-Host "`nNext:" -ForegroundColor Cyan
if ($needRestart) { Write-Host "  - RESTART the computer (WSL was just installed)." }
Write-Host "  - Edit SETTINGS.bat: set SOURCE to your project's folder and OUT to an empty folder."
Write-Host "  - Double-click 1_TEST_RUN.bat for a short test, then 2_START_RECORDING.bat."
Read-Host "`nPress Enter to close"
