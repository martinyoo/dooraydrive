"""설정 파일 관리 — 규약 §4.

- 읽기는 표준 `tomllib`(읽기 전용), 쓰기는 수동 직렬화. 표준 라이브러리에 TOML writer가 없다.
- 프로파일은 `[profile.<이름>]` 테이블 하나에 대응한다. 설정 파일 1개가 전 프로파일을 담는다.
- `state_dir`/`log_dir`/`db_path`/`lock_path`는 **경로만 반환**하고 디렉터리를 만들지 않는다
  (생성 시점은 호출측이 정한다). 예외적으로 `save_config`만 설정 디렉터리를 만든다.
- 로컬 파일 IO는 전부 `util.paths.ext_path()`를 거친다(규약 §0.2, §12-4).
"""
from __future__ import annotations

import dataclasses
import os
import re
import sys
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util.paths import ext_path

__all__ = [
    "APP_NAME",
    "AUTO_DEFAULTS",
    "DEFAULT_EXCLUDE",
    "Profile",
    "load_auto",
    "save_auto",
    "config_path",
    "state_dir",
    "db_path",
    "log_dir",
    "lock_path",
    "load_config",
    "save_config",
    "config_exists",
]

APP_NAME = "dooray-sync"

# 규약 §4 확정값. 순서·내용을 임의로 늘리지 않는다(다른 모듈의 테스트 기대값).
DEFAULT_EXCLUDE = ['~$*', '*.tmp', '*.part', '*.crdownload', 'Thumbs.db',
                   'desktop.ini', '.dooraysync/', '.~lock.*#']

# 테스트/이식 목적의 위치 재지정. 미설정이면 %APPDATA% / %LOCALAPPDATA%를 쓴다.
ENV_CONFIG_DIR = "DOORAY_SYNC_CONFIG_DIR"
ENV_STATE_DIR = "DOORAY_SYNC_STATE_DIR"

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# 프로파일 이름이 곧 state_dir의 디렉터리 이름이 되므로 경로 탈출 문자를 막는다.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class Profile:
    name: str = "default"
    base_url: str = "https://api.gov-dooray.com"
    drive_id: str = ""
    local_root: str = ""              # 절대경로 문자열
    # 동기화할 원격 하위 폴더('업무/2026' 형태). 비면 드라이브 전체.
    # 실측: 순회 비용이 폴더 수에 비례하므로(폴더당 약 0.4초) 대형 드라이브에서는
    # 업무 폴더 하나만 지정하는 편이 현실적이다.
    remote_path: str = ""
    poll_interval_sec: int = 120
    # 2026-08-11 기본값 전환(False → True). 근거: 원격은 휴지통 이동만 하고 영구삭제
    # API가 코드에 없으며, 로컬은 send2trash가 실패하면 os.remove로 대체하지 않고
    # '실패'로 보고한다(util/trash.py). 되돌릴 수 없는 삭제 경로가 없다.
    # 안전은 이 값이 아니라 세 장치가 지킨다 — 관측하지 않은 항목은 삭제 판정 금지,
    # 대량 삭제 게이트(50건/20%), 그리고 휴지통 자체.
    # **기존 config에는 영향이 없다** — save_config이 이 키를 항상 기록하므로 이미
    # 설치된 PC는 자기 값을 유지한다. 이 기본값은 새로 만드는 프로파일에만 적용된다.
    propagate_deletes: bool = True
    upload_conflict_copy: bool = True
    max_file_mb_warn: int = 400
    bulk_delete_abort_count: int = 50
    bulk_delete_abort_ratio: float = 0.20
    # 이 프로파일에 어떤 자동 동작을 허용하는가 — sync 제외 정책의 단일 정본
    # (예전에는 tools/sync_here.py와 SYNC.ps1에 하드코딩 표가 중복돼 있었다).
    # 'sync'(양방향) | 'push'(올리기 전용) | 'pull'(받기 전용) | 'off'(수동 push/pull만)
    sync_mode: str = "sync"
    sync_note: str = ""               # 결정 사유(사람이 읽는 문장) — 래퍼가 제외 사유로 출력
    # 사람 없이 도는 자동 루프(M3)가 이 프로파일을 건드려도 되는가. **opt-in**이다.
    # sync_mode와 AND로 걸린다 — sync_mode는 '무엇을 하는가', 이 값은 '누가 시키는가'.
    # 켜는 창구는 synchere.bat --auto on(편입 게이트 통과 시)뿐이다.
    auto_sync: bool = False
    # 자동 해제가 sync_note를 덮기 전의 '사람이 쓴 사유'. 자동 복원은 하지 않는다 —
    # 재등록 화면에 후보로 보여 주기만 한다(사람의 결정을 기계가 되살리지 않는다).
    sync_note_prev: str = ""
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))

    @property
    def root_path(self) -> Path:
        return Path(self.local_root)


