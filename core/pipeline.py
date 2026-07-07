from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from .errors import APDLError, ErrorClassifier
from .result import Err, Ok, Result

log = logging.getLogger(__name__)


class PipelineStage:
    """단일 파이프라인 단계."""

    def __init__(self, name: str, fn: Callable[..., Result]) -> None:
        self.name = name
        self._fn = fn

    def run(self, *args, **kwargs) -> Result:
        try:
            result = self._fn(*args, **kwargs)
            if isinstance(result, Err):
                result.error.stage = self.name
            return result
        except Exception as exc:
            raw = str(exc)
            kind, code = ErrorClassifier.classify_detail(raw)
            return Err(APDLError(
                kind=kind,
                message=raw,
                code=code,
                stage=self.name,
                tb=traceback.format_exc(),
            ))


def pipeline(*stages: PipelineStage) -> Callable[..., Result]:
    """여러 PipelineStage를 순서대로 실행하고 첫 Err에서 중단."""

    def _run(initial_value: Any, **kwargs) -> Result:
        result: Result = Ok(initial_value)
        for stage in stages:
            if isinstance(result, Err):
                log.debug(
                    "pipeline early-exit at stage '%s' (skipping '%s')",
                    result.error.stage,
                    stage.name,
                )
                break
            result = stage.run(result.value, **kwargs)
            if isinstance(result, Ok):
                log.debug("stage '%s' → Ok", stage.name)
            else:
                log.warning("stage '%s' → Err: %s", stage.name, result.error)
        return result

    return _run
