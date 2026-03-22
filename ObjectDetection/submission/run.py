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

Model format: ONNX (opset 17, FP16 or FP32)
  Loaded with onnxruntime CUDAExecutionProvider (pre-installed in sandbox).

Inference strategy: 3-pass TTA (original + hflip + scale×1.2) merged with WBF.
"""

import argparse
import base64
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image
from ensemble_boxes import weighted_boxes_fusion

# ── Inference constants ───────────────────────────────────────────────────────
IMGSZ        = 1280     # Must match the imgsz used during export
CONF_THRESH  = 0.001   # Very low: mAP evaluation sweeps thresholds — keep all detections
MAX_DET      = 1000    # Max detections per image (shelf images can have 200+ products)
PAD_VALUE    = 114     # Letterbox fill colour (YOLO standard)
TIME_BUDGET  = 250.0   # Seconds; skip remaining TTA passes if exceeded

# WBF parameters
WBF_IOU_THR      = 0.50    # Merges TTA boxes of same product; distinct products have IoU ~0.05–0.20
WBF_SKIP_THR     = 0.0001  # Keep all boxes (mAP evaluation sweeps thresholds)
WBF_CONF_TYPE    = "avg"   # Average confidence across merged TTA boxes
WBF_WEIGHTS      = [1.0, 1.0, 0.8]  # original, hflip, scale×1.2
# Per-model weights applied on top of aug weights.
# Models loaded in sorted order: yolo11x_fp16(0), yolov8l_fp16(1), yolov8x_fp16(2).
# YOLO11x has C2PSA spatial attention → better fine-grained classification → upweight it.
MODEL_WEIGHTS    = [1.0, 1.0, 1.0]  # yolo11x, yolov8l, yolov8x (equal weights — best empirically)


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


def to_tensor(padded: np.ndarray) -> np.ndarray:
    """HWC uint8 [0,255] → NCHW float32 [0,1]."""
    t = padded.astype(np.float32) / 255.0
    t = np.transpose(t, (2, 0, 1))
    return t[np.newaxis, ...]


def preprocess(img_path: Path):
    """Load image and return (arr_rgb, orig_h, orig_w)."""
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img)
    return arr, arr.shape[0], arr.shape[1]


# ── TTA augmentation helpers ──────────────────────────────────────────────────

def make_original_tensor(arr: np.ndarray):
    """Standard letterbox → tensor. Returns (tensor, scale, pad_top, pad_left)."""
    padded, scale, pad_top, pad_left = letterbox(arr, IMGSZ)
    return to_tensor(padded), scale, pad_top, pad_left


def make_hflip_tensor(arr: np.ndarray):
    """Horizontal flip then letterbox → tensor."""
    flipped = arr[:, ::-1, :].copy()
    padded, scale, pad_top, pad_left = letterbox(flipped, IMGSZ)
    return to_tensor(padded), scale, pad_top, pad_left


def make_scale12_tensor(arr: np.ndarray):
    """
    Scale up by 1.2× (effectively imgsz=1536 crop) then letterbox to IMGSZ.
    Achieved by resizing so the longer side becomes IMGSZ*1.2, then letterboxing to IMGSZ.
    Improves small-product recall on large shelf images.
    """
    h, w = arr.shape[:2]
    scale_up = (IMGSZ * 1.2) / max(h, w)
    new_h = int(round(h * scale_up))
    new_w = int(round(w * scale_up))
    pil_up = Image.fromarray(arr).resize((new_w, new_h), Image.BILINEAR)
    arr_up = np.array(pil_up)
    padded, scale_lb, pad_top, pad_left = letterbox(arr_up, IMGSZ)
    # Effective scale from original → padded space: scale_up * scale_lb
    combined_scale = scale_up * scale_lb
    return to_tensor(padded), combined_scale, pad_top, pad_left


# ── YOLOv8 ONNX output decoding ───────────────────────────────────────────────

def decode(
    output:    np.ndarray,
    scale:     float,
    pad_top:   int,
    pad_left:  int,
    orig_h:    int,
    orig_w:    int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Decode YOLOv8 ONNX output tensor to arrays of (boxes_xyxy, class_ids, scores)
    in original-image pixel coordinates. Returns empty arrays if no detections pass
    the confidence threshold.

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
        empty = np.empty((0, 4), dtype=np.float32)
        return empty, np.empty(0, dtype=np.int32), np.empty(0, dtype=np.float32)

    boxes_raw  = boxes_raw[mask]
    class_ids  = class_ids[mask]
    class_conf = class_conf[mask]

    # cx,cy,w,h → x1,y1,x2,y2  (padded pixel space)
    x1 = boxes_raw[:, 0] - boxes_raw[:, 2] / 2
    y1 = boxes_raw[:, 1] - boxes_raw[:, 3] / 2
    x2 = boxes_raw[:, 0] + boxes_raw[:, 2] / 2
    y2 = boxes_raw[:, 1] + boxes_raw[:, 3] / 2
    boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)

    # Map padded-image coords → original image coords
    #   Step 1: remove letterbox padding
    boxes_xyxy[:, [0, 2]] -= pad_left
    boxes_xyxy[:, [1, 3]] -= pad_top
    #   Step 2: undo resize scale
    boxes_xyxy /= scale
    #   Step 3: clip to original image bounds
    boxes_xyxy[:, [0, 2]] = np.clip(boxes_xyxy[:, [0, 2]], 0, orig_w)
    boxes_xyxy[:, [1, 3]] = np.clip(boxes_xyxy[:, [1, 3]], 0, orig_h)

    return boxes_xyxy, class_ids, class_conf


# ── Crop classifier ───────────────────────────────────────────────────────────

class CropClassifier:
    """
    Dedicated product crop classifier trained on 22,700 labeled shelf crops.

    Unlike the old MobileNetV3 re-ranker (generic ImageNet features + PCA),
    this model is fine-tuned on the exact same crop domain as the test images,
    making it much more reliable for the 356-class classification task.

    Overrides category_id for ALL detections — detection head handles bbox
    localization & scoring, this model handles classification.

    Loaded from weights/:
      crop_cls_fp16.onnx   — YOLO11m-cls fine-tuned on shelf product crops
                             Input:  [B, 3, 224, 224] float32 [0, 1]
                             Output: [B, 356] softmax probabilities
    """

    _SIZE = 224
    # ImageNet normalisation — matches ultralytics YOLO classification training pipeline
    _MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    _STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def __init__(self, weights_dir: Path):
        cls_path = weights_dir / "crop_cls_fp16.onnx"
        self._session    = ort.InferenceSession(
            str(cls_path),
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        self._input_name = self._session.get_inputs()[0].name
        print(f"  CropClassifier ready: {cls_path.name}")

    def _preprocess(self, crop_rgb: np.ndarray) -> np.ndarray:
        """Resize crop to 224×224, apply ImageNet normalisation, return CHW float32."""
        pil = Image.fromarray(crop_rgb).resize(
            (self._SIZE, self._SIZE), Image.BILINEAR
        )
        arr = np.array(pil, dtype=np.float32) / 255.0   # [0, 1]
        arr = (arr - self._MEAN) / self._STD             # ImageNet normalise
        return np.transpose(arr, (2, 0, 1))              # [3, 224, 224]

    def classify(self, img_rgb: np.ndarray, dets: list[dict]) -> list[dict]:
        """
        Override category_id for every detection using the crop classifier.
        Processes all crops in a single GPU batch — adds ~150ms per image.

        dets: list of {'bbox': [x,y,w,h], 'category_id': int, 'score': float}
        Returns dets with updated category_ids.
        """
        if not dets:
            return dets

        h, w = img_rgb.shape[:2]
        tensors:   list[np.ndarray] = []
        valid_idx: list[int]        = []

        for i, d in enumerate(dets):
            bx, by, bw, bh = d["bbox"]
            x1 = max(0, int(bx))
            y1 = max(0, int(by))
            x2 = min(w, int(bx + bw))
            y2 = min(h, int(by + bh))
            if x2 <= x1 or y2 <= y1:
                continue
            crop = img_rgb[y1:y2, x1:x2]
            tensors.append(self._preprocess(crop))
            valid_idx.append(i)

        if not tensors:
            return dets

        # Single batched forward pass → [B, 356]
        batch = np.stack(tensors, axis=0)
        probs = self._session.run(None, {self._input_name: batch})[0]
        pred_cats = np.argmax(probs, axis=1)

        result = list(dets)
        for k, det_i in enumerate(valid_idx):
            result[det_i] = dict(dets[det_i])
            result[det_i]["category_id"] = int(pred_cats[k])
        return result


# ── Single-image TTA inference ─────────────────────────────────────────────────

def infer_image(
    arr:        np.ndarray,
    orig_h:     int,
    orig_w:     int,
    sessions:   list,
    time_start: float,
) -> list[dict]:
    """
    Run up to 3 TTA passes (original, hflip, scale×1.2) for each provided session,
    merge all detections with WBF, and return COCO-format prediction dicts.

    Falls back to original-only if TIME_BUDGET is exceeded.
    """
    boxes_list  = []
    scores_list = []
    labels_list = []

    # Build list of (tensor, scale, pad_top, pad_left, is_hflip) augmentation passes
    # We'll dynamically skip later passes if time is running out.
    augmentations = [
        ("original", make_original_tensor),
        ("hflip",    make_hflip_tensor),
        ("scale12",  make_scale12_tensor),
    ]
    # WBF weights: one per (aug × session) combination — original gets weight 1.0,
    # hflip 1.0, scale12 0.8, multiplied per session equally.
    aug_weights = [1.0, 1.0, 0.8]

    wbf_weights = []

    for aug_idx, (aug_name, aug_fn) in enumerate(augmentations):
        elapsed = time.time() - time_start
        if elapsed > TIME_BUDGET and aug_idx > 0:
            break

        tensor, scale, pad_top, pad_left = aug_fn(arr)
        is_hflip = (aug_name == "hflip")

        for sess_idx, session in enumerate(sessions):
            input_name = session.get_inputs()[0].name
            outputs = session.run(None, {input_name: tensor})
            boxes_xyxy, class_ids, class_conf = decode(
                outputs[0], scale, pad_top, pad_left, orig_h, orig_w
            )

            model_w = MODEL_WEIGHTS[sess_idx] if sess_idx < len(MODEL_WEIGHTS) else 1.0
            combined_w = aug_weights[aug_idx] * model_w

            if len(boxes_xyxy) == 0:
                boxes_list.append([])
                scores_list.append([])
                labels_list.append([])
                wbf_weights.append(combined_w)
                continue

            # Flip correction: mirror x-coordinates back for hflip pass
            if is_hflip:
                x1_orig = orig_w - boxes_xyxy[:, 2]
                x2_orig = orig_w - boxes_xyxy[:, 0]
                boxes_xyxy[:, 0] = x1_orig
                boxes_xyxy[:, 2] = x2_orig

            # Normalize to [0, 1] for WBF
            boxes_norm = boxes_xyxy.copy().astype(np.float64)
            boxes_norm[:, [0, 2]] /= orig_w
            boxes_norm[:, [1, 3]] /= orig_h
            boxes_norm = np.clip(boxes_norm, 0.0, 1.0)

            boxes_list.append(boxes_norm.tolist())
            scores_list.append(class_conf.tolist())
            labels_list.append(class_ids.tolist())
            wbf_weights.append(combined_w)

    if not any(len(b) > 0 for b in boxes_list):
        return []

    # Weighted Boxes Fusion
    merged_boxes, merged_scores, merged_labels = weighted_boxes_fusion(
        boxes_list,
        scores_list,
        labels_list,
        weights=wbf_weights,
        iou_thr=WBF_IOU_THR,
        skip_box_thr=WBF_SKIP_THR,
        conf_type=WBF_CONF_TYPE,
    )

    # Denormalize → pixel coords → COCO xywh
    results = []
    for box, score, label in zip(merged_boxes, merged_scores, merged_labels):
        x1 = box[0] * orig_w
        y1 = box[1] * orig_h
        x2 = box[2] * orig_w
        y2 = box[3] * orig_h
        bw = x2 - x1
        bh = y2 - y1
        if bw <= 0 or bh <= 0:
            continue
        results.append({
            "category_id": int(label),
            "bbox":  [round(float(x1), 2), round(float(y1), 2),
                      round(float(bw),  2), round(float(bh),  2)],
            "score": round(float(score), 6),
        })

    # Keep top MAX_DET by score
    if len(results) > MAX_DET:
        results.sort(key=lambda d: d["score"], reverse=True)
        results = results[:MAX_DET]

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


class RefMatcher:
    """
    Reference-image cosine-similarity re-ranker.

    Pre-computed MobileNetV3-Small (576-dim → PCA-64) embeddings of the 327
    official product reference images are used to override YOLO's classification
    for uncertain detections (score < CONF_THR).

    Files loaded from weights/ (JSON — do NOT count as weight files):
      mobilenet_v3_small.onnx  — feature extractor, output [B, 576]
      ref_embeddings.json      — base64 FP16 [N, 64] embeddings
      ref_labels.json          — category_id for each of the N ref images
      pca_components.json      — PCA components [64, 576] and mean [576]
    """

    _SIZE     = 224
    _MEAN     = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    _STD      = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    _CONF_THR = 0.50   # Override YOLO category_id when its WBF score is below this

    def __init__(self, weights_dir: Path):
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        mob_path  = weights_dir / "mobilenet_v3_small.onnx"
        self._session    = ort.InferenceSession(str(mob_path), providers=providers)
        self._input_name = self._session.get_inputs()[0].name

        pca_data       = json.loads((weights_dir / "pca_components.json").read_text())
        self._pca_comp = np.array(pca_data["components"], dtype=np.float32)  # [64, 576]
        self._pca_mean = np.array(pca_data["mean"],       dtype=np.float32)  # [576]

        emb_data = json.loads((weights_dir / "ref_embeddings.json").read_text())
        raw      = base64.b64decode(emb_data["data"])
        emb      = np.frombuffer(raw, dtype=np.float16).reshape(emb_data["shape"])
        self._ref_emb    = emb.astype(np.float32)                               # [N, 64]
        self._ref_labels = np.array(
            json.loads((weights_dir / "ref_labels.json").read_text()), dtype=np.int32
        )                                                                        # [N]
        print(f"  RefMatcher ready: {self._ref_emb.shape[0]} ref imgs, "
              f"{len(set(self._ref_labels.tolist()))} categories")

    def _embed(self, crops: list[np.ndarray]) -> np.ndarray:
        """crops: list of HWC uint8 RGB → [M, 64] L2-normalised embeddings."""
        tensors = []
        for crop in crops:
            img = Image.fromarray(crop).resize((self._SIZE, self._SIZE), Image.BILINEAR)
            arr = np.array(img, dtype=np.float32) / 255.0
            arr = (arr - self._MEAN) / self._STD
            tensors.append(arr.transpose(2, 0, 1))
        batch     = np.stack(tensors, axis=0)
        feats     = self._session.run(None, {self._input_name: batch})[0]       # [M, 576]
        feats_pca = (feats - self._pca_mean) @ self._pca_comp.T                 # [M, 64]
        norms     = np.linalg.norm(feats_pca, axis=1, keepdims=True)
        return feats_pca / (norms + 1e-8)                                       # [M, 64]

    def rerank(self, img_rgb: np.ndarray, dets: list[dict]) -> list[dict]:
        """
        For detections where WBF score < _CONF_THR, override category_id with
        the nearest reference-image match (cosine similarity in PCA-64 space).
        """
        if not dets:
            return dets

        h, w = img_rgb.shape[:2]
        uncertain_idx = [i for i, d in enumerate(dets) if d["score"] < self._CONF_THR]
        if not uncertain_idx:
            return dets

        crops = []
        for i in uncertain_idx:
            bx, by, bw, bh = dets[i]["bbox"]
            x1 = max(0, int(bx));  y1 = max(0, int(by))
            x2 = min(w, int(bx + bw));  y2 = min(h, int(by + bh))
            if x2 <= x1 or y2 <= y1:
                crops.append(np.full((self._SIZE, self._SIZE, 3), 128, dtype=np.uint8))
            else:
                crops.append(img_rgb[y1:y2, x1:x2])

        embs         = self._embed(crops)                           # [M, 64]
        sims         = embs @ self._ref_emb.T                       # [M, N_ref]
        best_ref_idx = np.argmax(sims, axis=1)                      # [M]

        result = list(dets)
        for k, det_i in enumerate(uncertain_idx):
            result[det_i] = dict(dets[det_i])
            result[det_i]["category_id"] = int(self._ref_labels[best_ref_idx[k]])
        return result


def load_sessions(weights_dir: Path):
    """
    Load detection ONNX sessions and optional re-rankers.

    Detection models: any *.onnx whose name does NOT contain "crop_cls" or "mobilenet".
    CropClassifier:  crop_cls_fp16.onnx (optional)
    RefMatcher:      mobilenet_v3_small.onnx + JSON embeddings (optional)
    """
    providers  = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    onnx_files = sorted(
        f for f in weights_dir.glob("*.onnx")
        if "crop_cls" not in f.name and "mobilenet" not in f.name
    )
    assert onnx_files, f"No detection .onnx files found in {weights_dir}"
    sessions = [ort.InferenceSession(str(f), providers=providers) for f in onnx_files]
    print(f"Loaded {len(sessions)} detection model(s): {[f.name for f in onnx_files]}")

    crop_cls: "CropClassifier | None" = None
    if (weights_dir / "crop_cls_fp16.onnx").exists():
        print("Loading crop classifier …")
        crop_cls = CropClassifier(weights_dir)

    ref_matcher: "RefMatcher | None" = None
    if (weights_dir / "mobilenet_v3_small.onnx").exists() and \
       (weights_dir / "ref_embeddings.json").exists():
        print("Loading reference matcher (MobileNetV3 + product reference images) …")
        ref_matcher = RefMatcher(weights_dir)
    else:
        print("Reference matcher not found — using YOLO classification head only")

    return sessions, crop_cls, ref_matcher


def main():
    args = parse_args()

    input_dir   = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Load ONNX model(s) + optional re-ranker ───────────────────────────────
    weights_dir = Path(__file__).parent / "weights"
    assert weights_dir.exists(), f"weights/ directory not found: {weights_dir}"
    sessions, crop_cls, ref_matcher = load_sessions(weights_dir)

    # ── Collect images ────────────────────────────────────────────────────────
    valid_exts  = {".jpg", ".jpeg", ".png", ".bmp"}
    image_files = sorted(
        p for p in input_dir.iterdir()
        if p.suffix.lower() in valid_exts
    )

    predictions = []
    t_start = time.time()

    # ── Inference loop ────────────────────────────────────────────────────────
    for img_path in image_files:
        image_id = get_image_id(img_path.name)
        arr, orig_h, orig_w = preprocess(img_path)

        dets = infer_image(arr, orig_h, orig_w, sessions, t_start)
        if crop_cls is not None and (time.time() - t_start) < 265.0:
            dets = crop_cls.classify(arr, dets)
        if ref_matcher is not None and (time.time() - t_start) < 270.0:
            dets = ref_matcher.rerank(arr, dets)
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
    elapsed = time.time() - t_start
    n_preds = len(predictions)
    if n_imgs:
        print(f"Processed {n_imgs} images in {elapsed:.1f}s "
              f"({elapsed/n_imgs:.2f}s/img) → {n_preds} predictions "
              f"({n_preds/n_imgs:.1f} avg/img)")
    else:
        print("No images found.")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
