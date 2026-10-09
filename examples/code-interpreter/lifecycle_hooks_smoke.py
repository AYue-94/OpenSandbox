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

"""Minimal Lifecycle Hooks smoke test (preStart + periodic).

Requires Kubernetes provider (Docker / Fast Sandbox reject lifecycle).
See docs/guides/lifecycle-hooks.md (中文: docs/guides/zh/lifecycle-hooks.html).

Run:
  export SANDBOX_DOMAIN=localhost:8080
  export SANDBOX_API_KEY=123456
  python lifecycle_hooks_smoke.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import (
    LifecycleHook,
    PeriodicLifecycleHook,
    SandboxLifecycle,
)

IMAGE = os.getenv(
    "SANDBOX_IMAGE",
    "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.1.0",
)
API_KEY = os.getenv("SANDBOX_API_KEY", "123456")
DOMAIN = os.getenv("SANDBOX_DOMAIN", "localhost:8080")
KEEP_SANDBOXES = os.getenv("KEEP_SANDBOXES", "1") != "0"
HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "0"))
# Periodic schedule for smoke; guide uses @every 5m — keep short here.
PERIODIC_EVERY = os.getenv("PERIODIC_EVERY", "2s")


def _text(result) -> str:
    if getattr(result, "text", None):
        return result.text
    logs = getattr(result, "logs", None)
    if logs and getattr(logs, "stdout", None):
        return "".join(m.text for m in logs.stdout)
    return str(result)


async def main() -> None:
    sandbox = await Sandbox.create(
        IMAGE,
        connection_config=ConnectionConfig(
            domain=DOMAIN,
            api_key=API_KEY,
            use_server_proxy=True,
            request_timeout=timedelta(seconds=120),
        ),
        timeout=timedelta(minutes=15),
        entrypoint=["tail", "-f", "/dev/null"],
        lifecycle=SandboxLifecycle(
            pre_start=LifecycleHook(
                command=["sh", "-c", "echo ready > /tmp/prestart.done"],
                timeout_seconds=60,
            ),
            periodic=[
                PeriodicLifecycleHook(
                    name="smoke-tick",
                    schedule=f"@every {PERIODIC_EVERY}",
                    command=["sh", "-c", "date -u >> /tmp/periodic.log"],
                    timeout_seconds=30,
                )
            ],
        ),
    )

    try:
        print(f"sandbox_id={sandbox.id}")

        # 1) preStart must have run before entrypoint → file exists
        r1 = await sandbox.commands.run(
            "cat /tmp/prestart.done",
            opts=RunCommandOpts(timeout=timedelta(seconds=30)),
        )
        pre = _text(r1).strip()
        print(f"preStart file: {pre!r}")
        assert "ready" in pre, f"preStart did not write /tmp/prestart.done: {pre!r}"
        print("OK: preStart ran before entrypoint")

        # 2) wait for at least one periodic tick
        deadline = asyncio.get_running_loop().time() + 15
        periodic_text = ""
        while asyncio.get_running_loop().time() < deadline:
            r2 = await sandbox.commands.run(
                "test -f /tmp/periodic.log && cat /tmp/periodic.log || true",
                opts=RunCommandOpts(timeout=timedelta(seconds=30)),
            )
            periodic_text = _text(r2)
            if periodic_text.strip():
                break
            await asyncio.sleep(1)

        print(f"periodic.log:\n{periodic_text}")
        assert periodic_text.strip(), (
            f"periodic hook did not write /tmp/periodic.log within 15s "
            f"(schedule=@every {PERIODIC_EVERY})"
        )
        print("OK: periodic hook ran at least once")
        print("OK: Lifecycle Hooks 闭环成功")

        if HOLD_SECONDS > 0:
            print(
                f"挂起 {HOLD_SECONDS}s 便于进容器查看 "
                f"(kubectl -n sandbox-alpha exec -it {sandbox.id}-0 -c sandbox -- bash)"
            )
            await asyncio.sleep(HOLD_SECONDS)
    finally:
        if KEEP_SANDBOXES:
            print("KEEP_SANDBOXES=1：保留沙箱，跳过 destroy")
            print(
                f"清理: kubectl -n sandbox-alpha delete batchsandbox {sandbox.id}\n"
                "  或: KEEP_SANDBOXES=0 python lifecycle_hooks_smoke.py"
            )
        else:
            await sandbox.destroy()


if __name__ == "__main__":
    asyncio.run(main())
