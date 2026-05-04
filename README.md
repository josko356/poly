# ⚡ Polymarket Latency Arbitrage Bot — Windows

Automatski latency-arbitrage bot za Polymarket BTC/ETH 5-min i 15-min up/down ugovore.

**Coinbase Advanced Trade API** (JWT autentikacija s tvojim API ključem) za real-time
cijene. Eksploatira ~2.7s zakašnjenje Polymarket tržišta.

---

## Brzi start

```bat
REM 1. Pokreni setup (jednom)
setup.bat

REM 2. Testiraj sve
venv\Scripts\activate
python scripts\run_tests.py

REM 3. Provjeri aktivne ugovore
python scripts\check_contracts.py

REM 4. Pokreni bot (paper mode, bez rizika)
python main.py
```

---

## Coinbase API ključ

Bot radi i BEZ API ključa (javni WebSocket) — ali s ključem dobiva:
- Autenticirani feed (stabilniji, bolji rate limit)
- Pristup user channelu za potvrdu cijena

**Kako dobiti ključ:**
1. Idi na https://www.coinbase.com/settings/api
2. Klikni **New API Key**
3. Permissions: samo **View** (ne trebaš trading permission)
4. Spremi **API Key Name** i **Private Key** u `.env`

Format u `.env`:
```env
COINBASE_API_KEY_NAME=organizations/abc123/apiKeys/xyz789
COINBASE_API_PRIVATE_KEY="-----BEGIN EC PRIVATE KEY-----
MHQCAQEEIABC...
-----END EC PRIVATE KEY-----"
```

---

## Telegram setup

1. Otvori Telegram → pretraži `@BotFather`
2. Pošalji `/newbot` → prati upute → dobit ćeš token
3. Pretraži `@userinfobot` → `/start` → dobit ćeš chat ID
4. Unesi u `.env`:
```env
TELEGRAM_BOT_TOKEN=123456:ABC-abc...
TELEGRAM_CHAT_ID=987654321
```

---

## Parametri (config.py)

| Parametar | Vrijednost | Opis |
|-----------|-----------|------|
| `MIN_EDGE` | 5% | Minimalni edge za trade |
| `MIN_CONFIDENCE` | 85% | Minimalni confidence score |
| `MAX_POSITION_PCT` | 8% | Max % portfolia po tradu |
| `KELLY_FRACTION` | 0.5 | Half-Kelly |
| `MAX_DAILY_DRAWDOWN` | 20% | Kill switch prag |
| `MAX_OPEN_POSITIONS` | 6 | Max istovremenih pozicija |

---

## Live trading

Kad si zadovoljan paper rezultatima, u `.env` postavi:
```env
LIVE_TRADING_ENABLED=true
LIVE_TRADING_CONFIRMED=true
LIVE_TRADING_RISK_ACKNOWLEDGED=true
POLYGON_PRIVATE_KEY=0x...
POLYGON_ADDRESS=0x...
```

Bot će tražiti potvrdu u terminalu pri pokretanju.

---

## Struktura projekta

```
polymarket_win\
├── main.py                     # Ulazna točka
├── config.py                   # Svi parametri
├── setup.bat                   # Jednolinijski setup
├── requirements.txt
├── .env.example                # Template za .env
├── core\
│   ├── coinbase_feed.py        # Coinbase WS + JWT auth
│   ├── polymarket_client.py    # Auto contract discovery
│   ├── arbitrage_engine.py     # Detekcija (bug-fixed)
│   ├── kelly_sizer.py          # Half-Kelly
│   ├── risk_manager.py         # Kill switch
│   ├── trading_engine.py       # Paper + live
│   ├── telegram_alerts.py      # Obavijesti
│   ├── dashboard.py            # Rich terminal
│   └── database.py             # SQLite
└── scripts\
    ├── run_tests.py            # Pre-flight testovi
    ├── check_contracts.py      # Provjera aktivnih ugovora
    └── backtest.py             # Backtest modela
```

---

## ⚠️ Napomena

Eksperimentalni bot. Uvijek testiraj u paper modu. Autor ne odgovara za gubitke.
