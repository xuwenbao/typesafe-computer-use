"""A Windows child session: jev runs there, and the console session only hosts the window.

The controller enables the child session, shows it in a Remote Desktop window, and talks to the
jev process over a named pipe. It does not import the platform adapter and it never sends input.
Nothing here falls back to driving the console desktop.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .models import Abort

KINDS = ("task", "log", "progress", "question", "answer", "cancel", "done")
PIPE_NAME = "jev-session"

_cancelled = False


@dataclass(frozen=True)
class SessionFacts:
    """What has to be true before a child session is worth opening. All of it is asked up front."""

    edition: str
    rdp_localhost: bool
    smart_card: bool
    child_running: bool


def rejection(facts: SessionFacts) -> str | None:
    """Why this machine cannot host the session, or None when it can.

    Home editions have no Remote Desktop host. A smart-card logon will not auto-logon into the
    child session. One child session at a time. A machine that cannot accept a localhost Remote
    Desktop connection cannot show the window.
    """
    edition = facts.edition.strip().lower()
    if "home" in edition or edition == "core" or edition.startswith("core"):
        return "Windows Home cannot host a child session"
    if facts.smart_card:
        return "a smart card logon cannot open a child session"
    if facts.child_running:
        return "a child session is already running"
    if not facts.rdp_localhost:
        return "remote desktop cannot connect to localhost"
    return None


def should_refuse_session(jev_session: bool, in_console: bool) -> bool:
    """A jev that was started for the child session must not keep running on the console desktop."""
    return bool(jev_session) and bool(in_console)


def encode(kind: str, **payload) -> str:
    """One session message as a single line of JSON."""
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a session message")
    return json.dumps({"type": kind, **payload}, ensure_ascii=False) + "\n"


def decode(line: str) -> dict:
    """The message in one line. Anything else is refused, not guessed."""
    data = json.loads(line)
    if not isinstance(data, dict) or data.get("type") not in KINDS:
        raise ValueError(f"not a session message: {line!r}")
    return data


def session_cancelled() -> bool:
    """Whether the controller has told this process to stop."""
    return _cancelled


def note_cancel() -> None:
    """Record a cancel so `check_abort` takes the existing abort path."""
    global _cancelled
    _cancelled = True


def reset_cancel() -> None:
    """Test hook. A real process starts with no cancel."""
    global _cancelled
    _cancelled = False


class SessionChannel:
    """The child's view of the pipe: ask a question, or notice a cancel.

    `send` and `recv` move one line each. A queue pair stands in for the pipe in tests.
    """

    def __init__(self, send: Callable[[str], None], recv: Callable[[], str]):
        self._send = send
        self._recv = recv

    def ask(self, question: str) -> str:
        """Write a question and wait. A cancel raises Abort and arms `check_abort`."""
        self._send(encode("question", text=question))
        message = decode(self._recv())
        if message["type"] == "cancel":
            note_cancel()
            raise Abort("the session was cancelled")
        if message["type"] != "answer":
            raise ValueError(f"expected an answer, got {message['type']}")
        return str(message.get("text") or "")

    def cancel(self) -> None:
        """The controller tells the child to stop."""
        self._send(encode("cancel"))


def refuse_if_console() -> None:
    """Exit before any screenshot or input when this jev is still on the console session."""
    if should_refuse_session(True, in_console_session()):
        sys.exit("clicker session is running in the console session")


def run_controller(argv: list[str]) -> None:
    """Open the child session and relay its pipe until the run is done.

    A failed precondition exits with the reason. A failed launch exits too. Neither starts jev
    on the console desktop.
    """
    if not argv:
        sys.exit("clicker session needs the goal")
    reason = rejection(collect_facts())
    if reason:
        sys.exit(reason)
    pipe = create_session_pipe(PIPE_NAME)
    try:
        enable_child_session()
        connect_child_session()
        launch_in_session(PIPE_NAME)
        _relay(pipe, " ".join(argv))
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"the child session did not start: {e}")


def child_startup() -> tuple[str, Callable[[str], str], Callable[[str, str], None]]:
    """The jev inside the session: the goal, an ask that uses the pipe, and a done callback.

    A background read records cancel, so `check_abort` sees it even between questions.
    """
    pipe_name = os.environ.get("JEV_SESSION_PIPE") or PIPE_NAME
    client = attach_session_pipe(pipe_name)
    incoming: queue.Queue[dict] = queue.Queue()

    def pump() -> None:
        while True:
            message = decode(_read_line(client))
            if message["type"] == "cancel":
                note_cancel()
            incoming.put(message)

    threading.Thread(target=pump, name="jev-session", daemon=True).start()
    first = incoming.get()
    if first["type"] != "task":
        sys.exit("the session did not send a task")

    def ask(question: str) -> str:
        _write_line(client, encode("question", text=question))
        message = incoming.get()
        if message["type"] == "cancel" or session_cancelled():
            note_cancel()
            raise Abort("the session was cancelled")
        return str(message.get("text") or "")

    def finish(outcome: str, folder: str) -> None:
        _write_line(client, encode("done", text=outcome, folder=folder))

    return str(first.get("goal") or ""), ask, finish


def collect_facts() -> SessionFacts:
    """Edition, localhost Remote Desktop, smart-card logon, and an existing child session."""
    return SessionFacts(_edition(), _rdp_localhost(), _smart_card_logon(), _child_running())


def in_console_session() -> bool:
    """Whether this process's session is the active console session."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(session)):
        raise OSError(ctypes.get_last_error(), "ProcessIdToSessionId failed")
    return session.value == kernel.WTSGetActiveConsoleSessionId()


