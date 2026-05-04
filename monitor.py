"""
monitor.py -- Polymarket Bot Health Monitor
===========================================
Radi samostalno u pozadini. Provjera bot svakih 5 minuta.
Sprema izvjestaj u monitor_report.txt.
Salje Windows toast notifikaciju za kriticne dogadjaje.

Pokretanje:
    venv\\Scripts\\python.exe monitor.py

Zaustavljanje: Ctrl+C
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BOT_DIR     = Path(__file__).parent
LOG_FILE    = BOT_DIR / "bot.log"
OUTPUT_FILE = BOT_DIR / "bot_output.txt"
REPORT_FILE = BOT_DIR / "monitor_report.txt"

CHECK_INTERVAL   = 300    # seconds between checks (5 min)
STALE_LOG_LIMIT  = 180    # seconds without new log line = bot likely dead (contract refresh every 60s)
LOOKBACK_LINES   = 200    # lines to scan per check cycle


# -- Windows toast notification -----------------------------------------------

def toast(title: str, message: str):
    """Send a Windows balloon notification (no external lib needed)."""
    safe_title   = title.replace("'", "")
    safe_message = message.replace("'", "")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(10000, '{safe_title}', '{safe_message}', "
        "[System.Windows.Forms.ToolTipIcon]::Warning); "
        "Start-Sleep -Seconds 3; "
        "$n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # notification is best-effort


# -- Log helpers --------------------------------------------------------------

def read_last_lines(path: Path, n: int) -> list:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def log_freshness_secs(path: Path) -> float:
    """Seconds since the log file was last modified."""
    if not path.exists():
        return float("inf")
    return time.time() - path.stat().st_mtime


def is_bot_alive() -> bool:
    """Bot is alive if bot.log was updated recently.
    Contract refresh writes to bot.log every 60s, so >3min stale = bot dead."""
    stale = log_freshness_secs(LOG_FILE)
    if stale > STALE_LOG_LIMIT:
        return False
    # Secondary check: any python.exe running main.py
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
            capture_output=True, text=True, timeout=5,
        )
        return "main.py" in result.stdout
    except Exception:
        # If wmic fails, trust log freshness alone
        return stale < STALE_LOG_LIMIT


def parse_log_lines(lines: list) -> dict:
    """Parse log lines, resetting counters at each bot restart marker.
    This prevents kill-switch or error events from old sessions carrying over."""
    events = {
        "kill_switch":  [],
        "errors":       [],
        "trades_open":  [],
        "trades_close": [],
        "bundles":      [],
        "signals":      [],
        "last_time":    None,
    }
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

    # Find index of last bot startup line — ignore everything before it
    startup_idx = 0
    for i, line in enumerate(lines):
        if "Bot running. Press Ctrl+C to stop." in line:
            startup_idx = i

    for line in lines[startup_idx:]:
        m = ts_re.match(line)
        if m:
            events["last_time"] = m.group(1)
        if "KILL SWITCH FIRED" in line or "KILL SWITCH ACTIVE" in line:
            events["kill_switch"].append(line.strip())
        elif "[ERROR]" in line or "[CRITICAL]" in line:
            events["errors"].append(line.strip())
        elif "[PAPER]" in line and ("Opened" in line or ("BUNDLE" in line and "WON" not in line)):
            events["trades_open"].append(line.strip())
        elif "Trade closed:" in line or " WON " in line or " LOST " in line or "EARLY" in line:
            events["trades_close"].append(line.strip())
        elif "BUNDLE arb" in line:
            events["bundles"].append(line.strip())
        elif "LAG signal" in line:
            events["signals"].append(line.strip())
    return events


def extract_balance(lines: list):
    pattern = re.compile(r"balance=([\d.]+)")
    last = None
    for line in lines:
        m = pattern.search(line)
        if m:
            last = m.group(1)
    return last


# -- Report writer ------------------------------------------------------------

def write_report(cycles: list):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("POLYMARKET BOT MONITOR REPORT\n")
        f.write(f"Generated: {now}  |  Checks: {len(cycles)}\n")
        f.write("=" * 60 + "\n\n")

        if not cycles:
            f.write("No check cycles completed yet.\n")
            return

        last = cycles[-1]
        ok = last["bot_alive"] and not last["kill_switch"] and last["stale_secs"] < STALE_LOG_LIMIT
        status_str = "ALL OK" if ok else "*** PROBLEM DETECTED ***"
        f.write(f"LATEST STATUS  [{last['time']}]  {status_str}\n")
        f.write(f"  Bot process running : {'YES' if last['bot_alive'] else 'NO  <-- ALERT'}\n")
        f.write(f"  Log freshness       : {last['stale_secs']:.0f}s ago ({LOG_FILE.name})\n")
        f.write(f"  Last log timestamp  : {last['last_log_time'] or 'unknown'}\n")
        f.write(f"  Last known balance  : ${last['balance'] or '?'}\n")
        f.write(f"  Kill switch         : {'FIRED  <-- ALERT' if last['kill_switch'] else 'ok'}\n")
        f.write(f"  Errors this cycle   : {last['errors']}\n")
        f.write(f"  Trades opened       : {last['trades_open']}\n")
        f.write(f"  Trades closed       : {last['trades_closed']}\n")
        f.write(f"  Bundle arbs found   : {last['bundles']}\n")
        f.write(f"  LAG signals fired   : {last['signals']}\n\n")

        if last["kill_switch_lines"]:
            f.write("KILL SWITCH EVENTS:\n")
            for l in last["kill_switch_lines"]:
                f.write(f"  {l}\n")
            f.write("\n")

        if last["error_lines"]:
            f.write("ERRORS (last cycle):\n")
            for l in last["error_lines"][-10:]:
                f.write(f"  {l}\n")
            f.write("\n")

        if last["recent_trade_lines"]:
            f.write("RECENT TRADE ACTIVITY:\n")
            for l in last["recent_trade_lines"][-15:]:
                f.write(f"  {l}\n")
            f.write("\n")

        # Cumulative summary across all cycles
        total_opens   = sum(c["trades_open"] for c in cycles)
        total_closes  = sum(c["trades_closed"] for c in cycles)
        total_bundles = sum(c["bundles"] for c in cycles)
        total_signals = sum(c["signals"] for c in cycles)
        total_errors  = sum(c["errors"] for c in cycles)
        any_kill      = any(c["kill_switch"] for c in cycles)
        any_dead      = any(not c["bot_alive"] or c["stale_secs"] > STALE_LOG_LIMIT for c in cycles)

        f.write("=" * 60 + "\n")
        f.write(f"CUMULATIVE SINCE MONITOR START:\n")
        f.write(f"  LAG signals   : {total_signals}\n")
        f.write(f"  Trades opened : {total_opens}\n")
        f.write(f"  Trades closed : {total_closes}\n")
        f.write(f"  Bundle arbs   : {total_bundles}\n")
        f.write(f"  Log errors    : {total_errors}\n")
        f.write(f"  Kill switch   : {'YES -- check log!' if any_kill else 'no'}\n")
        f.write(f"  Bot went dead : {'YES -- check log!' if any_dead else 'no'}\n\n")

        f.write("CHECK HISTORY (last 20):\n")
        for c in cycles[-20:]:
            flag = "OK" if c["bot_alive"] and not c["kill_switch"] and c["stale_secs"] < STALE_LOG_LIMIT else "!!"
            f.write(
                f"  [{flag}] {c['time']}  "
                f"alive={c['bot_alive']}  "
                f"stale={c['stale_secs']:.0f}s  "
                f"bal=${c['balance'] or '?'}  "
                f"opens={c['trades_open']}  closes={c['trades_closed']}\n"
            )


# -- Main loop ----------------------------------------------------------------

def check_once(prev_balance) -> dict:
    lines_log = read_last_lines(LOG_FILE, LOOKBACK_LINES)
    lines_out = read_last_lines(OUTPUT_FILE, LOOKBACK_LINES)
    lines_all = lines_log + lines_out
    events    = parse_log_lines(lines_all)
    balance   = extract_balance(lines_all)
    stale     = log_freshness_secs(LOG_FILE)
    bot_alive = is_bot_alive()

    recent_trade_lines = [
        l.strip() for l in lines_all
        if "Trade closed" in l or " WON " in l or " LOST " in l or "EARLY" in l
    ][-10:]

    result = {
        "time":               datetime.now().strftime("%H:%M:%S"),
        "bot_alive":          bot_alive,
        "stale_secs":         stale,
        "last_log_time":      events["last_time"],
        "balance":            balance,
        "kill_switch":        bool(events["kill_switch"]),
        "kill_switch_lines":  events["kill_switch"][-3:],
        "errors":             len(events["errors"]),
        "error_lines":        events["errors"][-5:],
        "trades_open":        len(events["trades_open"]),
        "trades_closed":      len(events["trades_close"]),
        "bundles":            len(events["bundles"]),
        "signals":            len(events["signals"]),
        "recent_trade_lines": recent_trade_lines,
    }

    # Alert conditions (toast + console)
    if not bot_alive:
        toast("Bot PAO!", "Python main.py nije running. Provjeri PC!")
        print(f"[{result['time']}] !!! BOT NIJE RUNNING !!!")
    elif stale > STALE_LOG_LIMIT:
        toast("Bot ZAMRZNUT?", f"Nema log aktivnosti {stale:.0f}s")
        print(f"[{result['time']}] !!! LOG STALE {stale:.0f}s !!!")
    elif result["kill_switch"]:
        toast("Kill Switch AKTIVAN", "Prekoracio 20% dnevnog gubitka")
        print(f"[{result['time']}] !!! KILL SWITCH AKTIVAN !!!")
    elif result["errors"] > 8:
        toast("Bot greske", f"{result['errors']} gresaka u zadnjih {LOOKBACK_LINES} linija")
        print(f"[{result['time']}] UPOZORENJE: {result['errors']} gresaka")
    else:
        print(
            f"[{result['time']}] OK | "
            f"alive={bot_alive} stale={stale:.0f}s bal=${balance or '?'} | "
            f"opens={result['trades_open']} closes={result['trades_closed']} "
            f"bundles={result['bundles']} signals={result['signals']}"
        )

    return result


def main():
    print("=" * 60)
    print("  POLYMARKET BOT MONITOR")
    print(f"  Log:    {LOG_FILE}")
    print(f"  Report: {REPORT_FILE}")
    print(f"  Interval: {CHECK_INTERVAL // 60} min  |  Stale limit: {STALE_LOG_LIMIT}s")
    print("  Ctrl+C to stop")
    print("=" * 60)

    cycles = []
    prev_balance = None

    # First check immediately
    result = check_once(prev_balance)
    prev_balance = result["balance"]
    cycles.append(result)
    write_report(cycles)

    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            result = check_once(prev_balance)
            prev_balance = result["balance"]
            cycles.append(result)
            write_report(cycles)
        except KeyboardInterrupt:
            print("\nMonitor zaustavljen.")
            write_report(cycles)
            print(f"Izvjestaj: {REPORT_FILE}")
            break


if __name__ == "__main__":
    main()
