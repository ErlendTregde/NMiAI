"""
augment_copypaste.py
--------------------
OPTIONAL enhancement: synthetically augment rare product classes by pasting
product reference images onto existing shelf training images.

When to use:
  - After running convert_coco_to_yolo.py
  - BEFORE running train.py
  - Only worthwhile if you have time; YOLOv8's built-in copy_paste=0.3
    already helps with existing crops. This targets classes with < MIN_ANNS
    annotations in the training split.

Strategy:
  1. Load product reference photos (data/NM_NGD_product_images/{barcode}/)
  2. Map barcode → category_id via fuzzy name matching
  3. Identify rare categories (< MIN_ANNS training annotations)
  4. Remove white/neutral backgrounds from reference images
  5. Paste onto random training shelf images at plausible scales
  6. Append new YOLO label lines to the target image's .txt file
  7. Save augmented images with _aug{N} suffix

Run:
  pip install opencv-python-headless difflib
  python augment_copypaste.py

Dependencies: cv2 (opencv), pathlib, json — no yaml/os imports needed.
"""

import json
import random
from difflib import SequenceMatcher
from pathlib import Path

# cv2 is used locally here (not in sandbox run.py, so it's fine to import)
try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

# ── Config ─────────────────────────────────────────────────────────────────────
ROOT            = Path(__file__).parent
COCO_JSON       = ROOT / "data" / "NM_NGD_coco_dataset" / "train" / "annotations.json"
REF_ROOT        = ROOT / "data" / "NM_NGD_product_images"
DATASET_DIR     = ROOT / "dataset"

SEED            = 42
MIN_ANNS        = 10     # Augment categories with fewer than this many training annotations
PASTES_PER_CAT  = 8      # How many synthetic images to generate per rare category
FUZZY_THRESHOLD = 0.80   # Name similarity threshold for barcode→category_id mapping
BG_THRESHOLD    = 235    # Pixel value above which background is considered white/neutral

# View preference order for reference images
VIEW_PREFERENCE = ["front", "main", "left", "right", "back", "top", "bottom"]


def fuzzy_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_category_id(product_name: str, category_names: list[str]) -> int | None:
    """Return the best-matching category_id for a product name, or None."""
    best_score = 0.0
    best_idx   = None
    for idx, cat_name in enumerate(category_names):
        score = fuzzy_similarity(product_name, cat_name)
        if score > best_score:
            best_score = score
            best_idx   = idx
    if best_score >= FUZZY_THRESHOLD:
        return best_idx
    return None


