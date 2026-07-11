#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
اسکریپت جمع‌آوری روزانه‌ی اخبار از چند سایت و ارسال نتیجه به تلگرام.

روش کار:
1) برای هر سایتِ داده‌شده در sites.txt، ابتدا تلاش می‌کند فید RSS سایت را پیدا کند.
2) اگر فید پیدا نشد، صفحه‌ی اصلی سایت را اسکرپ می‌کند و لینک خبرها را با حدس‌زدن الگوهای معمول استخراج می‌کند.
3) برای هر خبر، عنوان + لینک + خلاصه‌ی کوتاه (از متادیتای صفحه) استخراج می‌شود.
4) خبرهایی که قبلاً ارسال شده‌اند (بر اساس state/sent_links.json) فیلتر می‌شوند تا فقط خبر جدید فرستاده شود.
5) نتیجه به‌صورت پیام(های) تلگرام ارسال می‌شود.
"""

import os
import re
import json
import time
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import feedparser

# ---------- تنظیمات ----------
SITES_FILE = os.path.join(os.path.dirname(__file__), "sites.txt")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state", "sent_links.json")
MAX_ARTICLES_PER_SITE = 12          # حداکثر خبر جدید در هر سایت در هر اجرا
MAX_LINKS_TO_SCRAPE_HOMEPAGE = 25   # حداکثر لینک کاندید هنگام اسکرپ صفحه اصلی
REQUEST_TIMEOUT = 12
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ---------- توابع کمکی عمومی ----------

def load_sites():
    if not os.path.exists(SITES_FILE):
        return []
    sites = []
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sites.append(line)
    return sites


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def safe_get(url, **kwargs):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, **kwargs)
        if resp.status_code == 200:
            return resp
    except requests.RequestException:
        pass
    return None


def clean_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ---------- پیدا کردن و خواندن فید RSS ----------

def discover_feed_url(site_url):
    """تلاش می‌کند آدرس فید RSS سایت را پیدا کند."""
    # حالت اول: تگ <link type="application/rss+xml"> در هدر صفحه
    resp = safe_get(site_url)
    if resp:
        soup = BeautifulSoup(resp.text, "lxml")
        link_tag = soup.find("link", attrs={"type": re.compile("rss|atom", re.I)})
        if link_tag and link_tag.get("href"):
            return urljoin(site_url, link_tag["href"])

    # حالت دوم: حدس زدن آدرس‌های رایج فید
    common_paths = ["/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/rss/feed"]
    for path in common_paths:
        candidate = urljoin(site_url, path)
        r = safe_get(candidate)
        if r and ("xml" in r.headers.get("Content-Type", "") or "<rss" in r.text[:500].lower() or "<feed" in r.text[:500].lower()):
            return candidate
    return None


def get_entries_from_feed(feed_url, limit):
    entries = []
    parsed = feedparser.parse(feed_url)
    for entry in parsed.entries[:limit]:
        title = clean_text(getattr(entry, "title", ""))
        link = getattr(entry, "link", "")
        summary = clean_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
        # حذف تگ‌های HTML احتمالی داخل خلاصه
        summary = clean_text(BeautifulSoup(summary, "lxml").get_text())
        if title and link:
            entries.append({"title": title, "link": link, "summary": summary[:300]})
    return entries


# ---------- اسکرپ صفحه اصلی وقتی فید وجود ندارد ----------

def scrape_homepage_links(site_url):
    resp = safe_get(site_url)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    domain = urlparse(site_url).netloc
    candidates = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(site_url, href)
        parsed = urlparse(full_url)

        if parsed.netloc != domain:
            continue
        if full_url in seen:
            continue

        title = clean_text(a.get_text())
        if len(title) < 15:  # عنوان‌های خیلی کوتاه معمولاً منو/دکمه هستن نه خبر
            continue

        # الگوی معمول لینک خبر: عدد در مسیر، یا کلمات مشخص در URL
        path = parsed.path
        looks_like_article = bool(re.search(r"\d{3,}", path)) or any(
            kw in path for kw in ["/news/", "/article", "/post", "/story"]
        )
        if not looks_like_article:
            continue

        seen.add(full_url)
        candidates.append({"title": title, "link": full_url})
        if len(candidates) >= MAX_LINKS_TO_SCRAPE_HOMEPAGE:
            break

    return candidates


def get_summary_for_link(url):
    resp = safe_get(url)
    if not resp:
        return ""
    soup = BeautifulSoup(resp.text, "lxml")

    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if meta and meta.get("content"):
        return clean_text(meta["content"])[:300]

    p = soup.find("p")
    if p:
        return clean_text(p.get_text())[:300]

    return ""


# ---------- پردازش یک سایت ----------

def process_site(site_url, sent_links):
    print(f"در حال پردازش: {site_url}")
    new_items = []

    feed_url = discover_feed_url(site_url)
    if feed_url:
        print(f"  فید پیدا شد: {feed_url}")
        entries = get_entries_from_feed(feed_url, MAX_ARTICLES_PER_SITE * 2)
    else:
        print("  فید پیدا نشد، اسکرپ صفحه اصلی...")
        raw_candidates = scrape_homepage_links(site_url)
        entries = []
        for c in raw_candidates:
            if c["link"] in sent_links:
                continue
            summary = get_summary_for_link(c["link"])
            entries.append({"title": c["title"], "link": c["link"], "summary": summary})
            time.sleep(0.3)  # کمی مکث برای احترام به سرور مقصد

    for e in entries:
        if e["link"] not in sent_links:
            new_items.append(e)
        if len(new_items) >= MAX_ARTICLES_PER_SITE:
            break

    return new_items


# ---------- ارسال به تلگرام ----------

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("توکن یا چت‌آیدی تلگرام تنظیم نشده. پیام در ترمینال چاپ می‌شود:\n")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print(f"خطا در ارسال تلگرام: {r.status_code} {r.text}")
    except requests.RequestException as ex:
        print(f"خطا در اتصال به تلگرام: {ex}")


def chunk_message(lines, max_len=3800):
    """پیام را به قطعات کوچک‌تر از سقف تلگرام (۴۰۹۶ کاراکتر) تقسیم می‌کند."""
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks


def format_site_block(site_url, items):
    lines = [f"📰 <b>{site_url}</b>"]
    for item in items:
        title = item["title"]
        link = item["link"]
        summary = item.get("summary", "")
        block = f"\n<b>{title}</b>\n{link}"
        if summary:
            block += f"\n<i>{summary}</i>"
        lines.append(block)
    return lines


# ---------- اجرای اصلی ----------

def main():
    sites = load_sites()
    if not sites:
        print("هیچ سایتی در sites.txt تعریف نشده است.")
        return

    state = load_state()  # ساختار: { site_url: [link1, link2, ...] }
    all_lines = []
    total_new = 0

    for site in sites:
        sent_links = set(state.get(site, []))
        new_items = process_site(site, sent_links)

        if new_items:
            all_lines.extend(format_site_block(site, new_items))
            total_new += len(new_items)
            updated_links = list(sent_links) + [it["link"] for it in new_items]
            # فقط ۵۰۰ لینک آخر هر سایت نگه داشته می‌شود که فایل state بزرگ نشود
            state[site] = updated_links[-500:]
        else:
            print(f"  خبر جدیدی برای {site} پیدا نشد.")

    if total_new == 0:
        print("هیچ خبر جدیدی در هیچ‌کدام از سایت‌ها پیدا نشد.")
        send_telegram_message("📭 امروز خبر جدیدی از سایت‌های تعریف‌شده پیدا نشد.")
    else:
        chunks = chunk_message(all_lines)
        for chunk in chunks:
            send_telegram_message(chunk)
            time.sleep(1)
        print(f"مجموعاً {total_new} خبر جدید ارسال شد.")

    save_state(state)


if __name__ == "__main__":
    main()
