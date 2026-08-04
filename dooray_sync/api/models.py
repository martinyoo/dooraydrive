"""Dooray Drive API 응답 → 값 객체 (규약 §6).

파싱은 전부 방어적이다. 스펙에 없는 필드는 무시하고, 있어야 할 필드가 없거나
형이 다르면 안전한 기본값으로 떨어진다 — API가 조용히 필드를 추가/변경해도
동기화가 죽지 않아야 한다(공식 가이드 요구사항).

실측 근거 원본: poc/poc_results/{02_listing,04_upload,05_changes}.json
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..util.paths import join_remote, to_nfc

# ---------------------------------------------------------------------------
# 파싱 헬퍼
# ---------------------------------------------------------------------------


def _to_int(value: Any, default: int = 0) -> int:
    """실측: revision/version이 문자열로 온다 — meta의 revision="17469",
    upload 응답의 revision="0", changes의 revision="17515".
    변환 불가(None, 빈 문자열, 비숫자)면 조용히 default."""
    if isinstance(value, bool):  # bool은 int의 서브클래스라 먼저 걸러낸다
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _to_int_or_none(value: Any) -> int | None:
    """None을 보존하는 정수 변환.
    실측: deleted 항목은 version/size가 전부 null, folder는 size가 0 또는 null.
    '값 없음'과 '0'은 의미가 다르므로 None을 0으로 뭉개지 않는다."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _to_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _norm_hash(value: Any) -> str | None:
    """실측(PoC-05, 2회 교차검증): hash는 MD5 소문자 hex.
    util.hashing.md5_file의 출력과 직접 비교하므로 표기를 소문자로 고정한다.
    folder/deleted 항목은 null이며 목록 API 응답에는 hash 필드 자체가 없다."""
    if not isinstance(value, str):
        return None
    h = value.strip().lower()
    return h or None


def _norm_remote_path(value: Any) -> str | None:
    """원격 '부모 폴더 경로' 표기 통일.

    실측: 같은 개념을 두 표기로 준다.
      - changes v2 의 file.path        → '/_poc_sandbox'   (선행 '/', root 미표기)
      - meta/upload 의 parentFile.path → 'root/_poc_sandbox' (선행 '/' 없음, root 접두)
    두 출처가 섞이면 경로 비교(=동기화 판정)가 통째로 깨지므로 changes 표기로 통일한다.
    루트 자신은 '/'.
    """
    if not isinstance(value, str):
        return None
    p = to_nfc(value).replace("\\", "/").strip()
    if not p:
        return ""
    while len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    # 드라이브 최상위 폴더의 실제 이름이 'root'다(실측 02_listing: subType='root').
    if p == "root":
        return "/"
    if p.startswith("root/"):
        p = p[len("root"):]
    if not p.startswith("/"):
        p = "/" + p
    return p


# ---------------------------------------------------------------------------
# 값 객체
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteFile:
    """meta / 목록 / 업로드 응답이 공유하는 파일·폴더 표현."""

    id: str
    name: str
    type: str                 # 'file' | 'folder'
    drive_id: str = ""
    parent_path: str = ""     # 부모 폴더 경로 (파일명 미포함)
    parent_id: str = ""
    sub_type: str = ""        # root, trash, users / etc, doc, photo, movie, music, zip
    version: int = 0
    revision: int = 0
    size: int | None = None
    mime_type: str = ""
    md5: str | None = None    # 목록 API 응답에는 없음(실측), changes에만 존재

    @property
    def is_dir(self) -> bool:
        return self.type == "folder"

    @property
    def full_path(self) -> str:
        return join_remote(self.parent_path, self.name)

    @classmethod
    def from_api(cls, d: dict, drive_id: str = "", parent_id: str = "") -> RemoteFile:
        """meta/list/upload 응답의 파일 dict를 파싱. 미지 필드는 무시.

        parentFile.{id,path}가 있으면 parent_id/parent_path에 반영한다(meta/upload).
        목록 API 응답에는 parentFile도 path도 없으므로(실측 02_listing) 호출측이
        넘긴 parent_id를 쓰고 parent_path는 ''로 남는다 — 순회측(DriveAPI.walk)이
        경로를 따로 조립한다.
        """
        if not isinstance(d, dict):
            d = {}

        parent = d.get("parentFile")
        if not isinstance(parent, dict):
            parent = {}

        # parentFile.path(meta)를 우선, 없으면 file.path(changes 형태)를 본다.
        raw_parent_path = parent.get("path")
        if raw_parent_path is None:
            raw_parent_path = d.get("path")
        parent_path = _norm_remote_path(raw_parent_path) or ""

        return cls(
            id=_to_str(d.get("id")),
            name=to_nfc(_to_str(d.get("name"))),
            type=_to_str(d.get("type")),
            # 응답의 driveId가 정본, 없을 때만 호출측 값으로 보완.
            drive_id=_to_str(d.get("driveId")) or _to_str(drive_id),
            parent_path=parent_path,
            parent_id=_to_str(parent.get("id")) or _to_str(parent_id),
            sub_type=_to_str(d.get("subType")),
            version=_to_int(d.get("version")),
            revision=_to_int(d.get("revision")),
            size=_to_int_or_none(d.get("size")),
            mime_type=_to_str(d.get("mimeType")),
            md5=_norm_hash(d.get("hash")),
        )


