"""Safe persistence errors shared by focused repository operations."""

from uuid import UUID

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata


def missing_record(entity: str, record_id: UUID) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_WRITE_FAILED,
            message="persistence update target does not exist",
            details=SafeMetadata({"entity": entity, "record_id": str(record_id)}),
        )
    )


def inconsistent_run() -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_UNAVAILABLE,
            message="persisted Run relationships are incomplete",
        )
    )


def graph_revision_conflict(reason: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_WRITE_FAILED,
            message="Graph revision history rejected an append",
            details=SafeMetadata({"reason": reason}),
        )
    )
