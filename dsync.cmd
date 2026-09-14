@echo off
REM Dooray Drive 동기화 CLI 실행 래퍼
REM 사용법:  dsync -p <프로파일> push --dry-run
setlocal
cd /d "%~dp0"
python -m dooray_sync.cli.main %*
endlocal
