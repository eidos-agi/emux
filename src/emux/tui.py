"""emux textual TUI — two-pane session picker.

Pattern lifted from ai-cockpit's `cockpit` command (the "cr v2 / cockpit"
reference Daniel pointed at) and codified in cockpit-eidos's tui-forge
brief (2026-04-28). Floor patterns:

  1. textual (not stdlib input)
  2. Two-pane: nav (40%) + preview (60%)
  3. Group headers (non-selectable) for category separation
  4. Number keys (1-9) for instant select
  5. Explicit BINDINGS table — auto-renders in footer
  6. Custom eidos themes (dark + light)
  7. Visual indicators (●, ⚙, color) over words
  8. Live preview updates on highlight
  9. Crash logging to ~/.config/emux/crashes/
 10. Action methods set a result and exit; outer caller does the work

Run: `from emux.tui import run_tui; result = run_tui()` returns a dict
describing the user's selection, or None if they quit. Caller dispatches.
"""

from __future__ import annotations

import datetime as _dt
import os
import traceback
from pathlib import Path
from typing import Any

from .server import _live_sessions, _load_registry


CRASH_DIR = Path(
    os.environ.get("EMUX_CRASH_DIR")
    or (Path.home() / ".config" / "emux" / "crashes")
)


def _log_crash(error: Exception) -> Path:
    """Persist a crash trace and return the log path."""
    CRASH_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = CRASH_DIR / f"{ts}.log"
    log.write_text(
        f"emux TUI crash at {ts}\n"
        f"{type(error).__name__}: {error}\n\n"
        f"{traceback.format_exc()}\n"
    )
    return log


def _format_unix(ts: int | None) -> str:
    if not ts:
        return "—"
    try:
        return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "—"


def _build_groups() -> dict[str, list[dict[str, Any]]]:
    """Return ordered groups for the nav list.

    Keys (in display order):
      - registered_live    — registered, tmux session alive
      - registered_stale   — registered, tmux session gone
      - unregistered_live  — live tmux sessions not in registry
      - actions            — synthetic group, always contains "register new"
    """
    registry = _load_registry()
    live = _live_sessions()
    live_by_name = {s["name"]: s for s in live}
    registered_sessions = {entry["session"] for entry in registry.values()}

    groups: dict[str, list[dict[str, Any]]] = {
        "registered_live": [],
        "registered_stale": [],
        "unregistered_live": [],
        "actions": [],
    }

    # Newest registered first.
    for name, entry in sorted(
        registry.items(),
        key=lambda kv: -int(kv[1].get("registered_at", 0)),
    ):
        session = entry["session"]
        is_live = session in live_by_name
        item = {
            "kind": "registered",
            "name": name,
            "session": session,
            "description": entry.get("description"),
            "tags": entry.get("tags") or [],
            "registered_at": entry.get("registered_at"),
            "is_stale": not is_live,
            "tmux": live_by_name.get(session),  # may be None if stale
        }
        if is_live:
            groups["registered_live"].append(item)
        else:
            groups["registered_stale"].append(item)

    for s in live:
        if s["name"] not in registered_sessions:
            groups["unregistered_live"].append({
                "kind": "live",
                "name": s["name"],
                "session": s["name"],
                "tmux": s,
            })

    groups["actions"].append({
        "kind": "register_new",
        "label": "(register new)",
        "detail": "press enter to register a new session by name + tmux id",
    })

    return groups


GROUP_TITLES = {
    "registered_live": "Registered (live)",
    "registered_stale": "Registered (stale)",
    "unregistered_live": "Unregistered live tmux",
    "actions": "Actions",
}


