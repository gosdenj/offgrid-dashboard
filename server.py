#!/usr/bin/env python3
"""The off-grid dashboard, served over HTTP.

The dashboard is a single HTML file that talks to Home Assistant over its
WebSocket API - the browser does the talking, not this server.  So the
server has exactly two jobs:

  * serve that file, with the settings filled in;
  * be where the settings are edited and kept.

The settings - the Home Assistant URL, the long-lived access token, the
site coordinates - live in a JSON file in a Docker volume.  They are not in
the image and they are not in the repository, so a rebuild can never lose
them and a published image can never leak them.  The page is rendered per
request, so a change to the settings is live on the next reload - there is
nothing to restart.

Written against the Python standard library only: there is nothing to
install and nothing to compile.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


# ─────────────────────────────────────────────────────────────────────
# THE SETTINGS
# ─────────────────────────────────────────────────────────────────────

DEFAULTS: dict[str, object] = {
    "hass": "",          # the Home Assistant URL, http:// or https://
    "token": "",         # the Home Assistant long-lived access token
    "lat": 0.0,          # the site's latitude
    "lon": 0.0,          # the site's longitude, east-positive
    "tz": "UTC",         # the site's timezone
    "histEveryMs": 600000,  # how often to ask for history, in ms
}

# The order the fields appear in on the settings page.
FIELDS = ("hass", "token", "lat", "lon", "tz", "histEveryMs")

# The smallest history interval worth asking for.  Home Assistant keeps
# its own history for far longer than this, so a shorter interval only
# wastes requests.
MIN_HIST_MS = 60_000


def validate(data: object) -> tuple[dict[str, object], list[str]]:
    """Check the settings, and return the usable ones with what was wrong.

    A field that is wrong is replaced by its default and reported; the
    other fields are kept, so one bad value does not throw the whole file
    away.
    """
    clean: dict[str, object] = {}
    errors: list[str] = []

    if not isinstance(data, dict):
        return dict(DEFAULTS), ["the settings are not a JSON object"]

    def as_text(key: str) -> str:
        value = data.get(key, "")
        if value is None:
            value = ""
        return str(value).strip()

    def as_number(key: str, low: float, high: float) -> float:
        raw = data.get(key, None)
        if raw is None or raw == "":
            clean[key] = DEFAULTS[key]
            return float(DEFAULTS[key])  # type: ignore[arg-type]
        try:
            number = float(raw)
        except (TypeError, ValueError):
            errors.append(f"{key} has to be a number")
            clean[key] = DEFAULTS[key]
            return float(DEFAULTS[key])  # type: ignore[arg-type]
        if not (low <= number <= high):
            errors.append(f"{key} has to be between {low} and {high}")
            clean[key] = DEFAULTS[key]
            return float(DEFAULTS[key])  # type: ignore[arg-type]
        clean[key] = number
        return number

    hass = as_text("hass")
    if hass and not (hass.startswith("http://") or hass.startswith("https://")):
        errors.append("hass has to start with http:// or https://")
    clean["hass"] = hass

    clean["token"] = as_text("token")

    as_number("lat", -90.0, 90.0)
    as_number("lon", -180.0, 180.0)

    tz = as_text("tz") or "UTC"
    if ZoneInfo is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"tz has to be a timezone name, like Europe/London - not '{tz}'")
            tz = str(DEFAULTS["tz"])
    clean["tz"] = tz

    raw = data.get("histEveryMs", None)
    if raw is None or raw == "":
        clean["histEveryMs"] = DEFAULTS["histEveryMs"]
    else:
        try:
            every = int(float(raw))
        except (TypeError, ValueError):
            errors.append("histEveryMs has to be a whole number of milliseconds")
            every = int(DEFAULTS["histEveryMs"])  # type: ignore[arg-type]
        if every < MIN_HIST_MS:
            errors.append(f"histEveryMs has to be at least {MIN_HIST_MS} (a minute)")
            every = int(DEFAULTS["histEveryMs"])  # type: ignore[arg-type]
        clean["histEveryMs"] = every

    return clean, errors


class Config:
    """The settings file, and the lock that keeps it honest."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()
        self.values: dict[str, object] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        """Read the settings file, if there is one."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            print(f"  cannot read the settings file: {exc}")
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"  the settings file is not valid JSON: {exc}")
            return

        clean, errors = validate(data)
        for problem in errors:
            print(f"  settings problem: {problem}")
        with self.lock:
            self.values = clean

    def save(self, data: object) -> list[str]:
        """Write the settings file.  Returns what was wrong, if anything.

        The write goes to a temporary file and is then renamed over the
        old one, so a crash halfway through cannot leave a half-written
        file behind.
        """
        clean, errors = validate(data)
        if errors:
            return errors

        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".new")
            temporary.write_text(
                json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(self.path)
            self.values = clean
        return []

    def get(self, key: str) -> object:
        with self.lock:
            return self.values.get(key, DEFAULTS.get(key))

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return dict(self.values)

    def configured(self) -> bool:
        """Is there enough here to reach Home Assistant?"""
        values = self.snapshot()
        return bool(values.get("hass")) and bool(values.get("token"))


def seed_from_environment(config: Config) -> None:
    """Write a first settings file from the environment, if there is one.

    This is how a stack gets its first token: the environment variables
    are read once, when the settings file is not there yet, and the file
    is then written into the volume.  After that the settings page is
    the way to change them.
    """
    if config.path.exists():
        return

    from_env: dict[str, object] = {}
    mapping = {
        "OFFGRID_HASS_URL": "hass",
        "OFFGRID_HA_TOKEN": "token",
        "OFFGRID_LAT": "lat",
        "OFFGRID_LON": "lon",
        "OFFGRID_TZ": "tz",
        "OFFGRID_HIST_EVERY_MS": "histEveryMs",
    }
    for variable, key in mapping.items():
        value = os.environ.get(variable, "").strip()
        if value:
            from_env[key] = value

    if not from_env:
        return

    clean, errors = validate(from_env)
    for problem in errors:
        print(f"  settings problem: {problem}")
    if errors:
        print("  the settings from the environment were not written")
        return

    with config.lock:
        config.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = config.path.with_name(config.path.name + ".new")
        temporary.write_text(
            json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(config.path)
        config.values = clean
    print("  the settings file was written from the environment")


# ─────────────────────────────────────────────────────────────────────
# THE PAGE
# ─────────────────────────────────────────────────────────────────────

# The block of the dashboard between these two markers is replaced with
# the settings, as a JavaScript object literal.
CFG_PATTERN = re.compile(r"/\*CFG_START\*/.*?/\*CFG_END\*/", re.S)


def render(html_text: str, values: dict[str, object]) -> str:
    """Put the settings into the dashboard.

    JSON is valid JavaScript for the values involved here - strings,
    numbers - and json.dumps escapes them properly.  The '<' and '>' are
    escaped as well, so that a token containing '</script>' cannot close
    the script block it is sitting in.
    """
    literal = json.dumps(
        {key: values.get(key, DEFAULTS.get(key)) for key in FIELDS},
        separators=(",", ":"),
    )
    literal = (
        literal.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )

    # A lambda, not a string: re.sub would read a backslash in the
    # replacement as an escape, and a token may well contain one.
    rendered, count = CFG_PATTERN.subn(lambda _match: literal, html_text, count=1)
    if count != 1:
        raise ValueError(
            "the dashboard file has no CFG_START/CFG_END markers - "
            "it cannot be given its settings"
        )
    return rendered


# ─────────────────────────────────────────────────────────────────────
# THE SETTINGS PAGE
# ─────────────────────────────────────────────────────────────────────

SETTINGS_CSS = """
:root{
  --bg:#05080d; --panel:#0e141e; --panel2:#141c28; --line:#1f2b3b; --line2:#27374b;
  --fg:#e9f0f8; --fg2:#c2cede; --muted:#8296ad; --solar:#f7b23b; --batt:#3ddc84;
  --bad:#f87171; --good:#34d399; --r:12px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(120% 90% at 50% -20%,#0e1725 0%,var(--bg) 60%);
  color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
  "Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;
  min-height:100vh;padding:32px 20px 64px}
