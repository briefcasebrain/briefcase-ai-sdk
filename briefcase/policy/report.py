"""Post one policy decision to the receiver. Run as a detached child of the hook.

Reads ``{"endpoint", "path", "token", "event"}`` as JSON on stdin, posts
``event`` once with a short timeout, and exits 0 whatever happens. It prints
nothing: its stdout and stderr are ``/dev/null``.

Run by file path with ``python -I -S``, so it must not import ``briefcase``.
"""

import json
import urllib.request
import sys


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to another origin: never follow one."""

    def redirect_request(self, *args, **kwargs):
        return None


def main():
    try:
        job = json.loads(sys.stdin.buffer.read(1024 * 1024).decode("utf-8", "replace"))
        import urllib.request

        request = urllib.request.Request(
            str(job["endpoint"]).rstrip("/") + str(job["path"]),
            data=json.dumps(job["event"], default=str).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        if job.get("token"):
            request.add_header("Authorization", "Bearer " + str(job["token"]))
        # No proxy: the receiver is normally local, and a proxy from the
        # environment must not see the event.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=2.0) as response:
            response.read(1024)
    except BaseException:  # noqa: B036 - fire and forget
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