def _build_preview_for(item: dict[str, Any] | None) -> str:
    """Render the preview pane for a given item (BBCode markup for textual.Static)."""
    if item is None:
        return "[dim]nothing selected[/dim]"

    kind = item["kind"]
    lines: list[str] = []

    if kind == "registered":
        lines.append(f"[bold cyan]{_esc(item['name'])}[/bold cyan]")
        if item.get("description"):
            lines.append(f"[dim]{_esc(item['description'])}[/dim]")
        lines.append("")
        lines.append(f"[bold]→[/bold]   {_esc(item['session'])}")
        if item["is_stale"]:
            lines.append(f"[bold]Status[/bold]  [yellow]● STALE — tmux session gone[/yellow]")
        else:
            lines.append(f"[bold]Status[/bold]  [green]● live[/green]")
        if item.get("tags"):
            lines.append(f"[bold]Tags[/bold]    {' '.join('#' + _esc(t) for t in item['tags'])}")
        if item.get("registered_at"):
            lines.append(f"[bold]Since[/bold]   {_format_unix(item['registered_at'])}")
        lines.append("")

        if item.get("tmux"):
            t = item["tmux"]
            lines.append("[bold yellow]━━ tmux state ━━[/bold yellow]")
            lines.append(f"  Windows    {t.get('windows', '?')}")
            lines.append(f"  Created    {_format_unix(t.get('created_unix'))}")
            attached = "[green]yes[/green]" if t.get("attached") else "[dim]no[/dim]"
            lines.append(f"  Attached   {attached}")
            lines.append("")

        lines.append("[bold]━━ Launch command ━━[/bold]")
        lines.append(f"  [dim]tmux attach -t {_esc(item['session'])}[/dim]")

    elif kind == "live":
        lines.append(f"[bold cyan]{_esc(item['name'])}[/bold cyan]")
        lines.append("[dim]live tmux session, not yet in your registry[/dim]")
        lines.append("")
        t = item.get("tmux") or {}
        lines.append(f"[bold]→[/bold]   {_esc(item['session'])}")
        lines.append(f"[bold]Windows[/bold] {t.get('windows', '?')}")
        lines.append(f"[bold]Created[/bold] {_format_unix(t.get('created_unix'))}")
        attached = "[green]yes[/green]" if t.get("attached") else "[dim]no[/dim]"
        lines.append(f"[bold]Attached[/bold] {attached}")
        lines.append("")
        lines.append("[bold]━━ Actions ━━[/bold]")
        lines.append("  [bold]enter[/bold]   attach (you'll be in tmux until you detach)")
        lines.append("  [bold]r[/bold]       register this session under a friendly name")

    elif kind == "register_new":
        lines.append("[bold cyan]Register a new session[/bold cyan]")
        lines.append("[dim]name an existing tmux session for future emux invocations[/dim]")
        lines.append("")
        lines.append("[bold]Press enter[/bold] to walk through:")
        lines.append("  • registry name (e.g., claude-prod)")
        lines.append("  • tmux session id (e.g., main)")
        lines.append("  • description (optional)")
        lines.append("  • tags (space-separated, optional)")
        lines.append("")
        lines.append("[dim]After registering, you'll be offered to attach.[/dim]")

    return "\n".join(lines)


def _esc(text: Any) -> str:
    """Escape textual markup brackets in user-supplied strings."""
    return str(text).replace("[", "\\[")


