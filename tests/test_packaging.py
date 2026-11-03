import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


def test_uvicorn_outlasts_reverse_proxy_idle_timeouts():
    service = re.search(r"^ExecStart=.*--timeout-keep-alive (\d+)$", read("deploy/lxcwatch.service"), re.M)
    docker = re.search(r'"--timeout-keep-alive", "(\d+)"', read("Dockerfile"))
    assert service and docker
    assert int(service[1]) == int(docker[1]) > 30  # Zoraxy keeps idle upstream connections for 30 seconds
