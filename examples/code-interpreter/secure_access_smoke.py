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

"""Minimal Secure Access smoke test (ingress gateway + static header token).

Requires:
  - server with [ingress] mode = "gateway" and ingress-gateway deployed
  - port-forwards (both):
      kubectl -n opensandbox-system port-forward svc/opensandbox-server 8080:80
      kubectl -n opensandbox-system port-forward svc/opensandbox-ingress-gateway 28080:80
  - SANDBOX_DOMAIN / SANDBOX_API_KEY (multi-tenant: sandbox-alpha → 123456)
  - values: gateway.address must match the gateway port-forward (127.0.0.1:28080)

Important: use_server_proxy must be False for get_endpoint. Server proxy URLs
require OPEN-SANDBOX-API-KEY; Secure Access is enforced on the ingress gateway.

Run:
  export SANDBOX_DOMAIN=localhost:8080
  export SANDBOX_API_KEY=123456
  python secure_access_smoke.py
  HOLD_SECONDS=300 python secure_access_smoke.py   # pause for manual curl
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import httpx
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

HTTP_PORT = 8080
HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "0"))
SECURE_ACCESS_HEADER = "OpenSandbox-Secure-Access"
API_KEY = os.getenv("SANDBOX_API_KEY", "123456")
DOMAIN = os.getenv("SANDBOX_DOMAIN", "localhost:8080")


def _endpoint_url(endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint.rstrip("/") + "/"
    return f"http://{endpoint.rstrip('/')}/"


async def _wait_http_ready(url: str, headers: dict[str, str], timeout_s: float = 60) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=5.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code < 500:
                    return
                last_err = RuntimeError(f"HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001 - poll until ready
                last_err = exc
            await asyncio.sleep(1)
    raise RuntimeError(f"sandbox HTTP endpoint not ready: {last_err}")


async def main() -> None:
    image = os.getenv(
        "SANDBOX_IMAGE",
        "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.1.0",
    )
    # Control-plane calls still go to SANDBOX_DOMAIN (server :8080).
    # use_server_proxy=False so get_endpoint returns gateway.address (e.g. 127.0.0.1:28080).
    sandbox = await Sandbox.create(
        image,
        connection_config=ConnectionConfig(
            domain=DOMAIN,
            api_key=API_KEY,
            use_server_proxy=False,
            request_timeout=timedelta(seconds=120),
        ),
        timeout=timedelta(minutes=15),
        secure_access=True,
        entrypoint=[
            "python3",
            "-m",
            "http.server",
            str(HTTP_PORT),
            "--bind",
            "0.0.0.0",
        ],
    )

    try:
        print(f"sandbox_id={sandbox.id}")
        ep = await sandbox.get_endpoint(HTTP_PORT)
        url = _endpoint_url(ep.endpoint)
        headers = dict(ep.headers or {})
        print(f"endpoint={url}")
        print(f"headers={headers}")

        if "proxy/" in url or "/v1/sandboxes/" in url:
            raise RuntimeError(
                f"endpoint looks like server proxy ({url}); "
                "set use_server_proxy=False and point gateway.address at "
                "the ingress-gateway port-forward (127.0.0.1:28080)."
            )

        token = headers.get(SECURE_ACCESS_HEADER) or headers.get(
            SECURE_ACCESS_HEADER.lower()
        )
        if not token:
            raise RuntimeError(
                f"GetEndpoint missing {SECURE_ACCESS_HEADER}; "
                "is ingress.mode=gateway and secureAccess enabled on create?"
            )

        try:
            await _wait_http_ready(url, headers)
        except RuntimeError as exc:
            raise RuntimeError(
                f"{exc}\nHint: port-forward the gateway:\n"
                "  kubectl -n opensandbox-system port-forward "
                "svc/opensandbox-ingress-gateway 28080:80"
            ) from exc

        no_token_headers = {
            k: v for k, v in headers.items() if k.lower() != SECURE_ACCESS_HEADER.lower()
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            denied = await client.get(url, headers=no_token_headers)
            print(f"without {SECURE_ACCESS_HEADER}: {denied.status_code}")
            assert denied.status_code == 401, (
                f"expected 401 without token, got {denied.status_code}: {denied.text}"
            )

            allowed = await client.get(url, headers=headers)
            print(f"with {SECURE_ACCESS_HEADER}: {allowed.status_code}")
            assert allowed.status_code == 200, (
                f"expected 200 with token, got {allowed.status_code}: {allowed.text}"
            )

        print("OK: Secure Access 静态 Header Token 闭环成功")

        if HOLD_SECONDS > 0:
            hdr_flags = " ".join(f"-H '{k}: {v}'" for k, v in no_token_headers.items())
            print(
                "手动验证:\n"
                f"  # 无 token → 401\n"
                f"  curl -sS -o /dev/null -w '%{{http_code}}\\n' {hdr_flags} {url}\n"
                f"  # 有 token → 200\n"
                f"  curl -sS -o /dev/null -w '%{{http_code}}\\n' "
                f"{hdr_flags} -H '{SECURE_ACCESS_HEADER}: {token}' {url}\n"
                f"挂起 {HOLD_SECONDS}s（Ctrl+C 结束）"
            )
            await asyncio.sleep(HOLD_SECONDS)
    finally:
        await sandbox.destroy()


if __name__ == "__main__":
    asyncio.run(main())
