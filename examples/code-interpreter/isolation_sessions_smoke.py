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

"""Minimal Isolation Sessions (bwrap) smoke test.

Requires:
  - execd image with isolation support (server injects session-gate via init)
  - create with extensions={"bootstrap.execd.isolation": "enable"}
  - SANDBOX_DOMAIN / API key (multi-tenant: sandbox-alpha → 123456)

Run:
  export SANDBOX_DOMAIN=localhost:8080
  export SANDBOX_API_KEY=123456
  python isolation_sessions_smoke.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.isolated import (
    CreateIsolatedSessionRequest,
    IsolatedWorkspaceSpec,
)

IMAGE = os.getenv(
    "SANDBOX_IMAGE",
    "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.1.0",
)
# Multi-tenancy: use tenant key; single-tenant: use your server api_key.
API_KEY = os.getenv("SANDBOX_API_KEY", "123456")
DOMAIN = os.getenv("SANDBOX_DOMAIN", "localhost:8080")
KEEP_SANDBOXES = os.getenv("KEEP_SANDBOXES", "1") != "0"
HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "0"))


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
        # Server injects CAP_SYS_ADMIN / apparmor / tmpfs for bwrap when set.
        extensions={"bootstrap.execd.isolation": "enable"},
    )

    try:
        print(f"sandbox_id={sandbox.id}")

        caps = await sandbox.isolation.capabilities()
        setpriv = getattr(caps, "setpriv_available", None)
        userns = getattr(caps, "userns_available", None)
        print(
            f"capabilities: available={caps.available} isolator={caps.isolator} "
            f"version={caps.version} setpriv={setpriv} userns={userns} "
            f"message={caps.message}"
        )
        if not caps.available:
            raise AssertionError(
                "isolation unavailable: need bwrap + "
                "/opt/opensandbox/opensandbox-session-gate from server execd init. "
                f"message={caps.message}"
            )

        # 1) one-shot run_once
        once = await sandbox.isolation.run_once(
            "echo hello-isolation && python3 -c 'print(2+2)'",
            workspace="/workspace",
            profile="strict",
        )
        once_text = _text(once)
        print(f"run_once:\n{once_text}")
        assert "hello-isolation" in once_text
        assert "4" in once_text
        print("OK: run_once")

        # 2) persistent session: shell state + PID namespace
        async with sandbox.isolation.session(
            CreateIsolatedSessionRequest(
                workspace=IsolatedWorkspaceSpec(path="/workspace", mode="overlay"),
                profile="strict",
                idle_timeout_seconds=300,
            )
        ) as session:
            print(f"session_id={session.session_id}")
            r1 = await session.run("export MARK=from-session-1; echo $MARK")
            assert "from-session-1" in _text(r1)

            r2 = await session.run("echo $MARK")
            assert "from-session-1" in _text(r2), "shell state should persist in session"
            print("OK: session shell state persists across run")

            r3 = await session.run("echo $$")
            pid = int(_text(r3).strip().splitlines()[-1])
            assert pid <= 2, f"expected PID 1/2 inside bwrap, got {pid}"
            print(f"OK: PID isolation ($$={pid})")

            # peer session must not see MARK
            async with sandbox.isolation.session(
                CreateIsolatedSessionRequest(
                    workspace=IsolatedWorkspaceSpec(path="/workspace", mode="rw"),
                    profile="strict",
                )
            ) as peer:
                r4 = await peer.run('echo "PEER:${MARK:-empty}"')
                peer_text = _text(r4)
                assert "PEER:empty" in peer_text, peer_text
                print("OK: peer session does not see other session env")

        print("OK: Isolation Sessions 闭环成功")

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
                "  或: KEEP_SANDBOXES=0 python isolation_sessions_smoke.py"
            )
        else:
            await sandbox.destroy()


if __name__ == "__main__":
    asyncio.run(main())
