# TODO

## 완료

- [x] 입력 형식을 `.zip` 하나로 확정
- [x] `POST /apple/run/`의 `macro`/`mesh` 입력을 `archive` 하나로 변경
- [x] 작업별 `runs/{job_id}` 폴더에 ZIP 압축 해제
- [x] 경로 탈출, 심볼릭 링크, 암호화, 중복 경로, 파일 개수와 용량 제한 검증
- [x] ZIP 루트의 `setup.apdl` 존재 여부 검증
- [x] 기존 `[hash].inp`, `.cdb`, `hash` 전제 제거
- [x] 작업 폴더에서 ANSYS 배치 프로세스 직접 실행
- [x] `ANSYS_EXE`와 `ANSYS_NP` 서버 설정 지원
- [x] timeout, 종료 코드, `solve.out` 기반 실패 처리
- [x] 근본 오류와 결과 누락을 순서대로 반환 (`errors`: `E101`, `E501` 등)
- [x] `results.csv` 생성 검증 및 API/SSE/WebSocket 반환
- [x] DB 스키마 마이그레이션에서 기존 작업 이력 보존
- [x] 응답 스키마를 `2.0`으로 갱신
- [x] 미사용 PyMAPDL 실행 계층과 `ansys-mapdl-core` 의존성 제거
- [x] 정상 실행, 잘못된 ZIP, 경로 탈출, `setup.apdl` 누락, 실패, timeout, 결과 누락 테스트
- [x] README API 예제와 실행 스크립트 갱신

## 배포 전 확인

- [ ] 실제 ANSYS 설치 환경에서 샘플 ZIP으로 `results.csv` 생성까지 확인

---

## LatSim outbound ANSYS agent 전환

### 목표 구조

APPLE을 외부에서 ZIP을 받는 독립 실행 API가 아니라, LatSim HTTPS API에서 작업을
claim하는 outbound agent로 전환한다. 외부 APPLE host에는 Redis/Celery 접속 권한이나
inbound HTTP port를 주지 않는다.

```text
APPLE agent -> HTTPS long poll/claim -> LatSim API
APPLE agent -> HTTPS setup ZIP download -> local MAPDL
APPLE agent -> HTTPS results.csv upload -> LatSim API
```

LatSim 내부 Redis/Celery는 서버 내부 작업에만 사용한다. ANSYS 실행은 빈도가 낮고 오래
걸리므로 외부 broker 공개 대신 HTTPS claim + lease protocol을 사용한다.

### A. Agent process

- [x] APPLE API와 분리된 단일 outbound agent process를 만들고 동시에 한 solve만 실행한다.
- [x] Agent는 `LATSIM_API_URL`, `LATSIM_WORKER_ID`, `LATSIM_WORKER_KEY_ID`,
  `LATSIM_WORKER_PRIVATE_KEY_PATH`를 설정으로 받고 HTTPS 이외의 remote URL을 거부한다.
- [ ] 유휴 상태에서는 `POST /worker/analysis/claim`을 long polling하고 `204 No Content`이면
  제한된 간격 후 다시 요청한다.
- [x] Windows 실행 script를 추가하고 시작할 때 ANSYS executable 존재 여부, 버전,
  `ANSYS_NP`, API URL, key ID와 Ed25519 private key를 검증한다. 하나라도 유효하지 않으면
  agent를 시작하지 않는다.
- [x] 하나의 agent process는 하나의 solve만 실행해 license concurrency를 제한한다.

### B. Claim and lease contract

- [x] Claim 응답은 다음 versioned payload만 허용한다.

```json
{
  "schema_version": 1,
  "analysis_job_id": "analysis-<hash>",
  "setup_job_id": "setup-<hash>",
  "setup_bundle_sha256": "<sha256>",
  "analysis_type": "periodic_static | periodic_modal",
  "timeout_seconds": 3600,
  "attempt": 1,
  "lease_token": "<opaque-token>",
  "lease_expires_at": "<UTC timestamp>"
}
```

- [x] 누락/추가 필드, 잘못된 job ID, 지원하지 않는 schema/analysis type, 비양수 timeout,
  만료된 lease를 MAPDL 실행 전에 거부한다.
