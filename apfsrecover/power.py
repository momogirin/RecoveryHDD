"""전원 상태. 배터리로 돌리다 덮개를 닫거나 배터리가 바닥나면 연결이 끊겨 작업이 멈춘다."""
import subprocess
import sys


def on_battery(runner=subprocess.run, platform=sys.platform):
    """True(배터리) / False(전원 연결) / None(알 수 없음)"""
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
