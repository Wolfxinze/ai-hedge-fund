# Observing Pools — Phase 0 Usage

Research-only feature: ranked per-platform pools plus opportunity reports. Read-only,
loopback API. No order placement or trading actions.

## Run the app

Backend (FastAPI, http://localhost:8000):

```bash
cd app/backend
SERVER_BIND_HOST=127.0.0.1 poetry run uvicorn main:app --reload
```

Frontend (Vite dev server):

```bash
cd app/frontend
npm run dev
```

The frontend reads the API base URL from `VITE_API_URL` (defaults to
`http://localhost:8000`).

## Open the UI tab

In the top bar (top-right), click the line-chart icon ("Open Observing Pools").
This opens an **Observing Pools** tab that renders, per platform
(`ai`, `robotics`, `energy_storage`, `blockchain`, `multiomic_sequencing`):

- ranked pool entries with composite score and component breakdown
  (platform fit, value, innovation/growth, risk-adjusted momentum, Serenity bottleneck)
- opportunity reports with confidence, degraded flag, summary, and disclaimer

The view is fully EN/CN — use the language toggle in the tab or the global one in the
top bar.

## CLI commands

Observing pools pipeline:

```bash
python -m src.observing_pools init      # initialize pool data
python -m src.observing_pools refresh   # recompute rankings / scores
python -m src.observing_pools inspect   # print current pool state
```

Serenity (bottleneck research):

```bash
python -m src.serenity research         # run bottleneck research
python -m src.serenity apply            # apply research into scoring inputs
```

Monitoring:

```bash
python -m src.monitoring create         # create a monitor
python -m src.monitoring run            # run monitors
python -m src.monitoring list           # list monitors
```

## API endpoints (read-only)

- `GET /observing-pools/{platform_key}` — ranked pool for a platform
- `GET /opportunity-reports` — opportunity reports

These are the endpoints the UI tab consumes.
