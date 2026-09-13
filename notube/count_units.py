"""Run the daily sync and count every YouTube Data API request it makes.

    uv run python -m notube.count_units

All notube Data API calls are `.list` (1 unit each, parts do not multiply cost),
so the request count IS the quota cost of a run.
"""
import atexit
import collections
import runpy

import googleapiclient.http as H

counts = collections.Counter()
_orig = H.HttpRequest.execute


def _patched(self, *a, **kw):
    uri = self.uri
    if "/youtube/v3/" in uri:
        counts[uri.split("/youtube/v3/")[1].split("?")[0]] += 1
    return _orig(self, *a, **kw)


def main() -> None:
    """Patch the Data API transport, run the daily sync in-process, print the counts at exit."""
    H.HttpRequest.execute = _patched
    atexit.register(lambda: print("\nAPI_UNITS_TOTAL=" + str(sum(counts.values())),
                                  "\nAPI_UNITS_BY_ENDPOINT=" + repr(dict(counts)), flush=True))
    runpy.run_module("notube.daily", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
