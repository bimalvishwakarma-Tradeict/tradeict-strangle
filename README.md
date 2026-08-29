# Delta Exchange India — Short Strangle Trading Bot

Lightweight options trade management bot for Delta Exchange India.  
**Strategy:** S001 — Short Strangle with Dynamic Adjustment  
**Model:** User initiates trades → Bot manages (monitor, adjust, target/SL, pre-expiry close)

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| Database | SQLite + SQLAlchemy |
| Exchange | Delta Exchange India REST + WebSocket |
| Frontend | React + Vite + TailwindCSS (Phase 7) |
| Encryption | cryptography (Fernet) |

## Project Structure

```
trading-bot/
├── backend/          # FastAPI API + strategy engine
├── frontend/         # React UI (scaffolded in Phase 7)
├── .env.example
├── requirements.txt
└── README.md
```

## How to Run

### 1. Backend setup

```bash
cd trading-bot
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # or: cp .env.example .env
# Edit .env and set APP_SECRET_KEY to a random 32-character string
```

### 2. Start the API server

```bash
uvicorn backend.main:app --reload --port 8000
```

API will be available at `http://localhost:8000`

### 3. Frontend (Phase 7)

```bash
cd frontend
npm install
npm run dev
```

UI will be available at `http://localhost:5173`

## Documentation

| Document | Description |
|----------|-------------|
| [claude/SESSION_2026-08-29_BOT_CHANGES.md](claude/SESSION_2026-08-29_BOT_CHANGES.md) | **Latest** — B1–B8: basket sizing, ATM straddle, structure-wide hedge SL |
| [docs/SESSION_2026-08-16_CHANGES.md](docs/SESSION_2026-08-16_CHANGES.md) | Aug 16 session — exit funnel, adjustment target, bracket orders |
| [docs/HEDGE_MODE_SPEC.md](docs/HEDGE_MODE_SPEC.md) | Hedge mode design spec (partially superseded by session records above) |

## Notes

- Delta Exchange API keys are stored encrypted in the database (not in `.env`)
- Never commit `.env` or `*.db` files
- Paper/live trading is controlled via your Delta account credentials
