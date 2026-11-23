"""Run the briefcase MCP server: ``briefcase-ai-mcp`` or ``python -m briefcase.mcp``.

``briefcase-ai-mcp --check`` exits 0 if this environment can serve, else 1 with one line saying why."""
import sys


def main() -> None:
    try:
        from briefcase.mcp.server import main as serve
    except ImportError as exc:
        print(f"briefcase-ai-mcp: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if "--check" in sys.argv[1:]:
        print("briefcase-ai-mcp: ok")
        raise SystemExit(0)
    serve()


if __name__ == "__main__":
    main()
