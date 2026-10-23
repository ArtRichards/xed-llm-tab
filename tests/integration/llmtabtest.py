"""Test driver plugin: exercises the LLM Tab plugin inside a real xed window
in the display mode given by $LLMTAB_MODE, writes its observations to
results.json, then quits xed."""
import json
import os
import sys
import time
import traceback

from gi.repository import GLib, GObject, Gio, Gtk, Xed

TEST_DIR = os.environ["LLMTAB_TEST_DIR"]
INCOGNITO = os.environ["LLMTAB_MODE"] == "incognito"
LOG = os.path.join(TEST_DIR, "requests.jsonl")
SAVE_DIR = os.path.join(TEST_DIR, "saved")
ERROR_LOG = os.path.join(TEST_DIR, "cache", "xed-llm-tab", "errors.log")
STYLE_GUIDE = "Answer tersely."


def n_requests():
    try:
        with open(LOG) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def last_request():
    with open(LOG) as f:
        return json.loads(f.readlines()[-1])["body"]


def next_reply():
    return "Reply #%d done" % (n_requests() + 1)


def error_log():
    try:
        with open(ERROR_LOG) as f:
            return f.read()
    except OSError:
        return ""


def saved_files():
    return sorted(os.listdir(SAVE_DIR)) if os.path.isdir(SAVE_DIR) else []


def text(doc):
    return doc.get_text(doc.get_start_iter(), doc.get_end_iter(), True)


def names(docs):
    return [d.get_short_name_for_display() for d in docs]


