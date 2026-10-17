"""GNOME (GTK 4 / libadwaita) window for cloudsync. Linux only; Windows uses Tk."""

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

APP_ID = "io.github.htlemke.CloudSync"


def bg(fn, done, error):
    """Run fn in a thread, then done(result) or error(exc) in the GTK thread."""
    def run():
        try:
            res = fn()
        except Exception as e:
            GLib.idle_add(error, e)
        else:
            GLib.idle_add(done, res)
    threading.Thread(target=run, daemon=True).start()


class Folder(GObject.Object):
    def __init__(self, remote, path, name):
        super().__init__()
        self.remote, self.path, self.name = remote, path, name
        self.children = Gio.ListStore(item_type=Folder)
        self.loaded = self.loading = False
        self.size = ""


class Window(Adw.ApplicationWindow):
    def __init__(self, app, cs):
        super().__init__(application=app, title=f"Cloud Sync – {cs.machine_name()}")
        self.cs = cs
        self.sel = cs.Selection()
        self.set_default_size(820, 640)
        self.bound = {}  # remote -> {list item: (folder, check, size label)}
        self.updating = False
        self.switches = {}
        self.models = {}  # remote -> (TreeListModel, ListView)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self.save_btn = Gtk.Button(label="Save", css_classes=["suggested-action"])
        self.save_btn.connect("clicked", lambda *_: self.save(False))
        header.pack_end(self.save_btn)
        self.sync_btn = Gtk.Button(icon_name="emblem-synchronizing-symbolic",
                                   tooltip_text="Save and sync now")
        self.sync_btn.connect("clicked", lambda *_: self.save(True))
        header.pack_end(self.sync_btn)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner)
        view.add_top_bar(header)
        view.set_content(self.stack)

        free = cs.shutil.disk_usage(cs.local_root()).free / 1e9
        self.status = Gtk.Label(xalign=0, css_classes=["dim-label"], margin_start=12,
                                margin_end=12, margin_top=6, margin_bottom=6,
                                label=f"Free on {cs.local_root()}: {free:.1f} GB")
        view.add_bottom_bar(self.status)

        for name in self.sel.remotes:
            self.stack.add_titled_with_icon(self.page(name), name, name,
                                            "folder-remote-symbolic")
        if not self.sel.online:
            self.toast("gdrive unreachable – showing the cached selection")

    # --- one page per remote ------------------------------------------------

    def page(self, name):
        self.bound[name] = {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        prefs = Adw.PreferencesGroup(margin_start=12, margin_end=12, margin_top=12)
        row = Adw.SwitchRow(title="Sync this remote",
                            subtitle=f"to {self.cs.local_root() / name}",
                            active=self.sel.enabled[name])
        row.connect("notify::active",
                    lambda r, _p: self.sel.enabled.__setitem__(name, r.get_active()))
        self.switches[name] = row
        prefs.add(row)
        box.append(prefs)

        top = Gio.ListStore(item_type=Folder)
        top.append(Folder(name, "", "Whole remote"))
        model = Gtk.TreeListModel.new(top, False, False, lambda f: f.children)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.setup_row)
        factory.connect("bind", self.bind_row, name)
        factory.connect("unbind", lambda _f, li: self.bound[name].pop(li, None))
        lv = Gtk.ListView(model=Gtk.SingleSelection(model=model, autoselect=False),
                          factory=factory, css_classes=["navigation-sidebar"])
        self.models[name] = (model, lv)
        frame = Gtk.Frame(child=Gtk.ScrolledWindow(child=lv, vexpand=True),
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        box.append(frame)
        model.get_row(0).set_expanded(True)
        return box

    def setup_row(self, _factory, li):
        expander = Gtk.TreeExpander()
        h = Gtk.Box(spacing=8)
        check = Gtk.CheckButton()
        label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        size = Gtk.Label(css_classes=["dim-label", "numeric"])
        size_btn = Gtk.Button(icon_name="drive-harddisk-symbolic", css_classes=["flat"],
                              tooltip_text="Measure folder size")
        for w in (check, label, size, size_btn):
            h.append(w)
        expander.set_child(h)
        li.set_child(expander)
        li.widgets = (expander, check, label, size, size_btn)
        check.connect("toggled", self.on_toggled, li)
        size_btn.connect("clicked", self.on_size, li)

    def bind_row(self, _factory, li, name):
        expander, check, label, size, _ = li.widgets
        row = li.get_item()
        folder = row.get_item()
        li.folder, li.row = folder, row
        expander.set_list_row(row)
        label.set_label(folder.name)
        label.set_css_classes(["heading"] if folder.path == "" else [])
        size.set_label(folder.size)
        self.bound[name][li] = folder
        self.show_state(li)
        if getattr(li, "handler", None):
            li.handler_row.disconnect(li.handler)
        li.handler_row = row
        li.handler = row.connect("notify::expanded", lambda r, _p: self.on_expand(r))
        if row.get_expanded():
            self.on_expand(row)

    def show_state(self, li):
        _, check, *_ = li.widgets
        state = self.cs.tri_state(self.sel.rules[li.folder.remote], li.folder.path)
        self.updating = True
        check.set_inconsistent(state == "partial")
        check.set_active(state == "on")
        self.updating = False

    def on_toggled(self, _check, li):
        if self.updating:
            return
        f = li.folder
        self.sel.toggle(f.remote, f.path)
        self.switches[f.remote].set_active(self.sel.enabled[f.remote])
        for other in self.bound[f.remote]:
            self.show_state(other)

    def on_expand(self, row):
        f = row.get_item()
        if not row.get_expanded() or f.loaded or f.loading:
            return
        f.loading = True
        self.busy(+1)

        def done(dirs):
            for d in dirs:
                f.children.append(Folder(f.remote, f"{f.path}/{d}" if f.path else d, d))
            f.loaded, f.loading = True, False
            self.busy(-1)

        def error(e):
            f.loading = False
            self.busy(-1)
            self.toast(f"Listing failed: {e}")
        bg(lambda: self.cs.list_dirs(f.remote, f.path), done, error)

    def on_size(self, _btn, li):
        f = li.folder
        self.busy(+1)

        def done(b):
            f.size = f"{b / 1e9:.2f} GB"
            if getattr(li, "folder", None) is f:
                li.widgets[3].set_label(f.size)
            self.busy(-1)
        bg(lambda: self.cs.folder_size(f.remote, f.path), done,
           lambda e: (self.busy(-1), self.toast(f"Size failed: {e}")))

    def reveal(self, remote, path):
        """Show remote's page with the tree opened down to path."""
        if remote not in self.models:
            self.toast(f"No rclone remote {remote!r}")
            return
        self.stack.set_visible_child_name(remote)
        model, lv = self.models[remote]
        parts = [p for p in path.split("/") if p]

        def step(row, i):
            if i == len(parts):
                pos = row.get_position()
                lv.scroll_to(pos, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
                self.toast(f"Tick “{row.get_item().name}” to sync it, then Save")
                return
            f = row.get_item()
            row.set_expanded(True)
            self.on_expand(row)  # rows off screen have no expand handler yet

            def wait():
                if f.loading:
                    return True  # still listing, check again
                if not f.loaded:
                    return False  # listing failed, already shown as a toast
                for k in range(f.children.get_n_items()):
                    if f.children.get_item(k).name == parts[i]:
                        step(row.get_child_row(k), i + 1)
                        return False
                self.toast(f"Not found: {remote}:{path}")
                return False
            GLib.timeout_add(100, wait)
        step(model.get_row(0), 0)

    # --- save / sync ----------------------------------------------------------

    def save(self, then_sync):
        try:
            gone = self.sel.save()
        except RuntimeError as e:
            self.toast(str(e))
            return
        self.toast("Selection saved to gdrive")
        listing = [f"{n}: {p}" for n, ps in gone.items() for p in ps]
        if not listing:
            if then_sync:
                self.sync()
            return
        body = "\n".join(listing[:15]) + (f"\n… and {len(listing) - 15} more"
                                          if len(listing) > 15 else "")
        dlg = Adw.AlertDialog(heading="Delete local copies?",
                              body="These are no longer synced. The cloud copies stay.\n\n" + body)
        dlg.add_response("keep", "Keep")
        dlg.add_response("delete", "Delete local copies")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")

        def answered(_d, resp):
            if resp == "delete":
                for n, ps in gone.items():
                    self.cs.remove_local(self.cs.local_root() / n, ps)
            if then_sync:
                self.sync()
        dlg.connect("response", answered)
        dlg.present(self)

    def sync(self):
        self.sync_btn.set_sensitive(False)
        self.busy(+1)

        def done(res):
            self.sync_btn.set_sensitive(True)
            self.busy(-1)
            self.toast("Sync: " + (", ".join(f"{n} {'ok' if rc == 0 else 'FAILED'}"
                                             for n, rc in res) or "nothing selected"))
        bg(self.sel.sync_all, done,
           lambda e: (self.sync_btn.set_sensitive(True), self.busy(-1),
                      self.toast(f"Sync failed: {e}")))

    # --- helpers --------------------------------------------------------------

    _busy = 0

    def busy(self, delta):
        self._busy += delta
        self.spinner.set_spinning(self._busy > 0)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))


def run(cs, show=None):
    """show: "remote:path" to open at; a second call reuses the open window."""
    app = Adw.Application(application_id=APP_ID,
                          flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    def command_line(a, cl):
        win = a.get_active_window() or Window(a, cs)
        win.present()
        args = cl.get_arguments()[1:]
        if args and ":" in args[0]:
            win.reveal(*args[0].split(":", 1))
        return 0
    app.connect("command-line", command_line)
    return app.run(["cloudsync"] + ([show] if show else []))
