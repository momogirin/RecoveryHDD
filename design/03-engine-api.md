# 03. 엔진·서버 인터페이스

## 1. 명령줄 (구현됨 + 예정)
```
python3 -m apfsrecover inspect <장치>                       # 구현됨
python3 -m apfsrecover scan <장치> <시작블록> <끝블록> <출력>  # 구현됨
python3 -m apfsrecover survey <장치> <총바이트> <간격MB> <구간GB>  # 구현됨
python3 -m apfsrecover carve-sample ...                     # 구현됨 (테스트용)
python3 -m apfsrecover catalog <장치> <세션폴더> <시작블록> <끝블록>  # P1 구현됨
# extract / verify 는 현재 `gui` 화면과 서버 API 로 실행한다 (명령줄 단독 명령은 미구현)
python3 -m apfsrecover gui [--session 폴더] [--image 이름=경로]  # P2~P5 구현됨
```
모든 명령은 장치를 읽기 전용으로만 연다.

## 2. 세션 폴더 (작업 상태 저장소, 기본 `~/Documents/recovered/session`, 저장소 밖)
`gui --session`으로 지정한다. 복구된 사진·영상을 두는 **저장 폴더(dest)와는 별개**다. 다시 검사하면 옛 파일은 `archive_<시각>/`으로 보관한다.
| 파일 | 내용 |
|---|---|
| `device.json` | 선택한 디스크, 크기, 파티션 오프셋, 검사 시각 |
| `catalog.jsonl` | 파일 후보 한 줄에 하나 |
| `plan.json` | 사용자가 정한 복구 계획 |
| `progress.json` | 완료한 항목 id, 현재 위치, 마지막 오류 |
| `verify.jsonl` | 검증 결과 한 줄에 하나 |

### catalog.jsonl 한 줄
```json
{"id": 1234, "kind": "jpeg", "offset": 105644032, "length": 2373103,
 "structure": "ok", "width": 2976, "height": 1984,
 "taken": "2024-03-06T09:38:35", "model": "Canon EOS R8", "note": ""}
```
- `kind`: `jpeg | raw-cr3 | raw-other | video-mp4 | video-mov | heic | unknown-bmff`
- `structure`: `ok`(구조가 끝까지 맞음) / `suspect`(구조가 어긋나거나 길이가 비정상) / `unknown`
- `taken`, `model`은 EXIF/박스에서 읽을 수 있을 때만 채운다. 못 읽으면 `null`
- 이 값들은 **서명과 구조로 추정한 것**이며 파일이 실제로 열린다는 보증은 아니다. 열림은 verify가 판정한다

### plan.json
```json
{"dest": "/Volumes/Big/recovered",
 "kinds": ["jpeg", "raw-cr3", "video-mp4"],
 "date_from": null, "date_to": null,
 "include_suspect": false,
 "batch": {"unit": "gb", "size": 100},
 "layout": "kind/date/taken"}
```
- `layout`: `kind/date/taken`(촬영 시각 이름, 2026-11-25부터 기본) 또는 `kind/date/kind_seq_blk`(이전 이름). 없으면 이미 꺼낸 파일이 있을 때 이전 방식으로 본다(`07-roadmap.md` D).

### progress.json
```json
{"state": "paused", "batch": 2, "batches": 7, "done_ids": [1, 2, 3],
 "resume_offset": 189565763584, "last_error": "device disconnected"}
```

## 3. 서버 API (P2)
기본: `http://127.0.0.1:<포트>/?token=<실행마다 새로 생성>`. 토큰 없는 요청은 거부한다.

| 메서드 경로 | 설명 |
|---|---|
| `GET /api/devices` | 외장 디스크 목록(내장/시스템 디스크 제외) |
| `POST /api/scan/start` `{device}` | 검사(catalog 생성) 시작 |
| `GET /api/jobs/<id>` | 진행률 `{phase, done_bytes, total_bytes, mb_s, eta_s, found}` |
| `POST /api/jobs/<id>/cancel` | 취소 |
| `GET /api/catalog/summary` | 종류별 개수·용량, 월별 분포, 모델 목록 |
| `POST /api/plan/check` `{plan}` | 저장 폴더 검증과 용량 계산 → `{ok, free, need, margin, reasons[]}` |
| `POST /api/extract/start` | 계획대로 한 묶음 추출 |
| `POST /api/verify/start` | 검증 시작 |
| `GET /api/report` | 개인정보를 가린 텍스트 보고서 |

### 서버가 반드시 거부하는 것
- 장치 목록에 없는 경로를 `device`로 주는 요청
- `dest`가 원본 장치의 마운트 경로 안, 저장소 폴더 안, 시스템 폴더(`/System`, `C:\Windows` 등)인 요청
- 원본 디스크에 쓰라는 어떤 요청도 (그런 엔드포인트는 존재하지 않는다)

## 4. 플랫폼별 `blockdev`
| | macOS (현재) | Windows (P6) |
|---|---|---|
| 장치 | `/dev/rdiskNsM` (파티션) | `\\.\PhysicalDriveN` (전체 디스크) |
| 오프셋 | 파티션 기준 0 | GPT에서 파티션 시작을 읽어 더한다 |
| 읽기 | `os.pread`, 512B 정렬 | `ReadFile`, 섹터 정렬(실측 필요) |
| 권한 | `sudo` | 관리자 |
| 목록 | `diskutil list -plist external` | `Get-Disk` (미검증) |

Windows 항목은 **미검증**이며 실측 전에는 동작을 보장하지 않는다.

## 5. 테스트 계획
- **합성 이미지 테스트**: 작은 가짜 디스크 파일에 JPEG/MP4/CR3 구조를 넣고 catalog/extract/verify가 정확한 길이를 내는지 확인 (실제 디스크 불필요)
- **쓰기 금지 테스트**: 소스에 `O_WRONLY|O_RDWR|pwrite|write 모드 open(장치)`가 없는지 검사
- **경로 가드 테스트**: 저장소 안, 원본 안, 시스템 폴더가 거부되는지
- **중단/재개 테스트**: 중간에 멈춘 뒤 이어서 했을 때 결과가 같은지
