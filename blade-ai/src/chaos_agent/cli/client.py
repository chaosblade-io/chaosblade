"""HTTP client wrapper for the CLI to communicate with the Agent Server."""

import json
import logging
from typing import Optional

import httpx

from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode

logger = logging.getLogger(__name__)


class AgentClient:
    """HTTP client for communicating with the Blade AI Server."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: int = 30,
    ):
        self.base_url = (base_url or f"http://localhost:{settings.server_port}").rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def post(self, path: str, json_data: dict) -> dict:
        """Send a POST request to the agent server."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(self._url(path), json=json_data)
                return response.json()
            except httpx.ConnectError:
                return {
                    "code": 5001,
                    "message": f"Cannot connect to agent server at {self.base_url}",
                }
            except httpx.TimeoutException:
                return {
                    "code": 5001,
                    "message": f"Connection to agent server at {self.base_url} timed out",
                }
            except Exception as e:
                return {"code": 5001, "message": str(e)}

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        """Send a GET request to the agent server."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(self._url(path), params=params)
                return response.json()
            except httpx.ConnectError:
                return {
                    "code": 5001,
                    "message": f"Cannot connect to agent server at {self.base_url}",
                }
            except httpx.TimeoutException:
                return {
                    "code": 5001,
                    "message": f"Connection to agent server at {self.base_url} timed out",
                }
            except Exception as e:
                return {"code": 5001, "message": str(e)}

    # Convenience methods

    async def inject(self, **kwargs) -> dict:
        return await self.post("/api/v1/inject", kwargs)

    def _recover_stream_payload_to_envelope(
        self,
        payload: dict,
        inject_task_id: str,
    ) -> dict:
        """Normalize /recover-stream result payload for CLI output."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return JSONEnvelope.fail(
                code=ResponseCode.RECOVERY_FAILED,
                message="Invalid recover result payload",
                data={"task_id": inject_task_id},
            )

        task_state = str(data.get("task_state") or "")
        target = data.get("target") if isinstance(data.get("target"), dict) else {}
        namespace = str(target.get("namespace") or "")
        names = target.get("names") or []
        targets = [
            {"name": str(name), "namespace": namespace}
            for name in names
        ] if isinstance(names, list) else []

        if task_state == "partial_recovered":
            result = "partial"
        elif task_state == "recovered":
            result = "recovered"
        elif task_state == "failed":
            result = "failed"
        else:
            result = task_state or "unknown"

        normalized = {
            "task_id": inject_task_id,
            "recover_task_id": data.get("task_id", ""),
            "operation": "recover",
            "task_state": task_state,
            "result": result,
            "fault_type": data.get("fault_type", ""),
            "blade_uid": data.get("blade_uid", ""),
            "targets": targets,
            "target": target,
            "verification": data.get("verification"),
            "duration_ms": data.get("duration_ms", 0),
        }
        if data.get("error"):
            normalized["error"] = data["error"]

        if result == "failed":
            return JSONEnvelope.fail(
                code=ResponseCode.RECOVERY_FAILED,
                message=str(data.get("error") or "Recovery failed"),
                data=normalized,
            )
        return JSONEnvelope.ok(data=normalized)

    async def recover(self, task_id: str, **kwargs) -> dict:
        payload = {"task_id": task_id, **kwargs}
        timeout = httpx.Timeout(self.timeout, read=None)
        last_error = ""
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    self._url("/api/v1/recover-stream"),
                    json=payload,
                    headers={"accept": "text/event-stream"},
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line.removeprefix("data:").strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        event_type = event.get("type")
                        if event_type == "error":
                            last_error = event.get("content", "") or last_error
                            continue
                        if event_type != "result":
                            continue
                        content = event.get("content")
                        if isinstance(content, str):
                            return self._recover_stream_payload_to_envelope(
                                json.loads(content),
                                task_id,
                            )
                        if isinstance(content, dict):
                            return self._recover_stream_payload_to_envelope(
                                content,
                                task_id,
                            )
                        return JSONEnvelope.fail(
                            code=ResponseCode.RECOVERY_FAILED,
                            message="Invalid recover result event",
                            data={"task_id": task_id},
                        )
            except httpx.ConnectError:
                return JSONEnvelope.fail(
                    code=ResponseCode.SERVER_SHUTTING_DOWN,
                    message=f"Cannot connect to agent server at {self.base_url}",
                )
            except httpx.TimeoutException:
                return JSONEnvelope.fail(
                    code=ResponseCode.SERVER_SHUTTING_DOWN,
                    message=f"Connection to agent server at {self.base_url} timed out",
                )
            except Exception as e:
                return JSONEnvelope.fail(code=ResponseCode.RECOVERY_FAILED, message=str(e))
        return JSONEnvelope.fail(
            code=ResponseCode.RECOVERY_FAILED,
            message=last_error or "Recover stream completed without result",
            data={"task_id": task_id},
        )

    async def metric(self, task_id: str = "") -> dict:
        if task_id:
            return await self.get(f"/api/v1/metric/{task_id}")
        return await self.get("/api/v1/metric")

    async def list_skills(self, **params) -> dict:
        return await self.get("/api/v1/skills", params=params)

    async def confirm(self, task_id: str, action: str, reason: str = "") -> dict:
        return await self.post(
            f"/api/v1/confirm/{task_id}",
            {"action": action, "reason": reason},
        )

    async def health(self) -> dict:
        return await self.get("/api/v1/health")

    async def version(self) -> dict:
        return await self.get("/api/v1/version")

    async def cleanup(self):
        """No-op for HTTP client (stateless)."""
        pass
