@echo off
REM ===========================================================================
REM  Dooray Drive sync - installer (single-file bootstrap)
REM
REM  Two modes, auto-detected:
REM   (A) Bootstrap : this .bat was downloaded on its own.
REM                   Fetch the repo into the install folder, run setup there.
REM   (B) In-repo   : this .bat sits next to INSTALL.ps1. Run setup here.
REM                   In an INSTALLED copy (no .git) setup asks first whether
REM                   to update; choosing it stages this file into %TEMP% and
REM                   hands over, and that copy runs mode (A) against this
REM                   folder. A folder cannot replace itself from the inside -
REM                   the staged copy is what makes the choice real instead of
REM                   an instruction printed on a screen.
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
REM  Version, for ROLLBACK. Default is the latest on main.
REM    installer.bat v0.2.0              a token shaped like vN... is a version
REM    installer.bat D:\dooraydrive v0.2.0
REM    set DSYNC_VERSION=v0.2.0          env var, same effect
REM  It must be a tag that exists in the repo; the zip is then fetched from
REM  refs/tags/<version> instead of refs/heads/main. Rollback exists only
REM  because the tag does - without tags there is no way to name a past build.
REM  When a version was ASKED FOR and the download fails, this installer does
REM  NOT quietly fall back to the copy already on disk: you asked to change
REM  versions, so ending up on the old one while reporting success would be
REM  the failure mode the whole feature is meant to remove.
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

REM ZIPURL is assembled after argument parsing - the version can come from the
REM command line, so it is not known yet at this point.
set "ZIPBASE=https://github.com/martinyoo/dooraydrive/archive"
set "ZIPFILE=%TEMP%\dooraydrive-src.zip"
set "EXDIR=%TEMP%\dooraydrive-extract"
REM Set only by the installed-copy branch of mode (B). OFFERUPD is what makes
REM exit code 10 mean anything; PSEXTRA is what asks INSTALL.ps1 for the prompt.
set "OFFERUPD="
set "PSEXTRA="

REM Split argv: the first non-switch argument is the install folder, the rest
REM go to INSTALL.ps1. Parsed with goto, not with (), because %VAR% inside a
REM parenthesised block expands before the block runs.
set "TARGET="
set "PSARGS="
set "VERSION="
:parse
if "%~1"=="" goto :parsed
set "ARG=%~1"
REM A token shaped like vN (v then a digit) is a version, wherever it sits -
REM checked before TARGET so `installer.bat D:\dooraydrive v0.2.0` works too.
REM The digit test enumerates 0-9 instead of using LSS/GTR: those operators
REM switch between numeric and string comparison depending on the operands,
REM and this file must never guess.
set "VDIGIT="
if /I not "%ARG:~0,1%"=="v" goto :not_version
for %%N in (0 1 2 3 4 5 6 7 8 9) do if "%ARG:~1,1%"=="%%N" set "VDIGIT=1"
if not defined VDIGIT goto :not_version
set "VERSION=%ARG%"
shift
goto :parse
:not_version
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
if not defined VERSION if defined DSYNC_VERSION set "VERSION=%DSYNC_VERSION%"

REM Two plain lines, not an if/else block: %VAR% inside () expands before the
REM block runs (same reason the parser above uses goto).
set "ZIPURL=%ZIPBASE%/refs/heads/main.zip"
set "VERLABEL=main"
if defined VERSION set "ZIPURL=%ZIPBASE%/refs/tags/%VERSION%.zip"
if defined VERSION set "VERLABEL=%VERSION%"

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
REM Spaces around what was typed. `set /p` keeps them verbatim, and a stray one
REM used to break things quietly: "c: " skipped the bare-drive rule below (the
REM last character is a space, not ":") yet still passed validation, so the
REM install went to a folder literally named "c: ". Trim before anything reads
REM the ends of the string. A real folder name cannot end in a space anyway.
REM The `if not defined` guards are load-bearing, not decoration. Trimming an
REM all-spaces entry empties TARGET, and %TARGET:~0,1% on an UNDEFINED variable
REM does not yield "" - cmd re-pairs the surrounding %% signs and executes the
REM wreckage. Measured: typing three spaces produced the literal command
REM   if "~0,1TARGET:~1trim_lead
REM and the installer died with "The syntax of the command is incorrect".
REM So: never let a substring expansion see an undefined TARGET.
:trim_lead
if not defined TARGET goto :trim_done
if "%TARGET:~0,1%"==" " set "TARGET=%TARGET:~1%"& goto :trim_lead
:trim_trail
if not defined TARGET goto :trim_done
if "%TARGET:~-1%"==" " set "TARGET=%TARGET:~0,-1%"& goto :trim_trail
:trim_done
REM Nothing left means the same thing pressing ENTER means. Rejecting it would
REM print "[STOP] Not a usable folder:" with a blank path, which explains nothing.
if not defined TARGET set "TARGET=%DEFTARGET%"
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
if defined VERSION echo   Version requested: %VERSION%
echo.

