"""Separate opt-in entrypoint; existing `ao-lore workspace query` stays unchanged."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .contracts import ViewerError
from .demo import create_demo
from .server import make_server
from .store import SnapshotStore


def run(home: Path, grant_id: str, *, native: bool = False) -> int:
    if native:
        from .native_store import NativeSourceStore
        store = NativeSourceStore(home, grant_id)
    else:
        store = SnapshotStore(home, grant_id)
    server = make_server(store)
    print("AO Lore local source viewer — " + store.provenance_mode, flush=True)
    print("Open: " + server.origin, flush=True)
    print("One-use code (expires in two minutes): " + server.state.launch_code, flush=True)
    print("Credentials stay in this tab's memory. Reloading requires a new viewer process.", flush=True)
    print("Press Ctrl+C to stop. No customer data is authorized by the demo.", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("Viewer stopped.", flush=True)
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AO Lore local source-viewer pilot (POSIX)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="start a disposable PUBLIC SYNTHETIC PDF/text/DOCX viewer")
    serve = commands.add_parser("serve", help="open one separately approved retained snapshot")
    serve.add_argument("--grant-id", required=True, help="identifier only; no source paths or URLs")
    native = commands.add_parser("serve-native", help="open native AO Lore retained evidence")
    native.add_argument("--grant-id", required=True, help="identifier only; no source paths or URLs")
    args = parser.parse_args(argv)
    try:
        home = Path(os.environ.get("AO_LORE_HOME", ".ao-lore"))
        if args.command == "demo":
            # New synthetic state only. Never inspect existing customer workspaces.
            # tempfile cleanup removes this owned child, not AO_LORE_HOME or any sibling.
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            from .store import SafeRoot
            SafeRoot(home)
            with tempfile.TemporaryDirectory(prefix="source-viewer-demo-", dir=home) as temporary:
                root = Path(temporary)
                return run(root, create_demo(root))
        return run(home, args.grant_id, native=args.command == "serve-native")
    except (ViewerError, OSError, ValueError):
        print("ao-lore: source viewer operation rejected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
