"""The browser tests.

These are the ones that need a real browser: they load the page the
server serves, point it at a stub Home Assistant, and check that what it
shows is what the stub said.

The stub is here rather than the live Home Assistant so that the tests
need no network and no token, and so that they mean the same thing at
four in the morning as at four in the afternoon.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import start_dashboard_server, stop_dashboard_server  # noqa: E402
from stub_ha import StubHomeAssistant  # noqa: E402

import server  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - no browser here
    sync_playwright = None

TOKEN = "stub-token"

MPPT1 = "sensor.victron_mppt_100_20_panel_power"
MPPT2 = "sensor.victron_ble_pv_power"
SOC = "sensor.victron_smartshunt_500_state_of_charge"
POWER = "sensor.victron_smartshunt_500_instantaneous_power"
VOLTAGE = "sensor.victron_smartshunt_500_battery_voltage"

STATES = {
    MPPT1: {"s": "120"},
    MPPT2: {"s": "40"},
    SOC: {"s": "87"},
    POWER: {"s": "60"},
    VOLTAGE: {"s": "13.20"},
}


def history() -> dict:
    """A day's worth of readings, for the chart to draw."""
    now = int(time.time())
    midnight = now - (now % 86400)
    points = []
    for hour in range(0, 24):
        stamp = midnight + hour * 3600
        # A rough bell over the middle of the day.
        shape = max(0.0, 1 - abs(hour - 13) / 7.0)
        points.append({"lu": stamp, "s": f"{200 * shape:.1f}"})
    return {
        MPPT1: list(points),
        MPPT2: [dict(p, s=f"{80 * shape:.1f}") for p, shape in
                zip(points, [max(0.0, 1 - abs(h - 13) / 7.0) for h in range(24)])],
        SOC: [{"lu": midnight + h * 3600, "s": f"{60 + h * 1.1:.1f}"} for h in range(24)],
    }