def run_tui() -> dict[str, Any] | None:
    """Run the textual TUI. Returns a result dict or None on quit.

    Result shapes:
      {"action": "attach", "session": "..."}
      {"action": "register_then_attach", "default_session": "..."}
      {"action": "register_new"}
      {"action": "unregister", "name": "..."}

    The caller is responsible for actually performing the side effect.
    """
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical, VerticalScroll
        from textual.theme import Theme as TextualTheme
        from textual.widgets import Footer, Header, ListItem, ListView, Static
    except ImportError as e:
        print(f"emux: textual is required for the TUI ({e}).", file=sys.stderr)
        print("       install with: uv pip install 'textual>=0.40.0'", file=sys.stderr)
        return None

    groups = _build_groups()

    # Flatten into the order the ListView will render. Each entry remembers its
    # ordinal position among selectable items so number keys can target them.
    selectable: list[dict[str, Any]] = []
    flat_render: list[tuple[str, dict[str, Any] | None]] = []  # (kind, payload)
    for group_key in ("registered_live", "registered_stale", "unregistered_live", "actions"):
        items = groups[group_key]
        if not items:
            continue
        flat_render.append(("header", {"key": group_key, "count": len(items)}))
        for item in items:
            selectable.append(item)
            flat_render.append(("item", item))

    if not selectable:
        # Nothing to pick; return None so caller can show a friendly message.
        return None

    THEME_DEFS = [
        TextualTheme(
            name="eidos",
            primary="#c4935a",
            secondary="#7a8c72",
            accent="#b8c4a0",
            background="#1e1a17",
            surface="#161210",
            panel="#2a2420",
            error="#c4694f",
            warning="#c4935a",
            success="#7a8c72",
        ),
        TextualTheme(
            name="eidos-light",
            primary="#9a6d35",
            secondary="#5a6c52",
            accent="#4a6a3a",
            background="#f0ebe4",
            surface="#e4ded6",
            panel="#d8d2c8",
            dark=False,
        ),
    ]

    class GroupHeader(ListItem):
        def __init__(self, group_key: str, count: int, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.group_key = group_key
            self.count = count
            self.payload = None  # marker: not selectable

        def compose(self) -> ComposeResult:
            title = GROUP_TITLES.get(self.group_key, self.group_key)
            yield Static(
                f"\n [bold]{title}[/bold]  [dim]{self.count}[/dim]",
                markup=True,
            )

    class SessionRow(ListItem):
        def __init__(self, number: int, item: dict[str, Any], **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.number = number
            self.payload = item

        def compose(self) -> ComposeResult:
            item = self.payload
            assert item is not None
            kind = item["kind"]
            num = f"[bold yellow]{self.number}[/bold yellow]" if self.number <= 9 else "[dim] [/dim]"
            if kind == "registered":
                if item["is_stale"]:
                    dot = "[yellow]●[/yellow]"
                else:
                    dot = "[green]●[/green]"
                name = f"[bold]{_esc(item['name'])}[/bold]"
                arrow_line = f"[dim]→ {_esc(item['session'])}[/dim]"
                yield Static(f"   {num}  {dot}  {name}  {arrow_line}", markup=True)
            elif kind == "live":
                yield Static(
                    f"   {num}  [cyan]○[/cyan]  [bold]{_esc(item['session'])}[/bold]  [dim]unregistered[/dim]",
                    markup=True,
                )
            elif kind == "register_new":
                yield Static(
                    f"   {num}  [cyan]⊕[/cyan]  [bold]{_esc(item['label'])}[/bold]  [dim]{_esc(item['detail'])}[/dim]",
                    markup=True,
                )

    result_holder: dict[str, Any] = {"result": None}

    class EmuxApp(App):
        CSS = """
        Screen { layout: horizontal; }
        #nav-pane    { width: 40%; border-right: heavy $primary; padding: 1; background: $surface; }
        #preview-pane { width: 60%; padding: 1 2; overflow-y: auto; }
        ListView { height: 100%; }
        ListItem { padding: 0; height: 1; }
        ListView > ListItem.--highlight { background: $primary 25%; }
        """

        TITLE = "emux"
        SUB_TITLE = "pick up where you left off in tmux"

        BINDINGS = [
            Binding("enter", "primary", "Attach"),
            Binding("r", "register", "Register"),
            Binding("u", "unregister", "Unregister"),
            Binding("R", "rescan", "Rescan"),
            Binding("t", "cycle_theme", "Theme"),
            Binding("q", "quit", "Quit"),
            Binding("escape", "quit", "Quit"),
        ]

        def compose(self) -> ComposeResult:
            yield Header()
            list_items: list[ListItem] = []
            num = 1
            for kind, payload in flat_render:
                if payload is None:
                    continue
                if kind == "header":
                    list_items.append(GroupHeader(payload["key"], payload["count"]))
                else:
                    list_items.append(SessionRow(num, payload))
                    num += 1
            with Horizontal():
                with Vertical(id="nav-pane"):
                    yield ListView(*list_items, id="nav-list")
                with VerticalScroll(id="preview-pane"):
                    yield Static(id="preview", markup=True)
            yield Footer()

        def on_mount(self) -> None:
            for t in THEME_DEFS:
                self.register_theme(t)
            self.theme = THEME_DEFS[0].name
            lv = self.query_one("#nav-list", ListView)
            # Find the first selectable row and highlight it.
            for i, list_item in enumerate(lv.children):
                if isinstance(list_item, SessionRow):
                    lv.index = i
                    self.query_one("#preview", Static).update(_build_preview_for(list_item.payload))
                    break

        def _handle_exception(self, error: Exception) -> None:
            log = _log_crash(error)
            self.notify(f"Crash logged: {log}", severity="error")
            super()._handle_exception(error)

        def on_key(self, event: Any) -> None:
            ch = getattr(event, "character", None)
            if ch and ch.isdigit() and ch != "0":
                target = int(ch)
                lv = self.query_one("#nav-list", ListView)
                # Walk children, count SessionRows, find the Nth.
                seen = 0
                for i, list_item in enumerate(lv.children):
                    if isinstance(list_item, SessionRow):
                        seen += 1
                        if seen == target:
                            lv.index = i
                            self.query_one("#preview", Static).update(
                                _build_preview_for(list_item.payload)
                            )
                            event.stop()
                            return

        def on_list_view_highlighted(self, event: Any) -> None:
            item = event.item
            if isinstance(item, SessionRow):
                self.query_one("#preview", Static).update(_build_preview_for(item.payload))
            elif isinstance(item, GroupHeader):
                # Skip headers — try to move past them in the direction the user came from.
                # Simplest: leave preview alone, let next arrow press pick the next selectable.
                pass

        def _selected_payload(self) -> dict[str, Any] | None:
            lv = self.query_one("#nav-list", ListView)
            cur = lv.highlighted_child
            if isinstance(cur, SessionRow):
                return cur.payload
            return None

        def action_primary(self) -> None:
            payload = self._selected_payload()
            if payload is None:
                self.notify("nothing selected", severity="warning")
                return
            kind = payload["kind"]
            if kind == "registered":
                if payload["is_stale"]:
                    self.notify(
                        f"'{payload['name']}' is stale — tmux session '{payload['session']}' is gone. "
                        "Press 'u' to unregister, 'R' to rescan, or pick something else.",
                        severity="warning",
                        timeout=5,
                    )
                    return
                result_holder["result"] = {"action": "attach", "session": payload["session"]}
                self.exit()
            elif kind == "live":
                # Bare-enter on an unregistered live session: just attach.
                result_holder["result"] = {"action": "attach", "session": payload["session"]}
                self.exit()
            elif kind == "register_new":
                result_holder["result"] = {"action": "register_new"}
                self.exit()

        def action_register(self) -> None:
            payload = self._selected_payload()
            if payload is None:
                return
            kind = payload["kind"]
            if kind == "live":
                result_holder["result"] = {
                    "action": "register_then_attach",
                    "default_session": payload["session"],
                }
                self.exit()
            elif kind == "register_new":
                result_holder["result"] = {"action": "register_new"}
                self.exit()
            elif kind == "registered":
                self.notify(f"'{payload['name']}' is already registered.")

        def action_unregister(self) -> None:
            payload = self._selected_payload()
            if payload is None or payload["kind"] != "registered":
                self.notify("select a registered entry to unregister.")
                return
            result_holder["result"] = {"action": "unregister", "name": payload["name"]}
            self.exit()

        def action_rescan(self) -> None:
            self.notify("rescan: quit (q) and re-run `emux` to pick up changes.")

        def action_cycle_theme(self) -> None:
            current = self.theme
            names = [t.name for t in THEME_DEFS]
            try:
                idx = names.index(current)
            except ValueError:
                idx = -1
            self.theme = names[(idx + 1) % len(names)]
            self.notify(f"theme: {self.theme}")

    app = EmuxApp()
    try:
        app.run()
    except Exception as e:
        log = _log_crash(e)
        print(f"\n  emux TUI crashed; log: {log}", flush=True)
        return None

    if hasattr(app, "_exception") and app._exception is not None:
        log = _log_crash(app._exception)
        print(f"\n  textual error logged: {log}", flush=True)

    return result_holder["result"]


# Defensive lazy import: keep `sys` out of the top-level import unless needed.
import sys  # noqa: E402
