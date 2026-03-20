"""
train.py
--------
Fine-tune YOLOv8l on the NM NGD grocery detection dataset.

Prerequisites:
  1. Run convert_coco_to_yolo.py first to create dataset/
  2. Install matching versions:
       pip install ultralytics==8.1.0
       pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124

Run:
  python train.py

The best checkpoint is saved to:
  runs/train/ngd_yolov8l_1280/weights/best.pt
"""

from pathlib import Path
from ultralytics import YOLO

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent
DATA_YAML = ROOT / "dataset" / "data.yaml"

# ── Model ─────────────────────────────────────────────────────────────────────
# Start from COCO-pretrained YOLOv8l (~87 MB, nc=80 pretrained → fine-tune to nc=356)
# ultralytics automatically adapts the head when nc differs from pretraining.
MODEL_WEIGHTS = "yolov8l.pt"

# ── Hyperparameters ───────────────────────────────────────────────────────────
# Tuned for:
#   - Small dataset (248 shelf images → ~198 train after 80/20 split)
#   - Large images (up to 5712×4624 px); products are small relative to image size
#   - 356-class fine-grained classification
#
# Memory budget: YOLOv8l at imgsz=1280 with AMP ≈ 5–6 GB/image
#   batch=4 → ~20–24 GB, fits NVIDIA L4 (24 GB) with AMP
#   If OOM: reduce batch=2 or imgsz=1024

TRAIN_ARGS = dict(
    data       = str(DATA_YAML),
    epochs     = 100,
    patience   = 20,           # Early stopping: halt if no mAP50 improvement for 20 epochs
    imgsz      = 1280,         # High resolution — critical for detecting small products
    batch      = 4,            # Adjust if OOM (try batch=2 or imgsz=1024)
    device     = "0" if __import__("torch").cuda.is_available() else "cpu",
    workers    = 4,
    amp        = True,         # Mixed precision (FP16) — smaller memory + faster

    # ── Augmentation ──────────────────────────────────────────────────────────
    mosaic     = 1.0,          # Tile 4 images → model sees more products per pass
    mixup      = 0.1,          # Light blending of two images
    copy_paste = 0.3,          # Copy objects from one image and paste into another
    degrees    = 5.0,          # Small rotation (products are upright on shelves)
    translate  = 0.1,          # Random translation ±10%
    scale      = 0.5,          # Random scale ±50%
    fliplr     = 0.5,          # Horizontal flip 50%
    flipud     = 0.0,          # No vertical flip (shelves have defined up/down)
    hsv_h      = 0.015,        # Hue jitter
    hsv_s      = 0.7,          # Saturation jitter
    hsv_v      = 0.4,          # Value/brightness jitter

    # ── LR schedule ───────────────────────────────────────────────────────────
    optimizer      = "SGD",
    lr0            = 0.01,     # Initial learning rate
    lrf            = 0.01,     # Final LR = lr0 * lrf (cosine decay to 0.0001)
    momentum       = 0.937,
    weight_decay   = 0.0005,
    warmup_epochs  = 3.0,      # Gradual LR warm-up for first 3 epochs
    close_mosaic   = 10,       # Disable mosaic for last 10 epochs (stabilize)

    # ── Saving & logging ──────────────────────────────────────────────────────
    project     = "runs/train",
    name        = "ngd_yolov8l_1280",
    save_period = 10,          # Save checkpoint every 10 epochs
    val         = True,        # Validate after each epoch
    plots       = True,        # Save training plots (loss curves, mAP, etc.)
    verbose     = True,
)


def main():
    assert DATA_YAML.exists(), (
        f"Dataset not found: {DATA_YAML}\n"
        "Run `python convert_coco_to_yolo.py` first."
    )

    print(f"Loading model: {MODEL_WEIGHTS}")
    model = YOLO(MODEL_WEIGHTS)

    print(f"\nStarting training with imgsz={TRAIN_ARGS['imgsz']}, "
          f"batch={TRAIN_ARGS['batch']}, epochs={TRAIN_ARGS['epochs']}")
    print(f"Data: {DATA_YAML}\n")

    results = model.train(**TRAIN_ARGS)

    best_pt = Path(results.save_dir) / "weights" / "best.pt"
    print(f"\nTraining complete.")
    print(f"Best weights: {best_pt}")
    print(f"Val mAP50:    {results.results_dict.get('metrics/mAP50(B)', 'N/A'):.4f}")
    print(f"Val mAP50-95: {results.results_dict.get('metrics/mAP50-95(B)', 'N/A'):.4f}")

    # ── Export to ONNX for submission ─────────────────────────────────────────
    # ONNX is version-independent: works in the sandbox regardless of which
    # ultralytics version you trained with locally.
    # opset=17 is required by onnxruntime-gpu 1.20.0 (opset > 20 is not supported).
    # half=False: FP32 ONNX — fully compatible; size ~175 MB (well within 420 MB limit).
    print(f"\nExporting best checkpoint to ONNX (opset=17) ...")
    best_model = YOLO(str(best_pt))
    onnx_path  = best_model.export(
        format       = "onnx",
        imgsz        = TRAIN_ARGS["imgsz"],
        half         = False,   # FP32: maximally compatible with onnxruntime 1.20.0
        opset        = 17,      # Required: sandbox has onnxruntime 1.20.0 (supports ≤ opset 20)
        dynamic      = False,   # Fixed batch size = 1 (safer for onnxruntime)
        simplify     = True,    # onnxsim: removes redundant ops
    )
    print(f"ONNX model saved: {onnx_path}")
    print(f"\nNext steps:")
    print(f"  python evaluate.py --weights {best_pt}")
    print(f"  Then package: copy {onnx_path} → submission/weights/best.onnx")


if __name__ == "__main__":
    main()
