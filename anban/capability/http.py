"""Bounded multi-method HTTP Capability without a destination allowlist."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.capability.workspace import capability_error
from anban.config import policy
from anban.core.errors import ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata, SafeScalar, validate_safe_text
from anban.core.models import now_utc

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_METHODS: tuple[HttpMethod, ...] = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
)
_SENSITIVE_HEADER_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "proxy-",
    "secret",
    "token",
    "api-key",
    "apikey",
)
_CONTROLLED_HEADERS = frozenset(
    {"content-length", "host", "connection", "transfer-encoding", "content-type"}
)


class HttpCapability:
    """Perform one bounded HTTP request; callers choose destinations without a host allowlist."""

    def __init__(
        self,
        *,
        method: Literal["GET"] | None = None,
        protected_values: tuple[str, ...] = (),
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._fixed_method = method
        self._protected_values = tuple(value for value in protected_values if value)
        self._client_factory = client_factory or self._new_client
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._cancelled: set[str] = set()
        self._descriptor = self._build_descriptor(method)

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        method = self._fixed_method or arguments.get("method")
        url = arguments.get("url")
        timeout = arguments.get("timeout", policy.HTTP_TIMEOUT_MAX_SECONDS)
        if not isinstance(method, str) or method not in _METHODS:
            raise self._argument_error("method_invalid")
        if not isinstance(url, str) or not isinstance(timeout, int):
            raise self._argument_error("argument_type")
        self._validate_url(url)
        headers = self._headers(arguments.get("headers", []))
        json_value = self._json_body(arguments.get("json_body"), method)
        self._reject_protected_data(url, headers, arguments.get("json_body"))

        remaining = (context.deadline_at - now_utc()).total_seconds()
        effective_timeout = min(float(timeout), remaining, policy.HTTP_TIMEOUT_MAX_SECONDS)
        if effective_timeout <= 0:
            return self._timeout(method)

        key = str(context.invocation_id)
        client = self._client_factory()
        self._clients[key] = client
        try:
            try:
                async with asyncio.timeout(effective_timeout):
                    request = client.build_request(
                        method,
                        url,
                        headers=headers,
                        json=json_value,
                    )
                    response = await client.send(request, stream=True)
                    try:
                        content, exceeded = await self._read_response(response)
                    finally:
                        await response.aclose()
            except TimeoutError:
                return self._timeout(method)
            except (httpx.TimeoutException, httpx.TransportError):
                if key in self._cancelled:
                    return self._cancelled_result(method)
                return self._failure("transport_error", method=method)
            if key in self._cancelled:
                return self._cancelled_result(method)
            if exceeded:
                return self._failure(
                    "output_limit", method=method, status_code=response.status_code
                )
            if 300 <= response.status_code < 400:
                return self._failure(
                    "redirect_rejected",
                    method=method,
                    status_code=response.status_code,
                    content=content,
                )
            if not 200 <= response.status_code < 300:
                return self._failure(
                    "http_status",
                    method=method,
                    status_code=response.status_code,
                    content=content,
                )
            if content and not self._is_textual(response.headers.get("content-type")):
                return self._failure(
                    "content_type_rejected",
                    method=method,
                    status_code=response.status_code,
                    content=content,
                )
            try:
                observation = content.decode(response.encoding or "utf-8", errors="replace")
                validate_safe_text(
                    observation,
                    label="HTTP response",
                    max_length=policy.HTTP_RESPONSE_MAX_BYTES,
                )
            except ValueError:
                return self._failure(
                    "unsafe_output",
                    method=method,
                    status_code=response.status_code,
                    content=content,
                )
            return CapabilityResult(
                status=CapabilityResultStatus.COMPLETED,
                observation=observation,
                metadata=SafeMetadata(
                    {
                        "method": method,
                        "status_code": response.status_code,
                        "size_bytes": len(content),
                        "content_hash": hashlib.sha256(content).hexdigest(),
                    }
                ),
            )
        finally:
            self._clients.pop(key, None)
            self._cancelled.discard(key)
            await client.aclose()

    async def cancel(self, context: InvocationContext) -> None:
        key = str(context.invocation_id)
        client = self._clients.get(key)
        if client is None:
            return
        self._cancelled.add(key)
        await client.aclose()

    @staticmethod
    def _new_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(trust_env=False, follow_redirects=False)

    async def _read_response(self, response: httpx.Response) -> tuple[bytes, bool]:
        retained = bytearray()
        async for chunk in response.aiter_bytes():
            remaining = policy.HTTP_RESPONSE_MAX_BYTES - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                return bytes(retained), True
        return bytes(retained), False

    def _headers(self, raw: JsonValue) -> dict[str, str]:
        if not isinstance(raw, list):
            raise self._argument_error("headers_invalid")
        headers: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"name", "value"}:
                raise self._argument_error("headers_invalid")
            name = item.get("name")
            value = item.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                raise self._argument_error("headers_invalid")
            normalized = name.strip().lower()
            if (
                not normalized
                or normalized in _CONTROLLED_HEADERS
                or any(part in normalized for part in _SENSITIVE_HEADER_PARTS)
            ):
                raise self._argument_error("sensitive_header")
            try:
                validate_safe_text(value, label="HTTP header value", max_length=1024)
            except ValueError as exc:
                raise self._argument_error("unsafe_header") from exc
            headers[name] = value
        return headers

    def _json_body(self, raw: JsonValue, method: str) -> JsonValue | None:
        if raw is None:
            return None
        if method in {"GET", "HEAD"} or not isinstance(raw, str):
            raise self._argument_error("body_not_allowed")
        if len(raw.encode("utf-8")) > policy.HTTP_REQUEST_BODY_MAX_BYTES:
            raise self._argument_error("body_limit")
        try:
            return cast(JsonValue, json.loads(raw))
        except json.JSONDecodeError as exc:
            raise self._argument_error("body_json_invalid") from exc

    def _validate_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise self._argument_error("url_invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port is not None
            and not 1 <= port <= 65_535
        ):
            raise self._argument_error("url_invalid")

    def _reject_protected_data(
        self, url: str, headers: dict[str, str], raw_body: JsonValue
    ) -> None:
        values = (url, *headers.values(), raw_body if isinstance(raw_body, str) else "")
        if any(secret in value for secret in self._protected_values for value in values):
            raise self._argument_error("protected_data_detected")

    @staticmethod
    def _is_textual(content_type: str | None) -> bool:
        if content_type is None:
            return True
        media_type = content_type.partition(";")[0].strip().lower()
        return (
            media_type.startswith("text/")
            or media_type
            in {"application/json", "application/xml", "application/x-www-form-urlencoded"}
            or media_type.endswith("+json")
            or media_type.endswith("+xml")
        )

    def _argument_error(self, reason: str) -> Exception:
        return capability_error(
            ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
            "HTTP request arguments are invalid",
            reason=reason,
            capability_name=self.descriptor.name,
        )

    def _failure(
        self,
        reason: str,
        *,
        method: str,
        status_code: int | None = None,
        content: bytes | None = None,
    ) -> CapabilityResult:
        details: dict[str, SafeScalar] = {
            "capability_name": self.descriptor.name,
            "reason": reason,
        }
        if status_code is not None:
            details["status_code"] = status_code
        metadata: dict[str, SafeScalar] = {"method": method}
        if status_code is not None:
            metadata["status_code"] = status_code
        if content is not None:
            metadata["size_bytes"] = len(content)
            metadata["content_hash"] = hashlib.sha256(content).hexdigest()
        return CapabilityResult(
            status=CapabilityResultStatus.FAILED,
            metadata=SafeMetadata(metadata),
            error=ErrorInfo(
                code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
                message="HTTP request failed",
                details=SafeMetadata(details),
            ),
        )

    def _timeout(self, method: str) -> CapabilityResult:
        return CapabilityResult(
            status=CapabilityResultStatus.TIMED_OUT,
            metadata=SafeMetadata({"method": method}),
            error=ErrorInfo(
                code=ErrorCode.EXECUTION_TIMED_OUT,
                message="HTTP request timed out",
                details=SafeMetadata({"capability_name": self.descriptor.name}),
            ),
        )

    def _cancelled_result(self, method: str) -> CapabilityResult:
        return CapabilityResult(
            status=CapabilityResultStatus.CANCELLED,
            metadata=SafeMetadata({"method": method}),
            error=ErrorInfo(
                code=ErrorCode.EXECUTION_INTERRUPTED,
                message="HTTP request was cancelled",
                details=SafeMetadata({"capability_name": self.descriptor.name}),
            ),
        )

    @staticmethod
    def _build_descriptor(method: Literal["GET"] | None) -> CapabilityDescriptor:
        properties: dict[str, JsonValue] = {
            "url": {
                "type": "string",
                "minLength": 1,
                "maxLength": policy.HTTP_URL_MAX_LENGTH,
            },
            "headers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": policy.HTTP_HEADER_NAME_MAX_LENGTH,
                        },
                        "value": {
                            "type": "string",
                            "maxLength": policy.HTTP_HEADER_VALUE_MAX_LENGTH,
                        },
                    },
                    "required": ["name", "value"],
                    "additionalProperties": False,
                },
                "maxItems": policy.HTTP_HEADER_MAX_ITEMS,
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": policy.HTTP_TIMEOUT_MAX_SECONDS,
            },
        }
        required: list[JsonValue] = ["url"]
        if method is None:
            properties["method"] = {"type": "string", "enum": list(_METHODS)}
            properties["json_body"] = {
                "type": "string",
                "maxLength": policy.HTTP_REQUEST_BODY_MAX_BYTES,
            }
            required.append("method")
        name = "http.get" if method == "GET" else "http.request"
        description = (
            "Perform one bounded HTTP GET request to a caller-selected HTTP or HTTPS URL."
            if method == "GET"
            else "Perform one bounded GET, POST, PUT, PATCH, DELETE, HEAD, or OPTIONS request."
        )
        return CapabilityDescriptor(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        )
