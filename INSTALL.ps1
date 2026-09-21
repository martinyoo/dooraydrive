# Dooray Drive 동기화 — 원클릭 설치. 사용자는 `설치.bat`을 더블클릭한다.
#
# 설치는 **프로그램 준비까지만** 한다: 차단 해제(MOTW) · 의존성 설치 · 토큰 등록 ·
# 연결 확인. 동기화 폴더는 여기서 정하지 않는다(2026-08-10 사용자 요구: 특정 폴더를
# 미리 등록하는 방식은 동료 PC에 맞지 않는다). 등록은 사용 시점에 폴더 단위로 한다 —
# 동기화할 폴더에 synchere.bat를 복사해 더블클릭하면 그 폴더가 등록되고 시작된다
# (tools/sync_here.py의 유도 사슬 참조).
#
# 사용 예
#   .\INSTALL.ps1           전체 설치 (물어보며 진행)
#   .\INSTALL.ps1 -Check    이 PC가 준비됐는지 점검만 (아무것도 안 바꿈)
#   .\INSTALL.ps1 -OfferUpdate
#                           설치된 폴더 안에서 실행됐을 때, 설치를 시작하기 전에
#                           갱신할지 먼저 묻는다. 설치.bat만 이 스위치를 붙인다.
param(
  [switch]$Check,
  # 설치.bat이 '프로그램 폴더 안의 사본'이라고 판단했을 때만 붙인다.
  [switch]$OfferUpdate,
  # 롤백 지정(`설치.bat v0.2.0`)이 있었으면 그 이름. 질문에 그대로 보여 준다.
  [string]$UpdateVersion
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$MIN_PY    = [version]'3.11'

# --------------------------------------------------------------------- 출력
function Head($t) { Write-Host ""; Write-Host "== $t ==" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [정상] $t" -ForegroundColor DarkGray }
function Warn($t) { Write-Host "  [주의] $t" -ForegroundColor Yellow }
function Info($t) { Write-Host "  $t" -ForegroundColor DarkGray }
function Die($t, $hint = '') {
  Write-Host ""
  Write-Host "  [중단] $t" -ForegroundColor Red
  if ($hint) { $hint -split "`n" | ForEach-Object { Write-Host "         $_" -ForegroundColor Yellow } }
  Write-Host ""
  Write-Host "  조치 후 '설치.bat'을 다시 더블클릭하시면 됩니다. 이미 끝난 단계는 건너뜁니다." -ForegroundColor DarkGray
  exit 1
}

Write-Host ""
Write-Host "  Dooray Drive 동기화 설치" -ForegroundColor White
if ($Check) { Write-Host "  (점검 모드 — 아무것도 바꾸지 않습니다)" -ForegroundColor Yellow }
Write-Host "  설치 폴더: $PSScriptRoot" -ForegroundColor DarkGray

# --------------------------------------------------------- 0) 갱신할지 먼저 묻는다
# 프로그램 폴더 **안**의 사본을 더블클릭한 경우다(설치.bat이 -OfferUpdate로 알려 준다).
# 예전에는 그 화면이 "갱신하려면 이 파일을 폴더 밖으로 복사해 실행하세요"라고 시키고
# 끝났다. 시키기만 하는 기능은 없는 기능이다(AGENTS.md) — 여기서 번호로 고르게 하고,
# 고른 것은 설치.bat이 그대로 실행한다.
#
# 계약은 종료코드 하나다: **10 = 갱신을 골랐다.** 설치.bat의 :self_update 한 곳만
# 읽는다. 교체를 이 파일이 직접 하지 않는 이유는 단순하다 — 자기가 들어 있는 폴더를
# 자기가 갈아 끼울 수는 없다. 설치.bat이 자신을 %TEMP%로 빼내 거기서 실행한다.
#
# 기본값(엔터·EOF·오입력 3회)은 2다. **2는 "아무것도 안 함"이 아니다** — 이 파일이 지금까지
# 하던 일 그대로다: 빠진 구성요소를 pip으로 채우고, 파이썬이 없으면 받아 설치하고, 토큰이
# 없으면 묻고, DSYNC_HOME을 이 폴더로 등록한다. 그래서 화면에 "점검"이라고 쓰지 않는다 —
# 그 이름은 `-Check`(아무것도 안 바꿈)가 이미 쓰고 있고, 2에 붙이면 거짓말이 된다.
# 안전의 기준은 여기서 **되돌릴 수 없는 것**이다: 폴더 통째 교체는 1에만 있고 기본값은
# 그리로 가지 않는다. 대화형이 아니면(파이프·EOF·죽은 스트림) 묻지 않고 기본값으로 간다 —
# isatty로 판정하지 않는 것과 같은 이유로, 방어선은 기본값과 유한 루프다.
if ($OfferUpdate -and -not $Check) {
  $verLabel = if ($UpdateVersion) { "$UpdateVersion 버전을" } else { '최신 버전을' }
  Write-Host ""
  Write-Host "  이 폴더는 이미 설치된 프로그램입니다. 무엇을 할까요?" -ForegroundColor White
  Write-Host ""
  Write-Host "    1  갱신 — $verLabel 받아 이 폴더를 통째로 바꿉니다" -ForegroundColor White
  Write-Host "    2  이 PC 다시 맞추기 — 프로그램 코드는 그대로 두고, 빠진 구성요소·토큰을" -ForegroundColor White
  Write-Host "       채운 뒤 연결을 확인합니다 (기본값 · 지금까지 이 파일이 하던 일)" -ForegroundColor White
  Write-Host ""
  Write-Host "  갱신하려면 동기화 창을 먼저 모두 닫아 주세요(자동 동기화 창 포함)." -ForegroundColor Yellow
  Write-Host "  동기화가 도는 중에는 폴더를 바꾸지 못하고, 열어 둔 창은 갱신 뒤에" -ForegroundColor DarkGray
  Write-Host "  옛 코드와 새 코드를 섞어 쓰게 됩니다." -ForegroundColor DarkGray
  Write-Host "  설정·토큰·동기화 폴더는 이 폴더 밖에 있어 갱신해도 그대로입니다." -ForegroundColor DarkGray
  Write-Host "  2는 프로그램을 바꾸지 않지만 아무것도 안 하는 것도 아닙니다 — 빠진 것은" -ForegroundColor DarkGray
  Write-Host "  내려받아 채우고, 이 폴더를 실행 대상(DSYNC_HOME)으로 등록합니다." -ForegroundColor DarkGray
  Write-Host ""

  $pick = ''
  for ($i = 0; $i -lt 3 -and -not $pick; $i++) {
    $ans = $null
    try { $ans = Read-Host "  번호 (그냥 엔터 = 2)" } catch { $ans = $null }
    if ($null -eq $ans) { break }                             # EOF·죽은 스트림 — 즉시 포기
    $ans = $ans.Trim()
    if (-not $ans) { break }                                  # 엔터 = 기본값
    if     ($ans -match '^(1|갱신|update|u)$') { $pick = 'update' }
    elseif ($ans -match '^(2|점검|맞추기|check|setup|c|s)$') { $pick = 'check' }
    else   { Warn "1 또는 2를 입력해 주세요." }
  }

  if ($pick -eq 'update') {
    Write-Host ""
    Write-Host "  갱신을 시작합니다. 새 창이 열리고 이 창은 닫힙니다." -ForegroundColor Green
    exit 10
  }
  Write-Host ""
  Info "이 PC 설정만 다시 맞춥니다. 갱신하려면 다시 실행해 1을 고르세요."
}

# --------------------------------------------------------------------- 1) Python
Head "1/5  Python 확인"

function Invoke-Py {
  <# python을 부르고 {Code, Out, Err}를 돌려준다. 스크립트를 죽이지 않는다.

     PowerShell 5.1은 네이티브 exe가 stderr에 한 줄이라도 쓰면 그것을 NativeCommandError로
     감싸고, 이 파일 머리의 $ErrorActionPreference='Stop' 아래에서 **종료 오류**로 만든다.
     `2>$null`로는 막을 수 없다 — 리다이렉션은 출력을 버릴 뿐 오류 레코드 생성은 그대로다.
     실제로 구성요소가 없는 새 PC에서 `python -c "import httpx, ..."`가 ImportError
     트레이스백을 내자 3/6 단계에서 설치가 그대로 중단됐다(2026-08-07 실측).
     그래서 두 스트림을 파일로 받고, 호출 구간에서만 Stop을 푼다. #>
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PyArgs)
  $o = [System.IO.Path]::GetTempFileName()
  $e = [System.IO.Path]::GetTempFileName()
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $code = 9009   # python 자체를 찾지 못한 경우의 기본값
  try {
    & python @PyArgs > $o 2> $e
    $code = $LASTEXITCODE
  } catch {
    # 실행 파일 자체가 없을 때. $code는 9009로 둔다.
  } finally {
    $ErrorActionPreference = $prev
  }
  $res = [pscustomobject]@{
    Code = $code
    Out  = (('' + (Get-Content -LiteralPath $o -Raw -Encoding UTF8 -ErrorAction SilentlyContinue)).Trim())
    Err  = (('' + (Get-Content -LiteralPath $e -Raw -Encoding UTF8 -ErrorAction SilentlyContinue)).Trim())
  }
  Remove-Item -LiteralPath $o, $e -Force -ErrorAction SilentlyContinue
  return $res
}

function Get-WorkingPython {
  <# 쓸 수 있는 python이면 {Version, Path}, 아니면 $null.
     Microsoft Store의 'python' 껍데기는 실행하면 실패하므로 --version까지 확인한다. #>
  $c = Get-Command python -ErrorAction SilentlyContinue
  if (-not $c) { return $null }
  $r = Invoke-Py '--version'
  # --version은 구버전에서 stderr로 나가기도 한다. 두 스트림을 모두 본다.
  $raw = if ($r.Out) { $r.Out } else { $r.Err }
  if ($r.Code -ne 0 -or $raw -notmatch 'Python\s+(\d+\.\d+)') { return $null }
  return [pscustomobject]@{ Version = [version]$Matches[1]; Path = $c.Source }
}

function Update-SessionPath {
  # 방금 설치한 Python을 이 창에서 바로 쓰려면 PATH를 다시 읽어야 한다.
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if ($userPath) { $env:PATH = "$userPath;$env:PATH" }
  $h = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311'
  if (Test-Path (Join-Path $h 'python.exe')) { $env:PATH = "$h;$h\Scripts;$env:PATH" }
}

$py = Get-WorkingPython
if (-not $py) {
  if ($Check) {
    Warn "Python이 없습니다 — 설치 시 python.org에서 자동으로 받아 설치합니다"
  } else {
    $pyVer = '3.11.9'
    $url = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-amd64.exe"
    $exe = Join-Path $env:TEMP "python-$pyVer-amd64.exe"
    Info "Python이 없습니다. python.org에서 $pyVer 을 자동으로 설치합니다 (약 25MB)."
    try {
      [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
      $ProgressPreference = 'SilentlyContinue'
      Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    } catch {
      Die "Python 내려받기에 실패했습니다: $($_.Exception.Message)" @"
인터넷 연결(사내 프록시 포함)을 확인하세요. 직접 설치하셔도 됩니다:
  https://www.python.org/downloads/  ('Add python.exe to PATH' 체크)
"@
    }
    # 프록시 로그인 페이지가 대신 내려오면 파일이 아주 작다
    if ((Get-Item $exe).Length -lt 1MB) {
      Remove-Item $exe -ErrorAction SilentlyContinue
      Die "내려받은 설치 파일이 너무 작습니다." "프록시 로그인 페이지일 수 있습니다. 브라우저로 사내망에 먼저 로그인한 뒤 다시 실행하세요."
    }
    Info "설치하는 중입니다. 1~2분 걸립니다..."
    $proc = Start-Process $exe -Wait -PassThru -ArgumentList @(
      '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_pip=1')
    Remove-Item $exe -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) {
      Die "Python 설치에 실패했습니다 (종료 코드 $($proc.ExitCode))." @"
python.org에서 직접 설치한 뒤 '설치.bat'을 다시 더블클릭해 주세요.
설치 첫 화면의 'Add python.exe to PATH'를 반드시 체크하세요.
"@
    }
    Update-SessionPath
    $py = Get-WorkingPython
    if (-not $py) {
      Die "Python은 설치됐지만 이 창에서 찾지 못합니다." "이 창을 닫고 '설치.bat'을 다시 더블클릭하면 됩니다."
    }
    Ok "Python $($py.Version) 설치 완료"
  }
}
if ($py) {
  if ($py.Version -lt $MIN_PY) {
    Die "Python $($py.Version) — 이 도구는 $MIN_PY 이상이 필요합니다." "python.org에서 최신 버전을 설치한 뒤 다시 실행하세요."
  }
  Ok "Python $($py.Version) — $($py.Path)"
}

# --------------------------------------------------------------------- 2) 차단 해제
# zip으로 받아 탐색기로 풀면 파일에 '인터넷에서 받음' 표시(MOTW)가 붙고, 기본
# 실행 정책(RemoteSigned)이 .ps1 실행을 거부한다. 여기서 미리 풀어 둔다.
Head "2/5  파일 차단 해제"
if ($Check) {
  $blocked = @(Get-ChildItem -Path $PSScriptRoot -Recurse -Include *.ps1, *.cmd, *.bat, *.py -ErrorAction SilentlyContinue |
               Where-Object { Get-Item -LiteralPath $_.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue })
  if ($blocked.Count -gt 0) { Warn "차단된 파일 $($blocked.Count)건 — 설치 시 해제됩니다" } else { Ok "차단된 파일 없음" }
} else {
  try {
    Get-ChildItem -Path $PSScriptRoot -Recurse -Include *.ps1, *.cmd, *.bat, *.py -ErrorAction SilentlyContinue |
      Unblock-File -ErrorAction SilentlyContinue
    Ok "인터넷에서 받은 파일의 차단을 해제했습니다"
  } catch {
    Warn "차단 해제 중 오류(계속 진행): $($_.Exception.Message)"
  }
}

# --------------------------------------------------------------------- 3) 의존성
Head "3/5  필요한 구성요소 설치"
# Python이 없는 채로 여기 오는 것은 점검 모드뿐이다. 그대로 `python`을 부르면
# 명령을 찾지 못해 ErrorActionPreference='Stop'에 걸려 스크립트가 죽는다.
if (-not $py) {
  Warn "Python이 없어 확인을 건너뜁니다 — 설치할 때 함께 처리됩니다"
} elseif ((Invoke-Py '-c' 'import httpx, keyring, typer, send2trash').Code -eq 0) {
  Ok "이미 설치되어 있습니다"
} elseif ($Check) {
  Warn "구성요소가 없습니다 — 설치 시 자동으로 받습니다 (인터넷 필요)"
} else {
  Info "pip로 내려받는 중입니다. 1~2분 걸릴 수 있습니다..."
  # pip은 진행 상황을 보여줘야 하므로 화면에 그대로 흘린다. 다만 경고를 stderr로
  # 내보내므로 이 구간에서만 Stop을 풀어 NativeCommandError로 죽지 않게 한다.
  # PYTHONUTF8=1: pip은 BOM 없는 requirements.txt를 로케일 코덱(cp949)으로 읽어,
  # 한글 주석이 있으면 UnicodeDecodeError로 설치가 통째로 죽는다(2026-08-07 동료 PC
  # 실측). requirements.txt는 ASCII 전용으로 유지하지만, 재유입 대비 이중 방어다.
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $prevUtf8 = $env:PYTHONUTF8
  $env:PYTHONUTF8 = '1'
  try {
    & python -m pip install --disable-pip-version-check -r requirements.txt
    $pipCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prevEAP
    $env:PYTHONUTF8 = $prevUtf8
  }
  if ($pipCode -ne 0) {
    Die "구성요소 설치에 실패했습니다." @"
인터넷 연결(사내 프록시 포함)을 확인하고 다시 실행하세요.
수동으로는:  python -m pip install -r requirements.txt
"@
  }
  $verify = Invoke-Py '-c' 'import httpx, keyring, typer, send2trash'
  if ($verify.Code -ne 0) {
    Die "설치는 끝났지만 구성요소를 불러오지 못합니다." @"
Python이 여러 개 설치돼 있을 수 있습니다.
불러오기 오류:
$($verify.Err)
"@
  }
  Ok "설치 완료"
}

# --------------------------------------------------------------------- 4) 토큰
Head "4/5  Dooray API 토큰"
$tokenState = 'N'
if ($py -and (Invoke-Py '-c' 'import keyring').Code -eq 0) {
  $tokenState = (Invoke-Py '-c' "import keyring;t=keyring.get_password('dooray-sync','api-token');print('Y' if t else 'N')").Out
}
if (-not $py) {
  Warn "Python이 없어 확인을 건너뜁니다 — 설치할 때 입력받습니다"
} elseif ($tokenState -eq 'Y') {
  Ok "이미 등록되어 있습니다 (다시 입력하지 않아도 됩니다)"
} elseif ($Check) {
  Warn "토큰이 등록되지 않았습니다 — 설치 중에 입력받습니다"
} else {
  Write-Host ""
  Write-Host "  Dooray 웹에서 토큰을 발급받아 주세요:" -ForegroundColor Yellow
  Write-Host "    우측 상단 프로필 > 개인설정 > API > 개인 인증 토큰 > 생성 > 복사" -ForegroundColor DarkGray
  Write-Host ""
  Write-Host "  아래에 붙여넣으세요. 보안을 위해 화면에는 표시되지 않습니다." -ForegroundColor Yellow
  Write-Host "  (붙여넣기: 마우스 오른쪽 클릭)" -ForegroundColor DarkGray
  $sec = Read-Host "  토큰" -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
  if (-not $plain -or $plain.Trim().Length -lt 10) { Die "토큰이 비어 있거나 너무 짧습니다." "복사가 제대로 됐는지 확인하고 다시 실행하세요." }

  # 명령줄 인자로 넘기면 작업 관리자·프로세스 목록에 토큰이 노출된다. stdin으로 넘긴다.
  $plain | python -c "import sys,keyring;keyring.set_password('dooray-sync','api-token',sys.stdin.read().strip())"
  $rc = $LASTEXITCODE
  $plain = $null
  if ($rc -ne 0) { Die "토큰 저장에 실패했습니다." "Windows 자격 증명 관리자에 접근하지 못했습니다." }

  $tokenState = (Invoke-Py '-c' "import keyring;t=keyring.get_password('dooray-sync','api-token');print('Y' if t else 'N')").Out
  if ($tokenState -ne 'Y') { Die "토큰이 저장되지 않았습니다." "다시 실행해 주세요." }
  Ok "등록 완료 (Windows 자격 증명 관리자에 저장 — 설정 파일에는 남지 않습니다)"
}

# --------------------------------------------------------------------- 5) 연결
Head "5/5  연결 확인"
# 실제 API 왕복으로 토큰·프록시·TLS(사내 SSL 검사망 포함)·IP ACL을 한 번에 검증한다.
# 예전 6단계(폴더 지정·프로파일 생성·내려받기)는 폐지 — 동기화 폴더는 설치 때 정하지
# 않고, 사용할 폴더에 synchere.bat를 복사해 실행하는 시점에 등록된다(파일 머리 주석).
$connCmd = "import sys;sys.path.insert(0,'.');from dooray_sync.api.client import DoorayClient;from dooray_sync.api.drive import DriveAPI;from dooray_sync.auth import get_token;from dooray_sync.config import Profile;c=DoorayClient(Profile().base_url,get_token());ds=DriveAPI(c).list_drives();c.close();print(chr(10).join((d.get('name') or '?') for d in ds))"
if (-not $py) {
  Warn "Python이 없어 건너뜁니다 — 설치할 때 함께 확인합니다"
} elseif ($Check -and $tokenState -ne 'Y') {
  Warn "토큰이 없어 건너뜁니다 — 설치 중 토큰 등록 뒤 확인합니다"
} else {
  $r = Invoke-Py '-c' $connCmd
  if ($r.Code -eq 0 -and $r.Out) {
    $names = @($r.Out -split "`r?`n" | Where-Object { $_ })
    Ok ("Dooray 연결 정상 — 접근 가능한 드라이브: {0}" -f ($names -join ', '))
  } elseif ($r.Code -eq 0) {
    $msg = "연결은 되지만 접근 가능한 드라이브가 없습니다."
    if ($Check) { Warn $msg } else { Die $msg @"
토큰 권한과 IP ACL 설정을 확인하세요 (Dooray 웹 > 개인설정 > API).
"@ }
  } else {
    # 트레이스백의 마지막 줄만 — 원인은 대개 거기에 있다(ConnectError 등).
    $last = @($r.Err -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 1)
    $msg = "Dooray에 연결하지 못했습니다: $last"
    if ($Check) { Warn $msg } else { Die $msg @"
네트워크(사내 프록시 포함)와 토큰을 확인한 뒤 다시 실행하세요.
진단:  .\dsync doctor
"@ }
  }
}

if ($Check) {
  Head "점검 완료"
  Write-Host "  위 항목에 [중단]이 없으면 설치를 진행할 수 있습니다." -ForegroundColor Green
  Write-Host "  설치하려면 '설치.bat'을 더블클릭하세요." -ForegroundColor DarkGray
  exit 0
}

# ------------------------------------------------------- 프로그램 위치 기록
# synchere.bat은 프로그램 폴더를 스스로 찾아야 한다(복사본은 어디에나 놓인다).
# 드라이브를 훑는 폴백이 있지만, 설치 폴더가 <드라이브>:\dooraydrive 형태가
# 아니면 그 폴백도 못 찾는다. 설치 위치를 사용자가 정하게 만든 이상 여기서
# 정확한 경로를 남겨 두는 것이 유일하게 확실한 방법이다.
#
# **소스 체크아웃이면 먼저 묻는다.** `설치.bat`은 `.git`을 보면 "여기를 등록하면 이
# 체크아웃이 매일 도는 프로그램이 된다"고 경고하고는 **그대로 등록했다.** 경고만 하고
# 그대로 하는 것은 사람에게 멈출 기회를 주지 않는 것이고, 이 저장소가 「화면이 명령을
# 시키면 그 기능은 없는 것이다」로 금지한 모양의 사촌이다.
# 2026-09-21 실측: 한 PC의 `DSYNC_HOME`이 개발 체크아웃을 가리키고 있었다. 결과는 셋이다 —
# `git pull` 한 번이 곧 그날의 실행 코드 교체이고, 새 버전 알림이 **구조적으로 영원히
# 침묵하며**(체크아웃의 `__version__`은 main과 같거나 앞선다), 그 PC에는 갱신·롤백 수단이
# 아예 없다(`설치.bat`이 `.git` 때문에 갱신 질문을 띄우지 않는다).
# `.bat`이 넘기는 플래그가 아니라 여기서 `.git`을 직접 본다 — `.\INSTALL.ps1`을 손으로
# 실행한 경우에도 같은 질문이 떠야 한다.
$homeCurrent  = [Environment]::GetEnvironmentVariable('DSYNC_HOME', 'User')
$registerHome = $true
if (Test-Path (Join-Path $PSScriptRoot '.git')) {
  Write-Host ""
  Write-Host "  이 폴더는 소스 체크아웃입니다(.git 있음)." -ForegroundColor Yellow
  Write-Host "  여기를 '매일 도는 프로그램'으로 등록하면 개발 중인 코드가 실제 파일을" -ForegroundColor Yellow
  Write-Host "  동기화합니다. 그리고 그 PC에서는 새 버전 알림이 영원히 뜨지 않습니다" -ForegroundColor DarkGray
  Write-Host "  (체크아웃은 main과 같거나 앞서므로 '더 높은 원격'이 성립하지 않습니다)." -ForegroundColor DarkGray
  if ($homeCurrent) {
    Write-Host "  지금 등록된 곳: $homeCurrent" -ForegroundColor DarkGray
  } else {
    Write-Host "  지금은 등록된 곳이 없습니다." -ForegroundColor DarkGray
  }
  Write-Host ""
  Write-Host "    1  등록한다 — 이 체크아웃을 실행 대상으로 삼습니다" -ForegroundColor White
  Write-Host "    2  등록하지 않는다 (기본값 · 지금 등록된 곳을 그대로 둡니다)" -ForegroundColor White
  Write-Host ""

  # 기본값은 안전한 쪽(바꾸지 않음)이다. 대화형이 아니면 묻지 않고 그리로 간다.
  $registerHome = $false
  for ($i = 0; $i -lt 3 -and -not $registerHome; $i++) {
    $ans = $null
    try { $ans = Read-Host "  번호 (그냥 엔터 = 2)" } catch { $ans = $null }
    if ($null -eq $ans) { break }                             # EOF·죽은 스트림
    $ans = $ans.Trim()
    if (-not $ans) { break }                                  # 엔터 = 기본값
    if     ($ans -match '^(1|등록|yes|y)$')    { $registerHome = $true }
    elseif ($ans -match '^(2|아니오|no|n)$')   { break }
    else   { Warn "1 또는 2를 입력해 주세요." }
  }
}

if (-not $registerHome) {
  Write-Host ""
  if ($homeCurrent) {
    Info "DSYNC_HOME을 바꾸지 않았습니다 — synchere.bat은 계속 '$homeCurrent'를 씁니다."
  } else {
    Warn "DSYNC_HOME을 등록하지 않았습니다 — synchere.bat이 각 드라이브의 \dooraydrive 를 훑어 찾습니다."
  }
} else {
  try {
    [Environment]::SetEnvironmentVariable('DSYNC_HOME', $PSScriptRoot, 'User')
    $env:DSYNC_HOME = $PSScriptRoot
    Ok "프로그램 위치 등록: DSYNC_HOME = $PSScriptRoot"
    Info "(synchere.bat이 이 폴더를 찾는 데 씁니다. 이미 열려 있는 창에는 다음 로그인 후 적용됩니다)"
  } catch {
    Warn "DSYNC_HOME 등록 실패(계속 진행): $($_.Exception.Message)"
    Info "synchere.bat은 각 드라이브의 \dooraydrive 를 훑어 찾습니다"
  }
}

# --------------------------------------------------------------------- 완료
Head "설치 완료"
Write-Host "  동기화는 폴더 단위로 시작합니다. 대상 폴더를 미리 정할 필요가 없습니다:" -ForegroundColor White
Write-Host ""
Write-Host "    1. 동기화할 폴더에 이 파일을 복사합니다:" -ForegroundColor White
Write-Host ("         {0}" -f (Join-Path $PSScriptRoot 'synchere.bat')) -ForegroundColor Yellow
Write-Host "    2. 복사한 synchere.bat 를 더블클릭합니다." -ForegroundColor White
Write-Host "       → 그 폴더가 동기화 대상으로 등록되고 첫 동기화가 시작됩니다." -ForegroundColor DarkGray
Write-Host "       → 다른 PC에서 이미 동기화하던 같은 이름의 폴더면 그 내용을 이어받습니다." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  이후에도 같은 파일을 더블클릭하면 그 폴더가 동기화됩니다." -ForegroundColor DarkGray
Write-Host "  등록 해제: 폴더에서 synchere.bat 를 지우면 다음 실행 때 해제됩니다." -ForegroundColor DarkGray
Write-Host "  여러 폴더도 같은 방법입니다 — 폴더마다 복사해 실행하면 됩니다." -ForegroundColor DarkGray
exit 0
