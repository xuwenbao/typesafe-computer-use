# PyInstaller spec for jev.exe, built by scripts/build-helper.ps1 on Windows.
#
# One folder, with a console: `jev mcp` speaks MCP on stdio, and a console program is what an
# agent's pipe can attach to. `jev mcp` starts `jev helper` detached, so that process has no
# window of its own. `jev mcp` drives the screen in this process. The perception's weights are
# not frozen into this folder: the installer places them in `models` beside `jev.exe`, and
# jev_entry.py points the hub cache there.

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

datas, binaries, hiddenimports = [], [], []
for package in ("rapidocr", "uiautomation", "typesafe_sdk", "fastmcp"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports
# transformers and FastMCP check their dependencies' versions through their metadata when they load.
for dist in ("typesafe-computer-use", "transformers", "torch", "torchvision", "fastmcp", "fastmcp-slim", "mcp"):
    datas += copy_metadata(dist, recursive=True)
hiddenimports += collect_submodules("typesafe_computer_use")
hiddenimports += ["win32timezone"]

a = Analysis(
    ["jev_entry.py"],
    pathex=["../.."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jev",
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="jev", upx=False)
