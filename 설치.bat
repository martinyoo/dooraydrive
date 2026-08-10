@echo off
REM ===========================================================================
REM  Dooray Drive sync - installer (single-file bootstrap)
REM
REM  Two modes, auto-detected:
REM   (A) Bootstrap : this .bat was downloaded on its own.
REM                   Fetch the repo into the install folder, run setup there.
REM   (B) In-repo   : this .bat sits next to INSTALL.ps1. Run setup here.
REM
REM  Install folder, highest priority first:
REM    1. first argument      installer.bat D:\dooraydrive
REM                           (dragging a folder onto this file works too)
REM    2. DSYNC_TARGET env    set DSYNC_TARGET=D:\dooraydrive
REM    3. what you type at the prompt
REM    4. the drive THIS FILE sits on - save it on D: and it installs on D:
REM       (a copy already installed at C:\dooraydrive wins, so re-runs do not
REM        fork into a second copy)
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

set "ZIPURL=https://github.com/martinyoo/dooraydrive/archive/refs/heads/main.zip"
set "ZIPFILE=%TEMP%\dooraydrive-main.zip"
set "EXDIR=%TEMP%\dooraydrive-extract"

REM Split argv: the first non-switch argument is the install folder, the rest
REM go to INSTALL.ps1. Parsed with goto, not with (), because %VAR% inside a
REM parenthesised block expands before the block runs.
set "TARGET="
set "PSARGS="
:parse
if "%~1"=="" goto :parsed
set "ARG=%~1"
if defined TARGET goto :parse_ps
if "%ARG:~0,1%"=="-" goto :parse_ps
if "%ARG:~0,1%"=="/" goto :parse_ps
set "TARGET=%ARG%"
shift
goto :parse
:parse_ps
set "PSARGS=%PSARGS% %1"
shift
goto :parse
:parsed

if not defined TARGET if defined DSYNC_TARGET set "TARGET=%DSYNC_TARGET%"

REM Mode B is decided before any prompt - running in place needs no folder.
if exist "%~dp0INSTALL.ps1" goto :in_repo

REM ===========================================================================
REM  (A) Bootstrap
REM ===========================================================================
echo.
echo ============================================================
echo   Dooray Drive sync - installer
echo ============================================================
echo.

REM Default: the drive this file sits on. %~d0 is empty on a UNC path, so
REM require "X:" shape before trusting it.
set "DEFTARGET=C:\dooraydrive"
set "SELFDRIVE=%~d0"
if "%SELFDRIVE:~1,1%"==":" set "DEFTARGET=%SELFDRIVE%\dooraydrive"

if defined TARGET goto :have_target
echo   Where should the program go? Press ENTER for the default.
echo   Default: %DEFTARGET%
REM Point at an install elsewhere instead of silently defaulting back to it.
REM Quietly overriding the folder the user chose is the whole complaint here.
if /I not "%DEFTARGET%"=="C:\dooraydrive" if exist "C:\dooraydrive\INSTALL.ps1" echo   Note: another copy is already installed at C:\dooraydrive
echo.
set /p "TARGET=  Folder: "
REM set /p leaves TARGET undefined on EOF (piped input) - fall back.
if not defined TARGET set "TARGET=%DEFTARGET%"

:have_target
REM Paths pasted from Explorer arrive wrapped in quotes.
set TARGET=%TARGET:"=%
REM Trailing separator, but keep it for a drive root so "D:\" stays valid.
if not "%TARGET:~-2%"==":\" if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"
REM A bare drive means "the usual folder on that drive", not the drive root -
REM extracting the repo onto D:\ itself would scatter it across the disk.
if "%TARGET:~-1%"==":" set "TARGET=%TARGET%\dooraydrive"
if "%TARGET:~-2%"==":\" set "TARGET=%TARGET%dooraydrive"
if not defined TARGET goto :bad_target
if "%TARGET:~1,1%"==":" goto :target_ok
if "%TARGET:~0,2%"=="\\" goto :target_ok
goto :bad_target

:target_ok
echo.
echo   Fetching the program into %TARGET%
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

REM `move SRC DST` puts SRC *inside* DST when DST already exists, which would
REM bury the program one level deep (%TARGET%\dooraydrive-main\INSTALL.ps1) and
REM the run step below would then not find INSTALL.ps1. Pre-making the folder
REM in Explorer is a normal thing to do now that the folder is chooseable, so
REM handle it: plain rmdir removes it only if empty, which is exactly the test
REM we want - a non-empty folder is someone else's data and must not be touched.
if exist "%TARGET%" rmdir "%TARGET%" 2>nul
if exist "%TARGET%" goto :target_busy

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
powershell -NoProfile -ExecutionPolicy Bypass -File "%RUNDIR%\INSTALL.ps1"%PSARGS%
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
echo          Run this file again and give another folder, for example:
echo            D:\dooraydrive
goto :bail

:bad_target
echo.
echo   [STOP] Not a usable folder: %TARGET%
echo          Give a full path on a drive, for example:
echo            D:\dooraydrive
goto :bail

:target_busy
echo.
echo   [STOP] %TARGET% already exists and is not a dooraydrive install.
echo          Nothing was changed. Empty that folder, or run this file
echo          again and give another one, for example:
echo            D:\dooraydrive2
goto :bail

:bail
echo.
pause
endlocal & exit /b 1
