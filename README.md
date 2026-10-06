# kavita-prewarm

Small, stdlib-only poller for Kavita **v0.8.4**. It asks for On Deck series, finds the partial (or latest read) chapter plus `FORWARD_CHAPTERS` following available chapters, and requests page 0 so Kavita performs extraction/cache warming. It never deletes or cleans Kavita content.

## Verified v0.8.4 API contract

Verified against the `v0.8.4` source tag:

- `POST /api/Account/login` with `{"apiKey":"…","username":"","password":""}` returns a JWT and user API key.
- `POST /api/Series/on-deck?PageNumber=1&PageSize=50&libraryId=0` uses the JWT.
- `GET /api/Series/volumes?seriesId=ID` returns `VolumeDto` objects containing `chapters`; `ChapterDto` supplies `id`, `sortOrder`, `pages`, and `pagesRead`.
- `GET /api/Reader/image?chapterId=ID&page=0&apiKey=KEY` triggers image extraction and authenticates by API-key query parameter.

Sources: `API/Controllers/{Account,Series,Reader}Controller.cs`, `API/DTOs/{Volume,Chapter}Dto.cs` at <https://github.com/Kareadita/Kavita/tree/v0.8.4>.

## Run

```sh
export KAVITA_URL=http://kavita:5000
export KAVITA_API_KEY='replace-me'
python3 kavita_prewarm.py
```

Or copy `compose.example.yml` to `compose.yml`, put `KAVITA_API_KEY=...` in an uncommitted `.env`, attach the Kavita network, and run:

```sh
docker compose up -d --build
```

## Configuration

| Variable | Default | Meaning |
|---|---:|---|
| `KAVITA_URL` | required | Kavita base URL |
| `KAVITA_API_KEY` | required | Kavita 0.8.4 user API key; never persisted |
| `FORWARD_CHAPTERS` | `2` | Available chapters after the anchor |
| `INTERVAL_SECONDS` | `300` | Delay between polls |
| `COOLDOWN_SECONDS` | `21600` | Per-chapter re-warm cooldown |
| `HTTP_TIMEOUT_SECONDS` | `30` | Request timeout |
| `STATE_PATH` | `/state/state.json` | Atomic cooldown state file |
| `LOG_LEVEL` | `INFO` | Python logging level |

A single process handles extraction sequentially. Missing entries, chapter-number gaps, zero-page chapters, HTTP 404s, and other per-series/per-chapter failures are logged and skipped. State is updated only after a successful page response and replaced atomically. Do not scale this service above one replica.

## Test

```sh
python3 -m unittest discover -s tests -v
```
