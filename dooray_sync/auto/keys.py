"""자동 동기화 창의 키 입력 — 기다리지 않고 보고, 시간 제한을 두고 묻는다.

input()을 쓰지 않는 이유: 루프는 사람 대기의 인질이 되면 안 된다. input()은 답이
올 때까지 루프 전체를 세운다 — 그동안 다른 모든 폴더의 자동 동기화가 멈춘다.
그래서 콘솔 입력 버퍼를 직접 본다.

**msvcrt(kbhit/getwch)가 아니라 콘솔 입력 기록(ReadConsoleInputW)을 읽는다.**
getwch는 화살표·F키를 두 글자('\\xe0'+코드)로 쪼개 돌려주면서 둘째 글자를 C 런타임
안쪽에 숨겨 두는데, kbhit은 그 숨은 글자를 보지 못한다. 그래서 '쌓인 키 버리기'가
반쪽을 남겼고, Shift+F6 의 둘째 글자가 하필 'Y'라서 미리보기 중에 Shift+F6 을 누른
뒤 Enter 만 쳐도 '예'로 읽혔다(2026-11-10 검토에서 실제 콘솔로 재현). 기록 단위로
읽으면 특수키는 글자가 없는 기록 하나로 와서 통째로 버려진다 — 반쪽이 남을 수 없다.

대화형인지를 isatty로 판정하지 않는다(AGENTS.md — Windows의 NUL은 문자 장치라
stdin=DEVNULL에서도 True다). 콘솔이 없으면 읽을 것이 없고, 묻는 자리는 시간이
지나 '아니오'로 떨어진다 — 방어선은 기본값과 시간 제한이다.
"""
from __future__ import annotations

import sys
import time

__all__ = ["YES_KEYS", "drain", "discard", "ask", "chars_from_records"]

# 대기 중에 '계획 보기'를 여는 키. 한글 입력 상태에서 Y 를 누르면 'ㅛ'가 들어온다
# (2026-09-23 Obsidian 런처 — 이 저장소 밖의 다른 프로그램 — 에서 실측). 여는 것은
# 해가 없으므로(미리보기는 아무것도 바꾸지 않는다) 'ㅛ'도 받는다.
YES_KEYS = frozenset({"y", "Y", "ㅛ"})
# '지울까요?'에 '예'로 받는 답(Enter 로 끝낸 줄, 앞뒤 공백·대소문자 무시).
# 'ㅛ'는 받지 않는다 — 한글 입력기가 조합 중인 'ㅛ'(계획 보기를 열려고 누른 y 의
# 남은 조각일 수 있다)는 다음 Enter 에 확정되어 들어오므로, '아니오'로 치려던 Enter 가
# '예'가 될 수 있다. 대신 영문 y 로 다시 답해 달라고 되묻는다.
_YES_WORDS = frozenset({"y", "yes", "예", "네"})
_HANGUL_Y = frozenset({"ㅛ", "ㅛㅛ"})
_MAX_TRIES = 3

KEY_EVENT = 0x0001


def chars_from_records(records) -> str:
    """콘솔 입력 기록 → 글자. 기록은 (EventType, bKeyDown, uChar, wRepeatCount).

    키를 **누른** 사건 중 글자가 있는 것만 남긴다. 화살표·F키·Shift·Ctrl 같은 키는
    글자가 0 이라 기록째 버리고, 마우스·포커스·창 크기 사건도 버린다.
    """
    out: list[str] = []
    for etype, down, ch, rep in records:
        if etype != KEY_EVENT or not down or not ch or ch == "\x00":
            continue
        try:
            n = int(rep or 1)
        except (TypeError, ValueError):
            n = 1
        out.append(ch * max(1, min(n, 16)))
    return "".join(out)


