"""Start a local company install next to the dev stack (separate-install model, ADR-007).

    .venv\\Scripts\\python scripts\\dev_company.py            # company "abc"
    .venv\\Scripts\\python scripts\\dev_company.py abc 8101 8001

Runs that company's console/Admin API (default 8101), gateway (8001) and worker on the
same PostgreSQL, in schema <code>, with its own anchors folder. The Stixis settings
(control plane, admin key) are switched off for it. Logs: .dev/logs/<code>-*.log.
Stop with scripts\\dev_down.ps1 (it stops every dev port) or by closing the processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import sqlalchemy as sa

from captain.envfile import DEFAULT_ENV_FILE, dotenv_values

code = sys.argv[1] if len(sys.argv) > 1 else "abc"
admin_port = sys.argv[2] if len(sys.argv) > 2 else "8101"
gw_port = sys.argv[3] if len(sys.argv) > 3 else "8001"

backend = Path(__file__).resolve().parent.parent
os.chdir(backend)
base = dotenv_values(DEFAULT_ENV_FILE)
url = sa.engine.make_url(base["CAPTAIN_DATABASE_URL"]).update_query_dict(
    {"options": f"-csearch_path={code}"}
)
anchors = backend / ".dev" / f"{code}-anchors"
anchors.mkdir(parents=True, exist_ok=True)
logs = backend / ".dev" / "logs"
logs.mkdir(parents=True, exist_ok=True)

env = dict(os.environ)
env.update(
    CAPTAIN_DATABASE_URL=url.render_as_string(hide_password=False),
    CAPTAIN_WORM_DIR=str(anchors),
    CAPTAIN_CONTROL_PLANE="",  # a company install, not the Stixis console
    CAPTAIN_PLATFORM_ADMIN_KEY="",
    CAPTAIN_PUBLIC_CONSOLE_URL=f"http://127.0.0.1:{admin_port}",
    CAPTAIN_PUBLIC_GATEWAY_URL=f"http://127.0.0.1:{gw_port}",
)
env.pop("CAPTAIN_WORM", None)

# The link token the Stixis console uses for this company (Bootstrap, health check).
try:
    from captain.config import build_secrets, load_settings

    engine = sa.create_engine(base["CAPTAIN_DATABASE_URL"])
    with engine.connect() as c:
        doc = c.execute(
            sa.text("select doc from cp_companies where doc->>'code' = :c"), {"c": code}
        ).scalar()
    if doc and doc.get("token_secret_id"):
        secrets = build_secrets(load_settings({**base, **os.environ}), engine)
        env["CAPTAIN_PLATFORM_TOKEN"] = secrets.reveal(doc["token_secret_id"])
    else:
        print(f"note: {code} is not on the Stixis Companies list; Bootstrap will not work")
except Exception as e:  # noqa: BLE001
    print(f"note: could not read {code}'s link token ({type(e).__name__}: {e})")
py = str(backend / ".venv" / "Scripts" / "python.exe")
hidden = 0x08000000 if os.name == "nt" else 0


def start(name: str, args: list[str]) -> None:
    out = open(logs / f"{code}-{name}.log", "w")  # noqa: SIM115 (kept open for the child)
    subprocess.Popen([py, *args], env=env, stdout=out, stderr=subprocess.STDOUT,
                     creationflags=hidden)  # fmt: skip
    print(f"started {code} {name}")


start("worker", ["-m", "captain.workers"])
for name, factory, port in (
    ("admin", "captain.api.admin_main:create_admin_app_from_env", admin_port),
    ("gateway", "captain.api.gateway_main:create_gateway_app_from_env", gw_port),
):
    start(name, ["-m", "uvicorn", "--factory", factory, "--host", "127.0.0.1", "--port", port])

time.sleep(10)
for port in (admin_port, gw_port):
    path = "/admin/v1/health" if port == admin_port else "/health"
    try:
        status = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3).status
    except Exception as e:  # noqa: BLE001
        status = f"no answer ({type(e).__name__})"
    print(f"port {port}: {status}")
print(f"{code} console: http://127.0.0.1:{admin_port}   gateway: http://127.0.0.1:{gw_port}")
