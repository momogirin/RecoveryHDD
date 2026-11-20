#!/bin/bash
# 더블클릭하면 macOS 비밀번호 창이 뜨고, 이어서 브라우저에서 복구 도구가 열립니다.
# 하는 일: 원본 디스크를 '읽기 전용'으로 읽는 복구 도구를 관리자 권한으로 시작합니다(디스크에 쓰지 않음).
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
if [ -z "$PY" ]; then
  osascript -e 'display dialog "Python 3 가 필요합니다. python.org 에서 설치한 뒤 다시 실행하세요." buttons {"확인"} default button 1 with icon caution'
  exit 1
fi
LOG="$(mktemp "${TMPDIR:-/tmp}/apfsrecover.XXXXXX")"
echo "관리자 암호를 물어보면 입력하세요. (읽기 전용 복구 도구를 시작합니다)"
osascript - "$HERE/_run_as_root.sh" "$(id -un)" "$(id -u)" "$(id -g)" "$LOG" "$PY" "$@" <<'OSA'
on run argv
  set cmd to "/bin/bash"
  repeat with a in argv
    set cmd to cmd & " " & quoted form of (a as text)
  end repeat
  do shell script (cmd & " >/dev/null 2>&1 &") with administrator privileges
end run
OSA
[ $? -ne 0 ] && { echo "관리자 권한을 받지 못해 종료합니다."; exit 1; }
for i in $(seq 1 40); do
  URL="$(grep -o 'http://127.0.0.1:[0-9]*/?token=[A-Za-z0-9_-]*' "$LOG" 2>/dev/null | head -1)"
  [ -n "$URL" ] && break
  sleep 0.5
done
if [ -z "$URL" ]; then echo "도구가 시작되지 않았습니다. 로그: $LOG"; cat "$LOG"; exit 1; fi
open "$URL"
echo "브라우저에서 열렸습니다. 이 창은 닫아도 됩니다."
echo "끌 때는 화면 오른쪽 위의 '도구 종료' 버튼을 누르세요."
