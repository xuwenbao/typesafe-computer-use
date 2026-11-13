"""UI Automation control types in the AX role vocabulary, with no Windows imports.

`windows.py` and the script that walks a WAA VM's tree both read this table, so a control is the
same role on the maintainer's machine and in the benchmark. The names are what the `uiautomation`
package reports (`ButtonControl`); the ids are UI Automation's own, which is what a VM walk reads.
"""

from __future__ import annotations

# (ControlTypeName, UIA control type id, AX role)
_TYPES = (
    ("ButtonControl", 50000, "AXButton"),
    ("CheckBoxControl", 50002, "AXCheckBox"),
    ("ComboBoxControl", 50003, "AXComboBox"),
    ("DataItemControl", 50029, "AXCell"),
    ("EditControl", 50004, "AXTextField"),
    ("GroupControl", 50026, "AXGroup"),
    ("HyperlinkControl", 50005, "AXLink"),
    ("ImageControl", 50006, "AXImage"),
    ("ListItemControl", 50007, "AXRow"),
    ("MenuItemControl", 50011, "AXMenuBarItem"),
    ("RadioButtonControl", 50013, "AXRadioButton"),
    ("SliderControl", 50015, "AXSlider"),
    ("SpinnerControl", 50016, "AXIncrementor"),
    ("TabItemControl", 50019, "AXTab"),
    ("TextControl", 50020, "AXStaticText"),
)

CONTROL_TYPE_TO_ROLE = {name: role for name, _, role in _TYPES}
CONTROL_TYPE_ID_TO_ROLE = {type_id: role for _, type_id, role in _TYPES}

DOCUMENT_ID = 50030  # UIA_DocumentControlTypeId: Notepad's editor, and a browser's page
STATE_READ_ONLY = 0x40  # STATE_SYSTEM_READONLY, in LegacyIAccessible's state


def role_for(control_type_name: str) -> str:
    """The AX role a UI Automation control type stands for, or "" for one the walk ignores."""
    return CONTROL_TYPE_TO_ROLE.get(control_type_name, "")


def role_for_id(control_type_id: int) -> str:
    """The same role, from UI Automation's numeric control type."""
    return CONTROL_TYPE_ID_TO_ROLE.get(control_type_id, "")


def document_role(legacy_state: int) -> str:
    """A document you can type into is a text area. A read-only one, like a web page, is ignored."""
    return "" if legacy_state & STATE_READ_ONLY else "AXTextArea"


def pressable(has_invoke: bool, default_action: str) -> bool:
    """Whether an element accepts a press. Almost every element carries the LegacyIAccessible
    pattern, so that pattern alone says nothing; only a named default action makes it a control."""
    return has_invoke or bool(default_action.strip())