# --------------------------------------------------------------------------
# 경로
# --------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _config_dir() -> Path:
    override = os.environ.get(ENV_CONFIG_DIR, "").strip()
    if override:
        return Path(override)
    # APPDATA는 Windows에서 항상 있지만, 서비스 계정·테스트 환경에서 빈 경우가 있다.
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else _home() / "AppData" / "Roaming"
    return base / APP_NAME


def _state_root() -> Path:
    override = os.environ.get(ENV_STATE_DIR, "").strip()
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else _home() / "AppData" / "Local"
    return base / APP_NAME


def _check_profile_name(profile_name: str) -> str:
    name = (profile_name or "").strip()
    if not name or not _PROFILE_NAME_RE.match(name) or name in (".", ".."):
        raise ValueError(
            f"프로파일 이름이 올바르지 않습니다: {profile_name!r} "
            "(영문/숫자/'.'/'_'/'-'만 허용)"
        )
    return name


def config_path() -> Path:
    """%APPDATA%\\dooray-sync\\config.toml — 전 프로파일이 이 파일 하나에 들어간다."""
    return _config_dir() / "config.toml"


def state_dir(profile_name: str) -> Path:
    """%LOCALAPPDATA%\\dooray-sync\\<profile>. 생성하지 않는다."""
    return _state_root() / _check_profile_name(profile_name)


def db_path(profile_name: str) -> Path:
    return state_dir(profile_name) / "state.db"


def log_dir(profile_name: str) -> Path:
    return state_dir(profile_name) / "logs"


def lock_path(profile_name: str) -> Path:
    return state_dir(profile_name) / "lock"


# --------------------------------------------------------------------------
# TOML 직렬화 (수동)
# --------------------------------------------------------------------------

_BASIC_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _needs_basic(s: str) -> bool:
    # literal string은 escape가 없어 작은따옴표/제어문자를 담지 못한다.
    return "'" in s or any(ch < " " or ch == "\x7f" for ch in s)


def _toml_string(s: str) -> str:
    r"""문자열 리터럴. 백슬래시 경로(D:\DoorayDrive)가 깨지지 않도록 literal string 우선."""
    if not _needs_basic(s):
        return f"'{s}'"
    out = ['"']
    for ch in s:
        if ch in _BASIC_ESCAPES:
            out.append(_BASIC_ESCAPES[ch])
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_key(k: str) -> str:
    return k if _BARE_KEY_RE.match(k) else _toml_string(str(k))


def _toml_float(v: float) -> str:
    f = float(v)
    if f != f:
        return "nan"
    if f == float("inf"):
        return "inf"
    if f == float("-inf"):
        return "-inf"
    s = repr(f)
    # TOML float은 소수점 또는 지수부가 필요하다. repr(2.0)='2.0'이지만 방어적으로 확인.
    if "." not in s and "e" not in s and "E" not in s:
        s += ".0"
    return s


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):          # bool은 int의 서브클래스 — 반드시 먼저 검사
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _toml_float(v)
    if isinstance(v, str):
        return _toml_string(v)
    if isinstance(v, (list, tuple)):
        items = list(v)
        if not items:
            return "[]"
        if any(isinstance(x, dict) for x in items):
            raise TypeError("array of tables는 직렬화하지 않습니다(설정 스키마에 없음)")
        body = ",\n".join("  " + _toml_value(x) for x in items)
        return "[\n" + body + ",\n]"
    if hasattr(v, "isoformat"):      # tomllib이 돌려주는 date/time/datetime 보존
        return v.isoformat()
    raise TypeError(f"TOML로 직렬화할 수 없는 값: {type(v).__name__}")


