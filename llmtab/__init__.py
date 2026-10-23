# -*- coding: utf-8 -*-
#
#  __init__.py - LLM Tab plugin
#
#  Adds an output tab to every xed window. Typing === in any other tab sends
#  the system prompt, the initial prompt, an optional style guide and the text
#  of all other tabs to an OpenAI model, and the reply appears in the output tab.
#
#  Two display modes:
#    incognito - the tab looks and behaves like a normal file tab. The previous
#                reply stays until the next one is complete, and nothing but
#                replies ever appears in it (errors go to the error log).
#    streaming - replies stream in as they are generated, after a "waiting"
#                line, and errors are shown in the tab.
#
#  Closing the tab saves its last reply; Tools > Open <tab name> brings it back.
#  The toolbar's Send button sends without typing ===, and its gear button
#  opens the preferences.

import os
import threading

import gi
gi.require_version("PeasGtk", "1.0")
gi.require_version("GtkSource", "4")
from gi.repository import GLib, GObject, Gio, Gtk, GtkSource, PeasGtk, Xed

from . import core

MENU_PATH = "/MenuBar/ToolsMenu/ToolsOps_2"
TOOLBAR_PATH = "/ToolBar"

# Every active window's plugin instance, so settings changes apply at once.
_instances = set()

# Output tabs that just left a window, with that window's reply state. Moving a
# tab to another window removes it and adds it again straight away, and the
# receiving window adopts it from here. Cleared once the main loop is idle.
_released_tabs = {}


def _forget_released(tab):
    _released_tabs.pop(tab, None)
    return False


def _toolbar_box(window):
    """The box of button groups in xed 3's own toolbar, or None in older versions.

    xed 3 draws its toolbar itself instead of showing the UI manager's.
    """
    def find(widget):
        if isinstance(widget, Gtk.Toolbar) and widget.get_style_context().has_class("primary-toolbar"):
            return widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                found = find(child)
                if found is not None:
                    return found
        return None

    toolbar = find(window)
    if toolbar is None or toolbar.get_n_items() != 1:
        return None
    box = toolbar.get_nth_item(0).get_child()
    return box if isinstance(box, Gtk.Box) else None


def _doc_path(doc):
    location = doc.get_file().get_location()
    return location.get_path() if location is not None else None


