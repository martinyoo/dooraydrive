"""Drive 엔드포인트 래퍼 (규약 §8).

**영구삭제 API(`DELETE /drive/v1/drives/{d}/files/{f}`)는 의도적으로 미구현이다**
(규약 §0-4, §12-3). 이 프로젝트에서 '삭제'는 휴지통 이동(move → "trash")뿐이며,
복구 불가능한 작업을 코드에 존재시키지 않는 것이 안전 정책의 일부다. 필요해 보이는
상황이 생겨도 여기에 추가하지 말 것.

이 계층은 HTTP를 직접 다루지 않는다 — 전송·envelope 검사·307·rate-limit은 전부
DoorayClient(규약 §7)에 있다. 여기서는 엔드포인트·파라미터·페이징 의미론만 책임진다.

실측 근거 원본: poc/poc_results/{01_auth_drives,02_listing,05_changes,09_net_semantics}.json
"""
from __future__ import annotations

import dataclasses
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..util.paths import ext_path, join_remote, server_name_will_differ, to_nfc
from .client import DoorayApiError, DoorayClient
from .models import ChangeItem, Cursor, RemoteFile

__all__ = ["DriveAPI"]

# 목록 API는 엄격 페이지네이션(실측 09_net_semantics.listing_pagination_strict)이지만
# 서버가 totalCount를 빠뜨리는 경우에 대비해 0건까지 페이징한다. 그 경우의 폭주 방지선.
_MAX_LIST_PAGES = 10_000
# changes를 tip까지 밀 때의 폭주 방지선. 실측 전량 스캔이 75페이지(14,317건)였다.
_MAX_TIP_PAGES = 10_000
_TIP_PAGE_SIZE = 200
# 배증 탐색 상한. 60회면 2^60까지 커버하므로 실제로는 도달할 일이 없다.
_MAX_TIP_PROBES = 60

# DriveAPI.download가 쓰는 임시 디렉터리 이름. dest와 **같은 볼륨**이어야
# os.replace가 원자적이다(규약 §8 C1).
TMP_DIR_NAME = ".dooraysync_tmp"

# 실측: 이미 휴지통에 있는 항목의 move는 HTTP 200 + 이 resultCode로 실패한다
# (PoC-09에서 8건 관측). 호출측이 '이미 처리됨'으로 관용 처리할 수 있게 공개한다.
NO_ACCESS_AUTHORITY = -15700100


def _q(value: Any) -> str:
    """쿼리스트링에 끼워 넣는 식별자 이스케이프."""
    return quote(str(value or ""), safe="")


