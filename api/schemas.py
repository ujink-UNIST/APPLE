from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"
JobStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]


class AppleRunAcceptedResponse(BaseModel):
    """APPLE APDL 실행 접수 응답."""

    job_id: str = Field(..., description="작업 ID")
    status: JobStatus = Field("QUEUED", description="작업 상태")
    hash: str = Field(..., description="업로드된 [hash].inp 파일명의 hash 부분")
    schema_version: str = Field(SCHEMA_VERSION, description="응답 스키마 버전")


class AppleJobStatusResponse(BaseModel):
    """APPLE APDL 작업 상태/결과 응답."""

    job_id: str = Field(..., description="작업 ID")
    status: JobStatus = Field(..., description="QUEUED/RUNNING/SUCCEEDED/FAILED")
    error_code: str | None = Field(None, description="오류 코드. 예: E101, E201, E501. 성공/대기/실행 중이면 null")
    error_kind: str | None = Field(None, description="오류 종류. 성공/대기/실행 중이면 null")
    hash: str = Field(..., description="업로드된 [hash].inp 파일명의 hash 부분")
    schema_version: str = Field(SCHEMA_VERSION, description="응답 스키마 버전")
    elapsed_sec: float | None = Field(None, ge=0, description="실행 소요 시간(초). 완료 전이면 null")
    results_csv: str | None = Field(None, description="results.txt 내용을 CSV 문자열로 반환. 완료 전/실패/파일 없음이면 null")


AppleRunResponse = AppleJobStatusResponse
