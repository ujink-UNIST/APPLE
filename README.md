# APPLE ANSYS Agent

운영 모드에서 APPLE은 LatSim HTTPS API를 polling하는 outbound agent입니다. 작업을
claim하면 setup ZIP을 내려받아 ANSYS를 실행하고 `results.csv`만 LatSim에 업로드합니다.
Redis 연결과 inbound HTTP port는 필요하지 않습니다.

기존 ZIP runner API는 전환 기간의 로컬 호환 모드로 유지합니다.

## 실행

```powershell
$env:ANSYS_EXE = "C:\Program Files\ANSYS Inc\v232\ansys\bin\winx64\ANSYS232.exe"
$env:ANSYS_NP = "2"
.\scripts\run_apple.ps1
```

기본값이 위와 같으므로 같은 위치에 ANSYS가 설치되어 있으면 환경 변수는 생략할 수 있습니다.

## Outbound agent 설정과 실행

관리자 PowerShell에서 setup script를 한 번 실행한다. 이 script는 hosts entry와 Caddy root
인증서를 설치하고, conda 환경을 갱신하고, Ed25519 key와 ignored settings file을 생성하고,
LatSim HTTPS 연결을 확인한다.

```powershell
.\scripts\set_agent.ps1 `
  -RootCertPath "C:\path\to\caddy-root.crt" `
  -BackendIp "10.74.19.162" `
  -ApiUrl "https://latsim-backend" `
  -WorkerId "ansys-workstation-01"
```

출력된 public mapping만 LatSim Backend의 `LATSIM_WORKER_PUBLIC_KEYS`에 병합한다. Private key는
APPLE host의 `secrets/worker-key.pem`에만 남는다. Backend 재시작 후에는 별도 환경변수 없이
실행한다.

```powershell
.\scripts\run_agent.ps1
```

Agent는 `claim -> setup download -> heartbeat -> MAPDL -> results.csv upload` 순서로 동작하며
동시에 하나의 해석만 실행합니다. Localhost 이외의 `LATSIM_API_URL`은 HTTPS만 허용합니다.
Canonical protocol은 LatSim Backend의 `docs/ansys-worker-contract.md`에 정의되어 있습니다.

## 기존 호환 API 요청

```bash
curl -X POST http://127.0.0.1:49913/apple/run/ \
  -F "archive=@input.zip" \
  -F "timeout=3600"
```

ZIP 루트에 `setup.apdl`이 있어야 하며, APDL이 같은 작업 폴더에 `results.csv`를 생성해야 합니다.

```bash
curl http://127.0.0.1:49913/apple/jobs/{job_id}
curl http://127.0.0.1:49913/apple/jobs/{job_id}/result
```

실패 응답의 `errors`에는 오류 코드가 원인부터 결과 순서로 담깁니다. 예를 들어 라이선스 오류로 결과가 생성되지 않으면 `["E101", "E501"]`입니다. 기존 `error_code`에는 첫 번째 오류가 유지됩니다.

`APPLE_API_KEY`를 설정한 경우 모든 요청에 `X-API-Key` 헤더를 추가합니다.
