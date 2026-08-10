# [폐지 예정 2026-08-10] 이 스크립트는 더 이상 설치 흐름에서 쓰이지 않는다.
# 하드코딩된 프로파일 목록은 특정 사용자 전용이다. 대체 흐름: 동기화할 폴더에
# synchere.bat를 복사해 더블클릭하면 그 폴더가 등록되고 시작된다.
#
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
Write-Host "== 2-1) 원격 스캔 완료 확인 ==" -ForegroundColor Cyan
$missing = @()
foreach ($j in $jobs) {
  if (-not (Test-Path -LiteralPath $j.local)) { continue }
  # 기록 '건수'가 아니라 원격 스캔이 끝났는지를 본다.
  # 건수로 판정하면 새로 만든 빈 원격 폴더가 정상인데도 막힌다(spri2026 사례).
  # last_full_scan_at은 init이 원격 스캔을 끝낸 직후에만 기록되므로 이쪽이 정확한 신호다.
  $out = python -c "import sys;sys.path.insert(0,'.');from dooray_sync.config import db_path,load_config;from dooray_sync.store.db import Store,META_LAST_FULL_SCAN;p=load_config('$($j.p)');s=Store(db_path('$($j.p)'));print((s.get_meta(META_LAST_FULL_SCAN) or '-')+'|'+str(s.count_files(p.drive_id)));s.close()" 2>$null
  $scan, $n = ($out -split '\|')
  if ($LASTEXITCODE -eq 0 -and $scan -and $scan -ne '-') {
    Write-Host ("  [정상] {0} — 원격 기록 {1}건 (스캔 {2})" -f $j.p, $n, $scan) -ForegroundColor DarkGray
  } else {
    Write-Host ("  [스캔 미완료] {0} — init의 원격 스캔이 끝나지 않았습니다" -f $j.p) -ForegroundColor Red
    $missing += $j.p
  }
}
if ($missing.Count -gt 0) {
  Write-Host ""
  Write-Host "원격 스캔이 끝나지 않은 프로파일이 있습니다: $($missing -join ', ')" -ForegroundColor Red
  Write-Host "기준선 없이 push하면 원격에 이미 있는 파일을 신규로 오인해 중복 업로드하니 중단합니다." -ForegroundColor Red
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
