# 전체 프로파일 일괄 동기화 —  .\SYNC.ps1
#
# 프로파일 목록을 config.toml에서 직접 읽는다. 폴더(프로파일)를 추가해도
# 이 스크립트는 고칠 필요가 없다.
#
# 기본 동작은 "계획을 먼저 보여주고 확인받은 뒤 실행"이다.
# push는 원격을 삭제·이동하지 않고, pull은 수정된 로컬 파일을 덮어쓰지 않는다.
#
# 사용 예
#   .\SYNC.ps1                    전체 push (계획 확인 후 실행)
#   .\SYNC.ps1 -Pull              전체 pull
#   .\SYNC.ps1 -DryRun            계획만 보고 끝
#   .\SYNC.ps1 -Yes               확인 없이 바로 실행
#   .\SYNC.ps1 -Status            상태만 요약
#   .\SYNC.ps1 -Only spri2026,swstat    일부 프로파일만
param(
  [switch]$Pull,
  [switch]$DryRun,
  [switch]$Yes,
  [switch]$Status,
  [string[]]$Only
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$verb = if ($Pull) { 'pull' } else { 'push' }

# ---------------------------------------------------------------------------
# 프로파일 목록 (config.toml에서 읽음)
# ---------------------------------------------------------------------------
$profiles = python -c "import sys,tomllib;sys.path.insert(0,'.');from dooray_sync.config import config_path;d=tomllib.load(open(config_path(),'rb'));print('\n'.join(d.get('profile',{}).keys()))" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $profiles) {
  Write-Host "설정을 읽지 못했습니다. 'dsync init'을 먼저 실행하세요." -ForegroundColor Red
  exit 1
}
$profiles = @($profiles -split "`r?`n" | Where-Object { $_ })
if ($Only) {
  $unknown = $Only | Where-Object { $_ -notin $profiles }
  if ($unknown) {
    Write-Host ("모르는 프로파일: {0}" -f ($unknown -join ', ')) -ForegroundColor Red
    Write-Host ("설정에 있는 것: {0}" -f ($profiles -join ', '))
    exit 1
  }
  $profiles = @($Only)
}

# ---------------------------------------------------------------------------
# 상태만 보기
# ---------------------------------------------------------------------------
if ($Status) {
  foreach ($p in $profiles) {
    Write-Host ("== {0}" -f $p) -ForegroundColor Cyan
    python -m dooray_sync.cli.main status -p $p |
      Select-String -Pattern '총 항목|sync_status|synced|error|pending|미해결 충돌|미완료 저널|마지막 push|마지막 pull'
  }
  exit 0
}

# ---------------------------------------------------------------------------
# 1) 계획 확인 (아무것도 바꾸지 않음)
# ---------------------------------------------------------------------------
Write-Host ("== 계획 확인 ({0}, 프로파일 {1}개) ==" -f $verb, $profiles.Count) -ForegroundColor Cyan
Write-Host "  이 단계는 아무것도 바꾸지 않습니다." -ForegroundColor DarkGray
Write-Host ""

$todo = @()
foreach ($p in $profiles) {
  $out = python -m dooray_sync.cli.main $verb -p $p --dry-run 2>&1
  $rc  = $LASTEXITCODE
  $txt = $out -join "`n"

  if ($rc -ne 0) {
    Write-Host ("  [실패] {0} — 계획 산출 실패(종료코드 {1})" -f $p, $rc) -ForegroundColor Red
    ($out | Select-Object -Last 3) | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkGray }
    continue
  }
  # '변경 없음(건너뜀)' 같은 요약줄과 구분해서 계획 유무를 판정한다.
  if ($txt -match '(?m)^\s*변경 없음\s*$') {
    Write-Host ("  [-] {0,-10} 변경 없음" -f $p) -ForegroundColor DarkGray
    continue
  }
  $n = @($out | Select-String -Pattern '신규업로드|새버전|폴더생성|기록갱신|신규다운로드|덮어쓰기').Count
  Write-Host ("  [*] {0,-10} 계획 {1}건" -f $p, $n) -ForegroundColor Yellow
  ($out | Select-String -Pattern '^\s*(신규업로드|새버전|폴더생성|기록갱신|신규다운로드|덮어쓰기)' |
     Select-Object -First 5) | ForEach-Object { Write-Host ("        {0}" -f $_.Line.Trim()) -ForegroundColor DarkGray }
  if ($n -gt 5) { Write-Host ("        ... 외 {0}건" -f ($n - 5)) -ForegroundColor DarkGray }
  $todo += $p
}

if ($todo.Count -eq 0) {
  Write-Host ""
  Write-Host "모두 최신 상태입니다. 할 일이 없습니다." -ForegroundColor Green
  exit 0
}

if ($DryRun) {
  Write-Host ""
  Write-Host ("계획만 확인했습니다. 실행하려면 -DryRun 없이 다시 실행하세요. (대상: {0})" -f ($todo -join ', ')) -ForegroundColor Green
  exit 0
}

# ---------------------------------------------------------------------------
# 2) 확인
# ---------------------------------------------------------------------------
Write-Host ""
if (-not $Yes) {
  Write-Host ("실행 대상: {0}" -f ($todo -join ', ')) -ForegroundColor Yellow
  if ($verb -eq 'push') {
    Write-Host "push는 원격을 삭제·이동하지 않습니다(추가와 새 버전만)." -ForegroundColor DarkGray
  } else {
    Write-Host "pull은 수정된 로컬 파일을 덮어쓰지 않습니다(보호로 건너뜁니다)." -ForegroundColor DarkGray
  }
  $ans = Read-Host "진행할까요? (y/N)"
  if ($ans -notmatch '^[Yy]') {
    Write-Host "취소했습니다. 아무것도 바꾸지 않았습니다." -ForegroundColor DarkGray
    exit 0
  }
}

# ---------------------------------------------------------------------------
# 3) 실행 — 한 프로파일이 실패해도 나머지는 계속한다
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ("== 실행 ({0}) ==" -f $verb) -ForegroundColor Cyan
$failed = @()
foreach ($p in $todo) {
  Write-Host ""
  Write-Host ("-- {0}" -f $p) -ForegroundColor Cyan
  python -m dooray_sync.cli.main $verb -p $p
  if ($LASTEXITCODE -ne 0) {
    Write-Host ("   [실패] {0} — 종료코드 {1}" -f $p, $LASTEXITCODE) -ForegroundColor Red
    $failed += $p
  }
}

# ---------------------------------------------------------------------------
Write-Host ""
if ($failed.Count -gt 0) {
  Write-Host ("실패한 프로파일: {0}" -f ($failed -join ', ')) -ForegroundColor Red
  Write-Host "파일 단위로 격리되므로 나머지는 처리됐습니다. 다시 실행하면 실패분만 재시도합니다." -ForegroundColor Yellow
  Write-Host "계속 실패하면:  .\dsync doctor -p <프로파일>" -ForegroundColor Yellow
  exit 1
}
Write-Host "전부 완료했습니다." -ForegroundColor Green
Write-Host "확인:  .\SYNC.ps1 -DryRun   (남은 게 없으면 '변경 없음')" -ForegroundColor DarkGray
