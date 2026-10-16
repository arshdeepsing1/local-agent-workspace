"""Routes for endpoint checks (endpoint_probes.py) and the runner that sends their requests.

Every route but /run and /test sends nothing to a model. /run and /test send requests only with an approval
of the estimate the user saw: the plan_id of a plan computed again for that request and approved_dbu. A
request without approved_dbu is refused (HTTP 400) and one whose plan_id differs (another endpoint, choice of
checks or price list) gets HTTP 409, and nothing is sent in either case. No approval is remembered: each run
needs its own. One run at a time; its requests go out one after another, never retried: a 401, 403, 429, a
5xx or a failed connection stops the run, and an HTTP 4xx answer to a probe is a result.
"""
import asyncio
import json
import time
import uuid
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import endpoint_probes, serving
from .config import ENDPOINT_NAME
from .model_stream import error_body_prefix, sse_data
from .request_limits import encode_payload
from .telemetry import InferenceError

ProbeId = Literal["basic", "reasoning", "cache", "tools", "response_format", "image"]  # endpoint_probes.PROBE_IDS
PROBE_READ_TIMEOUT = 120.0  # Probe replies are short; a silent stream this long ends the run.
MAX_PROBE_CHUNKS = 5_000  # Chunks kept of one reply (a short answer streams far fewer).
RATE_OWNER = "endpoint-check"  # The rate gate's owner name for probe requests (RateLimitGate.remaining).
clock = time.monotonic  # Tests replace it.


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128, strict=True)
    probes: list[ProbeId] | None = Field(default=None, min_length=1, max_length=6)


class RunBody(PlanBody):
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The estimate the user approved; None (missing) is no approval.
    approved_dbu: float | None = Field(default=None, ge=0, le=10_000, allow_inf_nan=False)


class TestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128, strict=True)
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_dbu: float | None = Field(default=None, ge=0, le=10_000, allow_inf_nan=False)


def endpoint_name(value):
    name = value.strip()
    if not ENDPOINT_NAME.match(name):
        raise ValueError("Enter a serving endpoint name of letters, digits, '.', '_' and '-'.")
    return name


def check_approval(body, fresh):
    """Raise unless body approves exactly fresh (a plan computed now): HTTP 400 without an approval or below the
    estimate, HTTP 409 for another plan. Nothing has been sent at this point."""
    if body.approved_dbu is None:
        raise HTTPException(400, "Approve the estimated cost first: this request carries no approval, so nothing was sent.")
    if body.plan_id != fresh["plan_id"]:
        raise HTTPException(409, "The checks changed since you saw their estimate (the endpoint, the chosen checks or "
                                 "the price list). Review the new estimate; nothing was sent.")
    if fresh["priced"] and body.approved_dbu + 1e-9 < fresh["total_dbu"]:
        raise HTTPException(400, f"The approval ({body.approved_dbu:g} DBU) is below the estimate "
                                 f"({fresh['total_dbu']:g} DBU), so nothing was sent.")


