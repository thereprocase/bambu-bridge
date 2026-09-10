# Bambu Bridge

Runs the bambu-bridge server as a Home Assistant Supervisor add-on: a LAN
bridge for Bambu Lab printers exposing a translated REST/WebSocket API on
port 8080. It feeds the **Bambu Bridge** Home Assistant integration and the
companion phone app.

It also serves a self-contained **3D print-progress viewer** in any browser
(`/api/v1/printers/<printer-id>/viz`), reachable over the network from a phone
or embedded in a Lovelace iframe card.

The bridge owns the single connection to the printer — run only one. See
`DOCS.md` for installation and configuration.