def _dump_table(parts: list[str], table: dict, lines: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subs = {k: v for k, v in table.items() if isinstance(v, dict)}
    # 값 없이 하위 테이블만 가진 super-table([profile])은 헤더를 생략한다 —
    # [profile.default] 선언만으로 충분하고, 빈 헤더는 사람이 읽기에 방해된다.
    emit_header = bool(parts) and (bool(scalars) or not subs)
    if emit_header:
        lines.append("[" + ".".join(_toml_key(p) for p in parts) + "]")
    for k, v in scalars.items():
        lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    if emit_header or scalars:
        lines.append("")
    for k, v in subs.items():
        _dump_table(parts + [k], v, lines)


def _dumps(doc: dict) -> str:
    lines = [
        f"# {APP_NAME} 설정 파일 (UTF-8)",
        "# 이 파일은 프로그램이 재작성하므로 직접 단 주석은 보존되지 않습니다.",
        "# 경로 값은 백슬래시를 그대로 쓰기 위해 작은따옴표(literal string)를 사용합니다.",
        "",
    ]
    _dump_table([], doc, lines)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 읽기 / 쓰기
# --------------------------------------------------------------------------

def _read_doc() -> dict:
    """설정 파일 전체를 dict로. **정말 없을 때만** 빈 dict. 그 외 실패는 전파(fail-stop).

    `os.path.exists()`로 판정하면 안 된다 — 권한 거부나 파일 잠김(백신 스캔 등)일 때도
    False를 돌려주기 때문이다(파이썬 문서 명시). 그러면 설정이 멀쩡한데 '없음'으로
    오인하고, 그 상태로 init을 돌리면 save_config가 다른 프로파일까지 날린다.
    실제로 백신이 잠근 순간에 이 오탐이 관측됐다.
    """
    ep = ext_path(config_path())
    try:
        with open(ep, "rb") as f:      # tomllib은 바이너리 스트림만 받는다
            return tomllib.load(f)
    except FileNotFoundError:
        return {}                      # 진짜 없는 경우
    except OSError as exc:
        raise RuntimeError(
            f"설정 파일을 읽을 수 없습니다: {config_path()}\n"
            f"  {type(exc).__name__}: {exc}\n"
            f"  파일이 다른 프로그램(백신·편집기)에 잠겨 있을 수 있습니다. "
            f"잠시 후 다시 시도하세요."
        ) from exc


def diagnose_config_lookup() -> str:
    """설정을 못 찾았을 때 원인을 좁힐 수 있는 사실만 모아 문자열로.

    '파일은 분명히 있는데 프로그램은 없다고 한다'는 상황은 대개 **다른 경로를 보고
    있기 때문**이다(다른 파이썬, 환경변수 재지정, AppData 리다이렉션). 재현이 어려운
    문제라 오류 메시지가 스스로 근거를 들고 오게 한다.
    """
    lines: list[str] = ["", "  [진단]"]
    try:
        cp = config_path()
        lines.append(f"    설정 경로   : {cp}")
        try:
            ep = ext_path(cp)
            lines.append(f"    확장 경로   : {ep}")
            lines.append(f"    존재(확장)  : {os.path.exists(ep)}")
        except Exception as exc:            # noqa: BLE001 — 진단은 절대 실패하면 안 된다
            lines.append(f"    확장 경로   : 계산 실패 {type(exc).__name__}: {exc}")
        lines.append(f"    존재(원본)  : {os.path.exists(str(cp))}")

        parent = cp.parent
        try:
            names = sorted(os.listdir(ext_path(parent)))
            lines.append(f"    폴더 내용   : {', '.join(names) if names else '(비어 있음)'}")
        except OSError as exc:
            lines.append(f"    폴더 열기   : 실패 {type(exc).__name__}: {exc}")

        lines.append(f"    파이썬      : {sys.executable}")
        env_dir = os.environ.get(ENV_CONFIG_DIR)
        lines.append(f"    {ENV_CONFIG_DIR} : {env_dir if env_dir else '(미설정)'}")
        lines.append(f"    APPDATA     : {os.environ.get('APPDATA') or '(미설정)'}")
        lines.append("  이 내용을 그대로 알려 주시면 원인을 특정할 수 있습니다.")
    except Exception as exc:                # noqa: BLE001
        lines.append(f"    진단 자체 실패: {type(exc).__name__}: {exc}")
    return "\n".join(lines)


def _profiles(doc: dict) -> dict:
    section = doc.get("profile")
    return section if isinstance(section, dict) else {}


def _coerce(name: str, ann: str, raw: Any) -> Any:
    """TOML 값 → 필드 타입. 손으로 편집한 설정을 관용적으로 받아들인다."""
    try:
        if ann == "str":
            return str(raw)
        if ann == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, (int, float)):
                return bool(raw)
            return str(raw).strip().lower() in ("true", "yes", "on", "1")
        if ann == "int":
            return int(raw)
        if ann == "float":
            return float(raw)
        if ann.startswith("list"):
            if isinstance(raw, (list, tuple)):
                return [str(x) for x in raw]
            return [str(raw)]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"설정 값이 올바르지 않습니다: {name}={raw!r} ({exc})") from exc
    return raw


