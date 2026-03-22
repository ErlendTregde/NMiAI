"""
build_reference_embeddings.py — Offline packaging script.

Exports MobileNetV3-Small to ONNX (weight file) and precomputes L2-normalised
PCA-64 embeddings for all product reference images (stored as base64 JSON —
does NOT count toward the 3-weight-file limit).

Outputs (in submission/weights/):
  mobilenet_v3_small.onnx      ~10 MB  [weight file #3]
  ref_embeddings.json          ~300 KB [base64 FP16 numpy — NOT a weight file]
  ref_labels.json              ~12 KB  [NOT a weight file]
  pca_components.json          ~400 KB [NOT a weight file]

Usage:
  uv run python build_reference_embeddings.py
"""

import base64
import difflib
import json
import shutil
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torchvision.models as tvm
import onnxruntime as ort
from PIL import Image

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent
PRODUCT_DIR = ROOT / "data" / "NM_NGD_product_images"
ANNOT_PATH  = ROOT / "data" / "NM_NGD_coco_dataset" / "train" / "annotations.json"
OUTPUT_DIR  = ROOT / "submission" / "weights"

# ── Config ────────────────────────────────────────────────────────────────────
EMBED_SIZE   = 224   # MobileNetV3-Small input resolution
PCA_DIMS     = 64    # Reduced embedding size
BATCH_SIZE   = 64    # Inference batch size
FUZZY_CUTOFF = 0.75  # Minimum fuzzy-match score for barcode → category mapping

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32)


# ── MobileNetV3-Small feature extractor ──────────────────────────────────────

class MobileFeats(nn.Module):
    """MobileNetV3-Small with classifier replaced by identity → 576-dim features."""
    def __init__(self):
        super().__init__()
        base         = tvm.mobilenet_v3_small(weights="DEFAULT")
        self.features = base.features
        self.avgpool  = base.avgpool

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.avgpool(self.features(x)).flatten(1)   # [B, 576]


def export_mobilenet_onnx(path: Path) -> None:
    print("Exporting MobileNetV3-Small to ONNX …")
    model = MobileFeats().eval()
    dummy = torch.zeros(1, 3, EMBED_SIZE, EMBED_SIZE)
    torch.onnx.export(
        model, dummy, str(path),
        opset_version=17,
        input_names=["input"],
        output_names=["embedding"],
        dynamic_axes={"input": {0: "batch"}, "embedding": {0: "batch"}},
        dynamo=False,   # Use legacy exporter (no onnxscript dependency)
    )
    print(f"  Saved: {path} ({path.stat().st_size / 1e6:.1f} MB)")


# ── Barcode → category_id mapping ────────────────────────────────────────────

def build_barcode_mapping() -> dict[str, int]:
    """
    Map barcode (folder name) → COCO category_id.
    metadata.json lives at the ROOT of NM_NGD_product_images/ and contains
    a 'products' list with 'product_code' (= barcode) and 'product_name'.
    """
    coco       = json.loads(ANNOT_PATH.read_text())
    name_to_id = {c["name"]: c["id"] for c in coco["categories"]}
    cat_names  = list(name_to_id.keys())

    meta_path = PRODUCT_DIR / "metadata.json"
    meta      = json.loads(meta_path.read_text())

    barcode_map: dict[str, int] = {}
    unmatched = 0
    for product in meta.get("products", []):
        code = product.get("product_code", "")
        name = product.get("product_name", "")
        if not code or not name:
            continue
        matches = difflib.get_close_matches(name, cat_names, n=1, cutoff=FUZZY_CUTOFF)
        if matches:
            barcode_map[code] = name_to_id[matches[0]]
        else:
            unmatched += 1

    print(f"  {len(barcode_map)} matched, {unmatched} unmatched")
    return barcode_map


# ── Image preprocessing ───────────────────────────────────────────────────────

def preprocess_image(img_path: Path) -> np.ndarray | None:
    try:
        img = Image.open(img_path).convert("RGB").resize(
            (EMBED_SIZE, EMBED_SIZE), Image.BILINEAR
        )
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
        return np.transpose(arr, (2, 0, 1))    # [3, H, W]
    except Exception as e:
        print(f"  WARNING: could not load {img_path}: {e}")
        return None


# ── Batch embedding extraction ────────────────────────────────────────────────

def extract_embeddings(
    session: ort.InferenceSession, paths: list[Path]
) -> np.ndarray:
    input_name = session.get_inputs()[0].name
    all_embs: list[np.ndarray] = []

    for i in range(0, len(paths), BATCH_SIZE):
        batch_paths = paths[i : i + BATCH_SIZE]
        arrs = [preprocess_image(p) for p in batch_paths]
        arrs = [a for a in arrs if a is not None]
        if not arrs:
            continue
        batch = np.stack(arrs, axis=0).astype(np.float32)
        out   = session.run(None, {input_name: batch})[0]   # [B, 576]
        all_embs.append(out)
        print(f"\r  {i + len(arrs)}/{len(paths)} images", end="", flush=True)

    print()
    return np.concatenate(all_embs, axis=0)   # [N, 576]


