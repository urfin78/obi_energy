# Changelog

## v0.4.0-beta.6

**Beta pre-release.** Metadata only — no functional change.

### Changed

- `manifest.json` still pointed at the upstream repository for documentation
  and issue reporting, so Home Assistant directed users to file bugs about
  *this* build at `Karo-X/obi_energy` — including the `state_class` warning
  fixed in beta.5, which came from a commit that only exists in this fork.
  `codeowners`, `documentation` and `issue_tracker` now point here, because
  all three describe the code that actually runs.
- The README gains a fork notice crediting the original integration and
  stating where issues with this build belong, and the installation
  instructions now name this repository's URL instead of upstream's.

Attribution is unchanged: the MIT license file keeps its original
`Copyright (c) 2026 Karo-X` notice — which is what the license requires — and
the README credits the original prominently.

## v0.4.0-beta.5

**Beta pre-release.**

### Fixed

- `sensor.obi_forecast_weekly` and `sensor.obi_forecast_monthly` used
  `state_class: measurement` together with `device_class: energy`, a
  combination Home Assistant rejects. Both sensors logged a warning on every
  startup and poll, and were excluded from long-term statistics — the opposite
  of what v0.4.0-beta.3 intended when it added the `state_class`. They now use
  `state_class: total`, which tolerates a forecast being revised downwards
  (`total_increasing` would read a drop as a meter reset) while keeping
  long-term statistics working.

  Existing statistics for these two entities may need clearing under
  **Developer tools → Statistics** if Home Assistant recorded anything odd
  before this fix.

  The four standby sensors were unaffected: they are `device_class: power`,
  for which `measurement` is correct.

### Confirmed

The JWT expiry handling added in v0.4.0-beta.4 now has a measured value behind
it. With debug logging enabled, the login line reports:

```
OBI login succeeded (token valid until 2027-03-17T13:40:45+00:00)
```

**OBI issues tokens valid for 180 days.** The previous fixed 55-minute refresh
therefore re-authenticated with the account password roughly 4,700 times over a
period the backend expected a single login to cover. Actual logins are now one
per token lifetime instead of ~26 per day.

