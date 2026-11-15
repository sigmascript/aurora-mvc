"""Exercise generated applications through ASGI, not only source/template checks."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from aurora.cli import create_project


_REPOSITORY = Path(__file__).resolve().parents[2]
_ASGI_PROBE = '''import asyncio
import json
from app import app

async def main():
    results = []
    for path in PATHS:
        events = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            events.append(message)
        await app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "path": path, "raw_path": path.encode(),
            "root_path": "", "query_string": b"", "headers": [],
            "scheme": "http", "server": ("localhost", 80),
            "client": ("127.0.0.1", 10000),
        }, receive, send)
        results.append({"path": path, "status": events[0]["status"],
                        "body": b"".join(event.get("body", b"") for event in events[1:]).decode()})
    print(json.dumps(results))

asyncio.run(main())
'''


@pytest.mark.parametrize("layout,paths,expected", [
    ("minimal", ["/"], ["Hello from acceptance!"]),
    ("mvc", ["/"], ["Welcome to acceptance"]),
    ("modular", ["/", "/blog/"], ["Welcome to acceptance", "Your blog app is ready"]),
])
def test_generated_project_handles_real_asgi_requests(tmp_path, layout, paths, expected):
    """Validate generated imports, routing and Jinja views in a fresh process."""
    project = create_project(
        "acceptance", directory=tmp_path,
        mvc=layout == "mvc", modular=layout == "modular",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_REPOSITORY) + os.pathsep + environment.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", _ASGI_PROBE.replace("PATHS", repr(paths))],
        cwd=project, env=environment, capture_output=True, text=True, check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    responses = json.loads(result.stdout.strip())
    assert [response["path"] for response in responses] == paths
    assert all(response["status"] == 200 for response in responses)
    for response, snippet in zip(responses, expected):
        assert snippet in response["body"]
    assert not (project / "data.sqlite3").exists()
