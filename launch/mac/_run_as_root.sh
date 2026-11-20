#!/bin/bash
# RecoveryTool.command 가 관리자 권한으로 호출한다. 직접 실행하지 마세요.
# 인자: 사용자이름 UID GID 로그파일 python경로 [gui에 넘길 추가 인자...]
export SUDO_USER="$1" SUDO_UID="$2" SUDO_GID="$3"
LOG="$4"; PY="$5"; shift 5
cd "$(dirname "$0")/../.." || exit 1
# 맥이 잠자기에 들어가면 연결이 끊겨 데이터가 손상될 수 있어서 작업 내내 잠자기를 막는다
exec /usr/bin/caffeinate -i "$PY" -B -m apfsrecover gui --no-open "$@" >"$LOG" 2>&1
