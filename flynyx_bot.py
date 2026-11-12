#!/usr/bin/env python3
"""NyxAir (flynyx.com) seat watcher with a Telegram control bot.

Watches one or more flights (route + date + optional departure time) and sends a
Telegram message the moment seats become available. Watches are added/removed by
chatting with the bot. State lives in watches.json (committed back to the repo when
running on GitHub Actions so it survives restarts).

Env:
  TG_TOKEN     Telegram bot token (required)
  TG_CHAT      your numeric Telegram chat id (required; the bot only obeys this chat)
  INTERVAL     seconds between checks (default 60)
  MAX_SECONDS  stop cleanly after this many seconds (0 = never). Used on GitHub Actions.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

import requests
from playwright.sync_api import sync_playwright

URL = "https://flynyx.com/booking"
STATE_FILE = os.environ.get("STATE_FILE", "watches.json")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = str(os.environ.get("TG_CHAT", "")).strip()
INTERVAL = int(os.environ.get("INTERVAL", "60"))
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "0"))
START = time.time()

PLACES = {
    "tallinn": "TLL", "tll": "TLL",
    "kuressaare": "URE", "ure": "URE",
    "kärdla": "KDL", "kardla": "KDL", "kdl": "KDL",
}
NAMES = {"TLL": "Tallinn", "URE": "Kuressaare", "KDL": "Kärdla"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

HELP = (
    "Käsud:\n"
    "/lisa Tallinn Kuressaare 15.11 18:30  – hakka jälgima (kellaaeg pole kohustuslik)\n"
    "/lisa Kuressaare Tallinn 2026-11-22  – kõik selle päeva lennud\n"
    "/nimekiri  – mida praegu jälgin\n"
    "/kustuta 2  – lõpeta jälgimine (number nimekirjast)\n"
    "/kontrolli  – viimane seis kohe\n"
    "Kohad: Tallinn, Kuressaare, Kärdla.\n"
    "Vastus võib võtta kuni minuti."
)


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("offset", 0)
    s.setdefault("next_id", 1)
    s.setdefault("watches", [])
    for w in s["watches"]:
        s["next_id"] = max(s["next_id"], w["id"] + 1)
    return s


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    if os.environ.get("GITHUB_ACTIONS"):
        try:
            subprocess.run(["git", "add", STATE_FILE], check=True)
            r = subprocess.run(["git", "diff", "--cached", "--quiet"])
            if r.returncode != 0:
                subprocess.run(["git", "-c", "user.name=flynyx-bot", "-c",
                                "user.email=bot@users.noreply.github.com", "commit", "-m",
                                "update watches"], check=True)
                subprocess.run(["git", "pull", "--rebase", "-q"], check=False)
                subprocess.run(["git", "push", "-q"], check=True)
        except Exception as e:  # not fatal; state just won't survive a restart
            log(f"could not commit state: {e}")


# ---------------------------------------------------------------- telegram
def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        log("TG not configured: " + text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text}, timeout=20)
    except Exception as e:
        log(f"telegram send error: {e}")


def tg_updates(offset, timeout):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"offset": offset, "timeout": timeout}, timeout=timeout + 15)
        return r.json().get("result", [])
    except Exception as e:
        log(f"telegram poll error: {e}")
        time.sleep(3)
        return []


def parse_add(text):
    """'Tallinn Kuressaare 15.11 18:30' -> dict or error string."""
    low = text.lower()
    found = []
    for m in re.finditer(r"[a-zäöüõ]+", low):
        code = PLACES.get(m.group(0))
        if code:
            found.append((m.start(), code))
    if len(found) < 2:
        return "Ei leidnud kahte kohta. Näide: /lisa Tallinn Kuressaare 15.11 18:30"
    frm, to = found[0][1], found[1][1]
    if frm == to:
        return "Algus ja lõpp on samad."
    today = dt.date.today()
    d = None
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", low)
    if m:
        d = dt.date(int(m[1]), int(m[2]), int(m[3]))
    else:
        m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?!\d*:)", low)
        if m:
            year = int(m[3]) if m[3] else today.year
            d = dt.date(year, int(m[2]), int(m[1]))
            if not m[3] and d < today:
                d = d.replace(year=year + 1)
    if not d:
        return "Ei leidnud kuupäeva. Kasuta kujul 15.11 või 2026-11-15."
    if d < today:
        return "See kuupäev on juba möödas."
    t = None
    m = re.search(r"(\d{1,2}):(\d{2})", low)
    if m:
        t = f"{int(m[1]):02d}:{m[2]}"
    return {"from": frm, "to": to, "date": d.isoformat(), "time": t}


def describe(w):
    return (f"{NAMES.get(w['from'], w['from'])} → {NAMES.get(w['to'], w['to'])} "
            f"{w['date']}" + (f" kell {w['time']}" if w.get("time") else " (kõik lennud)"))


def handle_message(text, state, status):
    """Returns True if the watch list changed (so checks should run right away)."""
    text = text.strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lstrip("/").split("@")[0].lower()
    if cmd in ("start", "abi", "help"):
        tg_send("Tere! Ma jälgin NyxAiri lende ja annan teada, kui koht vabaneb.\n\n" + HELP)
    elif cmd == "lisa":
        w = parse_add(rest)
        if isinstance(w, str):
            tg_send(w)
            return False
        for x in state["watches"]:
            if all(x.get(k) == w[k] for k in ("from", "to", "date", "time")):
                tg_send(f"Juba jälgin: #{x['id']} {describe(x)}")
                return False
        w["id"] = state["next_id"]
        state["next_id"] += 1
        state["watches"].append(w)
        save_state(state)
        tg_send(f"Lisatud #{w['id']}: {describe(w)}")
        return True
    elif cmd in ("nimekiri", "list"):
        if not state["watches"]:
            tg_send("Praegu ei jälgi midagi. Lisa: /lisa Tallinn Kuressaare 15.11 18:30")
        else:
            tg_send("Jälgin:\n" + "\n".join(f"#{w['id']} {describe(w)}" for w in state["watches"]))
    elif cmd in ("kustuta", "eemalda", "del"):
        m = re.search(r"\d+", rest)
        n = int(m[0]) if m else -1
        before = len(state["watches"])
        state["watches"] = [w for w in state["watches"] if w["id"] != n]
        if len(state["watches"]) < before:
            save_state(state)
            tg_send(f"Kustutatud #{n}")
        else:
            tg_send("Sellist numbrit pole. Vaata /nimekiri")
    elif cmd in ("kontrolli", "seis", "status"):
        if not state["watches"]:
            tg_send("Ei jälgi midagi.")
        else:
            tg_send("\n".join(f"#{w['id']} {describe(w)}: {status.get(w['id'], 'pole veel kontrollitud')}"
                              for w in state["watches"]))
    else:
        tg_send("Ei saanud aru.\n\n" + HELP)
    return False


def process_updates(state, status, timeout):
    changed = False
    updates = tg_updates(state["offset"], timeout)
    if updates:
        for u in updates:
            state["offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if chat != TG_CHAT or not msg.get("text"):
                continue
            try:
                changed |= bool(handle_message(msg["text"], state, status))
            except Exception as e:
                log(f"command error: {e}")
                tg_send(f"Viga käsu töötlemisel: {str(e)[:120]}")
        save_state(state)  # persist offset so old commands are not replayed after a restart
    return changed


# ---------------------------------------------------------------- website
def parse_flights(text):
    """Return list of {no, origin, time, open} from the results page text."""
    text = re.sub(r"\s+", " ", text)
    i = text.find("OUTBOUND FLIGHT")
    if i < 0:
        return []
    body = text[i:]
    j = body.find("FLY WITH NYXAIR")
    if j > 0:
        body = body[:j]
    flights = []
    for chunk in body.split("Flight:")[1:]:
        m = re.search(r"\((\w{3})\)\s*(\d{2}:\d{2})", chunk)
        if not m:
            continue
        no = re.match(r"\s*([A-Z0-9]{2}\s*\d+)", chunk)
        flights.append({
            "no": no[1] if no else "?",
            "origin": m[1],
            "time": m[2],
            "open": bool(re.search(r"\d+\.\d{2}\s*EUR", chunk)),
            "raw": chunk[:140],
        })
    return flights


def do_search(page, frm, to, date):
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.get_by_text("Reject", exact=True).first.click(timeout=4000)
    except Exception:
        pass
    try:
        page.get_by_text(re.compile(r"^\s*one way\s*$", re.I)).first.click(timeout=3000)
    except Exception:
        pass
    page.get_by_placeholder("Departure").click()
    page.get_by_text(re.compile(rf"\({frm}\)")).locator("visible=true").first.click()
    page.get_by_text(re.compile(rf"\({to}\)")).locator("visible=true").first.click()
    page.wait_for_timeout(600)
    page.evaluate(
        """(d) => {
            const f = document.getElementById('tripsearch_form');
            f.querySelectorAll('input[name=traveltype]').forEach(x => x.checked = (x.value === 'oneway'));
            const el = f.querySelector('[name=departdate]');
            el.value = d;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", date)
    page.get_by_role("button", name=re.compile(r"^\s*search\s*$", re.I)).first.click()
    page.wait_for_selector("text=OUTBOUND FLIGHT", timeout=45000)
    page.wait_for_timeout(2500)
    head = re.sub(r"\s+", " ", page.inner_text("body"))[:700]
    if date not in head:
        raise RuntimeError(f"date {date} was not applied (page header: {head[:160]})")


def refresh_view(page, date):
    d = dt.date.fromisoformat(date)
    for delta in (1, -1, 2, -2):
        n = d + dt.timedelta(days=delta)
        label = re.compile(rf"{n.day}\.{MONTHS[n.month - 1]} {n.year}")
        try:
            page.get_by_text(label).first.click(timeout=3000)
            page.wait_for_timeout(2500)
            page.get_by_text(re.compile(rf"{d.day}\.{MONTHS[d.month - 1]} {d.year}")).first.click(timeout=3000)
            page.wait_for_timeout(3000)
            return True
        except Exception:
            continue
    return False


def get_flights(page, ctx, frm, to, date):
    key = (frm, to, date)
    if ctx.get("current") == key and refresh_view(page, date):
        fl = parse_flights(page.inner_text("body"))
        if fl:
            return fl
    ctx["current"] = None
    do_search(page, frm, to, date)
    ctx["current"] = key
    return parse_flights(page.inner_text("body"))


def check_all(page, ctx, state, status):
    groups = {}
    for w in state["watches"]:
        groups.setdefault((w["from"], w["to"], w["date"]), []).append(w)
    for (frm, to, date), ws in groups.items():
        fl = get_flights(page, ctx, frm, to, date)
        now = dt.datetime.now().strftime("%H:%M")
        for w in ws:
            if w.get("time"):
                match = [f for f in fl if f["time"] == w["time"] and f["origin"] == frm]
            else:
                match = [f for f in fl if f["origin"] == frm]
            if not match:
                status[w["id"]] = f"lendu ei leitud ({now})"
                if not ctx["missing_alerted"].get(w["id"]):
                    ctx["missing_alerted"][w["id"]] = True
                    tg_send(f"⚠️ #{w['id']} {describe(w)}: lendu ei leitud tulemustest "
                            "(ehk sellel päeval pole lendu või kellaaeg on vale).")
                continue
            ctx["missing_alerted"][w["id"]] = False
            open_now = [f for f in match if f["open"]]
            status[w["id"]] = (f"KOHT VABA ({now})" if open_now else f"välja müüdud ({now})")
            was_open = ctx["was_open"].get(w["id"], False)
            ctx["was_open"][w["id"]] = bool(open_now)
            log(f"#{w['id']} {describe(w)} -> {status[w['id']]}")
            if open_now and not was_open:
                lines = ", ".join(f"{f['no']} {f['time']}" for f in open_now)
                tg_send(f"🛫 KOHT VABANES! #{w['id']} {describe(w)}\n{lines}\nMine kohe: {URL}")


# ---------------------------------------------------------------- main
def main():
    if not (TG_TOKEN and TG_CHAT):
        sys.exit("Set TG_TOKEN and TG_CHAT")
    state = load_state()
    status = {}
    ctx = {"current": None, "was_open": {}, "missing_alerted": {}}
    if not os.environ.get("NO_START_MSG"):
        tg_send(f"Jälgija käivitus. Jälgin {len(state['watches'])} lendu. /abi näitab käske.")
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1600, "height": 900}, locale="en-GB").new_page()
        while True:
            if MAX_SECONDS and time.time() - START > MAX_SECONDS:
                log("max runtime reached, exiting")
                return
            t0 = time.time()
            today = dt.date.today().isoformat()
            old = [w for w in state["watches"] if w["date"] < today]
            if old:
                state["watches"] = [w for w in state["watches"] if w["date"] >= today]
                save_state(state)
            process_updates(state, status, 0)
            if state["watches"]:
                try:
                    check_all(page, ctx, state, status)
                    fails = 0
                except Exception as e:
                    fails += 1
                    ctx["current"] = None
                    log(f"check error #{fails}: {e}")
                    try:
                        page.screenshot(path="flynyx_error.png")
                    except Exception:
                        pass
                    if fails in (3, 10, 30):
                        tg_send(f"⚠️ Ei saa NyxAiri lehte lugeda ({fails} viga järjest): {str(e)[:160]}")
            # wait for the next cycle, but answer Telegram commands while waiting
            while time.time() - t0 < INTERVAL:
                left = INTERVAL - (time.time() - t0)
                if left < 2:
                    break
                if process_updates(state, status, int(min(20, left))):
                    break  # a watch was added: check it right away


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
