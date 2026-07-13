import json
import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

host = os.environ.get("MCP_HOST", "127.0.0.1")
port = int(os.environ.get("MCP_PORT", "8000"))
mcp = FastMCP("Ubuntu Pro Client Behave Workshop", host=host, port=port)
ALLOWED_FEATURES = {"features/cli/attach.feature"}
ALLOWED_ENV_VARS = {
    "UACLIENT_BEHAVE_CONTRACT_TOKEN",
    "UACLIENT_BEHAVE_INSTALL_FROM",
    "PYCLOUDLIB_CONFIG",
    "AZURE_CONFIG_DIR",
    "GCE_CREDENTIALS_PATH",
}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthcheck(request):
    return JSONResponse({"status": "ok"})


@mcp.tool()
def list_features() -> str:
    repo_root = resolve_repo_root()
    features_dir = repo_root / "features"
    if not features_dir.exists():
        return json.dumps({"features": []})

    feature_files = sorted(
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in features_dir.rglob("*.feature")
    )
    return json.dumps({"features": feature_files})


@mcp.tool()
def run_behave_scenario(
    feature_file: str,
    scenario_name: str = "",
    releases: list[str] | None = None,
    machine_types: list[str] | None = None,
    timeout: int = 1800,
) -> str:
    if feature_file not in ALLOWED_FEATURES:
        return json.dumps(
            {"ok": False, "error": f"Feature not allowed: {feature_file}"}
        )

    repo_root = resolve_repo_root()
    command = ["tox", "-e", "behave", "--", feature_file]
    if scenario_name:
        command.extend(["--name", scenario_name])
    if releases:
        command.extend(["-D", f"releases={','.join(releases)}"])
    if machine_types:
        command.extend(["-D", f"machine_types={','.join(machine_types)}"])

    env = {
        key: os.environ[key]
        for key in sorted(ALLOWED_ENV_VARS)
        if key in os.environ
    }
    completed = subprocess.run(
        command,
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    return json.dumps(
        {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    )


def resolve_repo_root() -> Path:
    env_value = os.environ.get("UBUNTU_PRO_CLIENT_REPO")
    if env_value:
        return Path(env_value).resolve()

    current = Path(__file__).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "features").exists() and (
            candidate / "tox.ini"
        ).exists():
            return candidate

    return current.parents[3]


def main() -> None:
    mcp.run(transport="sse")