def load_config(profile: str = "default") -> Profile:
    """없으면 FileNotFoundError. tomllib로 읽기.

    파일은 있으나 해당 프로파일 테이블이 없을 때도 FileNotFoundError로 통일한다
    (호출측은 config_exists로 사전 판정한다).
    """
    name = _check_profile_name(profile)
    path = config_path()
    doc = _read_doc()
    if not doc:
        raise FileNotFoundError(
            f"설정 파일이 없습니다: {path}\n"
            f"  먼저 'dsync init'을 실행하세요.\n"
            f"{diagnose_config_lookup()}"
        )
    table = _profiles(doc).get(name)
    if not isinstance(table, dict):
        raise FileNotFoundError(
            f"프로파일 '{name}'이(가) 설정 파일에 없습니다: {path}\n"
            f"  먼저 'dsync init --profile {name}'을 실행하세요."
        )

    p = Profile(name=name)
    for f in dataclasses.fields(Profile):
        if f.name == "name" or f.name not in table:
            continue                    # name은 섹션 키가 정본 — 파일 안의 중복 기재는 무시
        setattr(p, f.name, _coerce(f.name, str(f.type), table[f.name]))
    # 뒤에 '/'가 붙으면 client가 조립하는 URL이 '//'가 된다.
    p.base_url = p.base_url.strip().rstrip("/")
    p.drive_id = p.drive_id.strip()
    p.local_root = p.local_root.strip()
    # config는 프로그램 소유 파일이라 잘못된 값 = 손상/손편집이다 — fail-closed.
    # (_load_profile이 ValueError를 EXIT_CONFIG로 옮긴다.)
    p.sync_mode = (p.sync_mode or "sync").strip().lower()
    if p.sync_mode not in ("sync", "push", "pull", "off"):
        raise ValueError(f"sync_mode 값이 올바르지 않습니다: {p.sync_mode!r} "
                         "(sync | push | pull | off)")
    return p


