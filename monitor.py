#!/usr/bin/env python3
"""United MileagePlus award monitor.

Polls the seats.aero Partner API for United award space on configured routes
and sends a notification (email / SMS / push via Apprise) when space matching
your criteria (cabin, max miles, saver fare class, seats, nonstop) appears.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import apprise
import requests
import yaml

API_BASE = "https://seats.aero/partnerapi"
SOURCE = "united"

CABIN_PREFIX = {"economy": "Y", "premium": "W", "business": "J", "first": "F"}

# United saver award buckets. Polaris/business saver = I, economy saver = X,
# premium plus saver = O on many routes (also used for first on some domestic).
DEFAULT_SAVER_CLASSES = {
    "economy": ["X"],
    "premium": ["O"],
    "business": ["I"],
    "first": ["O", "I"],
}

log = logging.getLogger("award-monitor")
_running = True


def _stop(signum, _frame):
    global _running
    log.info("Received signal %s, shutting down after this cycle", signum)
    _running = False


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class Watch:
    name: str
    origins: list[str]
    destinations: list[str]
    start_date: dt.date
    end_date: dt.date
    cabins: list[str]
    max_miles: int | None = None
    fare_classes: list[str] | None = None  # e.g. ["I"]; "saver" expands per cabin
    min_seats: int = 1
    direct_only: bool = False
    carriers: list[str] | None = None  # operating carriers, e.g. ["UA"]

    @classmethod
    def from_dict(cls, d: dict) -> "Watch":
        def as_list(v):
            if v is None:
                return None
            if isinstance(v, str):
                return [x.strip().upper() for x in v.split(",") if x.strip()]
            return [str(x).strip().upper() for x in v]

        cabins = [c.lower() for c in (d.get("cabins") or ["business"])]
        for c in cabins:
            if c not in CABIN_PREFIX:
                raise ValueError(f"{d.get('name')}: unknown cabin '{c}'")

        fare_classes = d.get("fare_classes")
        if isinstance(fare_classes, str) and fare_classes.lower() == "saver":
            fare_classes = ["SAVER"]
        else:
            fare_classes = as_list(fare_classes)

        w = cls(
            name=d.get("name") or f"{d['origin']}-{d['destination']}",
            origins=as_list(d["origin"]),
            destinations=as_list(d["destination"]),
            start_date=_parse_date(d["start_date"]),
            end_date=_parse_date(d["end_date"]),
            cabins=cabins,
            max_miles=int(d["max_miles"]) if d.get("max_miles") else None,
            fare_classes=fare_classes,
            min_seats=int(d.get("min_seats", 1)),
            direct_only=bool(d.get("direct_only", False)),
            carriers=as_list(d.get("carriers")),
        )
        if w.max_miles is None and not w.fare_classes:
            raise ValueError(f"{w.name}: set max_miles and/or fare_classes")
        return w

    def allowed_classes(self, cabin: str) -> set[str] | None:
        if not self.fare_classes:
            return None
        out: set[str] = set()
        for fc in self.fare_classes:
            if fc == "SAVER":
                out.update(DEFAULT_SAVER_CLASSES[cabin])
            else:
                out.add(fc)
        return out


def _parse_date(v) -> dt.date:
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


@dataclass
class Settings:
    interval_minutes: float = 30
    jitter_minutes: float = 5
    max_trip_lookups_per_cycle: int = 25
    realert_hours: float = 24
    notify_urls: list[str] = field(default_factory=list)


def load_config(path: Path) -> tuple[Settings, list[Watch]]:
    raw = yaml.safe_load(path.read_text())
    s = raw.get("settings", {}) or {}
    settings = Settings(
        interval_minutes=float(s.get("interval_minutes", 30)),
        jitter_minutes=float(s.get("jitter_minutes", 5)),
        max_trip_lookups_per_cycle=int(s.get("max_trip_lookups_per_cycle", 25)),
        realert_hours=float(s.get("realert_hours", 24)),
        notify_urls=[os.path.expandvars(u) for u in (raw.get("notify") or [])],
    )
    # NOTIFY_URLS env (space/comma separated) is appended so secrets can live in .env
    env_urls = os.environ.get("NOTIFY_URLS", "")
    settings.notify_urls += [u for u in env_urls.replace(",", " ").split() if u]
    watches = [Watch.from_dict(w) for w in raw.get("watches", [])]
    if not watches:
        raise ValueError("No watches configured")
    return settings, watches


# --------------------------------------------------------------------------- #
# seats.aero client
# --------------------------------------------------------------------------- #


class SeatsAero:
    def __init__(self, api_key: str):
        self.s = requests.Session()
        self.s.headers.update(
            {"Partner-Authorization": api_key, "Accept": "application/json"}
        )
        self.calls_today = 0
        self._day = dt.date.today()

    def _get(self, path: str, params: dict | None = None) -> dict:
        if dt.date.today() != self._day:
            self._day, self.calls_today = dt.date.today(), 0
        for attempt in range(4):
            self.calls_today += 1
            r = self.s.get(f"{API_BASE}{path}", params=params, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 30 * (2**attempt)
                log.warning("seats.aero %s -> %s, retrying in %ss", path, r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code == 401:
                raise RuntimeError("seats.aero rejected API key (401). Check SEATS_AERO_API_KEY.")
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def search(self, w: Watch) -> list[dict]:
        start = max(w.start_date, dt.date.today())
        if start > w.end_date:
            return []
        params = {
            "origin_airport": ",".join(w.origins),
            "destination_airport": ",".join(w.destinations),
            "start_date": start.isoformat(),
            "end_date": w.end_date.isoformat(),
            "cabins": ",".join(w.cabins),
            "sources": SOURCE,
            "take": 1000,
            "include_filtered": "true",  # dynamic prices too; we filter ourselves
        }
        if w.direct_only:
            params["only_direct_flights"] = "true"
        if w.carriers:
            params["carriers"] = ",".join(w.carriers)

        results: dict[str, dict] = {}
        skip = 0
        while True:
            data = self._get("/search", params)
            batch = data.get("data") or []
            for a in batch:
                results[a["ID"]] = a
            if not data.get("hasMore") or not batch:
                break
            skip += len(batch)
            params.update({"skip": skip, "cursor": data.get("cursor")})
        return list(results.values())

    def trips(self, availability_id: str) -> list[dict]:
        data = self._get(f"/trips/{availability_id}", {"include_filtered": "true"})
        return data.get("data") or []


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


@dataclass
class Hit:
    watch: str
    date: str
    origin: str
    destination: str
    cabin: str
    miles: int
    seats: int
    detail: str = ""
    last_seen: str = ""

    @property
    def key(self) -> str:
        return f"{self.watch}|{self.date}|{self.origin}|{self.destination}|{self.cabin}|{self.detail}"


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def summary_candidates(w: Watch, avail: list[dict]) -> list[tuple[dict, str, int, int]]:
    """Filter availability summaries; returns (availability, cabin, miles, seats)."""
    out = []
    for a in avail:
        if a.get("Source") != SOURCE:
            continue
        for cabin in w.cabins:
            p = CABIN_PREFIX[cabin]
            if not a.get(f"{p}Available"):
                continue
            if w.direct_only and not a.get(f"{p}Direct"):
                continue
            miles = _int(a.get(f"{p}MileageCost"))
            seats = _int(a.get(f"{p}RemainingSeats"))
            if miles <= 0:
                continue
            # Summary shows the cheapest itinerary; if even that is over budget, skip.
            if w.max_miles and miles > w.max_miles:
                continue
            # seats.aero returns 0 when unknown; only reject when a count is reported.
            if seats and seats < w.min_seats:
                continue
            out.append((a, cabin, miles, seats))
    return out


def trip_matches(w: Watch, cabin: str, trip: dict) -> Hit | None:
    if (trip.get("Cabin") or "").lower() != cabin:
        return None
    miles = _int(trip.get("MileageCost"))
    if w.max_miles and miles > w.max_miles:
        return None
    seats = _int(trip.get("RemainingSeats"))
    if seats and seats < w.min_seats:
        return None
    if w.direct_only and _int(trip.get("Stops")) > 0:
        return None
    segs = sorted(trip.get("AvailabilitySegments") or [], key=lambda s: s.get("Order", 0))
    allowed = w.allowed_classes(cabin)
    if allowed is not None:
        if not segs or any((s.get("FareClass") or "").upper() not in allowed for s in segs):
            return None
    if w.carriers:
        if any((s.get("FlightNumber") or "")[:2].upper() not in w.carriers for s in segs):
            return None
    route = " ".join(
        f"{s.get('FlightNumber')}({s.get('FareClass')}) {s.get('OriginAirport')}-{s.get('DestinationAirport')} "
        f"{(s.get('DepartsAt') or '')[11:16]}"
        for s in segs
    )
    return Hit(
        watch=w.name,
        date=(trip.get("DepartsAt") or "")[:10],
        origin=segs[0].get("OriginAirport") if segs else "",
        destination=segs[-1].get("DestinationAirport") if segs else "",
        cabin=cabin,
        miles=miles,
        seats=seats,
        detail=route or trip.get("FlightNumbers", ""),
    )


def check_watch(api: SeatsAero, w: Watch, trip_budget: list[int]) -> list[Hit]:
    avail = api.search(w)
    cands = summary_candidates(w, avail)
    log.info("[%s] %d availability rows, %d candidate(s)", w.name, len(avail), len(cands))

    # Without fare class / carrier rules the summary is enough.
    if not w.fare_classes and not w.carriers:
        return [
            Hit(
                watch=w.name,
                date=a.get("Date", ""),
                origin=a["Route"]["OriginAirport"],
                destination=a["Route"]["DestinationAirport"],
                cabin=cabin,
                miles=miles,
                seats=seats,
                detail=f"{a.get(CABIN_PREFIX[cabin] + 'Airlines', '')}"
                + (" nonstop" if a.get(CABIN_PREFIX[cabin] + "Direct") else ""),
                last_seen=a.get("ComputedLastSeen", ""),
            )
            for a, cabin, miles, seats in cands
        ]

    hits: list[Hit] = []
    for a, cabin, _miles, _seats in sorted(cands, key=lambda c: c[2]):
        if trip_budget[0] <= 0:
            log.warning("[%s] trip lookup budget exhausted this cycle", w.name)
            break
        trip_budget[0] -= 1
        for t in api.trips(a["ID"]):
            h = trip_matches(w, cabin, t)
            if h:
                h.last_seen = a.get("ComputedLastSeen", "")
                hits.append(h)
    return hits


# --------------------------------------------------------------------------- #
# State + notifications
# --------------------------------------------------------------------------- #


class State:
    """Remembers what we've already alerted on so we don't spam."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, dict] = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def should_alert(self, h: Hit, realert_hours: float) -> bool:
        prev = self.data.get(h.key)
        if not prev:
            return True
        if h.miles < prev["miles"]:
            return True  # got cheaper
        if not prev.get("present", True):
            return True  # disappeared then came back
        age_h = (time.time() - prev["alerted_at"]) / 3600
        return realert_hours > 0 and age_h >= realert_hours

    def mark_alerted(self, h: Hit):
        self.data[h.key] = {"miles": h.miles, "alerted_at": time.time(), "present": True}

    def sync_present(self, watch_names: set[str], current_keys: set[str]):
        for k, v in self.data.items():
            if k.split("|", 1)[0] in watch_names:
                v["present"] = k in current_keys

    def prune(self):
        today = dt.date.today().isoformat()
        self.data = {k: v for k, v in self.data.items() if k.split("|")[1] >= today}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)


