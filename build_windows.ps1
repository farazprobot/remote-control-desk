$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -eq $pythonLauncher) {
  $pythonLauncher = Get-Command python -ErrorAction SilentlyContinue
}
if ($null -eq $pythonLauncher) {
  throw "Python was not found. Install Python 3.11 or 3.12 and enable Add Python to PATH."
}

$venv = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (!(Test-Path -LiteralPath $venvPython)) {
  Write-Host "Creating a private Windows build environment..."
  if ($pythonLauncher.Name -eq "py.exe") {
    & $pythonLauncher.Source -3 -m venv $venv
  } else {
    & $pythonLauncher.Source -m venv $venv
  }
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install --requirement (Join-Path $PSScriptRoot "desktop\requirements.txt")

Remove-Item -Recurse -Force (Join-Path $PSScriptRoot "build"), (Join-Path $PSScriptRoot "dist") -ErrorAction SilentlyContinue

Write-Host "Building the standalone Windows executable..."
& $venvPython -m PyInstaller --noconfirm --clean --onefile --windowed `
  --collect-submodules websockets `
  --hidden-import pyautogui._pyautogui_win `
  --hidden-import pyperclip `
  --name RemoteControlDesk `
  --distpath (Join-Path $PSScriptRoot "dist") `
  --workpath (Join-Path $PSScriptRoot "build") `
  (Join-Path $PSScriptRoot "desktop\app.py")

$exe = Join-Path $PSScriptRoot "dist\RemoteControlDesk.exe"
if (!(Test-Path -LiteralPath $exe)) {
  throw "PyInstaller did not create $exe."
}

Write-Host ""
Write-Host "Build complete: $exe"
Write-Host "The app asks for the relay URL and one owner-issued login key."
Write-Host "Run installer\RemoteControl.iss with Inno Setup to create an installer."