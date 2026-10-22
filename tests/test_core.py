"""Tests for llmtab.core. Run with: python3 -m unittest discover -s tests"""

import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "llmtab"))
import core  # noqa: E402


class MockResponsesServer(http.server.ThreadingHTTPServer):
    """Serves POST /v1/responses with canned SSE events and records request bodies.

    An event {"sleep": seconds} pauses the stream instead of sending anything.
    """

    def __init__(self, events=None, status=200, error_body=None, headers=None):
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.events = events or []
        self.status = status
        self.error_body = error_body
        self.headers = headers or {}
        self.requests = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self):
        self.shutdown()
        self.server_close()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self.server_address[1]


class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        self.server.requests.append({
            "path": self.path,
            "auth": self.headers["Authorization"],
            "body": json.loads(self.rfile.read(length)),
        })
        if self.server.status != 200:
            payload = json.dumps(self.server.error_body).encode()
            self.send_response(self.server.status)
            for name, value in self.server.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in self.server.events:
                if "sleep" in event:
                    time.sleep(event["sleep"])
                    continue
                self.wfile.write(("event: %s\ndata: %s\n\n" % (event["type"], json.dumps(event))).encode())
                self.wfile.flush()
        except OSError:
            pass


def delta(text):
    return {"type": "response.output_text.delta", "delta": text}


COMPLETED = {"type": "response.completed"}


class MarkerTests(unittest.TestCase):
    def test_typing_third_equals_fires(self):
        self.assertFalse(core.marker_added("", "="))
        self.assertFalse(core.marker_added("=", "=="))
        self.assertTrue(core.marker_added("==", "==="))

    def test_extending_a_marker_does_not_fire_again(self):
        self.assertFalse(core.marker_added("===", "===="))

    def test_marker_after_text_fires_unless_own_line_is_required(self):
        self.assertTrue(core.marker_added("what is 2+2? ==", "what is 2+2? ==="))
        self.assertFalse(core.marker_added("a == b", "a === b", own_line=True))
        self.assertTrue(core.marker_added("  ==", "  ===", own_line=True))

    def test_pasting_several_lines_with_a_marker_fires(self):
        self.assertTrue(core.marker_added("x", "first\n===\nx"))


class BuildInputTests(unittest.TestCase):
    def test_joins_prompt_and_non_empty_tabs_in_order(self):
        text = core.build_input("Intro", "", [("a.txt", "alpha\n"), ("empty", "  \n"), ("b.md", "beta")])
        self.assertEqual(text, "Intro\n\n--- a.txt ---\nalpha\n\n--- b.md ---\nbeta")

    def test_style_guide_follows_the_initial_prompt(self):
        text = core.build_input("Intro", "Be terse.\n", [("a.txt", "alpha")])
        self.assertEqual(text, "Intro\n\n--- Style guide ---\nBe terse.\n\n--- a.txt ---\nalpha")

    def test_nothing_to_ask_when_all_tabs_are_empty(self):
        self.assertEqual(core.build_input("Intro", "Be terse.", [("Unsaved Document 1", "")]), "")


class SettingsAndFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_round_trip_and_unknown_keys_are_dropped(self):
        self.assertEqual(core.load_settings(), core.DEFAULTS)
        settings = core.load_settings()
        settings["tab_name"] = "Answers"
        settings["bogus"] = 1
        core.save_settings(settings)
        loaded = core.load_settings()
        self.assertEqual(loaded["tab_name"], "Answers")
        self.assertNotIn("bogus", loaded)

    def test_hand_edited_values_of_the_wrong_type_or_mode_fall_back(self):
        os.makedirs(os.path.dirname(core.config_path()))
        with open(core.config_path(), "w") as f:
            json.dump({"tab_name": None, "query_on_open": "yes", "style_guide_file": 3,
                       "display_mode": "Streaming", "model": "other-model"}, f)
        loaded = core.load_settings()
        self.assertEqual(loaded, dict(core.DEFAULTS, model="other-model"))

    def test_public_defaults_send_nothing_on_their_own(self):
        self.assertEqual(core.DEFAULTS["api_key_file"], "")
        self.assertFalse(core.DEFAULTS["query_on_open"])

    def test_key_file_wins_over_environment(self):
        key_file = os.path.join(self.tmp.name, "key")
        with open(key_file, "w") as f:
            f.write("sk-file\n")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env"}):
            self.assertEqual(core.read_api_key(key_file), ("sk-file", key_file))
            self.assertEqual(core.read_api_key(key_file + ".missing"), ("sk-env", "$OPENAI_API_KEY"))
            self.assertEqual(core.read_api_key(""), ("sk-env", "$OPENAI_API_KEY"))

    def test_multi_key_file_prefers_the_sk_line(self):
        key_file = os.path.join(self.tmp.name, "keys")
        with open(key_file, "w") as f:
            f.write("old-key-without-prefix\n\nsk-new\n")
        self.assertEqual(core.read_api_key(key_file), ("sk-new", key_file))

    def test_malformed_key_is_rejected_without_quoting_it(self):
        key_file = os.path.join(self.tmp.name, "key")
        with open(key_file, "w") as f:
            f.write("OPENAI_API_KEY = sk-secret\n")
        settings = dict(core.DEFAULTS, api_key_file=key_file)
        with self.assertRaises(core.LLMError) as caught:
            core.stream_response(settings, "q", lambda t: None, core.Cancel())
        self.assertIn("spaces or control characters", str(caught.exception))
        self.assertNotIn("sk-secret", str(caught.exception))

    def test_style_guide_is_optional_but_must_be_readable(self):
        self.assertEqual(core.read_style_guide(""), "")
        guide = os.path.join(self.tmp.name, "style.md")
        with open(guide, "w") as f:
            f.write("Be terse.")
        self.assertEqual(core.read_style_guide(guide), "Be terse.")
        with self.assertRaisesRegex(core.LLMError, "Could not read the style guide"):
            core.read_style_guide(guide + ".missing")

    def test_errors_are_appended_to_the_log(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp.name}):
            core.log_error("first")
            core.log_error("second")
            with open(os.path.join(core.cache_dir(), "errors.log")) as f:
                lines = f.read().splitlines()
        self.assertEqual([line.split(" ", 2)[2] for line in lines], ["first", "second"])

    def test_save_output_never_overwrites(self):
        first = core.save_output(self.tmp.name, "LLM Output", "one", now=0)
        second = core.save_output(self.tmp.name, "LLM Output", "two", now=0)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.basename(first).startswith("LLM-Output-"))
        with open(second) as f:
            self.assertEqual(f.read(), "two")

    def test_empty_save_dir_falls_back_to_the_default(self):
        self.assertEqual(core.save_dir(dict(core.DEFAULTS, save_dir="  ")),
                         os.path.expanduser(core.DEFAULTS["save_dir"]))


class StreamTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def server(self, *args, **kwargs):
        server = MockResponsesServer(*args, **kwargs)
        self.addCleanup(server.stop)
        return server

    def settings(self, server):
        return dict(core.DEFAULTS, base_url=server.base_url, reasoning_effort="high")

    def run_stream(self, server, cancel=None):
        chunks = []
        core.stream_response(self.settings(server), "question", chunks.append, cancel or core.Cancel())
        return chunks

    def test_streams_text_deltas_and_sends_expected_request(self):
        server = self.server([{"type": "response.created"}, delta("Hel"), delta("lo"), COMPLETED])
        self.assertEqual(self.run_stream(server), ["Hel", "lo"])

        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/responses")
        self.assertEqual(request["auth"], "Bearer sk-test")
        self.assertEqual(request["body"]["model"], "gpt-6.1-sol")
        self.assertEqual(request["body"]["reasoning"], {"effort": "high"})
        self.assertEqual(request["body"]["input"], "question")
        self.assertEqual(request["body"]["instructions"], core.DEFAULT_SYSTEM_PROMPT)
        self.assertIs(request["body"]["stream"], True)
        self.assertIs(request["body"]["store"], False)

    def test_http_error_message_is_reported_without_the_masked_key(self):
        server = self.server(status=401, error_body={"error": {
            "message": "Incorrect API key provided: sk-abc1********************wxyz. See the docs."}})
        with self.assertRaises(core.LLMError) as caught:
            self.run_stream(server)
        message = str(caught.exception)
        self.assertRegex(message, "HTTP 401.*Incorrect API key provided: <key> See the docs")
        self.assertNotIn("sk-abc1", message)

    def test_redirects_are_not_followed(self):
        target = self.server([delta("leaked"), COMPLETED])
        server = self.server(status=302, error_body={}, headers={"Location": target.base_url + "/responses"})
        with self.assertRaisesRegex(core.LLMError, "HTTP 302"):
            self.run_stream(server)
        self.assertEqual(target.requests, [])

    def test_plain_http_only_for_this_machine(self):
        settings = dict(core.DEFAULTS, base_url="http://example.com/v1")
        with self.assertRaisesRegex(core.LLMError, "must use https"):
            core.stream_response(settings, "q", lambda t: None, core.Cancel())

    def test_failed_response_is_reported(self):
        server = self.server([delta("partial"),
                              {"type": "response.failed", "response": {"error": {"message": "server overloaded"}}}])
        with self.assertRaisesRegex(core.LLMError, "server overloaded"):
            self.run_stream(server)

    def test_stream_cut_off_before_completion_is_an_error(self):
        server = self.server([delta("half a rep")])
        with self.assertRaisesRegex(core.LLMError, "closed before the reply was complete"):
            self.run_stream(server)

    def test_cancel_stops_reading(self):
        server = self.server([delta("a"), delta("b"), COMPLETED])
        cancel = core.Cancel()
        chunks = []

        def on_delta(text):
            chunks.append(text)
            cancel.set()

        core.stream_response(self.settings(server), "q", on_delta, cancel)
        self.assertEqual(chunks, ["a"])

    def test_cancel_closes_the_connection_while_waiting(self):
        # The model is still "thinking": nothing arrives for 30 seconds.
        server = self.server([{"type": "response.created"}, {"sleep": 30}, delta("late"), COMPLETED])
        cancel = core.Cancel()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        self.assertEqual(self.run_stream(server, cancel), [])
        self.assertLess(time.monotonic() - started, 3)

    def test_missing_key_is_reported(self):
        server = self.server()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(core.LLMError, "No API key"):
                core.stream_response(self.settings(server), "q", lambda t: None, core.Cancel())


if __name__ == "__main__":
    unittest.main()
