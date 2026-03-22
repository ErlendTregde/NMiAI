"""
train_rtdetr.py — Fine-tune RT-DETR-l on the full NM NGD dataset.

Fix v2: corrected learning rate and dataset.
  Previous run (lr=2.8e-05, 248 images) showed mAP=0 after 32 epochs because:
  - optimizer=auto with batch=2 auto-scaled AdamW LR to 2.8e-05 (too low)
  - Only 248 clean images (transformers need more data)
  This version uses explicit AdamW with lr=1e-4 and the full 951-image dataset.

Usage:
  uv run train_rtdetr.py

Output:
  submission/weights/rtdetr_l_fp16.onnx   ~70 MB (weight file)
"""

import shutil
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent
DATASET_DIR = ROOT / "dataset"
DATA_YAML   = DATASET_DIR / "data.yaml"   # 951 train + 49 val images

MODEL_WEIGHTS = "rtdetr-l.pt"
IMGSZ         = 1280
BATCH         = 4       # V100 32GB can handle batch=4 (~17GB); was 2 → better gradients
EPOCHS        = 150
PATIENCE      = 30
PROJECT       = "runs/train"
MODEL_NAME    = "ngd_rtdetr_l_1280_v2"   # new name to avoid conflict with previous runs


# ── Training ─────────────────────────────────────────────────────────────────

def main() -> None:
    from ultralytics import YOLO

    assert DATA_YAML.exists(), f"Dataset not found: {DATA_YAML}"

    print(f"\nStarting RT-DETR-l training (v2 — fixed LR + full dataset)")
    print(f"  model={MODEL_WEIGHTS}  imgsz={IMGSZ}  batch={BATCH}  epochs={EPOCHS}")
    print(f"  data={DATA_YAML}  (951 train + 49 val images)")
    print(f"  optimizer=AdamW  lr0=0.0001  cos_lr=True")

    model = YOLO(MODEL_WEIGHTS)
    model.train(
        data=str(DATA_YAML),
        epochs=EPOCHS,
        imgsz=IMGSZ,
        batch=BATCH,
        patience=PATIENCE,
        device=0,
        project=PROJECT,
        name=MODEL_NAME,
        exist_ok=True,
        save_period=20,
        workers=4,
        amp=True,
        # ── Fixed LR — explicit AdamW so lr0 is used directly (no auto-scaling) ──
        optimizer="AdamW",     # was "auto" → produced AdamW(lr=2.8e-05), too slow
        lr0=0.0001,            # direct AdamW LR for fine-tuning (was 0.01, auto-scaled to 2.8e-5)
        lrf=0.01,              # cosine decay to lr0 * lrf = 1e-6
        cos_lr=True,           # cosine LR schedule (better for transformers)
        warmup_epochs=1.0,     # short warmup (pretrained weights, no need for 3 epochs)
        weight_decay=0.0005,
        # ── Augmentations ────────────────────────────────────────────────────
        mosaic=1.0,
        fliplr=0.5,
        hsv_h=0.015,
        hsv_s=0.7,
        hsv_v=0.4,
        degrees=5.0,
        translate=0.1,
        scale=0.5,
        mixup=0.0,
        copy_paste=0.0,        # RT-DETR handles this differently
    )

    # ── Locate best.pt (training saves relative to cwd) ──────────────────────
    best_pt: Path | None = None
    for candidate in [
        ROOT / PROJECT / MODEL_NAME / "weights" / "best.pt",
        Path(PROJECT) / MODEL_NAME / "weights" / "best.pt",
        *Path("/home/coder").rglob(f"{MODEL_NAME}/weights/best.pt"),
    ]:
        if Path(candidate).exists():
            best_pt = Path(candidate)
            break

    if best_pt is None:
        print("ERROR: best.pt not found — check training output above for save_dir")
        return

    print(f"\nBest weights: {best_pt}")

    # ── Export to ONNX FP16 ───────────────────────────────────────────────────
    print("Exporting to ONNX FP16 …")
    export_model = YOLO(str(best_pt))
    export_model.export(
        format="onnx",
        imgsz=IMGSZ,
        half=True,
        opset=17,
        simplify=True,
        dynamic=False,
    )
    onnx_path = best_pt.with_suffix(".onnx")

    dst = ROOT / "submission" / "weights" / "rtdetr_l_fp16.onnx"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(onnx_path, dst)
    print(f"Saved: {dst} ({dst.stat().st_size / 1e6:.1f} MB)")

    # ── Verify ONNX output shape ──────────────────────────────────────────────
    import onnxruntime as ort
    sess   = ort.InferenceSession(str(dst), providers=["CPUExecutionProvider"])
    inp    = sess.get_inputs()[0]
    out    = sess.get_outputs()[0]
    print(f"\nONNX input  shape: {inp.shape}   dtype: {inp.type}")
    print(f"ONNX output shape: {out.shape}   dtype: {out.type}")
    print("Expected: output[1] = 4+nc = 360, output[2] = num_queries (300 for RT-DETR)")
    print("\nNext: package submission with YOLOv8x + YOLO11x + RT-DETR-l ensemble.")


if __name__ == "__main__":
    main()
