from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

from .errors import APDLError, ErrorClassifier, ErrorKind
from .job import APDLJob
from .mapdl_adapter import MapdlLauncher, PyMAPDLLauncher
from .result import Err, Ok, Result

if TYPE_CHECKING:
    from ansys.mapdl.core.mapdl_grpc import MapdlGrpc

log = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 2.0
    backoff_factor: float = 2.0
    retryable_kinds: set[ErrorKind] = field(default_factory=lambda: {
        ErrorKind.LICENSE,
        ErrorKind.TIMEOUT,
        ErrorKind.UNKNOWN,
    })

    def delays(self):
        delay = self.base_delay
        for _ in range(self.max_attempts - 1):
            yield delay
            delay *= self.backoff_factor

    def is_retryable(self, err: APDLError) -> bool:
        return err.kind in self.retryable_kinds


class LicenseWatchdog:
    """MAPDL 라이선스 상태 확인 책임."""

    def check(self, mapdl: "MapdlGrpc") -> Result:
        try:
            resp = mapdl.run("/STATUS", mute=True) or ""
            if "license" in resp.lower() and "error" in resp.lower():
                return Err(APDLError(ErrorKind.LICENSE, "라이선스 서버 응답 오류", raw_output=resp))
            return Ok(True)
        except Exception as exc:
            kind, code = ErrorClassifier.classify_detail(str(exc))
            return Err(APDLError(kind, str(exc), code=code, tb=traceback.format_exc()))


class APDLSupervisor:
    """MAPDL 수명주기와 재시도 정책만 담당."""

    def __init__(
        self,
        retry_policy: RetryPolicy | None = None,
        watchdog: LicenseWatchdog | None = None,
        launcher: MapdlLauncher | None = None,
    ) -> None:
        self.retry_policy = retry_policy or RetryPolicy()
        self.watchdog = watchdog or LicenseWatchdog()
        self.launcher = launcher or PyMAPDLLauncher()
        self._mapdl: "MapdlGrpc | None" = None

    def start_mapdl(self, job: APDLJob) -> Result:
        result = self.launcher.launch(job)
        if isinstance(result, Ok):
            self._mapdl = result.value
        return result

    def stop_mapdl(self) -> None:
        if self._mapdl is not None:
            try:
                self._mapdl.exit(force=True)
            except Exception:
                log.debug("MAPDL 종료 실패", exc_info=True)
            self._mapdl = None

    def supervise(self, fn: Callable[[], Result]) -> Result:
        last_result: Result = Err(APDLError(ErrorKind.UNKNOWN, "실행되지 않음"))
        delays = list(self.retry_policy.delays())

        for attempt in range(self.retry_policy.max_attempts):
            log.info("시도 %d / %d", attempt + 1, self.retry_policy.max_attempts)
            last_result = fn()

            if isinstance(last_result, Ok):
                return last_result

            err = last_result.error
            log.warning("시도 %d 실패: %s", attempt + 1, err)

            if not self.retry_policy.is_retryable(err):
                log.info("재시도 불가 오류 종류(%s), 중단", err.kind.name)
                break

            if attempt < len(delays):
                wait = delays[attempt]
                log.info("%.1f초 후 재시도...", wait)
                self.stop_mapdl()
                time.sleep(wait)

        return last_result
