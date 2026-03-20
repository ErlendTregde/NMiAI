"""
convert_coco_to_yolo.py
-----------------------
One-time script: converts the NM NGD COCO dataset to YOLO format.

Outputs:
  dataset/
    images/train/     <- copies of shelf images (80% split)
    images/val/       <- copies of shelf images (20% split)
    labels/train/     <- YOLO .txt label files
    labels/val/
    data.yaml         <- YOLO dataset config (nc=356)

Run:
  python convert_coco_to_yolo.py

No dependencies beyond the standard library + pathlib + shutil.
"""

import json
import random
import shutil
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
COCO_IMAGES = ROOT / "data" / "NM_NGD_coco_dataset" / "train" / "images"
COCO_JSON   = ROOT / "data" / "NM_NGD_coco_dataset" / "train" / "annotations.json"
OUT_DIR     = ROOT / "dataset"

# ── Config ───────────────────────────────────────────────────────────────────
SEED      = 42
VAL_RATIO = 0.20     # 20% validation


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def coco_bbox_to_yolo(bbox, img_w: int, img_h: int):
    """Convert COCO [x, y, w, h] pixels to YOLO [cx, cy, w, h] normalized."""
    x, y, w, h = bbox
    cx = (x + w / 2) / img_w
    cy = (y + h / 2) / img_h
    nw = w / img_w
    nh = h / img_h
    # Clamp to valid range (prevents degenerate labels)
    cx = clamp(cx)
    cy = clamp(cy)
    nw = clamp(nw, 0.001)
    nh = clamp(nh, 0.001)
    return cx, cy, nw, nh


def main():
    print(f"Loading annotations from {COCO_JSON} ...")
    with open(COCO_JSON, encoding="utf-8") as f:
        coco = json.load(f)

    # ── Build lookup structures ───────────────────────────────────────────────
    img_info = {img["id"]: img for img in coco["images"]}

    img_anns: dict[int, list] = {}
    for ann in coco["annotations"]:
        img_anns.setdefault(ann["image_id"], []).append(ann)

    # ── Category names list (sorted by id, used in data.yaml) ────────────────
    categories_sorted = sorted(coco["categories"], key=lambda c: c["id"])
    nc = len(categories_sorted)
    names = [c["name"] for c in categories_sorted]
    print(f"  {nc} categories, IDs {categories_sorted[0]['id']}–{categories_sorted[-1]['id']}")
    print(f"  {len(img_info)} images, {len(coco['annotations'])} annotations")

    # ── Train / val split ─────────────────────────────────────────────────────
    img_ids = list(img_info.keys())
    random.seed(SEED)
    random.shuffle(img_ids)
    n_val  = int(len(img_ids) * VAL_RATIO)
    val_ids   = set(img_ids[:n_val])
    train_ids = set(img_ids[n_val:])
    print(f"  Split: {len(train_ids)} train / {len(val_ids)} val  (seed={SEED})")

    # ── Convert and write ─────────────────────────────────────────────────────
    for split_name, id_set in [("train", train_ids), ("val", val_ids)]:
        img_out = OUT_DIR / "images" / split_name
        lbl_out = OUT_DIR / "labels" / split_name
        img_out.mkdir(parents=True, exist_ok=True)
        lbl_out.mkdir(parents=True, exist_ok=True)

        copied = 0
        missing_src = 0

        for iid in id_set:
            info  = img_info[iid]
            fname = info["file_name"]
            W, H  = info["width"], info["height"]

            # Locate source image (handle .jpg / .jpeg)
            src = COCO_IMAGES / fname
            if not src.exists():
                # Try alternate extension
                alt = src.with_suffix(".jpeg" if src.suffix == ".jpg" else ".jpg")
                if alt.exists():
                    src = alt
                    fname = alt.name
                else:
                    print(f"  WARNING: image not found: {fname}")
                    missing_src += 1
                    continue

            # Copy image
            dst_img = img_out / fname
            if not dst_img.exists():
                shutil.copy2(src, dst_img)

            # Write YOLO label file
            stem       = Path(fname).stem
            label_path = lbl_out / f"{stem}.txt"
            lines = []
            for ann in img_anns.get(iid, []):
                cat_id = ann["category_id"]
                cx, cy, nw, nh = coco_bbox_to_yolo(ann["bbox"], W, H)
                lines.append(f"{cat_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")

            label_path.write_text("\n".join(lines), encoding="utf-8")
            copied += 1

        print(f"  [{split_name}] {copied} images written"
              + (f", {missing_src} missing" if missing_src else ""))

    # ── Write data.yaml ───────────────────────────────────────────────────────
    # Uses absolute path so training works from any working directory.
    abs_dataset = OUT_DIR.resolve().as_posix()
    # Serialize names as a JSON array (valid YAML subset, no quotes needed for ASCII)
    # For names with non-ASCII (Norwegian chars) we use JSON encoding.
    names_json = json.dumps(names, ensure_ascii=False)

    yaml_content = (
        f"path: {abs_dataset}\n"
        f"train: images/train\n"
        f"val:   images/val\n"
        f"\n"
        f"nc: {nc}\n"
        f"names: {names_json}\n"
    )
    data_yaml = OUT_DIR / "data.yaml"
    data_yaml.write_text(yaml_content, encoding="utf-8")
    print(f"\nWrote {data_yaml}")
    print(f"  nc={nc}")
    print(f"  First 3 names: {names[:3]}")
    print(f"  Last  3 names: {names[-3:]}")
    print("\nDone. Dataset ready at:", OUT_DIR)


if __name__ == "__main__":
    main()