def remove_white_background(img: "np.ndarray", threshold: int = BG_THRESHOLD) -> "np.ndarray":
    """
    Create an alpha mask that removes near-white and near-neutral backgrounds.
    Returns BGRA image (4 channels).
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY_INV)

    # Morphological cleanup: fill small holes, remove noise
    kernel  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask    = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask    = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))

    bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = mask
    return bgra


def paste_product_onto_shelf(
    shelf_bgr:   "np.ndarray",
    product_bgra: "np.ndarray",
    x: int, y: int,
) -> "np.ndarray":
    """Alpha-composite product_bgra onto shelf_bgr at position (x, y)."""
    ph, pw = product_bgra.shape[:2]
    sh, sw = shelf_bgr.shape[:2]

    # Clip to shelf bounds
    x2 = min(x + pw, sw)
    y2 = min(y + ph, sh)
    pw_clip = x2 - x
    ph_clip = y2 - y

    if pw_clip <= 0 or ph_clip <= 0:
        return shelf_bgr

    product_clip = product_bgra[:ph_clip, :pw_clip]
    shelf_roi    = shelf_bgr[y:y2, x:x2].copy()

    alpha  = product_clip[:, :, 3:4].astype(float) / 255.0
    fore   = product_clip[:, :, :3].astype(float)
    back   = shelf_roi.astype(float)
    blended = (alpha * fore + (1 - alpha) * back).astype("uint8")

    result = shelf_bgr.copy()
    result[y:y2, x:x2] = blended
    return result


def build_barcode_to_cat_id(
    metadata_path: Path,
    category_names: list[str],
) -> dict[str, int]:
    """Map product barcode → category_id using fuzzy name matching."""
    with open(metadata_path, encoding="utf-8") as f:
        meta = json.load(f)

    mapping: dict[str, int] = {}
    unmatched = 0

    for product in meta.get("products", []):
        code = product["product_code"]
        name = product["product_name"]
        cat_id = find_category_id(name, category_names)
        if cat_id is not None:
            mapping[code] = cat_id
        else:
            unmatched += 1

    print(f"  Barcode→category mapping: {len(mapping)} matched, {unmatched} unmatched")
    return mapping


def get_reference_image(barcode_dir: Path) -> Path | None:
    """Return the best available reference image for a product."""
    for view in VIEW_PREFERENCE:
        for ext in [".jpg", ".jpeg", ".png"]:
            p = barcode_dir / f"{view}{ext}"
            if p.exists():
                return p
    # Fallback: any image in the directory
    for p in barcode_dir.iterdir():
        if p.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            return p
    return None


def main():
    if not CV2_AVAILABLE:
        print("ERROR: opencv-python-headless not installed.")
        print("  pip install opencv-python-headless numpy")
        return

    print("Loading COCO annotations ...")
    with open(COCO_JSON, encoding="utf-8") as f:
        coco = json.load(f)

    category_names = [c["name"] for c in sorted(coco["categories"], key=lambda c: c["id"])]

    # ── Build barcode → category_id map ────────────────────────────────────────
    metadata_path = REF_ROOT / "metadata.json"
    barcode_to_cat = build_barcode_to_cat_id(metadata_path, category_names)

    # ── Count annotations per category in training split ──────────────────────
    # Read existing train label files to count per-category occurrences
    train_lbl_dir = DATASET_DIR / "labels" / "train"
    if not train_lbl_dir.exists():
        print(f"ERROR: {train_lbl_dir} not found. Run convert_coco_to_yolo.py first.")
        return

    cat_counts: dict[int, int] = {}
    for lbl_file in train_lbl_dir.glob("*.txt"):
        for line in lbl_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts:
                cat_id = int(parts[0])
                cat_counts[cat_id] = cat_counts.get(cat_id, 0) + 1

    rare_cats = {cat_id for cat_id, cnt in cat_counts.items() if cnt < MIN_ANNS}
    print(f"  Categories with < {MIN_ANNS} training annotations: {len(rare_cats)}")

    # ── Find reference images for rare categories ──────────────────────────────
    # Invert map: category_id → list of barcode dirs with reference images
    cat_to_barcode_dirs: dict[int, list[Path]] = {}
    for barcode_dir in REF_ROOT.iterdir():
        if not barcode_dir.is_dir():
            continue
        barcode = barcode_dir.name
        cat_id  = barcode_to_cat.get(barcode)
        if cat_id is not None and cat_id in rare_cats:
            cat_to_barcode_dirs.setdefault(cat_id, []).append(barcode_dir)

    print(f"  Rare categories with reference images available: {len(cat_to_barcode_dirs)}")

    if not cat_to_barcode_dirs:
        print("  Nothing to augment. Exiting.")
        return

    # ── Load available training images ────────────────────────────────────────
    train_img_dir = DATASET_DIR / "images" / "train"
    train_imgs    = sorted(train_img_dir.glob("*.jpg")) + sorted(train_img_dir.glob("*.jpeg"))
    if not train_imgs:
        print(f"ERROR: No training images in {train_img_dir}")
        return

    random.seed(SEED)
    augmented_count = 0

    for cat_id, barcode_dirs in cat_to_barcode_dirs.items():
        # Find a reference image
        ref_img_path = None
        for bdir in barcode_dirs:
            ref_img_path = get_reference_image(bdir)
            if ref_img_path:
                break

        if ref_img_path is None:
            continue

        ref_bgr = cv2.imread(str(ref_img_path))
        if ref_bgr is None:
            continue

        # Remove background
        product_bgra = remove_white_background(ref_bgr)

        for paste_idx in range(PASTES_PER_CAT):
            # Pick a random training shelf image
            shelf_img_path = random.choice(train_imgs)
            shelf_stem     = shelf_img_path.stem
            lbl_path       = train_lbl_dir / f"{shelf_stem}.txt"

            shelf_bgr = cv2.imread(str(shelf_img_path))
            if shelf_bgr is None:
                continue
            sh, sw = shelf_bgr.shape[:2]

            # Scale product to ~5–15% of shelf image width (realistic shelf size)
            scale = random.uniform(0.05, 0.15)
            target_w = int(sw * scale)
            target_h = int(target_w * product_bgra.shape[0] / product_bgra.shape[1])
            target_h = max(20, target_h)
            target_w = max(20, target_w)

            product_resized = cv2.resize(product_bgra, (target_w, target_h))

            # Random position (avoid very edges)
            margin = 20
            x = random.randint(margin, max(margin, sw - target_w - margin))
            y = random.randint(margin, max(margin, sh - target_h - margin))

            # Paste
            shelf_aug = paste_product_onto_shelf(shelf_bgr, product_resized, x, y)

            # Save augmented image
            aug_name  = f"{shelf_stem}_aug{cat_id}_{paste_idx}.jpg"
            aug_img_p = train_img_dir / aug_name
            cv2.imwrite(str(aug_img_p), shelf_aug, [cv2.IMWRITE_JPEG_QUALITY, 90])

            # Copy existing labels + add new annotation
            existing_labels = lbl_path.read_text(encoding="utf-8") if lbl_path.exists() else ""
            cx = (x + target_w / 2) / sw
            cy = (y + target_h / 2) / sh
            nw = target_w / sw
            nh = target_h / sh
            new_line = f"{cat_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}"

            aug_lbl_p = train_lbl_dir / f"{shelf_stem}_aug{cat_id}_{paste_idx}.txt"
            aug_lbl_p.write_text(
                (existing_labels.rstrip() + "\n" + new_line).strip(),
                encoding="utf-8",
            )
            augmented_count += 1

        print(f"  Category {cat_id} ({category_names[cat_id][:40]}): "
              f"{PASTES_PER_CAT} synthetic images generated")

    print(f"\nDone. Generated {augmented_count} augmented training images.")
    print(f"Training set is now in {train_img_dir}")
    print("Run train.py to start training.")


if __name__ == "__main__":
    main()
