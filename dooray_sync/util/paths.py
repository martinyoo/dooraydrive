"""경로 정규화·파일명 검사.

이 모듈이 프로젝트 안전성의 토대다. 로컬 파일시스템에 닿는 모든 경로는
반드시 ext_path()를 거친다(규약 §0-2, 구현계획서 C3). 접두 없이 열면
260자 제한과 끝 점 절삭에 그대로 노출된다.
"""
from __future__ import annotations

import os
import unicodedata
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import Path

WINDOWS_RESERVED: frozenset[str] = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

# 제어문자 포함 — 서버에는 존재할 수 있으나 Windows 파일명으로는 만들 수 없다
ILLEGAL_CHARS: str = '<>:"/\\|?*' + "".join(chr(c) for c in range(32))

# r"..." 로는 끝 백슬래시를 쓸 수 없어 이스케이프 표기. 값은 각각 \\?\ 와 \\?\UNC\
_EXT_PREFIX = "\\\\?\\"
_UNC_EXT_PREFIX = "\\\\?\\UNC\\"
_DEVICE_PREFIX = "\\\\.\\"


def to_nfc(s: str) -> str:
    """유니코드 NFC 정규화. None 안전(빈 문자열 반환)."""
    if not s:
        return ""
    return unicodedata.normalize("NFC", str(s))


def path_key(rel_path: str) -> str:
    """비교용 키: NFC 정규화 + casefold + 구분자 '/' 통일.

    Windows 대소문자 무시 특성 때문에 DB UNIQUE 키로 사용한다.
    """
    if not rel_path:
        return ""
    parts = [p for p in str(rel_path).replace("\\", "/").split("/") if p and p != "."]
    # casefold 결과가 NFC가 아닐 수 있어(예: 'ẞ' → 'ss') 접은 뒤 한 번 더 정규화한다
    return to_nfc(to_nfc("/".join(parts)).casefold())


def _strip_ext_prefix(s: str) -> str:
    """\\\\?\\ / \\\\?\\UNC\\ 접두를 떼어 일반 경로 표기로 되돌린다."""
    if s.startswith(_UNC_EXT_PREFIX):
        return "\\\\" + s[len(_UNC_EXT_PREFIX):]
    if s.startswith(_EXT_PREFIX):
        return s[len(_EXT_PREFIX):]
    return s


def _lexical_abs(p: Path | str) -> str:
    r"""'.'/'..' 를 문자열 수준에서만 해소한 절대경로(접두 없음).

    os.path.abspath/normpath는 Windows API(GetFullPathName)를 거치며 마지막
    컴포넌트의 끝 점·끝 공백을 절삭한다 — \\?\ 접두로 지키려는 바로 그 이름이
    거기서 사라지므로(실측: 끝 점 절삭, 검토보고서 §3 표8) 직접 정규화한다.
    """
    s = _strip_ext_prefix(str(p).replace("/", "\\"))
    drive, rest = os.path.splitdrive(s)
    if drive:
        if rest.startswith("\\"):
            root, tail = drive + "\\", rest
        else:
            # 'C:foo' = 해당 드라이브의 현재 디렉터리 기준. cwd 드라이브가 같을 때만 의미가 있다
            cwd_drive, cwd_rest = os.path.splitdrive(os.getcwd())
            same = cwd_drive.casefold() == drive.casefold()
            root, tail = drive + "\\", (cwd_rest + "\\" + rest) if same else rest
    else:
        cwd_drive, cwd_rest = os.path.splitdrive(os.getcwd())
        root = (cwd_drive + "\\") if cwd_drive else "\\"
        # '\foo' 는 cwd 드라이브의 루트 기준, 그 외는 cwd 기준
        tail = s if s.startswith("\\") else (cwd_rest + "\\" + s)

    parts: list[str] = []
    for comp in tail.split("\\"):
        if not comp or comp == ".":
            continue
        if comp == "..":
            if parts:
                parts.pop()
            continue
        parts.append(comp)
    return root + "\\".join(parts)


def ext_path(p: Path | str) -> str:
    r"""절대경로로 변환 후 '\\?\' 접두를 붙인 str 반환.

    - 이미 접두가 있으면 그대로 반환
    - UNC 경로(\\server\share)는 '\\?\UNC\server\share' 형태로 변환
    - 상대경로는 cwd 기준 절대경로로 확장
    - 경로 구분자는 백슬래시로 통일 (\\?\ 접두는 슬래시를 구분자로 인정하지 않는다)
    """
    s = str(p)
    if not s:
        raise ValueError("빈 경로는 확장 경로로 변환할 수 없습니다")
    s = s.replace("/", "\\")
    if s.startswith(_EXT_PREFIX) or s.startswith(_DEVICE_PREFIX):
        return s
    abs_s = _lexical_abs(s)
    # UNC 판정은 원본이 아니라 확장 결과로 한다 — cwd 자체가 UNC일 수 있다
    if abs_s.startswith("\\\\"):
        return _UNC_EXT_PREFIX + abs_s[2:]
    return _EXT_PREFIX + abs_s


