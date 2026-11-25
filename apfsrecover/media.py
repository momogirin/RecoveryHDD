"""영상 도구(ffmpeg/ffprobe): 꺼낸 영상이 끝까지 재생되는지 확인하고, 결과 화면용 첫 장면을 만든다.

저장 폴더의 파일만 다룬다(원본 장치는 읽지 않는다). ffmpeg 가 없으면 아무것도 하지 않고 None 을 돌려준다
(검증은 구조 확인까지만 하고, 화면은 영상 아이콘을 보인다).

ffmpeg 를 찾는 순서
  1) 환경 변수 RECOVERYHDD_FFMPEG_DIR 의 폴더
  2) 프로그램과 함께 둔 폴더: <프로그램 폴더>/ffmpeg/ 또는 <프로그램 폴더>/tools/ffmpeg/
     (exe 로 묶었으면 exe 가 있는 폴더와 묶음 안, 개발 중이면 저장소 폴더)
  3) PATH
"""
import os
import shutil
import subprocess
import sys

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # 윈도우: 검은 창이 뜨지 않게
END_SECONDS = 3      # 끝에서 이만큼을 실제로 풀어 본다(잘린 영상은 끝이 망가진다)
FRAME_AT = 1.0       # 첫 장면: 처음 화면이 검은 경우가 많아 1초 뒤
_found = {}


def _candidates():
    env = os.environ.get("RECOVERYHDD_FFMPEG_DIR")
    if env:
        yield env
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(sys.executable))
        if getattr(sys, "_MEIPASS", None):
            roots.append(sys._MEIPASS)
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for r in roots:
        yield os.path.join(r, "ffmpeg")
        yield os.path.join(r, "ffmpeg", "bin")
        yield os.path.join(r, "tools", "ffmpeg")
        yield os.path.join(r, "tools", "ffmpeg", "bin")


def find_tool(name):
    """ffmpeg / ffprobe 실행 파일 경로. 없으면 None. 한 번 찾은 결과는 기억한다."""
    if name in _found:
        return _found[name]
    exe = name + (".exe" if os.name == "nt" else "")
    path = None
    for d in _candidates():
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            path = p
            break
    if path is None:
        path = shutil.which(name)
    _found[name] = path
    return path


def _run(args, timeout, text=True):
    return subprocess.run(args, capture_output=True, text=text, timeout=timeout, creationflags=NO_WINDOW,
                          stdin=subprocess.DEVNULL)


def play_check(path, timeout=180):
    """영상의 끝부분(마지막 몇 초)을 실제로 풀어 본다. 반환: (True, "") / (False, 이유) / (None, "") = ffmpeg 없음.
    처음부터 끝까지 다 풀면 오래 걸리므로 끝만 본다. 파일이 잘렸거나 조각이 섞였으면 대개 끝에서 오류가 난다."""
    ff = find_tool("ffmpeg")
    if not ff:
        return None, ""
    try:
        r = _run([ff, "-v", "error", "-nostdin", "-sseof", "-%d" % END_SECONDS, "-i", path, "-f", "null", "-"], timeout)
    except subprocess.TimeoutExpired:
        return False, "재생 확인이 너무 오래 걸림"
    except OSError as e:
        return None, str(e)
    err = (r.stderr or "").strip()
    if r.returncode != 0 or err:
        return False, "끝부분 재생 오류: %s" % (err.splitlines()[-1] if err else "코드 %d" % r.returncode)[:120]
    return True, ""


def video_frame(path, px=240, timeout=60):
    """결과 화면용 첫 장면(JPEG 바이트). 만들 수 없으면 None."""
    ff = find_tool("ffmpeg")
    if not ff:
        return None
    for at in (FRAME_AT, 0):  # 1초보다 짧은 영상은 맨 처음
        try:
            r = _run([ff, "-v", "error", "-nostdin", "-ss", str(at), "-i", path, "-frames:v", "1",
                      "-vf", "scale=%d:-2" % px, "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-"], timeout, text=False)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode == 0 and r.stdout[:3] == b"\xff\xd8\xff":
            return r.stdout
    return None