- [x] Claim payload의 임의 URL, command, executable path, shell argument는 허용하지 않는다.
  Download/upload endpoint와 MAPDL command는 APPLE 설정 및 코드로 고정한다.
- [x] 기본 heartbeat 간격은 30초, lease TTL은 120초로 두고 heartbeat 성공 때마다 lease를
  갱신한다. 값은 LatSim 응답 contract가 소유한다.

### C. LatSim job claim 및 setup download

- [x] Claim 성공 자체가 LatSim job을 `STARTED`로 전환하도록 하고, APPLE은 반환된
  `attempt`와 `lease_token` 없이는 실행하지 않는다.
- [x] `GET /worker/analysis/{analysis_job_id}/setup`에 lease token을 보내 setup ZIP을 내려받는다.
- [x] 응답 크기에 상한을 두고 setup ZIP을 저장한 뒤
  `setup_bundle_sha256`을 검증한다. Hash가 다르면 압축을 풀거나 실행하지 않는다.
- [x] 기존 안전한 ZIP 검증을 재사용해 path traversal, symlink, 암호화, 중복 경로, 항목 수와
  압축 해제 용량 초과를 거부한다.
- [x] ZIP 루트에 `setup.apdl`, `mesh.cdb`, `periodic_pairs.dat`가 있는지 확인한다.
- [x] 작업 폴더는 `runs/{analysis_job_id}`로 격리한다. MAPDL은 반드시 이 폴더를 `cwd`로
  사용하며 프로젝트 루트에 `file.lock`, `cleanup-ansys-*`, `file*.out`을 만들지 않는다.

### D. MAPDL 실행 및 재시도 안전성

- [x] 기존 `execute_ansys()`와 오류 분류기를 재사용하되 `shell=False`, 고정 executable,
  고정 APDL filename을 유지한다.
- [x] jobname은 검증된 `analysis_job_id` hash에서 만든 짧은 값으로 지정한다.
- [x] timeout, 비정상 exit code, 라이선스/수렴/메모리/I/O/문법 오류, `results.csv` 누락을
  현재 APPLE error code로 명시적으로 분류한다.
- [x] 성공한 `results.csv`가 이미 로컬에 있고 source setup hash와 attempt가 일치하면 lease
  재할당 또는 agent 재시작 시 MAPDL을 다시 실행하지 않고 upload만 재시도한다.
- [x] 실패 또는 hash 불일치 결과를 성공 결과로 재사용하지 않는다.
- [x] `periodic_static`은 `step,sub,time,out,comp,set,val`, `periodic_modal`은 `mode,freq`
  header를 요구한다. 빈 파일, 비정상 header, 비유한 숫자를 업로드하지 않는다.

### E. 결과 및 실패 보고

- [x] 성공 시 `PUT /worker/analysis/{analysis_job_id}/result`에 `results.csv` bytes만 전송한다.
  Content-Type은 `text/csv`, `X-Result-SHA256` header에는 전송 bytes의 SHA-256을 넣는다.
- [x] LatSim 서버가 result upload를 성공 응답할 때만 local execution을 완료 처리한다.
- [x] 동일 analysis job에 동일 hash를 다시 upload하는 것은 성공으로 처리하고, 이미 성공한
  job에 다른 hash를 upload하면 conflict로 처리한다.
- [x] 실패 시 `POST /worker/analysis/{analysis_job_id}/failure`에 `error_code`, `error_kind`, 제한된
  길이의 `error_message`를 전송한다. Solver output 전체나 host 경로는 전송하지 않는다.
- [ ] LatSim API 통신 실패는 제한된 exponential backoff로 재시도한다. Lease를 갱신할 수
  없으면 MAPDL process를 중단하고 결과를 성공으로 업로드하지 않는다.

### F. Worker 등록과 heartbeat

- [x] 시작 시 worker id, supported analysis types, ANSYS version, `ANSYS_NP`를 LatSim worker
  endpoint에 등록한다.
