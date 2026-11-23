# 윈도우용 RecoveryTool.exe 를 만든다. (윈도우에서 실행해야 한다. 맥에서는 만들 수 없다)
#   powershell -ExecutionPolicy Bypass -File launch\windows\build_exe.ps1
# 결과: dist\RecoveryTool.exe  (더블클릭 -> Windows 관리자 확인 창 -> 프로그램 창)
# 시험용(관리자 확인 없이 실행되는 사본): $env:RT_BUILD_NOADMIN = "1" 로 실행하면 dist\RecoveryTool_test.exe
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

$venv = Join-Path $root ".buildenv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "빌드용 가상환경을 만듭니다 (.buildenv)"
    py -3 -m venv $venv
}
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet pyinstaller pywebview pillow

$name = "RecoveryTool"
$piArgs = @("--noconfirm", "--clean", "--onefile", "--windowed",
          "--add-data", "apfsrecover\ui;apfsrecover\ui",
          "--collect-submodules", "webview",
          "--hidden-import", "PIL.Image")
if ($env:RT_BUILD_NOADMIN -eq "1") {
    $name = "RecoveryTool_test"
} else {
    $piArgs += "--uac-admin"   # 실행하면 Windows 관리자 확인 창이 뜬다 (원본 디스크를 읽으려면 필요)
}
$piArgs += @("--name", $name, "launch\windows\RecoveryTool_entry.py")
& $py -m PyInstaller @piArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패" }
Write-Host "완료: dist\$name.exe"
