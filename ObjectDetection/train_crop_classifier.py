"""
train_crop_classifier.py
------------------------
Train a dedicated YOLO11m-cls crop classifier on the 22,700 labeled product
crops extracted directly from the 248 training shelf images.

WHY THIS WORKS (unlike the MobileNetV3 re-ranker):
  - Fine-tuned on SHELF CROPS — same domain as test images
  - Pure classification model: no detection head sharing capacity
  - Replaces YOLOv8l in the ensemble (88MB → ~15MB crop classifier)

Expected improvement: classification mAP ~0.82 → ~0.90
Score impact:  0.3 × (0.90 - 0.82) = +0.024  →  ~0.935

Run:
    uv run train_crop_classifier.py

Output:
    submission/weights/crop_cls_fp16.onnx   ~15 MB  (weight file)
"""

import json
import random
import shutil
from collections import defaultdict
from pathlib import Path

from PIL import Image
from ultralytics import YOLO

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent
COCO_JSON  = ROOT / "data" / "NM_NGD_coco_dataset" / "train" / "annotations.json"
IMAGES_DIR = ROOT / "data" / "NM_NGD_coco_dataset" / "train" / "images"
CROP_DIR   = ROOT / "crop_dataset"
WEIGHTS    = ROOT / "submission" / "weights"

# ── Crop extraction config ────────────────────────────────────────────────────
PAD_FACTOR   = 0.15   # 15% padding around each annotated box
MIN_CROP_PX  = 24     # Skip crops smaller than this (too blurry at 224)
VAL_SPLIT    = 0.15   # 15% of crops per class held out for validation
RANDOM_SEED  = 42

# ── Classifier training config ────────────────────────────────────────────────
MODEL_WEIGHTS = "yolo11m-cls.pt"   # ~20MB; small enough to fit in 3-file budget
IMGSZ        = 224
BATCH        = 64
EPOCHS       = 100
PATIENCE     = 30


# ── Step 1: Extract crops ─────────────────────────────────────────────────────

def extract_crops() -> int:
    """
    Extract every annotated bounding box as a padded crop image, organised into
    a YOLO classification dataset:
        crop_dataset/
            train/<category_id>/<ann_id>.jpg
            val/<category_id>/<ann_id>.jpg
    Returns the number of crops saved.

    Groups by image so each shelf image is opened exactly once (248 opens total
    instead of 22,000+), making extraction complete in ~2 min instead of timing out.
    """
    print("Loading COCO annotations …")
    with open(COCO_JSON) as f:
        coco = json.load(f)

    id_to_path = {img["id"]: IMAGES_DIR / img["file_name"] for img in coco["images"]}

    # Create all category directories up front (3-digit zero-padded so
    # alphabetical sort == numerical sort: index i → category_id i)
    for split in ("train", "val"):
        for cat_id in range(356):
            (CROP_DIR / split / f"{cat_id:03d}").mkdir(parents=True, exist_ok=True)

    # Step 1: assign each annotation to train or val (balanced per category)
    random.seed(RANDOM_SEED)
    cat_to_anns: dict[int, list] = defaultdict(list)
    for ann in coco["annotations"]:
        cat_to_anns[ann["category_id"]].append(ann)

    ann_split: dict[int, str] = {}   # ann_id → "train" | "val"
    for cat_id, anns in cat_to_anns.items():
        shuffled = list(anns)
        random.shuffle(shuffled)
        n_val = max(1, int(len(shuffled) * VAL_SPLIT))
        for ann in shuffled[:n_val]:
            ann_split[ann["id"]] = "val"
        for ann in shuffled[n_val:]:
            ann_split[ann["id"]] = "train"

    # Step 2: group annotations by image — open each image exactly once
    img_to_anns: dict[int, list] = defaultdict(list)
    for ann in coco["annotations"]:
        img_to_anns[ann["image_id"]].append(ann)

    saved = skipped = 0
    n_images = len(img_to_anns)

    for i, (img_id, anns) in enumerate(img_to_anns.items(), 1):
        img_path = id_to_path.get(img_id)
        if img_path is None or not img_path.exists():
            skipped += len(anns)
            continue

        img = Image.open(img_path).convert("RGB")
        iw, ih = img.size

        for ann in anns:
            bx, by, bw, bh = ann["bbox"]
            px  = bw * PAD_FACTOR
            py  = bh * PAD_FACTOR
            x1  = max(0.0, bx - px)
            y1  = max(0.0, by - py)
            x2  = min(iw, bx + bw + px)
            y2  = min(ih, by + bh + py)

            if (x2 - x1) < MIN_CROP_PX or (y2 - y1) < MIN_CROP_PX:
                skipped += 1
                continue

            split    = ann_split[ann["id"]]
            cat_id   = ann["category_id"]
            crop     = img.crop((x1, y1, x2, y2))
            dst      = CROP_DIR / split / f"{cat_id:03d}" / f"{ann['id']}.jpg"
            crop.save(dst, quality=92)
            saved += 1

        if i % 50 == 0 or i == n_images:
            print(f"  {i}/{n_images} images processed, {saved} crops saved so far …")

    print(f"Saved {saved} crops  (skipped {skipped} too-small)")
    return saved


