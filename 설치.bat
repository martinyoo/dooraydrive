@echo off
REM ===========================================================================
REM  Dooray Drive sync - one-click installer (launcher)
REM
REM  This file is intentionally ASCII-only. cmd.exe reads .bat files using the
REM  OEM code page (CP949 on Korean Windows), so UTF-8 Korean text placed here
REM  would render as mojibake. All user-facing Korean lives in INSTALL.ps1,
REM  which PowerShell reads as UTF-8 (BOM) correctly.
REM
REM  -ExecutionPolicy Bypass is required because files extracted from a
REM  downloaded .zip carry the Mark-of-the-Web, and the default RemoteSigned
REM  policy refuses to run them.
REM ===========================================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALL.ps1" %*

echo.
pause
endlocal
exit /b %ERRORLEVEL%
