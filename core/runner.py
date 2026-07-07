from __future__ import annotations

from .pipeline import PipelineStage, pipeline
from .result import Err, Ok, Result
from .stages import assert_result, collect_output, run_solve, validate_syntax
from .supervisor import APDLSupervisor, RetryPolicy
from .job import APDLJob


class APDLRunner:
    """
    APDLJob 실행 오케스트레이터.

    SRP 기준으로 이 클래스는 실행 흐름 조립만 담당한다.
    - MAPDL 시작/종료/재시도: APDLSupervisor
    - 단계별 처리: core.stages
    - 오류/Result 모델: core.errors, core.result
    - PyMAPDL 연결: core.mapdl_adapter
    """

    def __init__(
        self,
        retry_policy: RetryPolicy | None = None,
        extra_stages: list[PipelineStage] | None = None,
        supervisor: APDLSupervisor | None = None,
    ) -> None:
        self._supervisor = supervisor or APDLSupervisor(retry_policy=retry_policy)
        self._extra_stages = extra_stages or []

    def run(self, job: APDLJob) -> Result:
        stages = [
            PipelineStage("validate_syntax", validate_syntax),
            PipelineStage("run_solve", run_solve),
            PipelineStage("assert_result", assert_result),
            PipelineStage("collect_output", collect_output),
            *self._extra_stages,
        ]
        run_pipeline = pipeline(*stages)

        def _attempt() -> Result:
            start_result = self._supervisor.start_mapdl(job)
            if isinstance(start_result, Err):
                return start_result

            mapdl = start_result.value
            ctx: dict = {"job": job, "mapdl": mapdl}

            watchdog_result = self._supervisor.watchdog.check(mapdl)
            if isinstance(watchdog_result, Err):
                self._supervisor.stop_mapdl()
                return watchdog_result

            try:
                return run_pipeline(ctx)
            finally:
                self._supervisor.stop_mapdl()

        return self._supervisor.supervise(_attempt)


__all__ = ["APDLRunner", "APDLJob", "APDLSupervisor", "PipelineStage", "RetryPolicy", "Ok", "Err", "Result"]
