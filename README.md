# Bambu Bridge

A self-hosted bridge and Android companion for Bambu Lab printers on a local
network. The Android app is the primary owner interface; the bridge also
provides a browser dashboard, REST/WebSocket API, OrcaSlicer gateway, and Home
Assistant integration.

## Beta status

P1S is the current target. Feature availability depends on the printer model,
firmware, attached hardware, and qualified protocol support. Controls under
review are withheld. Confirm printer behavior during supervised testing.

The bridge and app are developed together in this repository:

| Location | Contents |
| --- | --- |
| `src/`, `tests/` | Python bridge, browser dashboard, protocol adapters and tests |
| `mobile/` | Android app, native components, assets and app tests |
| `homeassistant/` | Integration and add-on build tooling |
| `companion/`, `plugins/` | Desktop library receiver and slicer helpers |
| `deploy/`, `docs/` | Service installation and user guides |

## Get started

- [Bridge setup](docs/GETTING-STARTED.md)
- [Android setup and build](mobile/README.md)
- [Secure local pairing](docs/LOCAL-PAIRING.md)
- [OrcaSlicer](docs/ORCA.md)
- [Print library](docs/PRINT-LIBRARY.md)
- [Home Assistant](homeassistant/README.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Video performance and optional GPU encoding](docs/video-performance.md)

## Development

Python 3.12+ and Node.js 22 are used for development.

```sh
uv sync --frozen --extra dev
uv run pytest
cd mobile
npm ci
npm test -- --runInBand
npx tsc --noEmit
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change and
[SECURITY.md](SECURITY.md) before sharing diagnostics. Beta reports should
include app/server versions, printer model and firmware, reproduction steps,
and redacted logs.

AGPL-3.0-only. See [LICENSE](LICENSE), [NOTICE](NOTICE), and
[THIRD_PARTY.md](THIRD_PARTY.md). Bambu Lab product names describe compatibility.
This is an independent community project.
