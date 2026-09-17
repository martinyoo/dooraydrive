# AGENTS.md

이 저장소에서 일할 때 따라야 하는 규약입니다. **이 파일이 정본입니다** — `CLAUDE.md`는
이 파일을 import 하는 한 줄뿐입니다. 규약을 고칠 때는 여기만 고치고, 다른 파일에 복사하지
마십시오(2026-09-15에 두 벌을 두었다가 **하루 만에 갈라졌습니다** — 한쪽이 "태그가 하나도
없다"·"C 미구현"·"롤백 수단이 없다"고 말하는 동안 셋 다 이미 사실이 아니었습니다).

## Knowledge Base — AgentOps vault

이 프로젝트의 **결정·교훈·배포 전략은 저장소가 아니라 AgentOps vault에 있습니다.**
코드와 코드에 붙는 설명은 이 저장소(`docs/`)에, 프로젝트를 건너 재사용되는 지식은 vault에 둡니다.

| 항목 | 값 |
|---|---|
| 로컬 경로 | **PC마다 다릅니다** — 아래 §개발 PC 구성 표를 보십시오 |
| 원격 | `https://github.com/martinyoo/AgentOps` — **private** |
| 이 프로젝트 관련 문서 | `AI-Sessions/wiki/` 아래, `project: AgentOps` 또는 `shared` |
| 명령 키워드 | `save`(저장) · `ingest`(raw 가공) · `query`(조회) · `lint`(점검) |
| 규약 정본 | vault의 `AGENTS.md` — 저장 전에 그것을 먼저 읽습니다(vault의 `CLAUDE.md`도 그 파일을 import 하는 한 줄뿐입니다) |

**원격/웹 세션 주의**: vault는 private이라 `https://github.com/martinyoo/AgentOps/blob/...`을 WebFetch하면 404입니다. 도달 경로는 둘뿐입니다 — (a) `"AgentOps 저장소도 이 세션에 추가해줘"` 후 상대경로로 열기, (b) GitHub MCP `get_file_contents(owner=martinyoo, repo=AgentOps, path=AI-Sessions/wiki/...)`.

### 지금 이 저장소에서 반드시 알아야 할 vault 문서

- `wiki/dev-tasks/dooraydrive-deployment-strategy.md` — **배포·갱신 전략(구현 보류)**
- `wiki/errors/AgentOps-dooraydrive-agent-lessons.md` — 이 도구를 만들며 실제로 낸 사고 17건
- `wiki/errors/windows-cp949-encoding-failures.md` — `shared`. 배포 파일은 ASCII 전용
- `wiki/errors/tls-inspection-python-certifi.md` — `shared`. SSL 검사망에서 Python만 죽는다
- `wiki/errors/hardcoded-path-and-its-consumers.md` — `shared`. 설정 가능하게 만든 값에는 소비자가 있다

vault에 저장할 때는 소스코드를 복사하지 않습니다. **결정·개념·에러·맥락**만 옮깁니다.

## 배포 문서를 쓸 때 — 반드시 먼저 볼 것

**개발이 끝나 배포 문서(사용자 매뉴얼)를 작성하는 단계에 들어가면, 먼저 vault의
`wiki/dev-tasks/dooraydrive-deployment-strategy.md`를 읽고 그 전략을 구현·반영합니다.**

그 문서가 지금 잡아 둔 것(요지만, 상세는 vault에):

- 설정(`%APPDATA%`)·상태 DB(`%LOCALAPPDATA%`)·토큰(자격 증명 관리자)이 전부 프로그램 폴더
  밖이므로 **프로그램 폴더는 소모품이다. 폴더 통째 교체가 유일하게 안전한 갱신 단위다.**
  파일 단위 복사는 금지 — 2026-08-10 실증: `main.py`만 갱신하면 `remote.py`의
  `DEFAULT_PROBE_BUDGET`이 없어 ImportError로 기동조차 못 한다.
- `docs/설치안내.md`의 "프로그램 업데이트"(`git pull`)는 **틀렸다.** `설치.bat`은 zip으로 받아
  `.git`이 없다. 권장 경로로 설치한 사람에게만 안 통한다. **매뉴얼을 쓸 때 이 절을 다시 써야 한다.**

**구현 현황 (A~D)**

- **B. `설치.bat` 갱신 모드 — 완료** (`fca71ed`, 2026-08-11). 기존 설치를 재사용하지 않고
  최신본으로 교체한다. 받기·풀기가 성공한 뒤에만 옛 사본을 `.old`로 밀어내고 바꿔치기하며,
  받기 실패 시에는 기존 사본으로 진행하되 `OUT OF DATE`를 명시한다.
  **2026-09-16 — 프로그램 폴더 *안*의 사본으로도 갱신된다.** 그 전까지 in-repo 모드는
  "갱신하려면 이 파일을 폴더 밖으로 복사해 실행하세요"를 화면에 적고 끝났다(아래 §화면이
  명령을 시키면 — 그 규칙의 세 번째 사례였다). 이제 `INSTALL.ps1 -OfferUpdate`가 한국어로
  1(갱신)/2(이 PC 다시 맞추기, 기본값)를 묻고, 1이면 **종료코드 10**으로 알린다.
  2를 "점검"이라 부르지 않는 것은 의도다 — 그 이름은 `-Check`(아무것도 안 바꿈)가 쓰고
  있고, 2는 실제로 pip·토큰·`DSYNC_HOME`을 건드린다. 여기서 안전의 기준은 **되돌릴 수
  없는 것**이고, 그것은 폴더 교체 하나뿐이라 1에만 둔다. `설치.bat :self_update`가
  그 하나만 읽어 자신을 `%TEMP%\dooraydrive-update\dsync-update-<RANDOM>.bat`으로 빼내고
  `start`로 넘긴 뒤 **즉시 끝난다** — 자기가 든 폴더를 자기가 갈아 끼울 수는 없고,
  현재 디렉터리가 그 안이면 rename 자체가 막힌다. 빠져나온 사본은 받기·풀기를 먼저 하므로
  교체 시점에는 원래 프로세스가 이미 없다. 폴더 밖 사본 경로는 그대로 남는다(프로그램이
  아예 안 뜰 때의 길).
- **A. 버전 정체성 — 완료** (2026-09-14~15). `__version__`이 `dsync --version`·`doctor`·
  `--report-json`·자동 루프 헤더·로그 첫 줄에 표시되는 것에 더해, 남아 있던 둘이 끝났다:
  (a) **`v0.2.0` 태그**를 달았다 — zip URL을 `refs/tags/<version>.zip`으로 지목할 수 있다.
  (b) **설치 스탬프** `INSTALLED.txt`(버전·URL·시각·경로)를 `설치.bat`이 교체 직후 쓴다 —
  실행조차 안 되는 PC도 버전을 말할 수 있다.
- **C. `synchere.bat` 새 버전 알림 — 완료** (2026-09-15). `tools/sync_here.py`의
  `_fetch_remote_version`/`_print_version_notice`. 성공 경로 끝에서 main의
  `__version__`을 raw.githubusercontent로 받아 **원격이 더 높을 때만** 한 줄 알린다
  (자동 갱신 없음). 주의할 점 셋이 코드 주석에 근거와 함께 박혀 있다 — truststore는
  `api/client.py` import로만 주입되는데 이 프로세스는 그것을 import하지 않는다 /
  `DoorayClient` 재사용은 Dooray 토큰을 GitHub로 보낸다 / 예외는
  `except (Exception, KeyboardInterrupt)`로 삼켜야 종료코드가 안전하다.
- **D. DB `PRAGMA user_version` — 완료** (M3 단위 2). `store/db.py:43`에 `SCHEMA_VERSION`,
  `db.py:187~210`에 승격·검증. 새 스키마 DB를 옛 코드가 열면 거부한다
  (`tests/test_schema_version.py`).
- **롤백 — v0.2.0부터 가능하다.** `설치.bat <버전>`(또는 `DSYNC_VERSION`)이
  `refs/tags/<버전>.zip`을 받고, 압축 안 폴더 이름을 가정하지 않는다(태그는
  `dooraydrive-0.2.0`, main은 `dooraydrive-main`). 버전을 지정했는데 받기에 실패하면
  기존 사본으로 넘어가지 않는다. 태그는 지금 `v0.2.0`·`v0.3.0` 둘이다.
  **남은 한계 둘**: v0.2.0 **이전**으로는 되돌릴 수 없고
  (태그가 없다), 스키마를 바꾼 릴리스는 어느 방법으로도 롤백되지 않는다 — D는 조용한
  파손을 명시적 거부로 바꿨을 뿐 마이그레이션을 주지 않는다.
- **M3가 B에 새 위험을 얹었다(미검증, 분석 수준).** 자동 루프가 도입되면서 프로그램
  폴더를 `sys.path`에 얹은 파이썬 프로세스가 **하루 종일 살아 있다.** `설치.bat`이
  교체에 성공하면 그 프로세스의 `sys.path` 문자열은 이제 **새 폴더**를 가리키므로,
  교체 이후에 일어나는 지연 임포트는 옛 코드가 도는 프로세스에 새 코드를 섞어 넣는다
  — 원칙 1이 금지한 "파일 단위 혼합"이 프로세스 안에서 재현되는 형태다.
  전략 문서 §4 B의 "미구현: 동기화 실행 중 갱신 방지"가 이제 예외가 아니라 **평시**다.
  처방 (a)는 들어갔다 — 갱신 질문 화면이 "동기화 창을 먼저 닫으라"고 말하고,
  `docs/설치안내.md`의 §프로그램 업데이트가 같은 것을 절로 둔다. **(b)·(c)는 여전히 없고,
  위험 자체도 여전히 미실측이다.**

## 화면이 명령을 시키면 그 기능은 없는 것이다

**사용자에게 "이 명령을 실행하세요"라고 적어 주고 끝내지 않습니다. 그 자리에서 번호로
고르게 하고, 고른 것을 그대로 실행합니다.**

이 저장소가 반복해 밟은 자리입니다. 2026-09-04에 `sync`가 보류 항목을 "다른 명령을
실행하세요"로 끝내던 것을 고쳐(`b41ef01`) 대량 삭제·기준선·충돌을 동기화 창 안에서 번호로
끝내게 했는데, 2026-09-15에 **같은 패턴이 `reconcile` 구석에 그대로 남아 있는 것이 사용자
지적으로 발견**됐습니다. vault의 파생 원칙이 이것을 한 줄로 적어 두었습니다 —
*CLI 인자를 안내해야 하는 기능은 없는 기능이다*.

2026-09-18에 **세 번째로 같은 자리를 밟았습니다.** `resolve`는 번호로 묻기는 했지만
**한 건씩만** 물었고 중간에 나갈 길이 없었습니다. 사용자가 충돌 3,869건에 `2`를 2,736번
누르다 중단했고, 남은 길은 창을 닫고 `--keep local`을 직접 치는 것뿐이었습니다.
`tools/sync_here.py:523`의 주석이 *"resolve는 충돌 1건마다 프롬프트를 띄우고 중도 이탈
수단이 없다"*고 이미 적어 두고 있었습니다 — **알면서 남겨 둔 구멍이 실사용을 막았습니다.**
반복 작업을 번호로 만들 때는 **끝까지 가는 길**과 **그만두는 길**을 함께 냅니다.

### 지키는 방법

- 번호는 `_KEEP_BY_NUM`(`1`/`2`/`3`)과 `_prompt_keep`의 계약을 따릅니다 — **이름도 함께 받고**,
  잘못 입력하면 그 건을 건너뛰지 않고 되묻되 **유한 루프**이며, EOF·Ctrl+C·죽은 스트림은
  즉시 포기합니다.
- **N건을 도는 프롬프트에는 `0) 그만`과 진행 표시(`(3/3869)`)를 답니다.** 끝이 안 보이고
  나갈 길이 없으면 사람은 창을 닫습니다 — 그러면 그 기능은 없는 것과 같습니다.
  '그만'은 `_KEEP_BY_NUM`에 넣지 않습니다(그 표는 `--keep` 검증과 공유하므로 `--keep 0`이
  조용히 통과합니다). `_prompt_keep(allow_quit=True)`로만 엽니다.
- **기본값은 언제나 안전한 쪽**(아무것도 하지 않음)입니다.
- `--dry-run`·`--unattended`에서는 묻지 않습니다.
- 대화형 여부를 **`isatty`로 판정하지 않습니다** — Windows의 `NUL`은 문자 장치라
  `stdin=DEVNULL`에서도 `True`입니다(2026-08-21 실측). 방어선은 기본값과 유한 루프입니다.
- 안내·선택 경로의 어떤 실패도 **종료코드를 바꾸지 않습니다**(`synchere.bat`이 0/2 외의 값을
  30회 재시도 루프로 돌립니다). 예외는 `except (Exception, KeyboardInterrupt)`로 삼킵니다.

### 넘지 말아야 할 선

**위험한 전역 스위치를 번호로 만들지 않습니다.** `--assume-local-newer`가 그 예입니다 —
보류 **전체**를 덮어쓰는 스위치라 `SYNC.ps1`이 화면에서 따로 "쓰지 마세요"라고 경고하고,
`docs/에이전트_운영교훈.md`는 *도구가 위험한 지름길을 화면에서 직접 권할 수 있다*고 적어
두었습니다. 그것을 한 번 누르면 실행되는 번호로 바꾸면 **지금보다 나빠집니다.**

번호로 제공해도 되는 것은 **영향 범위가 그 항목 하나로 닫히고, 되돌릴 수 있는** 선택뿐입니다.
파일 단위로 좁힐 수 없다면 번호를 만들지 말고, **좁힐 수 있게 먼저 고치십시오.**

### 예외로 허용한 것 하나 — "남은 전부 같게"

`resolve`의 `_ask_apply_rest`는 위 선을 넘는 것처럼 보이지만 아닙니다. 지키는 조건이
넷이고, 넷 다 만족할 때만 이런 것을 만듭니다.

1. **번호 목록에 넣지 않습니다.** `4) 전부`였다면 오타 하나가 3,869건을 움직입니다.
   1건을 **실제로 처리해 결과를 보여 준 뒤**, 별도 질문으로 한 번만 묻습니다.
2. **무엇을 몇 건에 적용하는지 화면에 적습니다.** 건수와 선택지를 밝히지 않으면 동의가
   아닙니다.
3. **기본값이 '아니오'입니다.** 빈 입력·EOF·Ctrl+C·죽은 스트림 전부 '아니오'로 떨어집니다.
4. **되돌릴 수 있습니다.** 어느 선택지도 영구 삭제하지 않습니다(로컬·원격 모두 휴지통).
   `--assume-local-newer`가 금지된 진짜 이유가 이것이고, 여기서는 해당하지 않습니다.

`tests/test_pending_offers.py`의 「1-b·1-c」가 넷을 전부 검사합니다. 지우지 마십시오 —
이 예외는 조건이 무너지면 그대로 위험해집니다.

## 작업 디렉터리 밖에 쓰기·삭제 명령을 실행하지 않는다

실제로 사고가 있었습니다. 어시스턴트 실행 환경과 사용자 PC의 파일시스템 경계가 **경로마다
다르게** 동작했습니다 — `%APPDATA%`는 분리되어 있어 어시스턴트가 만든 설정이 사용자 PC에
반영되지 않았고, 반대로 `%LOCALAPPDATA%`는 공유되어 있어 어시스턴트의 `rm -rf`가
**사용자의 상태 DB를 실제로 삭제**했습니다. 그 결과 원인 진단이 세 차례 빗나갔습니다.

- 설정(`%APPDATA%\dooray-sync`)·상태 DB(`%LOCALAPPDATA%\dooray-sync`)·자격 증명은
  **직접 만들거나 지우지 않습니다.** 사용자가 실행할 명령을 안내합니다.
- 재현·실험이 필요하면 `DOORAY_SYNC_CONFIG_DIR` / `DOORAY_SYNC_STATE_DIR`를 프로젝트 폴더
  하위로 지정해 격리합니다(`.gitignore`에 `.dbg/`가 있습니다).
- 사용자 PC의 상태는 조회 결과가 아니라 **사용자가 실행한 `python diag.py`의 출력**으로만
  판단합니다.

## 개발 PC 구성 (혼동 주의)

**아래 경로는 예시이지 상수가 아닙니다. PC마다 다릅니다.**

2026-09-16에 이 표와 vault 경로를 랩탑에서 그대로 믿었다가 **둘 다 존재하지 않는
폴더**였습니다 — 랩탑에는 `D:` 드라이브 자체가 없습니다. 하드코딩된 경로에는 언제나
소비자가 있고(vault `hardcoded-path-and-its-consumers`), 여기서 그 소비자는 **이 파일을
읽는 다음 에이전트**입니다. 새 PC에서 시작하면 **먼저 확인하고, 확인한 값을 여기 한 줄로
추가**하십시오.

| 무엇 | 데스크톱 (HSY) | 랩탑 |
|---|---|---|
| 저장소(개발) | `D:\drive\dev\dooraydrive` | `C:\drive\dev\dooraydrive` |
| AgentOps vault | `D:\drive\dooraydrive\obsidian\agent_base` | `C:\drive\obsidian\agent_base` |
| 실행되는 설치본 | `C:\dooraydrive` — git이 아닌 **zip 사본** | 같은 규칙 |
| 어느 쪽이 도는가 | `DSYNC_HOME` 사용자 환경변수 | 〃 |

확인하는 법 (PowerShell):

```powershell
Get-PSDrive -PSProvider FileSystem | Select-Object Name          # 어떤 드라이브가 있나
Get-ChildItem C:\, D:\ -Directory -Filter 'AI-Sessions' -Recurse -Depth 4 `
  -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName   # vault
[Environment]::GetEnvironmentVariable('DSYNC_HOME', 'User')      # 실행본
```

`synchere.bat`은 `DSYNC_HOME`을 **가장 먼저** 봅니다([synchere.bat:55](synchere.bat:55)).
**저장소를 고치고 커밋해도 실행본은 바뀌지 않습니다** — 프로그램 폴더를 갱신해야 반영됩니다.
이 분리는 의도된 것입니다(동료 PC와 같은 구성을 개발 PC에서도 밟기 위함). `DSYNC_HOME`을
저장소로 돌리지 않습니다.
