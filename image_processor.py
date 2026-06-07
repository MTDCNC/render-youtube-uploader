import io
import os
import time
import uuid
import logging
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Tuple, Dict, Any, Optional

import requests
from PIL import Image

logger = logging.getLogger("image_processor")

# Defaults – tweak via env if you want
DEFAULT_MAX_FILESIZE = int(os.environ.get("IMG_MAX_FILESIZE_BYTES", 2 * 1024 * 1024))  # 2MB
DEFAULT_MAX_CONTAINER_WIDTH = int(os.environ.get("IMG_MAX_CONTAINER_WIDTH", 1280))
DEFAULT_MAX_CONTAINER_HEIGHT = int(os.environ.get("IMG_MAX_CONTAINER_HEIGHT", 720))
DEFAULT_MIN_WIDTH = int(os.environ.get("IMG_MIN_WIDTH", 640))
PROCESSED_DIR = os.environ.get("PROCESSED_IMAGES_DIR", "processed_images")

# Batch sideload tuning (see process_linkedin_images_batch)
SIDELOAD_CONCURRENCY = int(os.environ.get("WP_SIDELOAD_CONCURRENCY", 4))
MAX_IMAGES_PER_POST = int(os.environ.get("MAX_IMAGES_PER_POST", 10))
SIDELOAD_RETRIES = int(os.environ.get("WP_SIDELOAD_RETRIES", 2))
# When true, check the WP media library for an existing attachment with the
# same (deterministic) filename before uploading, and reuse it if found.
# Makes uploads idempotent so retries/replays cannot create duplicates.
WP_DEDUPE_BY_FILENAME = os.environ.get("WP_DEDUPE_BY_FILENAME", "true").lower() == "true"


# ---------- Helpers ----------

def ensure_output_dir(path: str = PROCESSED_DIR) -> str:
    """Ensure processed_images directory exists."""
    os.makedirs(path, exist_ok=True)
    return path


def sanitize_filename(raw: str) -> str:
    """Filename sanitizer that is latin-1 safe.

    HTTP headers (Content-Disposition) are encoded as latin-1, and Python's
    str.isalnum() returns True for non-ASCII letters (e.g. the Czech 'ř'), so
    the old sanitizer let them through and the WordPress upload crashed with
    UnicodeEncodeError. Normalising to ASCII first fixes that for every caller
    (LinkedIn images AND product images).
    """
    raw = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    safe = "".join(c for c in raw if c.isalnum() or c in ("-", "_", ".", " "))
    return safe.replace(" ", "_")


# ---------- Download ----------

def download_linkedin_image(url: str, timeout: int = 15) -> Tuple[bytes, Dict[str, Any]]:
    """
    Download image bytes from LinkedIn CDN.
    Returns (bytes, meta_dict) or raises on error.
    """
    start = time.time()
    logger.info("[download_linkedin_image] Start download url=%s", url)

    resp = requests.get(url, timeout=timeout)
    elapsed = time.time() - start

    logger.info("[download_linkedin_image] HTTP %s in %.2fs", resp.status_code, elapsed)

    if resp.status_code != 200:
        raise RuntimeError(f"LinkedIn image download failed with status {resp.status_code}")

    content_type = resp.headers.get("Content-Type", "")
    content_len = int(resp.headers.get("Content-Length", len(resp.content) or 0))

    logger.info(
        "[download_linkedin_image] Content-Type=%s, length=%s",
        content_type,
        content_len,
    )

    if not content_type.startswith("image/"):
        raise RuntimeError(f"URL did not return an image (Content-Type={content_type})")

    return resp.content, {
        "status_code": resp.status_code,
        "content_type": content_type,
        "content_length": content_len,
        "elapsed": elapsed,
    }


# ---------- Size calculation ----------

