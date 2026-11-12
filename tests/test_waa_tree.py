"""The WAA tree walk and its reading, driven by stand-ins. Nothing here reaches a VM."""

import itertools
import json
import time
from pathlib import Path

from typesafe_computer_use import uia_roles, windows
from typesafe_computer_use.models import Field
from typesafe_computer_use.waa import tree
from typesafe_computer_use.waa.walk_vm import INVOKE, LEGACY, VALUE, LiveElement, empty, walk

FIXTURES = Path(__file__).parent / "fixtures" / "waa"
DISPLAY = (1440.0, 900.0)


class Node:
    """One element of a tree the walk can be pointed at."""

    def __init__(self, name, control_type, rect, children=(), focused=False, value="", invoke=False, action=""):
        self._name = name
        self._control_type = control_type
        self._rect = rect
        self._children = list(children)
        self._focused = focused
        self._value = value
        self._invoke = invoke
        self._action = action

    def name(self):
        return self._name

    def control_type(self):
        return self._control_type

    def rect(self):
        return self._rect

    def focused(self):
        return self._focused

    def value(self):
        return self._value

    def has_invoke(self):
        return self._invoke

    def default_action(self):
        return self._action

    def children(self):
        return list(self._children)


def test_a_button_comes_back_as_a_button():
    window = Node("Edge", 50032, (0, 0, 800, 600), [Node("保存", 50000, (10, 10, 40, 20), invoke=True)])
    got = walk(window, clock=lambda: 0)
    assert got["nodes"] == [
        {
            "role": "AXButton",
            "name": "保存",
            "rect": [10, 10, 40, 20],
            "focused": False,
            "value": "",
            "pressable": True,
            "children": [],
        }
    ]
    assert got["truncated"] is False
    assert got["window"] == {"name": "Edge", "x": 0, "y": 0, "w": 800, "h": 600}


def test_a_wide_node_is_truncated_at_the_width_limit():
    kids = [Node(str(i), 50020, (0, i, 10, 10)) for i in range(300)]
    got = walk(Node("w", 50032, (0, 0, 100, 100), kids), clock=lambda: 0, max_width=256)
    assert len(got["nodes"]) == 256
    assert got["truncated"] is True
    assert got["reason"] == "truncated"


def test_a_slow_walk_stops_at_the_deadline():
    ticks = iter([0, 100])

    def clock():
        return next(ticks, 100)

    kids = [Node("a", 50000, (0, 0, 10, 10), invoke=True), Node("b", 50000, (0, 20, 10, 10), invoke=True)]
    got = walk(Node("w", 50032, (0, 0, 100, 100), kids), clock=clock, seconds=8)
    assert got["nodes"] == []
    assert got["truncated"] is True


def test_no_foreground_window_is_an_empty_tree():
    got = empty("no foreground window")
    assert got["nodes"] == []
    assert got["window"] is None
    assert got["reason"] == "no foreground window"
    assert walk(None, clock=lambda: 0) == got


class _Box:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Raw:
    """The attributes a comtypes UI Automation element exposes, and nothing else."""

    def __init__(self, name, control_type, box, patterns=None, focus=False):
        self.CurrentName = name
        self.CurrentControlType = control_type
        self.CurrentBoundingRectangle = box
        self.CurrentHasKeyboardFocus = focus
        self.patterns = patterns or {}

    def GetCurrentPattern(self, pattern_id):
        if pattern_id not in self.patterns:
            raise OSError("unsupported pattern")
        return self.patterns[pattern_id]


class _Pattern:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class _Walker:
    def __init__(self, children):
        self.children = children
        self._siblings = {}

    def GetFirstChildElement(self, raw):
        kids = self.children.get(id(raw), [])
        for earlier, later in itertools.pairwise(kids):
            self._siblings[id(earlier)] = later
        return kids[0] if kids else None

    def GetNextSiblingElement(self, raw):
        return self._siblings.get(id(raw))


