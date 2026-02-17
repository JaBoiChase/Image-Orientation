import argparse
import pathlib
import requests
import torch
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms
import timm

from shopify_gql import gql, product_gid, GET_PRODUCT, FILE_UPDATE

LABEL_TO_TEXT = {
    "left": "medial side view",
    "right": "lateral side view",
    "upper": "upper view",
    "outsole": "outsole view",
    "rear": "rear view showing heel counter",
    "angled": "angled view",
}

def extract_unique_variant_option(product: dict, option_name: str) -> str | None:
    """
    Returns the option value if there is exactly one unique value across all variants.
    Otherwise returns None.
    """
    vals = set()
    variants = ((product.get("variants") or {}).get("nodes") or [])
    for v in variants:
        for opt in (v.get("selectedOptions") or []):
            if (opt.get("name") or "").strip().lower() == option_name.strip().lower():
                val = (opt.get("value") or "").strip()
                if val:
                    vals.add(val)
    if len(vals) == 1:
        return next(iter(vals))
    return None

def normalize_color(color: str | None) -> str:
    # optional cleanup: collapse whitespace, etc.
    if not color:
        return ""
    return " ".join(color.split())


def safe_name(s: str) -> str:
    s = (s or "UnknownVendor").strip()
    return s.replace("/", "-").replace("\\", "-")

def load_vendor_model(vendor: str):
    ckpt_path = pathlib.Path("models") / f"{vendor}.pt"
    if not ckpt_path.exists():
        return None, None, None

    ckpt = torch.load(ckpt_path, map_location="cpu")
    model_name = ckpt["model_name"]
    classes = ckpt["classes"]
    img_size = ckpt.get("img_size", 224)

    net = timm.create_model(model_name, pretrained=False, num_classes=len(classes))
    net.load_state_dict(ckpt["state_dict"])
    net.eval()
    return net, classes, img_size

def build_alt(product_title: str, color: str, label: str) -> str:
    view = LABEL_TO_TEXT.get(label, "product photo")
    # Structure: {Title} {Color} {view}
    # If color is blank, it becomes "{Title} {view}"
    parts = [product_title]
    if color:
        parts.append(color)
    parts.append(view)
    return " ".join(parts)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product_id", type=int, required=True)
    ap.add_argument("--min_conf", type=float, default=0.80)
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    data = gql(GET_PRODUCT, {"id": product_gid(args.product_id)})
    p = data.get("product")
    if not p:
        raise SystemExit("Product not found.")

    vendor = safe_name(p.get("vendor"))
    title = p.get("title") or "Product"
    color = normalize_color(extract_unique_variant_option(p, "Color"))
    # If multiple colors exist on this product, you can choose a fallback:
    # color = "Multiple colors"
    # or leave it blank (recommended for now):
    if not color:
        color = ""


    net, classes, img_size = load_vendor_model(vendor)
    if net is None:
        print(f"Skipping: no model for vendor '{vendor}'")
        return

    device = "cuda" if torch.cuda.is_available() else "cpu"
    net.to(device)

    tfm = transforms.Compose([
        transforms.Resize((img_size, img_size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
    ])

    updates = []

    for node in p["media"]["nodes"]:
        if node.get("__typename") != "MediaImage":
            continue
        if node.get("fileStatus") != "READY":
            continue

        img = node.get("image") or {}
        url = img.get("url")
        if not url:
            continue

        # Download and decode image (cross-platform, no temp files)
        r = requests.get(url, stream=True, timeout=60)
        r.raise_for_status()
        pil = Image.open(r.raw).convert("RGB")

        x = tfm(pil).unsqueeze(0).to(device)

        with torch.no_grad():
            logits = net(x)
            prob = F.softmax(logits, dim=1)[0]
            conf, idx = torch.max(prob, dim=0)

        label = classes[int(idx)]
        conf = float(conf)

        if conf < args.min_conf:
            continue

        alt = build_alt(title, color, label)
        updates.append({"id": node["id"], "alt": alt})

    if not updates:
        print("No updates (all low confidence / not READY / non-image).")
        return

    if args.dry_run:
        print("DRY RUN. Would update:")
        for u in updates:
            print(u["id"], "=>", u["alt"])
        return

    resp = gql(FILE_UPDATE, {"files": updates})
    errs = resp["fileUpdate"]["userErrors"]
    if errs:
        print("Shopify userErrors:")
        for e in errs:
            print(e)
    else:
        print(f"Updated {len(updates)} images.")

if __name__ == "__main__":
    main()
