from __future__ import annotations

import traceback
from typing import Any, Callable, Generic, TypeVar

from .errors import APDLError, ErrorKind

T = TypeVar("T")


class Ok(Generic[T]):
    """성공 결과 컨테이너."""

    __slots__ = ("value",)
    __match_args__ = ("value",)

    def __init__(self, value: T) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"Ok({self.value!r})"

    def unwrap(self) -> T:
        return self.value

    def map(self, fn: Callable[[T], Any]) -> "Ok | Err":
        try:
            return Ok(fn(self.value))
        except Exception as exc:
            return Err(APDLError(ErrorKind.UNKNOWN, str(exc), tb=traceback.format_exc()))


class Err:
    """실패 결과 컨테이너."""

    __slots__ = ("error",)
    __match_args__ = ("error",)

    def __init__(self, error: APDLError) -> None:
        self.error = error

    def __repr__(self) -> str:
        return f"Err({self.error})"

    def unwrap(self) -> None:
        raise RuntimeError(f"Err.unwrap() called: {self.error}")

    def map(self, _fn: Callable) -> "Err":
        return self


Result = Ok[T] | Err
