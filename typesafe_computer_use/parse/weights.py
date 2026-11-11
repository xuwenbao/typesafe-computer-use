"""The model files the desktop perception reads, pinned to a commit and checked on first use.

Every file comes from the Hugging Face hub at a fixed commit, into the hub's own cache, so a moved
branch or a closed pull request cannot change what jev loads. The two weight files are checked
against their SHA-256 once per process. `HF_HUB_OFFLINE=1` keeps every call off the network; a
file that is not in the cache then fails with the command that fetches it.

`icon_detect_v3` is OmniParser's YOLOv9-E detector, MIT licensed. The earlier `icon_detect` is
Ultralytics-based and AGPL, and jev never reads it. The captioner is OmniParser's fine-tune of
Florence-2, MIT licensed. Its weights are laid out for Microsoft's remote code, which current
transformers cannot run; jev loads them into transformers' own Florence-2 class instead, with the
config and processor (tokenizer and image preprocessing) of `florence-community/Florence-2-base-ft`,
an MIT-licensed conversion of Florence-2-base-ft to the layout that class reads.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.errors import LocalEntryNotFoundError

FETCH_COMMAND = "clicker-models fetch"
CHUNK = 1 << 20


class WeightsError(RuntimeError):
    """A model file that is missing or is not the file jev pinned."""


@dataclass(frozen=True)
class Weights:
    """Files of one hub repository at one commit, and the SHA-256 of those worth checking."""

    repo: str
    revision: str
    files: tuple[str, ...]
    sha256: dict[str, str] = field(default_factory=dict)


OMNIPARSER_REVISION = "f55d0750e5b94db2125ef0b45b0fa4a85ddc59b4"

DETECTOR = Weights(
    "microsoft/OmniParser-v2.0",
    OMNIPARSER_REVISION,
    ("icon_detect_v3/model.pt",),
    {"icon_detect_v3/model.pt": "11c6cbb77f22569fab22d86c76407a83ec81ab89dbfe28279854822d6e3fb00c"},
)
CAPTIONER = Weights(
    "microsoft/OmniParser-v2.0",
    OMNIPARSER_REVISION,
    ("icon_caption/model.safetensors",),
    {"icon_caption/model.safetensors": "01b934b0fe2d07b181e2d07752f16ae27c9d0ea88ddffe13a9a003aa9680f233"},
)
FLORENCE = Weights(
    "florence-community/Florence-2-base-ft",
    "0b03b6f15a4a211370fb204aee4e7dd48887ea37",
    (
        "config.json",
        "preprocessor_config.json",
        "processor_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
        "merges.txt",
        "added_tokens.json",
        "special_tokens_map.json",
    ),
)

ALL = (DETECTOR, CAPTIONER, FLORENCE)


def path(weights: Weights, filename: str) -> Path:
    """The local path of one pinned file, downloading it into the hub cache if it is not there yet."""
    try:
        local = Path(hf_hub_download(weights.repo, filename, revision=weights.revision))
    except LocalEntryNotFoundError as exc:
        raise WeightsError(
            f"{weights.repo}/{filename} is not in the local cache and the hub cannot be reached; "
            f"run `{FETCH_COMMAND}` once while online"
        ) from exc
    expected = weights.sha256.get(filename)
    if expected is not None:
        verify(local, expected, f"{weights.repo}/{filename}")
    return local


def folder(weights: Weights, subfolder: str = "") -> Path:
    """Every file of `weights`, fetched, and the local folder that holds them (or their `subfolder`)."""
    paths = [path(weights, f) for f in weights.files]
    root = paths[0].parents[len(Path(weights.files[0]).parts) - 1]
    return root / subfolder if subfolder else root


@functools.cache
def verify(local: Path, expected: str, name: str) -> None:
    """Raise unless the file's SHA-256 is `expected`. Checked once per process per file."""
    digest = hashlib.sha256()
    with local.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise WeightsError(f"{name} at {local} has SHA-256 {digest.hexdigest()}, expected {expected}; delete it and fetch again")


def fetch_all() -> list[Path]:
    """Every pinned file, downloaded and checked: what `clicker-models fetch` does."""
    return [path(weights, f) for weights in ALL for f in weights.files]


def versions() -> dict[str, str]:
    """Each repository and the commit jev reads it at, for a run's record."""
    return {weights.repo: weights.revision for weights in ALL}


def record() -> dict[str, dict]:
    """Each repository's commit and the SHA-256 of its checked files: what a run's record names."""
    out: dict[str, dict] = {}
    for weights in ALL:
        entry = out.setdefault(weights.repo, {"revision": weights.revision, "sha256": {}})
        entry["sha256"].update(weights.sha256)
    return out


def main(argv: list[str] | None = None) -> int:
    """`clicker-models fetch`: download and check every model file, to run offline afterwards."""
    parser = argparse.ArgumentParser(prog="clicker-models", description="The desktop perception's model files.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="download every pinned model file into the Hugging Face cache and check it")
    sub.add_parser("versions", help="print each model repository and the commit jev reads it at")
    args = parser.parse_args(argv)
    if args.command == "versions":
        for repo, revision in versions().items():
            print(f"{repo} {revision}")
        return 0
    try:
        for local in fetch_all():
            print(local)
    except WeightsError as exc:
        print(f"clicker-models: {exc}", file=sys.stderr)
        return 1
    return 0
