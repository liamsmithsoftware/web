@echo off
REM ============================================================
REM  Website Content Collector - stop running instances
REM  Use this when you want to stop background server instances
REM ============================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

echo.
echo  Looking for running instances of Website Content Collector ...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$here = (Get-Location).Path; $found = $false; Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -match 'run\.py' -or $_.CommandLine -match 'launcher\.py') -and $_.CommandLine.IndexOf($here, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 } | ForEach-Object { if ($_.ProcessId -ne $PID) { Write-Host ('  Stopping PID ' + $_.ProcessId); try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $found = $true } catch {} } }; if ($found) { Write-Host ''; Write-Host '  Done. You can start the program again with start_windows.bat' } else { Write-Host '  No running instance of this program was found.'; Write-Host '  Another program may be using port 8000.' }"

echo.
echo  You can also start on another port:
echo      start_windows.bat --port 8010
echo.
pause
endlocal
