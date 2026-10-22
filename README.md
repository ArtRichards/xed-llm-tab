# LLM Tab for xed

LLM Tab is a plugin for [xed](https://github.com/linuxmint/xed), the Linux Mint text editor. It adds an output tab to each window.

Type `===` in any other tab and the plugin sends a request to an OpenAI model. The reply appears in the output tab. The request contains:

- the system prompt
- your initial prompt
- an optional style guide
- the text of every other open tab, in tab order

> **Privacy:** every open tab's full text is sent to the API whenever a request runs. Close tabs you don't want sent. Requests use `store: false`.

## Requirements

- xed 2.8 or later, which means Linux Mint 20 or later, or LMDE 4 or later. Tested on xed 3.6.6.
- An OpenAI API key.

No extra Python packages are needed: the plugin uses only the Python standard library and the GTK bindings that xed already ships with.

## Install

```sh
git clone https://github.com/ArtRichards/xed-llm-tab.git ~/.local/share/xed/plugins/xed-llm-tab
```

1. Restart xed.
2. Go to **Edit → Preferences → Plugins** and enable **LLM Tab**.
3. Give the plugin your API key. Either set the `OPENAI_API_KEY` environment variable, or put the key in a file and enter that file's path under the plugin's **Preferences → API key file**.

To update, run `git pull` in that folder and restart xed. To uninstall, disable the plugin and delete the folder.

## Display modes

**Incognito** (the default): the output tab looks and behaves like an ordinary file tab.

- **Only finished replies appear.** The previous reply stays until the next one is complete, then it's replaced in one go. The tab never shows a waiting message, streaming text or an error.
- **Errors go to a log file:** `~/.cache/xed-llm-tab/errors.log`. If nothing seems to happen, look there first.
- **The window title reads like a normal file.** It shows `<tab name> (<save folder>)`. No file is actually written there unless you press Ctrl+S in the tab.
- **The tab is editable.** Your edits get the usual `*` mark, and the next reply replaces them.

**Streaming**: the reply streams in as it's generated, after an "Asking…" line, with Markdown highlighting. Errors are shown in the tab. The tab is read-only.

In both modes, typing a new `===` cancels a reply that's still in progress.

## Behavior in both modes

- **What triggers a request.** A new run of three or more `=` that you type or paste. Extending `===` to `====` doesn't send again. Opening a file doesn't trigger a request, and neither does the model's own output.
- **Replies never mark the tab modified.** The tab never shows a `*` for a reply, and closing it never asks to save.
- **Closing the tab saves its last finished reply.** It goes to `<save folder>/<tab-name>-<timestamp>.md`.
- **A closed tab stays closed.** While it's closed, `===` does nothing. **Tools → Open ‹tab name›** reopens it.
- **Several windows.** Each window has its own output tab. If you move the output tab to a window that doesn't have one, it takes over that role there.

## Settings

Open them from **Edit → Preferences → Plugins → LLM Tab → Preferences**. They're stored in `~/.config/xed/llm-tab.json` and take effect immediately.

| Setting | Default | Notes |
|---|---|---|
| Display | Incognito | Incognito or Streaming |
| Tab name | `LLM Output` | Also used for the menu item and saved files |
| Model | `gpt-6.1-sol` | Any model that supports the Responses API |
| Reasoning level | `medium` | `low`, `medium`, `high`, `xhigh` or `max` |
| API key file | none | When empty, `$OPENAI_API_KEY` is used. If the file holds several keys, the line starting with `sk-` is used. |
| API base URL | `https://api.openai.com/v1` | Must be `https`; plain `http` only for `localhost` |
| Save closed tabs to | `~/xed-llm-tab` | In incognito mode this folder's name appears in the window title |
| Style guide file | none | Sent after the initial prompt; not sent twice if it's also open in a tab |
| Query the open tabs when the output tab opens | off | When on, a request also runs whenever a window opens or the tab is reopened |
| Only trigger on a line containing just `===` | off | Turn on if you write code containing `===` |
| System prompt, Initial prompt | built-in text | Fully editable |

## Development

Run the tests from the repository root:

```sh
python3 -m unittest discover -s tests   # core logic, against a local mock API
tests/integration/run.sh                # a real, headless xed in both display modes
```

The integration test needs the `broadwayd` and `dbus-run-session` commands, from the `libgtk-3-bin` and `dbus` packages. It runs xed with its own D-Bus session, settings and folders against a mock API. It never touches your xed configuration and never calls OpenAI.

## License

MIT. See [LICENSE](LICENSE).
