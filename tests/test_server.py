"""The tests for the dashboard server.

These are the tests that do not need a browser.  The browser ones are in
test_browser.py.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402
from harness import ROOT, dashboard_html, start_dashboard_server, stop_dashboard_server  # noqa: E402


# A Home Assistant long-lived access token is a JSON Web Token: three
# base64url pieces with dots between them.  The first one always starts
# with 'eyJ', which is base64 for a JSON opening brace.
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

SKIP_DIRECTORIES = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stops urllib from following a redirect, so the answer can be seen."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def repository_files() -> list[Path]:
    """Every text file in the repository, apart from the build noise."""
    found = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary file, or one we cannot read
        found.append(path)
    return found


class TestNothingSecretIsCommitted(unittest.TestCase):
    """The repository is public.  A token in it is a token in the world."""

    def test_no_file_in_the_repository_holds_an_access_token(self) -> None:
        for path in repository_files():
            text = path.read_text(encoding="utf-8")
            found = JWT.search(text)
            self.assertIsNone(
                found,
                f"{path.relative_to(ROOT)} looks like it holds an access "
                f"token: {found.group(0)[:24] if found else ''}...",
            )

    def test_the_dashboard_file_has_no_settings_in_it(self) -> None:
        text = dashboard_html()
        self.assertNotIn("hassio.jb-interactive", text)
        self.assertNotIn("50.78511", text)
        self.assertNotIn("-1.07523", text)

    def test_the_settings_file_is_not_in_the_repository(self) -> None:
        self.assertFalse(
            (ROOT / "config.json").exists(),
            "config.json holds the access token and must never be committed",
        )
        self.assertFalse((ROOT / "config").exists())


class TestValidate(unittest.TestCase):
    def test_a_good_set_of_settings_is_accepted(self) -> None:
        clean, errors = server.validate(
            {
                "hass": "http://ha.local:8123",
                "token": "abc",
                "lat": 50.5,
                "lon": -1.5,
                "tz": "Europe/London",
                "histEveryMs": 600000,
            }
        )
        self.assertEqual(errors, [])
        self.assertEqual(clean["hass"], "http://ha.local:8123")
        self.assertEqual(clean["lat"], 50.5)
        self.assertEqual(clean["lon"], -1.5)
        self.assertEqual(clean["tz"], "Europe/London")

    def test_an_address_that_is_not_a_url_is_rejected(self) -> None:
        _, errors = server.validate({"hass": "ha.local:8123"})
        self.assertTrue(any("http" in e for e in errors), errors)

    def test_a_latitude_outside_the_world_is_rejected(self) -> None:
        _, errors = server.validate({"lat": 120})
        self.assertTrue(any("lat" in e for e in errors), errors)

    def test_a_longitude_outside_the_world_is_rejected(self) -> None:
        _, errors = server.validate({"lon": 400})
        self.assertTrue(any("lon" in e for e in errors), errors)

    def test_a_timezone_that_does_not_exist_is_rejected(self) -> None:
        _, errors = server.validate({"tz": "Europe/Atlantis"})
        self.assertTrue(any("tz" in e for e in errors), errors)

    def test_a_history_interval_that_is_too_short_is_rejected(self) -> None:
        _, errors = server.validate({"histEveryMs": 1000})
        self.assertTrue(any("histEveryMs" in e for e in errors), errors)

    def test_one_bad_field_does_not_throw_the_others_away(self) -> None:
        clean, errors = server.validate(
            {"hass": "http://ha.local:8123", "token": "abc", "lat": 999}
        )
        self.assertTrue(errors)
        self.assertEqual(clean["hass"], "http://ha.local:8123")
        self.assertEqual(clean["token"], "abc")
        self.assertEqual(clean["lat"], server.DEFAULTS["lat"])


class TestRender(unittest.TestCase):
    def test_the_settings_are_put_into_the_dashboard(self) -> None:
        page = server.render(
            dashboard_html(),
            {
                "hass": "http://ha.local:8123",
                "token": "s3cret",
                "lat": 50.5,
                "lon": -1.5,
                "tz": "Europe/London",
                "histEveryMs": 600000,
            },
        )
        self.assertNotIn("/*CFG_START*/", page)
        self.assertNotIn("/*CFG_END*/", page)
        self.assertIn('"hass":"http://ha.local:8123"', page)
        self.assertIn('"token":"s3cret"', page)
        self.assertIn('"lat":50.5', page)
        self.assertIn('"tz":"Europe/London"', page)

    def test_a_token_containing_a_script_tag_cannot_close_the_script(self) -> None:
        page = server.render(dashboard_html(), {"token": "</script><script>alert(1)"})
        self.assertNotIn("</script><script>alert(1)", page)
        self.assertIn("\\u003c/script\\u003e", page)

    def test_a_dashboard_file_with_no_markers_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            server.render("<html><body>no markers here</body></html>", {})


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "config.json"
        self.config = server.Config(self.path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_missing_file_leaves_the_defaults(self) -> None:
        self.assertEqual(self.config.get("hass"), "")
        self.assertFalse(self.config.configured())

    def test_saving_and_reading_again_gives_the_same_things(self) -> None:
        problems = self.config.save(
            {
                "hass": "http://ha.local:8123",
                "token": "s3cret",
                "lat": 50.5,
                "lon": -1.5,
                "tz": "Europe/London",
                "histEveryMs": 600000,
            }
        )
        self.assertEqual(problems, [])
        again = server.Config(self.path)
        self.assertEqual(again.get("hass"), "http://ha.local:8123")
        self.assertEqual(again.get("token"), "s3cret")
        self.assertEqual(again.get("lat"), 50.5)
        self.assertTrue(again.configured())

    def test_the_file_with_the_token_in_it_is_not_readable_by_anybody_else(self) -> None:
        self.config.save({"hass": "http://ha.local:8123", "token": "s3cret"})
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"the settings file is mode {mode:o}")

    def test_a_bad_file_is_reported_rather_than_crashing(self) -> None:
        self.path.write_text("{not json at all", encoding="utf-8")
        config = server.Config(self.path)
        self.assertFalse(config.configured())

    def test_a_file_that_is_not_an_object_is_reported(self) -> None:
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        config = server.Config(self.path)
        self.assertFalse(config.configured())


class TestSeedFromEnvironment(unittest.TestCase):
    def test_the_environment_can_write_the_first_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.json"
            config = server.Config(path)
            os.environ["OFFGRID_HASS_URL"] = "http://ha.local:8123"
            os.environ["OFFGRID_HA_TOKEN"] = "s3cret"
            os.environ["OFFGRID_LAT"] = "50.5"
            os.environ["OFFGRID_LON"] = "-1.5"
            os.environ["OFFGRID_TZ"] = "Europe/London"
            try:
                server.seed_from_environment(config)
            finally:
                for key in (
                    "OFFGRID_HASS_URL",
                    "OFFGRID_HA_TOKEN",
                    "OFFGRID_LAT",
                    "OFFGRID_LON",
                    "OFFGRID_TZ",
                ):
                    os.environ.pop(key, None)
            self.assertTrue(path.exists())
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["token"], "s3cret")
            self.assertTrue(config.configured())

    def test_an_existing_file_is_not_overwritten_by_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.json"
            config = server.Config(path)
            config.save({"hass": "http://one.local", "token": "first"})
            os.environ["OFFGRID_HA_TOKEN"] = "second"
            try:
                server.seed_from_environment(config)
            finally:
                os.environ.pop("OFFGRID_HA_TOKEN", None)
            self.assertEqual(config.get("token"), "first")


class TestTheServer(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "config.json"
        self.config = server.Config(path)
        self.config.save(
            {
                "hass": "http://ha.local:8123",
                "token": "s3cret",
                "lat": 50.5,
                "lon": -1.5,
                "tz": "Europe/London",
                "histEveryMs": 600000,
            }
        )
        self.httpd, self.port = start_dashboard_server(self.config)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        stop_dashboard_server(self.httpd)
        self.directory.cleanup()

    def get(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, path: str, fields: dict) -> tuple[int, str]:
        body = "&".join(f"{k}={v}" for k, v in fields.items()).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # A redirect is the answer, so it must not be followed: urllib
        # would follow it and report the page it landed on instead.
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def test_the_dashboard_is_served_with_the_settings_in_it(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('"token":"s3cret"', body)
        self.assertIn('"hass":"http://ha.local:8123"', body)
        self.assertIn('"lat":50.5', body)
        self.assertNotIn("/*CFG_START*/", body)

    def test_healthz_is_always_good(self) -> None:
        status, body = self.get("/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["configured"])

    def test_readyz_says_not_ready_when_there_is_no_token(self) -> None:
        empty = server.Config(Path(self.directory.name) / "empty.json")
        httpd, port = start_dashboard_server(empty)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/readyz", timeout=10
            ) as response:
                self.assertEqual(response.status, 200)
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 503)
        finally:
            stop_dashboard_server(httpd)

    def test_the_settings_page_does_not_hand_out_the_token(self) -> None:
        status, body = self.get("/settings")
        self.assertEqual(status, 200)
        self.assertNotIn("s3cret", body)

    def test_saving_from_the_settings_page_keeps_the_token(self) -> None:
        # The token field is left blank, which means "keep the one that is
        # already stored" - so it never has to be read back off the page.
        status, _ = self.post(
            "/settings",
            {
                "hass": "http://ha.local:8123",
                "token": "",
                "lat": "50.5",
                "lon": "-1.5",
                "tz": "Europe/London",
                "histEveryMs": "600000",
            },
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.config.get("token"), "s3cret")

    def test_saving_a_new_token_takes_effect_at_once(self) -> None:
        status, _ = self.post(
            "/settings",
            {
                "hass": "http://ha.local:8123",
                "token": "brand-new",
                "lat": "50.5",
                "lon": "-1.5",
                "tz": "Europe/London",
                "histEveryMs": "600000",
            },
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.config.get("token"), "brand-new")
        _, body = self.get("/")
        self.assertIn('"token":"brand-new"', body)

    def test_a_bad_setting_is_refused_and_explained(self) -> None:
        status, body = self.post(
            "/settings",
            {
                "hass": "ha.local:8123",
                "token": "s3cret",
                "lat": "50.5",
                "lon": "-1.5",
                "tz": "Europe/London",
                "histEveryMs": "600000",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("Not saved", body)
        self.assertEqual(self.config.get("hass"), "http://ha.local:8123")

    def test_an_unknown_path_is_a_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nowhere")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
