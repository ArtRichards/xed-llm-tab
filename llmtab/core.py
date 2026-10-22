# -*- coding: utf-8 -*-
#
#  core.py - settings, prompt assembly, marker detection and OpenAI streaming
#            for the LLM Tab plugin.
#
#  Pure standard library (no GTK, no xed) so it can be tested on its own.

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.parse

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")  # what gpt-6.1-sol accepts

# incognito: the tab looks like a normal file and only ever shows finished replies.
# streaming: replies stream in, with a waiting line and errors shown in the tab.
DISPLAY_MODES = ("incognito", "streaming")

DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant embedded in a text editor. The user's message contains "
    "the contents of their open editor tabs, each introduced by a '--- name ---' "
    "header. Respond to the requests in them. Your reply is shown in a plain-text "
    "editor tab, so prefer plain text or light Markdown."
)

DEFAULTS = {
    "display_mode": "incognito",
    "tab_name": "LLM Output",
    "model": "gpt-6.1-sol",
    "reasoning_effort": "medium",
    "api_key_file": "",
    "base_url": "https://api.openai.com/v1",
    "save_dir": "~/xed-llm-tab",
    "query_on_open": False,
    "marker_on_own_line": False,
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "initial_prompt": "Here are my open editor tabs:",
    "style_guide_file": "",
}

MARKER_RE = re.compile(r"={3,}")

# OpenAI error messages quote a masked fragment of the key (sk-abc1****wxyz).
MASKED_KEY_RE = re.compile(r"\S*\*{4,}\S*")

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class LLMError(Exception):
    pass


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "xed", "llm-tab.json")


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "xed-llm-tab")


