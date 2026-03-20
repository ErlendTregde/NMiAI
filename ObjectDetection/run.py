"""
run.py — NorgesGruppen Object Detection submission entry point (ONNX version).

Executed in the sandbox as:
  python run.py --input /data/images --output /output/predictions.json

Output format (COCO-style JSON array):
  [{"image_id": 42, "category_id": 7, "bbox": [x, y, w, h], "score": 0.91}, ...]

Security-compliant:
  - No imports of os, sys, subprocess, socket, pickle, yaml, requests,
    multiprocessing, shutil, or any other blocked module.
  - Uses pathlib.Path exclusively for file operations.
  - Uses json exclusively for serialization.

Model format: ONNX (opset 17, FP32)
  Loaded with onnxruntime CUDAExecutionProvider (pre-installed in sandbox).
"""

import argparse
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

# ── Inference constants ───────────────────────────────────────────────────────
IMGSZ        = 1280     # Must match the imgsz used during export
CONF_THRESH  = 0.001   # Very low: mAP evaluation sweeps thresholds — keep all detections
IOU_THRESH   = 0.65    # NMS IoU threshold
MAX_DET      = 500     # Max detections per image (training data has up to 235 annotations)
PAD_VALUE    = 114     # Letterbox fill colour (YOLO standard)


# ── Image preprocessing ───────────────────────────────────────────────────────

def letterbox(img_rgb: np.ndarray, target: int = IMGSZ):
    """
    Resize image to target×target with letterboxing (maintain aspect ratio, pad).
    Returns (padded_image, scale, pad_top, pad_left).
    """
    h, w = img_rgb.shape[:2]
    scale = target / max(h, w)
    new_h = int(round(h * scale))
    new_w = int(round(w * scale))

    pil_resized = Image.fromarray(img_rgb).resize((new_w, new_h), Image.BILINEAR)
    img_resized = np.array(pil_resized)

    canvas   = np.full((target, target, 3), PAD_VALUE, dtype=np.uint8)
    pad_top  = (target - new_h) // 2
    pad_left = (target - new_w) // 2
    canvas[pad_top : pad_top + new_h, pad_left : pad_left + new_w] = img_resized

    return canvas, scale, pad_top, pad_left


def preprocess(img_path: Path):
    """Load image and return (NCHW float32 tensor, scale, pad_top, pad_left, orig_h, orig_w)."""
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img)
    orig_h, orig_w = arr.shape[:2]

    padded, scale, pad_top, pad_left = letterbox(arr, IMGSZ)

    # HWC uint8 [0,255] → NCHW float32 [0,1]
    tensor = padded.astype(np.float32) / 255.0
    tensor = np.transpose(tensor, (2, 0, 1))   # HWC → CHW
    tensor = tensor[np.newaxis, ...]            # CHW → NCHW

    return tensor, scale, pad_top, pad_left, orig_h, orig_w


# ── NMS ───────────────────────────────────────────────────────────────────────

