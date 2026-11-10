# Off-grid dashboard — verified findings

Probed the live HA instance (core 2025.2.5, tz Europe/London). The address and
the site's coordinates are left out of this file - they are in the settings, in
the Docker volume, and they are not anybody else's business.

Deliverable: **`offgrid-dashboard.html`** — single file, 800×480, Victron only.

## Transport (critical)

- **REST `/api/*` is unusable from a browser.** HA's CORS preflight returns 403 for every origin
  tested (`localhost:8080`, `localhost:3000`, the HA origin itself, `null`). A `fetch()` from a page
  fails with `TypeError: Failed to fetch`. `?access_token=` query auth also 401s.
- **The WebSocket API works from any origin**, including `Origin: null` (i.e. `file://`).
  → The dashboard must be WebSocket-only. No REST calls at all.
- Live values: `subscribe_entities` (compact format, `event.a[entity_id].s`).
- Chart history: `history/history_during_period` — **not** `history/period` (that is
  `unknown_command`; the real name is `history/history_during_period`).
  Also available: `recorder/statistics_during_period`, `recorder/list_statistic_ids`.
- `history/period`, `recorder/statistics` are `unknown_command` on 2025.2.5.

## Victron entities (the only system used)

SmartShunt 500A — `sensor.victron_smartshunt_500_*`
: SoC %, battery V, battery A, instantaneous W, time_to_go (min, `-1` = charging),
  consumed_amp_hours (Ah, negative = still charged), charge cycles, full discharges,
  charged/discharged energy kWh, min/max battery V, alarm condition + reason, last full charge.

Battery temp/humidity — `sensor.victron_bat_temp_victron_battery_*`
: 12.2 °C, 76 % RH. (The shunt's own `battery_temperature` is `unknown`.)

MPPT 100|20 (SN HQ22166AAZ3) — `sensor.victron_mppt_100_20_*`
: panel_power W, panel_voltage V (PV array, ~33–38 V), panel_current A (**battery-side charge
  current**, despite the name — verified: `battery_voltage × panel_current ≈ panel_power` to 2.2 %
  median error), battery_voltage V, load_current A, yield_today/yesterday/total,
  max_power_today/yesterday, charging_mode, tracking_mode, error, day_number, firmware.

MPPT BLE (2nd array) — `sensor.victron_ble_*`
: pv_power W, battery_voltage V, battery_current A, yield_today kWh, solar_energy_produced kWh,
  mppt_state, mppt_error_reason, is_in_fault_state / has_error / in_float.

## Derived metrics — validated

- `PV_total = mppt_100_20.panel_power + ble.pv_power` — both MPPTs feed the same 12 V bank
  (battery voltages agree to ~0.1 V; MPPT current + BLE current ≈ shunt current).
- `Load = PV_total − smartshunt.instantaneous_power` — stable 26–37 W all day, and works at night
  too (0 W PV, −24 W battery → 24 W load). Only 2 / 550 samples go negative.
- Positive shunt power = **charging** (97 % agreement with SoC rising over 24 h).
- Yield cross-check: integrating panel_power over today gave 109 Wh vs the sensor's 110 Wh
  (MPPT) and 237 Wh vs 230 Wh (BLE) — history is accurate enough to chart.
- Peak tracker cross-check: `max_power_yesterday = 135 W` for the 100|20 matches the observed
  max of `panel_power` (135 W), confirming `panel_power` is real PV power (not battery-side).

## Deliberate exclusions

- **FoxESS** — everything, per request.
- **Octopus / electricity rates** — grid-tied, irrelevant to an off-grid system.
- **`sensor.energy_production_today` / `sensor.power_production_now`** — "Solar production
  forecast" integration, but it forecasts the *home* array (4.7 kWh/day, 582 W now) not the
  off-grid array (201 W now). Including it would mix the two systems, so it is left out.

## Units / gotchas handled in the UI

- `yield_today`: MPPT = Wh, BLE = kWh → normalised to Wh.
- `yield_total`: MPPT = Wh (166 340) → kWh; BLE `solar_energy_produced` already kWh.
- `time_to_go` is negative while charging → show "—" / "∞", not "-0.0 days".
- `consumed_amp_hours` is negative → remaining Ah = −value.
- `alarm_reason` is the string "No error"; `alarm_condition_active` is "OFF".
- PV power vs `panel_voltage × panel_current` differ by 2.6× — the current is battery-side.
  Labelled "Chg A" in the UI rather than "Panel A".

## Bugs found and fixed during the build

1. **WebSocket message-id collision.** `subscribe_entities` and the history request both used id
   `1`; HA rejects the second command, so history silently never arrived (empty chart, empty
   footer). Every outgoing command now uses a unique incrementing id.
2. **`requestAnimationFrame` is throttled to zero in a hidden/background tab**, which would freeze
   a wall-mounted display that loses focus. Rendering is now driven by a timer instead.
3. **Solar maths.** A stray `RAD` factor in the gamma term corrupted the declination and equation
   of time, putting sunrise/sunset ~2 h out and the sun altitude 8° low. Now validated against
   Home Assistant's own `sun.sun` entity (sunset 16:29 computed vs 16:27 from HA — within NOAA
   tolerance; altitude agrees to ~1°).
4. **Unit markup rendered literally.** Footer values carried `<em>` unit spans but were written
   with `textContent`; they now use `innerHTML` via a dedicated `setHtml()` helper, and the
   Yesterday figure is scaled kWh/Wh correctly.
5. **Layout collisions** at 800×480: the radial chart's centre label overlapped the ring, and the
   battery stat grid sat under the SoC sparkline. The radial chart was replaced with a
   full-width area chart with day/night shading and sunrise/sunset markers.
