"""Playwright MCP 子进程池 — 管理 Node.js 进程生命周期和健康检查."""
import asyncio
import os
import time
from dataclasses import dataclass, field

from fastmcp import Client
from loguru import logger


@dataclass(eq=False)
class PooledMCP:
    process: asyncio.subprocess.Process
    client: Client
    port: int
    created_at: float = field(default_factory=time.time)
    request_count: int = 0
    last_used: float = field(default_factory=time.time)
    is_healthy: bool = True

    def __hash__(self):
        return id(self.process)


class MCPPoolExhausted(Exception):
    pass


class PlaywrightMCPPool:
    def __init__(
        self,
        max_size: int = 8,
        prewarm: int = 2,
        max_age_minutes: int = 30,
        max_requests: int = 50,
        idle_timeout_minutes: int = 10,
        acquire_timeout: float = 30.0,
        headless: bool = True,
        isolated: bool = True,
    ):
        self.max_size = max_size
        self.prewarm = prewarm
        self.max_age_seconds = max_age_minutes * 60
        self.max_requests = max_requests
        self.idle_timeout_seconds = idle_timeout_minutes * 60
        self.acquire_timeout = acquire_timeout
        self.headless = headless
        self.isolated = isolated

        self._available: asyncio.Queue[PooledMCP] = asyncio.Queue(max_size)
        self._all_instances: set[PooledMCP] = set()
        self._freed_ports: list[int] = []
        self._next_port = 9400
        self._prewarmed = False
        self._eviction_task: asyncio.Task | None = None

    async def start(self):
        logger.info("Starting PlaywrightMCPPool: pre-warming {} instances", self.prewarm)
        for _ in range(self.prewarm):
            pooled = await self._create_instance()
            await self._available.put(pooled)
        self._prewarmed = True
        self._eviction_task = asyncio.create_task(self._evict_expired())

    async def _create_instance(self) -> PooledMCP:
        port = self._freed_ports.pop() if self._freed_ports else self._next_port
        if not self._freed_ports:
            self._next_port += 1

        npx_cmd = "npx.cmd" if os.name == "nt" else "npx"
        args = [
            npx_cmd, "-y", "@playwright/mcp@latest",
            "--port", str(port),
        ]
        if self.headless:
            args.append("--headless")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _drain(stream, name):
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    logger.debug("[{}:{}] {}", name, port, line.decode(errors="replace").rstrip())
            except Exception:
                pass

        asyncio.create_task(_drain(proc.stdout, "out"))
        asyncio.create_task(_drain(proc.stderr, "err"))

        for i in range(60):
            await asyncio.sleep(1.0)
            if proc.returncode is not None:
                stderr_data = b""
                try:
                    stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
                except Exception:
                    pass
                raise RuntimeError(
                    f"MCP process exited early with code {proc.returncode}. "
                    f"stderr: {stderr_data[:500].decode('utf-8', errors='replace')}"
                )
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("localhost", port), timeout=1.0
                )
                writer.close()
                break
            except Exception:
                continue

        client = Client(f"http://localhost:{port}/mcp")
        await client.__aenter__()
        await client.ping()

        pooled = PooledMCP(process=proc, client=client, port=port)
        self._all_instances.add(pooled)
        logger.debug("MCP instance created on port {} (total: {})", port, len(self._all_instances))
        return pooled

    async def acquire(self) -> PooledMCP:
        if not self._prewarmed:
            await self.start()

        pooled = await self._try_get_healthy()
        if pooled:
            return pooled

        if len(self._all_instances) < self.max_size:
            return await self._create_instance()

        deadline = time.time() + self.acquire_timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                pooled = await asyncio.wait_for(self._available.get(), timeout=max(remaining, 0.1))
            except asyncio.TimeoutError:
                raise MCPPoolExhausted(f"No MCP instance available within {self.acquire_timeout}s")
            if self._is_usable(pooled):
                pooled.request_count += 1
                pooled.last_used = time.time()
                return pooled
            await self._destroy_instance(pooled)
        raise MCPPoolExhausted(f"No MCP instance available within {self.acquire_timeout}s")

    async def _try_get_healthy(self) -> PooledMCP | None:
        while True:
            try:
                pooled = self._available.get_nowait()
            except asyncio.QueueEmpty:
                return None
            if self._is_usable(pooled):
                pooled.request_count += 1
                pooled.last_used = time.time()
                return pooled
            await self._destroy_instance(pooled)

    async def release(self, pooled: PooledMCP):
        if pooled not in self._all_instances:
            return

        try:
            pooled.is_healthy = await pooled.client.ping()
        except Exception:
            pooled.is_healthy = False

        if not pooled.is_healthy:
            await self._destroy_instance(pooled)
            if len(self._all_instances) < self.max_size:
                await self._available.put(await self._create_instance())
            return

        age = time.time() - pooled.created_at
        if age > self.max_age_seconds or pooled.request_count >= self.max_requests:
            await self._destroy_instance(pooled)
            return

        idle_time = time.time() - pooled.last_used
        if idle_time > self.idle_timeout_seconds:
            await self._destroy_instance(pooled)
            return

        await self._available.put(pooled)

    def _is_usable(self, pooled: PooledMCP) -> bool:
        if not pooled.is_healthy:
            return False
        if time.time() - pooled.created_at > self.max_age_seconds:
            return False
        if pooled.request_count >= self.max_requests:
            return False
        if time.time() - pooled.last_used > self.idle_timeout_seconds:
            return False
        return True

    async def _destroy_instance(self, pooled: PooledMCP):
        if pooled in self._all_instances:
            self._all_instances.discard(pooled)
        try:
            await pooled.client.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            pooled.process.terminate()
            await pooled.process.wait()
        except Exception as e:
            logger.warning("Error stopping MCP process: {}", e)
        self._freed_ports.append(pooled.port)

    async def _evict_expired(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            to_evict = []
            for pooled in list(self._all_instances):
                if (now - pooled.last_used > self.idle_timeout_seconds
                    or now - pooled.created_at > self.max_age_seconds
                    or pooled.request_count >= self.max_requests):
                    to_evict.append(pooled)
            for pooled in to_evict:
                if pooled in self._all_instances:
                    await self._destroy_instance(pooled)


pool: PlaywrightMCPPool | None = None
