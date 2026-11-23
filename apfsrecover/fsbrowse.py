"""저장 폴더 고르기용 폴더 탐색. 폴더 이름만 다루며 파일 내용은 읽지 않는다.
어디에든 둘러볼 수는 있지만, 선택/새 폴더 만들기는 guards.check_dest 를 통과한 곳만 허용한다."""
import os
import shutil
import sys

from .guards import check_dest
from .ownership import chown_tree, expand, real_home

MAX_ENTRIES = 1000
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


def _free(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _problems(path, forbidden):
    """저장 폴더로 쓸 수 없는 이유(공간 부족은 제외 — 공간은 복구 계획에서 따로 본다)."""
    return [r for r in check_dest(path, forbidden, 0)["reasons"] if "여유 공간" not in r]


def places(forbidden=()):
    out = []
    home = real_home()
    for name, sub in (("홈", ""), ("문서", "Documents"), ("바탕화면", "Desktop"), ("다운로드", "Downloads")):
        p = os.path.join(home, sub) if sub else home
        if os.path.isdir(p):
            out.append({"name": name, "path": p, "external": False, "free": _free(p),
                        "problems": _problems(p, forbidden)})
    if os.name == "nt":
        sysdrive = os.environ.get("SystemDrive", "C:").upper()
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            p = letter + ":\\"
            if letter + ":" == sysdrive or not os.path.isdir(p):
                continue
            out.append({"name": "드라이브 " + letter + ":", "path": p, "external": True, "free": _free(p),
                        "problems": _problems(p, forbidden)})
    if sys.platform == "darwin" and os.path.isdir("/Volumes"):
        try:
            root_dev = os.stat("/").st_dev
            for e in sorted(os.listdir("/Volumes"), key=str.lower):
                p = os.path.join("/Volumes", e)
                if os.path.islink(p) or not os.path.isdir(p) or os.stat(p).st_dev == root_dev:
                    continue  # 내장 디스크(Macintosh HD 링크 등)는 제외
                out.append({"name": e, "path": p, "external": True, "free": _free(p),
                            "problems": _problems(p, forbidden)})
        except OSError:
            pass
    return out


def list_dir(path, forbidden=()):
    real = os.path.realpath(expand(path or real_home()))
    if not os.path.isdir(real):
        raise FileNotFoundError("폴더를 찾을 수 없습니다.")
    try:
        names = [n for n in os.listdir(real) if not n.startswith(".")]
    except PermissionError:
        names = []
    dirs = sorted((n for n in names if os.path.isdir(os.path.join(real, n)) and not os.path.islink(os.path.join(real, n))),
                  key=str.lower)
    parent = os.path.dirname(real)
    return {"path": real, "parent": parent if parent != real else None, "dirs": dirs[:MAX_ENTRIES],
            "truncated": len(dirs) > MAX_ENTRIES, "free": _free(real), "writable": os.access(real, os.W_OK),
            "problems": _problems(real, forbidden)}


def make_dir(parent, name, forbidden=()):
    name = (name or "").strip()
    if not name or len(name) > 100 or name in (".", "..") or name.startswith("."):
        raise ValueError("폴더 이름이 올바르지 않습니다.")
    if any(c in name for c in "/\\\0") or any(ord(c) < 32 for c in name) or (
            os.name == "nt" and (any(c in name for c in '<>:"|?*') or name.rstrip(". ") != name
                                 or name.split(".")[0].upper() in WIN_RESERVED)):
        raise ValueError("폴더 이름에 쓸 수 없는 문자가 있습니다.")
    target = os.path.join(os.path.realpath(expand(parent)), name)
    problems = _problems(target, forbidden)
    if problems:
        raise ValueError(" / ".join(problems))
    if os.path.exists(target):
        raise ValueError("같은 이름의 폴더가 이미 있습니다.")
    try:
        os.mkdir(target)  # 한 단계만 만든다
    except OSError as e:
        raise ValueError("폴더를 만들지 못했습니다: %s" % e)
    chown_tree(target)
    return target
