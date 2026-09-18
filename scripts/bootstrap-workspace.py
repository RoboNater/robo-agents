#!/usr/bin/env python3
"""Create an independent clone once; never reset or remove an existing workspace."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

from agent_hub_common.workspace import IDENTITY_FILE, canonical_workspace, git, read_identity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent")
    parser.add_argument("destination")
    parser.add_argument("repository")
    args = parser.parse_args()
    destination = Path(args.destination)
    coordination = Path(__file__).resolve().parents[1]
    if not destination.is_absolute() or destination != destination.resolve():
        parser.error("destination must be absolute and canonical")
    if destination == coordination or coordination in destination.parents:
        parser.error("destination must be outside the coordination checkout")
    if destination.exists():
        workspace = canonical_workspace(str(destination))
        if git(workspace, "remote", "get-url", "origin") != args.repository:
            parser.error("existing clone has a different origin; leaving it intact")
        if git(workspace, "status", "--porcelain"):
            parser.error("existing clone is dirty; leaving all work intact")
        value = read_identity(workspace, args.agent)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--", args.repository, str(destination)], check=True)
        workspace = canonical_workspace(str(destination))
        value = {
            "workspace_id": secrets.token_hex(32),
            "agent": args.agent,
            "path": str(workspace),
            "repository": args.repository,
        }
        descriptor = os.open(
            workspace / ".git" / IDENTITY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.write("\n")
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
