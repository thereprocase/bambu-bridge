# Pre-connected private dashboard

An optional dedicated loopback listener authenticates the configured owner using Tailscale Serve identity headers. Configure `BRIDGE_DASHBOARD_PORT`, `BRIDGE_DASHBOARD_ORIGIN` (an HTTPS .ts.net origin), and `BRIDGE_DASHBOARD_LOGIN` (the exact owner login), then point private Tailscale Serve at that loopback port. Do not use Funnel. Keep the listener bound to loopback with proxy-header rewriting disabled.

The gateway checks socket peer, host, HTTPS forwarding, and login. Cross-site origins are rejected; writes and WebSockets require the exact dashboard Origin. Ordinary API listeners do not trust these identity headers. The existing owner key stays server-side and is never returned by the session endpoint or embedded in frontend assets. The browser stores only a non-secret UI marker. Every request is authenticated again by the gateway.

On a fresh browser visit, the dashboard detects the private session, selects the registered printer and bypasses API-key onboarding. Existing deployments without this listener retain explicit key setup. The signed Android APK remains available from the dashboard; native Android pairing and printer transports are unchanged.

Only advertise the HTTPS Tailscale DNS URL. Disable any previous HTTP convenience redirect. A loopback HTTP backend used by Serve is an internal transport, not a published dashboard URL.

Reference: https://tailscale.com/docs/features/tailscale-serve (identity headers and spoofing protection).
