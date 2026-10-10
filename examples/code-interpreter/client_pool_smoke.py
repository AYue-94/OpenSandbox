# Copyright 2025 The OpenSandbox Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Client Pool demo: warmup latency + teardown that kills idle sandboxes.

See docs/guides/client-pool.md (中文: docs/guides/zh/client-pool.html).

Important:
  pool.shutdown() / ``async with`` only stops the local pool process.
  It does **not** kill idle sandboxes on the server. Always
  ``resize(0)`` + ``release_all_idle()`` (and optionally
  ``SandboxPoolManagerAsync.destroy``) before exit, or leftovers accumulate.

Run:
  export SANDBOX_DOMAIN=localhost:8080
  export SANDBOX_API_KEY=123456
  python client_pool_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import timedelta

from opensandbox import (
    AcquirePolicy,
    InMemoryAsyncPoolStateStore,
    PoolCreationSpec,
    PoolDestroyOptions,
    Sandbox,
    SandboxManager,
    SandboxPoolAsync,
    SandboxPoolManagerAsync,
)
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import SandboxFilter

DOMAIN = os.getenv("SANDBOX_DOMAIN", "localhost:8080")
API_KEY = os.getenv("SANDBOX_API_KEY", "123456")
IMAGE = os.getenv(
    "SANDBOX_IMAGE",
    "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.1.0",
)
POOL_NAME = os.getenv("CLIENT_POOL_NAME", "code-interpreter-client-pool-smoke")
MAX_IDLE = int(os.getenv("CLIENT_POOL_MAX_IDLE", "1"))
WARMUP_WAIT_S = float(os.getenv("CLIENT_POOL_WARMUP_WAIT_S", "180"))
SUITE = "client-pool-smoke"
ENTRYPOINT = ["tail", "-f", "/dev/null"]


def _text(result) -> str:
    if getattr(result, "text", None):
        return result.text
    logs = getattr(result, "logs", None)
    if logs and getattr(logs, "stdout", None):
        return "".join(m.text for m in logs.stdout)
    return str(result)


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


async def _wait_idle(pool: SandboxPoolAsync, want: int, timeout_s: float) -> None:
    """Block until warmup publishes enough idle IDs (acquire does not wait)."""
    deadline = time.perf_counter() + timeout_s
    last = -1
    while time.perf_counter() < deadline:
        snap = await pool.snapshot()
        if snap.idle_count != last:
            print(
                f"  warmup… idle={snap.idle_count}/{snap.max_idle} "
                f"state={snap.state.name} lifecycle={snap.lifecycle_state.name}"
                + (f" last_error={snap.last_error}" if snap.last_error else "")
            )
            last = snap.idle_count
        if snap.idle_count >= want:
            return
        await asyncio.sleep(0.5)
    snap = await pool.snapshot()
    raise TimeoutError(
        f"warmup did not reach idle>={want} within {timeout_s:.0f}s "
        f"(idle={snap.idle_count}/{snap.max_idle}, last_error={snap.last_error})"
    )


async def _kill_suite_sandboxes(config: ConnectionConfig) -> int:
    """Best-effort kill sandboxes tagged with this smoke's metadata (incl. prior runs)."""
    manager = await SandboxManager.create(config)
    killed = 0
    try:
        for _ in range(5):
            infos = await manager.list_sandbox_infos(
                SandboxFilter(metadata={"suite": SUITE}, page_size=50)
            )
            if not infos.sandbox_infos:
                break
            for info in infos.sandbox_infos:
                try:
                    await manager.kill_sandbox(info.id)
                    killed += 1
                    print(f"  killed leftover sandbox_id={info.id}")
                except Exception as exc:  # noqa: BLE001 - best-effort sweep
                    print(f"  kill failed sandbox_id={info.id}: {exc}")
        return killed
    finally:
        await manager.close()


async def _teardown_pool(
    pool: SandboxPoolAsync,
    store: InMemoryAsyncPoolStateStore,
    config: ConnectionConfig,
) -> None:
    """Stop replenish, kill idle buffer, fence namespace, sweep tagged leftovers."""
    print("\n[5] 销毁池子并清理服务端沙箱…")
    # Stop leader from immediately re-warming after we drain idle.
    await pool.resize(0)
    released = await pool.release_all_idle()
    print(f"  release_all_idle killed≈{released}")

    manager = SandboxPoolManagerAsync(
        state_store=store,
        connection_config=config,
        owner_id="client-pool-smoke-destroyer",
    )
    result = await manager.destroy(
        POOL_NAME,
        PoolDestroyOptions(drain_timeout=timedelta(seconds=60)),
    )
    print(
        f"  pool destroy: state={result.state.name} "
        f"drained={result.drained_idle_count} killed={result.killed_idle_count}"
    )

    swept = await _kill_suite_sandboxes(config)
    print(f"  suite metadata sweep killed={swept}")


