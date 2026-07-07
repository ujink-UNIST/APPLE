from __future__ import annotations

import logging
import re
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import APDLError, ErrorClassifier, ErrorKind
from .job import APDLJob
from .result import Err, Ok, Result

if TYPE_CHECKING:
    from ansys.mapdl.core.mapdl_grpc import MapdlGrpc

log = logging.getLogger(__name__)


def validate_syntax(ctx: dict) -> Result:
    """스크립트 파일 존재 여부 및 기초 APDL 문법 검사."""
    job: APDLJob = ctx["job"]
    script_path: Path = job.script_path
    if not script_path.exists():
        return Err(APDLError(ErrorKind.IO, f"스크립트 파일 없음: {script_path}"))

    text = script_path.read_text(errors="replace")
    required = [r"FINISH", r"/PREP7|/SOLU|SOLVE"]
    for pat in required:
        if not re.search(pat, text, re.I):
            log.warning("권장 키워드 '%s' 없음 (스크립트 불완전할 수 있음)", pat)

    for check_fn in job.pre_checks:
        result = check_fn(None, ctx)
        if isinstance(result, Err):
            return result

    return Ok(ctx)


def run_solve(ctx: dict) -> Result:
    """MAPDL input()으로 APDL 스크립트 실행."""
    job: APDLJob = ctx["job"]
    mapdl: MapdlGrpc | None = ctx.get("mapdl")
    if mapdl is None:
        return Err(APDLError(ErrorKind.UNKNOWN, "MAPDL 인스턴스가 없음"))

    try:
        output = mapdl.input(str(job.script_path))
        ctx["raw_output"] = output or ""

        kind, code = ErrorClassifier.classify_detail(ctx["raw_output"])
        if kind in (ErrorKind.CONVERGENCE, ErrorKind.SYNTAX):
            return Err(APDLError(kind, "MAPDL 실행 중 오류 감지", code=code, raw_output=ctx["raw_output"]))

        return Ok(ctx)
    except Exception as exc:
        raw = str(exc)
        kind, code = ErrorClassifier.classify_detail(raw)
        return Err(APDLError(kind, raw, code=code, raw_output=raw, tb=traceback.format_exc()))


def assert_result(ctx: dict) -> Result:
    """사용자 정의 post_checks 실행."""
    job: APDLJob = ctx["job"]
    mapdl: MapdlGrpc | None = ctx.get("mapdl")
    for check_fn in job.post_checks:
        result = check_fn(mapdl, ctx)
        if isinstance(result, Err):
            result.error.kind = ErrorKind.ASSERTION
            return result
    return Ok(ctx)


def collect_output(ctx: dict) -> Result:
    """결과 데이터 수집."""
    mapdl: MapdlGrpc | None = ctx.get("mapdl")
    collected: dict = {"raw_output": ctx.get("raw_output", "")}

    if mapdl is not None:
        try:
            collected["max_disp"] = mapdl.post_processing.nodal_displacement("ALL")
        except Exception:
            log.debug("변위 데이터 수집 실패 (무시)")

    ctx["result"] = collected
    return Ok(ctx)