def enable_child_session() -> None:
    """Allow one child session on this logon. The call comes from the parent session."""
    import ctypes

    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    if not wts.WTSEnableChildSessions(True):
        raise OSError(ctypes.get_last_error(), "WTSEnableChildSessions failed")


def connect_child_session():
    """Show the child session in a Remote Desktop window. No console-desktop fallback.

    `ConnectToChildSession` is set before `Connect`, which is what makes the control attach to
    the child session instead of opening a normal remote machine.
    """
    import win32com.client

    client = win32com.client.Dispatch("MsRdpClient9NotSafeForScripting")
    client.Server = "localhost"
    client.AdvancedSettings9.EnableCredSspSupport = True
    extended = client.GetExtendedSettings()
    extended.set_Property("ConnectToChildSession", True)
    client.Connect()
    return client


def create_session_pipe(name: str):
    """A duplex message pipe that only the current user can open, rejecting remote clients."""
    import ntsecuritycon
    import win32api
    import win32pipe
    import win32security

    user = win32security.LookupAccountName(None, win32api.GetUserName())[0]
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.Initialize()
    access = win32security.ACL()
    access.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE, user
    )
    descriptor.SetSecurityDescriptorDacl(1, access, 0)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return win32pipe.CreateNamedPipe(
        rf"\\.\pipe\{name}",
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE
        | win32pipe.PIPE_READMODE_MESSAGE
        | win32pipe.PIPE_WAIT
        | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
        1,
        65536,
        65536,
        0,
        attributes,
    )


def attach_session_pipe(name: str):
    """The child's end of the pipe the controller created."""
    import win32file

    return win32file.CreateFile(
        rf"\\.\pipe\{name}",
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )


def launch_in_session(pipe_name: str) -> None:
    """Start jev with the child session's explorer.exe token.

    The new process gets `JEV_SESSION=1` and the pipe name. A failure raises. This never starts
    jev in the console session instead.
    """
    import ctypes
    import sys
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    console = kernel.WTSGetActiveConsoleSessionId()
    pid = _explorer_pid(wts, console)
    process = kernel.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
    if not process:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed for explorer.exe")
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(process, 0x0002, ctypes.byref(token)):  # TOKEN_DUPLICATE
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    duplicated = wintypes.HANDLE()
    if not advapi.DuplicateTokenEx(token, 0x10000000, None, 2, 1, ctypes.byref(duplicated)):
        raise OSError(ctypes.get_last_error(), "DuplicateTokenEx failed")
    env = os.environ.copy()
    env["JEV_SESSION"] = "1"
    env["JEV_SESSION_PIPE"] = pipe_name
    block = "\0".join(f"{key}={value}" for key, value in env.items()) + "\0\0"
    command = f'"{sys.executable}" -m typesafe_computer_use'
    if not advapi.CreateProcessAsUserW(
        duplicated, None, command, None, None, False, 0x00000400, block, None, _startup(), ctypes.byref(_process_info())
    ):
        raise OSError(ctypes.get_last_error(), "CreateProcessAsUser failed")


