"""Human-operated audit and promotion CLI; never imported by the pipeline."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .audit import collapse_learning_events, read_learning_events
from .promotion import append_promotion_event


def _injected_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit staged V2 learnings")
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--learnings", type=Path, required=True)
    promote = commands.add_parser("promote")
    promote.add_argument("--learnings", type=Path, required=True)
    promote.add_argument("--promotions", type=Path, required=True)
    promote.add_argument("--learning-id", required=True)
    promote.add_argument("--actor", required=True)
    promote.add_argument("--promoted-at", type=_injected_datetime, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    events = read_learning_events(args.learnings)
    collapsed = collapse_learning_events(events)
    if args.command == "audit":
        print(json.dumps(
            {
                learning_id: {"occurrences": item.occurrences}
                for learning_id, item in sorted(collapsed.items())
            },
            sort_keys=True,
        ))
        return 0
    if args.learning_id not in collapsed:
        raise SystemExit("learning_id not found in staged log")
    event = append_promotion_event(
        args.promotions,
        learning_id=args.learning_id,
        actor=args.actor,
        promoted_at=args.promoted_at,
    )
    print(json.dumps({"learning_id": event.learning_id, "event": event.event}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