def test_a_comtypes_stand_in_is_walked_the_same_way():
    button = _Raw("保存", 50000, _Box(10, 10, 50, 30), {INVOKE: _Pattern()})
    address = _Raw("Address and search bar", 50004, _Box(80, 10, 400, 34), {VALUE: _Pattern(CurrentValue="example.com/a")})
    name = _Raw("Name", 50004, _Box(10, 80, 210, 104), {VALUE: _Pattern(CurrentValue="abc")}, focus=True)
    window = _Raw("Edge", 50032, _Box(0, 0, 800, 600))
    root = LiveElement(window, _Walker({id(window): [button, address, name]}))
    got = walk(root, clock=lambda: 0)
    assert [node["name"] for node in got["nodes"]] == ["保存", "Address and search bar", "Name"]
    assert got["nodes"][0]["pressable"] is True
    assert got["nodes"][1]["value"] == "example.com/a"
    assert got["nodes"][2]["focused"] is True
    assert LEGACY  # the legacy pattern id the live walk asks for


def test_the_script_embeds_the_one_role_table_and_fits_a_windows_command():
    source = tree.script()
    assert (tree.PACKAGE / "uia_roles.py").read_text(encoding="utf-8") in source
    assert windows.CONTROL_TYPE_TO_ROLE == uia_roles.CONTROL_TYPE_TO_ROLE
    assert windows.pressable is uia_roles.pressable
    for name, role in uia_roles.CONTROL_TYPE_TO_ROLE.items():
        type_id = next(type_id for type_name, type_id, _ in uia_roles._TYPES if type_name == name)
        assert uia_roles.role_for_id(type_id) == role
    assert len(source.encode("utf-8")) < tree.COMMAND_LIMIT


def test_a_fetch_returns_the_trees_json():
    raw = json.dumps({"app": "", "pid": 0, "window": None, "nodes": [], "truncated": False, "reason": None})

    def run(script, timeout):
        assert "main()" in script
        assert timeout == tree.WALK_SECONDS + tree.GRACE_SECONDS
        return 0, raw

    fetch = tree.Pending(run).result()
    assert fetch.raw["nodes"] == []
    assert fetch.reason is None
    assert fetch.nodes == 0


def test_a_nonzero_exit_is_a_miss():
    fetch = tree.Pending(lambda script, timeout: (1, "COMError: no type library\n"), timeout=2).result()
    assert fetch.raw is None
    assert fetch.reason == "COMError: no type library"


def test_invalid_json_is_a_miss():
    fetch = tree.Pending(lambda script, timeout: (0, "not json"), timeout=2).result()
    assert fetch.raw is None
    assert fetch.reason == "invalid JSON"


def test_a_fetch_that_does_not_return_times_out():
    def run(script, timeout):
        time.sleep(0.2)
        return 0, "{}"

    fetch = tree.Pending(run, timeout=0.05).result()
    assert fetch.raw is None
    assert fetch.reason == "timed out"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_a_saved_edge_tree_reads_the_button_the_address_and_the_focus():
    reading = tree.read(_load("edge-address.json"), *DISPLAY)
    assert reading.app == "Microsoft Edge"
    assert reading.pid == 4212
    assert reading.window == (0.0, 0.0, 1440.0, 900.0)
    assert [(node.role, node.label) for node in reading.found if node.label == "保存"] == [("AXButton", "保存")]
    assert reading.url == "example.com/a"
    assert reading.focused == Field("AXTextField", "Name", "", "abc", 10.0, 80.0, 200.0, 24.0)


def test_a_truncated_tree_keeps_the_button_and_says_so():
    raw = _load("truncated.json")
    reading = tree.read(raw, *DISPLAY)
    assert reading.app == "Google Chrome"
    assert [node.label for node in reading.found] == ["保存"]
    assert reading.capped is True
    fetch = tree.Fetch(raw, raw["reason"], 1.5, 1, True)
    assert tree.summary([fetch])["tree_truncated"] == 1
    assert tree.summary([fetch])["tree_nodes"] == {"mean": 1.0, "max": 1.0}


def test_a_failed_fetch_reads_as_nothing_and_is_counted(tmp_path):
    fetch = tree.Fetch(None, "timed out", 10.0, 0, False)
    reading = tree.read(None, *DISPLAY)
    assert reading.found == [] and reading.url is None and reading.focused is None
    assert tree.summary([fetch])["tree_failures"] == 1
    assert tree.summary([fetch])["tree_failure_reasons"] == ["timed out"]
    tree.save(tmp_path, 0, None)
    tree.save(tmp_path, 0, {"nodes": []})
    tree.save(tmp_path, 1, {"nodes": []})
    assert sorted(path.name for path in tmp_path.iterdir()) == ["obs-000-uia.json", "obs-001-uia.json"]