REM  Refresh an existing install instead of silently reusing it. Reusing was the
REM  old behaviour and it stranded PCs: a first run that failed left a stale copy
REM  behind, and every later double-click ran that same stale copy - no download
REM  lines, no explanation, no way for the user to tell. (measured 2026-08-07)
REM  Program files live here; settings and state live in %APPDATA% and
REM  %LOCALAPPDATA%, so replacing this folder loses nothing.
set "REFRESH="
if exist "%TARGET%\INSTALL.ps1" set "REFRESH=1"
if defined REFRESH echo   Existing copy found at %TARGET%
if defined REFRESH echo   Refreshing it to the latest version ...
if defined REFRESH echo.

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
REM  Find the extracted folder instead of assuming its name. GitHub names the
REM  top folder after the ref: refs/heads/main gives dooraydrive-main, but the
REM  tag v0.2.0 gives dooraydrive-0.2.0 - the leading v is dropped. Looking for
REM  the folder that actually holds INSTALL.ps1 handles both, and survives any
REM  later change to that naming.
set "SRCDIR="
for /d %%D in ("%EXDIR%\*") do if exist "%%D\INSTALL.ps1" set "SRCDIR=%%D"
if not defined SRCDIR goto :extract_failed

REM `move SRC DST` puts SRC *inside* DST when DST already exists, which would
REM bury the program one level deep (%TARGET%\dooraydrive-main\INSTALL.ps1) and
REM the run step below would then not find INSTALL.ps1. Pre-making the folder
REM in Explorer is a normal thing to do now that the folder is chooseable, so
REM handle it: plain rmdir removes it only if empty, which is exactly the test
REM we want - a non-empty folder is someone else's data and must not be touched.
REM  Swap, do not delete-then-move. Deleting the old copy first would leave the
REM  PC with no installer at all if the move then failed (permission, file in
REM  use). Rename it aside, move the new one in, and only then drop the old one.
REM  This runs after the download and extract have already succeeded, so a
REM  network failure never touches the copy that is already working.
REM  The name is normally <folder>.old, but that name can be occupied by a
REM  copy an earlier update could not delete (a file in it still open). A
REM  leftover must not block every future update, so :swap_aside falls back to
REM  a unique name, and retries: a handle on the folder is usually momentary
REM  (Explorer, a virus scanner, a console that just exited).
set "OLDDIR=%TARGET%.old"
if defined REFRESH call :swap_aside
if defined REFRESH if exist "%TARGET%" goto :refresh_locked

if exist "%TARGET%" rmdir "%TARGET%" 2>nul
if exist "%TARGET%" goto :target_busy

