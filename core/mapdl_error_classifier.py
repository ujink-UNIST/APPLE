"""
mapdl_error_classifier.py
==========================
MAPDL gRPC 텍스트 반환값 → 구조화된 MAPDLDiagnostic 변환기.

설계 원칙:
  - 하나의 텍스트 덩어리에서 복수의 오류/경고를 모두 추출
  - 각 진단에 고유 코드(Exx / Wxx) + 컨텍스트 정보 부여
  - 심각도(ERROR / WARNING / INFO) 분리
  - 서버/상위 레이어가 코드 번호만 보고 판단 가능하도록 설계

오류 코드 체계:
  E1xx  라이선스 / 환경
  E2xx  수렴 / 솔버
  E3xx  메모리 / 리소스
  E4xx  메시 / 모델
  E5xx  파일 I/O
  E6xx  문법 / 명령어
  E9xx  알 수 없음
  W1xx  경고 (솔버)
  W2xx  경고 (모델 품질)
  I1xx  정보
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterator


# ─────────────────────────────────────────────────────────────────────────────
# 심각도 및 코드 정의
# ─────────────────────────────────────────────────────────────────────────────

class Severity(Enum):
    ERROR   = "ERROR"
    WARNING = "WARNING"
    INFO    = "INFO"


@dataclass(frozen=True)
class DiagCode:
    """진단 코드 상수 정의."""
    code: int
    severity: Severity
    name: str
    description: str

    def __str__(self) -> str:
        prefix = self.severity.value[0]   # E / W / I
        return f"{prefix}{self.code:03d}"


# ── 오류 코드 테이블 ──────────────────────────────────────────────────────────
class Code:
    # E1xx: 라이선스 / 환경
    LICENSE_CHECKOUT    = DiagCode(101, Severity.ERROR,   "LICENSE_CHECKOUT",    "라이선스 체크아웃 실패")
    LICENSE_EXPIRED     = DiagCode(102, Severity.ERROR,   "LICENSE_EXPIRED",     "라이선스 만료")
    LICENSE_SERVER_DOWN = DiagCode(103, Severity.ERROR,   "LICENSE_SERVER_DOWN", "라이선스 서버 응답 없음")

    # E2xx: 수렴 / 솔버
    NOT_CONVERGED       = DiagCode(201, Severity.ERROR,   "NOT_CONVERGED",       "솔루션 수렴 실패")
    DIVERGED            = DiagCode(202, Severity.ERROR,   "DIVERGED",            "솔루션 발산 (과도한 pivot ratio)")
    TOO_MANY_BISECT     = DiagCode(203, Severity.ERROR,   "TOO_MANY_BISECT",     "substep 이분법 한계 초과")
    NEGATIVE_PIVOT      = DiagCode(204, Severity.ERROR,   "NEGATIVE_PIVOT",      "음의 피벗 — 재료/접촉 설정 확인")
    SINGULAR_MATRIX     = DiagCode(205, Severity.ERROR,   "SINGULAR_MATRIX",     "특이 행렬 — 구속 조건 확인")

    # E3xx: 메모리 / 리소스
    OUT_OF_MEMORY       = DiagCode(301, Severity.ERROR,   "OUT_OF_MEMORY",       "메모리 부족")
    DISK_FULL           = DiagCode(302, Severity.ERROR,   "DISK_FULL",           "디스크 공간 부족")

    # E4xx: 메시 / 모델
    MESH_QUALITY        = DiagCode(401, Severity.ERROR,   "MESH_QUALITY",        "메시 품질 기준 미달")
    ELEMENT_DISTORTED   = DiagCode(402, Severity.ERROR,   "ELEMENT_DISTORTED",   "요소 과도 변형")
    CONTACT_STATUS      = DiagCode(403, Severity.WARNING, "CONTACT_STATUS",      "접촉 상태 이상")

    # E5xx: 파일 I/O
    FILE_NOT_FOUND      = DiagCode(501, Severity.ERROR,   "FILE_NOT_FOUND",      "파일 없음")
    FILE_WRITE_ERROR    = DiagCode(502, Severity.ERROR,   "FILE_WRITE_ERROR",    "파일 쓰기 실패")

    # E6xx: 문법 / 명령어
    UNKNOWN_COMMAND     = DiagCode(601, Severity.ERROR,   "UNKNOWN_COMMAND",     "알 수 없는 APDL 명령어")
    INVALID_ARGUMENT    = DiagCode(602, Severity.ERROR,   "INVALID_ARGUMENT",    "잘못된 명령어 인수")

    # E9xx: 미분류
    UNKNOWN_ERROR       = DiagCode(901, Severity.ERROR,   "UNKNOWN_ERROR",       "분류되지 않은 오류")

    # W1xx: 솔버 경고
    WARN_CONVERGENCE    = DiagCode(101, Severity.WARNING, "WARN_CONVERGENCE",    "수렴 경고 (완료는 됨)")
    WARN_LARGE_DEFORM   = DiagCode(102, Severity.WARNING, "WARN_LARGE_DEFORM",   "대변형 감지 — NLGEOM 확인")
    WARN_SMALL_PIVOT    = DiagCode(103, Severity.WARNING, "WARN_SMALL_PIVOT",    "작은 피벗 비율 경고")

    # W2xx: 모델 품질 경고
    WARN_MESH_QUALITY   = DiagCode(201, Severity.WARNING, "WARN_MESH_QUALITY",   "메시 품질 경고")
    WARN_UNCONSTRAINED  = DiagCode(202, Severity.WARNING, "WARN_UNCONSTRAINED",  "구속되지 않은 자유도")

    # I1xx: 정보
    INFO_SOLUTION_DONE  = DiagCode(101, Severity.INFO,    "SOLUTION_DONE",       "정상 완료")
    INFO_SUBSTEP_DONE   = DiagCode(102, Severity.INFO,    "SUBSTEP_DONE",        "Substep 완료")


# ─────────────────────────────────────────────────────────────────────────────
# 진단 결과 데이터클래스
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MAPDLDiagnostic:
    """MAPDL 텍스트에서 추출된 단일 진단 항목."""

    code: DiagCode
    raw_line: str                          # 매칭된 원본 텍스트 라인
    context: dict = field(default_factory=dict)  # 추출된 부가 정보

    @property
    def severity(self) -> Severity:
        return self.code.severity

    def __str__(self) -> str:
        ctx_str = ""
        if self.context:
            parts = [f"{k}={v}" for k, v in self.context.items()]
            ctx_str = " [" + ", ".join(parts) + "]"
        return f"{self.code} ({self.code.name}): {self.code.description}{ctx_str}"

    def to_dict(self) -> dict:
        return {
            "code":        str(self.code),
            "code_number": self.code.code,
            "severity":    self.severity.value,
            "name":        self.code.name,
            "description": self.code.description,
            "context":     self.context,
            "raw_line":    self.raw_line,
        }


@dataclass
class ClassificationResult:
    """전체 텍스트 블록에 대한 분류 결과."""

    diagnostics: list[MAPDLDiagnostic] = field(default_factory=list)
    raw_text: str = ""

    @property
    def errors(self) -> list[MAPDLDiagnostic]:
        return [d for d in self.diagnostics if d.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[MAPDLDiagnostic]:
        return [d for d in self.diagnostics if d.severity == Severity.WARNING]

    @property
    def has_error(self) -> bool:
        return bool(self.errors)

    @property
    def worst(self) -> MAPDLDiagnostic | None:
        """오류 > 경고 > 정보 순으로 가장 심각한 항목 반환."""
        return self.errors[0] if self.errors else (
               self.warnings[0] if self.warnings else
               self.diagnostics[0] if self.diagnostics else None)

    def summary(self) -> str:
        if not self.diagnostics:
            return "I101 (SOLUTION_DONE): 진단 항목 없음"
        lines = [str(d) for d in self.diagnostics]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 패턴 정의 (regex + 추출 함수)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Pattern:
    """단일 진단 패턴."""
    code: DiagCode
    regex: re.Pattern
    extract: "Callable[[re.Match], dict]" = field(default=lambda m: {})


def _substep_ctx(m: re.Match) -> dict:
    """수렴 실패 패턴에서 substep, load step 추출."""
    ctx = {}
    for key in ("load_step", "substep", "time"):
        if m.lastindex and key in m.groupdict() and m.group(key):
            ctx[key] = m.group(key)
    return ctx


_PATTERNS: list[_Pattern] = [

    # ── E1xx 라이선스 ──────────────────────────────────────────────────────
    _Pattern(
        Code.LICENSE_CHECKOUT,
        re.compile(r"license\s+checkout\s+fail|cannot\s+check\s*out\s+license", re.I),
    ),
    _Pattern(
        Code.LICENSE_EXPIRED,
        re.compile(r"license\s+expir|license\s+has\s+expired", re.I),
    ),
    _Pattern(
        Code.LICENSE_SERVER_DOWN,
        re.compile(r"cannot\s+connect\s+to\s+license|flexlm.*unavailable|license\s+server\s+down", re.I),
    ),

    # ── E2xx 수렴 / 솔버 ──────────────────────────────────────────────────
    _Pattern(
        Code.NOT_CONVERGED,
        re.compile(
            r"solution\s+not\s+converged"
            r"(?:.*?load\s+step\s+(?P<load_step>\d+))?"
            r"(?:.*?substep\s+(?P<substep>\d+))?",
            re.I | re.S,
        ),
        _substep_ctx,
    ),
    _Pattern(
        Code.NOT_CONVERGED,
        re.compile(r"run\s+terminated|solution\s+stopped", re.I),
    ),
    _Pattern(
        Code.DIVERGED,
        re.compile(r"divergen|excessive\s+pivot\s+ratio", re.I),
    ),
    _Pattern(
        Code.TOO_MANY_BISECT,
        re.compile(r"too\s+many\s+bisection|bisection\s+limit", re.I),
    ),
    _Pattern(
        Code.NEGATIVE_PIVOT,
        re.compile(r"negative\s+pivot|large\s+negative\s+pivot", re.I),
    ),
    _Pattern(
        Code.SINGULAR_MATRIX,
        re.compile(r"singular\s+matrix|ill[\s-]conditioned\s+matrix", re.I),
    ),

    # ── E3xx 메모리 / 리소스 ─────────────────────────────────────────────
    _Pattern(
        Code.OUT_OF_MEMORY,
        re.compile(r"insufficient\s+memory|out\s+of\s+memory|memory\s+allocation\s+fail", re.I),
    ),
    _Pattern(
        Code.DISK_FULL,
        re.compile(r"no\s+space\s+left|disk\s+(full|quota)|write\s+failed.*space", re.I),
    ),

    # ── E4xx 메시 / 모델 ─────────────────────────────────────────────────
    _Pattern(
        Code.MESH_QUALITY,
        re.compile(r"mesh\s+quality|poor\s+element\s+quality|aspect\s+ratio\s+exceed", re.I),
    ),
    _Pattern(
        Code.ELEMENT_DISTORTED,
        re.compile(r"element\s+(?:is\s+)?distorted|highly\s+distorted\s+element", re.I),
    ),
    _Pattern(
        Code.CONTACT_STATUS,
        re.compile(r"contact\s+(?:status|detection)\s+(?:error|warning|fail)", re.I),
    ),

    # ── E5xx 파일 I/O ─────────────────────────────────────────────────────
    _Pattern(
        Code.FILE_NOT_FOUND,
        re.compile(r"cannot\s+open|file\s+not\s+found|no\s+such\s+file", re.I),
    ),
    _Pattern(
        Code.FILE_WRITE_ERROR,
        re.compile(r"cannot\s+write|write\s+error|i/o\s+error", re.I),
    ),

    # ── E6xx 문법 ─────────────────────────────────────────────────────────
    _Pattern(
        Code.UNKNOWN_COMMAND,
        re.compile(r"unknown\s+command|unrecognized\s+command", re.I),
    ),
    _Pattern(
        Code.INVALID_ARGUMENT,
        re.compile(r"invalid\s+(argument|parameter|value)|illegal\s+data", re.I),
    ),

    # ── W1xx 솔버 경고 ────────────────────────────────────────────────────
    _Pattern(
        Code.WARN_CONVERGENCE,
        re.compile(r"\*\*\*\s*warning\s*\*\*\*.*?converge", re.I | re.S),
    ),
    _Pattern(
        Code.WARN_LARGE_DEFORM,
        re.compile(r"large\s+deform|large\s+displacement|nlgeom", re.I),
    ),
    _Pattern(
        Code.WARN_SMALL_PIVOT,
        re.compile(r"small\s+pivot|pivot\s+ratio\s+(?:is\s+)?small", re.I),
    ),

    # ── W2xx 모델 품질 경고 ───────────────────────────────────────────────
    _Pattern(
        Code.WARN_MESH_QUALITY,
        re.compile(r"\*\*\*\s*warning\s*\*\*\*.*?(?:mesh|element\s+shape)", re.I | re.S),
    ),
    _Pattern(
        Code.WARN_UNCONSTRAINED,
        re.compile(r"unconstrained\s+(dof|degree)|free\s+body", re.I),
    ),

    # ── I1xx 정보 ─────────────────────────────────────────────────────────
    _Pattern(
        Code.INFO_SOLUTION_DONE,
        re.compile(r"solution\s+is\s+done|finished\s+at\s+cp", re.I),
    ),
    _Pattern(
        Code.INFO_SUBSTEP_DONE,
        re.compile(
            r"(?:completed|converged)\s+at\s+(?:time|substep)"
            r"(?:.*?time\s*=\s*(?P<time>[\d.]+))?",
            re.I,
        ),
        lambda m: {"time": m.group("time")} if m.group("time") else {},
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# 분류기 본체
# ─────────────────────────────────────────────────────────────────────────────

class MAPDLErrorClassifier:
    """
    MAPDL gRPC / stdout 텍스트 → ClassificationResult 변환기.

    사용:
        clf = MAPDLErrorClassifier()
        result = clf.classify(mapdl_output_string)

        if result.has_error:
            worst = result.worst
            raise SomeException(str(worst))   # "E201 (NOT_CONVERGED): ..."
    """

    def __init__(self, patterns: list[_Pattern] | None = None) -> None:
        self._patterns = patterns or _PATTERNS

    def classify(self, text: str) -> ClassificationResult:
        result = ClassificationResult(raw_text=text)
        seen_codes: set[int] = set()   # 동일 코드 중복 방지 (첫 번째만)

        for pattern in self._patterns:
            match = pattern.regex.search(text)
            if match:
                # 같은 코드가 이미 추출됐으면 스킵 (첫 발생만 기록)
                uid = (pattern.code.severity, pattern.code.code)
                if uid in seen_codes:
                    continue
                seen_codes.add(uid)

                ctx = pattern.extract(match)
                # 매칭된 줄 전체 추출 (최대 120자)
                start = text.rfind("\n", 0, match.start()) + 1
                end   = text.find("\n", match.end())
                raw_line = text[start: end if end != -1 else None].strip()[:120]

                result.diagnostics.append(
                    MAPDLDiagnostic(code=pattern.code, raw_line=raw_line, context=ctx)
                )

        # 심각도 순 정렬: ERROR → WARNING → INFO
        _order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
        result.diagnostics.sort(key=lambda d: _order[d.severity])

        return result

    def classify_lines(self, lines: list[str]) -> ClassificationResult:
        """줄 단위 리스트로도 받을 수 있는 편의 메서드."""
        return self.classify("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# 편의 함수: PyMAPDL mapdl.run() 결과에 바로 붙이기
# ─────────────────────────────────────────────────────────────────────────────

_default_clf = MAPDLErrorClassifier()


def classify(text: str) -> ClassificationResult:
    """모듈 레벨 단축 함수."""
    return _default_clf.classify(text)
