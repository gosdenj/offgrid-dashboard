# The off-grid dashboard

A single-page dashboard for an off-grid solar installation. It reads the
Victron hardware through Home Assistant and puts it on one 800×480 panel:
the solar power coming in, the battery, the load, and the day's history.

It was built as one HTML file and it is still one HTML file. There is no
framework, no build step and nothing to install to read it.

## How it works

The browser does the talking, not the server:

```
  ┌──────────────────┐    WebSocket    ┌────────────────────┐
  │  the wall panel  │ ──────────────► │  Home Assistant    │
  │  a browser       │ ◄────────────── │  Victron entities  │
  └────────┬─────────┘                 └────────────────────┘
           │  HTTP
           ▼
  ┌──────────────────┐        ┌──────────────────────┐
  │  the dashboard   │ ─────► │  offgrid-config      │
  │  container       │        │  (the settings)      │
  └──────────────────┘        └──────────────────────┘
```

Home Assistant's REST API refuses requests from a browser - the CORS
preflight answers 403 for every origin - but its WebSocket API accepts
any origin. So the dashboard connects straight to Home Assistant and the
server's only job is to hand out the page with the settings in it.

The settings are the Home Assistant address, a long-lived access token,
and the site's coordinates. They live in a Docker volume, and they reach
the browser at the moment the page is served. They are **not** in the
image and **not** in the repository.

## The three pieces

| Piece | Where it lives | Who changes it |
|---|---|---|
| **The image** | GitHub Container Registry | The GitHub workflow, on every push |
| **The stack** | `docker-compose.portainer.yml` in the repository | You, in a commit |
| **The settings** | The `offgrid-config` Docker volume | You, on the settings page |

To deploy it, see [docs/PORTAINER.md](docs/PORTAINER.md).

## The settings page

The dashboard is at `/`, and the settings are at `/settings`:

```
http://<the-host>:8767/           the dashboard
http://<the-host>:8767/settings   the settings
```

The settings page writes a JSON file into the volume. Changing the token
there does not need a redeploy - the page is rendered per request, so the
next reload of the dashboard has the new settings.

## Running it locally

No container needed:

```bash
python3 server.py
```

Then open <http://127.0.0.1:8767/> and fill the settings in on the
settings page.

To point it at something else without touching the settings file, the
address and the token can be given in the query string:

```
http://127.0.0.1:8767/?hass=http://homeassistant.local:8123&token=...
```

## The tests

```bash
python3 -m venv .venv
.venv/bin/pip install playwright websockets
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest discover -s tests
```

The browser tests drive the real page against a stub Home Assistant that
speaks the real WebSocket protocol, so they need no network and no token
and mean the same thing at four in the morning as at four in the
afternoon.

They check the things that are easy to get wrong and hard to notice:

- the token reaches the browser and is accepted - not that it was saved,
  but that the dashboard **used** it;
- a change arriving as a diff updates the panel, and the readings that
  did not change are still there;
- nothing spills outside the 800×480 panel;
- the text in a drawing stays inside its viewBox;
- no file in this repository holds an access token.

## The repository

```
offgrid-dashboard.html     the dashboard itself - one file, no build step
server.py                  serves it, and holds the settings
Dockerfile                 the container
docker-compose.portainer.yml   the stack Portainer deploys
.github/workflows/docker.yml   builds the image and publishes it
tests/                     the tests
docs/PORTAINER.md          how to deploy it
NOTES.md                   what the build found, and what it fixed
```

## The catch

**The dashboard has no authentication.** Anything that can reach the port
can read the page - which carries the access token - and can change the
settings. Keep the port on the local network, or put a password in front
of it.

## Notes on the hardware

The readings, the units and the bugs that were found along the way are
all in [NOTES.md](NOTES.md). The short version:

- the SmartShunt's positive power means **charging**;
- `panel_current` on the MPPT is the **battery-side** current, not the
  panel's, which is why it is labelled "Chg A";
- `time_to_go` is negative while charging, and shows "—" rather than
  "-0.0 days";
- `yield_today` is Wh on one MPPT and kWh on the other.
