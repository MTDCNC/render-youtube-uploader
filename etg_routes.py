import time
import hashlib
from datetime import datetime
from flask import Flask, jsonify, request
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

ETG_ENDPOINT = "https://engtechgroup.com/wp-content/themes/ETG/machines/filter-machines.php"

def slugify(text: str) -> str:
    t = (text or "").strip().lower().replace("&", "and")
    out, last_dash = [], False
    for ch in t:
        if ch.isalnum():
            out.append(ch); last_dash = False
        else:
            if not last_dash:
                out.append("-"); last_dash = True
    s = "".join(out).strip("-")
    return s or "unknown"

def make_hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:6]

def build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": "MTDCNC product-catalog-ingestor/1.0",
        "Accept": "application/json,text/plain,*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://engtechgroup.com/machines/"
    })
    return s

@app.route("/etg/products", methods=["GET"])
def etg_products():
    # raise defaults so you can prove full 310 first
    max_seconds = int(request.args.get("max_seconds", "120"))
    per_request_timeout = float(request.args.get("timeout", "25"))

    t0 = time.time()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    session = build_session()

    # fetch page 1
    r1 = session.get(ETG_ENDPOINT, params={"page": 1, "feature[type]": "all"}, timeout=per_request_timeout)
    try:
        data1 = r1.json()
    except Exception as e:
        return jsonify({"error": "page1_not_json", "status": r1.status_code, "body_preview": r1.text[:200], "details": str(e)}), 502

    details1 = data1.get("details") or {}
    etg_reported_count = int(details1.get("count") or 0)
    per_page = int(details1.get("products_per_page") or 15)
    expected_pages = max(1, (etg_reported_count + per_page - 1) // per_page)

    seen = set()
    products = []
    page_stats = []

    def ingest(page_data):
        added = 0
        for p in (page_data.get("products") or []):
            url = p.get("url")
            if not url or url in seen:
                continue
            seen.add(url)

            brand = (p.get("manufacturer") or "").strip() or "Unknown"
            products.append({
                "source": "etg",
                "brand": brand,
                "brand_slug": slugify(brand),
                "product_name": (p.get("name") or "").strip(),
                "product_url": url,
                "image_url": p.get("image"),
                "is_new": bool(p.get("new")),
                "hash": make_hash(url),
                "first_seen": today,
                "last_seen": today
            })
            added += 1
        return added

    # page 1 stats
    resp_page_1 = int((data1.get("details") or {}).get("page") or 1)
    returned_1 = len(data1.get("products") or [])
    added_1 = ingest(data1)
    page_stats.append({
        "requested_page": 1,
        "response_page": resp_page_1,
        "status": r1.status_code,
        "returned_count": returned_1,
        "unique_added": added_1
    })

    truncated = False

    for page in range(2, expected_pages + 1):
        if (time.time() - t0) > max_seconds:
            truncated = True
            break

        r = session.get(ETG_ENDPOINT, params={"page": page, "feature[type]": "all"}, timeout=per_request_timeout)

        try:
            data = r.json()
        except Exception:
            page_stats.append({
                "requested_page": page,
                "response_page": None,
                "status": r.status_code,
                "returned_count": None,
                "unique_added": 0,
                "error": "not_json",
                "body_preview": r.text[:120]
            })
            continue

        resp_page = int((data.get("details") or {}).get("page") or 0)
        returned = len(data.get("products") or [])
        added = ingest(data)

        page_stats.append({
            "requested_page": page,
            "response_page": resp_page,
            "status": r.status_code,
            "returned_count": returned,
            "unique_added": added
        })

    return jsonify({
        "total": len(products),
        "unique_urls": len(seen),
        "etg_reported_count": etg_reported_count,
        "per_page": per_page,
        "expected_pages": expected_pages,
        "duration_seconds": round(time.time() - t0, 2),
        "truncated": truncated,
        "page_stats": page_stats,
        "products": products
    })
