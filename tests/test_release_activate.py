import http.server
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy" / "release-activate.sh"
SOURCE = Path(__file__).parents[1] / "src"


@pytest.fixture
def health() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/api/v1/health"
    server.shutdown()


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "releases"
    release = root / "spool-beta-test"
    release.mkdir(parents=True)
    # Like the LXC releases: .venv is a link to another tree's environment.
    (release / ".venv").symlink_to(Path(sys.prefix))
    (release / "src").symlink_to(SOURCE)
    calls = tmp_path / "systemctl.log"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "$*" >> {calls}\n')
    systemctl.chmod(0o755)
    return {
        "root": root,
        "release": release,
        "dropins": tmp_path / "dropins",
        "systemctl": systemctl,
        "calls": calls,
    }


def activate(layout: dict[str, Path], health: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={
            "PATH": os.environ["PATH"],
            "DROPIN_DIR": str(layout["dropins"]),
            "RELEASES_ROOT": str(layout["root"]),
            "SYSTEMCTL": str(layout["systemctl"]),
            "HEALTH_URL": health,
            "HEALTH_WAIT_S": "3",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_writes_one_managed_dropin_and_restarts(layout, health) -> None:
    url = "BRIDGE_SPOOLMAN_URL=http://192.0.2.10:7912"
    result = activate(layout, health, str(layout["release"]), "--env", url)
    assert result.returncode == 0, result.stderr
    managed = (layout["dropins"] / "zz-release.conf").read_text()
    release = layout["release"]
    assert f"WorkingDirectory={release}\n" in managed
    assert f"Environment=PYTHONPATH={release}/src\n" in managed
    assert f"Environment={url}\n" in managed
    assert managed.rstrip().endswith(f"ExecStart={release}/.venv/bin/python -m bambu_bridge.main")
    assert "ExecStart=\n" in managed
    assert "release-version=" in managed
    assert [p.name for p in layout["dropins"].iterdir()] == ["zz-release.conf"]
    assert layout["calls"].read_text() == "daemon-reload\nrestart bambu-bridge\n"


def test_reactivation_keeps_one_previous_and_rollback_restores_it(layout, health) -> None:
    first = layout["release"]
    second = layout["root"] / "spool-beta-next"
    second.mkdir()
    (second / ".venv").symlink_to(first / ".venv")
    (second / "src").symlink_to(SOURCE)
    assert activate(layout, health, str(first)).returncode == 0
    assert activate(layout, health, str(second)).returncode == 0
    managed = layout["dropins"] / "zz-release.conf"
    assert f"WorkingDirectory={second}\n" in managed.read_text()
    assert f"WorkingDirectory={first}\n" in (layout["dropins"] / "zz-release.conf.prev").read_text()
    assert activate(layout, health, "--rollback").returncode == 0
    assert f"WorkingDirectory={first}\n" in managed.read_text()
    assert not (layout["dropins"] / "zz-release.conf.prev").exists()
    # With no previous managed file, rollback hands control back to older drop-ins.
    assert activate(layout, health, "--rollback").returncode == 0
    assert not managed.exists()
    assert activate(layout, health, "--rollback").returncode != 0


@pytest.mark.parametrize(
    "args",
    [
        ["--env", "lowercase=x"],
        ["--env", "BRIDGE_X=has space"],
        ["--env", 'BRIDGE_X="quoted"'],
    ],
)
def test_rejects_env_lines_that_could_break_the_unit(layout, health, args) -> None:
    result = activate(layout, health, str(layout["release"]), *args)
    assert result.returncode != 0
    assert not layout["dropins"].exists() or not any(layout["dropins"].iterdir())


def test_refuses_a_release_outside_the_root_or_one_that_does_not_import(
    layout, health, tmp_path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert activate(layout, health, str(outside)).returncode != 0
    broken = layout["root"] / "broken"
    (broken / ".venv" / "bin").mkdir(parents=True)
    (broken / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (broken / "src" / "bambu_bridge").mkdir(parents=True)
    (broken / "src" / "bambu_bridge" / "__init__.py").write_text("raise ImportError('x')\n")
    result = activate(layout, health, str(broken))
    assert result.returncode != 0
    assert "does not import" in result.stderr
    assert not (layout["dropins"] / "zz-release.conf").exists()
    assert not layout["calls"].exists()


def test_unhealthy_restart_is_reported(layout) -> None:
    result = activate(layout, "http://127.0.0.1:9/api/v1/health", str(layout["release"]))
    assert result.returncode != 0
    assert "--rollback" in result.stderr
