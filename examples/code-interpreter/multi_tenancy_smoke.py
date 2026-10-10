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

"""Minimal multi-tenancy smoke test (file provider).

Requires server with:
  [tenants]
  provider = "file"
and tenants.toml mapping:
  123456 -> sandbox-alpha
  sk-beta-demo  -> sandbox-beta

Also requires namespaces sandbox-alpha / sandbox-beta to exist.

Run:
  export SANDBOX_DOMAIN=localhost:8080
  python multi_tenancy_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import timedelta

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

IMAGE = os.getenv(
    "SANDBOX_IMAGE",
    "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.1.0",
)
DOMAIN = os.getenv("SANDBOX_DOMAIN", "localhost:8080")
ALPHA_KEY = os.getenv("TENANT_ALPHA_KEY", "123456")
BETA_KEY = os.getenv("TENANT_BETA_KEY", "sk-beta-demo")
KUBECTL_CONTEXT = os.getenv("KUBECTL_CONTEXT", "kind-dev")
# 默认不销毁，方便 kubectl 查看；KEEP_SANDBOXES=0 才自动 destroy
KEEP_SANDBOXES = os.getenv("KEEP_SANDBOXES", "1") != "0"


def _config(api_key: str) -> ConnectionConfig:
    return ConnectionConfig(
        domain=DOMAIN,
        api_key=api_key,
        use_server_proxy=True,
        request_timeout=timedelta(seconds=120),
    )


def _pod_namespace(sandbox_id: str) -> str:
    for ns in ("sandbox-alpha", "sandbox-beta", "opensandbox"):
        try:
            subprocess.check_output(
                [
                    "kubectl",
                    "--context",
                    KUBECTL_CONTEXT,
                    "-n",
                    ns,
                    "get",
                    "batchsandbox",
                    sandbox_id,
                    "-o",
                    "name",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return ns
        except subprocess.CalledProcessError:
            continue
    raise RuntimeError(f"BatchSandbox {sandbox_id} not found in known namespaces")


async def _create(api_key: str) -> Sandbox:
    return await Sandbox.create(
        IMAGE,
        connection_config=_config(api_key),
        timeout=timedelta(minutes=10),
        entrypoint=["sleep", "infinity"],
    )


async def main() -> None:
    alpha: Sandbox | None = None
    beta: Sandbox | None = None
    try:
        # wrong key → 401
        try:
            await Sandbox.create(
                IMAGE,
                connection_config=_config("sk-invalid"),
                timeout=timedelta(minutes=2),
                entrypoint=["sleep", "infinity"],
            )
            raise AssertionError("expected 401 for invalid tenant key")
        except Exception as exc:  # noqa: BLE001 - SDK wraps status
            msg = str(exc).lower()
            ok = any(
                token in msg
                for token in (
                    "401",
                    "unauthorized",
                    "invalid_api_key",
                    "authentication credentials are invalid",
                    "invalid api key",
                )
            )
            assert ok, f"unexpected error: {exc}"
            print("OK: invalid key → rejected")

        alpha = await _create(ALPHA_KEY)
        beta = await _create(BETA_KEY)
        print(f"alpha_sandbox_id={alpha.id}")
        print(f"beta_sandbox_id={beta.id}")

        alpha_ns = _pod_namespace(alpha.id)
        beta_ns = _pod_namespace(beta.id)
        print(f"alpha_namespace={alpha_ns}")
        print(f"beta_namespace={beta_ns}")
        assert alpha_ns == "sandbox-alpha", alpha_ns
        assert beta_ns == "sandbox-beta", beta_ns

        # list is tenant-scoped
        from opensandbox import SandboxManager
        from opensandbox.models.sandboxes import SandboxFilter

        alpha_mgr = await SandboxManager.create(connection_config=_config(ALPHA_KEY))
        beta_mgr = await SandboxManager.create(connection_config=_config(BETA_KEY))
        try:
            alpha_page = await alpha_mgr.list_sandbox_infos(SandboxFilter())
            beta_page = await beta_mgr.list_sandbox_infos(SandboxFilter())
            alpha_ids = {s.id for s in alpha_page.sandbox_infos}
            beta_ids = {s.id for s in beta_page.sandbox_infos}
        finally:
            await alpha_mgr.close()
            await beta_mgr.close()

        assert alpha.id in alpha_ids, "alpha list missing own sandbox"
        assert beta.id not in alpha_ids, "alpha list leaked beta sandbox"
        assert beta.id in beta_ids, "beta list missing own sandbox"
        assert alpha.id not in beta_ids, "beta list leaked alpha sandbox"
        print("OK: list is namespace-scoped per tenant key")
        print("OK: Multi-tenancy 闭环成功")
        print(
            "查看:\n"
            f"  kubectl --context {KUBECTL_CONTEXT} -n sandbox-alpha get batchsandbox,pod\n"
            f"  kubectl --context {KUBECTL_CONTEXT} -n sandbox-beta get batchsandbox,pod\n"
            f"清理: KEEP_SANDBOXES=0 python multi_tenancy_smoke.py\n"
            f"  或: kubectl -n sandbox-alpha delete batchsandbox {alpha.id}\n"
            f"      kubectl -n sandbox-beta delete batchsandbox {beta.id}"
        )
    finally:
        if KEEP_SANDBOXES:
            print("KEEP_SANDBOXES=1：保留沙箱，跳过 destroy")
            return
        if alpha is not None:
            await alpha.destroy()
        if beta is not None:
            await beta.destroy()


if __name__ == "__main__":
    asyncio.run(main())