class LLMTabWindowActivatable(GObject.Object, Xed.WindowActivatable):
    __gtype_name__ = "LLMTabWindowActivatable"

    window = GObject.Property(type=Xed.Window)

    def __init__(self):
        GObject.Object.__init__(self)

    def do_activate(self):
        self._settings = core.load_settings()
        self._out_tab = None
        self._state_handler = None
        self._creating_tab = False
        self._watched = {}        # document -> signal handler ids
        self._request = None      # core.Cancel of the running query
        self._shown = "empty"     # what the output tab shows: empty, waiting, partial, reply or error
        self._last_reply = None   # the latest complete reply
        self._saved_text = None   # what was last saved to the save folder
        self._backing_path = os.path.join(core.cache_dir(), "output-%d-%d.md" % (os.getpid(), id(self)))
        self._preferences = None  # the open preferences dialog

        for doc in self.window.get_documents():
            self._watch(doc)
        self._window_handlers = [
            self.window.connect("tab-added", self.on_tab_added),
            self.window.connect("tab-removed", self.on_tab_removed),
        ]
        self._insert_menu()
        _instances.add(self)

        # Let xed finish setting up the window and its startup tabs first.
        GLib.idle_add(self.on_startup_idle)

    def do_deactivate(self):
        _instances.discard(self)
        for handler in self._window_handlers:
            self.window.disconnect(handler)
        for doc in list(self._watched):
            self._unwatch(doc)
        self._remove_menu()
        for widget in self._toolbar_widgets:
            widget.destroy()
        if self._preferences is not None:
            self._preferences.destroy()

        tab = self._out_tab
        if tab is not None:
            self._release_output_tab()
            # Unsaved edits in the tab stay open as an ordinary document.
            if tab.get_document().get_modified():
                tab.get_view().set_editable(True)
            else:
                self.window.close_tab(tab)

    def apply_settings(self, settings):
        mode_changed = settings["display_mode"] != self._settings["display_mode"]
        self._settings = dict(settings)
        self._update_labels()
        if self._out_tab is None:
            return
        if mode_changed:
            # A reply in progress belongs to the old mode's display.
            self._cancel_request()
            if self._incognito() and self._shown != "reply":
                self._write(self._last_reply or "", replace=True)
                self._shown = "reply" if self._last_reply else "empty"
        self._apply_tab_look(mode_changed)

    def _incognito(self):
        return self._settings["display_mode"] == "incognito"

    def _tab_name(self):
        return self._settings["tab_name"].strip() or core.DEFAULTS["tab_name"]

    # Menu and toolbar

    def _insert_menu(self):
        manager = self.window.get_ui_manager()

        self._action_group = Gtk.ActionGroup(name="XedLLMTabPluginActions")
        self._action_group.add_actions([
            ("OpenLLMTabAction", None, "", None, None, self.on_open_llm_tab_activate),
            ("SendLLMTabAction", None, "", None, None, self.on_send_activate),
            ("LLMTabPreferencesAction", None, "", None, None, self.on_preferences_activate),
        ])
        send = self._action_group.get_action("SendLLMTabAction")
        send.set_icon_name("mail-send-symbolic")
        send.set_short_label("Send")
        send.set_is_important(True)  # shows "Send" beside the icon on the toolbar
        self._action_group.get_action("LLMTabPreferencesAction").set_icon_name("preferences-system-symbolic")
        manager.insert_action_group(self._action_group)

        self._ui_id = manager.new_merge_id()
        for name in ("OpenLLMTabAction", "SendLLMTabAction", "LLMTabPreferencesAction"):
            manager.add_ui(self._ui_id, MENU_PATH, name, name, Gtk.UIManagerItemType.MENUITEM, False)

        self._toolbar_buttons = {}
        self._toolbar_widgets = []
        box = _toolbar_box(self.window)
        if box is not None:
            self._add_toolbar_buttons(box)
        else:
            manager.add_ui(self._ui_id, TOOLBAR_PATH, "LLMTabToolSeparator", None,
                           Gtk.UIManagerItemType.SEPARATOR, False)
            for name in ("SendLLMTabAction", "LLMTabPreferencesAction"):
                manager.add_ui(self._ui_id, TOOLBAR_PATH, name, name, Gtk.UIManagerItemType.TOOLITEM, False)
        self._update_labels()

    def _add_toolbar_buttons(self, box):
        """A group at the end of xed 3's toolbar, made like xed's own buttons."""
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        group = Gtk.Box(spacing=0)
        for name in ("SendLLMTabAction", "LLMTabPreferencesAction"):
            action = self._action_group.get_action(name)
            button = Gtk.Button(relief=Gtk.ReliefStyle.NONE)
            button.set_use_action_appearance(False)
            button.set_related_action(action)  # clicking activates the action
            button.set_image(Gtk.Image.new_from_icon_name(action.get_icon_name(), Gtk.IconSize.MENU))
            if action.get_is_important():
                button.set_label(action.get_short_label())
                button.set_always_show_image(True)
            group.pack_start(button, False, False, 0)
            self._toolbar_buttons[name] = button
        box.pack_start(separator, False, False, 0)
        box.pack_start(group, False, False, 0)
        separator.show()
        group.show_all()
        self._toolbar_widgets = [separator, group]

    def _remove_menu(self):
        manager = self.window.get_ui_manager()
        manager.remove_ui(self._ui_id)
        manager.remove_action_group(self._action_group)
        manager.ensure_update()

    def _update_labels(self):
        # Named after the tab, so the menu and toolbar do not give the tab away.
        name = self._tab_name()
        menu_name = name.replace("_", "__")
        get = self._action_group.get_action
        get("OpenLLMTabAction").set_label("Open %s" % menu_name)
        get("SendLLMTabAction").set_label("Send to %s" % menu_name)
        get("SendLLMTabAction").set_tooltip("Send the open tabs to %s, as typing === does" % name)
        get("LLMTabPreferencesAction").set_label("%s Preferences" % menu_name)
        get("LLMTabPreferencesAction").set_tooltip("%s preferences" % name)
        for action_name, button in self._toolbar_buttons.items():
            button.set_tooltip_text(get(action_name).get_tooltip())
        if self._preferences is not None:
            self._preferences.set_title("%s Preferences" % name)

    def on_open_llm_tab_activate(self, action, user_data=None):
        if self._out_tab is not None:
            self.window.set_active_tab(self._out_tab)
        else:
            self._open_output_tab(jump_to=True, query=self._settings["query_on_open"])

    def on_send_activate(self, action, user_data=None):
        # Like typing ===, except that a closed output tab is reopened, in the background.
        if self._out_tab is not None:
            self._run_query()
        else:
            self._open_output_tab(jump_to=False, query=True)

    def on_preferences_activate(self, action, user_data=None):
        if self._preferences is not None:
            self._preferences.present()
            return
        dialog = Gtk.Dialog(title="%s Preferences" % self._tab_name(), transient_for=self.window,
                            destroy_with_parent=True)
        dialog.add_button("_Close", Gtk.ResponseType.CLOSE)
        dialog.get_content_area().add(SettingsGrid(core.load_settings()))
        dialog.connect("response", lambda dialog, response: dialog.destroy())
        dialog.connect("destroy", self.on_preferences_destroyed)
        dialog.show_all()
        self._preferences = dialog

    def on_preferences_destroyed(self, dialog):
        if self._preferences is dialog:
            self._preferences = None

    # Output tab

    def on_startup_idle(self):
        if self in _instances and self._out_tab is None:
            self._open_output_tab(jump_to=False, query=self._settings["query_on_open"])
        return False

    def _open_output_tab(self, jump_to, query):
        # create=True opens the tab the way `xed new-file.txt` does: a named
        # document whose file does not exist yet. xed neither counts it as
        # unsaved nor loads the next opened file into it, and nothing is
        # written to disk unless the user saves.
        location = Gio.File.new_for_path(self._tab_path(None))
        self._creating_tab = True
        try:
            tab = self.window.create_tab_from_location(location, None, 0, True, jump_to)
        finally:
            self._creating_tab = False
        self._adopt(tab, None)

        if query:
            self._query_when_loaded(tab)
        return tab

    def _adopt(self, tab, state):
        """Make tab this window's output tab; state carries over a moved tab's replies."""
        self._out_tab = tab
        self._state_handler = tab.connect("notify::state", self.on_out_tab_state_changed)
        if state is not None:
            self._shown, self._last_reply, self._saved_text = state
        else:
            self._shown, self._last_reply, self._saved_text = "empty", None, None
        self._apply_tab_look()

    def _tab_path(self, current):
        """Where the output tab's (never written) file lives in the current mode."""
        if not self._incognito():
            return self._backing_path
        # Incognito: a file named after the tab in the save folder, so the window
        # title and tooltip read like any other file. Never a file that exists:
        # xed would switch to this tab whenever that file is opened, and Ctrl+S
        # here would overwrite it.
        folder = core.save_dir(self._settings)
        stem, ext = os.path.splitext(self._tab_name().replace(os.sep, "-"))
        path = os.path.join(folder, stem + ext)
        n = 2
        while path != current and os.path.lexists(path):
            path = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
            n += 1
        return path

    def _apply_tab_look(self, mode_changed=False):
        """Set the output tab's location, label, editability and (on a mode change) highlighting."""
        tab = self._out_tab
        doc = tab.get_document()
        current = _doc_path(doc)
        path = self._tab_path(current)
        if path != current:
            doc.set_location(Gio.File.new_for_path(path))
        doc.set_short_name_for_display(self._tab_name())

        if mode_changed:
            manager = GtkSource.LanguageManager.get_default()
            if self._incognito():
                language = manager.guess_language(os.path.basename(path), None)
            else:
                language = manager.get_language("markdown")
            # The GtkSource call, unlike xed's, does not store the language as file metadata.
            GtkSource.Buffer.set_language(doc, language)
        tab.get_view().set_editable(self._incognito())

    def on_out_tab_state_changed(self, tab, pspec):
        # xed makes the view editable again whenever the tab state changes.
        if not self._incognito():
            tab.get_view().set_editable(False)

    def _release_output_tab(self):
        """Forget the output tab as it leaves this window, saving its last reply."""
        tab, self._out_tab = self._out_tab, None
        tab.disconnect(self._state_handler)
        self._cancel_request()

        # Save what the tab shows when that is a finished reply (perhaps edited),
        # otherwise the last finished reply; never a waiting line or an error.
        doc = tab.get_document()
        text = doc.get_text(doc.get_start_iter(), doc.get_end_iter(), True) if self._shown == "reply" \
            else self._last_reply
        if text and text != self._saved_text:
            try:
                core.save_output(core.save_dir(self._settings), self._tab_name(), text)
                self._saved_text = text
            except OSError as e:
                core.log_error("Could not save the output tab: %s" % e)

        _released_tabs[tab] = (self._shown, self._last_reply, self._saved_text)
        GLib.idle_add(_forget_released, tab)

    def _write(self, text, replace=False):
        doc = self._out_tab.get_document()
        doc.begin_not_undoable_action()
        if replace:
            doc.set_text(text)
            doc.place_cursor(doc.get_start_iter())
        else:
            doc.insert(doc.get_end_iter(), text)
        doc.end_not_undoable_action()
        # Output is not an edit: no '*' on the tab and no "save changes?" on close.
        doc.set_modified(False)

    # Tabs being watched for ===

    def on_tab_added(self, window, tab):
        if self._creating_tab or tab == self._out_tab:
            return
        moved = _released_tabs.pop(tab, None)
        if moved is not None:
            if self._out_tab is None:
                self._adopt(tab, moved)
                return
            # This window has its own output tab, so the moved one becomes an
            # ordinary document; its reply was saved when it left.
            tab.get_view().set_editable(True)
        self._watch(tab.get_document())

    def on_tab_removed(self, window, tab):
        if tab == self._out_tab:
            self._release_output_tab()
        else:
            self._unwatch(tab.get_document())

    def _watch(self, doc):
        if doc in self._watched:
            return
        state = {"in_action": False, "hit": False}
        self._watched[doc] = [
            doc.connect("begin-user-action", self.on_begin_user_action, state),
            doc.connect("end-user-action", self.on_end_user_action, state),
            doc.connect_after("insert-text", self.on_insert_text, state),
        ]

    def _unwatch(self, doc):
        for handler in self._watched.pop(doc, []):
            doc.disconnect(handler)

    def on_begin_user_action(self, doc, state):
        state["in_action"] = True

    def on_insert_text(self, doc, location, text, length, state):
        # Only typing and pasting count. File loads also run inside a user
        # action, so skip anything inserted while the tab is not in its normal state.
        if not state["in_action"] or "=" not in text:
            return
        if Xed.Tab.get_from_document(doc).get_state() != Xed.TabState.STATE_NORMAL:
            return
        # After the default handler, location points just past the new text.
        start = location.copy()
        start.backward_chars(len(text))
        line_start = start.copy()
        line_start.set_line_offset(0)
        line_end = location.copy()
        if not line_end.ends_line():
            line_end.forward_to_line_end()

        after = doc.get_text(line_start, line_end, True)
        before = doc.get_text(line_start, start, True) + doc.get_text(location, line_end, True)
        if core.marker_added(before, after, self._settings["marker_on_own_line"]):
            state["hit"] = True

    def on_end_user_action(self, doc, state):
        state["in_action"] = False
        if state["hit"]:
            state["hit"] = False
            GLib.idle_add(self.on_query_idle)

    # Queries

    def _query_when_loaded(self, tab, attempts=40):
        """Query once no tab is still loading its file (gives up waiting after ~10s)."""
        def check():
            nonlocal attempts
            if self._out_tab is not tab:
                return False
            attempts -= 1
            loading = any(Xed.Tab.get_from_document(doc).get_state() == Xed.TabState.STATE_LOADING
                          for doc in self.window.get_documents())
            if loading and attempts > 0:
                return True
            self._run_query()
            return False

        GLib.timeout_add(250, check)

    def on_query_idle(self):
        self._run_query()
        return False

    def _run_query(self):
        # A closed output tab means the plugin is idle until it is reopened.
        if self._out_tab is None:
            return
        out_doc = self._out_tab.get_document()
        # The style guide is sent once, after the initial prompt, even if it is open in a tab.
        style_guide = self._settings["style_guide_file"].strip()
        style_path = os.path.realpath(os.path.expanduser(style_guide)) if style_guide else None
        tabs = [(doc.get_short_name_for_display(),
                 doc.get_text(doc.get_start_iter(), doc.get_end_iter(), True))
                for doc in self.window.get_documents()
                if doc != out_doc and (style_path is None or _doc_path(doc) is None
                                       or os.path.realpath(_doc_path(doc)) != style_path)]
        if not any(text.strip() for name, text in tabs):
            return

        self._cancel_request()
        cancel = core.Cancel()
        self._request = cancel
        settings = dict(self._settings)
        if settings["display_mode"] == "streaming":
            self._write("Asking %s (reasoning: %s)..." % (settings["model"], settings["reasoning_effort"]),
                        replace=True)
            self._shown = "waiting"
        threading.Thread(target=self._query_worker, args=(settings, tabs, cancel), daemon=True).start()

    def _cancel_request(self):
        if self._request is not None:
            self._request.set()
            self._request = None

    def _query_worker(self, settings, tabs, cancel):
        streaming = settings["display_mode"] == "streaming"
        chunks = []

        def on_delta(text):
            chunks.append(text)
            if streaming:
                GLib.idle_add(self.on_delta, cancel, text)

        try:
            style_guide = core.read_style_guide(settings["style_guide_file"])
            user_input = core.build_input(settings["initial_prompt"], style_guide, tabs)
            core.stream_response(settings, user_input, on_delta, cancel)
        except core.LLMError as e:
            GLib.idle_add(self.on_query_error, cancel, settings, str(e))
        except Exception as e:
            GLib.idle_add(self.on_query_error, cancel, settings, "%s: %s" % (type(e).__name__, e))
        else:
            GLib.idle_add(self.on_query_done, cancel, settings, "".join(chunks))

    def on_delta(self, cancel, text):
        if cancel is self._request and not cancel.is_set():
            if self._shown == "waiting":
                self._write(text, replace=True)
                self._shown = "partial"
            else:
                self._write(text)
        return False

    def on_query_error(self, cancel, settings, message):
        if cancel is not self._request:
            return False
        self._request = None
        core.log_error(message)
        if settings["display_mode"] == "incognito":
            return False  # only replies ever appear in an incognito tab
        if self._shown == "partial":
            self._write("\n\n[LLM Tab] %s" % message)
        else:
            self._write("[LLM Tab] %s" % message, replace=True)
        self._shown = "error"
        return False

    def on_query_done(self, cancel, settings, text):
        if cancel is not self._request:
            return False
        self._request = None
        if not text.strip():
            core.log_error("The model returned no text.")
            if settings["display_mode"] == "streaming":
                self._write("[LLM Tab] The model returned no text.", replace=True)
                self._shown = "error"
            return False
        self._last_reply = text
        if settings["display_mode"] == "incognito":
            self._write(text, replace=True)
        self._shown = "reply"
        return False


