"""관리자 권한 확인(맥/리눅스: root, 윈도우: 관리자)과 작업 중 잠자기 방지."""
import os


def is_admin():
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return False


def keep_awake():
    """윈도우: 이 스레드가 살아 있는 동안 시스템 잠자기를 막는다(맥은 실행 스크립트의 caffeinate 가 담당).
    반환: 막았으면 True."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except (AttributeError, OSError):
        return False
