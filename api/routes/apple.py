from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse

from api.job_queue import RUNS_DIR, create_job, get_job, init_db, job_to_payload
from api.schemas import AppleJobStatusResponse, AppleRunAcceptedResponse
from api.security import require_api_key

router = APIRouter(prefix="/apple", tags=["apple"], dependencies=[Depends(require_api_key)])


@router.post(
    "/run/",
    response_model=AppleRunAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="APDL 작업 실행 요청",
    description="APDL [hash].inp 파일과 선택적 .cdb mesh 파일을 저장하고 SQLite queue에 작업을 등록합니다. 실제 실행은 worker가 비동기로 처리합니다.",
)
def run_apple_job(
    macro: UploadFile = File(..., description="실행할 APDL [hash].inp 매크로 파일"),
    mesh: UploadFile = File(None, description="실행에 사용할 선택적 MAPDL mesh .cdb 파일"),
    timeout: int = Form(3600, gt=0, description="실행 제한 시간(초)"),
) -> AppleRunAcceptedResponse:
    init_db()

    uploaded_name = Path(macro.filename or "")
    if uploaded_name.suffix.lower() != ".inp":
        raise HTTPException(status_code=400, detail="macro 파일은 .inp 확장자여야 합니다.")

    mesh_name = Path(mesh.filename or "") if mesh is not None else None
    if mesh_name is not None and mesh_name.suffix.lower() != ".cdb":
        raise HTTPException(status_code=400, detail="mesh 파일은 .cdb 확장자여야 합니다.")

    macro_hash = uploaded_name.stem
    if not macro_hash:
        raise HTTPException(status_code=400, detail="파일명은 [hash].inp 형식이어야 합니다.")

    job_id = str(uuid.uuid4())
    run_dir = RUNS_DIR / job_id
    run_dir.mkdir(parents=True, exist_ok=False)
    script_path = run_dir / f"{macro_hash}.inp"

    with script_path.open("wb") as fp:
        shutil.copyfileobj(macro.file, fp)
    if mesh is not None and mesh_name is not None:
        mesh_path = run_dir / mesh_name.name
        with mesh_path.open("wb") as fp:
            shutil.copyfileobj(mesh.file, fp)

    create_job(
        job_id=job_id,
        macro_hash=macro_hash,
        script_path=script_path,
        run_dir=run_dir,
        timeout=timeout,
    )

    return AppleRunAcceptedResponse(
        job_id=job_id,
        status="QUEUED",
        hash=macro_hash,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=AppleJobStatusResponse,
    summary="APDL 작업 상태 조회",
)
def get_apple_job(job_id: str) -> AppleJobStatusResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job을 찾을 수 없습니다.")
    return AppleJobStatusResponse(**job_to_payload(job, include_results=False))


@router.get(
    "/jobs/{job_id}/result",
    response_model=AppleJobStatusResponse,
    summary="APDL 작업 결과 조회",
)
def get_apple_job_result(job_id: str) -> AppleJobStatusResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job을 찾을 수 없습니다.")
    return AppleJobStatusResponse(**job_to_payload(job, include_results=True))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get(
    "/jobs/{job_id}/events",
    summary="APDL 작업 완료/오류 SSE 구독",
    response_class=StreamingResponse,
)
async def stream_apple_job_events(request: Request, job_id: str) -> StreamingResponse:
    """SSE로 상태를 push하고 SUCCEEDED/FAILED가 되면 최종 payload를 보낸 뒤 종료한다."""

    async def event_stream() -> AsyncIterator[str]:
        last_status: str | None = None
        while True:
            if await request.is_disconnected():
                return

            job = get_job(job_id)
            if job is None:
                yield _sse("error", {"message": "job을 찾을 수 없습니다.", "job_id": job_id})
                return

            include_results = job.is_terminal
            payload = job_to_payload(job, include_results=include_results)

            if job.status != last_status:
                yield _sse("status", payload)
                last_status = job.status

            if job.is_terminal:
                yield _sse("done" if job.status == "SUCCEEDED" else "error", payload)
                return

            await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.websocket("/jobs/{job_id}/ws")
async def watch_apple_job(websocket: WebSocket, job_id: str) -> None:
    """작업이 완료/실패하면 최종 상태를 클라이언트에게 push한다."""
    await websocket.accept()
    try:
        while True:
            job = get_job(job_id)
            if job is None:
                await websocket.send_json({"error": "job을 찾을 수 없습니다."})
                await websocket.close(code=1008)
                return

            include_results = job.is_terminal
            await websocket.send_json(job_to_payload(job, include_results=include_results))

            if job.is_terminal:
                await websocket.close(code=1000)
                return

            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
