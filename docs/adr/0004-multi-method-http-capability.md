# ADR-0004: Multi-method HTTP Capability Boundary

- Status: Accepted
- Date: 2026-07-17
- Scope: Issue #28 v0.1 remediation

## Context

The approved Workspace Weather Skill describes a real public HTTP operation, but the production
Capability Registry previously exposed only files and allowlisted local processes. Calling `curl`
outside the Registry could not prove the required Runtime, Invocation, Event, Audit, and Trace
closure. Limiting the new adapter to two weather hosts would also make the general Skill boundary a
Weather-specific bypass.

## Decision

The production Registry exposes `http.get` for the common read-only case and `http.request` for
`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, and `OPTIONS`. Both accept caller-selected HTTP or
HTTPS destinations, including non-default ports. There is deliberately no hostname, domain, or IP
allowlist and no Weather-specific routing.

The fixed, non-configurable safety boundary is:

- no URL user information, fragments, automatic redirects, environment proxies, cookies, file
  upload, streaming request body, or arbitrary binary response;
- optional request bodies are bounded JSON and are rejected for GET and HEAD;
- custom headers are bounded and credential-bearing, proxy, cookie, host, framing, and content-type
  headers are rejected;
- known configured Secret values are rejected before transport;
- request duration is at most 30 seconds and is also capped by the Agent's remaining deadline;
- response and JSON request body are each at most 16 KiB;
- Invocation, Event, Audit, and Trace retain only the method, status, byte count, and response hash,
  never the URL, request body, headers, or response body.

These hard bounds are code policy and cannot be relaxed through `anban.toml`. `http.get` and
`http.request` share the same implementation so their validation and audit behavior cannot drift.

## Security tradeoff

Without a destination allowlist, this Capability can reach public or private addresses visible to
the Anban host. That is an explicit SSRF and internal-service interaction risk, especially for
state-changing methods. The decision prioritizes a general-purpose HTTP Capability as requested;
it must not be represented as network isolation. Deployments requiring tenant isolation must add
an outer network boundary before exposing model-controlled execution. Redirect denial, Secret
filtering, bounded JSON, deadlines, and audit metadata reduce impact but do not remove that risk.

## Consequences

Skills can perform real HTTP operations through the normal Registry and persistence path without a
provider or Skill special case. A failed status, redirect, timeout, invalid content type, oversized
response, unsafe output, or cancellation fails closed. Future changes that add authentication,
binary transfer, redirects, proxy use, or configurable network policy require a new review because
they materially widen the data-exfiltration or side-effect boundary.