@dataclass(frozen=True)
class ChangeItem:
    """changes v2 응답의 항목 1건."""

    revision: int
    change_type: str          # 'updated' | 'deleted'
    file_id: str
    file_type: str            # 'file' | 'folder'
    version: int | None = None
    size: int | None = None
    name: str | None = None   # deleted면 None
    parent_path: str | None = None   # deleted면 None. API의 file.path
    md5: str | None = None    # deleted/folder면 None

    @property
    def is_deleted(self) -> bool:
        return self.change_type == "deleted"

    @property
    def full_path(self) -> str | None:
        """deleted 항목은 경로를 알 수 없다 — DB에서 file_id로 역참조해야 한다(B3)."""
        if self.name is None or self.parent_path is None:
            return None
        return join_remote(self.parent_path, self.name)

    @classmethod
    def from_api(cls, d: dict) -> ChangeItem:
        """실측: deleted 항목은 id/type/revision만 있고 나머지는 전부 null.

        revision은 항목 최상위와 file 안쪽 양쪽에 오지만, 최상위가 빠진 응답이
        관측되었으므로(05_changes.trash_representation) file.revision으로 보완한다.
        """
        if not isinstance(d, dict):
            d = {}
        f = d.get("file")
        if not isinstance(f, dict):
            f = {}

        revision = _to_int(d.get("revision"))
        if revision == 0:
            revision = _to_int(f.get("revision"))

        # 미지의 changeType을 deleted로 승격시키지 않는다 — 오삭제가 오잔존보다 훨씬 위험.
        change_type = _to_str(d.get("changeType")).strip().lower()

        name = _to_str_or_none(f.get("name"))
        return cls(
            revision=revision,
            change_type=change_type,
            file_id=_to_str(f.get("id")),
            file_type=_to_str(f.get("type")),
            version=_to_int_or_none(f.get("version")),
            size=_to_int_or_none(f.get("size")),
            name=to_nfc(name) if name is not None else None,
            parent_path=_norm_remote_path(f.get("path")),
            md5=_norm_hash(f.get("hash")),
        )


@dataclass(frozen=True)
class Cursor:
    """changes 페이징 커서.

    `file_id`는 **진단·기록용으로만** 보관한다. 질의에는 넣지 않는다 — 아래 참조.
    """

    revision: int = 0
    file_id: str | None = None

    def as_params(self) -> dict:
        """실측: 필터가 실제로 먹는 파라미터명은 latestRevision이다
        (05_changes.working_param — revision=은 무시되고 전체가 돌아온다).
        커서는 배타적(cursor_inclusive=false)이라 이 revision '다음'부터 받는다.

        **fileId를 함께 보내지 않는다 (2026-08-03 실계정 대조 실험으로 정정).**
        초판 규약은 "동일 revision에 복수 항목이 공존하므로 (revision, fileId) 복합
        커서가 필수"라고 적었으나, 실제로 fileId를 실어 보내면 **넣지 않았을 때 반환되는
        항목이 누락된다**:

            latestRevision=23776 + fileId=<a.txt>  → 0건
            latestRevision=23776                   → 1건 (rev=23778 원격.txt)
            latestRevision=23771 + fileId=<원격.txt> → 3건
            latestRevision=23771                   → 5건

        누락되는 항목이 그 fileId의 것만도 아니어서 서버 의미론은 규명되지 않았다.
        확실한 것은 하나뿐이다 — **fileId를 커서에 실으면 원격 변경을 영구히 놓친다.**
        M1은 changes를 소비하지 않아(push/pull은 목록 API 사용) 이 전제가 한 번도
        실행되지 않았고, M2 델타 모드에서 처음 드러났다.

        같은 revision에 항목이 여럿인 경우의 누락은 커서를 '완전히 소비한 마지막
        revision'까지만 물리는 방식으로 막는다(core/remote.py delta의 절단 처리).
        """
        return {"latestRevision": self.revision}
