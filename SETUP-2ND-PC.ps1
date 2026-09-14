# [축소됨 2026-09-14] 남은 기능은 '발견'(discover) 하나뿐이다.
#
# 예전에는 이 스크립트가 프로파일 4개를 하드코딩해 한 번에 init 했다. 그 목록은
# 특정 사용자의 업무 폴더 이름·파일 수·용량이었다. 공개 저장소에 둘 것이 아니고,
# 방식 자체도 2026-08-10 결정으로 이미 폐기됐다 — **동기화 폴더는 설치 때 정하지
# 않는다.** 하드코딩한 목록은 만든 사람의 PC에서만 맞고 동료 PC에서는 틀린다.
#
# 지금의 흐름: 동기화할 폴더에 synchere.bat 을 복사해 더블클릭하면 그 폴더가
# 등록되고 시작된다(tools/sync_here.py 의 유도 사슬 — 다른 PC에서 동기화하던
# 폴더는 원격 마커로 자동 결합해 이어받는다).
#
# 이 파일에 남은 것은 그 유도 사슬이 무엇을 찾게 될지 **미리 눈으로 보는** 도구다.
#
# 사용:  .\SETUP-2ND-PC.ps1
#        .\SETUP-2ND-PC.ps1 -LocalBase 'D:\Dooray'
param(
  [string]$LocalBase = 'C:\Dooray',
  # 예전에는 '발견만' 모드를 고르는 스위치였다. 지금은 그것이 유일한 모드라
  # 아무것도 바꾸지 않는다 — 옛 안내문을 보고 붙이는 사람이 있어 받아만 둔다.
  [switch]$Discover
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# 마커(synchere.bat)를 스캔해 동기화 루트 후보를 보여준다. 마커는 발견 힌트일 뿐
# 자동 등록은 하지 않는다 — 등록 여부는 사용자가 확인 후 결정한다.
# drive_id 는 넘기지 않는다: config 에 등록된 프로파일에서 찾는다(tools/_driveid.py).
Write-Host "== 원격 마커 기반 동기화 루트 발견 ==" -ForegroundColor Cyan
python tools\discover_roots.py --local-base $LocalBase
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "가장 쉬운 길: 쓰려는 폴더에 synchere.bat 을 복사해 더블클릭하세요." -ForegroundColor Green
Write-Host "             등록과 첫 동기화가 그 한 번으로 끝납니다." -ForegroundColor Green
Write-Host ""
Write-Host "dsync init 으로 직접 등록했다면, 그 폴더에 synchere.bat 을 꼭 복사해" -ForegroundColor Green
Write-Host "두세요 - 실행기이자 등록 스위치입니다(없으면 다음 실행이 자동 해제)." -ForegroundColor Green
exit 0