class StubThread:
    """The stub, running in its own thread with its own event loop."""

    def __init__(self, stub: StubHomeAssistant) -> None:
        self.stub = stub
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = threading.Event()
        self.port = 0

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.stub.start())
        self.port = self.stub.port
        self.started.set()
        self.loop.run_forever()

    def start(self) -> int:
        self.thread.start()
        if not self.started.wait(15):
            raise RuntimeError("the stub Home Assistant did not start")
        return self.port

    def stop(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self.stub.stop(), self.loop).result(10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    def push(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10)


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class TestTheDashboardInABrowser(unittest.TestCase):
    """The page, the server and a stub Home Assistant, together."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = StubHomeAssistant(token=TOKEN, states=STATES, history=history())
        cls.stub_thread = StubThread(cls.stub)
        cls.stub_port = cls.stub_thread.start()

        import tempfile

        cls.directory = tempfile.TemporaryDirectory()
        cls.config = server.Config(Path(cls.directory.name) / "config.json")
        cls.config.save(
            {
                "hass": f"http://127.0.0.1:{cls.stub_port}",
                "token": TOKEN,
                "lat": 50.5,
                "lon": -1.5,
                "tz": "Europe/London",
                "histEveryMs": 600000,
            }
        )
        cls.httpd, cls.port = start_dashboard_server(cls.config)

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        stop_dashboard_server(cls.httpd)
        cls.directory.cleanup()
        cls.stub_thread.stop()

    def setUp(self) -> None:
        # A fresh page for every test, and the stub's state put back, so
        # that one test cannot change what another one sees.
        self.stub.states = {k: dict(v) for k, v in STATES.items()}
        self.messages: list[str] = []
        self.page = self.browser.new_page(viewport={"width": 800, "height": 480})
        self.page.on("console", self._console)
        self.page.on("pageerror", self._page_error)
        self.page.goto(f"http://127.0.0.1:{self.port}/")

    def tearDown(self) -> None:
        self.page.close()

    def _console(self, message) -> None:
        if message.type in ("error", "warning"):
            self.messages.append(f"{message.type}: {message.text}")

    def _page_error(self, error) -> None:
        self.messages.append(f"pageerror: {error}")

    def wait_for(self, expression: str, timeout: int = 15000) -> None:
        self.page.wait_for_function(expression, timeout=timeout)

    def wait_until_connected(self) -> None:
        """Wait until the dashboard has authenticated and has its state."""
        self.wait_for(
            "() => document.querySelector('#liveTxt').textContent.trim() === 'live'"
        )
        self.wait_for(
            "() => document.querySelector('#pvNow').textContent.trim() !== '—'"
        )

    def text_of(self, selector: str) -> str:
        # text_content, not inner_text: some of these are SVG elements,
        # which inner_text refuses to touch.
        return (self.page.query_selector(selector).text_content() or "").strip()

    # ── the tests ───────────────────────────────────────────────────

    def test_the_page_authenticates_with_the_token_it_was_given(self) -> None:
        """The token has to reach the browser and be accepted.

        This is the check that matters: not that the token was saved, but
        that the dashboard actually used it.
        """
        self.wait_until_connected()
        self.assertEqual(self.text_of("#liveTxt"), "live")

    def test_the_values_on_the_panel_are_the_ones_the_stub_sent(self) -> None:
        self.wait_until_connected()
        self.assertEqual(self.text_of("#pvNow"), "160")
        self.assertEqual(self.text_of("#socPct"), "87")
        self.assertEqual(self.text_of("#bPow"), "+60")
        self.assertEqual(self.text_of("#bV"), "13.20")

    def test_a_change_arriving_as_a_diff_updates_the_panel(self) -> None:
        """Home Assistant sends a change as a diff, not as a whole state.

        Only the fields that changed are in it, so the dashboard has to
        merge it into the state it already holds.  If it replaces the
        state instead, the other readings vanish - which is what the
        first version did.
        """
        self.wait_until_connected()
        self.assertEqual(self.text_of("#pvNow"), "160")

        self.stub_thread.push(self.stub.push_diff(MPPT1, state="200"))
        self.wait_for("() => document.querySelector('#pvNow').textContent.trim() === '240'")
        self.assertEqual(self.text_of("#pvNow"), "240")
        # The readings that did not change are still there.
        self.assertEqual(self.text_of("#socPct"), "87")

    def test_nothing_spills_outside_the_panel(self) -> None:
        findings = self.page.evaluate(
            """() => {
              const findings = [];
              const stage = document.querySelector('#stage');
              const box = stage.getBoundingClientRect();
              const isSvg = el => el.namespaceURI === 'http://www.w3.org/2000/svg';
              for (const el of stage.querySelectorAll('*')) {
                // An element inside a drawing is checked against its own
                // viewBox further down, not against the panel.
                if (isSvg(el) && isSvg(el.parentElement)) continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) continue;
                if (r.right > box.right + 1 || r.bottom > box.bottom + 1 ||
                    r.left < box.left - 1 || r.top < box.top - 1) {
                  findings.push((el.id || el.className || el.tagName) +
                                ' is outside the panel');
                }
              }
              return findings;
            }"""
        )
        self.assertEqual(findings, [])

    def test_the_text_in_a_drawing_stays_inside_its_viewbox(self) -> None:
        """A viewBox is a promise about the space, and the text is part of it."""
        findings = self.page.evaluate(
            """() => {
              const findings = [];
              for (const svg of document.querySelectorAll('svg')) {
                const box = svg.viewBox.baseVal;
                if (!box || !box.width || !box.height) continue;
                for (const text of svg.querySelectorAll('text')) {
                  const ink = text.getBBox();
                  if (ink.width === 0 && ink.height === 0) continue;
                  if (ink.x < box.x - 0.5 || ink.y < box.y - 0.5 ||
                      ink.x + ink.width > box.x + box.width + 0.5 ||
                      ink.y + ink.height > box.y + box.height + 0.5) {
                    findings.push('"' + text.textContent.trim() +
                                  '" is outside its viewBox');
                  }
                }
              }
              return findings;
            }"""
        )
        self.assertEqual(findings, [])

    def test_the_console_is_clean(self) -> None:
        self.assertEqual(self.messages, [])


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class TestTheDashboardWithNoSettings(unittest.TestCase):
    """A dashboard with nothing configured says so.

    "Not set up" and "your token was refused" are different faults, and a
    wall panel that cannot tell them apart sends the reader off to look
    for a token problem that does not exist.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls.directory = tempfile.TemporaryDirectory()
        cls.config = server.Config(Path(cls.directory.name) / "config.json")
        cls.httpd, cls.port = start_dashboard_server(cls.config)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        stop_dashboard_server(cls.httpd)
        cls.directory.cleanup()

    def test_it_says_it_is_not_set_up_and_points_at_the_settings(self) -> None:
        page = self.browser.new_page(viewport={"width": 800, "height": 480})
        try:
            page.goto(f"http://127.0.0.1:{self.port}/")
            page.wait_for_function(
                "() => document.querySelector('#bnT').textContent.trim() === 'Not set up'",
                timeout=10000,
            )
            self.assertIn(
                "settings page",
                (page.query_selector("#bnM").text_content() or ""),
            )
            self.assertEqual(
                (page.query_selector("#liveTxt").text_content() or "").strip(),
                "not set up",
            )
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
