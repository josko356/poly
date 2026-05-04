# Polymarket Latency Arbitrage Bot — Windows

Automatski latency-arbitrage bot za Polymarket BTC/ETH/SOL/XRP 5-min i 15-min up/down ugovore.

Eksploatira ~2-3 sekunde zakašnjenja kojim Polymarket tržište reagira na kretanje cijena na spot burzama.

---

## Kako radi

**1. Dual price feed (Binance + Coinbase WebSocket)**
Bot se spaja na dvije burze istovremeno. Binance šalje cijene 50-200ms ranije, Coinbase služi kao potvrda. Svaki trade na burzi = jedan tick koji se zapisuje u memoriju.

**2. Signal detekcija**
Svake 100ms bot mjeri koliko se cijena promijenila u zadnjih 10 sekundi. Ako je pomak > 0.3%, to znači da je tržište počelo reagirati ali Polymarket market makeri još nisu repriceali svoje ugovore.

**3. Chainlink oracle cross-reference**
Polymarket settlea ugovore prema Chainlink oracle cijeni, ne prema Coinbase. Bot koristi Chainlink za preciznije modele vjerojatnosti.

**4. Latency arb**
Bot dohvaća order book za relevantne Polymarket ugovore i izračunava edge (razlika između naše procjene vjerojatnosti i tržišne cijene minus fee). Ako je edge > 2% i confidence > 40%, otvara poziciju.

**5. Bundle arb**
Paralelno skenira ima li slučajeva gdje UP.ask + DOWN.ask < 1 - fees. Kad postoji, kupnja oba tokena garantira profit bez obzira na ishod.

**6. Half-Kelly position sizing**
Veličina pozicije se računa Half-Kelly kriterijem, ograničena na maksimalno 8% portfolia po tradu.

---

## Brzi start

```bat
REM 1. Setup (jednom)
setup.bat

REM 2. Provjeri .env konfiguraciju
venv\Scripts\activate
python scripts\check_env.py

REM 3. Pokreni bot (paper mode — bez rizika)
python main.py
```

---

## Konfiguracija (.env)

Kopiraj `.env.example` u `.env` i popuni vrijednosti:

```env
COINBASE_API_KEY=organizations/xxx/apiKeys/yyy
COINBASE_API_SECRET=tvoj_base64_secret==

TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_CHAT_ID=987654321

PAPER_STARTING_BALANCE=1000.0

POLYGON_PRIVATE_KEY=tvoj_private_key
POLYGON_ADDRESS=0xtvoja_adresa

LIVE_TRADING_ENABLED=false
LIVE_TRADING_CONFIRMED=false
LIVE_TRADING_RISK_ACKNOWLEDGED=false
```

### Coinbase API key

1. Idi na [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/)
2. Kreiraj novi API key → View permisije (trading permission nije potreban)
3. Spremi **API Key ID** (format: `organizations/.../apiKeys/...`) i **Secret** (base64 string)

Bot radi i bez Coinbase key-a (javni WebSocket), ali s key-om je veza stabilnija.

### Telegram (opcionalno)

1. Otvori Telegram → `@BotFather` → `/newbot` → spremi token
2. Otvori `@userinfobot` → `/start` → spremi chat ID
3. Pošalji `/start` svom botu — bez toga bot ne može slati poruke

**Remote komande dok bot radi:**
- `/kill` — zaustavi sve tradanje odmah
- `/resume` — nastavi nakon kill switcha
- `/status` — trenutni balans, P&L, otvorene pozicije

---

## Parametri (config.py)

| Parametar | Vrijednost | Opis |
|-----------|-----------|------|
| `LAG_THRESHOLD_PCT` | 0.3% | Minimalni pomak cijene u 10s za signal |
| `MIN_EDGE` | 2% | Minimalni edge za trade (nakon feea) |
| `MIN_CONFIDENCE` | 40% | Minimalni confidence score |
| `MAX_POSITION_PCT` | 8% | Max % portfolia po tradu (Kelly cap) |
| `KELLY_FRACTION` | 0.5 | Half-Kelly faktor |
| `TAKER_FEE` | 2% | Polymarket taker fee |
| `MAX_DAILY_DRAWDOWN` | 20% | Kill switch prag |
| `MAX_OPEN_POSITIONS` | 6 | Max istovremenih pozicija |
| `MIN_MARKET_PRICE` | 15¢ | Odbaci deep OTM ugovore (model nije pouzdan) |
| `UPDOWN_DURATIONS` | 5, 15 min | Trajanja ugovora koja bot prati |

---

## Live trading

Preporučeno: papir trade barem tjedan dana prije prelaska na live.

**Preduvjeti:**
- MetaMask wallet s USDC na Polygon mreži
- $1-2 POL (ex-MATIC) za gas
- Privatni ključ u `.env`

**Provjera prije pokretanja:**
```
python scripts\check_env.py
```
Provjerava format svih ključeva, derivira adresu iz private key-a, šalje test Telegram poruku, čita USDC balans s lanca, testira Coinbase WebSocket.

**Aktivacija live moda:**
```env
LIVE_TRADING_ENABLED=true
LIVE_TRADING_CONFIRMED=true
LIVE_TRADING_RISK_ACKNOWLEDGED=true
```

**Dinamički safety limiti** (računaju se iz stvarnog balansa pri pokretanju):
- Max po tradu: 15% balansa
- Kill switch: ako balans padne ispod 10% početnog
- Max 10 tradova po satu

---

## Struktura projekta

```
polymarket_win/
├── main.py                      # Ulazna tocka, orchestrator
├── config.py                    # Svi parametri
├── setup.bat                    # Setup skripota (jednom)
├── requirements.txt
├── .env.example                 # Template za .env
├── core/
│   ├── coinbase_feed.py         # Binance + Coinbase dual WebSocket feed
│   ├── chainlink_feed.py        # Chainlink oracle cross-reference
│   ├── polymarket_client.py     # Auto contract discovery + WS order books
│   ├── arbitrage_engine.py      # Latency arb + bundle arb detekcija
│   ├── kelly_sizer.py           # Half-Kelly position sizing
│   ├── risk_manager.py          # Kill switch, drawdown pracenje
│   ├── trading_engine.py        # Paper + live izvrsavanje
│   ├── telegram_alerts.py       # Obavijesti + remote komande
│   ├── dashboard.py             # Terminal dashboard
│   └── database.py              # SQLite trade history
├── scripts/
│   ├── check_env.py             # Pre-flight .env validacija
│   ├── check_contracts.py       # Provjera aktivnih Polymarket ugovora
│   ├── backtest.py              # Backtest modela
│   └── diagnose.py              # Dijagnostika
└── docs/
    └── kako_radi.html           # Detaljna dokumentacija (otvori u browseru)
```

---

## Napomena

Eksperimentalni bot. Uvijek testiraj u paper modu prije live tradinga. Autor ne odgovara za gubitke.
