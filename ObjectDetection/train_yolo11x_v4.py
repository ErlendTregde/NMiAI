"""
train_yolo11x_v4.py
-------------------
Train YOLO11x with a PROPER train/val split:
  - Train: 199 clean images (never seen in val)
  - Val:   49 original competition val images (separate from train)

Why v4 is better than v1/v2/v3:
  - v1/v2/v3: val = train (same 248 images) → mAP=0.98 is fake, no real early stopping
  - v4: val = 49 DIFFERENT images → real generalization measurement, proper early stopping
  - label_smoothing=0.1: prevents overconfident classification → better mAP on test

Run:
    uv run train_yolo11x_v4.py

Output:
    submission/weights/yolo11x_v4_fp16.onnx   ~115 MB
"""

import shutil
from pathlib import Path

from ultralytics import YOLO

ROOT      = Path(__file__).parent
DATA_YAML = ROOT / "dataset_split" / "data.yaml"   # 199 train (no val overlap) + 49 val

MODEL_WEIGHTS = "yolo11x.pt"

TRAIN_ARGS = dict(
    data       = str(DATA_YAML),
    epochs     = 300,
    patience   = 50,           # Tighter: real val signal → stop when genuinely not improving
    imgsz      = 1280,
    batch      = 3,
    device     = "0" if __import__("torch").cuda.is_available() else "cpu",
    workers    = 4,
    amp        = True,

    # ── Augmentation (same as v1 which gave 0.9129) ───────────────────────────
    mosaic       = 1.0,
    mixup        = 0.15,
    copy_paste   = 0.5,
    degrees      = 5.0,
    translate    = 0.1,
    scale        = 0.5,
    fliplr       = 0.5,
    flipud       = 0.0,
    hsv_h        = 0.015,
    hsv_s        = 0.7,
    hsv_v        = 0.4,
    close_mosaic = 30,

    # ── LR schedule (same as v32 which was stable) ────────────────────────────
    optimizer      = "SGD",
    lr0            = 0.01,
    lrf            = 0.001,
    cos_lr         = True,
    momentum       = 0.937,
    weight_decay   = 0.0005,
    warmup_epochs  = 3.0,

    # ── Classification improvement ────────────────────────────────────────────
    label_smoothing = 0.1,    # Prevents overconfident class predictions; key for 356-class

    # ── Saving ────────────────────────────────────────────────────────────────
    project     = "runs/train",
    name        = "ngd_yolo11x_1280_v4",
    save_period = 20,
    val         = True,
    plots       = True,
    verbose     = True,
)


def main():
    assert DATA_YAML.exists(), f"Dataset not found: {DATA_YAML}"

    print(f"Loading model: {MODEL_WEIGHTS}")
    model = YOLO(MODEL_WEIGHTS)

    print(f"\nStarting YOLO11x v4 training")
    print(f"  data={DATA_YAML}  (199 train / 49 val — PROPER SPLIT)")
    print(f"  imgsz={TRAIN_ARGS['imgsz']}  batch={TRAIN_ARGS['batch']}  epochs={TRAIN_ARGS['epochs']}")
    print(f"  label_smoothing=0.1  cos_lr=True  patience=50\n")

    results = model.train(**TRAIN_ARGS)

    best_pt = Path(results.save_dir) / "weights" / "best.pt"
    if not best_pt.exists():
        for c in Path("/home/coder").rglob("ngd_yolo11x_1280_v4/weights/best.pt"):
            best_pt = c
            break

    if not best_pt.exists():
        print("ERROR: best.pt not found")
        return

    val_map = results.results_dict.get("metrics/mAP50(B)", "N/A")
    print(f"\nBest weights: {best_pt}")
    print(f"Val mAP50 (on 49 REAL val images): {val_map}")

    # ── Export to ONNX FP16 ───────────────────────────────────────────────────
    print("\nExporting to ONNX FP16 (opset=17) …")
    export_model = YOLO(str(best_pt))
    onnx_path = export_model.export(
        format   = "onnx",
        imgsz    = TRAIN_ARGS["imgsz"],
        half     = True,
        opset    = 17,
        dynamic  = False,
        simplify = True,
    )

    dst = ROOT / "submission" / "weights" / "yolo11x_v4_fp16.onnx"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_path, dst)
    print(f"Saved: {dst} ({dst.stat().st_size / 1e6:.1f} MB)")

    import onnxruntime as ort
    sess = ort.InferenceSession(str(dst), providers=["CPUExecutionProvider"])
    inp  = sess.get_inputs()[0]
    out  = sess.get_outputs()[0]
    print(f"ONNX input  shape: {inp.shape}  dtype: {inp.type}")
    print(f"ONNX output shape: {out.shape}  dtype: {out.type}")


if __name__ == "__main__":
    main()