class LLMTabConfigurable(GObject.Object, PeasGtk.Configurable):
    __gtype_name__ = "LLMTabConfigurable"

    def do_create_configure_widget(self):
        return SettingsGrid(core.load_settings())


class SettingsGrid(Gtk.Grid):
    """Plugin preferences; every change is saved and applied immediately."""

    def __init__(self, settings):
        Gtk.Grid.__init__(self, row_spacing=6, column_spacing=12, border_width=12)
        self._settings = settings
        self._row = 0

        self._add_combo("Display", "display_mode", [
            ("incognito", "Incognito: a normal-looking tab that shows only finished replies"),
            ("streaming", "Streaming: replies stream in, with progress and errors shown"),
        ])
        self._add_entry("Tab name", "tab_name")
        self._add_entry("Model", "model")
        self._add_combo("Reasoning level", "reasoning_effort",
                        [(level, level) for level in core.EFFORT_LEVELS])
        self._add_entry("API key file", "api_key_file",
                        "Optional. Uses $OPENAI_API_KEY when empty or missing. "
                        "If the file holds several keys, the line starting with sk- is used")
        self._add_entry("API base URL", "base_url")
        self._add_entry("Save closed tabs to", "save_dir",
                        "The output tab's last reply is saved here when the tab closes")
        self._add_entry("Style guide file", "style_guide_file",
                        "Optional. Its contents are sent after the initial prompt "
                        "to describe how replies should look")
        self._add_check("Query the open tabs when the output tab opens", "query_on_open")
        self._add_check("Only trigger on a line containing just ===", "marker_on_own_line")
        self._add_text("System prompt", "system_prompt")
        self._add_text("Initial prompt", "initial_prompt")
        self.set_size_request(600, -1)
        self.show_all()

    def _set(self, key, value):
        self._settings[key] = value
        core.save_settings(self._settings)
        for instance in _instances:
            instance.apply_settings(self._settings)

    def _add_row(self, label, widget):
        self.attach(Gtk.Label(label=label, xalign=0), 0, self._row, 1, 1)
        self.attach(widget, 1, self._row, 1, 1)
        self._row += 1

    def _add_entry(self, label, key, tooltip=None):
        entry = Gtk.Entry(text=self._settings[key], hexpand=True)
        entry.connect("changed", lambda entry: self._set(key, entry.get_text()))
        if tooltip:
            entry.set_tooltip_text(tooltip)
        self._add_row(label, entry)

    def _add_combo(self, label, key, choices):
        combo = Gtk.ComboBoxText()
        for value, text in choices:
            combo.append(value, text)
        combo.set_active_id(self._settings[key])
        combo.connect("changed", lambda combo: self._set(key, combo.get_active_id()))
        self._add_row(label, combo)

    def _add_check(self, label, key):
        check = Gtk.CheckButton(label=label, active=self._settings[key])
        check.connect("toggled", lambda check: self._set(key, check.get_active()))
        self.attach(check, 0, self._row, 2, 1)
        self._row += 1

    def _add_text(self, label, key):
        self.attach(Gtk.Label(label=label, xalign=0), 0, self._row, 2, 1)
        self._row += 1
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=4, right_margin=4)
        view.get_buffer().set_text(self._settings[key])
        view.get_buffer().connect("changed", lambda buf: self._set(
            key, buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)))
        scrolled = Gtk.ScrolledWindow(min_content_height=90, hexpand=True,
                                      shadow_type=Gtk.ShadowType.IN)
        scrolled.add(view)
        self.attach(scrolled, 0, self._row, 2, 1)
        self._row += 1
