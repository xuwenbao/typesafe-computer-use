"""The suite refuses docker and the WAA VM without any stub in the test itself."""

import socket
import subprocess

import pytest


def test_docker_and_the_waa_vm_are_refused():
    with pytest.raises(RuntimeError, match="real machine"):
        subprocess.Popen(["docker", "ps"])
    with pytest.raises(RuntimeError, match=r"20\.20\.20\.21"):
        socket.socket().connect(("20.20.20.21", 5000))
