# etg_routes.py
import time, os, mimetypes
import hashlib
from datetime import datetime
from flask import jsonify, request, Blueprint
import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry
from PIL import Image
from io import BytesIO

etg_bp = Blueprint("etg", __name__)

WP_API_BASE = os.environ.get("WP_API_BASE", "").rstrip("/")
WP_USER = os.environ.get("WP_USER", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")


ETG_ENDPOINT = "https://engtechgroup.com/wp-content/themes/ETG/machines/filter-machines.php"

def wp_auth():
    return HTTPBasicAuth(WP_USER, WP_APP_PASSWORD)

def strip_query(u: str) -> str:
    return u.split("?", 1)[0]

def filename_from_url(u: str) -> str:
    u = strip_query(u)
    return u.rsplit("/", 1)[-1] or f"image_{int(time.time())}.png"

def resize_to_16_9(image_url: str, max_width: int = 1000) -> BytesIO:
    """
    Download image, force to 16:9 aspect ratio by adding padding, 
    and scale to max width.
    """
    # Download image
    url = strip_query(image_url)
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    
    # Open with Pillow
    img = Image.open(r.raw)
    
    # Convert to RGB if needed (handles RGBA, P, etc)
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    
    orig_width, orig_height = img.size
    orig_aspect = orig_width / orig_height
    target_aspect = 16 / 9
    
    # Calculate new dimensions to achieve 16:9
    if orig_aspect < target_aspect:
        # Image is too tall/square - add width padding
        new_width = int(orig_height * target_aspect)
        new_height = orig_height
    else:
        # Image is too wide - add height padding
        new_width = orig_width
        new_height = int(orig_width / target_aspect)
    
    # Create new canvas with white background
    new_img = Image.new('RGB', (new_width, new_height), (255, 255, 255))
    
    # Center original image on canvas
    paste_x = (new_width - orig_width) // 2
    paste_y = (new_height - orig_height) // 2
    new_img.paste(img, (paste_x, paste_y))
    
    # Scale down to max width if needed
    if new_width > max_width:
        scale_factor = max_width / new_width
        final_width = max_width
        final_height = int(new_height * scale_factor)
        new_img = new_img.resize((final_width, final_height), Image.LANCZOS)
    
    # Save to BytesIO as JPEG
    output = BytesIO()
    new_img.save(output, format='JPEG', quality=90)
    output.seek(0)
    
    return output

def wp_upload_image(image_url: str, title: str = "", alt_text: str = "") -> tuple[int | None, str | None]:
    # Process image to 16:9 aspect ratio
    processed_img = resize_to_16_9(image_url)
    
    filename = filename_from_url(image_url)
    # Force .jpg extension since we're converting to JPEG
    if not filename.lower().endswith(('.jpg', '.jpeg')):
        filename = filename.rsplit('.', 1)[0] + '.jpg'
    
    files = {"file": (filename, processed_img, "image/jpeg")}
    data = {"title": title or filename, "alt_text": alt_text or ""}

    resp = requests.post(f"{WP_API_BASE}/media", files=files, data=data, auth=wp_auth(), timeout=180)
    if resp.status_code != 201:
        raise RuntimeError(f"WP media upload failed {resp.status_code}: {(resp.text or '')[:300]}")
    j = resp.json()
    return j.get("id"), j.get("source_url")

@etg_bp.route("/upload-product-images", methods=["POST"])
def upload_product_images():
    d = request.get_json(force=True) or {}
    image_urls = d.get("image_urls") or []
    featured_url = d.get("featured_url")
    title_prefix = (d.get("title_prefix") or "").strip()
    alt_text = (d.get("alt_text") or "").strip()

    # basic hygiene: strip queries + drop _thumb + de-dupe while preserving order
    seen = set()
    cleaned = []
    for u in image_urls:
        if not u: 
            continue
        u0 = strip_query(u)
        if "_thumb" in u0:
            continue
        if u0 in seen:
            continue
        seen.add(u0)
        cleaned.append(u0)

    if not cleaned:
        return jsonify({"error": "No valid image_urls"}), 400

    gallery_ids = []
    failed = []

    featured_id = None
    featured_url0 = strip_query(featured_url) if featured_url else None

    for idx, u in enumerate(cleaned, start=1):
        try:
            title = f"{title_prefix} ({idx})" if title_prefix else filename_from_url(u)
            alt = f"{alt_text} ({idx})" if alt_text else None
            
            att_id, _src = wp_upload_image(u, title=title, alt_text=alt)
            if att_id:
                gallery_ids.append(att_id)
                if featured_url0 and u == featured_url0:
                    featured_id = att_id
        except Exception as e:
            failed.append({"url": u, "error": str(e)})

    return jsonify({
        "uploaded": len(gallery_ids),
        "failed": failed,
        "gallery_ids": gallery_ids,
        "featured_id": featured_id
    }), 200

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

@etg_bp.route("/products", methods=["GET"])
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