# ── PCA (pure numpy — no sklearn required) ────────────────────────────────────

def fit_pca(
    X: np.ndarray, n_components: int
) -> tuple[np.ndarray, np.ndarray]:
    """Returns (components [n, D], mean [D]) via truncated SVD."""
    mean = X.mean(axis=0)
    Xc   = X - mean
    _, _, Vt = np.linalg.svd(Xc, full_matrices=False)
    return Vt[:n_components], mean   # [n, D], [D]


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Export MobileNetV3-Small ONNX ─────────────────────────────────────
    onnx_path = OUTPUT_DIR / "mobilenet_v3_small.onnx"
    if onnx_path.exists():
        print(f"Using cached ONNX: {onnx_path} ({onnx_path.stat().st_size/1e6:.1f} MB)")
    else:
        export_mobilenet_onnx(onnx_path)

    # ── 2. Build barcode → category_id map ───────────────────────────────────
    print("Building barcode → category_id mapping …")
    barcode_map = build_barcode_mapping()
    print(f"  {len(barcode_map)} barcodes mapped to categories")

    # ── 3. Collect reference image paths + labels ─────────────────────────────
    valid_exts = {".jpg", ".jpeg", ".png", ".bmp"}
    img_paths: list[Path] = []
    labels:    list[int]  = []

    for bdir in sorted(PRODUCT_DIR.iterdir()):
        if not bdir.is_dir():
            continue
        cat_id = barcode_map.get(bdir.name)
        if cat_id is None:
            continue
        for p in sorted(bdir.iterdir()):
            if p.suffix.lower() in valid_exts and p.name != "metadata.json":
                img_paths.append(p)
                labels.append(cat_id)

    print(
        f"  {len(img_paths)} reference images across "
        f"{len(set(labels))} categories"
    )

    # ── 4. Extract MobileNetV3-Small embeddings ───────────────────────────────
    print("Extracting embeddings …")
    session = ort.InferenceSession(
        str(onnx_path),
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    embeddings = extract_embeddings(session, img_paths)   # [N, 576]
    print(f"  Raw embeddings: {embeddings.shape}")

    # ── 5. PCA → 64 dims ─────────────────────────────────────────────────────
    print(f"Fitting PCA → {PCA_DIMS} components …")
    pca_comp, pca_mean = fit_pca(embeddings, PCA_DIMS)
    emb_pca = (embeddings - pca_mean) @ pca_comp.T         # [N, 64]

    # ── 6. L2-normalise ───────────────────────────────────────────────────────
    norms   = np.linalg.norm(emb_pca, axis=1, keepdims=True)
    emb_pca = (emb_pca / (norms + 1e-8)).astype(np.float16)   # FP16

    # ── 7. Save reference embeddings as base64 JSON (NOT a weight file) ───────
    emb_json = {
        "data":  base64.b64encode(emb_pca.tobytes()).decode("ascii"),
        "shape": list(emb_pca.shape),
        "dtype": "float16",
    }
    emb_path = OUTPUT_DIR / "ref_embeddings.json"
    emb_path.write_text(json.dumps(emb_json))
    print(f"  ref_embeddings.json:  {emb_path.stat().st_size / 1024:.0f} KB")

    # ── 8. Save labels ────────────────────────────────────────────────────────
    lbl_path = OUTPUT_DIR / "ref_labels.json"
    lbl_path.write_text(json.dumps(labels))
    print(f"  ref_labels.json:      {lbl_path.stat().st_size / 1024:.0f} KB")

    # ── 9. Save PCA parameters ────────────────────────────────────────────────
    pca_path = OUTPUT_DIR / "pca_components.json"
    pca_path.write_text(json.dumps({
        "components": pca_comp.tolist(),   # [64, 576]
        "mean":       pca_mean.tolist(),   # [576]
    }))
    print(f"  pca_components.json:  {pca_path.stat().st_size / 1024:.0f} KB")

    # ── Summary ───────────────────────────────────────────────────────────────
    total_mb = sum(
        p.stat().st_size for p in OUTPUT_DIR.iterdir()
    ) / 1e6
    print(f"\nDone. submission/weights/ total: {total_mb:.0f} MB")
    print("Weight files (count toward 3-file limit):")
    for p in sorted(OUTPUT_DIR.glob("*.onnx")) + sorted(OUTPUT_DIR.glob("*.npy")):
        print(f"  {p.name}: {p.stat().st_size/1e6:.1f} MB")
    print("JSON files (do NOT count toward limit):")
    for p in sorted(OUTPUT_DIR.glob("*.json")):
        print(f"  {p.name}: {p.stat().st_size/1024:.0f} KB")


if __name__ == "__main__":
    main()
