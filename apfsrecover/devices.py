"""연결된 외장 디스크 목록. 서버는 이 목록에 있는 장치만 읽는다(임의 경로 거부).

macOS: `diskutil list -plist external physical`. Windows 는 아직 미구현(빈 목록).
테스트·시연용 디스크 이미지 파일은 서버 시작 옵션(--image)으로만 등록할 수 있다.
"""
import os
import plistlib
import subprocess
import sys


def _mac_devices(runner):
    out = runner(["diskutil", "list", "-plist", "external", "physical"], capture_output=True, timeout=30)
    if out.returncode != 0:
        return []
    data = plistlib.loads(out.stdout)
    devs = []
    for disk in data.get("AllDisksAndPartitions", []):
        for part in disk.get("Partitions", []):
            content = part.get("Content", "")
            size = part.get("Size", 0)
            if content in ("EFI", "Apple_Boot") or size < 1 << 30:
                continue
            ident = part["DeviceIdentifier"]
            mounts = [part["MountPoint"]] if part.get("MountPoint") else []
            devs.append({"id": ident, "path": "/dev/r" + ident, "size": size, "content": content,
                         "whole_disk": disk.get("DeviceIdentifier"), "mounts": mounts,
                         "label": part.get("VolumeName") or ""})
    return devs


def list_devices(images=None, runner=subprocess.run, platform=sys.platform):
    devs = []
    if platform == "darwin":
        try:
            devs = _mac_devices(runner)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, KeyError):
            devs = []
    for name, path in (images or {}).items():
        devs.append({"id": "image:" + name, "path": path, "size": os.path.getsize(path),
                     "content": "disk-image", "whole_disk": None, "mounts": [], "label": name})
    for d in devs:
        d["readable"] = os.access(d["path"], os.R_OK)
    return devs