async def main() -> None:
    config = ConnectionConfig(
        domain=DOMAIN,
        api_key=API_KEY,
        request_timeout=timedelta(seconds=120),
        use_server_proxy=True,
    )
    store = InMemoryAsyncPoolStateStore()
    creation_spec = PoolCreationSpec(
        image=IMAGE,
        entrypoint=ENTRYPOINT,
        metadata={"suite": SUITE},
    )

    # Sweep orphans from earlier runs that exited without teardown.
    print("[0] 清理上次残留 (metadata suite=client-pool-smoke)…")
    prior = await _kill_suite_sandboxes(config)
    print(f"  prior leftovers killed={prior}")

    async with SandboxPoolAsync(
        pool_name=POOL_NAME,
        owner_id="client-pool-smoke",
        max_idle=MAX_IDLE,
        state_store=store,
        connection_config=config,
        creation_spec=creation_spec,
    ) as pool:
        print(f"\npool started: name={POOL_NAME} max_idle={MAX_IDLE}")
        print("\n[1] 等待后台 warmup 填满 idle 缓冲…")
        t0 = time.perf_counter()
        await _wait_idle(pool, want=MAX_IDLE, timeout_s=WARMUP_WAIT_S)
        print(f"  warmup 完成，耗时 {_ms(t0):.0f} ms（这笔成本在后台，不在热路径）")

        idle_before = await pool.snapshot_idle_entries()
        print(f"  idle IDs: {[e.sandbox_id for e in idle_before]}")

        print("\n[2] 热路径 acquire(FAIL_FAST) — 从 idle 缓冲取现成沙箱")
        t1 = time.perf_counter()
        warm = await pool.acquire(
            sandbox_timeout=timedelta(minutes=10),
            policy=AcquirePolicy.FAIL_FAST,
        )
        warm_ms = _ms(t1)
        try:
            snap = await pool.snapshot()
            print(
                f"  acquire 耗时 {warm_ms:.0f} ms  sandbox_id={warm.id}  "
                f"idle 剩余 {snap.idle_count}/{snap.max_idle}"
            )
            result = await warm.commands.run(
                "echo pool-ok",
                opts=RunCommandOpts(timeout=timedelta(seconds=30)),
            )
            out = _text(result).strip()
            print(f"  commands: {out!r}")
            assert "pool-ok" in out, f"unexpected output: {out!r}"
        finally:
            await warm.destroy()

        print("\n[3] 对照：冷创建 Sandbox.create（同等 image/entrypoint）")
        t2 = time.perf_counter()
        cold = await Sandbox.create(
            IMAGE,
            connection_config=config,
            entrypoint=ENTRYPOINT,
            timeout=timedelta(minutes=10),
            metadata={"suite": SUITE},
        )
        cold_ms = _ms(t2)
        try:
            print(f"  create 耗时 {cold_ms:.0f} ms  sandbox_id={cold.id}")
        finally:
            await cold.destroy()

        print("\n[4] 借出后 warmup 会再补 idle…")
        await _wait_idle(pool, want=MAX_IDLE, timeout_s=WARMUP_WAIT_S)
        snap = await pool.snapshot()
        print(f"  idle 已回补到 {snap.idle_count}/{snap.max_idle}")

        print("\n======== 对比 ========")
        print(f"  热 acquire (pool idle): {warm_ms:.0f} ms")
        print(f"  冷 create:              {cold_ms:.0f} ms")
        if cold_ms > 0:
            print(f"  加速比 ≈ {cold_ms / max(warm_ms, 1):.1f}x")

        await _teardown_pool(pool, store, config)

    print("OK: Client Pool demo + teardown 完成")
    print(
        "若仍有更早无 metadata 的残留，可手动:\n"
        "  kubectl -n sandbox-alpha get batchsandbox\n"
        "  kubectl -n sandbox-alpha delete batchsandbox <id>"
    )


if __name__ == "__main__":
    asyncio.run(main())
