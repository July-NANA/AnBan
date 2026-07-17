# v0.5 Conformance Manifest

This directory is the planning skeleton for the release-blocking S01-S12 acceptance suite. It
does not implement the product scenarios and does not claim that any scenario passes.

- `scenarios.yaml` declares integrations, evidence, positive and negative variants, restart
  expectations, and forbidden shortcuts.
- `evidence-schema.json` defines the evidence-bundle shape future black-box runs must emit.
- `anti-hardcoding-rules.yaml` records reusable-architecture checks.
- `fixtures/` describes dynamic fixture requirements.
- `test_manifest.py` validates this planning data only.

Final acceptance must use the ordinary Composition Root, real PostgreSQL, real Process execution,
and the real protocol or lifecycle named by each scenario. Fakes and mocks may support unit tests
or fault injection but are not release evidence.
