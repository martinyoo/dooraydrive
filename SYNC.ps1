# 전체 프로파일 일괄 동기화 —  .\SYNC.ps1
#
# 프로파일 목록을 config.toml에서 직접 읽는다. 폴더(프로파일)를 추가해도
# 이 스크립트는 고칠 필요가 없다.
#
# 기본 동작은 "계획을 먼저 보여주고 확인받은 뒤 실행"이다.
# push는 원격을 삭제·이동하지 않고, pull은 수정된 로컬 파일을 덮어쓰지 않는다.
# sync는 양방향이며 삭제는 전파하지 않고 보고만 한다(충돌은 양쪽 보존).
#
# ⚠ 이 스크립트는 어떤 경로로도 --propagate-deletes / --allow-bulk-delete 를
#   CLI에 넘기지 않는다. 삭제를 정말 전파하려면 CLI를 직접 실행한다.
#
# 사용 예
#   .\SYNC.ps1                    전체 push (계획 확인 후 실행)
#   .\SYNC.ps1 -Pull              전체 pull
#   .\SYNC.ps1 -Sync              전체 양방향 동기화 (제외 목록 적용)
#   .\SYNC.ps1 -DryRun            계획만 보고 끝
#   .\SYNC.ps1 -Yes               확인 없이 바로 실행
#   .\SYNC.ps1 -Status            상태만 요약
#   .\SYNC.ps1 -Only spri2026,swstat    일부 프로파일만
param(
  [switch]$Pull,
  [switch]$Sync,
  [switch]$DryRun,
  [switch]$Yes,
  [switch]$Status,
  [string[]]$Only
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if ($Pull -and $Sync) {
  Write-Host "-Pull과 -Sync는 함께 쓸 수 없습니다." -ForegroundColor Red
  exit 1
}
$verb = 'push'
if ($Pull) { $verb = 'pull' }
if ($Sync) { $verb = 'sync' }

# ---------------------------------------------------------------------------
# 프로파일 목록 (config.toml에서 읽음)
# ---------------------------------------------------------------------------
# 이름·sync_mode·sync_note를 탭 구분으로 읽는다(정책의 단일 정본 = config.toml).
# 주의: PS 5.1은 네이티브 인자 속 큰따옴표를 이스케이프하지 않으므로 이 파이썬
# 코드에는 큰따옴표·백슬래시를 넣지 않는다(탭·개행은 chr()로).
$raw = python -c "import sys,tomllib;sys.path.insert(0,'.');from dooray_sync.config import config_path;d=tomllib.load(open(config_path(),'rb'));print(chr(10).join(n+chr(9)+str((b or {}).get('sync_mode','') or '')+chr(9)+str((b or {}).get('sync_note','') or '').replace(chr(9),' ').replace(chr(10),' ') for n,b in d.get('profile',{}).items()))" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $raw) {
  Write-Host "설정을 읽지 못했습니다. 'dsync init'을 먼저 실행하세요." -ForegroundColor Red
  exit 1
}
$profiles = @()
$modeMap = @{}
$noteMap = @{}
foreach ($line in @($raw -split "`r?`n" | Where-Object { $_ })) {
  $parts = $line -split "`t", 3
  $profiles += $parts[0]
  $modeMap[$parts[0]] = if ($parts.Count -ge 2) { $parts[1] } else { '' }
  $noteMap[$parts[0]] = if ($parts.Count -ge 3) { $parts[2] } else { '' }
}
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
    # '마지막 sync'는 status CLI가 출력하지 않아 meta에서 직접 읽는다.
    # mode=ro 필수 — 일반 connect는 DB 파일이 없으면 빈 파일을 만들어 버린다.
    # 주의: 이 파이썬 코드에는 큰따옴표를 쓰면 안 된다 — PS 5.1이 네이티브 인자의
    # 내장 큰따옴표를 이스케이프하지 않아 인자가 그 지점에서 쪼개진다(실측).
    $lastSync = python -c @'
