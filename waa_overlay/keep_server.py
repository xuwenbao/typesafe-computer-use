"""Keep the arena server listening, without a window close_all can close.

close_all closes every window except one fixed PowerShell title. A server started in an
ordinary console dies with that window, and the next task's probe then hangs. This process
has no window. When port 5000 is down it starts the server the same way, and close_all has
nothing to close.
"""

import socket
import subprocess
import time

PYTHON = r"C:\Users\Docker\AppData\Local\Programs\Python\Python310\python.exe"
SCRIPT = r"\\host.lan\Data\server\main.py"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def up() -> bool:
    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect(("127.0.0.1", 5000))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def main() -> None:
    while True:
        if not up():
            subprocess.Popen([PYTHON, SCRIPT, "--port", "5000"], creationflags=NO_WINDOW)
            time.sleep(8)
        time.sleep(2)


if __name__ == "__main__":
    main()
