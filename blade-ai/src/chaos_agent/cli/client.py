"""HTTP client wrapper for the CLI to communicate with the Agent Server."""

import logging
from typing import Optional

import httpx

from chaos_agent.config.settings import settings

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

    async def recover(self, task_id: str, **kwargs) -> dict:
        return await self.post("/api/v1/recover", {"task_id": task_id, **kwargs})

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
