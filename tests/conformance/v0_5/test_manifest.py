from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
REQUIRED_SCENARIO_FIELDS = {
    "scenario_id",
    "title",
    "phase",
    "required_integrations",
    "required_evidence",
    "positive_variants",
    "negative_variants",
    "restart_required",
    "real_acceptance_required",
    "forbidden_shortcuts",
    "status",
}


def _load_json_yaml(name: str) -> dict[str, Any]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_manifest_has_all_unique_scenarios() -> None:
    scenarios = _load_json_yaml("scenarios.yaml")["scenarios"]
    ids = [scenario["scenario_id"] for scenario in scenarios]
    assert ids == [f"S{index:02d}" for index in range(1, 13)]
    assert len(ids) == len(set(ids))


def test_every_scenario_has_complete_planned_evidence_contract() -> None:
    scenarios = _load_json_yaml("scenarios.yaml")["scenarios"]
    for scenario in scenarios:
        assert scenario.keys() >= REQUIRED_SCENARIO_FIELDS
        assert scenario["required_integrations"]
        assert scenario["required_evidence"]
        assert scenario["forbidden_shortcuts"]
        assert scenario["positive_variants"] >= 3
        assert scenario["negative_variants"] >= 1
        assert scenario["real_acceptance_required"] is True
        assert scenario["status"] == "planned"


def test_anti_hardcoding_rules_cover_required_controls() -> None:
    rules = _load_json_yaml("anti-hardcoding-rules.yaml")["rules"]
    ids = {rule["id"] for rule in rules}
    assert ids == {f"AH{index:02d}" for index in range(1, 14)}
    assert all(rule["requirement"].strip() for rule in rules)


def test_no_scenario_is_marked_passed() -> None:
    scenarios = _load_json_yaml("scenarios.yaml")["scenarios"]
    assert not any(scenario["status"] == "passed" for scenario in scenarios)
