from __future__ import annotations

import asyncio
import json
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, BinaryIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse

from api.job_queue import RUNS_DIR, create_job, get_job, init_db, job_to_payload
from api.schemas import AppleJobStatusResponse, AppleRunAcceptedResponse
from api.security import require_api_key

router = APIRouter(prefix="/apple", tags=["apple"], dependencies=[Depends(require_api_key)])
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000


def _extract_zip(source: BinaryIO, destination: Path) -> None:
    """제한 안에서 ZIP을 풀고 경로 탈출과 심볼릭 링크를 거부한다."""
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError(f"압축 파일 항목은 최대 {MAX_ARCHIVE_ENTRIES}개까지 허용됩니다.")
        if sum(entry.file_size for entry in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError("압축 해제된 파일의 전체 크기가 2 GiB 제한을 초과합니다.")

        seen: set[str] = set()
        validated: list[tuple[zipfile.ZipInfo, Path]] = []
        root = destination.resolve()
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            relative = PurePosixPath(name)
            if not relative.parts or relative.is_absolute() or ".." in relative.parts or any(":" in part for part in relative.parts):
                raise ValueError(f"허용되지 않는 압축 경로입니다: {entry.filename}")
            if entry.flag_bits & 0x1:
                raise ValueError("암호화된 ZIP 파일은 지원하지 않습니다.")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f"심볼릭 링크는 허용되지 않습니다: {entry.filename}")

            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(root):
                raise ValueError(f"작업 폴더를 벗어나는 압축 경로입니다: {entry.filename}")
            key = str(relative).casefold()
            if key in seen:
                raise ValueError(f"중복된 압축 경로입니다: {entry.filename}")
            seen.add(key)
            validated.append((entry, target))

        for entry, target in validated:
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source_file, target.open("wb") as target_file:
                shutil.copyfileobj(source_file, target_file)


@router.post(
    "/run/",
    response_model=AppleRunAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="ANSYS 작업 실행 요청",
    description="setup.apdl과 입력 파일이 들어 있는 ZIP 하나를 저장하고 SQLite queue에 작업을 등록합니다.",
)
def run_apple_job(
    archive: UploadFile = File(..., description="루트에 setup.apdl이 포함된 ZIP 파일"),
    timeout: int = Form(3600, gt=0, description="실행 제한 시간(초)"),
) -> AppleRunAcceptedResponse:
    init_db()

    if Path(archive.filename or "").suffix.lower() != ".zip":
        raise HTTPException(status_code=400, detail="입력 파일은 .zip 확장자여야 합니다.")
    archive.file.seek(0, 2)
    archive_size = archive.file.tell()
    archive.file.seek(0)
    if archive_size > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="압축 파일은 최대 512 MiB까지 허용됩니다.")

    job_id = str(uuid.uuid4())
    run_dir = RUNS_DIR / job_id
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        _extract_zip(archive.file, run_dir)
        if not (run_dir / "setup.apdl").is_file():
            raise HTTPException(status_code=400, detail="압축 파일 루트에 setup.apdl이 없습니다.")
        create_job(job_id=job_id, run_dir=run_dir, timeout=timeout)
    except HTTPException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"ZIP 파일을 처리할 수 없습니다: {exc}") from exc

    return AppleRunAcceptedResponse(job_id=job_id, status="QUEUED")


@router.get(
    "/jobs/{job_id}",
    response_model=AppleJobStatusResponse,
    summary="ANSYS 작업 상태 조회",
)
def get_apple_job(job_id: str) -> AppleJobStatusResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job을 찾을 수 없습니다.")
    return AppleJobStatusResponse(**job_to_payload(job, include_results=False))


@router.get(
    "/jobs/{job_id}/result",
    response_model=AppleJobStatusResponse,
    summary="ANSYS 작업 결과 조회",
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
    summary="ANSYS 작업 완료/오류 SSE 구독",
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

            payload = job_to_payload(job, include_results=job.is_terminal)
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

            await websocket.send_json(job_to_payload(job, include_results=job.is_terminal))
            if job.is_terminal:
                await websocket.close(code=1000)
                return
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