move "%SRCDIR%" "%TARGET%" >nul
if errorlevel 1 goto :move_failed
if defined REFRESH rmdir /S /Q "%OLDDIR%" >nul 2>&1
REM  Say it rather than leave a mystery folder next to the program.
if defined REFRESH if exist "%OLDDIR%" echo   Note: the previous copy is still at %OLDDIR% (safe to delete).
rmdir /S /Q "%EXDIR%" >nul 2>&1
del "%ZIPFILE%" >nul 2>&1
REM  Install stamp. A PC whose program will not start at all (a bad import, a
REM  half-extracted folder) cannot print its own version - and that is exactly
REM  the PC whose version you need. Leave it where anyone can read it without
REM  running anything. Redirection goes BEFORE echo so no trailing space is
REM  written into the value.
>"%TARGET%\INSTALLED.txt" echo dooraydrive install stamp
>>"%TARGET%\INSTALLED.txt" echo version : %VERLABEL%
>>"%TARGET%\INSTALLED.txt" echo url     : %ZIPURL%
>>"%TARGET%\INSTALLED.txt" echo when    : %DATE% %TIME%
>>"%TARGET%\INSTALLED.txt" echo into    : %TARGET%
echo       Done - %TARGET%  (%VERLABEL%)
goto :fetched

REM  Could not fetch, but a working copy is already here - use it rather than
REM  dying, and say plainly that it may be out of date. Only reachable when
REM  REFRESH is set, so this never runs on a first install.
:stale_fallback
REM  A specific version was ASKED FOR. Falling back to the copy already on disk
REM  would leave the PC on the wrong version while the installer reports
REM  success - which is the exact failure this feature exists to remove.
REM  Recovery must not report success (vault: vault-sync-script-silent-commit-loss).
if defined VERSION goto :version_failed
echo.
echo   WARNING: could not fetch the latest version.
echo   Falling back to the existing copy at %TARGET% - it may be OUT OF DATE.
echo   If the install fails again, delete that folder and retry on a
echo   working internet connection.
del "%ZIPFILE%" >nul 2>&1
rmdir /S /Q "%EXDIR%" >nul 2>&1

:fetched
echo.
set "RUNDIR=%TARGET%"
goto :run

REM ===========================================================================
REM  (B) In-repo
REM ===========================================================================
REM  This branch used to run in complete silence, so someone who double-clicked
REM  a copy sitting inside the program folder saw the installer start with no
REM  download lines and had no way to tell that nothing had been updated.
REM  Say which copy is being run. (measured 2026-08-07)
:in_repo
set "RUNDIR=%~dp0"
if "%RUNDIR:~-1%"=="\" set "RUNDIR=%RUNDIR:~0,-1%"
echo.
echo ============================================================
echo   Dooray Drive sync - installer
echo   Running the copy next to this file:
echo   %RUNDIR%
echo ============================================================
echo.
REM  Say WHICH copy this is and what it can and cannot do.
REM  The old text was one parenthesised line ("nothing is downloaded in this
REM  mode") and people still read a run here as an update - measured twice:
REM  2026-08-07 (a stale copy kept being double-clicked) and 2026-09-15
REM  ("so I just run C:\dooraydrive\<this file>?"). synchere.bat already
REM  refuses in the very same situation and names the fix; this is the same
REM  treatment for the installer.
REM
REM  The test is .git, not INSTALLED.txt: a source checkout always has .git,
REM  while installs made before the stamp existed have no INSTALLED.txt and
REM  must still be recognised as installs.
if exist "%RUNDIR%\.git" goto :in_repo_source

REM  An installed copy. This branch used to end at "to update, copy this file
REM  somewhere else and run it there" - a command printed on a screen, which
REM  by this repo's rule is no feature at all (AGENTS.md: a feature you have to
REM  instruct the user to run is a feature you do not have). Setup now asks, in
REM  Korean, and UPDATE comes back as exit code 10 -> :self_update below.
set "OFFERUPD=1"
set "PSEXTRA= -OfferUpdate"
if defined VERSION set "PSEXTRA=%PSEXTRA% -UpdateVersion "%VERSION%""
echo   This folder is an installed copy.
echo   Setup will ask whether to UPDATE it or only re-check this PC.
if defined VERSION echo   Version requested: %VERSION%
echo.
goto :in_repo_go

:in_repo_source
echo   [!] This is a SOURCE CHECKOUT (.git found here).
echo       Setting it up in place registers DSYNC_HOME to THIS folder,
echo       which makes the checkout the program that runs every day -
echo       half-finished code would then sync real files.
echo       If you keep a separate installed copy, stop and run the
echo       installed one instead. Nothing is downloaded in this mode.
echo.

