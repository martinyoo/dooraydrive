"""PoC-08: 파일명/경로 엣지 케이스.

검증 항목
- 한글 NFC/NFD 정규화: 서버가 이름을 정규화하는지, NFD 업로드 후 무슨 이름으로 저장되는지
- 특수문자·예약어·끝 점/공백·이모지 파일명의 서버 수용 여부
- Windows에서 생성 불가한 이름(CON.txt 등)이 원격에 존재할 때 로컬 저장 가능성 (unsyncable 정책 근거)
- 260자 초과 경로: \\?\ 접두사로 로컬 생성 가능 여부

메모리 → 업로드 방식이므로 로컬에 위험한 이름의 파일을 만들지 않고도 서버 동작을 관측할 수 있다.

실행: python poc_08_names.py   (선행: poc_01, poc_02)
"""
import os
import unicodedata

from poc_common import PocClient, TMP_DIR, require_drive_id

NAME_CASES = [
    ("nfc_korean", unicodedata.normalize("NFC", "한글정규화테스트.txt")),
    ("nfd_korean", unicodedata.normalize("NFD", "한글정규화테스트NFD.txt")),
    ("spaces", "이름에 공백  두 칸.txt"),
    ("special_chars", "특수#%&+;=문자.txt"),
    ("leading_space", " 앞공백.txt"),
    ("trailing_space", "뒤공백.txt "),
    ("trailing_dot", "끝점."),
    ("reserved_con", "CON.txt"),
    ("reserved_aux", "aux.md"),
    ("emoji", "📄문서.txt"),
    ("quotes", "따옴표'와\"쌍따옴표.txt"),
    ("long_name_200", "긴이름" + "가" * 94 + ".txt"),  # 한글 200자 근처
]

pc = PocClient("08_names")
try:
    drive_id = require_drive_id()
    sandbox_id = pc.ensure_sandbox(drive_id)
    payload = os.urandom(1024)

    case_results = {}
    uploaded_ids = []
    for key, name in NAME_CASES:
        entry: dict = {"sent_name": name, "sent_repr": repr(name)}
        try:
            _, body = pc.upload_new(drive_id, sandbox_id, name, payload)
            fid = body["result"]["id"]
            uploaded_ids.append(fid)
            meta = pc.file_meta(drive_id, fid)["result"]
            stored = meta.get("name") or ""
            entry.update(
                accepted=True,
                stored_name=stored,
                stored_repr=repr(stored),
                stored_equals_sent=stored == name,
                stored_is_nfc=stored == unicodedata.normalize("NFC", stored),
                nfc_equal=unicodedata.normalize("NFC", stored) == unicodedata.normalize("NFC", name),
            )
            # 로컬 저장 시도: '정확한 이름 그대로' 생성되는지 검증.
            # 주의: Win32는 끝 점/공백을 조용히 제거하므로 open 성공만으로는 판정 불가 —
            #       쓰기 후 디렉터리 목록에서 실제 생성명을 대조해야 한다.
            local_ok, local_note = False, ""
            case_dir = TMP_DIR / "names" / key  # 케이스별 전용 폴더 → 생성명 대조가 명확
            target = case_dir / stored

            def exact_name_exists(dir_path) -> tuple[bool, list]:
                names = os.listdir(dir_path)
                hit = any(unicodedata.normalize("NFC", n) == unicodedata.normalize("NFC", stored) for n in names)
                return hit, names

            try:
                case_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                exact, created = exact_name_exists(case_dir)
                if exact:
                    local_ok, local_note = True, "일반 경로"
                else:
                    # 일반 경로에서는 이름이 변형됨(mangled) → \\?\ 접두로 정확한 이름 생성 재시도
                    entry["plain_path_mangled"] = True
                    entry["plain_path_created_as"] = created
                    try:
                        with open("\\\\?\\" + str(target), "wb") as f:
                            f.write(payload)
                        exact2, created2 = exact_name_exists("\\\\?\\" + str(case_dir))
                        local_ok = exact2
                        local_note = (
                            r"일반 경로에선 이름 변형 → \\?\ 접두로만 정확한 이름 생성 가능"
                            if exact2 else f"이름 변형됨: 실제 생성명={created}"
                        )
                    except OSError as e2:
                        local_note = f"이름 변형됨(일반 경로), \\?\ 접두도 실패: {e2}"
            except OSError as e1:
                try:
                    case_dir.mkdir(parents=True, exist_ok=True)
                    with open("\\\\?\\" + str(target), "wb") as f:
                        f.write(payload)
                    exact3, _ = exact_name_exists("\\\\?\\" + str(case_dir))
                    local_ok = exact3
                    local_note = r"\\?\ 접두 필요" if exact3 else r"\\?\ 접두로도 이름 변형"
                except OSError as e2:
                    local_note = f"로컬 생성 불가: {e1} / {e2}"
            entry["local_writable"] = local_ok  # 의미: '정확한 저장명 그대로' 생성 가능
            entry["local_note"] = local_note
        except Exception as e:
            entry.update(accepted=False, error=str(e)[:300])
        case_results[key] = entry
        pc.log(f"[{key}] 수용={entry.get('accepted')} 저장명={entry.get('stored_repr', '-')} 로컬={entry.get('local_note', '-')}")

    pc.results["name_cases"] = case_results

    # 260자 초과 로컬 경로 실험 (원격과 무관한 Windows 측 검증)
    # 폴더명 1개는 60자(컴포넌트 한도 255자 이내), 4단계 중첩으로 총 경로 길이를 260자 초과로 구성
    seg = "깊은폴더" * 15  # 60자
    deep = TMP_DIR / "names" / seg / seg / seg / seg
    assert len(str(deep)) > 260, f"경로 길이 부족: {len(str(deep))}"
    long_result = {}
    try:
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "파일.txt").write_bytes(b"x")
        long_result["plain"] = "성공 (LongPathsEnabled 활성 추정)"
    except OSError as e:
        long_result["plain"] = f"실패: {e}"
        try:
            ext_dir = "\\\\?\\" + str(deep)
            os.makedirs(ext_dir, exist_ok=True)
            with open(ext_dir + "\\파일.txt", "wb") as f:
                f.write(b"x")
            long_result["extended_prefix"] = "성공"
        except OSError as e2:
            long_result["extended_prefix"] = f"실패: {e2}"
    pc.results["long_path"] = long_result
    pc.log(f"260자 초과 경로: {long_result}")

    # 정리: 업로드한 테스트 파일 전부 휴지통으로
    for fid in uploaded_ids:
        try:
            pc.move_to_trash(drive_id, fid)
        except Exception as e:
            pc.log(f"휴지통 이동 실패 id={fid}: {e}")
    pc.log(f"정리 완료: {len(uploaded_ids)}개 휴지통 이동")

    pc.save()
finally:
    pc.close()
