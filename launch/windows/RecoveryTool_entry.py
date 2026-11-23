"""PyInstaller 가 .exe 로 묶는 시작 파일. (개발 중에는 `python -m apfsrecover gui` 를 쓴다)"""
import os
import sys

if getattr(sys, "frozen", False):
    # 포장된 exe 에서는 콘솔이 없을 수 있다. 출력 대상이 없으면 버린다(오류는 메시지 상자로 알린다).
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

from apfsrecover.desktop import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
