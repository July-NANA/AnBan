"""add unavailable Capability error code

Revision ID: 0003_capability_error
Revises: 0002_model_errors
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_capability_error"
down_revision: str | None = "0002_model_errors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CODES = (
    "configuration_missing",
    "validation_failed",
    "invalid_transition",
    "model_request_failed",
    "model_timeout",
    "model_transport_failed",
    "model_rejected",
    "model_response_invalid",
    "capability_unknown",
    "capability_arguments_invalid",
    "capability_execution_failed",
    "persistence_unavailable",
    "persistence_write_failed",
    "audit_trace_write_failed",
    "execution_timed_out",
    "execution_interrupted",
)
_NEW_CODES = (*_OLD_CODES[:9], "capability_unavailable", *_OLD_CODES[9:])


def _replace_constraints(codes: tuple[str, ...]) -> None:
    values = ", ".join(repr(code) for code in codes)
    for table in ("tasks", "execution_runs", "node_runs", "capability_invocations"):
        op.drop_constraint("ck_error_code_allowed", table, type_="check")
        op.create_check_constraint(
            "ck_error_code_allowed",
            table,
            f"error_code IS NULL OR error_code IN ({values})",
        )


def upgrade() -> None:
    _replace_constraints(_NEW_CODES)


def downgrade() -> None:
    _replace_constraints(_OLD_CODES)