def save_config(p: Profile) -> None:
    """수동 직렬화(표준 라이브러리에 toml writer 없음). UTF-8.

    다른 프로파일과 미지 키는 읽어서 그대로 다시 쓴다(파괴적 덮어쓰기 금지).
    임시 파일 → os.replace로 원자 교체 — 쓰기 중 중단 시 설정이 반쪽으로 남지 않게.
    """
    name = _check_profile_name(p.name)
    doc = _read_doc()

    # 방어선: 파일은 있는데 문서가 비어 보이면 쓰지 않는다. 읽기가 어떤 이유로든
    # 실패했는데 빈 dict가 넘어온 경우 그대로 쓰면 다른 프로파일이 전부 사라진다.
    dest_probe = ext_path(config_path())
    if not doc:
        try:
            with open(dest_probe, "rb") as f:
                nonempty = bool(f.read(1))
        except OSError:
            nonempty = False
        if nonempty:
            raise RuntimeError(
                f"설정 파일이 존재하는데 내용을 읽지 못했습니다: {config_path()}\n"
                f"  덮어쓰면 다른 프로파일이 사라지므로 중단합니다. 파일 상태를 확인하세요."
            )

    section = doc.get("profile")
    if not isinstance(section, dict):
        section = {}
        doc["profile"] = section

    existing = section.get(name)
    # 기존 테이블을 토대로 덮어써 우리가 모르는 키(수동 추가·미래 버전)를 보존한다.
    table: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    for f in dataclasses.fields(Profile):
        if f.name == "name":
            continue                    # 섹션 키와 중복 — 파일에 쓰지 않는다
        value = getattr(p, f.name)
        if f.name == "base_url" and isinstance(value, str):
            value = value.strip().rstrip("/")
        if isinstance(value, list):
            value = list(value)
        table[f.name] = value
    section[name] = table

    text = _dumps(doc)
    dest = config_path()
    os.makedirs(ext_path(dest.parent), exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
    ep_tmp = ext_path(tmp)
    try:
        with open(ep_tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(ep_tmp, ext_path(dest))
    except BaseException:
        try:
            os.remove(ep_tmp)
        except OSError:
            pass
        raise


def config_exists(profile: str = "default") -> bool:
    name = _check_profile_name(profile)
    return isinstance(_profiles(_read_doc()).get(name), dict)


# --------------------------------------------------------------------------
# [auto] — 자동 동기화 루프의 전역 설정 (M3)
# --------------------------------------------------------------------------
# 프로파일별 값이 아니라 PC 단위 정책이라 최상위 테이블에 둔다. save_config이
# 미지 최상위 테이블을 보존하므로(_read_doc → _dumps 왕복) 호환이 유지된다.
AUTO_DEFAULTS: dict[str, Any] = {
    # 출근(첫 틱)으로부터 몇 시간 뒤를 퇴근으로 볼 것인가. 퇴근 스윕은 그 15분 전.
    # 07시 기동이면 15:45 스윕(퇴근 16시). 러너가 매 틱 읽으므로 값 변경은
    # 재등록 없이 즉시 반영된다.
    "work_hours": 9.0,
    # 틱 간격(초). 창 안 루프가 이 간격으로 깨어난다.
    "tick_sec": 120,
    # 프로파일당 최소 동기화 간격(초). 사용자 결정(2026-08-16): 30분.
    "min_interval_sec": 1800,
    # 직전 틱과 이만큼 벌어졌으면 '출근'으로 본다(부팅·절전 복귀 포괄, 밤샘 가동
    # PC의 자정 틱 오인 방지).
    "day_gap_hours": 6.0,
    # 무인 실행이 자동으로 허용하는 삭제 상한. 5영업일 관측 후 조정(단위 14).
    "max_auto_deletes": 5,
    "max_auto_delete_mb": 50,
}


def load_auto() -> dict[str, Any]:
    """config의 [auto] 테이블 + 기본값. 손편집 오타는 기본값으로 떨어뜨린다 —
    자동 루프가 설정 한 글자 때문에 안 도는 것이 더 나쁘다(보고는 한다)."""
    doc = _read_doc()
    raw = doc.get("auto")
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = dict(AUTO_DEFAULTS)
    for key, default in AUTO_DEFAULTS.items():
        if key not in raw:
            continue
        try:
            out[key] = type(default)(raw[key])
        except (TypeError, ValueError):
            pass          # 기본값 유지
    return out


def save_auto(values: dict[str, Any]) -> None:
    """[auto] 테이블의 일부 키를 갱신한다(나머지 키·프로파일은 그대로 보존).

    save_config과 같은 왕복을 쓰지 않고 직접 쓰는 이유: save_config은 Profile
    하나를 인자로 받는 함수라 프로파일 없는 갱신을 표현할 수 없다. 원자 교체와
    '빈 문서면 쓰지 않는다' 방어는 동일하게 적용한다.
    """
    doc = _read_doc()
    dest_probe = ext_path(config_path())
    if not doc:
        try:
            with open(dest_probe, "rb") as f:
                nonempty = bool(f.read(1))
        except OSError:
            nonempty = False
        if nonempty:
            raise RuntimeError(
                f"설정 파일이 존재하는데 내용을 읽지 못했습니다: {config_path()}\n"
                f"  덮어쓰면 다른 설정이 사라지므로 중단합니다.")
    table = doc.get("auto")
    table = dict(table) if isinstance(table, dict) else {}
    table.update(values)
    doc["auto"] = table

    text = _dumps(doc)
    dest = config_path()
    os.makedirs(ext_path(dest.parent), exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
    ep_tmp = ext_path(tmp)
    try:
        with open(ep_tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(ep_tmp, ext_path(dest))
    except BaseException:
        try:
            os.remove(ep_tmp)
        except OSError:
            pass
        raise
