# United Award Monitor

Polls [seats.aero](https://seats.aero) for United MileagePlus award space on your routes and
alerts you (email / SMS / push) when something matches your rules.

## Why seats.aero instead of scraping united.com
united.com sits behind Akamai bot protection, so scrapers break often and can get your
MileagePlus account flagged. seats.aero already scrapes United continuously and exposes a
clean API. You need **seats.aero Pro** (~$10/mo) → Settings → API → copy key.
Pro allows 1,000 API calls/day.

## Matching rules (per watch)
| key | meaning |
|---|---|
| `origin` / `destination` | one or more airports, comma-separated |
| `start_date` / `end_date` | departure window |
| `cabins` | `economy`, `premium`, `business`, `first` |
| `max_miles` | alert only at or under this price |
| `fare_classes` | `saver` (X econ / O premium / I business) or explicit list like `[I]`. Every segment must be in that bucket. |
| `min_seats` | number of seats needed |
| `direct_only` | nonstop only |
| `carriers` | operating airline codes, e.g. `[UA]` |

Set at least `max_miles` or `fare_classes`. Using `fare_classes` or `carriers` costs one extra
API call per candidate date (capped by `max_trip_lookups_per_cycle`).

## Deploy on your Linux server
```bash
cp .env.example .env            # fill in API key + notification secrets
cp config.example.yaml config.yaml   # edit your routes
mkdir -p data && sudo chown 1000:1000 data
docker compose build
docker compose run --rm united-award-monitor --test-notify   # check alerts arrive
docker compose run --rm united-award-monitor --once --dry-run # check searches
docker compose up -d
docker compose logs -f
```
After editing `config.yaml`: `docker compose restart`.

## Notifications
Uses [Apprise](https://github.com/caronc/apprise/wiki) URLs, so almost anything works:
- **Gmail:** `mailtos://USER:APP_PASSWORD@gmail.com?to=you@x.com` (create an App Password; 2FA required)
- **SMS (Twilio):** `twilio://SID:TOKEN@+1FROM/+1TO`
- **Push (free, easiest "text"):** install the ntfy app, subscribe to a random topic, use `ntfys://ntfy.sh/TOPIC`
- Carrier email-to-SMS gateways are unreliable now; prefer Twilio or ntfy.

## Notes
- Data is cached by seats.aero (usually minutes to hours old). Always verify on united.com before booking.
- Dedup state lives in `data/state.json`; you're re-alerted when a flight gets cheaper,
  reappears after vanishing, or after `realert_hours`.