- [x] 유휴 상태와 실행 중 주기적으로 worker/lease heartbeat를 보낸다.
- [ ] LatSim은 heartbeat TTL로 online/offline을 표시한다. Worker registry는 관측용으로만
  사용하고 실제 단일 할당은 DB claim transaction과 lease token으로 보장한다.
- [ ] Worker 종료 시 best-effort offline 알림을 보내되, 알림 실패는 TTL 만료로 처리한다.

### G. Ed25519 인증 및 네트워크

- [ ] APPLE host에서 worker별 Ed25519 keypair를 생성한다. Private key는 파일 ACL로 해당
  service account만 읽게 하고 LatSim에는 public key만 provision한다.
- [x] `cryptography`의 Ed25519 구현을 사용하고 임의 자체 암호 구현은 만들지 않는다.
- [x] 모든 `/worker/*` 요청 body의 SHA-256을 먼저 계산하고 매 요청마다 UTC Unix timestamp와
  cryptographically random nonce를 새로 생성한다.
- [x] 다음 canonical UTF-8 message를 private key로 서명한다:

```text
latsim-worker-v1
<METHOD>
<PATH_AND_QUERY>
<UNIX_TIMESTAMP>
<NONCE>
<LOWERCASE_BODY_SHA256>
```

- [x] `X-Worker-ID`, `X-Key-ID`, `X-Timestamp`, `X-Nonce`, `X-Content-SHA256`, URL-safe base64
  `X-Signature` header를 전송한다. HTTP retry마다 새 timestamp/nonce/signature를 생성한다.
- [x] Private key bytes와 signature를 로그, claim payload, DB, error message에 기록하지 않는다.
- [ ] Server clock skew 오류를 구분 가능한 local 진단으로 남기되 server의 generic 401 내용을
  인증 실패 원인으로 추측하지 않는다.
- [ ] Key rotation 시 새 key ID/private key를 설치하고 server에 public key를 provision한 뒤
  agent를 전환한다. 확인 후 이전 key를 revoke/delete하며 private key를 자동 전송하지 않는다.
- [x] 운영 환경은 HTTPS 또는 VPN을 사용하고 LatSim 내부 Redis port를 APPLE host나 공용
  인터넷에 노출하지 않는다.
- [x] Ed25519 signing unit test와 server replay contract test를 추가한다.
- [ ] Method/path/query/body 변경,
  nonce 재사용, 잘못된 key와 signature가 거부되는지 검증한다.

### H. 기존 APPLE API 정리

- [ ] 전환 기간에는 기존 `POST /apple/run/` API를 명시적 compatibility mode에서만 유지한다.
- [ ] LatSim claim/lease end-to-end 검증 후 FastAPI ingress, APPLE SQLite queue, SSE/WebSocket result
  API를 제거한다. MAPDL runner와 error classifier는 유지한다.
- [x] LatSim Backend의 ANSYS agent 실행 script는 APPLE로 이동하고 Backend에는 analysis 및
  worker claim/lease API만 남긴다.
- [ ] `runs/{analysis_job_id}` 보존 정책을 정한다. 성공 upload 후 solver 임시 파일은 삭제하고,
  upload 실패 중인 `results.csv`는 재시도를 위해 제한된 기간 보존한다.

### I. 테스트 및 배포

- [x] Claim payload와 result schema validation을 자동 테스트한다.
- [ ] Setup hash mismatch, unsafe ZIP, claim conflict, timeout, solver 오류, upload hash mismatch를
  외부 I/O mock으로 테스트한다.
- [ ] Agent 재시작과 lease 재할당에서 완료된 solve를 다시 실행하지 않고 upload만 재시도하는지
  테스트한다.
- [ ] 동일 hash upload의 idempotency와 다른 hash upload의 conflict를 통합 테스트한다.
- [ ] Fake MAPDL executable로 claim -> download -> execute -> upload 전체 흐름을 로컬에서 검증한다.
- [ ] 실제 ANSYS host 두 대 이상이 동시에 claim해도 한 job을 정확히 한 worker만 받는지 확인한다.
- [x] README에 환경 변수, worker 실행법과 HTTPS 요구사항을 문서화한다.
- [ ] 상태/오류 흐름과 운영 복구 절차를 문서화한다.