def _explorer_pid(wts, console: int) -> int:
    """The explorer.exe that belongs to the child session, not the console session."""
    import ctypes
    from ctypes import wintypes

    class Process(ctypes.Structure):
        _fields_ = [
            ("SessionId", wintypes.DWORD),
            ("ProcessId", wintypes.DWORD),
            ("pProcessName", wintypes.LPWSTR),
            ("pUserSid", ctypes.c_void_p),
        ]

    listed = ctypes.POINTER(Process)()
    count = wintypes.DWORD()
    if not wts.WTSEnumerateProcessesW(0, 0, 1, ctypes.byref(listed), ctypes.byref(count)):
        raise OSError(ctypes.get_last_error(), "WTSEnumerateProcesses failed")
    try:
        for entry in listed[: count.value]:
            name = (entry.pProcessName or "").lower()
            if name == "explorer.exe" and entry.SessionId != console:
                return int(entry.ProcessId)
    finally:
        wts.WTSFreeMemory(listed)
    raise OSError("the child session has no explorer.exe")


def _startup():
    import ctypes
    from ctypes import wintypes

    class Startup(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    info = Startup()
    info.cb = ctypes.sizeof(Startup)
    info.lpDesktop = "winsta0\\default"
    return info


def _process_info():
    import ctypes
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    return Info()


def _edition() -> str:
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
        edition, _ = winreg.QueryValueEx(key, "EditionID")
    return str(edition)


def _rdp_localhost() -> bool:
    """Whether anything accepts Remote Desktop on this machine. A closed port cannot host the window."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 3389), timeout=1):
            return True
    except OSError:
        return False


def _smart_card_logon() -> bool:
    """Whether this logon used a smart card. Asking and failing counts as not a smart card."""
    try:
        import ctypes
        from ctypes import wintypes

        secur32 = ctypes.WinDLL("secur32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class Luid(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class Stats(ctypes.Structure):
            _fields_ = [
                ("TokenId", Luid),
                ("AuthenticationId", Luid),
                ("ExpirationTime", ctypes.c_int64),
                ("TokenType", wintypes.DWORD),
                ("ImpersonationLevel", wintypes.DWORD),
                ("DynamicCharged", wintypes.DWORD),
                ("DynamicAvailable", wintypes.DWORD),
                ("GroupCount", wintypes.DWORD),
                ("PrivilegeCount", wintypes.DWORD),
                ("ModifiedId", Luid),
            ]

        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return False
        stats = Stats()
        returned = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 10, ctypes.byref(stats), ctypes.sizeof(stats), ctypes.byref(returned)):
            return False

        class Data(ctypes.Structure):
            _fields_ = [
                ("Size", wintypes.ULONG),
                ("LogonId", Luid),
                ("UserName", wintypes.LPWSTR),
                ("LogonDomain", wintypes.LPWSTR),
                ("AuthenticationPackage", wintypes.LPWSTR),
                ("LogonType", wintypes.ULONG),
                ("Session", wintypes.ULONG),
                ("Sid", ctypes.c_void_p),
                ("LogonTime", ctypes.c_int64),
                ("LogonServer", wintypes.LPWSTR),
                ("DnsDomainName", wintypes.LPWSTR),
                ("Upn", wintypes.LPWSTR),
            ]

        found = ctypes.POINTER(Data)()
        if secur32.LsaGetLogonSessionData(ctypes.byref(stats.AuthenticationId), ctypes.byref(found)) != 0:
            return False
        package = (found.contents.AuthenticationPackage or "").lower()
        return "smartcard" in package or package == "cloudap"
    except Exception:
        return False


def _child_running() -> bool:
    import ctypes
    from ctypes import wintypes

    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    session = wintypes.DWORD()
    return bool(wts.WTSGetChildSessionId(0, ctypes.byref(session)))


def _relay(pipe, goal: str) -> None:
    """Send the task, then print whatever the child sends until it is done."""
    _write_line(pipe, encode("task", goal=goal))
    while True:
        message = decode(_read_line(pipe))
        kind = message["type"]
        if kind == "log":
            print(message.get("text", ""), flush=True)
        elif kind == "progress":
            print(f"  {message.get('taken')} / {message.get('budget')}", flush=True)
        elif kind == "question":
            print(message.get("text", ""), flush=True)
            try:
                reply = input("  > ")
            except EOFError:
                reply = ""
            _write_line(pipe, encode("answer", text=reply))
        elif kind == "done":
            print(message.get("text", ""), flush=True)
            return


def _write_line(pipe, line: str) -> None:
    import win32file

    win32file.WriteFile(pipe, line.encode("utf-8"))


def _read_line(pipe) -> str:
    import win32file

    _, data = win32file.ReadFile(pipe, 65536)
    return data.decode("utf-8")
