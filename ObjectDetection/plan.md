# NorgesGruppen Object Detection — Implementation Plan

## Context

This is the NorgesGruppen (NM) Object Detection competition. The goal is to detect and classify grocery products on store shelves from a sandboxed Docker container.

**Score formula:** `Score = 0.7 × detection_mAP@0.5 + 0.3 × classification_mAP@0.5`

- **Detection mAP (70%)**: Did you find the products? IoU ≥ 0.5, category ignored.
- **Classification mAP (30%)**: Did you identify the correct product? IoU ≥ 0.5 AND correct `category_id`.

Key constraints:
- Sandbox: Python 3.11, NVIDIA L4 24 GB, CUDA 12.4, 300s timeout, offline
- Pre-installed: `ultralytics==8.1.0`, `torch==2.6.0+cu124`, `onnxruntime-gpu 1.20.0`
- Submission: `.zip` with `run.py` at root, max 420 MB uncompressed, max 3 weight files
- Security: `run.py` cannot import `os, sys, subprocess, socket, pickle, yaml, requests, multiprocessing, shutil` — use `pathlib` and `json` instead.

**Strategy: Fine-tune YOLOv8l** on the competition COCO data with `nc=356`, `imgsz=1280`, strong augmentation. `ultralytics==8.1.0` is pre-installed in the sandbox — `.pt` weights load directly with no conversion.

---

## Dataset Facts

- **248 shelf images**, sizes from 481×399 up to 5712×4624 px (highly variable)
- **~22,700 annotations**, avg 91.7 per image (min 14, max 235)
- **356 categories**, `category_id` 0–355 (contiguous, no gaps; 355 = `unknown_product`)
- **327 product reference images**: 1,582 multi-angle photos organized by barcode folder
- COCO annotations format: `bbox = [x_topleft, y_topleft, width, height]` in pixels
- Images contain both `.jpg` and `.jpeg` extensions

---

## Files to Create

| File | Purpose | In submission zip? |
|------|---------|-------------------|
| `plan.md` | This plan (human-readable) | No |
| `convert_coco_to_yolo.py` | One-time COCO→YOLO conversion | No |
| `train.py` | YOLOv8l fine-tuning script | No |
| `evaluate.py` | Validation metrics inspection | No |
| `augment_copypaste.py` | Optional: rare-class augmentation with reference images | No |
| `run.py` | **Submission entry point** | **Yes (required)** |
| `submission/weights/best.pt` | Trained model weights (~87 MB) | Yes |

Critical source files:
- `data/NM_NGD_coco_dataset/train/annotations.json` — ground truth annotations
- `data/NM_NGD_coco_dataset/train/images/` — 248 shelf images
- `data/NM_NGD_product_images/metadata.json` — product barcode→name mapping

---

## Step 1: Data Preparation — `convert_coco_to_yolo.py`

Run once locally. Converts COCO annotations to YOLO `.txt` label format and creates the dataset folder structure.

**Key logic:**
- `category_id` 0–355 maps directly to YOLO `class_id` (no remapping)
- COCO `[x, y, w, h]` pixels → YOLO `[cx, cy, w, h]` normalized to `[0,1]`
- Train/val split: 80/20 (~198 train, ~50 val), `random.seed(42)`
- Clamp all bbox values to `[0.001, 1.0]` to prevent degenerate labels

**Output directory structure:**
```
dataset/
  images/
    train/   ← copies of .jpg/.jpeg files
    val/
  labels/
    train/   ← one .txt per image, YOLO format
    val/
  data.yaml  ← nc=356, names=[...], path=./dataset
```

**YOLO bbox conversion formula:**
```python
cx = (bbox[0] + bbox[2] / 2) / img_width
cy = (bbox[1] + bbox[3] / 2) / img_height
nw = bbox[2] / img_width
nh = bbox[3] / img_height
```

**`data.yaml` content:**
```yaml
path: /absolute/path/to/dataset
train: images/train
val:   images/val
nc: 356
names: ['VESTLANDSLEFSA TØRRE 10STK 360G', 'COFFEE MATE 180G NESTLE', ..., 'unknown_product']
```
Note: `names` list must be sorted by `category_id` (0, 1, 2, ..., 355). Extract from `coco["categories"]` sorted by `id`.

---

## Step 2: Training — `train.py`

Fine-tune YOLOv8l from COCO-pretrained weights. Run locally with `ultralytics==8.1.0`.

**Install exact sandbox versions:**
```bash
pip install ultralytics==8.1.0
pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
```

**Key hyperparameters:**

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `model` | `yolov8l.pt` | Best accuracy/size tradeoff; ~87 MB, fits 420 MB limit |
| `imgsz` | `1280` | Shelf images are huge; products are small — high res is critical |
| `batch` | `4` | YOLOv8l at 1280px uses ~5–6 GB/image; 4×≈20–24 GB fits L4 with AMP |
| `epochs` | `100` | With early stopping; small dataset trains quickly |
| `patience` | `20` | Early stop if no improvement for 20 epochs |
| `nc` | `356` | All product categories including `unknown_product` |
| `mosaic` | `1.0` | Critical for small datasets — shows more products per forward pass |
| `copy_paste` | `0.3` | YOLO's built-in copy-paste augmentation |
| `amp` | `True` | Mixed precision — faster + lower memory |
| `conf` (inference) | `0.001` | Very low: let the evaluator pick threshold; maximizes recall for mAP |
| `max_det` | `500` | Max 235 annotations per image; 500 gives headroom |

