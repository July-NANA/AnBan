"""Stable persistence failure categories shared by Runtime observers."""

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata


def persistence_error(stage: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_WRITE_FAILED,
            message="Runtime persistence operation failed",
            details=SafeMetadata({"stage": stage}),
        )
    )


def audit_trace_error(stage: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.AUDIT_TRACE_WRITE_FAILED,
            message="Runtime Event persistence failed",
            details=SafeMetadata({"stage": stage}),
        )
    )
