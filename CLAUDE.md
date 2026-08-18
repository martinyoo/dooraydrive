# CLAUDE.md

이 저장소에서 일할 때 따라야 하는 규약입니다.

## Knowledge Base — AgentOps vault

이 프로젝트의 **결정·교훈·배포 전략은 저장소가 아니라 AgentOps vault에 있습니다.**
코드와 코드에 붙는 설명은 이 저장소(`docs/`)에, 프로젝트를 건너 재사용되는 지식은 vault에 둡니다.

| 항목 | 값 |
|---|---|
| 로컬 경로 | `D:\drive\dooraydrive\obsidian\agent_base` (Obsidian vault) |
| 원격 | `https://github.com/martinyoo/AgentOps` — **private** |
| 이 프로젝트 관련 문서 | `AI-Sessions/wiki/` 아래, `project: AgentOps` 또는 `shared` |
| 명령 키워드 | `save`(저장) · `ingest`(raw 가공) · `query`(조회) · `lint`(점검) |
| 규약 정본 | vault의 `CLAUDE.md` — 저장 전에 그것을 먼저 읽습니다 |

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
- **A. 버전 정체성 — 부분 구현** (M3, 2026-08-18 HSY 실측). `__version__`은 `0.2.0`으로
  올랐고 **표시된다** — `dsync --version`·`doctor`·`--report-json`·자동 루프 헤더, 그리고
  로그 첫 줄(`logging_setup.py:132`). 동료가 로그를 붙여넣으면 버전을 안다.
  **남은 것 둘, 그리고 둘 다 롤백의 선행조건이다**: (a) **git 태그가 하나도 없다**
  (`git tag -l`이 빈 목록) — 태그가 없으면 zip URL을 `refs/tags/<version>.zip`으로
  지목할 수 없다. (b) **설치 스탬프가 없다** — 프로그램 폴더에 무엇을 언제 어디서
  받았는지 남지 않아, 실행조차 안 되는 PC에서는 버전을 알 길이 없다.
- **C. `synchere.bat` 새 버전 알림 — 미구현.** 확인함(2026-08-18): 저장소 어디에도
  `raw.githubusercontent` 버전 조회가 없다.
- **D. DB `PRAGMA user_version` — 완료** (M3 단위 2). `store/db.py:43`에 `SCHEMA_VERSION`,
  `db.py:187~210`에 승격·검증. 새 스키마 DB를 옛 코드가 열면 거부한다
  (`tests/test_schema_version.py`).
- **롤백 수단이 없다.** B는 교체 성공 직후 `.old`를 지우므로 되돌릴 사본이 남지
  않고, zip URL이 항상 `refs/heads/main.zip`이라 과거 버전을 받을 수단도 없다.
  **A의 (a) 태그가 선행되지 않으면 롤백은 불가능하다.** D가 끝났어도 이건 그대로다 —
  D는 "옛 코드가 새 DB를 조용히 깨뜨리는 것"을 막을 뿐, 되돌릴 코드를 만들어 주지 않는다.
- **M3가 B에 새 위험을 얹었다(미검증, 분석 수준).** 자동 루프가 도입되면서 프로그램
  폴더를 `sys.path`에 얹은 파이썬 프로세스가 **하루 종일 살아 있다.** `설치.bat`이
  교체에 성공하면 그 프로세스의 `sys.path` 문자열은 이제 **새 폴더**를 가리키므로,
  교체 이후에 일어나는 지연 임포트는 옛 코드가 도는 프로세스에 새 코드를 섞어 넣는다
  — 원칙 1이 금지한 "파일 단위 혼합"이 프로세스 안에서 재현되는 형태다.
  전략 문서 §4 B의 "미구현: 동기화 실행 중 갱신 방지"가 이제 예외가 아니라 **평시**다.
  **갱신 전에 자동 루프 창을 닫게 하는 안내가 매뉴얼에 반드시 들어가야 한다.**

## 개발 PC 구성 (혼동 주의)

| 무엇 | 경로 |
|---|---|
| 저장소(개발) | `D:\drive\dev\dooraydrive` |
| 실행되는 설치본 | `C:\dooraydrive` — git이 아닌 **zip 사본** |
| 어느 쪽이 도는가 | `DSYNC_HOME` 사용자 환경변수 = `C:\dooraydrive` |

`synchere.bat`은 `DSYNC_HOME`을 **가장 먼저** 봅니다([synchere.bat:55](synchere.bat:55)).
**저장소를 고치고 커밋해도 실행본은 바뀌지 않습니다** — 프로그램 폴더를 갱신해야 반영됩니다.
이 분리는 의도된 것입니다(동료 PC와 같은 구성을 개발 PC에서도 밟기 위함). `DSYNC_HOME`을
저장소로 돌리지 않습니다.
