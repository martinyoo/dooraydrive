# 두 번째 PC(회사 PC) 최초 설정 — PowerShell에서 실행:  .\SETUP-2ND-PC.ps1
#
# 이미 원격에 올라가 있는 내용을 이 PC로 내려받기 위한 설정이다.
# SETUP.ps1(첫 PC용)과 다른 점:
#   - 원격 폴더를 만들지 않는다(--create-remote 없음). 없으면 실패시킨다 — 오타로 빈 폴더가
#     새로 생기면 pull이 0건을 받고 정상처럼 보이기 때문이다.
#   - --force를 쓰지 않는다. 설정이 이미 있으면 멈춘다(기존 기준선 보호).
#   - 로컬이 비어 있다고 가정한다. 파일이 이미 있으면 init 뒤 reconcile을 먼저 돌려야 한다.
#
# 사용 예:  .\SETUP-2ND-PC.ps1 -LocalBase 'D:\Dooray'
param(
  [string]$LocalBase = 'C:\Dooray'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$DRIVE   = '3229053305881780627'
$NEED_GB = 11.5   # 원격 실측 11.24GB + 여유

$jobs = @(
  @{ p='spri2025'; remote='WORK/spri 2025'; sub='spri 2025'; files=700; gb=4.13 },
  @{ p='spri2026'; remote='WORK/spri 2026'; sub='spri 2026'; files=621; gb=2.23 },
  @{ p='swstat';   remote='WORK/SW통계';    sub='SW통계';    files=13;  gb=0.35 },
  @{ p='workenv';  remote='근무환경';        sub='근무환경';   files=440; gb=4.53 }
)

# ---------------------------------------------------------------------------
Write-Host "== 0) 사전 점검 ==" -ForegroundColor Cyan

$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) {
  Write-Host "  [실패] python을 찾을 수 없습니다." -ForegroundColor Red
  Write-Host "  python.org에서 3.11 이상 설치 (설치 첫 화면 'Add python.exe to PATH' 체크)"
  exit 1
}
$ver = (python -c "import sys;print('%d.%d'%sys.version_info[:2])")
Write-Host ("  [정상] python {0} — {1}" -f $ver, $py.Source) -ForegroundColor DarkGray

python -c "import httpx, keyring" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "  [실패] 의존성이 없습니다. 먼저 실행하세요:" -ForegroundColor Red
  Write-Host "    pip install -r requirements.txt"
  exit 1
}
Write-Host "  [정상] 의존성 확인" -ForegroundColor DarkGray

# 디스크 여유 — 받을 양보다 적으면 중간에 멈춘다
$qualifier = Split-Path -Qualifier $LocalBase
if ($qualifier) {
  $freeGB = [math]::Round((Get-PSDrive ($qualifier -replace ':','')).Free / 1GB, 1)
  if ($freeGB -lt $NEED_GB) {
    Write-Host ("  [실패] {0} 여유 {1}GB — 최소 {2}GB 필요" -f $qualifier, $freeGB, $NEED_GB) -ForegroundColor Red
    exit 1
  }
  Write-Host ("  [정상] {0} 여유 {1}GB (필요 {2}GB)" -f $qualifier, $freeGB, $NEED_GB) -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "== 1) 토큰·연결 점검 ==" -ForegroundColor Cyan
python -m dooray_sync.cli.main doctor
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "토큰이 없으면 먼저 등록하세요 (PC마다 따로 등록해야 합니다):" -ForegroundColor Yellow
  Write-Host '  python -c "import keyring; keyring.set_password(''dooray-sync'',''api-token'',''발급받은토큰'')"'
  Write-Host "(설정이 아직 없다는 경고는 정상입니다 — 계속 진행합니다)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ("== 2) 프로파일 init  (로컬 기준: {0}) ==" -f $LocalBase) -ForegroundColor Cyan