def format_hits(hits: list[Hit]) -> tuple[str, str]:
    hits = sorted(hits, key=lambda h: (h.date, h.miles))
    title = f"✈️ United award space: {len(hits)} new"
    lines = []
    for h in hits[:20]:
        seats = f"{h.seats} seat(s)" if h.seats else "seats ?"
        lines.append(
            f"{h.date} {h.origin}→{h.destination} {h.cabin.upper()} "
            f"{h.miles:,} mi, {seats}\n  {h.detail}"
        )
    if len(hits) > 20:
        lines.append(f"...and {len(hits) - 20} more")
    lines.append("\nVerify & book: https://www.united.com/en/us/book-flight/united-awards")
    return title, "\n".join(lines)


def build_notifier(urls: list[str]) -> apprise.Apprise:
    ap = apprise.Apprise()
    for u in urls:
        if not ap.add(u):
            log.error("Invalid notify URL (check syntax): %s", u.split("://")[0] + "://...")
    if not len(ap):
        log.warning("No notification targets configured; hits will only be logged")
    return ap


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


def run_cycle(api, settings, watches, state, notifier, dry_run=False) -> int:
    budget = [settings.max_trip_lookups_per_cycle]
    all_hits: list[Hit] = []
    checked: set[str] = set()
    for w in watches:
        try:
            all_hits += check_watch(api, w, budget)
            checked.add(w.name)
        except Exception as e:  # keep other watches running
            log.exception("[%s] check failed: %s", w.name, e)

    new = [h for h in all_hits if state.should_alert(h, settings.realert_hours)]
    for h in all_hits:
        log.info("  match: %s %s→%s %s %s mi %s", h.date, h.origin, h.destination, h.cabin, h.miles, h.detail)

    if new:
        title, body = format_hits(new)
        log.info("Alerting on %d new hit(s)", len(new))
        if dry_run:
            print(f"\n{title}\n{body}\n")
            ok = True
        else:
            ok = notifier.notify(title=title, body=body) if len(notifier) else False
        if ok:
            for h in new:
                state.mark_alerted(h)
        else:
            log.error("Notification failed; will retry next cycle")

    state.sync_present(checked, {h.key for h in all_hits})
    state.prune()
    state.save()
    log.info("Cycle done. API calls today ≈ %d", api.calls_today)
    return len(new)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default=os.environ.get("CONFIG", "config.yaml"))
    ap.add_argument("--state", default=os.environ.get("STATE_FILE", "data/state.json"))
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    settings, watches = load_config(Path(args.config))
    notifier = build_notifier(settings.notify_urls)

    if args.test_notify:
        ok = notifier.notify(title="United award monitor test", body="Notifications work 🎉")
        print("sent" if ok else "FAILED")
        sys.exit(0 if ok else 1)

    key = os.environ.get("SEATS_AERO_API_KEY")
    if not key:
        sys.exit("SEATS_AERO_API_KEY is not set")
    api = SeatsAero(key)
    state = State(Path(args.state))

    log.info("Monitoring %d watch(es): %s", len(watches), ", ".join(w.name for w in watches))
    while _running:
        run_cycle(api, settings, watches, state, notifier, args.dry_run)
        Path(args.state).with_name("heartbeat").write_text(str(int(time.time())))
        if args.once:
            break
        sleep_s = 60 * max(1.0, settings.interval_minutes + random.uniform(0, settings.jitter_minutes))
        log.info("Sleeping %.1f min", sleep_s / 60)
        end = time.time() + sleep_s
        while _running and time.time() < end:
            time.sleep(min(5, end - time.time()))


if __name__ == "__main__":
    main()
