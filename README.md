# Dooray Drive 로컬 동기화

공공기관용 Dooray(공공 클라우드) Drive를 로컬 PC와 동기화하는 프로그램입니다.
Dooray 공식 데스크톱 동기화 앱은 [공공기관 미지원](https://helpdesk.dooray.com/share/pages/9wWo-xwiR66BO5LGshgVTg/2909486247933660381)이라 자체 개발했습니다.

| 문서 | 내용 |
|---|---|
| [docs/검토보고서.md](docs/검토보고서.md) | 타당성 결론 · API 실측 결과 · 리스크 등록부 |
| [docs/구현계획서.md](docs/구현계획서.md) | 아키텍처 · 마일스톤 로드맵 |
| [docs/모듈규약.md](docs/모듈규약.md) | 모듈 인터페이스 계약 |
| [docs/세션_마일스톤.md](docs/세션_마일스톤.md) | 개발 진행 실적 기록 |

**현재 상태: M1(수동 단방향 push/pull) 구현 완료.** 양방향 동기화·충돌 처리(M2)와 자동 데몬(M3)은 미구현입니다.

## 동기화 도구 사용법 (M1)

```bash
pip install -r requirements.txt
```

토큰 등록(아래 PoC 준비의 3~4단계와 동일) 후:

```bash
python -m dooray_sync.cli.main doctor
```

```bash
python -m dooray_sync.cli.main init --remote-path "업무폴더" --local-root "D:\DoorayDrive"
```

| 명령 | 설명 |
|---|---|
| `init` | 드라이브 선택 → 로컬 루트 지정 → 원격 상태 수집(파일은 받지 않음) |
| `status` | 설정·건수·커서 요약 |
| `push` | 로컬 → 원격. **원격을 삭제·이동하지 않음** |
| `pull` | 원격 → 로컬. **수정된 로컬 파일은 절대 덮어쓰지 않음** |
| `doctor` | 토큰·연결·긴 경로·DB 점검 |

전 명령이 `--dry-run`을 지원합니다. **처음에는 반드시 `--dry-run`으로 계획을 확인하세요.**

안전 관련 동작 두 가지를 알아두시면 좋습니다.

- `init` 직후에는 로컬 기준선이 없어, 원격에도 존재하는 파일의 `push`가 **보류**됩니다(오래된 로컬본이 원격을 덮는 사고 방지). `pull`로 기준선을 만들거나, 로컬이 최신임이 확실하면 `push --assume-local-newer`를 쓰십시오.
- `init`의 원격 순회 비용은 **폴더 수에 비례**합니다(폴더당 약 0.4초). 드라이브 전체를 지정하면 대형 드라이브에서 한 시간을 넘길 수 있으니 `--remote-path`로 필요한 업무 폴더만 지정하세요.
- M1은 **삭제를 전파하지 않습니다.** 어느 쪽에서 파일을 지워도 반대편은 그대로 두고 건수만 보고합니다.

## PoC 실행 준비

1. **Python 3.11+** 설치 확인
2. 의존성 설치:

```bash
pip install -r poc/requirements.txt
```

3. **개인 API 토큰 발급**: Dooray 웹 → 우측 상단 프로필 → **개인설정 > API > 개인 인증 토큰** → 토큰 생성
4. 토큰 등록 (둘 중 하나):

```powershell
# 방법 A: 현재 PowerShell 세션에만 (간단, 세션 종료 시 소멸)
$env:DOORAY_API_TOKEN = '발급받은토큰'
```

```powershell
# 방법 B: Windows 자격 증명 관리자에 저장 (권장, 영구)
python -c "import keyring; keyring.set_password('dooray-sync','api-token','발급받은토큰')"
```

선택 환경변수:
- `DOORAY_BASE_URL` — 기본값 `https://api.gov-dooray.com` (공공 클라우드). 업무망이면 `https://api.gov-dooray.co.kr`
- `DOORAY_DRIVE_ID` — 대상 드라이브 강제 지정 (기본: 개인 드라이브 자동 선택)

## PoC 실행 순서

번호 순서대로 실행합니다. 결과는 `poc/poc_results/*.json` + `*.log`에 기록됩니다.

```bash
cd poc
python poc_01_auth_drives.py
```

| # | 스크립트 | 목적 | 비고 |
|---|---|---|---|
| 01 | `poc_01_auth_drives.py` | 인증·드라이브 목록·**IP ACL 접근성** | 실패 시 이후 진행 불가 — 관리자 협의 |
| 02 | `poc_02_listing.py` | 목록 API·페이지네이션·hash 유무, 샌드박스 생성 | |
| 03 | `poc_03_download.py` | 다운로드 307 흐름·무결성 | |
| 04 | `poc_04_upload.py` | 업로드 307 흐름·한글 파일명·버전 증가 | |
| 05 | `poc_05_changes.py` | **changes API 의미론 (최중요)** | 드라이브 전량 스캔 + 시나리오 폴링으로 **약 7~10분 소요**(changes 쿼리의 서버 응답이 요청당 ~5초로 느림. 드라이브 보유 객체 수에 비례) |
| 06 | `poc_06_largefile.py` | 대용량 왕복 (기본 100MB) | `--kill-test`로 중단 실험 |
| 07 | `poc_07_ratelimit.py --yes` | rate limit 실측 | **업무 시간 외에만 실행** |
| 08 | `poc_08_names.py` | 한글 NFC/NFD·특수문자·예약어·긴 경로 | |

## 안전 수칙 (PoC)

- 모든 쓰기 실험은 개인 드라이브의 **`_poc_sandbox`** 폴더 안에서만 수행됩니다.
- **영구삭제 API는 어떤 스크립트에도 없습니다.** 정리는 전부 휴지통 이동이며, 최종 비우기는 웹 UI에서 수동으로 합니다.
- PoC 완료 후 웹 UI에서 `_poc_sandbox` 폴더와 휴지통을 확인·정리하세요.

## 프로젝트 구조

```
dooraydrive/
├─ README.md              # 이 문서
├─ docs/검토보고서.md      # 타당성 검토 보고서 (PoC 실측 반영)
└─ poc/
   ├─ poc_common.py       # 공통: 토큰, httpx, 수동 307, rate-limit 관측, 결과 기록
   ├─ poc_01 ~ poc_08     # 실측 스크립트
   ├─ poc_results/        # 실행 결과 (JSON/log, git 제외)
   └─ tmp/                # 임시 파일 (git 제외)
```