class EndpointCheckRunner:
    """One run of endpoint checks at a time, with its live progress (state) for GET /api/endpoints/<model>/checks."""

    def __init__(self, manager):
        self.manager = manager
        self.lock = asyncio.Lock()
        self.state = None
        self.task = None

    def public(self, model=None):
        """The current or last run's progress, for one endpoint when model is given."""
        if self.state is None or (model is not None and self.state["model"].lower() != model.strip().lower()):
            return None
        return json.loads(json.dumps(self.state))

    def running(self):
        return self.task is not None and not self.task.done()

    async def start(self, model, plan, kind):
        """Start a run of an approved plan; its progress is in state. HTTP 409 while another runs."""
        async with self.lock:
            if self.running():
                raise HTTPException(409, f"An endpoint check of {self.state['model']} is running. Stop it or wait "
                                         "until it ends; nothing was sent.")
            self.state = {"id": str(uuid.uuid4()), "model": model, "kind": kind, "plan_id": plan["plan_id"],
                          "probes": [probe["id"] for probe in plan["probes"]], "status": "running",
                          "started": endpoint_probes.now(), "finished": None, "error": None, "waiting": None,
                          "approved_dbu": plan["total_dbu"], "reported_dbu": 0.0,
                          "requests": [{"id": request_id, "probe": probe_id, "label": label, "state": "pending",
                                        "http_status": None, "summary": None}
                                       for probe_id, request_id, label in endpoint_probes.planned_requests(plan)]}
            self.task = asyncio.create_task(self.run(model, plan, self.state))
            return self.public()

    async def stop(self):
        """Stop the running check: the request in flight is cancelled and what came back so far is kept."""
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return self.public()

    async def run(self, model, plan, state):
        outcomes, sent = {}, {}
        settings = self.manager.settings
        try:
            host, token = self.manager.serving.credentials()
            async with self.manager.serving.client(serving.stream_timeout(PROBE_READ_TIMEOUT)) as client:
                for entry in state["requests"]:
                    body = endpoint_probes.body(entry["id"], model, outcomes)
                    if body is None:
                        entry.update(state="skipped", summary="Not needed" if entry["id"] in endpoint_probes.ALTERNATIVES
                                     else "Not sent: the reply it builds on did not come")
                        continue
                    wait = self.manager.rate_gate.remaining(model, RATE_OWNER, body["max_tokens"])
                    if wait >= 0.5:
                        # Another chat's 429 on this endpoint: wait with it rather than add to the overload.
                        state["waiting"] = round(wait, 1)
                        await asyncio.sleep(wait)
                        state["waiting"] = None
                    entry["state"] = "running"
                    outcome = await self.send(client, host, token, model, body)
                    outcomes[entry["id"]], sent[entry["id"]] = outcome, body
                    entry.update(state="done", http_status=outcome.get("http_status"),
                                 summary=endpoint_probes.outcome_text(outcome))
                    record = endpoint_probes.call_record(entry["id"], model, outcome)
                    state["reported_dbu"] = round(state["reported_dbu"] + (record.get("estimated_dbu") or 0), 6)
                    status = outcome.get("http_status")
                    if status is None or status in endpoint_probes.STOPPING_STATUSES or status >= 500:
                        state["error"] = (f"Stopped after {entry['id']}: {endpoint_probes.outcome_text(outcome)}. "
                                          "Nothing is retried automatically.")
                        break
            state["status"] = "failed" if state["error"] else "completed"
        except asyncio.CancelledError:
            state["status"] = "stopped"
            raise
        except ValueError as exc:  # The credential file.
            state.update(status="failed", error=settings.redact(str(exc)))
        except Exception as exc:  # Never leave a run marked running.
            state.update(status="failed", error=settings.redact(str(exc) or type(exc).__name__)[:300])
        finally:
            for entry in state["requests"]:
                # A request stopped while it ran may still be billed; a pending one was never sent.
                if entry["state"] in ("pending", "running"):
                    entry["state"] = "stopped" if entry["state"] == "running" else "not_sent"
            state["finished"] = endpoint_probes.now()
            self.save(model, plan, outcomes, sent, state)

    def save(self, model, plan, outcomes, sent, state):
        """Keep the probes that ran (results merge into the endpoint's earlier ones) and each request's fixture."""
        if not outcomes:
            return
        store, redact = self.manager.endpoint_checks, self.manager.settings.redact
        results = {}
        for probe in plan["probes"]:
            result = endpoint_probes.interpret(probe["id"], outcomes)
            if result is not None:
                results[probe["id"]] = result
        calls = [endpoint_probes.call_record(request_id, model, outcome) for request_id, outcome in outcomes.items()]
        try:
            store.save(model, plan["plan_id"], results, calls)
            for request_id, outcome in outcomes.items():
                store.write_fixture(model, request_id, endpoint_probes.fixture(model, request_id, sent[request_id],
                                                                               outcome, redact))
        except (OSError, ValueError) as exc:
            state["error"] = (state.get("error") or "") + f" The results could not be saved: {redact(str(exc))[:200]}"

    async def send(self, client, host, token, model, body):
        """One probe request, read to its end: {http_status, headers, chunks, parsed, error, ttfb, elapsed}."""
        call = {"id": str(uuid.uuid4()), "purpose": "probe"}
        headers = serving.call_headers(serving.request_headers(token), {"id": RATE_OWNER}, call)
        redact = self.manager.settings.redact
        started = clock()
        outcome = {"http_status": None, "error": None, "ttfb": None, "elapsed": None}
        try:
            async with client.stream("POST", serving.invocations_url(host, model), headers=headers,
                                     content=encode_payload(body)) as response:
                outcome.update(http_status=response.status_code, ttfb=round(clock() - started, 2),
                               headers={key: response.headers[key] for key in ("content-type", "retry-after")
                                        if key in response.headers})
                if response.status_code != 200:
                    text, _ = await error_body_prefix(response)
                    outcome["error"] = " ".join(redact(text).split())[:1000]
                    return outcome
                chunks = []
                async for data in sse_data(response):
                    if data == "[DONE]":
                        break
                    try:
                        chunks.append(json.loads(data))
                    except ValueError:
                        chunks.append(None)  # read_stream reports it.
                        break
                    if len(chunks) >= MAX_PROBE_CHUNKS:
                        break
                outcome.update(chunks=chunks, parsed=endpoint_probes.read_stream(chunks))
        except InferenceError as exc:  # An oversized or malformed stream (model_stream bounds).
            outcome["parsed"] = {"error": redact(str(exc))[:300], "finish_reason": None}
        except httpx.HTTPError as exc:
            outcome["error"] = ("The connection failed: " + redact(str(exc) or type(exc).__name__))[:300]
            if outcome["http_status"] == 200:
                outcome["parsed"] = {"error": outcome["error"], "finish_reason": None}
        finally:
            outcome["elapsed"] = round(clock() - started, 2)
        return outcome