def log_error(message):
    """Append to the error log; incognito mode never shows errors in the tab."""
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        with open(os.path.join(cache_dir(), "errors.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def load_settings():
    settings = dict(DEFAULTS)
    try:
        with open(config_path(), encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        for key, value in stored.items():
            # Hand-edited values of the wrong type fall back to the default.
            if key in DEFAULTS and type(value) is type(DEFAULTS[key]):
                settings[key] = value
    if settings["display_mode"] not in DISPLAY_MODES:
        settings["display_mode"] = DEFAULTS["display_mode"]
    return settings


def save_settings(settings):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, path)


def save_dir(settings):
    return os.path.expanduser(settings["save_dir"].strip() or DEFAULTS["save_dir"])


def _count_markers(text, own_line):
    if own_line:
        return sum(1 for line in text.split("\n") if MARKER_RE.fullmatch(line.strip()))
    return len(MARKER_RE.findall(text))


def marker_added(before, after, own_line=False):
    """True if editing a line from `before` to `after` created a new === marker.

    A run of three or more '=' counts as one marker, so typing a fourth '='
    onto an existing === does not fire again.
    """
    return _count_markers(after, own_line) > _count_markers(before, own_line)


def read_style_guide(path):
    """Return the style guide's text, or "" when none is configured."""
    if not path.strip():
        return ""
    path = os.path.expanduser(path.strip())
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise LLMError("Could not read the style guide %s: %s" % (path, e.strerror))
    except UnicodeDecodeError:
        raise LLMError("The style guide %s is not UTF-8 text" % path)


def build_input(initial_prompt, style_guide, tabs):
    """Join the initial prompt, the style guide and the (name, text) of each tab.

    Returns "" when every tab is empty, meaning there is nothing to ask.
    """
    sections = ["--- %s ---\n%s" % (name, text.rstrip()) for name, text in tabs if text.strip()]
    if not sections:
        return ""
    head = []
    if initial_prompt.strip():
        head.append(initial_prompt.strip())
    if style_guide.strip():
        head.append("--- Style guide ---\n" + style_guide.strip())
    return "\n\n".join(head + sections)


def read_api_key(key_file):
    """Return (key, where_it_came_from); the key file wins over $OPENAI_API_KEY.

    A key file may hold several keys, one per line: the first line starting
    with "sk-" is used, otherwise the first non-empty line.
    """
    if key_file:
        path = os.path.expanduser(key_file)
        try:
            with open(path, encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        except OSError:
            lines = []
        if lines:
            return next((line for line in lines if line.startswith("sk-")), lines[0]), path
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key, "$OPENAI_API_KEY"
    return None, None


def save_output(folder, tab_name, text, now=None):
    """Write a closed tab's contents to folder as <tab-name>-<timestamp>.md."""
    folder = os.path.expanduser(folder)
    os.makedirs(folder, exist_ok=True)
    slug = re.sub(r"[^\w.-]+", "-", tab_name).strip("-") or "llm-output"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    path = os.path.join(folder, "%s-%s.md" % (slug, stamp))
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, "%s-%s-%d.md" % (slug, stamp, n))
        n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class Cancel:
    """Cancels a running stream_response, closing its connection at once."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._sock = None

    def is_set(self):
        return self._event.is_set()

    def set(self):
        with self._lock:
            self._event.set()
            sock = self._sock
        _shutdown(sock)

    def attach(self, sock):
        with self._lock:
            self._sock = sock
            cancelled = self._event.is_set()
        if cancelled:
            _shutdown(sock)


def _shutdown(sock):
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _redact(message):
    return MASKED_KEY_RE.sub("<key>", str(message))


def _sse_events(lines):
    """Yield each JSON payload of a server-sent event stream."""
    data = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            payload = "\n".join(data)
            data = []
            if payload == "[DONE]":
                return
            yield json.loads(payload)


def _error_message(response):
    try:
        return json.loads(response.read().decode("utf-8"))["error"]["message"]
    except Exception:
        return response.reason


def stream_response(settings, user_input, on_delta, cancel, timeout=900):
    """Stream a Responses API answer, calling on_delta(text) for each chunk.

    Runs on a worker thread. Returns once the reply is complete, or quietly as
    soon as `cancel` (a Cancel) is set; raises LLMError for anything the user
    should see.
    """
    key, key_source = read_api_key(settings["api_key_file"])
    if not key:
        raise LLMError("No API key: set an API key file in the plugin preferences "
                       "or the OPENAI_API_KEY environment variable.")
    # Checked here so http.client never raises an error that quotes the key.
    if any(c.isspace() or not c.isprintable() for c in key):
        raise LLMError("The API key from %s contains spaces or control characters." % key_source)

    endpoint = settings["base_url"].strip().rstrip("/") + "/responses"
    url = urllib.parse.urlsplit(endpoint)
    # The key goes only where it was configured to: no redirects are followed,
    # and plain http is accepted only for a server on this machine.
    if url.scheme == "https" and url.hostname:
        connection = http.client.HTTPSConnection(url.hostname, url.port, timeout=timeout)
    elif url.scheme == "http" and url.hostname in LOCAL_HOSTS:
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
    else:
        raise LLMError("The API base URL must use https (plain http only for localhost): %s"
                       % settings["base_url"])

    body = {
        "model": settings["model"],
        "input": user_input,
        "reasoning": {"effort": settings["reasoning_effort"]},
        "stream": True,
        "store": False,
    }
    if settings["system_prompt"].strip():
        body["instructions"] = settings["system_prompt"]

    try:
        connection.connect()
        cancel.attach(connection.sock)
        connection.request("POST", url.path, body=json.dumps(body).encode("utf-8"), headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        })
        response = connection.getresponse()
        if response.status != 200:
            raise LLMError("HTTP %d from %s (API key from %s): %s"
                           % (response.status, endpoint, key_source, _redact(_error_message(response))))

        for event in _sse_events(response):
            if cancel.is_set():
                return
            kind = event.get("type")
            if kind == "response.output_text.delta":
                on_delta(event.get("delta", ""))
            elif kind == "response.completed":
                return
            elif kind == "response.incomplete":
                details = event.get("response", {}).get("incomplete_details") or {}
                raise LLMError("Response stopped early: %s" % details.get("reason", "unknown reason"))
            elif kind == "response.failed":
                error = event.get("response", {}).get("error") or {}
                raise LLMError("Request failed: %s" % _redact(error.get("message", "unknown error")))
            elif kind == "error":
                raise LLMError("API error: %s" % _redact(event.get("message", event)))
    except LLMError:
        raise
    except ValueError:
        if cancel.is_set():
            return
        raise LLMError("Malformed response from %s" % endpoint)
    except Exception as e:  # OSError, ssl and http.client errors, or a cancelled read
        if cancel.is_set():
            return
        raise LLMError("Connection to %s failed: %s" % (endpoint, e))
    finally:
        connection.close()

    if not cancel.is_set():
        raise LLMError("The connection to %s closed before the reply was complete." % endpoint)
