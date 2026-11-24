"""테스트는 컴퓨터에 실제로 꽂힌 디스크와 무관해야 한다(실제 외장하드가 꽂혀 있어도 같은 결과)."""
import os

os.environ["APFSRECOVER_NO_SYSTEM_DISKS"] = "1"