# ── Step 2: Train YOLO11m-cls ─────────────────────────────────────────────────

def train_classifier():
    print(f"\nLoading model: {MODEL_WEIGHTS}")
    model = YOLO(MODEL_WEIGHTS)

    print(f"Training crop classifier:")
    print(f"  data={CROP_DIR}  imgsz={IMGSZ}  batch={BATCH}  epochs={EPOCHS}")

    results = model.train(
        data    = str(CROP_DIR),
        epochs  = EPOCHS,
        patience= PATIENCE,
        imgsz   = IMGSZ,
        batch   = BATCH,
        device  = "0" if __import__("torch").cuda.is_available() else "cpu",
        workers = 4,
        amp     = True,

        # Augmentation — match shelf appearance variation
        fliplr  = 0.5,
        degrees = 5.0,
        hsv_h   = 0.015,
        hsv_s   = 0.7,
        hsv_v   = 0.4,
        scale   = 0.3,
        translate = 0.1,

        # LR
        optimizer = "AdamW",
        lr0       = 0.001,
        lrf       = 0.01,
        cos_lr    = True,

        project     = "runs/train",
        name        = "ngd_crop_cls",
        save_period = 20,
        verbose     = True,
    )

    best_pt = Path(results.save_dir) / "weights" / "best.pt"
    if not best_pt.exists():
        for c in Path("/home/coder").rglob("ngd_crop_cls/weights/best.pt"):
            best_pt = c
            break

    print(f"\nBest weights: {best_pt}")
    val_acc = results.results_dict.get("metrics/accuracy_top1", "N/A")
    print(f"Val top-1 accuracy: {val_acc}")
    return best_pt


# ── Step 3: Export to ONNX FP16 ───────────────────────────────────────────────

def export_onnx(best_pt: Path) -> Path:
    print("\nExporting to ONNX FP16 …")
    export_model = YOLO(str(best_pt))
    onnx_path    = export_model.export(
        format   = "onnx",
        imgsz    = IMGSZ,
        half     = True,
        opset    = 17,
        simplify = True,
        dynamic  = False,
    )
    dst = WEIGHTS / "crop_cls_fp16.onnx"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_path, dst)
    print(f"Saved: {dst} ({dst.stat().st_size/1e6:.1f} MB)")

    # Verify
    import onnxruntime as ort
    sess = ort.InferenceSession(str(dst), providers=["CPUExecutionProvider"])
    inp  = sess.get_inputs()[0]
    out  = sess.get_outputs()[0]
    print(f"ONNX input  shape: {inp.shape}  dtype: {inp.type}")
    print(f"ONNX output shape: {out.shape}  dtype: {out.type}")
    print("Expected output:   [1, 356]  — softmax class probabilities")
    return dst


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Step 1 — extract crops (idempotent)
    if not CROP_DIR.exists() or not any(CROP_DIR.rglob("*.jpg")):
        extract_crops()
    else:
        n = sum(1 for _ in CROP_DIR.rglob("*.jpg"))
        print(f"Crop dataset already exists: {n} crops in {CROP_DIR}")

    # Step 2 — train
    best_pt = train_classifier()

    # Step 3 — export
    dst = export_onnx(best_pt)

    # Budget check
    onnx_files = sorted(WEIGHTS.glob("*.onnx"))
    total_mb   = sum(f.stat().st_size for f in onnx_files) / 1e6
    print(f"\nCurrent submission/weights/ ONNX files ({len(onnx_files)}/3 max, {total_mb:.0f}MB/420MB):")
    for f in onnx_files:
        print(f"  {f.name}: {f.stat().st_size/1e6:.1f} MB")

    print("\nNext steps:")
    print("  1. Package: YOLOv8x + YOLO11x + crop_cls_fp16.onnx  (drop YOLOv8l)")
    print("  2. run.py already uses crop_cls_fp16.onnx if found in weights/")


if __name__ == "__main__":
    main()
