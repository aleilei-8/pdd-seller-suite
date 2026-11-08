@echo off
REM ============================================================
REM  PDD Seller Suite - launcher (ASCII)
REM  Best experience: double-click start_workbench.vbs (no black
REM  window, Playwright works).
REM  This .bat is a fallback: launches the vbs via wscript hidden.
REM  Important: must use python.exe (console), not pythonw, or
REM  Playwright crashes with "Assertion failed: process_title".
REM ============================================================
setlocal
set "PROJ=%~dp0"
if exist "%PROJ%start_workbench.vbs" (
    start "" wscript.exe "%PROJ%start_workbench.vbs"
) else (
    echo [ERROR] start_workbench.vbs not found next to this bat.
    pause
)
endlocal
