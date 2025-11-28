import io
import os
import time
import logging
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


# ---------- Helpers ----------

def ensure_output_dir(path: str = PROCESSED_DIR) -> str:
    """Ensure processed_images directory exists."""
    os.makedirs(path, exist_ok=True)
    return path


def sanitize_filename(raw: str) -> str:
    """Basic filename sanitizer: spaces → _, strip weird characters."""
    # You can tighten this if you like
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
