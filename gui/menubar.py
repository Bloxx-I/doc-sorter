"""Menu bar (status bar) icon: the app keeps watching the inbox while the window is closed.

Closing the window only hides it; the Dock icon disappears too. The menu opens the window,
jumps to a view, checks the inbox, toggles "start at login" and quits.
"""

import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import AppKit
import objc
from Foundation import NSObject, NSTimer
from PyObjCTools import AppHelper

ROOT = Path(__file__).resolve().parent.parent
AGENT_LABEL = "local.docsorter.mac"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def login_item_enabled():
    return AGENT_PLIST.exists()


def set_login_item(enabled):
    """A LaunchAgent starts the sorter hidden (menu bar only) after login."""
    if enabled:
        AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
        plistlib.dump({"Label": AGENT_LABEL, "RunAtLoad": True, "ProcessType": "Interactive",
                       "ProgramArguments": [sys.executable, str(ROOT / "main.py"), "--hidden"],
                       "WorkingDirectory": str(ROOT),
                       "StandardErrorPath": str(Path.home() / "Library" / "Logs" / "doc-sorter.log")},
                      AGENT_PLIST.open("wb"))
    else:
        AGENT_PLIST.unlink(missing_ok=True)


class MenuTarget(NSObject):
    """Objective-C target for the menu items; forwards to the Python MenuBar."""

    def initWithOwner_(self, owner):
        self = objc.super(MenuTarget, self).init()
        self.owner = owner
        return self

    def open_(self, _):
        self.owner.show()

    def showView_(self, item):
        self.owner.show(item.representedObject())

    def scan_(self, _):
        self.owner.service.scan(force=True)

    def pause_(self, item):
        minutes = item.representedObject()
        self.owner.service.pause(None if minutes == 0 else int(minutes))
        self.owner.refresh()

    def resume_(self, _):
        self.owner.service.resume()
        self.owner.refresh()

    def reveal_(self, item):
        subprocess.run(["open", item.representedObject()], check=False)

    def toggleLogin_(self, item):
        set_login_item(not login_item_enabled())
        item.setState_(AppKit.NSControlStateValueOn if login_item_enabled() else AppKit.NSControlStateValueOff)

    def checkUpdate_(self, _):
        self.owner.check_update(interactive=True)

    def installUpdate_(self, _):
        self.owner.api.install_update()

    def quit_(self, _):
        self.owner.quit()

    def tick_(self, _):
        self.owner.refresh()


