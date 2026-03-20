"""
Authenticated HTTP client for the Tripletex v2 REST API.

Key design decisions:
- One requests.Session per solve request (credentials are per-request)
- Always uses the base_url provided in the contest payload — never hardcoded
- Basic Auth: username="0", password=session_token
- Logs every call: method, path, status, elapsed_ms
- On 4xx: raises TripletexError immediately (let Gemini self-correct)
- On 5xx: one retry with 1 s delay
- Returns validation messages to caller so Gemini can fix its parameters
"""
import logging
import time
from typing import Any

import requests

from .errors import TripletexError, parse_error_response

logger = logging.getLogger(__name__)

_MAX_5XX_RETRIES = 1


class TripletexClient:
    def __init__(self, base_url: str, session_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = ("0", session_token)
        self._session = requests.Session()
        self._session.auth = self.auth
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        # Accumulated call log — written to structured logs after solve
        self.call_log: list[dict] = []

    # ── Public interface ──────────────────────────────────────────────────────

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, json_body=body)

    def put(self, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        return self._request("PUT", path, params=params, json_body=body)

    def delete(self, path: str) -> None:
        self._request("DELETE", path)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        # Strip None values from params so they don't appear as "None" strings
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        for attempt in range(1 + _MAX_5XX_RETRIES):
            t0 = time.time()
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    params=clean_params,
                    json=json_body,
                    timeout=30,
                )
            except requests.RequestException as exc:
                logger.error(f"Network error: {method} {path}: {exc}")
                raise TripletexError(0, str(exc))

            elapsed_ms = round((time.time() - t0) * 1000)
            log_entry = {
                "method": method,
                "path": path,
                "status_code": resp.status_code,
                "elapsed_ms": elapsed_ms,
            }
            self.call_log.append(log_entry)
            logger.info(
                f"Tripletex {method} {path} → {resp.status_code} ({elapsed_ms}ms)",
                extra=log_entry,
            )

            # Back off if rate limit is nearly exhausted
            remaining = resp.headers.get("X-Rate-Limit-Remaining")
            if remaining is not None and int(remaining) < 5:
                reset_secs = int(resp.headers.get("X-Rate-Limit-Reset", 2))
                logger.warning(f"Rate limit low ({remaining} left), sleeping {reset_secs}s")
                time.sleep(reset_secs)

            # Success
            if resp.status_code in (200, 201):
                return resp.json() if resp.content else None

            # No-content success (DELETE, some PUT actions)
            if resp.status_code == 204:
                return None

            # Server error — one retry
            if resp.status_code >= 500 and attempt < _MAX_5XX_RETRIES:
                logger.warning(f"5xx on {method} {path}, retrying in 1 s…")
                time.sleep(1)
                continue

            # Client error or exhausted retries — parse and raise
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise parse_error_response(resp.status_code, body)

        raise TripletexError(500, "Max retries exceeded")
