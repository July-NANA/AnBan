"""Stable structured error and safe serialization tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from anban.core import AnbanError, ErrorCategory, ErrorCode, ErrorInfo, SafeMetadata


def test_every_error_code_has_a_category() -> None:
    for code in ErrorCode:
        assert isinstance(ErrorInfo(code=code, message="safe failure").category, ErrorCategory)


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (ErrorCode.CONFIGURATION_MISSING, ErrorCategory.CONFIGURATION),
        (ErrorCode.VALIDATION_FAILED, ErrorCategory.VALIDATION),
        (ErrorCode.MODEL_REQUEST_FAILED, ErrorCategory.MODEL),
        (ErrorCode.CAPABILITY_EXECUTION_FAILED, ErrorCategory.CAPABILITY),
        (ErrorCode.PERSISTENCE_WRITE_FAILED, ErrorCategory.PERSISTENCE),
        (ErrorCode.AUDIT_TRACE_WRITE_FAILED, ErrorCategory.AUDIT_TRACE),
        (ErrorCode.EXECUTION_TIMED_OUT, ErrorCategory.TIMEOUT),
        (ErrorCode.EXECUTION_INTERRUPTED, ErrorCategory.INTERRUPTION),
    ],
)
def test_error_codes_have_stable_categories(code: ErrorCode, category: ErrorCategory) -> None:
    assert ErrorInfo(code=code, message="safe failure").category is category


def test_error_is_machine_readable_and_safe_to_render() -> None:
    error = AnbanError(
        ErrorInfo(
            code=ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
            message="capability arguments did not match the declared schema",
            details=SafeMetadata({"capability": "file.read"}),
        )
    )
    assert error.as_dict() == {
        "code": "capability_arguments_invalid",
        "message": "capability arguments did not match the declared schema",
        "details": {"capability": "file.read"},
        "category": "capability",
    }
    assert str(error) == "capability arguments did not match the declared schema"


def test_error_details_reuse_safe_metadata_boundary() -> None:
    with pytest.raises(ValidationError):
        ErrorInfo(
            code=ErrorCode.MODEL_REQUEST_FAILED,
            message="model request failed",
            details=SafeMetadata.model_validate({"provider_response": "raw"}),
        )


def test_error_message_rejects_host_path() -> None:
    with pytest.raises(ValidationError):
        ErrorInfo(
            code=ErrorCode.PERSISTENCE_WRITE_FAILED,
            message="failed at /Users/example/private.txt",
        )
