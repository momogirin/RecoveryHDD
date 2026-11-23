"""임시 파일을 목적 파일로 바꿔치기(os.replace).

윈도우에서는 대상 파일을 백신/색인 프로그램이 잠깐 열고 있으면 PermissionError 가 난다.
상태 파일을 자주 저장하므로 아주 짧게 재시도한다. 맥/리눅스에서는 첫 시도에 성공한다.
"""
import os
import time


def replace(src, dst, tries=20, delay=0.05):
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(delay)
