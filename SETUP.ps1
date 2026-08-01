# Dooray Sync 최초 설정 — PowerShell에서 이 파일을 실행하세요:  .\SETUP.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$DRIVE = '3229053305881780627'
$BASE  = "$env:USERPROFILE\내 드라이브(martin.hs.yoo@gmail.com)\WORK"

$jobs = @(
  @{ p='spri2025'; remote='WORK/spri 2025'; local="$BASE\spri 2025" },
  @{ p='spri2026'; remote='WORK/spri 2026'; local="$BASE\spri 2026" },
  @{ p='swstat';   remote='WORK/SW통계';    local="$BASE\SW통계"    },
  @{ p='workenv';  remote='근무환경';        local="$BASE\근무환경"   }
)

Write-Host "== 1) 토큰·연결 점검 ==" -ForegroundColor Cyan
python -m dooray_sync.cli.main doctor
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "doctor가 실패했습니다. 토큰이 없으면 아래를 먼저 실행하세요:" -ForegroundColor Yellow
  Write-Host '  pip install -r requirements.txt'
  Write-Host '  python -c "import keyring; keyring.set_password(''dooray-sync'',''api-token'',''발급받은토큰'')"'
  Write-Host "(설정이 아직 없다는 경고는 정상입니다 — 계속 진행합니다)" -ForegroundColor DarkGray
}

foreach ($j in $jobs) {
  Write-Host ""
  Write-Host ("== 2) init: {0}  ->  {1}" -f $j.p, $j.remote) -ForegroundColor Cyan
  if (-not (Test-Path -LiteralPath $j.local)) {
    Write-Host ("  건너뜀 — 로컬 폴더가 없습니다: {0}" -f $j.local) -ForegroundColor Yellow
    continue
  }
  python -m dooray_sync.cli.main init -p $j.p --force --drive-id $DRIVE `
      --remote-path $j.remote --local-root $j.local --create-remote
}

Write-Host ""
Write-Host "== 2-1) 상태 DB 생성 확인 ==" -ForegroundColor Cyan
$missing = @()
foreach ($j in $jobs) {
  if (-not (Test-Path -LiteralPath $j.local)) { continue }
  # 파일 존재만으로는 부족하다 — 빈 DB는 다른 명령이 스키마만 만들어 놓은 것일 수 있고,
  # 그 상태로 push하면 원격에 이미 있는 파일을 신규로 오인해 중복 업로드한다.
  $n = python -c "import sys;sys.path.insert(0,'.');from dooray_sync.config import db_path,load_config;from dooray_sync.store.db import Store;p=load_config('$($j.p)');s=Store(db_path('$($j.p)'));print(s.count_files(p.drive_id));s.close()" 2>$null
  if ($LASTEXITCODE -eq 0 -and [int]$n -gt 0) {
    Write-Host ("  [정상] {0} — 원격 기록 {1}건" -f $j.p, $n) -ForegroundColor DarkGray
  } else {
    Write-Host ("  [비어있음] {0} — 원격 기록 {1}건" -f $j.p, $n) -ForegroundColor Red
    $missing += $j.p
  }
}
if ($missing.Count -gt 0) {
  Write-Host ""
  Write-Host "원격 기록이 비어 있는 프로파일이 있습니다: $($missing -join ', ')" -ForegroundColor Red
  Write-Host "이 상태로 push하면 원격에 중복 파일이 생길 수 있으니 중단합니다." -ForegroundColor Red
  exit 1
}

Write-Host ""
Write-Host "== 3) 기준선 대조 (원격에 이미 있는 파일) ==" -ForegroundColor Cyan
foreach ($p in @('swstat','workenv')) {
  Write-Host ("-- {0}" -f $p)
  python -m dooray_sync.cli.main reconcile -p $p
}

Write-Host ""
Write-Host "설정 완료. 다음은 업로드 계획 확인입니다:" -ForegroundColor Green
Write-Host "  .\dsync push -p spri2025 --dry-run"
