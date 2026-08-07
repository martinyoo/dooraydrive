@echo off
REM ===========================================================================
REM  synchere.bat - Dooray Drive folder-level sync launcher.
REM
REM  Put a COPY of this file inside any folder and double-click it:
REM   - inside a sync target folder (or any of its subfolders): that target
REM     gets synchronized (both directions; plan is printed as it runs)
REM   - in a parent folder like WORK: every sync target underneath it is
REM     synchronized in turn (excluded targets are skipped with a reason)
REM  Every copy of this file is IDENTICAL - just copy it anywhere.
REM  This file itself is NEVER synchronized (excluded on both sides).
REM
REM  This file is also the REGISTRATION SWITCH (2026-08-07):
REM   - copy it into an unregistered folder and run it: the folder gets
REM     registered (derived from a registered sibling) and synced
REM   - delete it from a sync folder's root: the folder is unregistered on
REM     the next run (soft - config keeps the baseline; copy the file back
REM     and run it to re-register). Manual push/pull/off folders never flip.
REM
REM  Pass-through args work too:  synchere.bat --dry-run
REM
REM  Deletions are never propagated (report only). Conflicts keep both sides.
REM
REM  ENCODING - pure ASCII on purpose. cmd.exe reads .bat files in the console
REM  code page (CP949 on Korean Windows) and mis-splits UTF-8 Korean even with
REM  chcp 65001 (measured 2026-08-04). All Korean messages come from
REM  tools\sync_here.py, which prints UTF-8 correctly.
REM  %~dp0 may contain Korean folder names - that is fine: runtime variable
REM  expansion is Unicode; only Korean in the FILE TEXT breaks.
REM
REM  Program folder lookup order: DSYNC_HOME env var, then C:\dooraydrive
REM  (colleague PCs), then C:\drive\dev\dooraydrive (dev PC).
REM ===========================================================================
setlocal EnableExtensions
set "HERE=%~dp0"
REM %~dp0 ends with a backslash; inside "..." that \" escapes the closing
REM quote and swallows the next argument (measured: --dry-run got glued onto
REM the path). Strip it - but keep it for drive roots like "C:\".
if not "%HERE:~-2%"==":\" if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "REPO="
if defined DSYNC_HOME if exist "%DSYNC_HOME%\tools\sync_here.py" set "REPO=%DSYNC_HOME%"
if not defined REPO if exist "C:\dooraydrive\tools\sync_here.py" set "REPO=C:\dooraydrive"
if not defined REPO if exist "C:\drive\dev\dooraydrive\tools\sync_here.py" set "REPO=C:\drive\dev\dooraydrive"
if not defined REPO goto :no_repo

REM On failure (exit code 1: one or more profiles failed - usually transient
REM network / locked-file trouble) ANY KEY RETRIES in this same window instead
REM of closing it. Exit code 2 (guidance: unregistered/excluded folder) and 0
REM (success) close normally - retrying those changes nothing.
REM TRIES cap: if stdin is redirected (EOF), pause returns instantly and the
REM loop would spin forever - cap it.
set /a TRIES=0

:run
python "%REPO%\tools\sync_here.py" --root "%HERE%" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" goto :finish
if "%RC%"=="2" goto :finish
set /a TRIES+=1
if %TRIES% GEQ 30 goto :give_up
echo [FAILED] Sync finished with errors (exit code %RC%).
echo          Press ANY KEY to RETRY in this window. (attempt %TRIES%/30)
echo          To stop instead: close this window (X) or press Ctrl+C.
pause >nul
echo.
echo ============================== RETRY ==============================
echo.
goto :run

:give_up
echo [STOP] Still failing after %TRIES% attempts. Fix the cause first
echo        (check the messages above), then run this file again.

:finish
pause
endlocal & exit /b %RC%

:no_repo
echo [STOP] dooraydrive program folder not found.
echo        Checked: DSYNC_HOME, C:\dooraydrive, C:\drive\dev\dooraydrive
echo        Install the program first, or set DSYNC_HOME to its folder.
echo.
pause
endlocal & exit /b 1
