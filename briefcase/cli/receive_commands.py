"""``briefcase receive``: run the local OTLP receiver (foreground or detached)."""

from __future__ import annotations

import os
import signal
import sys
import threading

GROUP = "connect"

CONTENT_WARNING = (
    "warning: --keep-content is on. Prompts, responses, tool input, e-mail addresses and "
    "host paths that the tools send will be written to the exporter unredacted."
)


def register(sub) -> None:
    p = sub.add_parser(
        "receive",
        help="Run a local OTLP receiver that turns coding-tool telemetry into decision records.",
        description="Zero-config OTLP/HTTP receiver for the tools set up by `briefcase connect`.",
    )
    p.add_argument("--port", type=int, default=4318)
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address (default 127.0.0.1). Anything else requires --token.")
    p.add_argument("--to", default=None, metavar="console|PATH.jsonl|https://...|otlp",
                   help="Where records go. Default: console, or ~/.briefcase/receiver/records.jsonl with --detach.")
    p.add_argument("--keep-content", action="store_true",
                   help="Do not redact: keep prompts, responses, tool input, e-mail, host paths.")
    p.add_argument("--metrics", action="store_true", help="Also emit OTLP metric data points as records.")
    p.add_argument("--token", default=None,
                   help="Require `Authorization: Bearer <token>` (or set BRIEFCASE_RECEIVER_TOKEN).")
    p.add_argument("--max-body-mb", type=float, default=8.0, help="Request body cap in MiB (default 8).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--detach", action="store_true", help="Run in the background.")
    mode.add_argument("--stop", action="store_true", help="Stop the background receiver.")
    mode.add_argument("--status", action="store_true", help="Show whether the background receiver is up.")
    p.set_defaults(func=cmd_receive)


def _child_args(args) -> list:
    out = ["--port", str(args.port), "--host", args.host, "--to", args.to,
           "--max-body-mb", str(args.max_body_mb)]
    if args.keep_content:
        out.append("--keep-content")
    if args.metrics:
        out.append("--metrics")
    return out


def start_detached(args, out=print) -> int:
    from briefcase.receiver import daemon

    if args.to is None or args.to == "console":
        args.to = str(daemon.default_sink())
    try:
        info = daemon.detach(_child_args(args), args.host, args.port, args.to, args.token)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out(f"receiver started (pid {info['pid']}) on {info['endpoint']}")
    out(f"  records: {args.to}")
    out(f"  log:     {daemon.log_path()}")
    out("  stop:    briefcase receive --stop")
    return 0


def cmd_receive(args, store=None, engine=None, stack=None) -> int:
    from briefcase.receiver import daemon, otlp
    from briefcase.receiver.records import Redactor, load_salt
    from briefcase.receiver.server import Receiver, is_loopback, make_exporter, make_server

    if args.status:
        state = daemon.status()
        if not state.get("running"):
            print("receiver: not running (start it with: briefcase receive --detach)")
            return 1
        health = state["health"]
        print(f"receiver: running (pid {state['pid']}) on {state['endpoint']}")
        print(f"  records:   {state.get('to')}")
        print(f"  redaction: {health.get('redaction')}  protobuf: {'yes' if health.get('protobuf') else 'no'}")
        stats = health.get("stats", {})
        print(f"  requests {stats.get('requests', 0)}, records {stats.get('records', 0)}, "
              f"rejected {stats.get('rejected', 0)}")
        return 0
    if args.stop:
        info = daemon.stop()
        print(f"receiver stopped (pid {info['pid']})" if info else "receiver: not running")
        return 0

    args.token = args.token or os.environ.get(daemon.TOKEN_ENV) or None
    if not is_loopback(args.host) and not args.token:
        print(f"error: refusing to listen on {args.host} without --token (or {daemon.TOKEN_ENV}). "
              "Telemetry would be accepted from anyone who can reach this port.", file=sys.stderr)
        return 2
    if args.keep_content:
        print(CONTENT_WARNING, file=sys.stderr)
    if args.detach:
        return start_detached(args)

    try:
        exporter = make_exporter(args.to)
    except (ValueError, ImportError) as exc:
        print(f"error: --to {args.to}: {exc}", file=sys.stderr)
        return 2
    receiver = Receiver(
        exporter, keep_content=args.keep_content, emit_metrics=args.metrics, token=args.token,
        max_body_bytes=int(args.max_body_mb * 1024 * 1024),
        redactor=Redactor(keep_content=args.keep_content, salt=load_salt(daemon.salt_path())),
    )
    try:
        server = make_server(receiver, args.host, args.port)
    except (OSError, ValueError) as exc:
        print(f"error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    host, port = str(server.server_address[0]), server.server_address[1]
    print(f"briefcase receiver listening on http://{host}:{port} -> {args.to or 'console'}", file=sys.stderr)
    redaction = ("OFF (--keep-content)" if args.keep_content
                 else "on (tool parameters, commands, paths, URLs and error text are dropped)")
    print(f"  redaction: {redaction}; "
          f"protobuf: {'yes' if otlp.protobuf_available() else 'no'}", file=sys.stderr)
    if not otlp.protobuf_available():
        print("  OTLP/JSON only. For tools that send protobuf (Codex, Copilot): "
              "pip install 'briefcase-ai[receiver]'", file=sys.stderr)

    def _stop(signum, frame):  # pragma: no cover - signal path
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT"):
        try:
            signal.signal(getattr(signal, name), _stop)
        except (ValueError, OSError, AttributeError):  # not the main thread / unsupported
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
        receiver.close()
    return 0