class LLMTabTest(GObject.Object, Xed.WindowActivatable):
    __gtype_name__ = "LLMTabTest"

    window = GObject.Property(type=Xed.Window)

    def do_activate(self):
        # Only the first window drives the test; windows it opens get an instance too.
        if getattr(LLMTabTest, "started", False):
            return
        LLMTabTest.started = True
        self.checks = []
        self.waiting = None
        self.steps = self.scenario()
        GLib.timeout_add(100, self.tick)

    def do_deactivate(self):
        pass

    # Tiny step runner: the scenario yields (label, predicate, timeout).

    def tick(self):
        if self.waiting is not None:
            label, pred, deadline = self.waiting
            if not pred() and time.monotonic() < deadline:
                return True
            if label is not None:
                self.check("wait: " + label, pred())
            self.waiting = None
        try:
            label, pred, timeout = next(self.steps)
        except StopIteration:
            return self.finish()
        except Exception:
            self.checks.append({"name": "scenario crashed", "ok": False, "detail": traceback.format_exc()})
            return self.finish()
        self.waiting = (label, pred, time.monotonic() + timeout)
        return True

    def check(self, name, ok, detail=None):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def finish(self):
        with open(os.path.join(TEST_DIR, "results.json"), "w") as f:
            json.dump(self.checks, f, indent=2)
        Gio.Application.get_default().quit()
        return False

    # Helpers

    def plugin(self, window=None):
        module = sys.modules.get("llmtab")
        window = window or self.window
        return next((i for i in module._instances if i.window == window), None) if module else None

    def out_tab(self, window=None):
        plugin = self.plugin(window)
        return plugin._out_tab if plugin else None

    def out_text(self, window=None):
        tab = self.out_tab(window)
        return text(tab.get_document()) if tab else None

    def out_location(self):
        location = self.out_tab().get_document().get_file().get_location()
        return location.get_path() if location else None

    def menu_action(self, name="OpenLLMTabAction"):
        for group in self.window.get_ui_manager().get_action_groups():
            if group.get_action(name):
                return group.get_action(name)

    def toolbar_button(self, name):
        return self.window.get_ui_manager().get_widget("/ToolBar/" + name)

    def click(self, tool_button):
        # Clicking a toolbar item clicks the button inside it.
        tool_button.get_child().clicked()

    def doc_named(self, name, window=None):
        for doc in (window or self.window).get_documents():
            if doc.get_short_name_for_display() == name:
                return doc
        return None

    def load(self, path):
        name = os.path.basename(path)
        n_docs = len(self.window.get_documents())
        Xed.commands_load_location(self.window, Gio.File.new_for_path(path), None, 0)
        return ("%s loaded" % name, lambda: len(self.window.get_documents()) == n_docs + 1
                and all(Xed.Tab.get_from_document(d).get_state() == Xed.TabState.STATE_NORMAL
                        for d in self.window.get_documents()), 10)

    def type_into(self, doc, chars):
        """Type like a user: one user action per character, at the end."""
        doc.place_cursor(doc.get_end_iter())
        for ch in chars:
            doc.begin_user_action()
            doc.insert_at_cursor(ch, -1)
            doc.end_user_action()

    def set_setting(self, **changes):
        settings = dict(self.plugin()._settings, **changes)
        for instance in sys.modules["llmtab"]._instances:
            instance.apply_settings(settings)

    def sleep(self, seconds):
        return (None, lambda: False, seconds)

    def scenario(self):
        yield ("startup: notes.txt and output tab exist",
               lambda: self.doc_named("notes.txt") is not None and self.out_tab() is not None, 10)
        notes = self.doc_named("notes.txt")

        # A. The tab opens without stealing focus and queries the open tabs.
        if INCOGNITO:
            self.check("A: incognito tab opens empty, no waiting line", self.out_text() == "", self.out_text())
        yield ("first query on open", lambda: self.out_text() == "Reply #1 done", 10)
        body = last_request()
        self.check("A: one request on open", n_requests() == 1, n_requests())
        self.check("A: style guide follows the initial prompt, then the tabs",
                   body["input"] == "Here are my open editor tabs:\n\n--- Style guide ---\n%s\n\n"
                                    "--- notes.txt ---\nWhat is 2+2?" % STYLE_GUIDE, body["input"])
        self.check("A: model/effort/system prompt sent",
                   body["model"] == "gpt-6.1-sol" and body["reasoning"] == {"effort": "medium"}
                   and body["instructions"].startswith("You are an assistant"), body)
        tab = self.out_tab()
        self.check("A: label is 'LLM Output' without *", tab.props.name == "LLM Output", tab.props.name)
        self.check("A: output doc not modified", not tab.get_document().get_modified())
        self.check("A: focus stayed on notes.txt",
                   self.window.get_active_document().get_short_name_for_display() == "notes.txt")
        self.check("A: nothing reported unsaved", self.window.get_unsaved_documents() == [],
                   names(self.window.get_unsaved_documents()))
        self.check("A: no file written for the tab", not os.path.exists(self.out_location()), self.out_location())
        self.check("A: menu item named after the tab", self.menu_action().get_label() == "Open LLM Output",
                   self.menu_action().get_label())
        language = tab.get_document().get_language()
        if INCOGNITO:
            self.check("A: incognito tab is editable", tab.get_view().get_editable())
            self.check("A: incognito tab is plain text", language is None, language and language.get_id())
            self.check("A: incognito tab looks like a file in the save folder",
                       self.out_location() == os.path.join(SAVE_DIR, "LLM Output"), self.out_location())
        else:
            self.check("A: streaming tab is read-only", not tab.get_view().get_editable())
            self.check("A: streaming tab uses Markdown", language is not None and language.get_id() == "markdown")

        # B. Typing === in another tab fires exactly one query.
        self.type_into(notes, "\n===")
        yield ("=== query", lambda: self.out_text() == "Reply #2 done", 10)
        self.check("B: exactly two requests", n_requests() == 2, n_requests())
        self.check("B: request includes the marker", "What is 2+2?\n===" in last_request()["input"])
        self.check("B: label still has no *", self.out_tab().props.name == "LLM Output", self.out_tab().props.name)

        # C. Extending === to ==== does not fire again.
        self.type_into(notes, "=")
        yield self.sleep(1.0)
        self.check("C: ==== did not fire", n_requests() == 2, n_requests())

        # D. Opening files while the output tab is active must not replace it.
        self.window.set_active_tab(self.out_tab())
        n_docs = len(self.window.get_documents())
        yield self.load(os.path.join(TEST_DIR, "other.txt"))
        yield self.load(os.path.join(TEST_DIR, "style.md"))
        self.check("D: files opened in new tabs", len(self.window.get_documents()) == n_docs + 2,
                   names(self.window.get_documents()))
        self.check("D: output tab kept its reply", self.out_text() == "Reply #2 done", self.out_text())
        self.check("D: loading a file with === did not fire", n_requests() == 2, n_requests())
        other = self.doc_named("other.txt")

        # E. Renaming from the settings panel updates the label and menu immediately.
        if INCOGNITO:
            # A real file already has the new name: the tab must not take it over.
            os.makedirs(SAVE_DIR, exist_ok=True)
            with open(os.path.join(SAVE_DIR, "Renamed"), "w") as f:
                f.write("a real file")
        grid = sys.modules["llmtab"].SettingsGrid(sys.modules["llmtab"].core.load_settings())
        entry = [w for w in grid.get_children() if isinstance(w, Gtk.Entry) and w.get_text() == "LLM Output"][0]
        entry.set_text("Renamed")
        self.check("E: label renamed", self.out_tab().props.name == "Renamed", self.out_tab().props.name)
        self.check("E: menu renamed", self.menu_action().get_label() == "Open Renamed", self.menu_action().get_label())
        if INCOGNITO:
            self.check("E: an existing file of that name is not taken over",
                       self.out_location() == os.path.join(SAVE_DIR, "Renamed (2)"), self.out_location())
            self.check("E: renaming writes nothing", saved_files() == ["Renamed"], saved_files())
            yield self.load(os.path.join(SAVE_DIR, "Renamed"))
            real = [d for d in self.window.get_documents() if text(d) == "a real file"]
            self.check("E: opening the real file gives it its own tab", len(real) == 1 and real[0] !=
                       self.out_tab().get_document(), names(self.window.get_documents()))
            if real:
                real[0].set_text("")  # keep later requests small

        # F. A new === cancels a reply that is still in progress.
        other.set_text("")
        count = n_requests()
        self.type_into(other, "SLOW ===")
        if INCOGNITO:
            yield ("slow request sent", lambda: n_requests() == count + 1, 10)
            yield self.sleep(1.0)
            self.check("F: incognito keeps the previous reply while waiting (no waiting line, no partial text)",
                       self.out_text() == "Reply #2 done", self.out_text())
        else:
            yield ("slow reply streaming", lambda: (self.out_text() or "").startswith("slow-1"), 10)
        other.set_text("")
        reply = "Reply #%d done" % (count + 2)
        self.type_into(other, "===")
        yield ("replacement reply", lambda: self.out_text() == reply, 10)
        yield self.sleep(2.0)
        self.check("F: cancelled reply never appeared", self.out_text() == reply, self.out_text())

        # G. Incognito: typing in the tab behaves like a normal file; the next reply replaces it.
        if INCOGNITO:
            out_doc = self.out_tab().get_document()
            self.type_into(out_doc, " my edit")
            self.check("G: edits show the normal *", self.out_tab().props.name == "*Renamed", self.out_tab().props.name)
            reply = next_reply()
            self.type_into(notes, "\n===")
            yield ("reply replaces the edit", lambda: self.out_text() == reply, 10)
            self.check("G: * cleared by the reply", self.out_tab().props.name == "Renamed", self.out_tab().props.name)
        last = self.out_text()

        # H. Closing the output tab saves it.
        before = saved_files()
        self.window.close_tab(self.out_tab())
        yield ("output tab closed", lambda: self.out_tab() is None, 5)
        new = [name for name in saved_files() if name not in before]
        self.check("H: one file saved", len(new) == 1 and new[0].startswith("Renamed-"), saved_files())
        if new:
            with open(os.path.join(SAVE_DIR, new[0])) as f:
                self.check("H: saved contents", f.read() == last)
        count = n_requests()

        # I. With the tab closed, === does nothing.
        self.type_into(notes, "\n===")
        yield self.sleep(1.0)
        self.check("I: closed tab means no query", n_requests() == count, n_requests())

        # J. Tools > Open <name> reopens it, focused, and queries the open tabs.
        self.check("J: menu item present",
                   self.window.get_ui_manager().get_widget("/MenuBar/ToolsMenu/ToolsOps_2/OpenLLMTabAction") is not None)
        reply = next_reply()
        self.menu_action().activate()
        yield ("reopened tab queried", lambda: self.out_text() == reply, 10)
        self.check("J: reopened tab is active", self.window.get_active_tab() == self.out_tab())
        self.check("J: reopened tab keeps the renamed label", self.out_tab().props.name == "Renamed")
        body = last_request()["input"]
        self.check("J: request has notes.txt and other.txt",
                   "--- notes.txt ---" in body and "--- other.txt ---" in body, body)
        self.check("J: style guide sent once even though it is open in a tab",
                   body.count(STYLE_GUIDE) == 1 and "--- style.md ---" not in body, body)

        # P. The toolbar's Send button sends like ===, including text nobody typed
        # (a live transcript from another plugin, say); its gear opens the preferences.
        send, gear = self.toolbar_button("SendLLMTabAction"), self.toolbar_button("LLMTabPreferencesAction")
        self.check("P: Send and gear buttons on the toolbar", send is not None and gear is not None)
        manager = self.window.get_ui_manager()
        self.check("P: both in the Tools menu too",
                   manager.get_widget("/MenuBar/ToolsMenu/ToolsOps_2/SendLLMTabAction") is not None
                   and manager.get_widget("/MenuBar/ToolsMenu/ToolsOps_2/LLMTabPreferencesAction") is not None)
        self.check("P: named after the tab",
                   self.menu_action("SendLLMTabAction").get_label() == "Send to Renamed"
                   and self.menu_action("LLMTabPreferencesAction").get_label() == "Renamed Preferences"
                   and send.get_child().get_tooltip_text() == "Send the open tabs to Renamed, as typing === does"
                   and gear.get_child().get_tooltip_text() == "Renamed preferences",
                   (send.get_child().get_tooltip_text(), gear.get_child().get_tooltip_text()))
        self.check("P: Send shows its label, the gear only an icon",
                   send.get_is_important() and send.get_label() == "Send" and not gear.get_is_important(),
                   (send.get_label(), gear.get_is_important()))
        other.set_text("")
        count = n_requests()
        other.insert(other.get_end_iter(), "Me: written by another plugin")
        yield self.sleep(0.5)
        self.check("P: text that was not typed sends nothing by itself", n_requests() == count, n_requests())
        reply = next_reply()
        self.click(send)
        yield ("Send button queried", lambda: self.out_text() == reply, 10)
        self.check("P: the request includes that text",
                   "--- other.txt ---\nMe: written by another plugin" in last_request()["input"],
                   last_request()["input"])
        self.check("P: focus stayed where it was", self.window.get_active_tab() == self.out_tab())
        self.click(gear)
        dialog = self.plugin()._preferences
        self.check("P: the gear opens the preferences", dialog is not None and dialog.get_visible()
                   and dialog.get_title() == "Renamed Preferences", dialog and dialog.get_title())
        self.check("P: with the settings in it", dialog is not None and any(
            isinstance(w, sys.modules["llmtab"].SettingsGrid) for w in dialog.get_content_area().get_children()))
        self.click(gear)
        self.check("P: a second click brings back the same dialog", self.plugin()._preferences is dialog)
        dialog.response(Gtk.ResponseType.CLOSE)
        self.check("P: Close closes it", self.plugin()._preferences is None)
        other.set_text("")
        good = self.out_text()

        # M. Streaming: switching to incognito mid-reply cancels it and restores the last reply.
        if not INCOGNITO:
            other.set_text("")
            self.type_into(other, "SLOW ===")
            yield ("slow reply streaming again", lambda: (self.out_text() or "").startswith("slow-1"), 10)
            self.set_setting(display_mode="incognito")
            self.check("M: switching to incognito restores the last reply", self.out_text() == good, self.out_text())
            self.check("M: and makes the tab editable", self.out_tab().get_view().get_editable())
            yield self.sleep(1.5)
            self.check("M: the cancelled stream stays out", self.out_text() == good, self.out_text())
            self.set_setting(display_mode="streaming")
            self.check("M: back to streaming makes it read-only", not self.out_tab().get_view().get_editable())
            other.set_text("")

        # K. API errors: shown in a streaming tab, only logged in an incognito tab.
        other.set_text("")
        self.type_into(other, "FORCE_ERROR ===")
        yield ("error logged", lambda: "HTTP 401" in error_log(), 10)
        if INCOGNITO:
            yield self.sleep(0.5)
            self.check("K: incognito tab still shows the last reply", self.out_text() == good, self.out_text())
        else:
            self.check("K: streaming tab shows the error",
                       (self.out_text() or "").startswith("[LLM Tab] HTTP 401")
                       and "Incorrect API key provided" in self.out_text(), self.out_text())
        self.check("K: masked key fragment not logged", "sk-" not in error_log(), error_log())
        self.check("K: label still has no *", self.out_tab().props.name == "Renamed", self.out_tab().props.name)

        # L. A missing style guide is an error, handled the same way.
        style = self.plugin()._settings["style_guide_file"]
        self.set_setting(style_guide_file=os.path.join(TEST_DIR, "missing.md"))
        other.set_text("")
        count = n_requests()
        self.type_into(other, "===")
        yield ("missing style guide logged", lambda: "Could not read the style guide" in error_log(), 10)
        self.check("L: no request sent without the style guide", n_requests() == count, n_requests())
        if INCOGNITO:
            self.check("L: incognito tab still shows the last reply", self.out_text() == good, self.out_text())
        else:
            self.check("L: streaming tab shows the error",
                       (self.out_text() or "").startswith("[LLM Tab] Could not read the style guide"),
                       self.out_text())
        self.set_setting(style_guide_file=style)
        other.set_text("")

        # N. A second window: closing its output tab must not disturb this window's tab.
        w2 = Xed.App.get_default().create_window(None)
        yield ("second window gets its own output tab", lambda: self.out_tab(w2) is not None, 10)
        w2.create_tab(True)
        self.check("N: both output tabs share a name", self.out_tab(w2).props.name == "Renamed")
        w2.close_tab(self.out_tab(w2))
        yield ("second window's output tab closed", lambda: self.out_tab(w2) is None, 5)
        self.check("N: this window's output tab is still not unsaved",
                   self.out_tab().get_document() not in self.window.get_unsaved_documents(),
                   names(self.window.get_unsaved_documents()))

        # O. Moving the output tab to that window: it is adopted there, not duplicated.
        before = saved_files()
        moved = self.out_tab()
        moved.get_parent().move_tab(w2.get_active_tab().get_parent(), moved, -1)
        yield self.sleep(0.5)
        self.check("O: the other window adopted the tab", self.out_tab(w2) == moved)
        self.check("O: this window has no output tab now", self.out_tab() is None)
        self.check("O: no second output tab was created in the other window",
                   names(w2.get_documents()).count("Renamed") == 1, names(w2.get_documents()))
        saved_on_move = [name for name in saved_files() if name not in before]
        self.check("O: the reply was saved once at most", len(saved_on_move) <= 1, saved_on_move)
        blank = [d for d in w2.get_documents() if d != moved.get_document()][0]
        reply = next_reply()
        self.type_into(blank, "Hello from window two\n===")
        # Wait for the request to finish, not just for its text: in streaming mode
        # the last words show up before the response is marked complete.
        yield ("moved tab answers queries from its new window",
               lambda: self.out_text(w2) == reply and self.plugin(w2)._request is None, 10)
        self.check("O: request came from the new window's tabs",
                   "Hello from window two" in last_request()["input"]
                   and "--- notes.txt ---" not in last_request()["input"], last_request()["input"])
        before = saved_files()
        w2.close_tab(moved)
        yield ("moved tab closed", lambda: self.out_tab(w2) is None, 5)
        new = [name for name in saved_files() if name not in before]
        self.check("O: closing it in its new window saves the new reply", len(new) == 1, new)
        self.check("O: nothing on disk for the output tabs",
                   not os.path.exists(os.path.join(SAVE_DIR, "Renamed (2)")), saved_files())

        # Q. With the output tab closed, Send reopens it in the background and sends.
        self.check("Q: this window has no output tab", self.out_tab() is None)
        active = self.window.get_active_tab()
        reply = next_reply()
        self.click(self.toolbar_button("SendLLMTabAction"))
        yield ("Send reopened the tab and queried", lambda: self.out_text() == reply, 10)
        self.check("Q: focus stayed where it was", self.window.get_active_tab() == active)