def rel_posix(root: Path, target: Path) -> str:
    """root 기준 상대경로를 '/' 구분자 + NFC 문자열로. 루트 자신은 ''."""
    r = _lexical_abs(root).rstrip("\\")
    t = _lexical_abs(target).rstrip("\\")
    if t.casefold() == r.casefold():
        return ""
    prefix = r + "\\"
    if not t.casefold().startswith(prefix.casefold()):
        raise ValueError(f"동기화 루트 밖의 경로입니다: root={r} target={t}")
    return to_nfc(t[len(prefix):].replace("\\", "/"))


def local_path(root: Path, rel_posix_path: str) -> Path:
    """rel_posix('a/b.txt')를 root 기준 Path로 복원."""
    parts = [p for p in str(rel_posix_path or "").replace("\\", "/").split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        # 원격에서 온 경로가 동기화 루트를 탈출하는 것을 차단한다
        raise ValueError(f"상위 참조가 포함된 상대경로는 허용하지 않습니다: {rel_posix_path!r}")
    if not parts:
        return Path(root)
    # Path.__truediv__ 로 한 조각씩 붙이면 'C:' 같은 조각이 드라이브로 재해석돼
    # 루트가 통째로 날아간다. 문자열로 이어 붙여 드라이브 해석을 선두로 고정한다.
    return Path(str(root).rstrip("\\/") + "\\" + "\\".join(parts))


def join_remote(parent_path: str, name: str) -> str:
    """원격 전체 경로 = parent_path + '/' + name.

    실측: changes/meta의 path는 '부모 폴더 경로'이고 name은 별도 필드다.
    parent_path가 '/'이거나 빈 문자열이면 '/name'.
    """
    parent = str(parent_path or "").replace("\\", "/").rstrip("/")
    n = str(name or "").replace("\\", "/").strip("/")
    if not n:
        return parent or "/"
    return f"{parent}/{n}" if parent else f"/{n}"


def split_remote(full_path: str) -> tuple[str, str]:
    """join_remote의 역. ('/a/b', 'c.txt')."""
    s = str(full_path or "").replace("\\", "/")
    if not s:
        return "", ""
    s = s.rstrip("/")
    if not s:
        return "/", ""
    head, _, name = s.rpartition("/")
    return (head or "/"), name


def is_windows_reserved(name: str) -> bool:
    """확장자를 제외한 스템이 예약어인지. 'CON', 'CON.txt' 모두 True.

    실측: 서버는 'CON'을 그대로 저장한다(검토보고서 §3.7-4) — 원격에는 존재할 수 있다.
    """
    stem = to_nfc(name).split(".")[0].rstrip(" ")
    return stem.upper() in WINDOWS_RESERVED


def name_issue(name: str) -> str | None:
    """Windows 로컬에 그대로 저장할 수 없는 이름이면 사유 문자열, 아니면 None.

    끝 점/끝 공백은 ext_path 경유 시 보존 가능하지만, 탐색기·일반 도구에서
    조용히 절삭되므로(실측: 검토보고서 §3 표8) unsyncable로 표시한다.
    """
    n = to_nfc(name)
    if not n or not n.strip():
        return "빈 이름"
    bad = sorted({c for c in n if c in ILLEGAL_CHARS})
    if bad:
        return "Windows 금지문자 포함: " + ", ".join(repr(c) for c in bad)
    if is_windows_reserved(n):
        return f"Windows 예약어: {n.split('.')[0].rstrip(' ').upper()}"
    if n.endswith("."):
        return "이름이 점(.)으로 끝남 — 일반 경로에서 절삭됨"
    if n.endswith(" "):
        return "이름이 공백으로 끝남 — 일반 경로에서 절삭됨"
    return None


def server_name_will_differ(name: str) -> bool:
    """서버가 이름을 바꿀 것으로 예상되면 True.

    실측(R14): 서버는 앞/뒤 공백을 절삭하고 '"'를 '%22'로 치환한다.
    앞 공백은 Windows에서 합법이라 방치하면 매 주기 재전송 루프가 된다.
    호출측은 이 경우 업로드 응답의 실제 저장명을 정본으로 기록해야 한다(규약 §12-7).
    """
    n = to_nfc(name)
    if not n:
        return False
    return n != n.strip() or '"' in n


def matches_any(rel_path: str, patterns: Sequence[str]) -> bool:
    """fnmatch 기반 exclude 판정.

    패턴이 '/'로 끝나면 디렉터리 접두 매칭(예: '.dooraysync/'), 아니면 전체 경로와
    각 컴포넌트 양쪽에 매칭한다(예: '*.tmp'는 'a/b.tmp'도 매치).
    Windows 대소문자 무시를 반영해 양쪽을 casefold한 뒤 fnmatchcase를 쓴다
    (fnmatch.fnmatch는 normcase가 '/'를 '\\'로 바꿔 패턴을 망가뜨린다).
    """
    if not rel_path or not patterns:
        return False
    key = path_key(rel_path)
    if not key:
        return False
    comps = key.split("/")
    for raw in patterns:
        if not raw:
            continue
        pat = to_nfc(str(raw)).replace("\\", "/").casefold()
        if pat.endswith("/"):
            d = pat.strip("/")
            if not d:
                continue
            if "/" in d:
                if key == d or key.startswith(d + "/"):
                    return True
            elif any(fnmatchcase(c, d) for c in comps):
                return True
            continue
        if fnmatchcase(key, pat):
            return True
        if any(fnmatchcase(c, pat) for c in comps):
            return True
    return False
