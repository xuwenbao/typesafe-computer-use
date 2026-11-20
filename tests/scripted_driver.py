"""A model that answers the driver from a script, so a test never calls an endpoint."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall

TOOL_OF = {
    "click": "click",
    "double_click": "double_click",
    "right_click": "right_click",
    "type": "type_text",
    "key": "press_key",
    "shortcut": "shortcut",
    "scroll_down": "scroll_down",
    "scroll_up": "scroll_up",
    "drag": "drag",
    "set_value": "set_value",
    "focus_offscreen": "focus_offscreen",
    "wait": "wait",
    "done": "done",
    "give_up": "give_up",
    "run": "press_key",
    "start": "press_key",
    "read_text": "read_text",
    "count": "count",
    "calculate": "calculate",
}


def _packet(items) -> dict | None:
    """The last user message that carries a screen, if this request has one."""
    if isinstance(items, str):
        return None
    found = None
    for item in items:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "input_text")
        for text in texts:
            try:
                packet = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(packet, dict) and packet.get("kind") == "screen":
                found = packet
    return found


class ScriptedModel(Model):
    """Pops one scripted reply per model call. A list is several tool calls in that one reply."""

    def __init__(self, scripts: list, packets: list | None = None):
        self.scripts = scripts
        self.packets = packets if packets is not None else []
        self.inputs: list = []
        self.on_request = None
        self._calls = 0

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id,
        conversation_id,
        prompt,
    ) -> ModelResponse:
        self.inputs.append(input)
        packet = _packet(input)
        if packet is not None and "screen_items" in packet:
            self.packets.append(packet)
        if self.on_request is not None:
            self.on_request()
        script = self.scripts.pop(0) if self.scripts else {"action": "give_up"}
        if callable(script):
            script = script(packet or {})
        if isinstance(script, BaseException):
            raise script
        if isinstance(script, dict) and script.get("action") == "text":
            return ModelResponse(output=[], usage=Usage(input_tokens=1, output_tokens=1), response_id=None)
        calls = script if isinstance(script, list) else [script]
        output = [self._call(one, packet) for one in calls]
        self._calls += 1
        return ModelResponse(output=output, usage=Usage(input_tokens=1, output_tokens=1), response_id=None)

    def _call(self, script: dict, packet: dict | None) -> ResponseFunctionToolCall:
        script = dict(script)
        action = str(script.get("action") or "give_up")
        if action in ("run", "start") and not script.get("key"):
            script["key"] = action
        if packet and script.get("item") not in (None, "", -1):
            numbers = {it["text"]: str(it["i"]) for it in packet.get("screen_items", [])}
            by_number = {str(it["i"]): it["text"] for it in packet.get("screen_items", [])}
            raw = str(script.get("item"))
            if not str(script.get("target") or "").strip():
                if raw in numbers:
                    script["target"] = raw
                elif raw in by_number:
                    script["target"] = by_number[raw]
            if action in (
                "click",
                "double_click",
                "right_click",
                "scroll_down",
                "scroll_up",
                "drag",
                "set_value",
                "focus_offscreen",
            ):
                script["item"] = numbers.get(raw, raw)
        name = TOOL_OF[action]
        arguments = _arguments(name, script)
        self._calls += 0
        call_id = f"call_{len(self.inputs)}_{name}_{script.get('item', '')}"
        return ResponseFunctionToolCall(call_id=call_id, name=name, arguments=json.dumps(arguments), type="function_call")

    def stream_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id,
        conversation_id,
        prompt,
    ) -> AsyncIterator:
        raise NotImplementedError("the scripted driver model does not stream")


def _arguments(name: str, script: dict) -> dict:
    reason = script.get("reason") or "scripted"
    if name == "read_text":
        return {"item": int(script.get("item") or 0)}
    if name == "count":
        return {
            "word": script.get("word") or script.get("text") or "",
            "item": -1 if script.get("item") in (None, "") else int(script["item"]),
            "text": "" if script.get("item") not in (None, "") else str(script.get("text") or ""),
            "whole_word": script.get("whole_word", True),
            "case_sensitive": script.get("case_sensitive", False),
        }
    if name == "calculate":
        return {"expression": script.get("expression") or ""}
    if name == "press_key":
        return {"reason": reason, "key": script.get("key") or ""}
    if name == "shortcut":
        return {"reason": reason, "key": script.get("key") or ""}
    if name == "type_text":
        return {
            "reason": reason,
            "text": script.get("text") or "",
            "item": str(script.get("item") or ""),
            "target": script.get("target") or "",
        }
    if name == "set_value":
        return {
            "reason": reason,
            "item": str(script.get("item") or ""),
            "text": script.get("text") or "",
            "target": script.get("target") or "",
        }
    if name == "drag":
        return {
            "reason": reason,
            "item": str(script.get("item") or ""),
            "target": script.get("target") or "",
            "to": str(script.get("to") or ""),
            "dx": str(script.get("dx") or ""),
            "dy": str(script.get("dy") or ""),
        }
    if name in ("wait", "give_up"):
        return {"reason": reason}
    if name == "done":
        return {"reason": reason, "achieved": bool(script.get("achieved", True))}
    return {
        "reason": reason,
        "item": str(script.get("item") or ""),
        "target": script.get("target") or "",
        "modifier": script.get("modifier") or "",
    }
