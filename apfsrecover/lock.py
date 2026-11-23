"""세션 폴더 잠금. 도구를 두 번 켜서 같은 작업 폴더를 동시에 쓰면 기록이 섞이므로 막는다."""
import os


def _alive_windows(pid):
    # 윈도우의 os.kill(pid, 0) 은 "확인"이 아니라 프로세스를 강제 종료한다. 절대 쓰지 않는다.
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5  # ACCESS_DENIED = 있지만 못 연다 = 살아 있음
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _alive(pid):
    if os.name == "nt":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # 다른 사용자(root)의 프로세스라서 신호를 못 보냄 = 살아 있음
        return True
    except OSError:
        return False
    return True


def acquire(session):
    """잠금 파일을 만든다. 이미 살아 있는 다른 프로세스가 쓰는 중이면 RuntimeError."""
    os.makedirs(session, exist_ok=True)
    path = os.path.join(session, ".lock")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                pid = int(f.read().strip() or 0)
        except (ValueError, OSError):
            pid = 0
        if pid and pid != os.getpid() and _alive(pid):
            raise RuntimeError("복구 도구가 이미 실행 중입니다(프로세스 %d). 열려 있는 창을 쓰거나 '도구 종료'를 누른 뒤 다시 실행하세요." % pid)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return path


def release(path):
    try:
        with open(path, encoding="utf-8") as f:
            mine = f.read().strip() == str(os.getpid())
        if mine:
            os.remove(path)
    except OSError:
        pass
