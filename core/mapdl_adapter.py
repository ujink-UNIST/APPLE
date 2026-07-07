from __future__ import annotations

import logging
import tempfile
import traceback
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING, cast, runtime_checkable

from .errors import APDLError, ErrorClassifier, ErrorKind
from .job import APDLJob
from .result import Err, Ok, Result

if TYPE_CHECKING:
    from ansys.mapdl.core.mapdl_grpc import MapdlGrpc

log = logging.getLogger(__name__)

try:
    import ansys.mapdl.core as pymapdl
    from ansys.mapdl.core.mapdl_console import MapdlConsole
    from ansys.mapdl.core.mapdl_grpc import MapdlGrpc

    PYMAPDL_AVAILABLE = True
except ImportError:
    pymapdl = None  # type: ignore[assignment]
    MapdlConsole = None  # type: ignore[assignment]
    MapdlGrpc = None  # type: ignore[assignment]
    PYMAPDL_AVAILABLE = False


@runtime_checkable
class MapdlLauncher(Protocol):
    """MAPDL 시작 책임만 갖는 인터페이스."""

    def launch(self, job: APDLJob) -> Result:
        ...


class PyMAPDLLauncher:
    """PyMAPDL gRPC 인스턴스 시작 어댑터."""

    def launch(self, job: APDLJob) -> Result:
        if not PYMAPDL_AVAILABLE or pymapdl is None:
            return Err(APDLError(ErrorKind.UNKNOWN, "ansys-mapdl-core가 설치되지 않았습니다."))

        try:
            run_dir = str(Path(job.working_dir).resolve()) if job.working_dir else tempfile.mkdtemp(prefix=f"mapdl_{job.name}_")
            mapdl: Any = pymapdl.launch_mapdl(
                run_location=run_dir,
                mode="grpc",
                **job.extra_args,
            )

            if MapdlConsole is not None and isinstance(mapdl, MapdlConsole):
                try:
                    mapdl.exit()
                except Exception:
                    log.debug("MAPDL Console 종료 실패", exc_info=True)
                return Err(APDLError(ErrorKind.UNKNOWN, "MAPDL Console 모드는 지원하지 않습니다. gRPC 모드만 지원합니다."))

            if MapdlGrpc is not None and not isinstance(mapdl, MapdlGrpc):
                return Err(APDLError(ErrorKind.UNKNOWN, f"지원하지 않는 MAPDL 타입: {type(mapdl).__name__}"))

            log.info("MAPDL gRPC 시작 완료 (run_location=%s)", run_dir)
            return Ok(cast("MapdlGrpc", mapdl))
        except Exception as exc:
            kind, code = ErrorClassifier.classify_detail(str(exc))
            return Err(APDLError(kind, f"MAPDL 시작 실패: {exc}", code=code, tb=traceback.format_exc()))
