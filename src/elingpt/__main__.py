from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .client import BridgeClient, BridgeError
from .mcp import McpServer
from .runner import ModelClient, run


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        configure = getattr(stream, "reconfigure", None)
        if configure is not None:
            configure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="ElinGPT local agent connector")
    parser.add_argument("--connection", type=Path, help="Pairing descriptor; defaults to LOCALAPPDATA\\ElinGPT\\connection.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("mcp", help="Serve MCP tools over stdio; does not start autonomous play")
    commands.add_parser("observe", help="Print one read-only observation")
    runner = commands.add_parser("run", help="Run a bounded chat-completions agent while you watch")
    runner.add_argument("--endpoint", required=True, help="API base URL including /v1, without /chat/completions")
    runner.add_argument("--model", required=True)
    runner.add_argument("--key-env", default="OPENAI_API_KEY")
    runner.add_argument("--allow-remote", action="store_true", help="Consent to sending game observations to the model endpoint")
    runner.add_argument("--objective", required=True)
    runner.add_argument("--name", default="ElinGPT")
    runner.add_argument("--max-steps", type=int, default=50)
    runner.add_argument("--max-seconds", type=int, default=1800)
    runner.add_argument("--transcript", type=Path, help="Create a new JSONL transcript (contains game observations)")
    args = parser.parse_args(argv)
    try:
        model = (ModelClient(args.endpoint, args.model, args.key_env, args.allow_remote)
                 if args.command == "run" else None)
        with BridgeClient(args.connection) as client:
            if args.command == "mcp":
                McpServer(client).run()
            elif args.command == "observe":
                client.call("ping")
                print(json.dumps(client.call("observe"), ensure_ascii=False, indent=2))
            else:
                assert model is not None
                run(client, model, args.objective, name=args.name, max_steps=args.max_steps,
                    max_seconds=args.max_seconds, transcript=args.transcript)
        return 0
    except (BridgeError, OSError) as error:
        print(f"ElinGPT: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ElinGPT: interrupted; closing connector and releasing control.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
