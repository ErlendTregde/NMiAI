"""
evaluate.py
-----------
Validate the trained YOLOv8l model on the val split and print mAP metrics.
Also runs a quick local inference test to verify run.py output format.

Run after training:
  python evaluate.py

Optionally point to a specific checkpoint:
  python evaluate.py --weights runs/train/ngd_yolov8l_1280/weights/best.pt
"""

import argparse
import json
from pathlib import Path

from ultralytics import YOLO

ROOT      = Path(__file__).parent
DATA_YAML = ROOT / "dataset" / "data.yaml"
DEFAULT_WEIGHTS = ROOT / "runs" / "train" / "ngd_yolov8l_1280" / "weights" / "best.pt"


def validate(weights: Path, imgsz: int = 1280):
    """Run YOLO validation and print metrics."""
    print(f"\n{'='*60}")
    print(f"Validating: {weights}")
    print(f"{'='*60}")

    model = YOLO(str(weights))
    metrics = model.val(
        data=str(DATA_YAML),
        imgsz=imgsz,
        batch=4,
        conf=0.001,    # Low threshold — matches submission inference
        iou=0.65,
        verbose=True,
        plots=True,
    )

    print(f"\n{'─'*40}")
    print(f"  mAP@0.5      (detection):      {metrics.box.map50:.4f}")
    print(f"  mAP@0.5-0.95 (detection):      {metrics.box.map:.4f}")
    print(f"  Precision:                      {metrics.box.mp:.4f}")
    print(f"  Recall:                         {metrics.box.mr:.4f}")

    # Estimate competition score (detection only — classification mAP requires
    # correct category matching which val() measures via per-class AP)
    det_map = metrics.box.map50
    # For classification mAP: val() already computes per-class mAP50 which
    # corresponds to the competition's classification_mAP when category_ids match.
    # A rough upper-bound: assume classification ≈ 0.6 * detection (empirical).
    est_cls_map = det_map * 0.6
    est_score   = 0.7 * det_map + 0.3 * est_cls_map
    print(f"\n  Estimated competition score ≈  {est_score:.4f}")
    print(f"    (0.7 × det_mAP={det_map:.3f} + 0.3 × ~cls_mAP={est_cls_map:.3f})")
    print(f"{'─'*40}\n")

    return metrics


def test_inference(weights: Path, imgsz: int = 1280, n_images: int = 5):
    """Run inference on a few training images and verify output format."""
    print(f"\n{'='*60}")
    print(f"Quick inference test (first {n_images} images)")
    print(f"{'='*60}")

    img_dir = ROOT / "data" / "NM_NGD_coco_dataset" / "train" / "images"
    if not img_dir.exists():
        print(f"  Skipping: image dir not found at {img_dir}")
        return

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model  = YOLO(str(weights))
    model.fuse()

    img_files = sorted(img_dir.iterdir())[:n_images]
    predictions = []

    for img_path in img_files:
        if img_path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        image_id = int(img_path.stem.split("_")[1])
        results  = model.predict(
            source=str(img_path),
            imgsz=imgsz,
            conf=0.001,
            iou=0.65,
            device=device,
            verbose=False,
            max_det=500,
        )
        for r in results:
            if r.boxes is None:
                continue
            for i in range(len(r.boxes)):
                x1, y1, x2, y2 = r.boxes.xyxy[i].cpu().tolist()
                predictions.append({
                    "image_id":    image_id,
                    "category_id": int(r.boxes.cls[i].cpu().item()),
                    "bbox":        [round(x1, 2), round(y1, 2),
                                    round(x2 - x1, 2), round(y2 - y1, 2)],
                    "score":       round(float(r.boxes.conf[i].cpu().item()), 6),
                })

    print(f"  Total predictions from {n_images} images: {len(predictions)}")
    if predictions:
        p = predictions[0]
        print(f"  Sample: image_id={p['image_id']}, category_id={p['category_id']}, "
              f"bbox={p['bbox']}, score={p['score']:.4f}")

    # Validate format
    required_keys = {"image_id", "category_id", "bbox", "score"}
    for p in predictions:
        assert required_keys <= p.keys(), f"Missing keys: {required_keys - p.keys()}"
        assert isinstance(p["image_id"], int)
        assert isinstance(p["category_id"], int)
        assert 0 <= p["category_id"] <= 355, f"category_id out of range: {p['category_id']}"
        assert len(p["bbox"]) == 4
        assert 0.0 <= p["score"] <= 1.0

    print("  Format validation: OK")

    # Save sample output
    sample_out = ROOT / "sample_predictions.json"
    sample_out.write_text(json.dumps(predictions[:50], indent=2), encoding="utf-8")
    print(f"  Saved first 50 predictions to {sample_out}")


def main():
    parser = argparse.ArgumentParser(description="Evaluate trained YOLOv8l model")
    parser.add_argument(
        "--weights",
        type=Path,
        default=DEFAULT_WEIGHTS,
        help=f"Path to .pt weights file (default: {DEFAULT_WEIGHTS})",
    )
    parser.add_argument("--imgsz", type=int, default=1280)
    args = parser.parse_args()

    if not args.weights.exists():
        print(f"ERROR: weights not found at {args.weights}")
        print("Make sure training has completed: python train.py")
        return

    validate(args.weights, imgsz=args.imgsz)
    test_inference(args.weights, imgsz=args.imgsz)

    print(f"\nNext steps:")
    print(f"  1. Copy weights:  cp {args.weights} submission/weights/best.pt")
    print(f"  2. Test run.py:   python run.py --input data/NM_NGD_coco_dataset/train/images --output /tmp/preds.json")
    print(f"  3. Package:       cd submission && Compress-Archive -Path .\\* -DestinationPath ..\\submission.zip")


if __name__ == "__main__":
    main()
