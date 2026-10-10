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
  - execd image with isolation support (server currently uses >= 1.0.22)
  - create with extensions={"bootstrap.execd.isolation": "enable"}
  - SANDBOX_DOMAIN / API key (multi-tenant: sandbox-alpha → 123456)

Older server charts (e.g. opensandbox-server v0.2.2) may omit copying
opensandbox-session-gate into the sandbox. Set INJECT_SESSION_GATE=1 (default)
to kubectl-copy it from EXECD_IMAGE after create.

Run:
  export SANDBOX_DOMAIN=localhost:8080
  export SANDBOX_API_KEY=123456
  python isolation_sessions_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

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
EXECD_IMAGE = os.getenv(
    "EXECD_IMAGE",
    "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/execd:v1.0.22",
)
# Multi-tenancy: use tenant key; single-tenant: use your server api_key.
API_KEY = os.getenv("SANDBOX_API_KEY", "123456")
DOMAIN = os.getenv("SANDBOX_DOMAIN", "localhost:8080")
K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "sandbox-alpha")
KUBECTL_CONTEXT = os.getenv("KUBECTL_CONTEXT", "kind-dev")
KEEP_SANDBOXES = os.getenv("KEEP_SANDBOXES", "1") != "0"
HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "0"))
# Workaround for servers whose execd-installer init omits session-gate.
INJECT_SESSION_GATE = os.getenv("INJECT_SESSION_GATE", "1") != "0"

GATE_SRC = "/usr/local/libexec/opensandbox-session-gate"
GATE_DST = "/opt/opensandbox/opensandbox-session-gate"


def _text(result) -> str:
    if getattr(result, "text", None):
        return result.text
    logs = getattr(result, "logs", None)
    if logs and getattr(logs, "stdout", None):
        return "".join(m.text for m in logs.stdout)
    return str(result)


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["kubectl", "--context", KUBECTL_CONTEXT, "-n", K8S_NAMESPACE, *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _inject_session_gate(sandbox_id: str) -> None:
    """Copy session-gate from EXECD_IMAGE when the server init skipped it."""
    pod = f"{sandbox_id}-0"
    probe = _kubectl(
        "exec",
        pod,
        "-c",
        "sandbox",
        "--",
        "test",
        "-x",
        GATE_DST,
        check=False,
    )
    if probe.returncode == 0:
        print(f"session-gate already present at {GATE_DST}")
        return

    print(
        f"INJECT_SESSION_GATE: {GATE_DST} missing — copying from {EXECD_IMAGE} "
        f"(server init likely omitted it)"
    )
    extract = "extract-session-gate-smoke"
    _kubectl("delete", "pod", extract, "--ignore-not-found", check=False)
    manifest = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {extract}
spec:
  restartPolicy: Never
  containers:
  - name: c
    image: {EXECD_IMAGE}
    command: ["sleep", "180"]
"""
    subprocess.run(
        [
            "kubectl",
            "--context",
            KUBECTL_CONTEXT,
            "-n",
            K8S_NAMESPACE,
            "apply",
            "-f",
            "-",
        ],
        input=manifest,
        check=True,
        capture_output=True,
        text=True,
    )
    _kubectl("wait", "--for=condition=Ready", f"pod/{extract}", "--timeout=90s")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "opensandbox-session-gate"
            _kubectl("cp", f"{extract}:{GATE_SRC}", str(local))
            _kubectl("cp", str(local), f"{pod}:{GATE_DST}", "-c", "sandbox")
        _kubectl(
            "exec",
            pod,
            "-c",
            "sandbox",
            "--",
            "sh",
            "-c",
            f"chown root:root {GATE_DST} && chmod 0555 {GATE_DST}",
        )
    finally:
        _kubectl("delete", "pod", extract, "--wait=false", check=False)
    print(f"OK: injected {GATE_DST}")


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

        if INJECT_SESSION_GATE:
            await asyncio.to_thread(_inject_session_gate, sandbox.id)

        caps = await sandbox.isolation.capabilities()
        setpriv = getattr(caps, "setpriv_available", None)
        userns = getattr(caps, "userns_available", None)
        print(
            f"capabilities: available={caps.available} isolator={caps.isolator} "
            f"version={caps.version} setpriv={setpriv} userns={userns} "
            f"message={caps.message}"
        )
        if not caps.available:
            hint = (
                "isolation unavailable: need bwrap + "
                f"{GATE_DST}. "
                "If setpriv/userns look fine, the server init container likely "
                "did not copy session-gate (upgrade server past the init-copy "
                "fix, or set INJECT_SESSION_GATE=1)."
            )
            raise AssertionError(f"{hint} message={caps.message}")

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
