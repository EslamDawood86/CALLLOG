"""Admin overview figures for a date range, built from per-day summaries (fast for a whole month)."""
from . import auth, stats
from .queries import reasons_by_key


def merged(d_from, d_to):
    """Combined totals / per-employee / per-branch figures + the per-day summaries (shared for ~2 s)."""
    store, reasons = auth.store(), reasons_by_key()
    version = tuple(sorted((key, r.get("status")) for key, r in reasons.items()))

    def compute():
        rollups = [(day, store.rollup(day, version, lambda records: stats.rollup_day(records, reasons)))
                   for day in store.days_with_data(d_from, d_to)]
        return {**stats.combine(rollups), "rollups": rollups}

    return store.memo(("merged", d_from, d_to, version), compute)


def recent_calls(d_from, d_to, count=10):
    """The newest ``count`` calls in the range (only the latest day(s) are read)."""
    store, out = auth.store(), []
    for day in reversed(store.days_with_data(d_from, d_to)):
        out += sorted(reversed(store.day_all(day)), key=lambda r: r["time"], reverse=True)[:count - len(out)]
        if len(out) >= count:
            break
    return out