:in_repo_go

REM ===========================================================================
REM  Run the real installer. INSTALL.ps1 holds all Korean text, all prompts
REM  and all error handling.
REM  -ExecutionPolicy Bypass is required: files extracted from a downloaded
REM  .zip carry the Mark-of-the-Web, and the default RemoteSigned policy
REM  refuses to run them.
REM ===========================================================================
:run
cd /d "%RUNDIR%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%RUNDIR%\INSTALL.ps1"%PSEXTRA%%PSARGS%
set "RC=%ERRORLEVEL%"
REM  10 means "the user picked UPDATE at the prompt -OfferUpdate produced".
REM  That is the whole contract with INSTALL.ps1, and it is read here only.
REM  The OFFERUPD guard is load-bearing: without it any other path that ever
REM  exits 10 would start replacing folders.
if defined OFFERUPD if "%RC%"=="10" goto :self_update
echo.
pause
endlocal & exit /b %RC%

REM ===========================================================================
REM  Self-update handoff - reached only from the line above.
REM ===========================================================================
:self_update
REM  The folder this file sits in is about to be replaced whole, so the updater
REM  cannot be this file where it stands. Two things block that:
REM    - cmd.exe keeps the running .bat open for the whole run, and
REM    - :run just did `cd /d "%RUNDIR%"`, and a folder that is any process's
REM      current directory cannot be renamed at all.
REM  So: copy this file out, step out of the folder, hand over, and leave. The
REM  copy downloads and extracts first (seconds), by which time this process is
REM  long gone and the swap sees no handle of ours.
REM  The copy lands in a folder of its own, guaranteed to hold no INSTALL.ps1 -
REM  that absence is exactly what puts it in mode (A).
REM  Its name is ASCII on purpose: this file's own name is not, and a non-ASCII
REM  name must never be written into a .bat (see the ENCODING note at the top).
REM  A NEW NAME EVERY TIME, and nothing here is ever deleted. Double-clicking
REM  this file again while an update window is still working is a thing people
REM  do when a download is slow, and a fixed name would make that second run
REM  delete or overwrite the very file the first window is executing. The cost
REM  is one ~20KB file left in %TEMP% per update, which is what %TEMP% is for.
set "UPDDIR=%TEMP%\dooraydrive-update"
mkdir "%UPDDIR%" >nul 2>&1
set "UPDBAT=%UPDDIR%\dsync-update-%RANDOM%.bat"
copy /Y "%~f0" "%UPDBAT%" >nul 2>&1
if not exist "%UPDBAT%" goto :update_copy_failed
cd /d "%UPDDIR%"
REM  Hand the folder over in the ENVIRONMENT, not on the command line. The copy
REM  must not be left to its default (the drive IT sits on - %TEMP%, usually C:):
REM  for anyone installed elsewhere that forks a second install instead of
REM  updating this one. But `start` is the wrong place to pass it. Its first
REM  quoted token is the window TITLE, and once further quoted tokens follow a
REM  quoted command it stops parsing them as a command at all and opens an EMPTY
REM  INTERACTIVE WINDOW instead - measured 2026-09-16 on a real PC, which is how
REM  this comment came to exist: an invalid-name error and a bare cmd prompt
REM  sitting in %TEMP%, with the program never updated and nothing to say so.
REM  DSYNC_TARGET / DSYNC_VERSION are read at :parsed near the top of this very
REM  file, so the copy already knows how to receive them, and the child inherits
REM  them because it is started before endlocal. What is left on the start line
REM  is the one form that is never ambiguous: a title, one quoted path, nothing.
REM  (%PSARGS% is dropped here on purpose: the only switch that reaches this
REM  branch is one INSTALL.ps1 asked about, and -Check skips the prompt outright,
REM  so there is nothing to forward - and re-introducing a quoted tail is exactly
REM  what broke this line.)
set "DSYNC_TARGET=%RUNDIR%"
if defined VERSION set "DSYNC_VERSION=%VERSION%"
echo.
echo   Updating %RUNDIR% in a new window. This one closes now.
echo.
start "Dooray Drive update" "%UPDBAT%"
REM  A failed launch must not vanish: this window is about to close, and the
REM  alternative is a double-click that appears to have done nothing at all.
if errorlevel 1 goto :update_start_failed
endlocal & exit /b 0

