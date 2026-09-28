# Contributing

Start with a small issue describing the change and its affected model, firmware,
or platform. Keep pull requests focused and add regression coverage.

Run the bridge tests from the repository root and app tests from `mobile/`.
Hardware controls require source evidence for the intended model, correct
payload construction, state guards, and supervised hardware qualification.
Treat publication, acknowledgment and observed printer state as separate events.

Use synthetic fixtures. Exclude access codes, tokens, serial numbers, private
hostnames, network captures, camera images, signing keys and device databases.
Review staged diffs and run `python3 scripts/privacy_check.py` before committing.
See [SECURITY.md](SECURITY.md) for disclosure and diagnostic guidance.

Contributions are accepted under the repository's AGPL-3.0-only license.
Preserve third-party copyright and license notices.
