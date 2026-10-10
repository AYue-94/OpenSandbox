import asyncio
import os
from datetime import timedelta

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import (
    Credential,
    CredentialBinding,
    CredentialProxyConfig,
    NetworkPolicy,
    NetworkRule,
)

HOST = "httpbin.org"
SECRET = "vault-smoke-secret-do-not-leak"
HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "6000"))  # 默认挂 10 分钟方便进容器


async def main() -> None:
    sandbox = await Sandbox.create(
        os.getenv(
            "SANDBOX_IMAGE",
            "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.1.0",
        ),
        connection_config=ConnectionConfig(
            domain=os.getenv("SANDBOX_DOMAIN", "localhost:8080"),
            api_key=os.getenv("SANDBOX_API_KEY", "123456"),
            use_server_proxy=True,  # kind ClusterIP 必需
            request_timeout=timedelta(seconds=120),
        ),
        timeout=timedelta(minutes=150),
        env={"API_TOKEN": "fake-inside-sandbox"},  # 沙箱里只有假值
        network_policy=NetworkPolicy(
            defaultAction="deny",
            egress=[NetworkRule(action="allow", target=HOST)],
        ),
        credential_proxy=CredentialProxyConfig(enabled=True),
    )

    try:
        await sandbox.credential_vault.create(
            credentials=[Credential(name="api-token", source={"value": SECRET})],
            bindings=[
                CredentialBinding(
                    name="httpbin-bearer",
                    match={
                        "schemes": ["https"],
                        "hosts": [HOST],
                        "methods": ["GET"],
                        "paths": ["/headers"],
                    },
                    auth={"type": "bearer", "credential": "api-token"},
                )
            ],
        )

        print(f"sandbox_id={sandbox.id}")
        print(
            "进容器手动试:\n"
            f"  kubectl --context kind-dev -n opensandbox exec -it "
            f"{sandbox.id}-0 -c sandbox -- bash\n"
            "容器内:\n"
            "  echo \"$API_TOKEN\"   # 应是 fake-inside-sandbox\n"
            "  curl -fsS https://httpbin.org/headers   # 应看到 Bearer 真实密钥\n"
            f"挂起 {HOLD_SECONDS}s（Ctrl+C 提前结束；HOLD_SECONDS=秒数 可改）"
        )
        try:
            await asyncio.sleep(HOLD_SECONDS)
        except asyncio.CancelledError:
            raise

        # 沙箱内不带任何 Authorization，靠 vault 注入
        result = await sandbox.commands.run(
            "curl -fsS https://httpbin.org/headers",
            opts=RunCommandOpts(timeout=timedelta(seconds=60)),
        )
        out = "".join(m.text for m in result.logs.stdout)
        print(out)

        assert SECRET in out, "vault 未注入真实密钥"
        assert "fake-inside-sandbox" not in out
        print("OK: Credential Vault 注入成功")
    finally:
        await sandbox.destroy()


if __name__ == "__main__":
    asyncio.run(main())