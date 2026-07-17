"""Allow distinct Model Gateway failure codes.

Revision ID: 0002_model_errors
Revises: 0001_v01_runtime
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_model_errors"
down_revision: str | None = "0001_v01_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("tasks", "execution_runs", "node_runs", "capability_invocations")
P1_CODES = (
    "'configuration_missing', 'validation_failed', 'invalid_transition', "
    "'model_request_failed', 'model_response_invalid', 'capability_unknown', "
    "'capability_arguments_invalid', 'capability_execution_failed', "
    "'persistence_unavailable', 'persistence_write_failed', 'audit_trace_write_failed', "
    "'execution_timed_out', 'execution_interrupted'"
)
MODEL_CODES = (
    "'configuration_missing', 'validation_failed', 'invalid_transition', "
    "'model_request_failed', 'model_timeout', 'model_transport_failed', 'model_rejected', "
    "'model_response_invalid', 'capability_unknown', 'capability_arguments_invalid', "
    "'capability_execution_failed', 'persistence_unavailable', 'persistence_write_failed', "
    "'audit_trace_write_failed', 'execution_timed_out', 'execution_interrupted'"
)


def replace_constraints(codes: str) -> None:
    for table in TABLES:
        op.drop_constraint("ck_error_code_allowed", table, type_="check")
        op.create_check_constraint(
            "ck_error_code_allowed",
            table,
            f"error_code IS NULL OR error_code IN ({codes})",
        )


def upgrade() -> None:
    replace_constraints(MODEL_CODES)


def downgrade() -> None:
    replace_constraints(P1_CODES)
