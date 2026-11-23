"""Run the briefcase MCP server: ``briefcase-ai-mcp`` or ``python -m briefcase.mcp``."""
import sys


def main() -> None:
    try:
        from briefcase.mcp.server import main as serve
    except ImportError as exc:
        print(f"briefcase-ai-mcp: {exc}", file=sys.stderr)
        raise SystemExit(1)
    serve()


if __name__ == "__main__":
    main()