def calculate_target_dimensions(
    original_size: Tuple[int, int],
    max_container_width: int = DEFAULT_MAX_CONTAINER_WIDTH,
    max_container_height: int = DEFAULT_MAX_CONTAINER_HEIGHT,
    min_width: int = DEFAULT_MIN_WIDTH,
) -> Tuple[Tuple[int, int], Dict[str, Any]]:
    """
    Given original (w, h), calculate resized dimensions that:
    - Maintain aspect ratio.
    - Fit inside max_container (width x height).
    - Respect a minimum width if possible.
    Returns ((target_w, target_h), container_info_dict)
    """
    orig_w, orig_h = original_size
    aspect = orig_w / orig_h
    container_aspect = max_container_width / max_container_height

    # Decide whether height or width is limiting
    if aspect < container_aspect:
        # More “square” than container → use container height
        fit_type = "height-constrained"
        target_h = max_container_height
        target_w = int(round(target_h * aspect))
    else:
        fit_type = "width-constrained"
        target_w = max_container_width
        target_h = int(round(target_w / aspect))

    # Enforce minimum width if we can (scale up while staying ≤ container max)
    if target_w < min_width:
        scale = min_width / target_w
        candidate_w = int(round(target_w * scale))
        candidate_h = int(round(target_h * scale))
        if candidate_w <= max_container_width and candidate_h <= max_container_height:
            target_w, target_h = candidate_w, candidate_h
            fit_type += "+minwidth-upscale"

    container_info = {
        "aspect_ratio": round(aspect, 4),
        "container_aspect": round(container_aspect, 4),
        "fit_type": fit_type,
        "max_container": [max_container_width, max_container_height],
        "min_width": min_width,
    }

    logger.info(
        "[calculate_target_dimensions] original=%sx%s, calculated=%sx%s, fit_type=%s",
        orig_w,
        orig_h,
        target_w,
        target_h,
        fit_type,
    )

    return (target_w, target_h), container_info


# ---------- Core image processing ----------

def process_image_bytes(
    image_bytes: bytes,
    target_size: Tuple[int, int],
    max_filesize: int = DEFAULT_MAX_FILESIZE,
) -> Tuple[bytes, Tuple[int, int], int]:
    """
    Resize and JPEG-compress an image to target_size and under max_filesize.
    Returns (processed_bytes, final_size_tuple, final_filesize_bytes).
    """
    start = time.time()
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")

    orig_size = img.size
    logger.info(
        "[process_image] Opened image, original size=%sx%s, mode=%s",
        orig_size[0],
        orig_size[1],
        img.mode,
    )

    target_w, target_h = target_size
    logger.info(
        "[process_image] Calculated dimensions %sx%s from original %sx%s",
        target_w,
        target_h,
        orig_size[0],
        orig_size[1],
    )

    img = img.resize((target_w, target_h), Image.LANCZOS)
    qualities = [85, 80, 75, 70, 65, 60, 55, 50]

    final_bytes: Optional[bytes] = None
    final_filesize = 0

    for idx, q in enumerate(qualities, start=1):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        final_bytes = buf.getvalue()
        final_filesize = len(final_bytes)

        logger.info(
            "[process_image] Iteration %s: quality=%s size=%s bytes",
            idx,
            q,
            final_filesize,
        )

        if final_filesize <= max_filesize or q == qualities[-1]:
            break

    elapsed = time.time() - start
    logger.info(
        "[process_image] Completed processing in %.2fs; final size=%s bytes, %sx%s",
        elapsed,
        final_filesize,
        target_w,
        target_h,
    )

    return final_bytes, (target_w, target_h), final_filesize


# ---------- High-level orchestration ----------

