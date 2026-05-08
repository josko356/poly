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

## Kompletni setup guide — od nule do live tradinga

### Korak 1 — Kreiraj Polymarket account

1. Idi na [polymarket.com](https://polymarket.com)
2. Klikni **Sign In** → **Continue with Email**
3. Unesi email — dobit ćeš magic link, klikni ga
4. Polymarket automatski kreira **DepositWallet** za tebe (Polygon pametni ugovor)
5. Idi na **Profile → Wallet** — vidiš svoju wallet adresu (format `0x...`)
   - Ovo je tvoj `POLYMARKET_PROXY_ADDRESS`

### Korak 2 — Deponiraj sredstva

Polymarket koristi **pUSD** (stablecoin na Polygonu).

**Opcija A — direktno s kartice/banke:**
1. Na Polymarket klikni **Deposit**
2. Odaberi iznos, plati karticom ili bankovnim transferom
3. sredstva se pojavljuju odmah u CLOB internom balansu

**Opcija B — USDC s Polygon walleta:**
1. Trebate USDC na Polygon mreži u MetaMask ili sličnom walletu
2. Idi na **Deposit** → **Crypto** → pošalji USDC na prikazanu adresu
3. Treba ti i malo POL (ex-MATIC) za gas (~$0.10 vrijedi)

Preporučeni minimum za bot: **$20+**

### Korak 3 — Izvuci API kredencijale

Polymarket CLOB API zahtijeva poseban API key vezan uz tvoj wallet.

1. Otvori [polymarket.com](https://polymarket.com) i prijavi se
2. Otvori **DevTools** (F12) → tab **Application** → **Local Storage** → `https://polymarket.com`
3. Traži ključeve koji sadrže `apiKey`, `secret`, `passphrase`

   Alternativno, otvori **Network** tab → napravi neku radnju (npr. klikni na tržište) → filtriraj po `clob.polymarket.com` → pogledaj request headere — vidiš `POLY_API_KEY` i `POLY_SIGNATURE`

4. Potrebna su ti 3 podatka:
   - `POLY_API_KEY` — UUID format (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)
   - `POLY_API_SECRET` — base64 string
   - `POLY_API_PASSPHRASE` — dugi hex string

### Korak 4 — Pripremi EOA wallet (za potpisivanje)

Bot treba Polygon wallet s privatnim ključem za potpisivanje narudžbi.

1. U MetaMasku kreiraj novi account (ili koristi postojeći)
2. **Account Details → Export Private Key** → spremi kao `POLYGON_PRIVATE_KEY`
3. Spremi adresu walleta kao `POLYGON_ADDRESS`
4. Pošalji mali iznos POL na tu adresu za gas (opcijski, bot koristi CLOB internal balance)

### Korak 5 — Konfiguriraj .env

```env
# Coinbase API (opcionalno — bot radi i bez, ali veza je stabilnija s keyem)
COINBASE_API_KEY=organizations/xxx/apiKeys/yyy
COINBASE_API_SECRET=-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n

# Telegram (opcionalno — za notifikacije i remote komande)
TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_CHAT_ID=987654321

# Paper trading početni balans
PAPER_STARTING_BALANCE=1000.0

# EOA wallet za potpisivanje
POLYGON_PRIVATE_KEY=tvoj_private_key_bez_0x
POLYGON_ADDRESS=0xtvoja_eoa_adresa

# Polymarket DepositWallet i API
POLYMARKET_PROXY_ADDRESS=0xtvoja_deposit_wallet_adresa
POLY_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
POLY_API_SECRET=base64string==
POLY_API_PASSPHRASE=hexstring...

# Live trading (ostavi false dok testiras)
LIVE_TRADING_ENABLED=false
LIVE_TRADING_CONFIRMED=false
LIVE_TRADING_RISK_ACKNOWLEDGED=false
```

### Korak 6 — Instaliraj i pokreni

```bat
# Setup (jednom)
setup.bat

# Aktiviraj venv
venv\Scripts\activate

# Pokreni u paper modu (bez pravog novca)
python main.py
```

Paper mod simulira sve tradove virtualno s $1000. Prati nekoliko sati — ako vidis signale i profit u terminalu, sve radi.

### Korak 7 — Prebaci na live trading

Kad si zadovoljan paper performansama:

1. Provjeri da imaš sredstva na Polymarket DepositWallet
2. U `.env` postavi sve tri zastavice na `true`:
   ```env
   LIVE_TRADING_ENABLED=true
   LIVE_TRADING_CONFIRMED=true
   LIVE_TRADING_RISK_ACKNOWLEDGED=true
   ```
3. Pokreni bota — pri startu se izvodi pre-flight provjera svih sustava
4. Bot čeka 30s i prikazuje sigurnosne limite — pritisni Ctrl+C za abort ako nešto ne štima

### Sigurnosni limiti (live mod)

Bot automatski izračunava limite iz stvarnog balansa pri pokretanju:

| Limit | Vrijednost | Opis |
|-------|-----------|------|
| Max po tradu | 15% balansa | Hard cap na veličinu jedne pozicije |
| Balance floor | 65% početnog | Kill switch ako balans padne ispod |
| Max drawdown | 35% | Kill switch na dnevnom gubitku |
| Max trades/sat | 10 | Rate limiting |

Sve remote komande dostupne su putem Telegrama: `/status`, `/kill`, `/resume`

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
| `MAX_DAILY_DRAWDOWN` | 35% | Kill switch prag |
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
- Kill switch: ako balans padne ispod 65% početnog (max 35% gubitak)
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
