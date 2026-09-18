from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from bambu_bridge.library import Capture, LibraryStore

PLUGIN = Path(__file__).parents[1] / "plugins/bridge_library/library_plugin.py"
spec = importlib.util.spec_from_file_location("library_plugin_test", PLUGIN)
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class Connection:
    def __init__(self, store: LibraryStore):
        self.store, self.disconnect = store, True

    def request(self, method: str, resource: str, body: object = None) -> dict:
        parts = urlsplit(resource)
        path = parts.path.split("/")
        if resource == "/captures" and method == "POST":
            return self.store.create("plugin", Capture.model_validate(body))
        if path[-1] == "finalize":
            return self.store.finalize(path[2], "plugin")
        if method == "PUT":
            offset = int(parse_qs(parts.query)["offset"][0])
            result = self.store.append(path[2], unquote(path[-1]), offset, body, "plugin")
            if self.disconnect:
                self.disconnect = False
                raise ConnectionError("Reply was lost after commit")
            return result
        raise AssertionError("Unexpected operation: " + method + " " + resource)


def test_frozen_source_survives_deletion_and_retry_never_dispatches_print(tmp_path: Path) -> None:
    source = tmp_path / "anywhere.stl"
    source.write_bytes(b"exact source bytes")
    outbox = plugin.Outbox(tmp_path / "outbox")
    cid = outbox.enqueue("Part", "2.5-dev", 1, [("original", source)], originals="complete")
    source.unlink()
    bridge = Connection(LibraryStore(tmp_path / "server"))
    with pytest.raises(ConnectionError):
        outbox.deliver(cid, bridge)
    assert outbox.pending() == [cid]
    # New client object simulates Orca restarting after the original disappeared.
    outbox = plugin.Outbox(tmp_path / "outbox")
    assert outbox.deliver(cid, bridge)["state"] == "stored"
    assert outbox.pending() == []
    assert bridge.store.download(cid, "anywhere.stl")[0].read_bytes() == b"exact source bytes"
    assert outbox.deliver(cid, bridge)["state"] == "stored"
    assert bridge.store.usage()["captures"] == 1
    # A torn local receipt must never make an unacknowledged queue entry vanish.
    (outbox.root / cid / "delivered.json").write_text('{"id":')
    assert outbox.pending() == [cid]
    assert outbox.deliver(cid, bridge)["state"] == "stored"
    assert outbox.pending() == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://bridge.example.ts.net",
        "https://user:secret@bridge.example.ts.net",
        "https://example.org",
        "https://bridge.example.ts.net/?token=secret",
        "https://bridge.example.ts.net:8443",
    ],
)
def test_only_https_tailnet_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        plugin.Bridge(origin, "P1S", "bbs_" + "x" * 40)


def test_redirects_are_refused() -> None:
    assert (
        plugin.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.ts.net")
        is None
    )


def test_local_queue_detects_damage_before_any_network_request(tmp_path: Path) -> None:
    source = tmp_path / "source.step"
    source.write_bytes(b"original")
    outbox = plugin.Outbox(tmp_path / "outbox")
    cid = outbox.enqueue("Part", "dev", 1, [("original", source)], originals="complete")
    (outbox.root / cid / "0").write_bytes(b"corrupt!")
    with pytest.raises(ValueError, match="damaged"):
        outbox.deliver(cid, object())
