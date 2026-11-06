"""
TTM Ticket Watcher (cloud) - notify only, never buys.
Checks Thai Ticket Major event pages every ~90s; when a watched round's
"buy" button becomes enabled, sends a push via ntfy and/or Telegram.

Env (GitHub Secrets / Variables):
  EVENT_URLS        event page URLs, comma separated (required)
  ROUNDS            data-button ids to watch, comma separated (empty = all)
  NTFY_TOPIC        ntfy topic name
  TELEGRAM_TOKEN    Telegram bot token
  TELEGRAM_CHAT_ID  your Telegram chat id
  INTERVAL          seconds between checks (min 60, default 90)
  RUN_MINUTES       minutes per workflow run (default 340)
"""
import os
import random
import re
import sys
import time
from html.parser import HTMLParser

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

EVENT_URLS = [u.strip() for u in os.getenv("EVENT_URLS", "").split(",") if u.strip()]
ROUNDS = {r.strip() for r in os.getenv("ROUNDS", "").split(",") if r.strip()}
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = max(60, int(os.getenv("INTERVAL", "90") or 90))
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "340") or 340)
RENOTIFY_SECONDS = 600  # re-alert every 10 min while still open


class RoundParser(HTMLParser):
    """Extract title, dates and a[data-button] inside #section-event-round"""

    def __init__(self):
        super().__init__()
        self.in_section = 0
        self.depth = 0
        self.title = ""
        self._in_h1 = False
        self._in_date = False
        self._in_show = False
        self.cur_date = ""
        self.cur_btn = None
        self.rounds = []  # (id, label, enabled)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "h1" and not self.title:
            self._in_h1 = True
        if a.get("id") == "section-event-round":
            self.in_section = 1
            self.depth = 0
        if self.in_section:
            self.depth += 1
            if "date" in cls.split():
                self._in_date = True
                self.cur_date = ""
            if tag == "a" and "data-button" in a:
                enabled = "disabled" not in a and "disabled" not in cls.split()
                self.cur_btn = {"id": a["data-button"], "enabled": enabled, "time": ""}
            if "item-show" in cls.split():
                self._in_show = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self._in_h1 = False
        if self.in_section:
            if tag == "div":
                self._in_date = False
            if tag == "span":
                self._in_show = False
            if tag == "a" and self.cur_btn:
                b = self.cur_btn
                label = (self.cur_date.strip() + " " + b["time"].strip()).strip() or "round " + b["id"]
                self.rounds.append((b["id"], label, b["enabled"]))
                self.cur_btn = None
            self.depth -= 1
            if self.depth <= 0:
                self.in_section = 0

    def handle_data(self, data):
        if self._in_h1:
            self.title += data
        if self._in_date:
            self.cur_date += data
        if self._in_show and self.cur_btn:
            self.cur_btn["time"] += data


def check(url, session):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    p = RoundParser()
    p.feed(r.text)
    title = re.sub(r"\s+", " ", p.title).strip() or url
    return title, p.rounds


def notify(title, msg, url):
    print(f"[NOTIFY] {title}: {msg}", flush=True)
    if NTFY_TOPIC:
        try:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                          headers={"Title": title.encode("utf-8"), "Priority": "urgent",
                                   "Tags": "rotating_light", "Click": url}, timeout=15)
        except Exception as e:
            print("ntfy error:", e, flush=True)
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": f"{title}\n{msg}\n{url}"}, timeout=15)
        except Exception as e:
            print("telegram error:", e, flush=True)


def main():
    if not EVENT_URLS:
        sys.exit("EVENT_URLS is required")
    if not NTFY_TOPIC and not (TG_TOKEN and TG_CHAT):
        sys.exit("Set NTFY_TOPIC or TELEGRAM_TOKEN + TELEGRAM_CHAT_ID")

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "th,en;q=0.8"})
    end = time.time() + RUN_MINUTES * 60
    last_sent = {}
    first = True
    errors = 0

    while time.time() < end:
        for url in EVENT_URLS:
            try:
                title, rounds = check(url, s)
                errors = 0
            except Exception as e:
                errors += 1
                print(f"error {url}: {e}", flush=True)
                if errors == 5:
                    notify("ตัวเฝ้าบัตร: เข้าเว็บไม่ได้", f"เช็คไม่สำเร็จ 5 ครั้งติด: {e}", url)
                continue

            watched = [r for r in rounds if not ROUNDS or r[0] in ROUNDS]
            state = ", ".join(f"{rid}={'OPEN' if en else 'closed'}" for rid, _, en in watched)
            print(time.strftime("%H:%M:%S"), title, "|", state or "no round buttons", flush=True)

            if first:
                if not watched:
                    notify("ตัวเฝ้าบัตร: หาปุ่มรอบไม่เจอ", f"{title} - หน้าเว็บอาจเปลี่ยนหรือถูกบล็อก", url)
                elif os.getenv("ANNOUNCE_START", "1") == "1":
                    notify("ตัวเฝ้าบัตรเริ่มทำงาน", f"{title}\n" + "\n".join(
                        f"- {lbl}: {'เปิด' if en else 'ยังปิด'}" for _, lbl, en in watched), url)

            for rid, label, enabled in watched:
                key = (url, rid)
                if enabled and time.time() - last_sent.get(key, 0) > RENOTIFY_SECONDS:
                    notify(f"มีบัตร! {title}", f"{label} ปุ่มซื้อบัตรเปิดแล้ว รีบเข้าไปกดเลย", url)
                    last_sent[key] = time.time()
                elif not enabled:
                    last_sent.pop(key, None)
        first = False
        time.sleep(INTERVAL + random.randint(0, 30))


if __name__ == "__main__":
    main()
