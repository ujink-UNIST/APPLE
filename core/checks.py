from __future__ import annotations

import re
import traceback

from .errors import APDLError, ErrorKind
from .job import APDLJob, CheckFn
from .result import Err, Ok, Result


def check_file_exists(_mapdl, ctx: dict) -> Result:
    """pre_check: 스크립트 파일 존재 확인."""
    job: APDLJob = ctx["job"]
    if not job.script_path.exists():
        return Err(APDLError(ErrorKind.IO, f"파일 없음: {job.script_path}"))
    return Ok(True)


def check_convergence(_mapdl, ctx: dict) -> Result:
    """post_check: raw_output에 수렴 실패 키워드가 없는지 확인."""
    output = ctx.get("raw_output", "")
    if re.search(r"not converged|convergence failure", output, re.I):
        return Err(APDLError(ErrorKind.CONVERGENCE, "수렴 실패 감지", raw_output=output))
    return Ok(True)


def make_stress_check(max_stress_mpa: float) -> CheckFn:
    """post_check 팩토리: 최대 응력이 임계값 이하인지 확인."""

    def _check(mapdl, _ctx: dict) -> Result:
        if mapdl is None:
            return Ok(True)
        try:
            mapdl.post1()
            mapdl.set("LAST")
            stress = mapdl.post_processing.nodal_eqv_stress().max()
            if stress > max_stress_mpa:
                return Err(APDLError(
                    ErrorKind.ASSERTION,
                    f"최대 응력 {stress:.2f} MPa > 임계값 {max_stress_mpa} MPa",
                ))
            return Ok({"max_stress_mpa": float(stress)})
        except Exception as exc:
            return Err(APDLError(ErrorKind.UNKNOWN, str(exc), tb=traceback.format_exc()))

    return _check
