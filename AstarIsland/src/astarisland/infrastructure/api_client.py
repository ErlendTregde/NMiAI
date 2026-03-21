from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from astarisland.domain.models import (
    AnalysisResult,
    BudgetStatus,
    MyPredictionSummary,
    ObservationRequest,
    ObservationResult,
    RoundDetail,
    RoundSummary,
    SubmissionResult,
)
from astarisland.infrastructure.settings import Settings


class ApiError(RuntimeError):
    pass


class ApiRetryableError(ApiError):
    pass


@dataclass(slots=True)
class _RateLimitState:
    simulate_last_called: float = 0.0
    submit_last_called: float = 0.0


class AstarIslandApiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        headers = {"User-Agent": settings.user_agent}
        if settings.access_token:
            headers["Authorization"] = f"Bearer {settings.access_token}"
        self._client = httpx.Client(
            base_url=str(settings.base_url),
            headers=headers,
            timeout=settings.request_timeout_seconds,
        )
        self._rate_limit_state = _RateLimitState()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AstarIslandApiClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _throttle(self, endpoint_kind: str) -> None:
        now = time.monotonic()
        if endpoint_kind == "simulate":
            delay = 0.2 - (now - self._rate_limit_state.simulate_last_called)
            if delay > 0:
                time.sleep(delay)
            self._rate_limit_state.simulate_last_called = time.monotonic()
        elif endpoint_kind == "submit":
            delay = 0.5 - (now - self._rate_limit_state.submit_last_called)
            if delay > 0:
                time.sleep(delay)
            self._rate_limit_state.submit_last_called = time.monotonic()

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        endpoint_kind: str | None = None,
    ) -> Any:
        if endpoint_kind is not None:
            self._throttle(endpoint_kind)

        retryer = Retrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
            retry=retry_if_exception_type((httpx.TransportError, ApiRetryableError)),
            reraise=True,
        )

        for attempt in retryer:
            with attempt:
                response = self._client.request(method, path, json=json_payload)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise ApiRetryableError(f"retryable response from {path}: {response.status_code}")
                if response.is_error:
                    raise ApiError(f"{method} {path} failed: {response.status_code} {response.text}")
                return response.json()
        raise ApiError(f"{method} {path} failed without a response")

    def list_rounds(self) -> list[RoundSummary]:
        payload = self._request_json("GET", "/astar-island/rounds")
        return [RoundSummary.model_validate(item) for item in payload]

    def get_round_detail(self, round_id: str) -> RoundDetail:
        payload = self._request_json("GET", f"/astar-island/rounds/{round_id}")
        return RoundDetail.model_validate(payload)

    def get_budget(self) -> BudgetStatus:
        payload = self._request_json("GET", "/astar-island/budget")
        return BudgetStatus.model_validate(payload)

    def simulate(self, request: ObservationRequest) -> ObservationResult:
        payload = self._request_json(
            "POST",
            "/astar-island/simulate",
            json_payload=request.model_dump(mode="json"),
            endpoint_kind="simulate",
        )
        return ObservationResult.model_validate(payload)

    def submit_prediction(self, round_id: str, seed_index: int, prediction: list[list[list[float]]]) -> SubmissionResult:
        payload = self._request_json(
            "POST",
            "/astar-island/submit",
            json_payload={
                "round_id": round_id,
                "seed_index": seed_index,
                "prediction": prediction,
            },
            endpoint_kind="submit",
        )
        return SubmissionResult.model_validate(payload)

    def my_rounds(self) -> list[dict[str, Any]]:
        return self._request_json("GET", "/astar-island/my-rounds")

    def my_predictions(self, round_id: str) -> list[MyPredictionSummary]:
        payload = self._request_json("GET", f"/astar-island/my-predictions/{round_id}")
        return [MyPredictionSummary.model_validate(item) for item in payload]

    def analysis(self, round_id: str, seed_index: int) -> AnalysisResult:
        payload = self._request_json("GET", f"/astar-island/analysis/{round_id}/{seed_index}")
        return AnalysisResult.model_validate(payload)