import os, sqlite3, sys
sys.path.insert(0, '.')
from dooray_sync.config import db_path
p = str(db_path(sys.argv[1]))
v = '-'
if os.path.exists(p):
    try:
        c = sqlite3.connect('file:' + p.replace(chr(92), '/') + '?mode=ro', uri=True)
        r = c.execute('SELECT value FROM meta WHERE key=?', ('last_sync_at',)).fetchone()
        if r:
            v = r[0]
    except sqlite3.Error:
        pass
print(v)
'@ $p
    Write-Host ("  마지막 sync      : {0}" -f $lastSync)
  }
  exit 0
}

# ---------------------------------------------------------------------------
# sync 정책 — 정본은 config.toml의 sync_mode다 (변경: tools/set_sync_mode.py).
# 'sync'만 실행하고 나머지(push/pull/off/미설정)는 사유를 표시하고 건너뛴다.
# -Only 로도 우회할 수 없다 — 우회해 봤자 CLI 게이트가 exit 2로 막는다.
# ---------------------------------------------------------------------------
if ($verb -eq 'sync') {
  foreach ($k in @($profiles)) {
    if ($modeMap[$k] -ne 'sync') {
      $why = $noteMap[$k]
      if (-not $why) {
        if ($modeMap[$k]) { $why = "sync_mode=$($modeMap[$k])" }
        else { $why = "sync_mode 미설정 — python tools\set_sync_mode.py $k sync" }
      }
      Write-Host ("  [제외] {0} — {1}" -f $k, $why) -ForegroundColor DarkGray
    }
  }
  $profiles = @($profiles | Where-Object { $modeMap[$_] -eq 'sync' })
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

  # --- sync 전용 판정 -------------------------------------------------------
  # 라벨 존재가 아니라 값을 본다(교훈 §5). sync 요약 kv에는 '변경 없음 : N건'이
  # 항상 있으므로 비앵커 '변경 없음' 매칭은 금지다(앵커형만 사용).
  if ($verb -eq 'sync') {
    $del = -1
    if ($out -match '실제로 사라질 항목\s*:\s*(\d+)건') { $del = [int]$Matches[1] }
    if ($del -ne 0) {
      # 이 스크립트는 삭제 전파를 절대 켜지 않으므로 항상 0이어야 한다.
      # 0이 아니거나 값을 못 읽으면 실행하지 않는다(fail-closed).
      $delTxt = "${del}건"
      if ($del -lt 0) { $delTxt = '판독 불가' }
      Write-Host ("  [중단] {0,-10} 실제로 사라질 항목 {1} — -Sync는 삭제를 실행하지 않습니다" -f $p, $delTxt) -ForegroundColor Red
      Write-Host  "             원인 확인:  .\dsync sync -p $p --full --dry-run" -ForegroundColor Yellow
      $blocked += "$p(사라질 $delTxt)"
      continue
    }

    $labels = '원격폴더생성|로컬폴더생성|신규업로드|새버전업로드|신규받기|갱신받기|충돌보존|로컬이동|원격이동|로컬휴지통|원격휴지통|기록갱신|기록정리'
    $n = 0; $conf = 0
    foreach ($m in [regex]::Matches($out, "(?m)^\s*($labels)\s+(\d+)건")) {
      $n += [int]$m.Groups[2].Value
      if ($m.Groups[1].Value -eq '충돌보존') { $conf += [int]$m.Groups[2].Value }
    }
    $none = $out -match '(?m)^\s*변경 없음\s*$'
    if ($none -ne ($n -eq 0)) {
      Write-Host ("  [실패] {0,-10} 출력 형식이 예상과 다릅니다(판정 불일치) — 직접 확인:  .\dsync sync -p {0} --dry-run" -f $p) -ForegroundColor Red
      continue
    }

    if ($n -gt 0) {
      $up = ''; $down = ''
      if ($out -match '올릴 용량\s*:\s*(\S+)') { $up = $Matches[1] }
      if ($out -match '받을 용량\s*:\s*(\S+)') { $down = $Matches[1] }
      Write-Host ("  [*] {0,-10} 계획 {1}건 (올릴 {2} / 받을 {3})" -f $p, $n, $up, $down) -ForegroundColor Yellow
      if ($conf -gt 0) {
        Write-Host ("      [충돌] {0}건 — 양쪽 내용을 모두 보존합니다. 실행 후 정리:  .\dsync resolve -p {1}" -f $conf, $p) -ForegroundColor Yellow
      }
      # 미리보기는 '동작별 건수' 표 이전 구간에서만 — 같은 라벨이 두 표에 나온다.
      $head = ($out -split '(?m)^\s*동작별 건수\s*$')[0]
      $rows = @([regex]::Matches($head, "(?m)^\s*($labels)\s+\S.*$") | ForEach-Object { $_.Value.Trim() })
      ($rows | Select-Object -First 5) | ForEach-Object { Write-Host ("        {0}" -f $_) -ForegroundColor DarkGray }
      if ($rows.Count -gt 5) { Write-Host ("        ... 외 {0}건" -f ($rows.Count - 5)) -ForegroundColor DarkGray }
      $todo += $p
    } else {
      $chg = 0
      if ($out -match 'changes 항목\s*:\s*(\d+)건') { $chg = [int]$Matches[1] }
      if ($chg -ge 1000) {
        Write-Host ("  [-] {0,-10} 변경 없음 (밀린 변경기록 {1}건 — '.\dsync sync -p {0}' 1회 실행으로 커서를 전진시키면 다음부터 빨라집니다)" -f $p, $chg) -ForegroundColor DarkGray
      } else {
        Write-Host ("  [-] {0,-10} 변경 없음" -f $p) -ForegroundColor DarkGray
      }
    }

    # 막힌 것은 계획이 없어도 반드시 보고한다(push 경로와 같은 규칙).
    $prot = 0
    if ($out -match '(?m)^\s*보호\s*:\s*(\d+)건') { $prot = [int]$Matches[1] }
    if ($prot -gt 0) {
      Write-Host ("      [보호] {0}건 — 덮어쓰지 않고 남겨둔 파일입니다. 상세:  .\dsync sync -p {1} --dry-run" -f $prot, $p) -ForegroundColor Yellow
      $blocked += "$p(보호 $prot)"
    }
    if ($rerr -gt 0) {
      Write-Host ("      [읽기 실패] {0}건 — 파일이 잠겨 있습니다. 오피스·한글을 닫고 다시 확인하세요." -f $rerr) -ForegroundColor Red
      $blocked += "$p(읽기실패 $rerr)"
    }
    if ($skip -gt 0) {
      Write-Host ("      [스캔 제외] {0}건 — 이 파일들은 계획에 아예 없습니다. 사유를 확인하세요." -f $skip) -ForegroundColor Red
      $blocked += "$p(스캔제외 $skip)"
    }
    continue
  }

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
  } elseif ($verb -eq 'pull') {
    Write-Host "pull은 수정된 로컬 파일을 덮어쓰지 않습니다(보호로 건너뜁니다)." -ForegroundColor DarkGray
  } else {
    Write-Host "sync는 양방향입니다. 삭제는 전파하지 않고 보고만 하며, 충돌은 양쪽을 보존합니다." -ForegroundColor DarkGray
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
  # sync에서 충돌 사본이 생겼으면 실패는 아니지만 정리를 안내한다.
  if ($verb -eq 'sync' -and "$($r.Out)" -match '충돌 사본\s*:\s*(\d+)건') {
    $confDone = [int]$Matches[1]
    if ($confDone -gt 0) {
      Write-Host ("   [충돌] {0} — 충돌 사본 {1}건 생성(양쪽 보존됨). 정리:  .\dsync resolve -p {0}" -f $p, $confDone) -ForegroundColor Yellow
    }
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
$hint = '.\SYNC.ps1 -DryRun'
if ($verb -eq 'pull') { $hint = '.\SYNC.ps1 -Pull -DryRun' }
if ($verb -eq 'sync') { $hint = '.\SYNC.ps1 -Sync -DryRun' }
Write-Host "확인:  $hint   (남은 게 없으면 '변경 없음')" -ForegroundColor DarkGray
