"""전원 상태. 배터리로 돌리다 덮개를 닫거나 배터리가 바닥나면 연결이 끊겨 작업이 멈춘다."""
import subprocess
import sys


def _win_on_battery():
    """GetSystemPowerStatus: ACLineStatus 0=배터리, 1=전원 연결, 255=알 수 없음."""
    try:
        import ctypes
        from ctypes import wintypes

        class Status(ctypes.Structure):
            _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                        ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]
        st = Status()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        if st.BatteryFlag == 128:  # 배터리 없음(데스크톱)
            return False
        return {0: True, 1: False}.get(st.ACLineStatus)
    except (AttributeError, OSError):
        return None


def on_battery(runner=subprocess.run, platform=sys.platform):
    """True(배터리) / False(전원 연결) / None(알 수 없음)"""
    if platform == "win32":
        return _win_on_battery()
    if platform != "darwin":
        return None
    try:
        out = runner(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.splitlines()[0] if out else ""
    if "Battery Power" in first:
        return True
    if "AC Power" in first:
        return False
    return None