def process_linkedin_image(
    url: str,
    filename: str,
    *,
    output_dir: str = PROCESSED_DIR,
    base_public_url: Optional[str] = None,
    max_container_width: int = DEFAULT_MAX_CONTAINER_WIDTH,
    max_container_height: int = DEFAULT_MAX_CONTAINER_HEIGHT,
    min_width: int = DEFAULT_MIN_WIDTH,
    max_filesize: int = DEFAULT_MAX_FILESIZE,
) -> Dict[str, Any]:
    """
    High-level helper used by Flask route.
    - Downloads the LinkedIn image.
    - Calculates container-aware resize dimensions.
    - Resizes & compresses under max_filesize.
    - Saves to disk in output_dir.
    - Returns a dict suitable for JSON response.
    """
    total_start = time.time()
    safe_filename = sanitize_filename(filename)
    if not safe_filename.lower().endswith(".jpg"):
        safe_filename += ".jpg"

    logger.info(
        "[process_linkedin_image] Start for url=%s filename=%s",
        url,
        safe_filename,
    )

    ensure_output_dir(output_dir)

    # 1) Download
    img_bytes, download_meta = download_linkedin_image(url)

    # 2) Open just to read original size
    with Image.open(io.BytesIO(img_bytes)) as tmp:
        orig_w, orig_h = tmp.size

    # 3) Calculate target size
    target_size, container_info = calculate_target_dimensions(
        (orig_w, orig_h),
        max_container_width=max_container_width,
        max_container_height=max_container_height,
        min_width=min_width,
    )

    # 4) Process image
    processed_bytes, processed_size, file_size = process_image_bytes(
        img_bytes,
        target_size,
        max_filesize=max_filesize,
    )

    # 5) Save to disk
    local_dir = ensure_output_dir(output_dir)
    local_path = os.path.join(local_dir, safe_filename)
    with open(local_path, "wb") as f:
        f.write(processed_bytes)

    # 6) Build public URL if provided
    processed_url = None
    if base_public_url:
        processed_url = f"{base_public_url.rstrip('/')}/images/{safe_filename}"

    total_elapsed = time.time() - total_start
    logger.info(
        "[process_linkedin_image] SUCCESS filename=%s size=%s bytes "
        "processed_size=%sx%s total_time=%.2fs",
        safe_filename,
        file_size,
        processed_size[0],
        processed_size[1],
        total_elapsed,
    )

    return {
        "success": True,
        "filename": safe_filename,
        "local_path": local_path,
        "original_size": [orig_w, orig_h],
        "calculated_size": list(target_size),
        "processed_size": list(processed_size),
        "file_size": file_size,
        "processed_url": processed_url,
        "container_info": container_info,
        "download": download_meta,
        "timing": {"total_seconds": round(total_elapsed, 2)},
    }