**Augmentation settings:**
```python
mosaic=1.0, mixup=0.1, copy_paste=0.3,
degrees=5.0, translate=0.1, scale=0.5, fliplr=0.5, flipud=0.0,
hsv_h=0.015, hsv_s=0.7, hsv_v=0.4
```

**Output:** `runs/train/ngd_yolov8l_1280/weights/best.pt`

**If OOM:** Reduce to `batch=2`, `imgsz=1024`, or switch to `yolov8m.pt`.

---

## Step 3: Optional Enhancement — `augment_copypaste.py`

Use product reference images to synthetically augment rare classes (< 10 annotations in training split). This can improve tail-class classification mAP.

**Strategy:**
1. Build `product_code → category_id` mapping via fuzzy name matching (threshold 0.85) between `metadata.json` product names and `annotations.json` category names
2. Identify categories with fewer than 10 training annotations
3. For each rare category: load reference images (prefer `front.jpg` or `main.jpg`)
4. Remove white/clean background using threshold + morphological cleanup
5. Paste product crop at a plausible scale onto existing training shelf images
6. Update corresponding `.txt` label files with new annotation

**Skip if time-constrained** — YOLOv8's built-in `copy_paste=0.3` already handles this for existing crops.

---

## Step 4: Evaluate — `evaluate.py`

Run validation to inspect mAP before submission:
```python
from ultralytics import YOLO
model = YOLO("runs/train/ngd_yolov8l_1280/weights/best.pt")
metrics = model.val(data="dataset/data.yaml", imgsz=1280)
print(f"mAP50: {metrics.box.map50:.4f}")
```

---

## Step 5: Submission Entry Point — `run.py`

**Security-compliant imports** (no `os`, `sys`, `yaml`, `pickle`, etc.):
```python
import argparse
import json
from pathlib import Path
import torch
from ultralytics import YOLO
```

**Key design choices:**
- `conf=0.001` — very low threshold so the evaluator sees all detections (mAP sweeps thresholds)
- `iou=0.65` — NMS IoU threshold to prevent duplicate boxes
- `max_det=500` — headroom above the max 235 annotations per image
- `imgsz=1280` — match training resolution exactly
- `model.fuse()` — fuse Conv+BN for ~10% faster inference
- Output via `Path.write_text(json.dumps(...))` — no blocked imports

**image_id parsing:** `img_00042.jpg` → `int(Path(name).stem.split("_")[1])` → `42`

**Bbox conversion:** YOLO `[x1,y1,x2,y2]` → COCO `[x, y, w, h]` where `w = x2-x1`, `h = y2-y1`

---

## Step 6: Package Submission

**Structure:**
```
submission/
  run.py
  weights/
    best.onnx   (~175 MB for YOLOv8l FP32 ONNX)
```

**Windows PowerShell:**
```powershell
mkdir submission\weights
copy run.py submission\run.py
copy runs\train\ngd_yolov8l_1280\weights\best.onnx submission\weights\best.onnx
cd submission
Compress-Archive -Path .\* -DestinationPath ..\submission.zip
```

**Verify zip structure:**
```bash
unzip -l submission.zip | head -10
# Must show "run.py" at root — NOT "submission/run.py"
```

---

## Verification Checklist

- [ ] `python run.py --input data/NM_NGD_coco_dataset/train/images --output /tmp/test_predictions.json` runs without error
- [ ] Output is a valid JSON array with `image_id`, `category_id`, `bbox` (length 4), `score` fields
- [ ] All `category_id` values are integers in range 0–355
- [ ] `submission.zip` uncompressed size < 420 MB
- [ ] `run.py` appears at zip root (not inside a subfolder)
- [ ] No blocked imports in any submitted `.py` file

---

## Expected Score Range

| Approach | Det mAP@0.5 | Class mAP@0.5 | Score |
|----------|-------------|---------------|-------|
| Random baseline | ~0 | ~0 | ~0 |
| YOLOv8n pretrained (no fine-tune) | ~0.10–0.20 | ~0 | ~0.07–0.14 |
| YOLOv8l fine-tuned `imgsz=640` | ~0.50–0.65 | ~0.30–0.45 | ~0.44–0.59 |
| **YOLOv8l fine-tuned `imgsz=1280`** | **~0.60–0.75** | **~0.40–0.55** | **~0.54–0.69** |
| + copy-paste augmentation | ~0.65–0.80 | ~0.45–0.60 | ~0.59–0.74 |

---

## Critical Gotchas

1. **`nc=356` not 357** — annotations have `category_id` 0–355 (356 total). Category 355 = `unknown_product`.
2. **Image filename gaps** — IDs like 3, 8, 16 are missing; always parse `image_id` from filename, never assume sequential.
3. **Mixed extensions** — both `.jpg` and `.jpeg` exist; handle both everywhere.
4. **Blocked imports in run.py** — no `os`, `sys`, `subprocess`, `yaml`, `pickle`, `shutil`. Use `pathlib` + `json`.
5. **Pin `ultralytics==8.1.0`** — weights from 8.2+ fail to load in the sandbox.
6. **Use `conf=0.001`** — NOT the default 0.25; mAP computation sweeps all confidence thresholds.
7. **Zip contents, not folder** — `run.py` must appear at the zip root directly.