class DriveAPI:
    """Drive 엔드포인트 래퍼. 영구삭제(DELETE) 메서드는 의도적으로 미구현."""

    def __init__(self, client: DoorayClient) -> None:
        self.client = client

    @property
    def logger(self):
        return self.client.logger

    # ------------------------------------------------------------------
    # 드라이브
    # ------------------------------------------------------------------
    def list_drives(
        self, type_: str = "private", scope: str | None = None, state: str = "active"
    ) -> list[dict]:
        """실측(01_auth_drives): type=private / type=project&scope=private|public 3종."""
        params: dict[str, Any] = {"type": type_}
        if scope:
            params["scope"] = scope
        if state:
            params["state"] = state
        body = self.client.api("GET", "/drive/v1/drives", params=params)
        result = body.get("result")
        return [d for d in result if isinstance(d, dict)] if isinstance(result, list) else []

    def get_drive(self, drive_id: str) -> dict:
        body = self.client.api("GET", f"/drive/v1/drives/{_q(drive_id)}")
        result = body.get("result")
        return result if isinstance(result, dict) else {}

    def find_root_folder(self, drive_id: str) -> str:
        """드라이브 최상위 폴더 id.

        실측(02_listing): parentId 없이 조회하면 root와 trash 두 개가 나오고
        구분자는 subType이다. 이름('root')이 아니라 subType으로 판별한다.
        """
        body = self.client.api(
            "GET",
            f"/drive/v1/drives/{_q(drive_id)}/files",
            params={"type": "folder", "subTypes": "root", "page": 0, "size": 10},
        )
        for d in body.get("result") or []:
            rf = RemoteFile.from_api(d, drive_id=drive_id)
            if rf.sub_type == "root" and rf.id:
                return rf.id
        raise DoorayApiError(
            f"root 폴더를 찾지 못했습니다 (drive_id={drive_id})",
            path=f"/drive/v1/drives/{drive_id}/files",
        )

    # ------------------------------------------------------------------
    # 목록 — 실측: 목록 API는 엄격 페이지네이션(totalCount 제공, 요청 수만큼 반환)
    # ------------------------------------------------------------------
    def list_children(
        self, drive_id: str, parent_id: str, page: int = 0, size: int = 100
    ) -> tuple[list[RemoteFile], int]:
        """(항목들, totalCount) 반환.

        totalCount가 응답에 없으면 -1(미상)을 돌려준다 — 0을 돌려주면 호출측의
        '누적 >= total' 종료 조건이 즉시 참이 되어 첫 페이지만 읽고 끝난다.
        """
        params: dict[str, Any] = {"page": page, "size": size}
        if parent_id:
            params["parentId"] = parent_id
        body = self.client.api(
            "GET", f"/drive/v1/drives/{_q(drive_id)}/files", params=params
        )
        raw = body.get("result")
        items = [
            RemoteFile.from_api(d, drive_id=drive_id, parent_id=parent_id)
            for d in (raw if isinstance(raw, list) else [])
            if isinstance(d, dict)
        ]

        total_raw = body.get("totalCount")
        total = -1
        if isinstance(total_raw, (int, float)) and not isinstance(total_raw, bool):
            total = int(total_raw)
        elif isinstance(total_raw, str):
            try:
                total = int(total_raw.strip())
            except ValueError:
                total = -1
        return items, total

    def iter_children(
        self, drive_id: str, parent_id: str, size: int = 100
    ) -> Iterator[RemoteFile]:
        """0건 또는 누적 >= totalCount까지 페이징.

        `len(items) < size`를 종료 조건으로 쓰지 않는다 — 목록 API는 실측상 엄격
        페이지네이션이지만, changes에서 그 가정이 무너진 전례(R11)가 있어 종료 조건을
        보수적으로 통일한다.
        """
        seen = 0
        for page in range(_MAX_LIST_PAGES):
            items, total = self.list_children(drive_id, parent_id, page=page, size=size)
            if not items:
                return
            for it in items:
                yield it
            seen += len(items)
            if total >= 0 and seen >= total:
                return
        raise DoorayApiError(
            f"목록 페이징이 {_MAX_LIST_PAGES}페이지를 넘었습니다 "
            f"(drive_id={drive_id} parent_id={parent_id}) — 중단합니다",
            path=f"/drive/v1/drives/{drive_id}/files",
        )

    def walk(
        self, drive_id: str, root_id: str, base_path: str = ""
    ) -> Iterator[tuple[RemoteFile, str]]:
        """루트 하위 전체를 BFS 순회. (RemoteFile, 전체경로) 튜플.

        subType == 'trash' 폴더는 순회에서 제외한다 — 휴지통 내용을 로컬로 끌어오면
        '원격 삭제'가 되레 파일 부활로 나타난다.
        반환되는 RemoteFile의 parent_path는 순회 중 조립한 경로로 채워 준다
        (목록 API 응답에는 path가 없다 — 실측 02_listing).
        """
        start = base_path or "/"
        queue: list[tuple[str, str]] = [(root_id, start)]
        visited: set[str] = {root_id}

        while queue:
            folder_id, cur_path = queue.pop(0)
            for child in self.iter_children(drive_id, folder_id):
                if child.sub_type == "trash":
                    continue
                enriched = dataclasses.replace(
                    child, parent_path=cur_path, parent_id=child.parent_id or folder_id
                )
                full = join_remote(cur_path, enriched.name)
                yield enriched, full
                if enriched.is_dir and enriched.id and enriched.id not in visited:
                    # id 기준 방문 표시 — 서버가 순환 구조를 주더라도 무한 순회하지 않는다.
                    visited.add(enriched.id)
                    queue.append((enriched.id, full))

    def get_file_meta(self, drive_id: str, file_id: str) -> RemoteFile:
        body = self.client.api(
            "GET",
            f"/drive/v1/drives/{_q(drive_id)}/files/{_q(file_id)}",
            params={"media": "meta"},
        )
        result = body.get("result")
        return RemoteFile.from_api(result if isinstance(result, dict) else {}, drive_id=drive_id)

    def find_child_by_name(
        self, drive_id: str, parent_id: str, name: str
    ) -> RemoteFile | None:
        """NFC 정규화 후 비교. 대소문자는 구분한다(서버는 구분하므로).

        업로드 직전 D1 분기(신규 POST / 새 버전 PUT)의 판정에 쓴다.
        """
        target = to_nfc(name)
        for child in self.iter_children(drive_id, parent_id):
            if to_nfc(child.name) == target:
                return child
        return None

    # ------------------------------------------------------------------
    # 전송
    # ------------------------------------------------------------------
    def download(
        self,
        drive_id: str,
        file_id: str,
        dest: Path,
        *,
        expected_size: int | None = None,
        expected_md5: str | None = None,
        pre_replace_guard: Callable[[], None] | None = None,
    ) -> dict:
        """C1 원자적 다운로드: 같은 볼륨 임시파일 → 검증 → os.replace.

        임시파일을 dest.parent 아래에 두는 이유는 os.replace가 **같은 볼륨** 안에서만
        원자적이기 때문이다(%TEMP%는 다른 드라이브일 수 있다).
        검증(크기/MD5)에 실패하면 임시파일을 지우고 DoorayApiError를 올린다 — 깨진
        내용이 dest 자리에 앉는 경우는 없다.

        C2: `pre_replace_guard`는 **os.replace 바로 직전**에 호출된다. 호출측이 여기서
        로컬 파일을 재-stat해 계획 시점과 달라졌으면 예외를 올린다. 전송 시작 전에만
        검사하면 전송에 걸린 시간(대용량은 수 분) 동안 사용자가 저장한 편집이
        경고 없이 덮여 사라진다. 가드가 예외를 올리면 tmp만 지우고 dest는 건드리지 않는다.
        반환: {'bytes', 'md5', 'redirect_host'}
        """
        dest = Path(dest)
        tmp_dir = dest.parent / TMP_DIR_NAME
        os.makedirs(ext_path(dest.parent), exist_ok=True)
        os.makedirs(ext_path(tmp_dir), exist_ok=True)
        tmp = tmp_dir / f"{uuid.uuid4().hex}.part"

        path = f"/drive/v1/drives/{_q(drive_id)}/files/{_q(file_id)}?media=raw"
        try:
            info = self.client.download_to(path, tmp)
            written = int(info.get("bytes") or 0)
            md5 = str(info.get("md5") or "")
            content_length = info.get("content_length")

            # 전송이 조용히 잘렸는지부터 본다 — 서버가 알려준 길이와 실제 기록량 비교.
            if isinstance(content_length, int) and content_length != written:
                raise DoorayApiError(
                    f"다운로드 크기 불일치(Content-Length): file_id={file_id} "
                    f"expected={content_length} actual={written}",
                    path=path,
                )
            if expected_size is not None and written != expected_size:
                raise DoorayApiError(
                    f"다운로드 크기 불일치: file_id={file_id} "
                    f"expected={expected_size} actual={written}",
                    path=path,
                )
            if expected_md5:
                want = str(expected_md5).strip().lower()
                if want and md5 != want:
                    raise DoorayApiError(
                        f"다운로드 MD5 불일치: file_id={file_id} "
                        f"expected={want} actual={md5}",
                        path=path,
                    )
            # C2: 교체 직전 재검증. 전송 중에 로컬이 바뀌었으면 여기서 멈춘다.
            if pre_replace_guard is not None:
                pre_replace_guard()
            os.replace(ext_path(tmp), ext_path(dest))
        except BaseException:
            # 검증 실패든 전송 실패든 임시파일은 남기지 않는다(정리 실패는 삼킨다).
            try:
                os.remove(ext_path(tmp))
            except OSError:
                pass
            raise
        finally:
            # 비어 있을 때만 정리된다 — 다른 전송이 쓰고 있으면 그대로 둔다.
            try:
                os.rmdir(ext_path(tmp_dir))
            except OSError:
                pass

        return {
            "bytes": written,
            "md5": md5,
            "redirect_host": info.get("redirect_host"),
        }

    def remote_md5(self, drive_id: str, file_id: str, tmp_dir: Path) -> str:
        """원격 내용의 MD5를 구한다. 로컬 파일은 건드리지 않는다.

        목록·메타 응답에는 hash가 없고(실측) changes에서만 오므로, 특정 파일의
        원격 내용을 확인하려면 받아서 직접 계산하는 수밖에 없다. 스트리밍으로
        해시만 뽑고 임시파일은 즉시 지운다.
        """
        os.makedirs(ext_path(tmp_dir), exist_ok=True)
        tmp = Path(tmp_dir) / f"{uuid.uuid4().hex}.verify"
        path = f"/drive/v1/drives/{_q(drive_id)}/files/{_q(file_id)}?media=raw"
        try:
            info = self.client.download_to(path, tmp)
            return str(info.get("md5") or "")
        finally:
            try:
                os.remove(ext_path(tmp))
            except OSError:
                pass

    def upload_new(
        self, drive_id: str, parent_id: str, filename: str, local_path: Path
    ) -> RemoteFile:
        """POST 신규 업로드.

        409(이름 충돌)는 DoorayApiError(status=409)로 그대로 전파한다 — 실측
        (09_net_semantics.upload_409_semantics) 409는 '같은 이름이 이미 있다'는
        순수 이름 충돌 신호이므로, 호출측이 재조회 후 재판정한다(D1).
        반환되는 RemoteFile의 name이 **서버 저장명 정본**이다 — 서버가 앞뒤 공백을
        절삭하므로(R14) 로컬 파일명을 DB에 기록하면 안 된다(규약 §12-7).
        """
        path = f"/drive/v1/drives/{_q(drive_id)}/files?parentId={_q(parent_id)}"
        body = self.client.upload_file("POST", path, filename, local_path)
        result = body.get("result")
        return RemoteFile.from_api(
            result if isinstance(result, dict) else {},
            drive_id=drive_id,
            parent_id=parent_id,
        )

    def upload_version(
        self, drive_id: str, file_id: str, filename: str, local_path: Path
    ) -> dict:
        """PUT ?media=raw — 기존 파일의 새 버전.

        실측(04_upload): 응답 result에는 {'id','version'}만 온다(version 0 → 1).
        이름·경로가 필요하면 호출측이 get_file_meta로 다시 읽는다.
        """
        path = f"/drive/v1/drives/{_q(drive_id)}/files/{_q(file_id)}?media=raw"
        body = self.client.upload_file("PUT", path, filename, local_path)
        result = body.get("result")
        rf = RemoteFile.from_api(result if isinstance(result, dict) else {}, drive_id=drive_id)
        return {"id": rf.id or file_id, "version": rf.version}

    # ------------------------------------------------------------------
    # 조작
    # ------------------------------------------------------------------
    def create_folder(self, drive_id: str, parent_folder_id: str, name: str) -> str:
        """폴더를 만들고 id를 반환. 저장명 정본이 필요하면 create_folder_full을 쓴다."""
        return self.create_folder_full(drive_id, parent_folder_id, name).id

    def create_folder_full(self, drive_id: str, parent_folder_id: str,
                           name: str) -> RemoteFile:
        """폴더 생성 후 **서버 저장명까지** 확정해서 돌려준다.

        실측(R14): 서버는 이름의 앞뒤 공백을 절삭하고 '"'를 '%22'로 바꾼다.
        create-folder 응답에는 id만 오는 경우가 있어, 이름이 바뀔 것으로 예상되는데
        응답에 name이 없으면 메타를 한 번 되물어 실제 저장명을 확정한다.
        로컬 이름을 정본으로 기록하면 다음 push가 같은 폴더를 다시 만들려 한다.
        """
        wanted = to_nfc(name)
        path = f"/drive/v1/drives/{_q(drive_id)}/files/{_q(parent_folder_id)}/create-folder"
        body = self.client.api("POST", path, json={"name": wanted})
        result = body.get("result")
        rf = RemoteFile.from_api(result if isinstance(result, dict) else {},
                                 drive_id=drive_id, parent_id=parent_folder_id)
        if not rf.id:
            raise DoorayApiError(f"폴더 생성 응답에 id가 없습니다 (name={name})", path=path)

        if not rf.name:
            if server_name_will_differ(wanted):
                # 서버가 이름을 바꿨을 가능성이 있으면 추정하지 말고 되묻는다.
                try:
                    return self.get_file_meta(drive_id, rf.id)
                except DoorayApiError:
                    pass  # 되묻기 실패 시 아래에서 요청명으로 대체
            rf = dataclasses.replace(rf, name=wanted)
        return rf

    def rename(self, drive_id: str, file_id: str, new_name: str) -> None:
        self.client.api(
            "PUT",
            f"/drive/v1/drives/{_q(drive_id)}/files/{_q(file_id)}?media=meta",
            json={"name": to_nfc(new_name)},
        )

    def move(self, drive_id: str, file_id: str, destination_file_id: str) -> None:
        self.client.api(
            "POST",
            f"/drive/v1/drives/{_q(drive_id)}/files/{_q(file_id)}/move",
            json={"destinationFileId": destination_file_id},
        )

    def move_to_trash(self, drive_id: str, file_id: str) -> None:
        """이 프로젝트의 유일한 '삭제'. 영구삭제 API는 구현하지 않는다.

        실측: 폴더를 휴지통으로 보내면 하위에 재귀 적용된다 — 하위 항목마다 move를
        또 부르면 안 된다(C5).
        이미 휴지통에 있는 항목의 move는 HTTP 200 + resultCode=-15700100으로
        실패하므로, 호출측은 `DoorayApiError.result_code == NO_ACCESS_AUTHORITY`를
        '이미 처리됨'으로 관용 처리할 수 있다.
        """
        self.move(drive_id, file_id, "trash")

    # ------------------------------------------------------------------
    # 변경 추적 — B1~B3
    # ------------------------------------------------------------------
    def get_changes(
        self, drive_id: str, cursor: Cursor, size: int = 200
    ) -> tuple[list[ChangeItem], Cursor]:
        """changes 한 페이지. (항목들, 다음 커서) 반환.

        실측: 항목은 revision 오름차순·단조 비내림차순이고 커서는 배타적이므로,
        다음 커서는 마지막 항목의 (revision, file_id)다. 응답에 totalCount는 없다.
        """
        path = f"/drive/v2/drives/{_q(drive_id)}/changes"
        params: dict[str, Any] = {"size": size, **cursor.as_params()}
        body = self.client.api("GET", path, params=params)
        raw = body.get("result")
        items = [
            ChangeItem.from_api(d)
            for d in (raw if isinstance(raw, list) else [])
            if isinstance(d, dict)
        ]
        if not items:
            return items, cursor

        last = items[-1]
        if last.revision < cursor.revision:
            # 커서가 뒤로 가면 이미 처리한 구간을 재생하게 된다. 전진만 허용하고
            # 미전진 상태로 돌려 호출측(iter_changes)이 중단하게 만든다.
            self.logger.warning(
                "changes 커서 역행 감지: cursor=%s last=%s — 커서를 유지합니다",
                cursor.revision, last.revision,
            )
            return items, cursor
        return items, Cursor(revision=last.revision, file_id=last.file_id or None)

    def iter_changes(
        self, drive_id: str, cursor: Cursor, size: int = 200, max_pages: int = 1000
    ) -> Iterator[tuple[ChangeItem, Cursor]]:
        """**B1: 종료 조건은 0건 응답뿐.**

        `len(items) < size`로 끊으면 커서가 과거에 갇혀 이후 모든 변경을 영구
        누락한다(실측 R11: size=5 요청에 3건만 반환됐는데 뒤에 1.4만 건이 남아 있었다).
        이 함정이 1차 PoC를 전부 실패시켰다 — 규약 §12-1의 첫 번째 금지 항목이다.

        각 항목과 함께 '그 항목까지 처리한' 커서를 내보내므로, 호출측은 항목 단위로
        커서를 저장해 중단 지점부터 재개할 수 있다.
        """
        cur = cursor
        for _ in range(max_pages):
            items, nxt = self.get_changes(drive_id, cur, size=size)
            if not items:
                return  # ← 유일한 정상 종료 (B1)
            for it in items:
                yield it, Cursor(revision=it.revision, file_id=it.file_id or None)
            if nxt == cur:
                # 커서 미전진 = 같은 페이지를 영원히 다시 받는 상태. 무한루프 방지(B2).
                self.logger.warning(
                    "changes 커서가 전진하지 않아 중단합니다 (revision=%s file_id=%s)",
                    cur.revision, cur.file_id,
                )
                return
            cur = nxt
        self.logger.warning(
            "changes 페이징이 max_pages(%d)에 도달해 중단합니다 — 다음 실행에서 이어집니다",
            max_pages,
        )

    def _has_changes_after(self, drive_id: str, revision: int) -> bool:
        """revision 이후에 변경 항목이 있는지만 본다(size=1이라 싸다)."""
        items, _ = self.get_changes(drive_id, Cursor(revision=revision), size=1)
        return bool(items)

    def advance_to_tip(self, drive_id: str, cursor: Cursor) -> Cursor:
        """항목을 버리고 커서만 live tip까지 전진(init에서 사용).

        초기화는 목록 API로 현재 상태를 만들고(B5), changes는 '지금부터'만 보면 된다.
        과거 이력을 재생하지 않는 대신 커서만 끝까지 민다.

        전량 페이징 대신 **이분 탐색**을 쓴다. 실측: changes 응답 지연은 revision 위치가
        아니라 결과 건수에 비례한다(size=1 약 0.2초 vs size=200 약 5.6초). 1.4만 건
        드라이브에서 전량 페이징은 75페이지 약 6분이 걸리지만, size=1 탐침 약 35회면
        10초 안에 끝난다.

        안전성: 탐침이 실제보다 적게 반환해도(§3.1 부분 페이지) 경계가 tip **아래로만**
        잡힌다. 커서가 낮으면 이미 아는 변경을 한 번 더 볼 뿐이라 무해하다. 반대로
        tip 위로 잡히는 일은 원리상 불가능하다(tip 위에는 항목이 없다).
        """
        lo = max(0, int(cursor.revision or 0))
        if not self._has_changes_after(drive_id, lo):
            return Cursor(revision=lo)

        # 1) 위쪽 경계를 배증으로 찾는다 — 0건이 나오는 지점.
        hi = max(lo, 1) * 2
        for _ in range(_MAX_TIP_PROBES):
            if not self._has_changes_after(drive_id, hi):
                break
            lo, hi = hi, hi * 2
        else:
            self.logger.warning("tip 상한 탐색이 한계에 도달했습니다 (revision=%s)", hi)
            return Cursor(revision=lo)

        # 2) lo(항목 있음)와 hi(0건) 사이에서 경계를 좁힌다.
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self._has_changes_after(drive_id, mid):
                lo = mid
            else:
                hi = mid

        # 3) 확인 사살 — 넉넉한 size로 정말 비었는지 본다. 남아 있으면 그때만 페이징.
        items, nxt = self.get_changes(drive_id, Cursor(revision=hi), size=_TIP_PAGE_SIZE)
        if not items:
            return Cursor(revision=hi)
        self.logger.info("이분 탐색 경계 이후에도 항목이 남아 페이징으로 마무리합니다")
        cur = nxt
        for _ in range(_MAX_TIP_PAGES):
            items, nxt = self.get_changes(drive_id, cur, size=_TIP_PAGE_SIZE)
            if not items:
                return cur
            if nxt == cur:
                self.logger.warning("커서가 전진하지 않아 중단 (revision=%s)", cur.revision)
                return cur
            cur = nxt
        return cur
