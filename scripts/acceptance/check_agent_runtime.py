"""Scoped real-model acceptance for the fixed LangGraph General Agent."""

from __future__ import annotations

import asyncio
import json
import sys

from anban.capability import CapabilityRegistry, register_workspace_skill
from anban.config import load_configuration
from anban.core.errors import AnbanError
from anban.core.ids import new_execution_run_id, new_node_run_id
from anban.model import OpenAICompatibleAdapter
from anban.runtime import AgentInput, AgentOutcomeStatus, FixedGeneralAgent


class AgentAcceptanceError(RuntimeError):
    """Safe failure without prompt, provider response, or physical path output."""


async def accept_agent_runtime() -> None:
    configuration = load_configuration()
    model = OpenAICompatibleAdapter.configured(
        configuration.require_model(), protected_values=configuration.protected_values()
    )
    registry = CapabilityRegistry()
    register_workspace_skill(registry)
    agent = FixedGeneralAgent(model, registry)
    try:
        outcome = await agent.execute(
            AgentInput(
                request=(
                    "Call skill.activate for @steipete/weather before answering. "
                    "After its Tool Result, state its primary weather service in one sentence."
                ),
                run_id=new_execution_run_id(),
                node_run_id=new_node_run_id(),
            )
        )
    finally:
        await model.aclose()
    if (
        outcome.status is not AgentOutcomeStatus.SUCCEEDED
        or outcome.model_turn_count < 2
        or outcome.capability_call_count != 1
        or not outcome.final_text
        or "wttr.in" not in outcome.final_text.lower()
    ):
        raise AgentAcceptanceError(
            json.dumps(
                {
                    "status": outcome.status.value,
                    "model_turns": outcome.model_turn_count,
                    "capability_calls": outcome.capability_call_count,
                    "error_code": None if outcome.error is None else outcome.error.code.value,
                    "error_reason": (
                        None if outcome.error is None else outcome.error.details.root.get("reason")
                    ),
                    "final_chars": 0 if outcome.final_text is None else len(outcome.final_text),
                    "mentions_service": (
                        False
                        if outcome.final_text is None
                        else "wttr.in" in outcome.final_text.lower()
                    ),
                },
                separators=(",", ":"),
            )
        )


def main() -> int:
    try:
        asyncio.run(accept_agent_runtime())
    except AnbanError as exc:
        print(f"agent runtime acceptance: FAIL [{exc.info.code.value}]", file=sys.stderr)
        return 1
    except AgentAcceptanceError as exc:
        print(f"agent runtime acceptance: FAIL [outcome_invalid] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"agent runtime acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("agent runtime acceptance: PASS - fixed graph, real model, Skill Tool Call, final")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