def nms(boxes_xyxy: np.ndarray, scores: np.ndarray, iou_thresh: float) -> list:
    """
    Greedy NMS. boxes_xyxy: [N,4] (x1,y1,x2,y2), scores: [N].
    Returns list of kept indices, sorted by descending score.
    """
    if len(boxes_xyxy) == 0:
        return []

    x1, y1, x2, y2 = boxes_xyxy[:, 0], boxes_xyxy[:, 1], boxes_xyxy[:, 2], boxes_xyxy[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep  = []

    while len(order):
        i = order[0]
        keep.append(i)
        if len(order) == 1:
            break
        xx1  = np.maximum(x1[i], x1[order[1:]])
        yy1  = np.maximum(y1[i], y1[order[1:]])
        xx2  = np.minimum(x2[i], x2[order[1:]])
        yy2  = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou   = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[1:][iou <= iou_thresh]

    return keep


# ── YOLOv8 ONNX output decoding ───────────────────────────────────────────────

def decode(
    output:    np.ndarray,
    scale:     float,
    pad_top:   int,
    pad_left:  int,
    orig_h:    int,
    orig_w:    int,
) -> list[dict]:
    """
    Decode YOLOv8 ONNX output tensor to COCO-format prediction dicts.

    YOLOv8 ONNX standard export shape: [1, 4+nc, num_anchors]
      - output[0, :4, :]  → cx, cy, w, h  in PADDED pixel space (0..IMGSZ)
      - output[0, 4:, :]  → class scores  (sigmoid already applied by ultralytics)
    """
    pred = output[0].T              # [num_anchors, 4+nc]
    boxes_raw   = pred[:, :4]      # cx, cy, w, h (padded pixel coords)
    class_prob  = pred[:, 4:]      # [num_anchors, nc]

    # Best class per anchor
    class_ids  = np.argmax(class_prob, axis=1).astype(np.int32)
    class_conf = class_prob[np.arange(len(class_ids)), class_ids]

    # Confidence filter
    mask = class_conf >= CONF_THRESH
    if not np.any(mask):
        return []

    boxes_raw  = boxes_raw[mask]
    class_ids  = class_ids[mask]
    class_conf = class_conf[mask]

    # cx,cy,w,h → x1,y1,x2,y2  (padded pixel space)
    x1 = boxes_raw[:, 0] - boxes_raw[:, 2] / 2
    y1 = boxes_raw[:, 1] - boxes_raw[:, 3] / 2
    x2 = boxes_raw[:, 0] + boxes_raw[:, 2] / 2
    y2 = boxes_raw[:, 1] + boxes_raw[:, 3] / 2
    boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)

    # NMS (class-agnostic — per-class mAP is handled by correct category_id)
    keep = nms(boxes_xyxy, class_conf, IOU_THRESH)
    if not keep:
        return []

    boxes_xyxy = boxes_xyxy[keep]
    class_ids  = class_ids[keep]
    class_conf = class_conf[keep]

    # Map padded-image coords → original image coords
    #   Step 1: remove letterbox padding
    boxes_xyxy[:, [0, 2]] -= pad_left
    boxes_xyxy[:, [1, 3]] -= pad_top
    #   Step 2: undo resize scale
    boxes_xyxy /= scale
    #   Step 3: clip to original image bounds
    boxes_xyxy[:, [0, 2]] = np.clip(boxes_xyxy[:, [0, 2]], 0, orig_w)
    boxes_xyxy[:, [1, 3]] = np.clip(boxes_xyxy[:, [1, 3]], 0, orig_h)

    # Truncate to MAX_DET (keep highest-confidence)
    if len(keep) > MAX_DET:
        top_idx    = np.argsort(class_conf)[::-1][:MAX_DET]
        boxes_xyxy = boxes_xyxy[top_idx]
        class_ids  = class_ids[top_idx]
        class_conf = class_conf[top_idx]

    # Build COCO prediction dicts
    results = []
    for (bx1, by1, bx2, by2), cat_id, score in zip(boxes_xyxy, class_ids, class_conf):
        bw = bx2 - bx1
        bh = by2 - by1
        if bw <= 0 or bh <= 0:
            continue
        results.append({
            "category_id": int(cat_id),
            "bbox":  [round(float(bx1), 2), round(float(by1), 2),
                      round(float(bw),  2), round(float(bh),  2)],
            "score": round(float(score), 6),
        })

    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="NM NGD product detection inference")
    parser.add_argument("--input",  required=True, help="Directory containing test images")
    parser.add_argument("--output", required=True, help="Output path for predictions.json")
    return parser.parse_args()


def get_image_id(filename: str) -> int:
    """img_00042.jpg  →  42"""
    return int(Path(filename).stem.split("_")[1])


def main():
    args = parse_args()

    input_dir   = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Load ONNX model ───────────────────────────────────────────────────────
    weights_path = Path(__file__).parent / "weights" / "best.onnx"
    assert weights_path.exists(), f"ONNX weights not found: {weights_path}"

    session = ort.InferenceSession(
        str(weights_path),
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    input_name = session.get_inputs()[0].name

    # ── Collect images ────────────────────────────────────────────────────────
    valid_exts  = {".jpg", ".jpeg", ".png", ".bmp"}
    image_files = sorted(
        p for p in input_dir.iterdir()
        if p.suffix.lower() in valid_exts
    )

    predictions = []

    # ── Inference loop (one image at a time — avoids batching complexity) ─────
    for img_path in image_files:
        image_id = get_image_id(img_path.name)

        tensor, scale, pad_top, pad_left, orig_h, orig_w = preprocess(img_path)
        outputs = session.run(None, {input_name: tensor})

        dets = decode(outputs[0], scale, pad_top, pad_left, orig_h, orig_w)
        for d in dets:
            predictions.append({
                "image_id":    image_id,
                "category_id": d["category_id"],
                "bbox":        d["bbox"],
                "score":       d["score"],
            })

    # ── Write output ──────────────────────────────────────────────────────────
    output_path.write_text(json.dumps(predictions), encoding="utf-8")

    n_imgs  = len(image_files)
    n_preds = len(predictions)
    print(f"Processed {n_imgs} images → {n_preds} predictions "
          f"({n_preds/n_imgs:.1f} avg/image)" if n_imgs else "No images found.")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
