"""
train_yolo11x.py
----------------
Fine-tune YOLO11x on the full 951-image NM NGD dataset (951 train + 49 val).

YOLO11x vs YOLOv8x:
  - 54.7% vs 53.9% COCO mAP (better accuracy)
  - 56.97M vs 68.2M params (smaller, fits budget better)
  - C2PSA spatial attention blocks → better fine-grained classification
  - FP16 ONNX: ~113MB vs ~132MB for YOLOv8x

Prerequisites:
  yolo11x.pt must exist in the working directory (auto-downloaded on first run).

Run:
  uv run train_yolo11x.py

Output:
  submission/weights/yolo11x_fp16.onnx   ~113 MB  (weight file)
"""

import shutil
from pathlib import Path

from ultralytics import YOLO

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent
DATA_YAML = ROOT / "dataset_clean" / "data.yaml"   # 248 clean shelf images (no noisy augmentation)

# ── Model ─────────────────────────────────────────────────────────────────────
MODEL_WEIGHTS = "yolo11x.pt"   # 56.97M params, C2PSA spatial attention

# ── Hyperparameters ───────────────────────────────────────────────────────────
# epochs=200 (more than YOLOv8x's 150) because dataset is 248 vs 951 images:
# fewer images per epoch → needs more passes to see the same total data.
# All other settings match train.py exactly for consistent comparison.
TRAIN_ARGS = dict(
    data       = str(DATA_YAML),
    epochs     = 300,          # More epochs: small 248-image dataset needs more passes
    patience   = 80,           # Wider window — cos_lr needs time to converge
    imgsz      = 1280,         # High resolution — critical for small shelf products
    batch      = 3,            # YOLO11x at imgsz=1280: ~7-8 GB/img → batch=3 on V100 32GB
    device     = "0" if __import__("torch").cuda.is_available() else "cpu",
    workers    = 4,
    amp        = True,         # Mixed precision — smaller memory + faster

    # ── Augmentation ──────────────────────────────────────────────────────────
    mosaic     = 1.0,          # Tile 4 shelf images → richer scenes
    mixup      = 0.15,         # Light blending (slightly higher than v1)
    copy_paste = 0.5,          # YOLO's built-in copy_paste uses real shelf objects
    degrees    = 5.0,          # Small rotation (products are upright)
    translate  = 0.1,
    scale      = 0.5,
    fliplr     = 0.5,
    flipud     = 0.0,          # No vertical flip
    hsv_h      = 0.015,
    hsv_s      = 0.7,
    hsv_v      = 0.4,
    close_mosaic = 30,         # Keep mosaic on for 270/300 epochs → better generalisation

    # ── LR schedule ───────────────────────────────────────────────────────────
    optimizer      = "SGD",
    lr0            = 0.01,
    lrf            = 0.001,    # Cosine decay to lr0 * lrf = 0.00001 (lower final LR)
    cos_lr         = True,     # Cosine LR schedule — smoother convergence on small dataset
    momentum       = 0.937,
    weight_decay   = 0.0005,
    warmup_epochs  = 3.0,

    # ── Saving & logging ──────────────────────────────────────────────────────
    project     = "runs/train",
    name        = "ngd_yolo11x_1280_v3",
    save_period = 20,
    val         = True,
    plots       = True,
    verbose     = True,
)


def main():
    assert DATA_YAML.exists(), (
        f"Dataset not found: {DATA_YAML}\n"
        "Run `python convert_coco_to_yolo.py` first."
    )

    print(f"Loading model: {MODEL_WEIGHTS}")
    model = YOLO(MODEL_WEIGHTS)

    print(f"\nStarting YOLO11x training")
    print(f"  model={MODEL_WEIGHTS}  imgsz={TRAIN_ARGS['imgsz']}")
    print(f"  batch={TRAIN_ARGS['batch']}  epochs={TRAIN_ARGS['epochs']}")
    print(f"  data={DATA_YAML}  (248 clean images, cos_lr=True)\n")

    results = model.train(**TRAIN_ARGS)

    best_pt = Path(results.save_dir) / "weights" / "best.pt"

    # Fallback: search by name if save_dir is unexpected
    if not best_pt.exists():
        for candidate in Path("/home/coder").rglob("ngd_yolo11x_1280/weights/best.pt"):
            best_pt = candidate
            break

    if not best_pt.exists():
        print("ERROR: best.pt not found — check training output above for save_dir")
        return

    print(f"\nBest weights: {best_pt}")
    print(f"Val mAP50:    {results.results_dict.get('metrics/mAP50(B)', 'N/A')}")

    # ── Export to ONNX FP16 ───────────────────────────────────────────────────
    print("\nExporting to ONNX FP16 (opset=17) …")
    export_model = YOLO(str(best_pt))
    onnx_path = export_model.export(
        format   = "onnx",
        imgsz    = TRAIN_ARGS["imgsz"],
        half     = True,      # FP16: ~113MB for YOLO11x
        opset    = 17,        # sandbox: onnxruntime-gpu 1.20.0 (supports opset ≤ 20)
        dynamic  = False,     # Fixed batch=1
        simplify = True,
    )

    dst = ROOT / "submission" / "weights" / "yolo11x_fp16.onnx"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_path, dst)
    print(f"Saved: {dst} ({dst.stat().st_size / 1e6:.1f} MB)")

    # ── Verify ONNX ───────────────────────────────────────────────────────────
    import onnxruntime as ort
    sess = ort.InferenceSession(str(dst), providers=["CPUExecutionProvider"])
    inp  = sess.get_inputs()[0]
    out  = sess.get_outputs()[0]
    print(f"\nONNX input  shape: {inp.shape}  dtype: {inp.type}")
    print(f"ONNX output shape: {out.shape}  dtype: {out.type}")
    print("Expected output shape: [1, 360, ~8400]  (4+nc=360, anchors depends on imgsz)")

    # ── Budget check ──────────────────────────────────────────────────────────
    weights_dir = ROOT / "submission" / "weights"
    onnx_files  = sorted(weights_dir.glob("*.onnx"))
    total_mb    = sum(f.stat().st_size for f in onnx_files) / 1e6
    print(f"\nCurrent submission/weights/ ONNX files ({len(onnx_files)}/3 max, {total_mb:.0f}MB/420MB):")
    for f in onnx_files:
        print(f"  {f.name}: {f.stat().st_size/1e6:.1f} MB")

    print("\nNext: package final ensemble zip (3 ONNX files)")


if __name__ == "__main__":
    main()
