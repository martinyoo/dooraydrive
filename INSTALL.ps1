# Dooray Drive 동기화 — 원클릭 설치. 사용자는 `설치.bat`을 더블클릭한다.
#
# 이 스크립트는 SETUP-2ND-PC.ps1을 **대체하지 않는다.** 그 앞뒤의 빈틈만 메운다:
#   차단 해제(MOTW) · 의존성 설치 · 토큰 등록 · 폴더 위치 입력 · 내려받기
# 프로파일 생성 자체는 이미 검증된 SETUP-2ND-PC.ps1에 그대로 맡긴다 — 새로 검증해야
# 할 로직을 늘리지 않기 위해서다.
#
# 사용 예
#   .\INSTALL.ps1                          전체 설치 (물어보며 진행)
#   .\INSTALL.ps1 -Check                   이 PC가 준비됐는지 점검만 (아무것도 안 바꿈)
#   .\INSTALL.ps1 -LocalBase "D:\Dooray"   폴더를 미리 지정
#   .\INSTALL.ps1 -NoPull                  설정까지만, 내려받기는 나중에
param(
  [string]$LocalBase = '',
  [switch]$NoPull,
  [switch]$Check
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$NEED_GB   = 11.5    # 원격 실측 11.24GB + 여유
$MIN_PY    = [version]'3.11'
$SMALLEST  = 'swstat'  # 가장 작은 프로파일 — 이것부터 받아 경로·권한을 먼저 검증한다

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

# --------------------------------------------------------------------- 1) Python
Head "1/6  Python 확인"

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
Head "2/6  파일 차단 해제"
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
Head "3/6  필요한 구성요소 설치"
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
Head "4/6  Dooray API 토큰"
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

# --------------------------------------------------------------------- 5) 폴더
Head "5/6  파일을 둘 폴더"
if (-not $LocalBase) {
  if ($Check) {
    $LocalBase = 'C:\Dooray'
  } else {
    Write-Host "  동기화한 파일을 저장할 폴더입니다. 약 12GB가 필요합니다." -ForegroundColor DarkGray
    Write-Host "  그냥 엔터를 누르면 C:\Dooray 를 씁니다." -ForegroundColor DarkGray
    $answer = (Read-Host "  폴더 경로").Trim().Trim('"')
    if ($answer) { $LocalBase = $answer } else { $LocalBase = 'C:\Dooray' }
  }
}
if (-not [System.IO.Path]::IsPathRooted($LocalBase)) { Die "폴더는 절대경로여야 합니다: $LocalBase" "예:  D:\Dooray" }
$qualifier = Split-Path -Qualifier $LocalBase
if ($qualifier) {
  $drive = Get-PSDrive ($qualifier -replace ':', '') -ErrorAction SilentlyContinue
  if (-not $drive) { Die "드라이브를 찾을 수 없습니다: $qualifier" }
  $freeGB = [math]::Round($drive.Free / 1GB, 1)
  if ($freeGB -lt $NEED_GB) { Die "$qualifier 여유 공간 ${freeGB}GB — 최소 ${NEED_GB}GB가 필요합니다." "다른 드라이브를 지정하거나 공간을 확보하세요." }
  Ok "$LocalBase  ($qualifier 여유 ${freeGB}GB)"
}

if ($Check) {
  Head "점검 완료"
  Write-Host "  위 항목에 [중단]이 없으면 설치를 진행할 수 있습니다." -ForegroundColor Green
  Write-Host "  설치하려면 '설치.bat'을 더블클릭하세요." -ForegroundColor DarkGray
  exit 0
}

# --------------------------------------------------------------------- 6) 프로파일
Head "6/6  동기화 폴더 설정"
Info "검증된 SETUP-2ND-PC.ps1을 실행합니다. 원격 목록을 읽는 동안 몇 분 걸립니다."
Write-Host ""
# 별도 프로세스로 부른다 — 그 스크립트의 exit 코드를 이 스크립트와 섞지 않기 위해서다.
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'SETUP-2ND-PC.ps1') -LocalBase $LocalBase
if ($LASTEXITCODE -ne 0) {
  Die "동기화 폴더 설정에 실패했습니다 (위 메시지 참고)." @"
토큰이 유효한지, 원격 폴더 이름이 맞는지 확인하세요.
진단:  .\dsync doctor
"@
}

