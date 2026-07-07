from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from .result import Result

if TYPE_CHECKING:
    from ansys.mapdl.core.mapdl_grpc import MapdlGrpc

CheckFn = Callable[["MapdlGrpc | None", dict], Result]


@dataclass
class APDLJob:
    """하나의 APDL 시뮬레이션 작업 정의."""

    name: str
    script_path: Path
    pre_checks: list[CheckFn] = field(default_factory=list)
    post_checks: list[CheckFn] = field(default_factory=list)
    working_dir: str | Path | None = None
    timeout: float = 3600.0
    extra_args: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.script_path = Path(self.script_path)
