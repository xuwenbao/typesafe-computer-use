"""The foreground window's UI Automation tree, run inside a WAA virtual machine.

Sent to the VM as one script, with uia_roles.py concatenated in front of it, and executed there
with `python -c`. It imports nothing of jev: on the VM the role table is already in this process
from that concatenation, and elsewhere it imports uia_roles. comtypes is imported only when
collecting a live tree, so the walk itself can be driven from a test with stand-in elements.

The walk stops at a depth, a width, a node count, and 8 seconds, and says so in the JSON.
"""

import ctypes
import ctypes.wintypes
import json
import sys
import time

MAX_DEPTH = 30
MAX_WIDTH = 256
MAX_NODES = 8000
SECONDS = 8.0


class Walker:
    """Walk the foreground UI Automation tree inside the VM."""

    @staticmethod
    def _pattern_table():
        """The shared pattern ids. The import works in jev; the names work in the concatenated VM script."""
        try:
            from yuanji_computer_use.platforms.uia_patterns import PATTERN_IDS as table
        except ImportError:
            table = globals()["PATTERN_IDS"]
        return table

    @staticmethod
    def _rules():
        """The role table. The import works in jev; the names work in the concatenated VM script."""
        try:
            from yuanji_computer_use.platforms.uia_roles import DOCUMENT_ID, document_role, pressable, role_for_id
        except ImportError:
            pressable = globals()["pressable"]
            role_for_id = globals()["role_for_id"]
            document_role = globals()["document_role"]
            DOCUMENT_ID = globals()["DOCUMENT_ID"]

        def role_of(node):
            control_type = node.control_type()
            return document_role(node.legacy_state()) if control_type == DOCUMENT_ID else role_for_id(control_type)

        return role_of, pressable

    @staticmethod
    def _control_facts(node):
        """Enabled and supported patterns, when the element can say. Missing methods mean unknown."""
        enabled = node.enabled() if callable(getattr(node, "enabled", None)) else None
        patterns = list(node.pattern_ids()) if callable(getattr(node, "pattern_ids", None)) else []
        checked = node.checked() if callable(getattr(node, "checked", None)) else None
        expanded = node.expanded() if callable(getattr(node, "expanded", None)) else None
        selected = node.selected() if callable(getattr(node, "selected", None)) else None
        return {"enabled": enabled, "patterns": patterns, "checked": checked, "expanded": expanded, "selected": selected}

    @staticmethod
    def empty(reason):
        """A tree that names why it has nothing."""
        return {"app": "", "pid": 0, "window": None, "nodes": [], "truncated": False, "reason": reason}

    @staticmethod
    def _runtime_id(node):
        method = getattr(node, "runtime_id", None)
        if method is None:
            return None
        try:
            value = method()
        except Exception:
            return None
        if not value:
            return None
        return tuple(value)

    @staticmethod
    def _same(node, other):
        if node is other:
            return True
        left, right = Walker._runtime_id(node), Walker._runtime_id(other)
        return left is not None and left == right

    @staticmethod
    def _text_focus(node, role_of):
        """A focused text field, or the only text field inside a focused host that is not one.

        Several text fields and none of them focused is left alone: guessing would type into the
        wrong box."""
        if node is None:
            return None
        if role_of(node) in TEXT_ROLES:
            return node
        found = []

        def collect(current):
            for child in current.children():
                if role_of(child) in TEXT_ROLES:
                    found.append(child)
                else:
                    collect(child)

        collect(node)
        focused = [item for item in found if item.focused()]
        if len(focused) == 1:
            return focused[0]
        if len(found) == 1:
            return found[0]
        return None

    @staticmethod
    def walk(root, *, clock, focus=None, seconds=SECONDS, max_depth=MAX_DEPTH, max_width=MAX_WIDTH, max_nodes=MAX_NODES):
        """`root`'s descendants as JSON-ready dicts. `root` itself is the window, recorded apart.

        An element answers `name`, `control_type`, `rect`, `focused`, `value`, `has_invoke`,
        `default_action`, and `children`, and a document also `legacy_state`. `focus` is the element
        `GetFocusedElement` returned. A text field there, or the one text field inside a host that is
        not one, is the focused node, which is how a XAML island's edit is read as a text field.
        Past any limit the walk returns what it has and sets `truncated`."""
        role_of, can_press = Walker._rules()
        chosen = Walker._text_focus(focus, role_of)
        deadline = clock() + seconds
        truncated = False
        seen = 0
        marked = False

        def visit(node, depth):
            nonlocal truncated, seen, marked
            if seen >= max_nodes or clock() >= deadline:
                truncated = True
                return None
            seen += 1
            if chosen is not None and Walker._same(node, chosen):
                marked = True
            kids = list(node.children())
            if len(kids) > max_width:
                kids = kids[:max_width]
                truncated = True
            children = []
            if depth >= max_depth:
                truncated = truncated or bool(kids)
            else:
                for child in kids:
                    item = visit(child, depth + 1)
                    if item is not None:
                        children.append(item)
                    if clock() >= deadline:
                        truncated = True
                        break
            rect = node.rect()
            focused = Walker._same(node, chosen) if chosen is not None else bool(node.focused())
            return {
                "role": role_of(node),
                "name": node.name() or "",
                "rect": list(rect) if rect else None,
                "focused": focused,
                "value": node.value() or "",
                "pressable": can_press(bool(node.has_invoke()), node.default_action() or ""),
                **Walker._control_facts(node),
                "children": children,
            }

        if root is None:
            return Walker.empty("no foreground window")
        seen = 1  # the window itself counts, and is not one of the nodes
        if clock() >= deadline:
            body = Walker.empty("truncated")
            body["truncated"] = True
            return body
        nodes = []
        kids = list(root.children())
        if len(kids) > max_width:
            kids = kids[:max_width]
            truncated = True
        for child in kids:
            item = visit(child, 1)
            if item is not None:
                nodes.append(item)
            if clock() >= deadline:
                truncated = True
                break
        if chosen is not None and not marked:
            rect = chosen.rect()
            nodes.append(
                {
                    "role": role_of(chosen),
                    "name": chosen.name() or "",
                    "rect": list(rect) if rect else None,
                    "focused": True,
                    "value": chosen.value() or "",
                    "pressable": can_press(bool(chosen.has_invoke()), chosen.default_action() or ""),
                    **Walker._control_facts(chosen),
                    "children": [],
                }
            )
        rect = root.rect()
        window = None if rect is None else {"name": root.name() or "", "x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]}
        return {
            "app": "",
            "pid": 0,
            "window": window,
            "nodes": nodes,
            "truncated": truncated,
            "reason": "truncated" if truncated else None,
        }

    @staticmethod
    def foreground():
        """The foreground window's handle, process id, and executable path, or `(0, 0, "")`."""
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0, 0, ""
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        name = ""
        if handle:
            buf = ctypes.create_unicode_buffer(32768)
            size = ctypes.wintypes.DWORD(32768)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = buf.value
            kernel32.CloseHandle(handle)
        return hwnd, int(pid.value), name

    @staticmethod
    def connect(hwnd):
        """The foreground window as a `LiveElement`, through comtypes. Windows, and the VM, only."""
        import comtypes
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as api

        automation = comtypes.CoCreateInstance(api.CUIAutomation._reg_clsid_, interface=api.IUIAutomation)
        interfaces = {
            INVOKE: api.IUIAutomationInvokePattern,
            VALUE: api.IUIAutomationValuePattern,
            LEGACY: api.IUIAutomationLegacyIAccessiblePattern,
        }
        walker = automation.RawViewWalker
        window = LiveElement(automation.ElementFromHandle(hwnd), walker, interfaces)
        focused = None
        try:
            raw_focus = automation.GetFocusedElement()
        except Exception:
            raw_focus = None
        if raw_focus is not None:
            focused = LiveElement(raw_focus, walker, interfaces)
        return window, focused

    @staticmethod
    def main():
        try:
            hwnd, pid, exe = Walker.foreground()
            if not hwnd:
                payload = Walker.empty("no foreground window")
            else:
                window, focused = Walker.connect(hwnd)
                payload = Walker.walk(window, clock=time.monotonic, focus=focused)
                payload["app"] = exe
                payload["pid"] = pid
        except Exception as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
            raise SystemExit(1) from None
        json.dump(payload, sys.stdout, ensure_ascii=True)
        sys.stdout.write("\n")


INVOKE = Walker._pattern_table()["invoke"]
VALUE = Walker._pattern_table()["value"]
LEGACY = 10018  # UIA_LegacyIAccessiblePatternId, which is not an action jev offers the model
READ_ONLY = 0x40  # STATE_SYSTEM_READONLY
TEXT_ROLES = ("AXTextField", "AXTextArea")


class LiveElement:
    """One UI Automation element, behind the methods `walk` calls.

    `raw` is a comtypes element, or a stand-in with the same attributes. `interfaces` maps a
    pattern id to the comtypes interface `QueryInterface` needs; without it the pattern object
    is used as it was returned, which is how a test hands one over.
    """

    def __init__(self, raw, walker, interfaces=None, max_width=MAX_WIDTH):
        self.raw = raw
        self.walker = walker
        self.interfaces = interfaces or {}
        self.max_width = max_width

    def name(self):
        try:
            return self.raw.CurrentName or ""
        except Exception:
            return ""

    def control_type(self):
        try:
            return int(self.raw.CurrentControlType)
        except Exception:
            return 0

    def rect(self):
        try:
            box = self.raw.CurrentBoundingRectangle
        except Exception:
            return None
        if box is None:
            return None
        left, top = float(box.left), float(box.top)
        if hasattr(box, "right"):
            return left, top, float(box.right) - left, float(box.bottom) - top
        return left, top, float(box.width), float(box.height)

    def focused(self):
        try:
            return bool(self.raw.CurrentHasKeyboardFocus)
        except Exception:
            return False

    def runtime_id(self):
        try:
            value = self.raw.GetRuntimeId()
        except Exception:
            return None
        if not value:
            return None
        return tuple(int(part) for part in value)

    def _pattern(self, pattern_id):
        try:
            unknown = self.raw.GetCurrentPattern(pattern_id)
        except Exception:
            return None
        if not unknown:
            return None
        interface = self.interfaces.get(pattern_id)
        query = getattr(unknown, "QueryInterface", None)
        if interface is None or query is None:
            return unknown
        try:
            return query(interface)
        except Exception:
            return None

    def value(self):
        pattern = self._pattern(VALUE)
        try:
            return (pattern.CurrentValue or "") if pattern is not None else ""
        except Exception:
            return ""

    def has_invoke(self):
        return self._pattern(INVOKE) is not None

    def enabled(self):
        try:
            return bool(self.raw.CurrentIsEnabled)
        except Exception:
            return None

    def pattern_ids(self):
        return [pattern_id for pattern_id in Walker._pattern_table().values() if self._pattern(pattern_id) is not None]

    def checked(self):
        pattern = self._pattern(Walker._pattern_table()["toggle"])
        try:
            return bool(int(pattern.CurrentToggleState)) if pattern is not None else None
        except Exception:
            return None

    def expanded(self):
        pattern = self._pattern(Walker._pattern_table()["expand_collapse"])
        try:
            return int(pattern.CurrentExpandCollapseState) != 0 if pattern is not None else None
        except Exception:
            return None

    def selected(self):
        """SelectionItem.CurrentIsSelected. An element without that pattern cannot say."""
        pattern = self._pattern(Walker._pattern_table()["selection_item"])
        try:
            return bool(pattern.CurrentIsSelected) if pattern is not None else None
        except Exception:
            return None

    def default_action(self):
        pattern = self._pattern(LEGACY)
        try:
            return (pattern.CurrentDefaultAction or "") if pattern is not None else ""
        except Exception:
            return ""

    def legacy_state(self):
        """LegacyIAccessible's state bits; a missing pattern reads as read-only."""
        pattern = self._pattern(LEGACY)
        try:
            return int(pattern.CurrentState) if pattern is not None else READ_ONLY
        except Exception:
            return READ_ONLY

    def children(self):
        found = []
        try:
            child = self.walker.GetFirstChildElement(self.raw)
        except Exception:
            return []
        while child is not None and len(found) <= self.max_width:
            found.append(LiveElement(child, self.walker, self.interfaces, self.max_width))
            try:
                child = self.walker.GetNextSiblingElement(child)
            except Exception:
                break
        return found


def main() -> None:
    Walker.main()
