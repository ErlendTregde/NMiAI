# NorgesGruppen Object Detection

Norwegian AI Championship 2026 — Grocery product detection and classification on store shelf images.

**Final score: 0.9129** (top ~60 of 352 teams)

## Task

Detect and classify grocery products on store shelf images.

```
Score = 0.7 × detection_mAP@0.5 + 0.3 × classification_mAP@0.5
```

- 248 training images, 356 product categories, ~22,700 bounding box annotations
- Inference runs inside a sandboxed Docker container (NVIDIA L4 24 GB, 300 s timeout, max 420 MB)

## Approach

**3-model ensemble with WBF + TTA**

| Model | Training data | Notes |
|---|---|---|
| YOLOv8x FP16 | 951 images (copy-paste augmented) | Strong detection backbone |
| YOLOv8l FP16 | 951 images (copy-paste augmented) | Ensemble diversity |
| YOLO11x FP16 | 248 clean images | C2PSA spatial attention, better fine-grained classification |

Inference strategy:
- **3-pass TTA**: original + horizontal flip + scale×1.2
- **WBF** (Weighted Boxes Fusion): merges TTA predictions across all 3 models (9 prediction sets total)
- **Reference matching** (post-processing): MobileNetV3-Small cosine similarity against 1558 reference product images overrides uncertain YOLO classifications (score < 0.5)

## Repository structure

```
submission/
  run.py                      # Competition inference script (submitted as-is)
  weights/
    ref_embeddings.json       # Pre-computed MobileNetV3 reference embeddings
    ref_labels.json           # Category IDs for reference images
    pca_components.json       # PCA-64 projection parameters

train.py                      # YOLOv8x/l training (951-image augmented dataset)
train_yolo11x.py              # YOLO11x training (248 clean images)
train_yolo11x_v4.py           # YOLO11x with proper 199-train / 49-val split
train_rtdetr.py               # RT-DETR-l training (experimental)
train_crop_classifier.py      # YOLO11m-cls crop classifier (experimental)
build_reference_embeddings.py # Builds MobileNetV3 reference embeddings from product images
augment_copypaste.py          # Copy-paste augmentation to expand training set
convert_coco_to_yolo.py       # COCO JSON → YOLO label format conversion
evaluate.py                   # Local mAP evaluation against val set
```

## Setup

```bash
uv sync
```

## Data

Download from the competition and place in `data/`:
```
data/
  NM_NGD_coco_dataset/        # 248 shelf images + COCO annotations
  NM_NGD_product_images/      # 327 reference product images
```

Then convert to YOLO format:
```bash
uv run convert_coco_to_yolo.py    # creates dataset_clean/
uv run augment_copypaste.py       # creates dataset/ (951 images)
```

## Training

```bash
uv run train.py              # YOLOv8x on 951 augmented images
uv run train_yolo11x.py      # YOLO11x on 248 clean images
uv run build_reference_embeddings.py  # build reference embeddings
```

Export weights to `submission/weights/` as FP16 ONNX (each script handles this).

## Inference

```bash
python submission/run.py --input /path/to/images --output predictions.json
```
