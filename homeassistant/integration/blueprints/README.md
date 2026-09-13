# Beluga alerts through Home Assistant

Import `beluga_print_alerts.yaml` as an automation blueprint and create one
automation for each printer. Select that printer's **phase** sensor and
**problem** binary sensor from the Bambu Bridge integration, then enter your
existing `notify.mobile_app_...` notification action. No Firebase project,
Beluga background service, or new app is needed on the phone.

This deliberately watches HA entity transitions, not the integration's two
event feeds: the current WebSocket feed uses `event/data`, while the REST poller
uses `kind/body`. Mixing both can duplicate notifications. Pauses are available
through the phase sensor even though the bridge does not emit a named pause event.
New problem codes can alert while the problem sensor remains on.

Delivery runs through the HA Companion app's configured push transport. Its
standard Android cloud push still uses FCM, under HA's existing project; this
does not make that transport Google-free. HA local push instead maintains a
phone connection and has its own battery cost. See the [HA local push docs](https://companion.home-assistant.io/docs/notifications/notification-local/).

The payload includes only the display name you select and a generic alert. It
does not contain a model filename, image, access token or camera stream. High
priority is used for visible notifications; the one-hour TTL allows delayed
delivery to an offline phone. Android can still defer or suppress delivery.

Before turning off Beluga app monitoring, send a synthetic notification through
the chosen HA notify action and verify delivery with the phone locked, both on
home Wi-Fi and away. Use temporary test entities or automation trace tests for
transition checks; do not operate a real printer just to test notifications.
Check completion, pause, failure and problem transitions, repeated unchanged
states, and HA reconnects. Then select **Use Home Assistant alerts** in Beluga.
That choice disables the app monitor; it does not install this automation.

Limitations: transitions HA does not observe while offline are not replayed.
Unknown/unavailable startup states do not generate historical alerts. A failed
print can produce both a phase alert and a problem alert. HA/server availability
and successful phone delivery remain part of device acceptance.
