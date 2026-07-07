from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import ClassVar

log = logging.getLogger(__name__)


class ErrorKind(Enum):
    SYNTAX = auto()
    CONVERGENCE = auto()
    LICENSE = auto()
    MEMORY = auto()
    IO = auto()
    ASSERTION = auto()
    TIMEOUT = auto()
    UNKNOWN = auto()


@dataclass
class APDLError:
    kind: ErrorKind
    message: str
    code: str = ""
    stage: str = ""
    raw_output: str = ""
    tb: str = field(default="", repr=False)

    def __str__(self) -> str:
        parts = [f"[{self.kind.name}]"]
        if self.code:
            parts.append(f"code={self.code}")
        if self.stage:
            parts.append(f"stage={self.stage}")
        parts.append(self.message)
        return " | ".join(parts)


class ErrorClassifier:
    """MAPDL 원시 출력 문자열에서 오류 코드와 ErrorKind를 추론한다."""

    _CODE_TABLE_PATH: ClassVar[Path] = Path(__file__).with_name("mapdl_error_codes.json")
    _BUILTIN_CODE_TABLE: ClassVar[dict[str, dict[str, str]]] = {
        "MAPDL_SYNTAX_UNKNOWN_COMMAND": {
            "kind": "SYNTAX",
            "regex": r"unknown command|unrecognized command|invalid command|syntax error",
        },
        "MAPDL_CONVERGENCE_FAILED": {
            "kind": "CONVERGENCE",
            "regex": r"convergence|not converged|solution not converged|failed to converge",
        },
        "MAPDL_LICENSE_CHECKOUT_FAILED": {
            "kind": "LICENSE",
            "regex": r"ansys license manager error|license checkout fail|cannot check\s*out license|cannot connect to license|license server down|licensed number of users already reached|flexnet licensing error|flexlm.*(?:error|unavailable)",
        },
        "MAPDL_MEMORY_ALLOCATION_FAILED": {
            "kind": "MEMORY",
            "regex": r"insufficient memory|out of memory|memory allocation|not enough memory",
        },
        "MAPDL_IO_FILE_ACCESS_FAILED": {
            "kind": "IO",
            "regex": r"cannot open|file not found|i/o error|permission denied|unable to open",
        },
        "MAPDL_TIMEOUT": {
            "kind": "TIMEOUT",
            "regex": r"timed? ?out|timeout|time limit exceeded",
        },
    }
    _RULES: ClassVar[list[tuple[str, ErrorKind, re.Pattern[str]]] | None] = None

    @classmethod
    def _read_code_table(cls, path: Path | None = None) -> dict[str, dict[str, str]]:
        table_path = path or cls._CODE_TABLE_PATH
        try:
            data = json.loads(table_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.warning("MAPDL 오류 코드 테이블 JSON 없음: %s, 내장 테이블 사용", table_path)
            return cls._BUILTIN_CODE_TABLE
        except json.JSONDecodeError as exc:
            log.warning("MAPDL 오류 코드 테이블 JSON 파싱 실패: %s, 내장 테이블 사용", exc)
            return cls._BUILTIN_CODE_TABLE

        if not isinstance(data, dict):
            log.warning("MAPDL 오류 코드 테이블 JSON 최상위 타입이 object가 아님, 내장 테이블 사용")
            return cls._BUILTIN_CODE_TABLE

        table: dict[str, dict[str, str]] = {}
        for code, entry in data.items():
            if not isinstance(code, str) or not isinstance(entry, dict):
                log.warning("잘못된 MAPDL 오류 코드 항목 무시: %r -> %r", code, entry)
                continue
            kind = entry.get("kind")
            regex = entry.get("regex") or entry.get("pattern")
            if not isinstance(kind, str) or not isinstance(regex, str):
                log.warning("MAPDL 오류 코드 항목에 kind/regex가 없음: %s", code)
                continue
            table[code] = {"kind": kind, "regex": regex}
        return table or cls._BUILTIN_CODE_TABLE

    @classmethod
    def reload(cls, path: Path | None = None) -> None:
        rules: list[tuple[str, ErrorKind, re.Pattern[str]]] = []
        for code, entry in cls._read_code_table(path).items():
            try:
                kind = ErrorKind[entry["kind"]]
            except KeyError:
                log.warning("알 수 없는 MAPDL 오류 kind 무시: %s -> %s", code, entry.get("kind"))
                continue
            try:
                rules.append((code, kind, re.compile(entry["regex"], re.I | re.M)))
            except re.error as exc:
                log.warning("MAPDL 오류 코드 정규식 컴파일 실패(%s): %s", code, exc)
        cls._RULES = rules

    @classmethod
    def classify_detail(cls, text: str) -> tuple[ErrorKind, str]:
        if cls._RULES is None:
            cls.reload()
        for code, kind, pattern in cls._RULES or []:
            if pattern.search(text):
                return kind, code
        return ErrorKind.UNKNOWN, "MAPDL_UNKNOWN"

    @classmethod
    def classify(cls, text: str) -> ErrorKind:
        kind, _code = cls.classify_detail(text)
        return kind
