# 설계 문서

| 문서 | 내용 |
|---|---|
| [01-plan.md](01-plan.md) | 목표, 원칙, 구조, 단계별 계획, 위험, 미정 사항 |
| [02-ui-spec.md](02-ui-spec.md) | 화면 6개의 흐름과 상태, 문구 규칙 |
| [03-engine-api.md](03-engine-api.md) | 엔진/서버 인터페이스, 데이터 형식(catalog, plan, progress) |
| [04-distribution.md](04-distribution.md) | 맥용·윈도우용 배포 방식과 미구현·미검증 항목 |
| [05-resilience.md](05-resilience.md) | 돌발상황(연결 끊김·전원 차단·다른 디스크 등)별 대응과 검증 상태 |
| [mockup/index.html](mockup/index.html) | 클릭해 볼 수 있는 시안 (브라우저로 열기, 서버 불필요) |

시안의 숫자는 모두 **예시 값**이다. 실제 디스크 결과가 아니다.

## 현재 상태 (2026-11-23)
- 진단 엔진(inspect / scan / survey)과 샘플 추출(carve-sample)은 구현·검증됨
- 목록 만들기·UI·본 복구·검증과 **윈도우 지원 코드(P6)** 까지 구현됨. 실제 디스크 실측은 아직
- 이 폴더는 설계 문서이고, 결정 사항은 01-plan.md 7절
- 실제 진행 기록은 [../reports/NOTES.md](../reports/NOTES.md)