:update_start_failed
echo.
echo   [STOP] Could not open the update window. Nothing was changed.
echo          Run this file instead - it does the same update:
echo          %UPDBAT%
goto :bail

:update_copy_failed
echo.
echo   [STOP] Could not put the updater in %TEMP%. Nothing was changed.
echo          Copy this file to your Desktop and run that copy instead -
echo          it does the same update from outside the folder.
goto :bail

REM ===========================================================================
REM  Errors (English - INSTALL.ps1 has not been reached yet)
REM ===========================================================================
:no_tools
if defined REFRESH goto :stale_fallback
echo.
echo   [STOP] curl or tar is missing (needs Windows 10 1809 or later).
echo          Download and unzip this into %TARGET% manually,
echo          then run the installer inside that folder:
echo          %ZIPURL%
goto :bail

:dl_failed
if defined REFRESH goto :stale_fallback
echo.
echo   [STOP] Download failed. Check your internet / corporate proxy,
echo          then run this file again.
goto :bail

:size_failed
if defined REFRESH goto :stale_fallback
echo.
echo   [STOP] Downloaded file is too small (%ZIPSIZE% bytes).
echo          A proxy login page was probably returned instead.
echo          Sign in to the corporate network in a browser, then retry.
del "%ZIPFILE%" >nul 2>&1
goto :bail

:extract_failed
if defined REFRESH goto :stale_fallback
echo.
echo   [STOP] Extraction failed. Check free space in %TEMP%
echo          and whether antivirus blocked it.
goto :bail

:move_failed
REM  Put the old copy back. The refresh renamed it aside a moment ago, so
REM  without this the PC is left with no installer at all.
if defined REFRESH if not exist "%TARGET%" move "%OLDDIR%" "%TARGET%" >nul 2>&1
if defined REFRESH if exist "%TARGET%\INSTALL.ps1" goto :stale_fallback
echo.
echo   [STOP] Could not create %TARGET%.
echo          A folder with that name may already exist, or permission denied.
echo          Run this file again and give another folder, for example:
echo            D:\dooraydrive
goto :bail

:version_failed
echo.
echo   [STOP] Could not fetch version %VERSION%.
echo          Nothing was changed - the copy at %TARGET% is untouched.
echo          Check that the tag exists and is spelled exactly:
echo          %ZIPURL%
echo          Tag list: https://github.com/martinyoo/dooraydrive/tags
del "%ZIPFILE%" >nul 2>&1
rmdir /S /Q "%EXDIR%" >nul 2>&1
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

:refresh_locked
echo.
echo   [STOP] Could not replace the existing copy at %TARGET%.
echo          Something in that folder is held open. The usual cause is a
echo          sync window still running (synchere, or the automatic loop) -
echo          close it, then run this again. An editor or Explorer window
echo          sitting in the folder does it too. Nothing was changed.
goto :bail

:bail
echo.
pause
endlocal & exit /b 1

REM ===========================================================================
REM  Rename %TARGET% aside. Returns with %TARGET% gone on success and OLDDIR
REM  holding the name it went to; the caller tests %TARGET% itself. Called only
REM  when REFRESH is set, and never inside setlocal - OLDDIR must survive.
REM ===========================================================================
:swap_aside
set "SWAPTRY=0"
:swap_try
rmdir /S /Q "%OLDDIR%" >nul 2>&1
if exist "%OLDDIR%" set "OLDDIR=%TARGET%.old%RANDOM%"
move "%TARGET%" "%OLDDIR%" >nul 2>&1
if not exist "%TARGET%" goto :eof
set /a SWAPTRY+=1
if %SWAPTRY% GEQ 3 goto :eof
REM  ping, not timeout: timeout fails outright when stdin is redirected.
ping -n 2 127.0.0.1 >nul 2>&1
goto :swap_try
