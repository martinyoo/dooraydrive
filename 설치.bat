@echo off
REM ===========================================================================
REM  Dooray Drive sync - installer (single-file bootstrap)
REM
REM  Two modes, auto-detected:
REM   (A) Bootstrap : this .bat was downloaded on its own.
REM                   Fetch the repo into C:\dooraydrive, then run setup there.
REM   (B) In-repo   : this .bat sits next to INSTALL.ps1. Run setup here.
REM
REM  ENCODING - this file must stay pure ASCII. No Korean, not even in
REM  comments or in a path that names this file.
REM  cmd.exe reads .bat in the console code page (CP949 on Korean Windows), so
REM  UTF-8 Korean renders as mojibake. `chcp 65001` does NOT fix it: measured
REM  2026-08-04, cmd mis-splits multibyte lines in a file that has labels and
REM  goto, and then executes fragments of the text as commands.
REM  All Korean lives in INSTALL.ps1 (UTF-8 with BOM), which PowerShell reads
REM  correctly. The English lines below show for a few seconds at most, before
REM  INSTALL.ps1 takes over.
REM ===========================================================================
setlocal EnableExtensions

REM Install location. Overridable for PCs that cannot write to the C:\ root:
REM   set DSYNC_TARGET=D:\dooraydrive
if not defined DSYNC_TARGET set "DSYNC_TARGET=C:\dooraydrive"
set "TARGET=%DSYNC_TARGET%"
set "ZIPURL=https://github.com/martinyoo/dooraydrive/archive/refs/heads/main.zip"
set "ZIPFILE=%TEMP%\dooraydrive-main.zip"
set "EXDIR=%TEMP%\dooraydrive-extract"

if exist "%~dp0INSTALL.ps1" goto :in_repo

REM ===========================================================================
REM  (A) Bootstrap
REM ===========================================================================
echo.
echo ============================================================
echo   Dooray Drive sync - installer
echo   Fetching the program into %TARGET%
echo ============================================================
echo.

if exist "%TARGET%\INSTALL.ps1" goto :have_repo

REM curl and tar ship with Windows 10 1803+ / 1809+
where curl >nul 2>&1
if errorlevel 1 goto :no_tools
where tar >nul 2>&1
if errorlevel 1 goto :no_tools

echo [1/2] Downloading ...
curl -L -o "%ZIPFILE%" "%ZIPURL%" --ssl-no-revoke --progress-bar
if errorlevel 1 goto :dl_failed

REM A proxy login page comes back tiny - reject it by size
for %%F in ("%ZIPFILE%") do set "ZIPSIZE=%%~zF"
if not defined ZIPSIZE goto :dl_failed
if %ZIPSIZE% LSS 50000 goto :size_failed

echo [2/2] Extracting ...
if exist "%EXDIR%" rmdir /S /Q "%EXDIR%"
mkdir "%EXDIR%"
tar -xf "%ZIPFILE%" -C "%EXDIR%"
if errorlevel 1 goto :extract_failed
if not exist "%EXDIR%\dooraydrive-main\INSTALL.ps1" goto :extract_failed

move "%EXDIR%\dooraydrive-main" "%TARGET%" >nul
if errorlevel 1 goto :move_failed
rmdir /S /Q "%EXDIR%" >nul 2>&1
del "%ZIPFILE%" >nul 2>&1
echo       Done - %TARGET%
goto :fetched

:have_repo
echo   Already present at %TARGET% - reusing it.
echo   Delete that folder first if you want a clean copy.

:fetched
echo.
set "RUNDIR=%TARGET%"
goto :run

REM ===========================================================================
REM  (B) In-repo
REM ===========================================================================
:in_repo
set "RUNDIR=%~dp0"
if "%RUNDIR:~-1%"=="\" set "RUNDIR=%RUNDIR:~0,-1%"

REM ===========================================================================
REM  Run the real installer. INSTALL.ps1 holds all Korean text, all prompts
REM  and all error handling.
REM  -ExecutionPolicy Bypass is required: files extracted from a downloaded
REM  .zip carry the Mark-of-the-Web, and the default RemoteSigned policy
REM  refuses to run them.
REM ===========================================================================
:run
cd /d "%RUNDIR%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%RUNDIR%\INSTALL.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
endlocal & exit /b %RC%

REM ===========================================================================
REM  Errors (English - INSTALL.ps1 has not been reached yet)
REM ===========================================================================
:no_tools
echo.
echo   [STOP] curl or tar is missing (needs Windows 10 1809 or later).
echo          Download and unzip this into %TARGET% manually,
echo          then run the installer inside that folder:
echo          %ZIPURL%
goto :bail

:dl_failed
echo.
echo   [STOP] Download failed. Check your internet / corporate proxy,
echo          then run this file again.
goto :bail

:size_failed
echo.
echo   [STOP] Downloaded file is too small (%ZIPSIZE% bytes).
echo          A proxy login page was probably returned instead.
echo          Sign in to the corporate network in a browser, then retry.
del "%ZIPFILE%" >nul 2>&1
goto :bail

:extract_failed
echo.
echo   [STOP] Extraction failed. Check free space in %TEMP%
echo          and whether antivirus blocked it.
goto :bail

:move_failed
echo.
echo   [STOP] Could not create %TARGET%.
echo          A folder with that name may already exist, or permission denied.
goto :bail

:bail
echo.
pause
endlocal & exit /b 1