.wrap{max-width:720px;margin:0 auto}
header{display:flex;align-items:center;gap:12px;margin-bottom:6px}
.mark{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;flex:none;
  background:linear-gradient(145deg,#ffc65c,#d9861b);box-shadow:0 0 18px rgba(247,178,59,.35)}
.mark svg{width:20px;height:20px}
h1{font-size:20px;font-weight:650;letter-spacing:.04em;text-transform:uppercase}
.sub{color:var(--muted);font-size:13.5px;margin-bottom:26px}
.card{background:linear-gradient(180deg,var(--panel2) 0%,var(--panel) 100%);
  border:1px solid var(--line);border-radius:var(--r);padding:22px 24px;margin-bottom:18px}
.card h2{font-size:13px;font-weight:650;letter-spacing:.09em;text-transform:uppercase;
  color:var(--solar);margin-bottom:16px}
label{display:block;font-size:12.5px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--muted);margin-bottom:6px}
input[type=text],input[type=password],input[type=number]{width:100%;background:#0a0f17;
  border:1px solid var(--line2);border-radius:8px;color:var(--fg);font:inherit;
  padding:10px 12px;user-select:text;-webkit-user-select:text}
input:focus{outline:none;border-color:var(--solar);box-shadow:0 0 0 3px rgba(247,178,59,.14)}
input[type=password]{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.row.three{grid-template-columns:1fr 1fr 1fr}
.hint{color:var(--muted);font-size:12.5px;margin-top:6px}
.hint code{background:#0a0f17;border:1px solid var(--line);border-radius:5px;padding:1px 5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
button{background:linear-gradient(145deg,#ffc65c,#d9861b);border:0;border-radius:9px;
  color:#1a1206;font:inherit;font-weight:650;font-size:15px;padding:11px 22px;cursor:pointer}
button:hover{filter:brightness(1.06)}
button:disabled{opacity:.5;cursor:default}
.row-btns{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.ghost{background:none;border:1px solid var(--line2);color:var(--fg2)}
.ghost:hover{border-color:var(--muted);filter:none}
.notice{border-radius:var(--r);padding:13px 16px;margin-bottom:18px;font-size:14px;
  border:1px solid}
.ok{background:rgba(52,211,153,.08);border-color:rgba(52,211,153,.35);color:var(--good)}
.warn{background:rgba(251,191,36,.08);border-color:rgba(251,191,36,.35);color:var(--warn)}
.bad{background:rgba(248,113,113,.08);border-color:rgba(248,113,113,.35);color:var(--bad)}
ul{color:var(--fg2);font-size:14px;padding-left:20px}
li{margin:5px 0}
a{color:var(--solar)}
@media (max-width:620px){.row,.row.three{grid-template-columns:1fr}}
"""

SETTINGS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Off-grid dashboard - settings</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
  <header>
    <span class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="#3a2405"
      stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="4.2"/>
      <path d="M12 2v2.6M12 19.4V22M4.2 4.2l1.9 1.9M17.9 17.9l1.9 1.9
        M2 12h2.6M19.4 12H22M4.2 19.8l1.9-1.9M17.9 6.1l1.9-1.9"/>
    </svg></span>
    <h1>Off-grid dashboard</h1>
  </header>
  <p class="sub">Settings for the off-grid solar dashboard.</p>

  __NOTICE__

  <form method="post" action="/settings" id="f">
    <div class="card">
      <h2>Home Assistant</h2>
      <div style="margin-bottom:16px">
        <label for="hass">Address</label>
        <input type="text" id="hass" name="hass" value="__HASS__"
               placeholder="http://homeassistant.local:8123" spellcheck="false" autocomplete="off">
        <p class="hint">The dashboard connects to this over its WebSocket API. The
        dashboard's own page is served from here, so this is the address of
        Home&nbsp;Assistant, not of this dashboard.</p>
      </div>
      <div>
        <label for="token">Long-lived access token</label>
        <input type="password" id="token" name="token" value="" spellcheck="false"
               autocomplete="off" placeholder="__TOKENHINT__">
        <p class="hint">Leave this blank to keep the token already stored. To make a
        new one, open Home&nbsp;Assistant, go to your profile, scroll to the bottom
        and use <em>Long-lived access tokens</em>.</p>
      </div>
    </div>

    <div class="card">
      <h2>The site</h2>
      <p class="hint" style="margin:0 0 16px">These are used for the sunrise and
      sunset times and the sun's height in the sky.</p>
      <div class="row three">
        <div>
          <label for="lat">Latitude</label>
          <input type="text" id="lat" name="lat" value="__LAT__" inputmode="decimal"
                 spellcheck="false" autocomplete="off">
        </div>
        <div>
          <label for="lon">Longitude</label>
          <input type="text" id="lon" name="lon" value="__LON__" inputmode="decimal"
                 spellcheck="false" autocomplete="off">
        </div>
        <div>
          <label for="tz">Timezone</label>
          <input type="text" id="tz" name="tz" value="__TZ__" spellcheck="false"
                 autocomplete="off" list="zones">
          <datalist id="zones">
            <option value="Europe/London"><option value="Europe/Dublin">
            <option value="Europe/Lisbon"><option value="UTC">
          </datalist>
        </div>
      </div>
      <p class="hint">Longitude is east-positive, so the UK is negative.</p>
    </div>

    <div class="card">
      <h2>History</h2>
      <div style="max-width:280px">
        <label for="histEveryMs">Ask for history every (ms)</label>
        <input type="text" id="histEveryMs" name="histEveryMs" value="__HIST__"
               inputmode="numeric" spellcheck="false" autocomplete="off">
      </div>
      <p class="hint">How often the dashboard asks Home&nbsp;Assistant for the
      day's history, in milliseconds. 600000 is ten minutes.</p>
    </div>

    <div class="row-btns">
      <button type="submit">Save</button>
      <a href="/settings" class="ghost" style="text-decoration:none;padding:10px 18px;
         border-radius:9px;font-size:15px">Reset the form</a>
      <a href="/" style="margin-left:auto">Open the dashboard &rarr;</a>
    </div>
  </form>

  <div class="card">
    <h2>Where these live</h2>
    <ul>
      <li>The settings are in a file in the <code>offgrid-config</code> Docker
      volume, mounted at <code>/config</code>. They survive a redeploy.</li>
      <li>The token is <strong>not</strong> in the image and <strong>not</strong> in
      the repository, so a published image cannot leak it.</li>
      <li>Everything on this page is served without a password, so anything that
      can reach this port can read the token. Keep the port on the local
      network.</li>
    </ul>
  </div>
</div>
</body>
</html>
"""


def escape(text: object) -> str:
    """Make a value safe to put in an HTML attribute or element."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def settings_page(values: dict[str, object], notice: str = "") -> bytes:
    token = str(values.get("token", "") or "")
    page = (
        SETTINGS_HTML.replace("__CSS__", SETTINGS_CSS)
        .replace("__NOTICE__", notice)
        .replace("__HASS__", escape(values.get("hass", "")))
        .replace("__TOKENHINT__", "stored - leave blank to keep it" if token else "paste a long-lived access token")
        .replace("__LAT__", escape(values.get("lat", "")))
        .replace("__LON__", escape(values.get("lon", "")))
        .replace("__TZ__", escape(values.get("tz", "")))
        .replace("__HIST__", escape(values.get("histEveryMs", "")))
    )
    return page.encode("utf-8")


# ─────────────────────────────────────────────────────────────────────
# THE SERVER
# ─────────────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "offgrid/1.0"
    protocol_version = "HTTP/1.1"

    # Set by main().
    config: Config
    html_text: str | None
    html_error: str | None

    # ── the plumbing ────────────────────────────────────────────────

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write(
            "  %s  %s\n" % (self.address_string(), fmt % args)
        )

    def send_bytes(
        self,
        body: bytes,
        status: HTTPStatus = HTTPStatus.OK,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_bytes(body, status, "application/json; charset=utf-8")

    # ── the routes ──────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - the name is the framework's
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/":
            self.get_dashboard()
        elif path == "/settings":
            self.get_settings()
        elif path == "/healthz":
            self.send_json(
                {
                    "ok": True,
                    "configured": self.config.configured(),
                    "settings_file": str(self.config.path),
                }
            )
        elif path == "/readyz":
            if self.config.configured():
                self.send_json({"ok": True})
            else:
                self.send_json(
                    {
                        "ok": False,
                        "error": "no Home Assistant address or token yet - "
                        "set them on the settings page",
                    },
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
        elif path == "/favicon.ico":
            self.send_bytes(b"", HTTPStatus.NO_CONTENT, "image/x-icon")
        else:
            self.send_bytes(
                b"<h1>404</h1>", HTTPStatus.NOT_FOUND, "text/html; charset=utf-8"
            )

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != "/settings":
            self.send_bytes(
                b"<h1>404</h1>", HTTPStatus.NOT_FOUND, "text/html; charset=utf-8"
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}

        values = self.config.snapshot()
        submitted = {
            "hass": form.get("hass", ""),
            "token": form.get("token", "").strip(),
            "lat": form.get("lat", ""),
            "lon": form.get("lon", ""),
            "tz": form.get("tz", ""),
            "histEveryMs": form.get("histEveryMs", ""),
        }
        # An empty token means "keep the one that is already stored", so the
        # token never has to be read back off the page to save something
        # else.
        if not submitted["token"]:
            submitted["token"] = str(values.get("token", "") or "")

        problems = self.config.save(submitted)
        if problems:
            notice = (
                '<div class="notice bad"><strong>Not saved.</strong><ul>'
                + "".join(f"<li>{escape(p)}</li>" for p in problems)
                + "</ul></div>"
            )
            self.send_bytes(
                settings_page(self.config.snapshot(), notice),
                HTTPStatus.BAD_REQUEST,
            )
            return

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/settings?saved=1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── the pages ──────────────────────────────────────────────────

    def get_dashboard(self) -> None:
        if self.html_text is None:
            self.send_bytes(
                f"<h1>The dashboard file is missing</h1><p>{escape(self.html_error or '')}</p>"
                .encode("utf-8"),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        try:
            page = render(self.html_text, self.config.snapshot())
        except ValueError as exc:
            self.send_bytes(
                f"<h1>The dashboard cannot be given its settings</h1><p>{escape(exc)}</p>"
                .encode("utf-8"),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self.send_bytes(page.encode("utf-8"))

    def get_settings(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        notice = ""
        if query.get("saved"):
            notice = (
                '<div class="notice ok"><strong>Saved.</strong> The dashboard has '
                "the new settings now - reload it if it is already open.</div>"
            )
        elif not self.config.configured():
            notice = (
                '<div class="notice warn"><strong>Not set up yet.</strong> The '
                "dashboard needs a Home Assistant address and a long-lived access "
                "token before it can show anything.</div>"
            )
        self.send_bytes(settings_page(self.config.snapshot(), notice))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    config_path = Path(
        os.environ.get("OFFGRID_CONFIG", "/config/config.json")
    )
    html_path = Path(
        os.environ.get(
            "OFFGRID_HTML", str(Path(__file__).resolve().parent / "offgrid-dashboard.html")
        )
    )
    host = os.environ.get("OFFGRID_HOST", "0.0.0.0")
    port = int(os.environ.get("OFFGRID_PORT", "8767"))

    config = Config(config_path)
    seed_from_environment(config)

    html_text: str | None = None
    html_error: str | None = None
    try:
        html_text = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        html_error = f"{html_path}: {exc}"
        print(f"  cannot read the dashboard file: {exc}")

    if html_text is not None:
        try:
            render(html_text, config.snapshot())
        except ValueError as exc:
            html_error = str(exc)
            print(f"  the dashboard file cannot be given its settings: {exc}")

    Handler.config = config
    Handler.html_text = html_text
    Handler.html_error = html_error

    httpd = Server((host, port), Handler)

    def stop(_signum: int, _frame: object) -> None:
        # shutdown() waits for serve_forever() to stop, so it must not be
        # called on the thread that is running it - which is this one.
        # A container that ignores SIGTERM makes Docker wait out its
        # whole stop timeout on every redeploy.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(f"\n  Off-grid dashboard -> http://{host}:{port}")
    print(f"  dashboard file     -> {html_path}")
    print(f"  settings file      -> {config.path}")
    print(f"  settings page      -> http://{host}:{port}/settings")
    if not config.configured():
        print("  no Home Assistant address or token yet - set them on the settings page\n")
    else:
        print("  configured\n")

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