6. **PV tile overflow.** The three stat tiles used a `repeat(3,1fr)` grid whose min-content width
   exceeded the space left by the hero block, pushing "Sun altitude" 19 px past the card edge.
   Fixed with `min-width:0` on the grid and its tiles, plus a shorter label.
7. **Battery tile overlap.** The absolutely-positioned card title sat on top of the SoC ring
   because the card had only 9 px of top padding. The ring now lives on the right of the tile and
   the card has 24 px of top padding, so title and ring no longer collide.
8. **MPPT footers clipped.** Both MPPT cards' status lines overflowed the 168 px card and were
   cut off (`overflow:hidden`). The footers were removed: MPPT1's tracking mode moved into its
   sub-label, MPPT2 gained a "Firmware" row, and a fault now turns the mode badge red instead.
9. **Battery vertical budget.** After the ring moved, the SoC sparkline collapsed to zero height
   because the fixed-height rows oversubscribed the card. Row spacing was tightened and the
   sparkline's box was given explicit offsets.
10. **SVG descender clipping.** The power-flow card's sub-labels ("charging", "off-grid") sit
    close to the lower edge of the SVG viewBox, and the descender of "g" was being shaved off.
    The text rows were moved up and the SVG box sized so the viewBox maps close to 1:1.
11. **Chart axis label overhang.** The centred "24" label overhung the chart's right edge by
    0.15 units, so the plot area now stops at x=400 instead of 404.
12. **Live updates were silently dropped (the big one).** Home Assistant's `subscribe_entities`
    sends the *initial snapshot* as `event.a` — `{entity_id: {s, a, c, lc}}` — but every
    subsequent change arrives as a **diff** in `event.c`:
    `{entity_id: {"+": {"s": "105", "lc": ...}}}`. The handler only understood `event.a`, so the
    dashboard froze on its first snapshot while the socket was still happily pushing ~1 event
    per second. `applyDiff()` now merges the `"+"` additions and `"-"` attribute removals into
    the held state — the diff only carries the fields that actually changed, so it must be
    merged, not substituted. `event.r` (entity removed) is handled too.
13. **Redundant SoC bar removed from the battery tile.** The ring already shows the state of
    charge, so the thin progress bar under the sparkline was dropped. That freed 5 px, which
    went to the sparkline (20.6 px → 25.6 px). The sparkline's viewBox was also corrected from
    `0 0 300 34` to `0 0 300 24` — the old one squashed the trace vertically by ~39 %, leaving
    the distortion at ~1 %. `drawSocSpark()` now reads the viewBox height instead of
    hard-coding it.

## Live-update cadence (measured from the recorder)

The dashboard now reflects every reading the moment it arrives, but the underlying Victron
integration does not report every 5 seconds:

| entity | changes/hour | median gap |
|---|---|---|
| `victron_mppt_100_20_panel_power` | 188 | 11 s |
| `victron_ble_pv_power` | 396 | 9 s |
| `victron_smartshunt_500_instantaneous_power` | 162 | 21 s |
| `victron_smartshunt_500_state_of_charge` | 3 | ~23 min |

So the numbers move roughly every 10–20 s, not every 5 s. That is the sensor's polling rate in
Home Assistant, not a dashboard limitation — end-to-end lag from the socket to the pixels is
under ~150 ms (the render debounce).

## Verified working

Live values, history chart, power-flow arrows, SoC sparkline, footer stats, automatic
reconnect after a forced socket drop, no console errors, no clipped footer text.

Layout is verified programmatically rather than by eye — an automated audit walks every card and
asserts that no descendant escapes the card's border box, that no two cards overlap, that no
text is clipped, and (for SVG) that every text element's real ink bounds
(`getBBox()`, which includes descenders) stay inside the viewBox. All checks report zero
findings.

The live-data path is verified the same way: the socket is instrumented to count event shapes
and the rendered values are sampled, so "the numbers actually move" is asserted, not assumed.

Runs from `file://` as well as over HTTP: the Home Assistant WebSocket API accepts an
`Origin: null` request, which is what `file://` sends.

## Putting it in a container

The dashboard was a file on a disk. It is now a container, and the change is small but not
trivial, because of one thing: **the token was in the file**.

The file carried the Home Assistant address, the long-lived access token and the site's
coordinates, in plain text, in a repository that is public. So:

- the token, the address and the coordinates moved into a **named Docker volume**, as
  `config.json`, written with `0600`;
- the container **serves** the dashboard and **injects** the settings into it per request,
  between two markers in the file, so a change to the settings is live on the next reload and
  needs no restart;
- the file in the repository now has empty defaults, and a test fails the build if anything
  that looks like an access token is committed.

The dashboard still talks to Home Assistant **from the browser** — the server never sees the
readings. That is deliberate: Home Assistant's REST API refuses a browser outright, and its
WebSocket API does not, so there is nothing for a backend to do. The cost is that the token
travels to the browser, which means the page carries it. The same bargain the other dashboards
in this family make, and the reason the port belongs on the LAN.

Two things the container needed that the file did not:

- **Graceful shutdown.** `httpd.shutdown()` waits for `serve_forever()` to stop, so it cannot
  be called from the thread that is running it — which is the thread the signal handler runs
  on. It is called from a separate thread instead. A container that ignores SIGTERM makes
  Docker wait out its whole stop timeout on every redeploy.
- **A precise "not set up" message.** An empty token is not the same fault as a token that was
  refused, and a panel that cannot tell them apart sends the reader off to look for a token
  problem that does not exist.

The layout was not touched — the only edits to the file were the config block and two
messages — but the browser tests re-check it anyway, against a stub Home Assistant that
speaks the real WebSocket protocol, so "nothing spills outside the panel" is asserted rather
than assumed.