def register_endpoint_checks(app, manager, settings):
    runner = manager.endpoint_runner = EndpointCheckRunner(manager)

    def checks(model):
        saved = manager.endpoint_checks.get(model)
        return {"model": model, "saved": saved, "run": runner.public(model),
                "capabilities": endpoint_probes.capabilities(model, saved)}

    @app.post("/api/endpoints/plan")
    async def plan(body: PlanBody):
        """What a check would send and its estimated cost; nothing is sent."""
        return endpoint_probes.plan(endpoint_name(body.model), body.probes)

    @app.post("/api/endpoints/run", status_code=202)
    async def run(body: RunBody):
        model = endpoint_name(body.model)
        fresh = endpoint_probes.plan(model, body.probes)
        check_approval(body, fresh)
        return await runner.start(model, fresh, "check")

    @app.post("/api/endpoints/test", status_code=202)
    async def test(body: TestBody):
        """Test model: the smoke probe alone (one small streamed request), approved like a check."""
        model = endpoint_name(body.model)
        fresh = endpoint_probes.plan(model, ["basic"])
        check_approval(body, fresh)
        return await runner.start(model, fresh, "test")

    @app.post("/api/endpoints/run/stop")
    async def stop():
        return {"run": await runner.stop()}

    @app.get("/api/endpoints/{model}/checks")
    async def saved_checks(model: str):
        """The saved results, the capability table and the current run's progress; nothing is sent."""
        return checks(endpoint_name(model))

    @app.get("/api/endpoints/{model}/state")
    async def endpoint_state(model: str):
        """The endpoint's serving state from Databricks (free: no model request)."""
        name = endpoint_name(model)
        try:
            host, token = manager.serving.credentials()
        except ValueError as exc:
            return {"model": name, "ready": None, "task": None, "error": settings.redact(str(exc))}
        try:
            async with manager.serving.client(serving.LISTING_TIMEOUT) as client:
                response = await client.get(serving.endpoint_url(host, name), headers=serving.request_headers(token))
        except (httpx.HTTPError, ValueError) as exc:
            return {"model": name, "ready": None, "task": None,
                    "error": settings.redact(str(exc) or type(exc).__name__)[:300]}
        if response.status_code != 200:
            return {"model": name, "ready": None, "task": None, "error": f"Databricks returned HTTP {response.status_code}."}
        try:
            data = response.json()
        except ValueError:
            data = None
        data = data if isinstance(data, dict) else {}
        state = data.get("state") if isinstance(data.get("state"), dict) else {}

        def text(value):
            return value[:64] if isinstance(value, str) else None
        return {"model": name, "ready": text(state.get("ready")), "config_update": text(state.get("config_update")),
                "task": text(data.get("task")), "error": None}

    return runner