def process_product_image(
    url: str,
    filename: str,
    *,
    output_dir: str = PROCESSED_DIR,
    base_public_url: Optional[str] = None,
    max_width: int = 1000,
    max_height: int = 1000,
    max_filesize: int = DEFAULT_MAX_FILESIZE,
) -> Dict[str, Any]:
    """
    Product image processor:
    - Downloads image (any format incl. WEBP)
    - Resizes for product display
    - Outputs PNG preferred
    - Falls back to JPEG if PNG exceeds max_filesize
    - Saves to processed_images/
    """

    total_start = time.time()
    ensure_output_dir(output_dir)

    safe_name = sanitize_filename(filename)

    logger.info("[process_product_image] Start url=%s filename=%s", url, safe_name)

    # 1) Download image (reuse existing downloader logic)
    resp = requests.get(
        url,
        timeout=15,
        allow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Image download failed with status {resp.status_code}")

    if not resp.headers.get("Content-Type", "").startswith("image/"):
        raise RuntimeError("URL did not return an image")

    original_bytes = resp.content

    # 2) Open image
    img = Image.open(io.BytesIO(original_bytes))
    img.load()

    orig_w, orig_h = img.size
    logger.info(
        "[process_product_image] Original size=%sx%s mode=%s format=%s",
        orig_w,
        orig_h,
        img.mode,
        img.format,
    )

    # 3) Calculate resize dimensions (simple fit within max_width/max_height)
    target_size, _ = calculate_target_dimensions(
        (orig_w, orig_h),
        max_container_width=max_width,
        max_container_height=max_height,
        min_width=0,  # no forced upscale for products
    )

    img = img.resize(target_size, Image.LANCZOS)

    # 4) Attempt PNG first
    png_bytes = None
    png_size = 0

    try:
        png_buf = io.BytesIO()
        if img.mode not in ("RGBA", "LA"):
            img_png = img.convert("RGBA")
        else:
            img_png = img

        img_png.save(png_buf, format="PNG", optimize=True, compress_level=6)
        png_bytes = png_buf.getvalue()
        png_size = len(png_bytes)

        logger.info("[process_product_image] PNG attempt size=%s bytes", png_size)

    except Exception as e:
        logger.warning("[process_product_image] PNG conversion failed: %s", e)

    # 5) Decide final format
    if png_bytes and png_size <= max_filesize:
        final_bytes = png_bytes
        output_format = "PNG"
        ext = ".png"

    else:
        # Fallback to JPEG
        logger.info("[process_product_image] Falling back to JPEG")

        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img_jpg = bg
        else:
            img_jpg = img.convert("RGB")

        qualities = [85, 80, 75, 70, 65, 60, 55]
        final_bytes = None

        for q in qualities:
            buf = io.BytesIO()
            img_jpg.save(buf, format="JPEG", quality=q, optimize=True)
            data = buf.getvalue()

            logger.info(
                "[process_product_image] JPEG quality=%s size=%s bytes",
                q,
                len(data),
            )

            final_bytes = data
            if len(data) <= max_filesize:
                break

        output_format = "JPEG"
        ext = ".jpg"

    # 6) Save to disk
    final_filename = safe_name + ext
    local_path = os.path.join(output_dir, final_filename)

    with open(local_path, "wb") as f:
        f.write(final_bytes)

    # 7) Build public URL
    processed_url = None
    if base_public_url:
        processed_url = f"{base_public_url.rstrip('/')}/images/{final_filename}"

    elapsed = round(time.time() - total_start, 2)

    logger.info(
        "[process_product_image] SUCCESS filename=%s format=%s size=%s bytes time=%.2fs",
        final_filename,
        output_format,
        len(final_bytes),
        elapsed,
    )

    return {
        "success": True,
        "filename": final_filename,
        "output_format": output_format,
        "local_path": local_path,
        "processed_url": processed_url,
        "original_size": [orig_w, orig_h],
        "processed_size": list(img.size),
        "file_size": len(final_bytes),
        "timing": {"total_seconds": elapsed},
    }


# ---------- WordPress sideload ----------

def _wp_credentials() -> Tuple[str, Tuple[str, str]]:
    """
    Read WordPress media credentials from the environment.
    Raises a clear error if any are missing so the batch fails loudly
    rather than silently producing posts with no images.

    Required env vars:
      WP_MEDIA_ENDPOINT  e.g. https://mtdcnc.com/wp-json/wp/v2/media
      WP_USER            WordPress username
      WP_APP_PASSWORD    WordPress application password
    """
    endpoint = os.environ.get("WP_MEDIA_ENDPOINT")
    user = os.environ.get("WP_USER")
    app_password = os.environ.get("WP_APP_PASSWORD")

    missing = [
        name for name, val in (
            ("WP_MEDIA_ENDPOINT", endpoint),
            ("WP_USER", user),
            ("WP_APP_PASSWORD", app_password),
        ) if not val
    ]
    if missing:
        raise RuntimeError(f"Missing WordPress env vars: {', '.join(missing)}")

    return endpoint, (user, app_password)


def _find_existing_wp_media(filename: str, timeout: int = 15) -> Optional[int]:
    """Return the media ID of an existing attachment whose stored filename
    matches `filename` exactly, or None.

    Idempotency key: our filenames are deterministic (same post+image -> same
    name every run), so a prior successful upload can be reused instead of
    creating a duplicate. WordPress does NOT dedupe on filename itself (it
    appends -1/-2 and makes a new attachment), so we check explicitly.

    Matching is EXACT against media_details.file (the stored path, e.g.
    '2026/06/grob-..._3.jpg') to avoid WP's fuzzy slug search returning a
    near-name from a different post.
    """
    endpoint, auth = _wp_credentials()

    # filename without extension is what WP's ?search matches on (the slug)
    stem = filename.rsplit(".", 1)[0]
    try:
        resp = requests.get(
            endpoint,
            auth=auth,
            params={"search": stem, "per_page": 100, "media_type": "image"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(
                "[_find_existing_wp_media] lookup HTTP %s for %s - treating as not-found",
                resp.status_code, filename,
            )
            return None

        for item in resp.json():
            details = (item.get("media_details") or {})
            stored = details.get("file", "")          # e.g. '2026/06/grob-..._3.jpg'
            stored_name = stored.rsplit("/", 1)[-1]    # -> 'grob-..._3.jpg'
            slug = item.get("slug", "")
            if stored_name == filename or slug == stem:
                logger.info(
                    "[_find_existing_wp_media] HIT media_id=%s for %s (skip re-upload)",
                    item.get("id"), filename,
                )
                return item.get("id")
        return None
    except Exception as e:
        # Never let the idempotency check break the pipeline; fall back to upload.
        logger.warning("[_find_existing_wp_media] lookup failed for %s: %s", filename, e)
        return None


def sideload_image_to_wordpress(local_path: str, filename: str, timeout: int = 60) -> int:
    """
    Upload a processed image file from disk into the WordPress media library.
    Mirrors the existing Zapier 'Post image to Wordpress' step:
      POST {WP_MEDIA_ENDPOINT}
      Basic Auth, Content-Disposition: attachment; filename="..."
    Returns the new attachment's media ID.

    NOTE: this is NOT idempotent. WordPress does not dedupe on filename - a
    colliding filename gets '-1', '-2' appended and a NEW attachment is created.
    So a retried call re-uploads as fresh media. The batch design avoids retries
    by never timing out; true idempotency (skip if already uploaded) is a
    separate hardening if you ever need it.
    """
    endpoint, auth = _wp_credentials()

    # Idempotency: if this exact filename is already in the media library
    # (a prior run / a Zapier retry), reuse it instead of creating a duplicate.
    if WP_DEDUPE_BY_FILENAME:
        existing = _find_existing_wp_media(filename)
        if existing:
            return existing

    with open(local_path, "rb") as f:
        file_bytes = f.read()

    logger.info(
        "[sideload_image_to_wordpress] Uploading %s (%s bytes) -> %s",
        filename, len(file_bytes), endpoint,
    )

    resp = requests.post(
        endpoint,
        auth=auth,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/jpeg",  # process_linkedin_image always outputs JPEG
        },
        data=file_bytes,
        timeout=timeout,
    )

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"WordPress media upload failed: HTTP {resp.status_code} {resp.text[:300]}"
        )

    media_id = resp.json().get("id")
    if not media_id:
        raise RuntimeError(f"WordPress media upload returned no id: {resp.text[:300]}")

    logger.info(
        "[sideload_image_to_wordpress] SUCCESS media_id=%s filename=%s",
        media_id, filename,
    )
    return media_id


def _sideload_with_retry(local_path: str, filename: str) -> int:
    """Wrap the existing sideload with bounded retry + backoff for transient
    WP 5xx / connection resets under concurrent load."""
    last_err = None
    for attempt in range(SIDELOAD_RETRIES + 1):
        try:
            return sideload_image_to_wordpress(local_path, filename)
        except Exception as e:
            last_err = e
            if attempt < SIDELOAD_RETRIES:
                time.sleep(1.5 * (attempt + 1))  # 1.5s, then 3s
    raise last_err


# ---------- Batch: conform N images + (optionally) sideload to WordPress ----------

def _conform_and_sideload(index, raw_url, stem, base_public_url, upload_to_wordpress):
    """One image, end to end. Runs inside a worker thread.

    process_linkedin_image is thread-safe here: each call uses a unique filename
    (stem_<index>) and its own PIL/requests objects, so there is no shared
    mutable state between workers.
    """
    entry: Dict[str, Any] = {"sourceUrl": raw_url, "ok": False}
    try:
        out = process_linkedin_image(
            raw_url,                       # URL passed through UNTOUCHED (signed/expiring)
            f"{stem}_{index}",             # e.g. bott-ltd_RoyalCornwall_1
            base_public_url=base_public_url,
        )
        entry["cleanUrl"] = out["processed_url"]
        entry["filename"] = out["filename"]
        entry["processed_size"] = out["processed_size"]
        entry["file_size"] = out["file_size"]

        if upload_to_wordpress:
            entry["mediaId"] = _sideload_with_retry(out["local_path"], out["filename"])

        entry["ok"] = True
    except Exception as e:
        logger.exception("[process_linkedin_images_batch] Failed url=%s", raw_url)
        entry["error"] = str(e)

    return index, entry


def process_linkedin_images_batch(
    urls,
    *,
    base_public_url: Optional[str] = None,
    upload_to_wordpress: bool = True,
    user_id: str = "",
    title: str = "",
) -> Dict[str, Any]:
    """
    Conform multiple LinkedIn images and (optionally) sideload each into the
    WordPress media library, ready for the ACF gallery write.

    `urls` may be a list of URLs OR a single comma-joined string
    (LinkedIn signed URLs contain no commas, so splitting on ',' is safe).
    URLs are passed through to requests.get() byte-for-byte — never edited.

    Concurrency: WP sideload is ~8.6s of idle network wait per image, so the
    sideloads run in a thread pool (WP_SIDELOAD_CONCURRENCY, default 4). A
    10-image post finishes in ~26s instead of ~86s, keeping every call well
    under any caller timeout. A per-post cap (MAX_IMAGES_PER_POST, default 10)
    is the backstop so even a 20-image carousel cannot approach the limit.

    Returns:
      {
        "ok": bool,                 # at least one image succeeded
        "processed": int,
        "failed": int,
        "featuredMediaId": int|None,# first successful media ID  -> WP featured_media
        "galleryIds": [int, ...],   # ALL media IDs incl. featured -> acf.gallery
        "allMediaIds": [int, ...],  # same list (kept for back-compat)
        "cleanUrls": [str, ...],    # conformed Render URLs (audit / fallback)
        "results": [ {per-item detail incl. failures} ]
      }
    """
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split(",") if u.strip()]
    urls = [str(u).strip() for u in (urls or []) if str(u).strip()]

    # Backstop cap. Featured is index 0, so the cap keeps the most important ones.
    if len(urls) > MAX_IMAGES_PER_POST:
        logger.info(
            "[process_linkedin_images_batch] capping %s urls to %s",
            len(urls), MAX_IMAGES_PER_POST,
        )
        urls = urls[:MAX_IMAGES_PER_POST]

    # Build a base filename stem: <user_id>_<title[:15]>, ASCII/latin-1 safe.
    title_part = sanitize_filename((title or "").strip())[:15].strip("_")
    user_part = sanitize_filename((user_id or "").strip())
    stem = "_".join(p for p in (user_part, title_part) if p) or "linkedin_image"

    # Conform + sideload each image concurrently, then reassemble in input order
    # so featuredMediaId is always the FIRST input image.
    ordered = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=SIDELOAD_CONCURRENCY) as pool:
        futures = [
            pool.submit(
                _conform_and_sideload, idx, raw_url, stem,
                base_public_url, upload_to_wordpress,
            )
            for idx, raw_url in enumerate(urls, start=1)
        ]
        for fut in as_completed(futures):
            idx, entry = fut.result()
            ordered[idx - 1] = entry

    results = ordered
    succeeded = [r for r in results if r["ok"]]
    media_ids = [r["mediaId"] for r in succeeded if r.get("mediaId")]
    clean_urls = [r["cleanUrl"] for r in succeeded if r.get("cleanUrl")]

    return {
        "ok": len(succeeded) > 0,
        "processed": len(succeeded),
        "failed": len(results) - len(succeeded),
        "featuredMediaId": media_ids[0] if media_ids else None,
        "galleryIds": media_ids,      # IMPROVEMENT #1: featured INCLUDED, never empty
        "allMediaIds": media_ids,     # kept for back-compat
        "cleanUrls": clean_urls,
        "results": results,
    }