foreach ($j in $jobs) {
  $local = Join-Path $LocalBase $j.sub
  Write-Host ""
  Write-Host ("-- {0}  ->  {1}" -f $j.p, $j.remote) -ForegroundColor Cyan

  # 로컬에 파일이 이미 있으면 이 스크립트의 전제(빈 로컬)가 깨진다.
  if (Test-Path -LiteralPath $local) {
    $n = @(Get-ChildItem -LiteralPath $local -Recurse -File -ErrorAction SilentlyContinue).Count
    if ($n -gt 0) {
      Write-Host ("  [주의] 로컬에 이미 {0}개 파일이 있습니다: {1}" -f $n, $local) -ForegroundColor Yellow
      Write-Host  "         init 뒤 'dsync reconcile -p $($j.p)'를 먼저 실행해야 합니다." -ForegroundColor Yellow
      Write-Host  "         (기준선 없이 pull하면 전부 '보류'로 멈춥니다)" -ForegroundColor DarkGray
    }
  }

  python -m dooray_sync.cli.main init -p $j.p --drive-id $DRIVE `
      --remote-path $j.remote --local-root $local
  if ($LASTEXITCODE -ne 0) {
    Write-Host ("  [실패] {0} init 실패 — 중단합니다." -f $j.p) -ForegroundColor Red
    Write-Host  "         원격 폴더 이름이 정확한지, 토큰이 유효한지 확인하세요." -ForegroundColor Red
    exit 1
  }
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== 2-1) 원격 스캔 완료 확인 ==" -ForegroundColor Cyan
# 건수가 아니라 스캔이 끝났는지를 본다(SETUP.ps1과 같은 판정). 자세한 이유는
# docs/에이전트_운영교훈.md §5 참조.
$missing = @()
foreach ($j in $jobs) {
  $out = python -c "import sys;sys.path.insert(0,'.');from dooray_sync.config import db_path,load_config;from dooray_sync.store.db import Store,META_LAST_FULL_SCAN;p=load_config('$($j.p)');s=Store(db_path('$($j.p)'));print((s.get_meta(META_LAST_FULL_SCAN) or '-')+'|'+str(s.count_files(p.drive_id)));s.close()" 2>$null
  $scan, $n = ($out -split '\|')
  if ($LASTEXITCODE -eq 0 -and $scan -and $scan -ne '-') {
    Write-Host ("  [정상] {0} — 원격 기록 {1}건 (예상 {2}건)" -f $j.p, $n, $j.files) -ForegroundColor DarkGray
  } else {
    Write-Host ("  [스캔 미완료] {0}" -f $j.p) -ForegroundColor Red
    $missing += $j.p
  }
}
if ($missing.Count -gt 0) {
  Write-Host ""
  Write-Host "원격 스캔이 끝나지 않은 프로파일이 있습니다: $($missing -join ', ')" -ForegroundColor Red
  exit 1
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "설정 완료. 다음은 내려받기입니다 — 총 1,774파일 / 약 11.2GB." -ForegroundColor Green
Write-Host ""
Write-Host "먼저 계획만 확인하고(아무것도 바꾸지 않습니다):" -ForegroundColor Green
Write-Host "  .\dsync pull -p swstat --dry-run"
Write-Host ""
Write-Host "작은 것부터 실제로 받아 보고, 정상이면 나머지를 받으세요:" -ForegroundColor Green
Write-Host "  .\dsync pull -p swstat      # 13파일 / 0.35GB — 경로 검증용"
Write-Host "  .\dsync pull -p spri2026    # 621파일 / 2.23GB"
Write-Host "  .\dsync pull -p spri2025    # 700파일 / 4.13GB"
Write-Host "  .\dsync pull -p workenv     # 440파일 / 4.53GB"
Write-Host ""
Write-Host "중단되어도 안전합니다 — 다시 실행하면 받은 것은 건너뛰고 남은 것만 받습니다." -ForegroundColor DarkGray
