"""sudo 로 실행했을 때 '~' 해석과 결과 파일 소유자를 원래 사용자로 맞춘다."""
import os

try:
    import pwd
except ImportError:  # Windows
    pwd = None


def _is_sudo():
    return hasattr(os, "geteuid") and os.geteuid() == 0 and bool(os.environ.get("SUDO_USER"))


def real_home():
    if _is_sudo() and pwd:
        try:
            return pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def expand(path):
    path = str(path)
    if path == "~" or path.startswith("~/"):
        return real_home() + path[1:]
    return os.path.expanduser(path)


def chown_tree(path):
    """sudo 로 만든 파일/폴더의 소유자를 SUDO_UID/GID 로 되돌린다. sudo 가 아니면 아무것도 안 한다."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not (_is_sudo() and uid and gid) or not os.path.exists(path):
        return
    uid, gid = int(uid), int(gid)
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), uid, gid)
            except OSError:
                pass
