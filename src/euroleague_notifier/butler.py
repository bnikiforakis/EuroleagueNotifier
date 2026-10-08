"""Client for the PiButler project API (ADR-012)."""

import asyncio
import json
import logging
from typing import Any, Self

import httpx

log = logging.getLogger(__name__)


class ButlerError(Exception):
    pass


class ButlerClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        project_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff: float = 1.0,
    ):
        self.project_id = project_id
        self._backoff = backoff
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, body: dict) -> dict:
        for attempt in range(3):
            try:
                resp = await self._http.request(method, f"/v1/projects/{self.project_id}{path}", json=body)
            except httpx.TransportError as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 500:
                    if resp.is_error:
                        raise ButlerError(f"{method} {path}: {resp.status_code} {resp.text}")
                    if not resp.content.strip():
                        return {}
                    try:
                        return resp.json()
                    except json.JSONDecodeError as exc:
                        raise ButlerError(f"{method} {path}: invalid JSON in response") from exc
                error = f"{resp.status_code} {resp.text}"
            if attempt < 2:
                await asyncio.sleep(self._backoff * 2**attempt)
        raise ButlerError(f"{method} {path} failed: {error}")

    async def register(self, manifest: dict[str, Any]) -> None:
        await self._request("PUT", "/manifest", manifest)

    async def notify(
        self,
        key: str,
        text: str,
        tags: dict[str, list[str]] | None = None,
        expires_at: str | None = None,
        *,
        topics: list[dict] | None = None,
        audience: dict | None = None,
        buttons: list[list[dict]] | None = None,
    ) -> dict:
        body = {"idempotency_key": key, "text": text, "tags": tags or {}, "expires_at": expires_at}
        body |= {k: v for k, v in (("topics", topics), ("audience", audience), ("buttons", buttons)) if v}
        result = await self._request("POST", "/notifications", body)
        dup = " (duplicate)" if result.get("duplicate") else ""
        log.info("notified %s → %s recipients%s", key, result.get("recipients"), dup)
        return result
