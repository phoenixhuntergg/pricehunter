#!/usr/bin/env python3
"""
PriceHunter Pro v2.0 — Backend Python
Scraping automatico con supporto piattaforme personalizzate.
Compatibile con Render.com (gratuito).

AVVIO LOCALE:    python backend.py
AVVIO RENDER:    gunicorn backend:app  (automatico via Procfile)

INSTALLAZIONE:   pip install -r requirements.txt
"""

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS

# ─── CONFIG ────────────────────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", 5000))
DB_PATH = Path(os.environ.get("DB_PATH", "pricehunter.db"))
UPDATE_INTERVAL_MINUTES = int(os.environ.get("UPDATE_INTERVAL", 60))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("pricehunter")

# ─── FLASK ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app, origins="*")

# ─── DATABASE ──────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            store TEXT NOT NULL,
            price REAL NOT NULL,
            url TEXT,
            timestamp INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS custom_platforms (
            name TEXT PRIMARY KEY,
            config TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    log.info("Database pronto ✓")

def save_price_db(product_id, store, price, url=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO price_history (product_id, store, price, url, timestamp) VALUES (?,?,?,?,?)",
        (product_id, store, price, url, int(time.time() * 1000))
    )
    conn.commit()
    conn.close()

def get_history_db(product_id, days=90):
    conn = get_conn()
    since = int((time.time() - days * 86400) * 1000)
    rows = conn.execute(
        "SELECT store, price, timestamp FROM price_history WHERE product_id=? AND timestamp>? ORDER BY timestamp DESC",
        (product_id, since)
    ).fetchall()
    conn.close()
    return [{"store": r["store"], "price": r["price"], "ts": r["timestamp"]} for r in rows]

# ─── STATE ─────────────────────────────────────────────────────────────────────

tracked_products = {}      # {id: product_data}
custom_platforms = {}      # {name: {emoji, domains, priceSelector, titleSelector}}
telegram_cfg = {"token": "", "chat_id": ""}

# ─── BUILT-IN SCRAPERS ─────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def fetch_soup(url, timeout=12):
    try:
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log.warning(f"fetch_soup error {url[:50]}: {e}")
        return None

def clean_price(text):
    """Estrae float da stringa prezzo italiana/internazionale."""
    if not text:
        return None
    text = str(text).strip()
    # rimuovi simboli
    text = re.sub(r'[€$£\s\xa0]', '', text)
    # formato 1.234,56 → 1234.56
    if re.search(r'\.\d{3}', text) and ',' in text:
        text = text.replace('.', '').replace(',', '.')
    elif ',' in text and '.' not in text:
        text = text.replace(',', '.')
    elif ',' in text and '.' in text:
        # 1,234.56
        text = text.replace(',', '')
    # prendi il primo numero decimale valido
    m = re.search(r'\d+(?:\.\d+)?', text)
    if m:
        try:
            return float(m.group())
        except:
            pass
    return None

def try_json_ld(soup):
    """Prova a estrarre il prezzo dal JSON-LD (structured data)."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                data = data[0]
            offers = data.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0]
            price_str = offers.get("price") or offers.get("lowPrice")
            if price_str:
                p = clean_price(str(price_str))
                if p and p > 0:
                    return p
        except:
            pass
    return None

def try_meta(soup):
    """Prova a estrarre il prezzo dai meta tag."""
    for attr in ["product:price:amount", "og:price:amount"]:
        tag = soup.find("meta", property=attr)
        if tag and tag.get("content"):
            p = clean_price(tag["content"])
            if p and p > 0:
                return p
    tag = soup.find("meta", itemprop="price")
    if tag and tag.get("content"):
        p = clean_price(tag["content"])
        if p and p > 0:
            return p
    return None

def try_selectors(soup, selectors):
    """Prova una lista di selettori CSS e restituisce il primo prezzo valido."""
    for sel in selectors:
        try:
            el = soup.select_one(sel)
            if el:
                p = clean_price(el.get_text())
                if p and p > 0:
                    return p
        except:
            pass
    return None

# ── Amazon ──────────────────────────────────────────

def scrape_amazon(url):
    soup = fetch_soup(url)
    if not soup:
        return None
    price = try_selectors(soup, [
        ".a-price .a-offscreen",
        ".apexPriceToPay .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#price_inside_buybox",
        ".priceToPay .a-offscreen",
        "#corePrice_desktop .a-offscreen",
    ])
    if not price:
        price = try_json_ld(soup) or try_meta(soup)
    if not price:
        return None
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else None
    return {"price": price, "store": "Amazon", "name": title}

# ── eBay ────────────────────────────────────────────

def scrape_ebay(url):
    soup = fetch_soup(url)
    if not soup:
        return None
    price = try_selectors(soup, [
        ".x-price-primary .ux-textspans",
        "[data-testid='x-price-primary'] .ux-textspans--BOLD",
        "#prcIsum",
        ".vi-price .notranslate",
    ])
    if not price:
        price = try_json_ld(soup)
    if not price:
        return None
    title_el = soup.select_one("h1.x-item-title__mainTitle") or soup.select_one("#itemTitle")
    title = title_el.get_text(strip=True).replace("Dettagli su", "").strip() if title_el else None
    return {"price": price, "store": "eBay", "name": title}

# ── MediaWorld ──────────────────────────────────────

def scrape_mediaworld(url):
    soup = fetch_soup(url)
    if not soup:
        return None
    price = try_selectors(soup, [
        "[data-testid='product-price'] .value",
        ".product-price .value",
        "[class*='ProductPrice'] [class*='value']",
    ])
    if not price:
        price = try_meta(soup) or try_json_ld(soup)
    if not price:
        return None
    title_el = soup.select_one("h1")
    return {"price": price, "store": "MediaWorld", "name": title_el.get_text(strip=True) if title_el else None}

# ── Unieuro ─────────────────────────────────────────

def scrape_unieuro(url):
    soup = fetch_soup(url)
    if not soup:
        return None
    price = try_selectors(soup, [
        "[data-testid='price-final']",
        ".price-final",
        ".product-price span.value",
        ".summary-price-final",
    ])
    if not price:
        price = try_meta(soup) or try_json_ld(soup)
    if not price:
        return None
    title_el = soup.select_one("h1.product-name") or soup.select_one("h1")
    return {"price": price, "store": "Unieuro", "name": title_el.get_text(strip=True) if title_el else None}

# ── Euronics ────────────────────────────────────────

def scrape_euronics(url):
    soup = fetch_soup(url)
    if not soup:
        return None
    price = try_selectors(soup, [
        ".product-price .price",
        ".price-box .normal-price .price",
        "[data-price-type='finalPrice'] .price",
    ])
    if not price:
        price = try_meta(soup) or try_json_ld(soup)
    if not price:
        return None
    title_el = soup.select_one("h1")
    return {"price": price, "store": "Euronics", "name": title_el.get_text(strip=True) if title_el else None}

# ── Zalando ─────────────────────────────────────────

def scrape_zalando(url):
    soup = fetch_soup(url)
    if not soup:
        return None
    price = try_json_ld(soup)
    if not price:
        price = try_selectors(soup, [
            "[data-testid='price'] span",
            "[class*='Price'] [class*='current']",
        ])
    if not price:
        return None
    title_el = soup.select_one("h1")
    return {"price": price, "store": "Zalando", "name": title_el.get_text(strip=True) if title_el else None}

# ─── GENERIC SCRAPER (piattaforme personalizzate) ───────────────────────────────

def scrape_generic(url, store_name, price_selector, title_selector="h1"):
    """
    Scraper generico per piattaforme personalizzate.
    Usa i selettori CSS forniti dall'utente.
    """
    soup = fetch_soup(url)
    if not soup:
        return None

    # 1. Prova selettore CSS fornito dall'utente
    price = try_selectors(soup, [price_selector])
    # 2. Fallback: JSON-LD
    if not price:
        price = try_json_ld(soup)
    # 3. Fallback: meta tags
    if not price:
        price = try_meta(soup)
    # 4. Fallback: attributo data-price
    if not price:
        el = soup.find(attrs={"data-price": True})
        if el:
            price = clean_price(el["data-price"])

    if not price:
        return None

    title_el = soup.select_one(title_selector) if title_selector else soup.select_one("h1")
    title = title_el.get_text(strip=True) if title_el else None
    return {"price": price, "store": store_name, "name": title}

# ─── ROUTER ────────────────────────────────────────────────────────────────────

BUILTIN_SCRAPERS = {
    "amazon.it": ("Amazon", scrape_amazon),
    "amazon.com": ("Amazon", scrape_amazon),
    "amazon.de": ("Amazon", scrape_amazon),
    "amazon.fr": ("Amazon", scrape_amazon),
    "amazon.es": ("Amazon", scrape_amazon),
    "ebay.it": ("eBay", scrape_ebay),
    "ebay.com": ("eBay", scrape_ebay),
    "mediaworld.it": ("MediaWorld", scrape_mediaworld),
    "unieuro.it": ("Unieuro", scrape_unieuro),
    "euronics.it": ("Euronics", scrape_euronics),
    "zalando.it": ("Zalando", scrape_zalando),
    "zalando.com": ("Zalando", scrape_zalando),
}

def scrape_url(url, extra_platforms=None):
    """
    Seleziona lo scraper giusto per l'URL.
    Prima prova i built-in, poi le piattaforme personalizzate.
    """
    platforms = {**custom_platforms, **(extra_platforms or {})}

    # Prova built-in
    for domain, (store_name, fn) in BUILTIN_SCRAPERS.items():
        if domain in url:
            log.info(f"  [{store_name}] {url[:55]}...")
            result = fn(url)
            if result:
                log.info(f"  → €{result['price']:.2f}")
            else:
                log.warning(f"  → Nessun prezzo trovato")
            return result

    # Prova piattaforme personalizzate
    for name, cfg in platforms.items():
        for domain in cfg.get("domains", []):
            if domain in url:
                log.info(f"  [{name} custom] {url[:55]}...")
                result = scrape_generic(url, name, cfg.get("priceSelector", ""), cfg.get("titleSelector", "h1"))
                if result:
                    log.info(f"  → €{result['price']:.2f}")
                else:
                    log.warning(f"  → Nessun prezzo trovato")
                return result

    log.warning(f"  Nessun scraper per: {url[:60]}")
    return None

# ─── SCHEDULED JOB ─────────────────────────────────────────────────────────────

def update_all():
    if not tracked_products:
        return []
    log.info(f"⏰ Aggiornamento automatico — {len(tracked_products)} prodotti")
    updates = []
    for pid, product in tracked_products.items():
        prices = {}
        for store, url in product.get("urls", {}).items():
            time.sleep(1.5)  # anti-ban
            result = scrape_url(url)
            if result and result.get("price"):
                price = result["price"]
                prices[store] = price
                save_price_db(pid, store, price, url)
                target = product.get("targetPrice")
                if target and price <= target and product.get("alertActive", True):
                    log.info(f"  🎯 ALERT: {product['name']} a €{price:.2f}")
                    send_telegram(product, store, price, url)
        if prices:
            updates.append({"id": pid, "prices": prices})
    log.info(f"✓ Aggiornamento completato — {len(updates)} aggiornati")
    return updates

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(product, store, price, url=""):
    token = telegram_cfg["token"]
    chat_id = telegram_cfg["chat_id"]
    if not token or not chat_id:
        return
    msg = (
        f"🎯 *PriceHunter Pro Alert!*\n\n"
        f"*{product.get('name','Prodotto')}*\n"
        f"📦 Store: {store}\n"
        f"💰 Prezzo attuale: *€{price:.2f}*\n"
        f"🎯 Target: €{product.get('targetPrice','—')}\n\n"
        f"⚡ È il momento di acquistare!"
    )
    if url:
        msg += f"\n[🔗 Vai al prodotto]({url})"
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")

# ─── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/status")
def status():
    return jsonify({
        "status": "online",
        "version": "2.0.0",
        "products": len(tracked_products),
        "custom_platforms": len(custom_platforms),
        "timestamp": int(time.time() * 1000)
    })

@app.route("/scrape", methods=["POST"])
def scrape_single():
    """Scrape un singolo URL."""
    data = request.json or {}
    url = data.get("url", "").strip()
    extra = data.get("customPlatforms", {})
    if not url:
        return jsonify({"error": "URL mancante"}), 400
    result = scrape_url(url, extra)
    if result:
        return jsonify(result)
    return jsonify({"error": "Nessun prezzo trovato"}), 404

@app.route("/scrape-product", methods=["POST"])
def scrape_product():
    """Scrape tutti gli URL di un prodotto."""
    data = request.json or {}
    product = data.get("product", {})
    extra_platforms = data.get("customPlatforms", {})
    urls = product.get("urls", {})
    if not urls:
        return jsonify({"error": "Nessun URL nel prodotto"}), 400
    prices = {}
    for store, url in urls.items():
        time.sleep(1)
        result = scrape_url(url, extra_platforms)
        if result and result.get("price"):
            prices[store] = result["price"]
            save_price_db(product.get("id", ""), store, result["price"], url)
    return jsonify({"prices": prices, "count": len(prices)})

@app.route("/register", methods=["POST"])
def register():
    """Registra un prodotto per aggiornamenti automatici."""
    data = request.json or {}
    pid = data.get("id")
    if not pid:
        return jsonify({"error": "ID mancante"}), 400
    tracked_products[pid] = {
        "name": data.get("name", ""),
        "urls": data.get("urls", {}),
        "targetPrice": data.get("targetPrice"),
        "alertActive": data.get("alertActive", True),
    }
    # Sync custom platforms
    if data.get("customPlatforms"):
        custom_platforms.update(data["customPlatforms"])
    log.info(f"+ Registrato: {data.get('name')}")
    return jsonify({"ok": True})

@app.route("/unregister/<pid>", methods=["DELETE"])
def unregister(pid):
    tracked_products.pop(pid, None)
    return jsonify({"ok": True})

@app.route("/sync-platforms", methods=["POST"])
def sync_platforms():
    """Sincronizza le piattaforme personalizzate dal frontend."""
    data = request.json or {}
    platforms = data.get("platforms", {})
    custom_platforms.update(platforms)
    log.info(f"Sync piattaforme: {list(platforms.keys())}")
    return jsonify({"ok": True, "count": len(custom_platforms)})

@app.route("/update-all", methods=["POST"])
def force_update():
    """Forza aggiornamento immediato."""
    data = request.json or {}
    products = data.get("products", [])
    extra_platforms = data.get("customPlatforms", {})
    custom_platforms.update(extra_platforms)
    for p in products:
        if p.get("id") and p.get("urls"):
            tracked_products[p["id"]] = {
                "name": p.get("name", ""),
                "urls": p.get("urls", {}),
                "targetPrice": p.get("targetPrice"),
                "alertActive": p.get("alertActive", True),
            }
    updates = update_all() or []
    return jsonify({"ok": True, "count": len(updates), "updates": updates})

@app.route("/history/<pid>")
def history(pid):
    days = int(request.args.get("days", 90))
    return jsonify(get_history_db(pid, days))

@app.route("/config", methods=["POST"])
def config():
    data = request.json or {}
    if data.get("telegramToken"):
        telegram_cfg["token"] = data["telegramToken"]
    if data.get("telegramChatId"):
        telegram_cfg["chat_id"] = data["telegramChatId"]
    return jsonify({"ok": True})

# ─── SCHEDULER ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(update_all, "interval", minutes=UPDATE_INTERVAL_MINUTES, id="auto_update", replace_existing=True)

# ─── AVVIO ─────────────────────────────────────────────────────────────────────

init_db()
scheduler.start()

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════╗
║   🎯 PriceHunter Pro v2.0 — Backend      ║
║   Porta: http://localhost:5000           ║
║   Aggiorna ogni: {} minuti             ║
╚══════════════════════════════════════════╝
    """.format(UPDATE_INTERVAL_MINUTES))

    try:
        import socket
        ip = socket.gethostbyname(socket.gethostname())
        print(f"  IP locale (iPhone stesso WiFi): http://{ip}:{PORT}")
    except:
        pass

    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