# --------------------------------------------------------------------- 내려받기
if ($NoPull) {
  Head "설정 완료"
  Write-Host "  내려받기는 건너뛰었습니다. 나중에 받으려면:" -ForegroundColor Green
  Write-Host "    .\SYNC.ps1 -Pull" -ForegroundColor White
  exit 0
}

$profiles = @()
$r = Invoke-Py '-c' "import sys,tomllib;sys.path.insert(0,'.');from dooray_sync.config import config_path;d=tomllib.load(open(config_path(),'rb'));print(chr(10).join(d.get('profile',{}).keys()))"
if ($r.Code -eq 0 -and $r.Out) { $profiles = @($r.Out -split "`r?`n" | Where-Object { $_ }) }
if ($profiles.Count -eq 0) { Die "설정에서 프로파일을 읽지 못했습니다." "설정은 끝났을 수 있습니다. '.\SYNC.ps1 -Pull'로 직접 받아 보세요." }

# 작은 것부터 받는다 — 경로·권한 문제는 13개 파일에서 드러나는 편이 낫다.
$ordered = @($profiles | Where-Object { $_ -eq $SMALLEST }) + @($profiles | Where-Object { $_ -ne $SMALLEST })

Head "파일 받기 (1/2) — 작은 폴더로 먼저 확인"
$firstProfile = $ordered[0]
Info "프로파일 '$firstProfile' 을 받습니다."
Write-Host ""
python -m dooray_sync.cli.main pull -p $firstProfile
if ($LASTEXITCODE -ne 0) {
  Die "'$firstProfile' 내려받기에 실패했습니다." @"
나머지는 받지 않았습니다. 파일 단위로 격리되므로 이미 받은 것은 남아 있습니다.
다시 시도:  .\dsync pull -p $firstProfile
진단:      .\dsync doctor -p $firstProfile
"@
}
Ok "'$firstProfile' 완료 — 경로·권한 정상"

$restProfiles = @($ordered | Select-Object -Skip 1)
if ($restProfiles.Count -eq 0) {
  Head "설치 완료"
} else {
  Head "파일 받기 (2/2) — 나머지 $($restProfiles.Count)개"
  Write-Host "  나머지: $($restProfiles -join ', ')  (약 11GB, 수십 분 걸릴 수 있습니다)" -ForegroundColor Yellow
  Write-Host "  중단해도 안전합니다 — 다시 실행하면 받은 것은 건너뛰고 이어받습니다." -ForegroundColor DarkGray
  $go = Read-Host "  지금 받을까요? (Y/n)"
  if ($go -match '^[Nn]') {
    Head "설정 완료 (내려받기는 나중에)"
    Write-Host "  나중에 받으려면:  .\SYNC.ps1 -Pull" -ForegroundColor White
    exit 0
  }
  $failed = @()
  foreach ($p in $restProfiles) {
    Write-Host ""
    Write-Host "-- $p" -ForegroundColor Cyan
    python -m dooray_sync.cli.main pull -p $p
    if ($LASTEXITCODE -ne 0) { $failed += $p }
  }
  if ($failed.Count -gt 0) {
    Head "일부 실패"
    Write-Host "  실패한 프로파일: $($failed -join ', ')" -ForegroundColor Red
    Write-Host "  파일 단위로 격리되므로 나머지는 받아졌습니다." -ForegroundColor Yellow
    Write-Host "  다시 실행하면 실패한 것만 재시도합니다:  .\SYNC.ps1 -Pull" -ForegroundColor Yellow
    exit 1
  }
  Head "설치 완료"
}

Write-Host "  파일 위치: $LocalBase" -ForegroundColor Green
Write-Host ""
Write-Host "  앞으로는 이 순서만 기억하시면 됩니다:" -ForegroundColor White
Write-Host "    일 시작 전   .\SYNC.ps1 -Pull    (다른 PC에서 올린 최신본 받기)" -ForegroundColor DarkGray
Write-Host "    일 끝난 뒤   .\SYNC.ps1          (내가 고친 것 올리기)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  두 명령 모두 계획을 먼저 보여주고 확인받은 뒤 실행합니다." -ForegroundColor DarkGray
exit 0