class _ConsoleInput:
    """현재 콘솔의 입력 버퍼(CONIN$)를 기록 단위로, 기다리지 않고 읽는다."""

    def __init__(self) -> None:
        import ctypes
        import ctypes.wintypes as w

        class KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [("bKeyDown", w.BOOL), ("wRepeatCount", w.WORD),
                        ("wVirtualKeyCode", w.WORD), ("wVirtualScanCode", w.WORD),
                        ("uChar", w.WCHAR), ("dwControlKeyState", w.DWORD)]

        class _EVENT(ctypes.Union):
            _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]

        class INPUT_RECORD(ctypes.Structure):
            _fields_ = [("EventType", w.WORD), ("Event", _EVENT)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = w.HANDLE
        k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                                    w.DWORD, w.DWORD, w.HANDLE]
        k32.GetNumberOfConsoleInputEvents.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        k32.ReadConsoleInputW.argtypes = [w.HANDLE, ctypes.POINTER(INPUT_RECORD),
                                          w.DWORD, ctypes.POINTER(w.DWORD)]
        k32.FlushConsoleInputBuffer.argtypes = [w.HANDLE]
        handle = k32.CreateFileW("CONIN$", 0x80000000 | 0x40000000, 0x1 | 0x2,
                                 None, 3, 0, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError("콘솔 입력을 열 수 없습니다")
        self._ct, self._w, self._k32, self._h = ctypes, w, k32, handle
        self._rec = INPUT_RECORD

    def pending(self) -> int:
        n = self._w.DWORD(0)
        if not self._k32.GetNumberOfConsoleInputEvents(self._h, self._ct.byref(n)):
            return 0
        return int(n.value)

    def read(self) -> str:
        """지금 쌓인 기록을 전부 읽어 글자로. 쌓인 것만 읽으므로 기다리지 않는다."""
        out: list[str] = []
        for _ in range(64):
            n = self.pending()
            if n <= 0:
                break
            buf = (self._rec * min(n, 256))()
            got = self._w.DWORD(0)
            if not self._k32.ReadConsoleInputW(self._h, buf, len(buf), self._ct.byref(got)):
                break
            out.append(chars_from_records(
                (r.EventType, r.Event.KeyEvent.bKeyDown, r.Event.KeyEvent.uChar,
                 r.Event.KeyEvent.wRepeatCount) for r in buf[:got.value]))
        return "".join(out)

    def flush(self) -> None:
        self._k32.FlushConsoleInputBuffer(self._h)


_BACKEND: object | None = None
_BACKEND_TRIED = False


def _console():
    """콘솔 입력 백엔드. 콘솔이 없으면 None — 한 번만 시도한다."""
    global _BACKEND, _BACKEND_TRIED
    if not _BACKEND_TRIED:
        _BACKEND_TRIED = True
        try:
            _BACKEND = _ConsoleInput()
        except Exception:   # noqa: BLE001 — 비Windows·콘솔 없음
            _BACKEND = None
    return _BACKEND


def drain(backend=None) -> str:
    """지금 쌓여 있는 키를 전부 읽어 글자로 돌려준다. **기다리지 않는다.**

    읽을 수 없으면(콘솔 없음·오류) 빈 문자열 — 호출측은 '아무 키도 없음'으로 다룬다.
    """
    b = backend if backend is not None else _console()
    if b is None:
        return ""
    try:
        return b.read() if b.pending() else ""
    except Exception:   # noqa: BLE001 — 키를 못 읽는 것이 루프를 죽이면 안 된다
        return ""


def discard(backend=None) -> None:
    """쌓인 키를 읽지 않고 버린다(타입어헤드 버리기). 실패는 삼킨다."""
    b = backend if backend is not None else _console()
    if b is None:
        return
    try:
        b.flush()
    except Exception:   # noqa: BLE001
        pass


def _write(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass


def ask(question: str, *, timeout: float, cancelled=lambda: False, backend=None,
        clock=time.monotonic, sleep=time.sleep) -> str:
    """기본값 '아니오'인 y/N 질문. 반환: 'yes' | 'no' | 'timeout' | 'cancel' | 'unavailable'.

    - Enter 로 끝낸 한 줄을 판정한다. 빈 줄·그 밖의 답은 'no'.
    - 묻기 **전에** 쌓인 키는 버린다 — 계획 보기를 열려고 누른 y, 미리보기 중에 누른 키,
      '아무 키나 누르세요'에 길든 손이 이 질문의 답이 되면 안 된다.
    - 'ㅛ'(한글 입력 상태의 y)는 예로 받지 않고 영문 y 로 다시 답해 달라고 되묻는다
      (유한 — _MAX_TRIES 번이 지나면 'no').
    - timeout 초 안에 답이 없으면 'timeout'(= 아니오). 사람이 자리를 비워도 루프가
      영원히 서지 않는다.
    - cancelled() 가 참이 되면(Ctrl+C — 러너의 신호 처리기가 세운다) 'cancel'(= 아니오).
      Esc 도 'cancel'.
    """
    b = backend if backend is not None else _console()
    _write(question)
    if b is None:
        _write("\n  (이 창에서는 입력을 받을 수 없어 '아니오'로 둡니다)\n")
        return "unavailable"
    try:
        end = clock() + max(0.0, float(timeout))
    except (TypeError, ValueError):
        end = clock()
    discard(b)
    buf: list[str] = []
    tries = 0
    try:
        while True:
            if cancelled():
                _write("\n  (중단 — '아니오'로 둡니다)\n")
                return "cancel"
            typed = b.read() if b.pending() else ""
            if not typed:
                if clock() >= end:
                    _write("\n  (답이 없어 '아니오'로 둡니다)\n")
                    return "timeout"
                sleep(0.05)
                continue
            for ch in typed:
                if ch in ("\r", "\n"):
                    _write("\n")
                    line = "".join(buf).strip().lower()
                    if line in _YES_WORDS:
                        return "yes"
                    if line in _HANGUL_Y and tries + 1 < _MAX_TRIES:
                        tries += 1
                        buf.clear()
                        _write("  (한글 입력 상태 같습니다 — 영문 y 로 다시 답해 주세요) "
                               + question.strip() + " ")
                        continue
                    return "no"
                if ch in ("\x1b", "\x03"):        # Esc · (처리 입력이 꺼진 콘솔의) Ctrl+C
                    _write("\n  (취소 — '아니오'로 둡니다)\n")
                    return "cancel"
                if ch == "\x08":
                    if buf:
                        buf.pop()
                        _write("\b \b")
                    continue
                if ch.isprintable() and len(buf) < 64:
                    buf.append(ch)
                    _write(ch)
    except Exception:   # noqa: BLE001 — 입력 오류는 '아니오'다
        _write("\n  (입력을 읽지 못해 '아니오'로 둡니다)\n")
        return "unavailable"
