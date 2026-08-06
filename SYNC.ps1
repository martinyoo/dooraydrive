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

# 두 스트림을 각각 파일로 받는다.
#   - 2>&1 을 쓰면 안 된다: PowerShell 5.1이 네이티브 exe의 stderr 한 줄을 NativeCommandError로
#     감싸고, $ErrorActionPreference='Stop' 아래에서 종료 오류가 되어 스크립트가 그 자리에서 죽는다.
#     실제로 '보류'가 있는 프로파일에서 중단되어 뒤 프로파일이 확인조차 안 됐다.
#   - 그런데 stderr를 버려도 안 된다: '보류'·'읽기 실패'·'경고'가 전부 stderr로 나가므로,
#     stdout만 보면 1,000건이 막혀 있어도 '변경 없음'으로 읽힌다(fail-open).
function Invoke-Dsync {
  param([string[]]$DsyncArgs)
  $o = [System.IO.Path]::GetTempFileName()
  $e = [System.IO.Path]::GetTempFileName()
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    & python -m dooray_sync.cli.main @DsyncArgs > $o 2> $e
    $rc = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  $res = [pscustomobject]@{
    Code   = $rc
    Out    = (Get-Content -LiteralPath $o -Raw -Encoding UTF8 -ErrorAction SilentlyContinue)
    Err    = (Get-Content -LiteralPath $e -Raw -Encoding UTF8 -ErrorAction SilentlyContinue)
  }
  Remove-Item -LiteralPath $o, $e -Force -ErrorAction SilentlyContinue
  return $res
}

$todo    = @()
$blocked = @()
foreach ($p in $profiles) {
  $r = Invoke-Dsync @($verb, '-p', $p, '--dry-run')
  $out = "$($r.Out)"; $err = "$($r.Err)"

  if ($r.Code -ne 0) {
    Write-Host ("  [실패] {0,-10} 계획 산출 실패 (종료코드 {1})" -f $p, $r.Code) -ForegroundColor Red
    ($err -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 3) |
      ForEach-Object { Write-Host "             $_" -ForegroundColor DarkGray }
    continue
  }

  # stderr에만 나오는 것들 — 이걸 안 보면 막힌 파일을 '이상 없음'으로 오독한다.
  $hold = if ($err -match '보류\s+(\d+)건')      { [int]$Matches[1] } else { 0 }
  $rerr = if ($err -match '읽기 실패\s+(\d+)건') { [int]$Matches[1] } else { 0 }
  $skip = if ($out -match '스캔 제외\s*:\s*(\d+)건') { [int]$Matches[1] } else { 0 }

  $n = @([regex]::Matches($out, '(?m)^\s*(신규업로드|새버전|폴더생성|기록갱신|신규다운로드|덮어쓰기)\s')).Count
  $none = $out -match '(?m)^\s*변경 없음\s*$'

  if ($none -and $hold -eq 0 -and $rerr -eq 0 -and $skip -eq 0) {
    Write-Host ("  [-] {0,-10} 변경 없음" -f $p) -ForegroundColor DarkGray
    continue
  }

  if (-not $none) {
    Write-Host ("  [*] {0,-10} 계획 {1}건" -f $p, $n) -ForegroundColor Yellow
    ([regex]::Matches($out, '(?m)^\s*(신규업로드|새버전|폴더생성|기록갱신|신규다운로드|덮어쓰기)\s.*$') |
       Select-Object -First 5) | ForEach-Object { Write-Host ("        {0}" -f $_.Value.Trim()) -ForegroundColor DarkGray }
    if ($n -gt 5) { Write-Host ("        ... 외 {0}건" -f ($n - 5)) -ForegroundColor DarkGray }
    $todo += $p
  }

  # 막힌 것은 계획이 없어도 반드시 보고한다.
  if ($hold -gt 0) {
    Write-Host ("      [보류] {0}건 — 원격에도 있는데 기준선이 없어 올리지 않습니다." -f $hold) -ForegroundColor Yellow
    Write-Host  "             해소:  .\dsync reconcile -p $p" -ForegroundColor DarkGray
    Write-Host  "             (화면이 권하는 --assume-local-newer 는 쓰지 마세요 — 보류 전체를 덮어쓰는 전역 스위치입니다)" -ForegroundColor DarkGray
    $blocked += "$p(보류 $hold)"
  }
  if ($rerr -gt 0) {
    Write-Host ("      [읽기 실패] {0}건 — 파일이 잠겨 있습니다. 오피스·한글을 닫고 다시 확인하세요." -f $rerr) -ForegroundColor Red
    $blocked += "$p(읽기실패 $rerr)"
  }
  if ($skip -gt 0) {
    Write-Host ("      [스캔 제외] {0}건 — 이 파일들은 계획에 아예 없습니다. 사유를 확인하세요." -f $skip) -ForegroundColor Red
    $blocked += "$p(스캔제외 $skip)"
  }
}

if ($blocked.Count -gt 0) {
  Write-Host ""
  Write-Host ("막혀 있는 항목: {0}" -f ($blocked -join ', ')) -ForegroundColor Yellow
  Write-Host "이 파일들은 push해도 올라가지 않습니다. 위 안내대로 먼저 해소하세요." -ForegroundColor Yellow
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
  $r = Invoke-Dsync @($verb, '-p', $p)
  if ($r.Out) { Write-Host $r.Out.TrimEnd() }
  if ($r.Err) { Write-Host $r.Err.TrimEnd() -ForegroundColor Yellow }
  # 종료코드만 믿지 않는다 — 보류·스캔 제외는 종료코드에 반영되지 않아
  # 1,000건을 안 올린 push도 '실패 0건 / exit 0'으로 끝난다.
  $left = if ("$($r.Err)" -match '보류\s+(\d+)건') { [int]$Matches[1] } else { 0 }
  if ($r.Code -ne 0) {
    Write-Host ("   [실패] {0} — 종료코드 {1}" -f $p, $r.Code) -ForegroundColor Red
    $failed += $p
  } elseif ($left -gt 0) {
    Write-Host ("   [주의] {0} — 보류 {1}건은 올라가지 않았습니다(종료코드는 0입니다)." -f $p, $left) -ForegroundColor Yellow
    $failed += "$p(보류 $left)"
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
