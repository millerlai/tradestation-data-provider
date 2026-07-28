"""Which ET days of a Tier-3 cache this binding has actually built.

`contract/semantics.md` §2.7. The question "has this day been built?" cannot be
answered by looking for `date=<D>/bars.parquet`, because four different writers
produce that path — the lazy resampler (only the queried window), `BarWriter`
during live ingest (the day so far, still growing), the batch aggregation tool
(its own column set), and older versions of this binding. File presence means
someone wrote something, not that the day is complete.

So the builder keeps its own record beside the partitions, and stamps each entry
with a fingerprint of the source that produced it. Comparing fingerprints is
what makes a growing session, a backfill, and a day that had no data until
yesterday all invalidate themselves — without anyone having to guess whether an
empty day was a market holiday or an ingestion outage. Those two are
indistinguishable in the data, which is why nothing here tries to tell them
apart.

The record is a cache of a cache: deleting it costs a recompute and can never
change an answer, and a reader that has never heard of it still sees the whole
store correctly.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

COVERAGE_FILENAME = "_coverage.json"

# Bumped when the on-disk shape changes. An unrecognised version is discarded
# rather than migrated: the record is derived, so throwing it away is free.
_VERSION = 1


class SourceFingerprint(NamedTuple):
    """What the source partitions for one ET day looked like when it was built.

    `(size, mtime_ns)` per file rather than a content hash: this is checked on
    every `load_bars` call, and a hash would mean reading the whole tick
    partition to answer a question the stat already answers. The gap it leaves
    — a rewrite of identical size that preserves mtime — needs
    `rebuild_bar_cache`, which is the escape hatch that exists for exactly this.

    An empty tuple is a real answer, not a missing one: it means "no source
    partition existed". Recording that is what stops a range outside the
    ingested span from re-scanning the tick tree on every call, and it stops
    being true the moment a source file appears.
    """

    files: tuple[tuple[str, int, int], ...]

    @classmethod
    def of(cls, paths: list[Path]) -> SourceFingerprint:
        stamped: list[tuple[str, int, int]] = []
        for path in sorted(paths):
            try:
                stat = path.stat()
            except OSError:
                continue  # vanished between glob and stat — treat as absent
            stamped.append((path.name, stat.st_size, stat.st_mtime_ns))
        return cls(tuple(stamped))

    def as_json(self) -> list[list[object]]:
        return [[name, size, mtime] for name, size, mtime in self.files]

    @classmethod
    def from_json(cls, raw: object) -> SourceFingerprint | None:
        if not isinstance(raw, list):
            return None
        out: list[tuple[str, int, int]] = []
        for item in raw:
            match item:
                case [str(name), int(size), int(mtime)]:
                    out.append((name, size, mtime))
                case _:
                    return None
        return cls(tuple(out))


class Entry(NamedTuple):
    """What one build of one ET day produced, and from what.

    `produced_rows` is not bookkeeping — it is what keeps the record honest
    about a file it does not own. An entry claiming rows while the day's
    partition has been deleted is self-contradictory, and trusting it is how
    `clear_bar_cache` turned a warm cache into a permanent empty answer. §2.7
    rule 4 says desyncing the record may only cost a recompute, so the record
    has to be able to notice.
    """

    source: SourceFingerprint
    produced_rows: bool


class CoverageRecord:
    """The builder's record for one `(timeframe, symbol)` cache directory."""

    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / COVERAGE_FILENAME
        self._days: dict[date, Entry] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[date, Entry]:
        if self._days is not None:
            return self._days
        self._days = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._days
        except (OSError, ValueError):
            # Unreadable or malformed. Discarding beats guessing: the worst
            # outcome is a rebuild, and trusting a half-parsed record would
            # mean claiming days were built that may not have been.
            log.warning("bar_coverage_unreadable", extra={"path": str(self._path)})
            return self._days
        if not isinstance(raw, dict) or raw.get("version") != _VERSION:
            return self._days
        days = raw.get("days")
        if not isinstance(days, dict):
            return self._days
        for key, value in days.items():
            try:
                day = date.fromisoformat(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            fingerprint = SourceFingerprint.from_json(value.get("src"))
            produced = value.get("rows")
            if fingerprint is not None and isinstance(produced, bool):
                self._days[day] = Entry(fingerprint, produced)
        return self._days

    def knows(self, day: date) -> bool:
        """Whether this builder has an entry for `day` at all.

        Distinct from `covers`: a partition with no entry was written by
        somebody else, and the caller must leave it alone rather than rebuild
        over it.
        """
        return day in self._load()

    def covers(self, day: date, current: SourceFingerprint, *, partition_exists: bool) -> bool:
        """True when this builder built `day` from this source and it is still there.

        The second half is what makes the record discardable in both
        directions. The record decides whether to build; the answer is read
        back off a partition the record does not own. Believing an entry whose
        partition has since been deleted answers 0 rows forever — which is
        precisely what the documented `clear_bar_cache` recovery produced.
        """
        entry = self._load().get(day)
        if entry is None or entry.source != current:
            return False
        return partition_exists or not entry.produced_rows

    def record(self, entries: dict[date, Entry]) -> None:
        if not entries:
            return
        days = dict(self._load())
        days.update(entries)
        self._write(days)

    def forget(self, days: list[date] | None = None) -> None:
        """Drop `days`, or every day when `days` is None.

        `rebuild_bar_cache` deletes the whole `(symbol, timeframe)` tree, not
        just the requested window, so forgetting only that window left the days
        either side deleted *and* still claimed as covered.
        """
        known = self._load()
        if days is None:
            if known:
                self._write({})
            return
        remaining = {d: e for d, e in known.items() if d not in days}
        if len(remaining) == len(known):
            return
        self._write(remaining)

    def discard(self) -> None:
        self._days = None
        self._path.unlink(missing_ok=True)

    def _write(self, days: dict[date, Entry]) -> None:
        payload = {
            "version": _VERSION,
            "days": {
                d.isoformat(): {"src": e.source.as_json(), "rows": e.produced_rows}
                for d, e in sorted(days.items())
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace so a reader never sees a half-written record and a
        # crash mid-write leaves the previous one intact. Two processes racing
        # end with one of the two complete records, which is acceptable
        # precisely because the record is discardable.
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            log.warning("bar_coverage_write_failed", extra={"path": str(self._path)})
            tmp.unlink(missing_ok=True)
            return
        self._days = days