This makes the hypothesis behind
[Karo-X/obi_energy#25](https://github.com/Karo-X/obi_energy/issues/25)
considerably more plausible: a backend handing out six-month tokens does not
expect a client to re-submit the password every hour, and treating that as
credential stuffing would explain the forced password resets several users have
reported. It is not proof — whether the resets stop is the actual test — but
the login rate is no longer a candidate explanation.

## v0.4.0-beta.4

**Beta pre-release.**

### Changed

- The API client now derives its token lifetime from the **JWT's own `exp`
  claim** and renews five minutes before it expires, instead of logging in
  again on a fixed 55-minute schedule. Only the token payload is decoded (no
  signature check) — it is our own token, and the only question is how long
  OBI considers it valid. The configured `login_refresh_interval` remains the
  fallback for a token without a readable `exp`, so behaviour is unchanged if
  OBI's token format differs from what was observed.

- **Logins are serialized** behind a lock, re-checking token staleness inside
  it. A poll cycle issues seven requests (`/bridges`, `/meter`, `/forecast`,
  and four `/standby` intervals); previously each of them could trigger its
  own login. Now at most one login happens per cycle, and the three
  refresh-after-401 paths share it, reusing a token a concurrent caller
  already fetched.

### Why

Since mid-September several users have reported being forced to set a **new**
heyOBI password every one to two days, not merely to re-enter the existing one
([Karo-X/obi_energy#25](https://github.com/Karo-X/obi_energy/issues/25)).

The cause is not established. The stored password genuinely stops being
accepted — a plain browser with the same password is rejected too, so the
symptom is not specific to this integration. But the integration
re-authenticates with the account password far more often than an app or a
browser ever would, which makes it a plausible trigger.

This release reduces that rate from roughly 26 logins per day to about two,
assuming OBI's token carries a long-lived `exp`. That both removes a possible
cause and makes the question testable: if the forced resets stop, repeated
password logins were involved; if they continue, the cause is server-side.

### Unverified

The effect **depends on OBI's token actually carrying a usable `exp` claim**,
which has not yet been confirmed against the live API. With debug logging
enabled, the login line now reports it:

```
OBI login succeeded (token valid until 2026-09-19T18:23:11+00:00)
```

If that reads `token valid until unknown`, the `exp` claim could not be read
and the previous fixed-interval behaviour applies unchanged — this release then
has no effect on login frequency. Reports of what that line shows are welcome
in the issue above.

## v0.4.0-beta.2

**Beta pre-release.**

### Fixed

- `sensor.obi_standby_daily`/`_weekly`/`_monthly`/`_yearly` were declared as
  energy sensors (Wh), but the `/analytics/.../standby` endpoint actually
  returns an average **power** value in watts (confirmed against the OBI
  app: its "standby power" reading matches the raw API value directly,
  while its separately shown "standby consumption" in kWh is that same
  power figure multiplied by the period length — not a second API field).
  Sensors now use `device_class: power` / unit `W`. The API client method
  was renamed from `async_get_standby_consumption` to
  `async_get_standby_power` to match.

## v0.4.0-beta.1

**Beta pre-release.**

### Added

Nine new sensors, sourced from two previously unused OBI API endpoints and
three fields already present in the `/bridges` response but not yet exposed:

- `sensor.obi_forecast_weekly` / `sensor.obi_forecast_monthly` — consumption
  forecast in Wh from `GET /analytics/{hh_id}/{mid_id}/forecast`.
- `sensor.obi_standby_daily` / `_weekly` / `_monthly` / `_yearly` — standby
  (baseline) consumption in Wh for the last completed period, from
  `GET /analytics/{hh_id}/{mid_id}/{interval}/standby`. Only report a value
  once a full period has elapsed; a freshly set up bridge will show `weekly`,
  `monthly`, and `yearly` as unavailable until enough time has passed.
- `sensor.obi_ota_status` / `sensor.obi_ota_progress` /
  `sensor.obi_upload_interval` — the bridge sensor's `otaStatus`,
  `otaProgress`, and `uploadInterval` fields, already fetched as part of
  every `/bridges` poll but previously discarded.

The standby query always requests a fixed 35-day window regardless of the
configured `historical_duration`, to reliably cover the last completed month
independent of that (often much shorter) setting.

## v0.3.0-beta.1

**Beta pre-release** — published so it can be installed via HACS by enabling
"Show beta versions" for this repository, without affecting users on the
regular v0.2.1 release. Please try it out and report any issues in the
GitHub issues before it's promoted to a regular release.

Adds `switch.obi_live_tracking` (fixes #19) to turn live tracking on/off at
runtime — from an automation, script, or the dashboard — without reloading
the integration. The **Enable live tracking** option now only controls the
state live tracking starts in after a Home Assistant restart; the switch is
what you'd wire up to e.g. disable live tracking during off-peak hours to
save the sensor's battery, and re-enable it later.

## v0.2.1

Raises the default `scan_interval` from 60 to 300 seconds.

OBI's product team confirmed directly that their backend only writes new
readings into its own timeseries database every 5 minutes, regardless of how
often it's polled. Polling more frequently than that doesn't get you fresher
data — it just repeats the same reading and adds unnecessary load on OBI's
servers. Users who already set a custom `scan_interval` are unaffected; this
only changes the out-of-the-box default for new setups.

## v0.2.0

Adds optional live tracking (PR #13, contributed by @la-sina), plus a fix for
stale `energy`/`negative_energy` readings that affects everyone.

### Added

- **Live tracking** (disabled by default, enable via the integration's
  **Configure** options): opens a WebSocket connection to OBI's live-mode
  endpoint and requests a fast (2-second) sensor upload interval, exposing
  four new diagnostic entities — `sensor.obi_live_power`,
  `sensor.obi_live_rssi`, `sensor.obi_live_battery`, and
  `sensor.obi_live_last_message`. `sensor.obi_live_power` reports raw watts as
  sent by OBI; the sign convention for consumption vs. feed-in is not yet
  confirmed. Turning live tracking off (or unloading/reloading the
  integration) restores the sensor's normal 300-second upload interval so the
  physical device doesn't stay in fast-report mode.

### Fixed

- Historical polling for `energy`/`negative_energy` no longer stalls while
  live tracking is active — the live-data listener previously nudged the
  coordinator's regular refresh timer on every live message, which could
  starve the normal historical-data poll when live updates arrived every
  couple of seconds.
- `_latest_measurement` now also recognizes a `time` field (in addition to
  `timestamp`) on historical records, matching a response variant observed
  from OBI's API.

## v0.1.2

Reduces the default `historical_duration` from `PT6H` to `PT15M`.

Community testing (see the OBI Energy Tracker thread on photovoltaikforum.de)
found that a large historical-data window like `PT6H` can make OBI's API
return older data instead of the latest reading, even with the v0.1.1 fix
applied — the symptom looks identical to issue #10 but is caused by the
window size itself, not the request format. Users who already changed
`historical_duration` in the options manually are unaffected; this only
changes the out-of-the-box default for new setups. The options description
now also documents this tradeoff directly in the UI.

## v0.1.1

Fixes #10 (energy/negative_energy measurements stop updating even though
the OBI app shows fresh readings):

- `/historical-data` now sends a single ISO 8601 `<start>/<duration>`
  interval as one `duration` parameter, instead of separate `end=`/
  `duration=` query parameters - matching the request shape confirmed to
  work in another OBI HACS integration. The previous two-parameter form
  may not have been parsed correctly by OBI's backend.
- Added `Cache-Control: no-cache` / `Pragma: no-cache` headers on every
  authenticated request, in case CloudFront (which fronts OBI's API) was
  serving a stale cached response.
- Added debug-level logging of each historical-data poll (record count,
  latest energy/negative_energy timestamp and value) to help diagnose any
  remaining staleness directly from the Home Assistant log.

## v0.1.0

Initial working release:
- UI config flow
- OBI login with country=de
- JWT kept only in memory
- bridge discovery
- energy and feed-in sensors
- kWh sensors compatible with HA Energy Dashboard
- diagnostics sensors
- automatic token refresh / 401 retry
- debug logging without token/password leakage

### Details

First tagged release. The integration is fully config-flow based (no YAML),
logs into the OBI/heyOBI Energy Tracking API, discovers the bridge/sensor,
and exposes native Home Assistant entities for consumption, feed-in, battery,
connectivity and diagnostics.

#### Added

- Config flow: email/password entry, automatic bridge/sensor discovery with
  a picker for multiple bridges, and a manual `HH_ID`/`MID_ID` fallback when
  `/bridges` is unavailable.
- Reauth flow (triggered automatically on authentication failure) and a
  reconfigure flow to update credentials later.
- Options flow: measurement scan interval, login refresh interval,
  historical data duration, debug logging toggle, and manual `HH_ID`/`MID_ID`
  overrides.
- `DataUpdateCoordinator`-based polling with sensors for:
  `sensor.obi_energy`, `sensor.obi_energy_kwh`, `sensor.obi_negative_energy`,
  `sensor.obi_einspeisung_kwh`, `sensor.obi_netto_energy_kwh`,
  `sensor.obi_bridge_battery`, `sensor.obi_bridge_connection_strength`,
  `sensor.obi_last_record_received`, and `binary_sensor.obi_bridge_online`.
- Diagnostics support (redacts email/password; the session token is never
  persisted, logged, or exposed anywhere).
- German and English translations for the config flow, options flow, and
  entity names.
- `hacs.json` for HACS custom-repository installation.

#### Fixed

- Login now sends the exact request shape confirmed via a mitmproxy capture
  of the real heyOBI app: a compact JSON body (`{"password", "country":
  "de", "email"}`, no whitespace) sent as raw bytes via `data=` with an
  explicit `Content-Length`, plus the `Host`/`Origin`/`Referer`/`Cookie`
  headers the app sends. Earlier attempts using aiohttp's `json=` shortcut
  were rejected by CloudFront in front of OBI's login endpoint.
- Entities become `unavailable` (instead of reporting `0`) when login fails,
  `/bridges` can't be retrieved, or historical data is empty/invalid, so
  Home Assistant's Energy Dashboard statistics never see a false reading.
- Config flow and API client now log the real failure reason (DNS, SSL,
  timeout, HTTP status with response headers/truncated body, JSON decode
  errors) instead of a generic "cannot connect", without ever logging
  tokens, passwords, or full request bodies.
- Device info is consistent across all entities (`manufacturer: "OBI"`,
  `model: "heyOBI Energy Tracking"`, device name `"OBI Energy Bridge"`), and
  entity names use `translation_key` so they render correctly per language.
  Each entity's `entity_id` is pinned via `suggested_object_id` (e.g.
  `sensor.obi_energy`) so it stays short and stable regardless of the
  translated display name.