class MenuBar:
    def __init__(self, window, service, api=None):
        self.window = window
        self.service = service
        self.api = api
        self.quitting = False
        self.item = None

    # ------------------------------------------------------------ setup (main thread)
    def install(self):
        AppHelper.callAfter(self._install)

    def _install(self):
        self.target = MenuTarget.alloc().initWithOwner_(self)
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.icons = {}
        for key, symbol in (("run", "tray.and.arrow.down.fill"), ("pause", "pause.circle")):
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, "Dokumenten-Sortierer")
            image.setTemplate_(True)
            self.icons[key] = image
        button = self.item.button()
        button.setImage_(self.icons["run"])
        button.setImagePosition_(AppKit.NSImageLeft)
        button.setToolTip_("Dokumenten-Sortierer")

        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.status_line = self._add(menu, "Bereit", None)
        self.status_line.setEnabled_(False)
        self.stage_line = self._add(menu, "", None)
        self.stage_line.setEnabled_(False)
        self.stage_line.setHidden_(True)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add(menu, "Sortierer öffnen", "open:", "o")
        self._add(menu, "Ablage-Karte", "showView:", view="archive")
        self._add(menu, "Suche", "showView:", "f", view="search")
        self._add(menu, "Protokoll", "showView:", view="log")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self.resume_item = self._add(menu, "Überwachung fortsetzen", "resume:")
        pause = self._add(menu, "Überwachung pausieren", None)
        pause_menu = AppKit.NSMenu.alloc().init()
        for title, minutes in (("15 Minuten", 15), ("30 Minuten", 30), ("1 Stunde", 60), ("Bis ich fortsetze", 0)):
            self._add(pause_menu, title, "pause:", view=minutes)
        pause.setSubmenu_(pause_menu)
        self.pause_item = pause
        self._add(menu, "Eingang jetzt prüfen", "scan:", "r")
        self.incoming_item = self._add(menu, "Eingangsordner im Finder", None)
        self.open_output = self._add(menu, "Ablage im Finder", "reveal:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add(menu, "Einstellungen …", "showView:", ",", view="settings")
        self.update_item = self._add(menu, "Nach Updates suchen …", "checkUpdate:")
        login = self._add(menu, "Beim Anmelden starten", "toggleLogin:")
        login.setState_(AppKit.NSControlStateValueOn if login_item_enabled() else AppKit.NSControlStateValueOff)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add(menu, "Beenden", "quit:", "q")
        self.item.setMenu_(menu)

        if not self.window.native.isVisible():
            AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.5, self.target, "tick:", None, True)
        self.refresh()
        self.check_update()

    def _add(self, menu, title, action, key="", view=None):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            item.setTarget_(self.target)
        if view is not None:
            item.setRepresentedObject_(view)
        menu.addItem_(item)
        return item

    # ------------------------------------------------------------ live status
    def refresh(self):
        if not self.item:
            return
        pending = len(self.service.db.pending())
        processing = self.service.processing()
        paused = self.service.paused
        self.item.button().setImage_(self.icons["pause" if paused else "run"])
        badge = (f" {pending}" if pending else "") + (" ↑" if getattr(self, "update_available", False) else "")
        self.item.button().setTitle_(badge)
        self.resume_item.setHidden_(not paused)
        self.pause_item.setHidden_(paused)
        if paused:
            until = self.service.paused_until
            self.status_line.setTitle_("Pausiert bis " + time.strftime("%H:%M", time.localtime(until))
                                       if until != float("inf") else "Überwachung pausiert")
            self.stage_line.setHidden_(True)
        elif processing:
            self.status_line.setTitle_(f"Analysiere {processing[0]['name']}")
            self.stage_line.setTitle_(processing[0]["stage"])
            self.stage_line.setHidden_(False)
        else:
            self.stage_line.setHidden_(True)
            self.status_line.setTitle_(f"{pending} Dokument{'e' if pending != 1 else ''} warten auf Ablage"
                                       if pending else "Alles abgelegt · Eingang wird beobachtet")
        dirs = self.service.config["incoming_dirs"]
        if dirs != getattr(self, "_menu_dirs", None):   # rebuild the submenu only when the list changed
            self._menu_dirs = list(dirs)
            submenu = AppKit.NSMenu.alloc().init()
            for folder in dirs:
                self._add(submenu, folder.replace(str(Path.home()), "~"), "reveal:", view=folder)
            self.incoming_item.setSubmenu_(submenu)
        self.open_output.setRepresentedObject_(self.service.config["output_dir"])

    # ------------------------------------------------------------ updates
    def check_update(self, interactive=False):
        """Background check; an available update turns the menu entry into 'Update installieren'."""
        import threading

        def run():
            res = self.api.check_update() if self.api else {}
            def apply():
                if res.get("available") and res.get("installed"):
                    self.update_item.setTitle_(f"Update auf {res['latest']} installieren")
                    self.update_item.setAction_("installUpdate:")
                    self.update_available = True
                elif interactive:
                    alert = AppKit.NSAlert.alloc().init()
                    alert.setMessageText_("Dokumenten-Sortierer")
                    alert.setInformativeText_(
                        f"Version {res.get('current')} ist aktuell." if not res.get("available") else
                        f"Version {res['latest']} ist verfügbar. Entwicklungs-Checkout: bitte 'git pull'.")
                    AppKit.NSApp.activateIgnoringOtherApps_(True)
                    alert.runModal()
            AppHelper.callAfter(apply)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ window handling
    def show(self, view=None):
        def apply():
            AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
            self.window.native.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)
        AppHelper.callAfter(apply)
        self.window.show()
        if view:
            self.window.evaluate_js(f"typeof showView === 'function' && showView({json.dumps(view)})")

    def hide(self):
        self.window.native.orderOut_(None)
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    def on_closing(self):
        """Red close button or ⌘W: hide instead of quitting. ⌘Q and 'Beenden' really quit."""
        event = AppKit.NSApp.currentEvent()
        cmd_q = (event is not None and event.type() == AppKit.NSEventTypeKeyDown
                 and event.modifierFlags() & AppKit.NSEventModifierFlagCommand
                 and (event.charactersIgnoringModifiers() or "").lower() == "q")
        if self.quitting or cmd_q:
            self.quitting = True
            return True
        self.hide()
        return False

    def quit(self):
        self.quitting = True
        self.window.destroy()


def name_process(title="Dokumenten-Sortierer"):
    """Show a proper app name in the menu bar instead of 'Python'."""
    info = AppKit.NSBundle.mainBundle().localizedInfoDictionary() or AppKit.NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info["CFBundleName"] = title
