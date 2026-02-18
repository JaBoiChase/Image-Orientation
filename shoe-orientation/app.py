import os
import pathlib
from typing import Optional, Dict, Any, Tuple, List

import requests

from fastapi import FastAPI, Form, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from shopify_gql import gql, product_gid, GET_PRODUCT, FILE_UPDATE

# ---------- Config ----------
DEFAULT_MIN_CONF = float(os.getenv("MIN_CONF", "0.80"))
RUN_SECRET = os.getenv("RUN_SECRET", "")  # optional; if set, require it for /api/run and /run
CLASSIFIER_URL = os.getenv("CLASSIFIER_URL", "").strip()
CLASSIFIER_SECRET = os.getenv("CLASSIFIER_SECRET", "").strip()

LABEL_TO_TEXT = {
    "left": "medial side view",
    "right": "lateral side view",
    "upper": "upper view",
    "outsole": "outsole view",
    "rear": "rear view",
    "angled": "angled view",
}

# ---------- Helpers ----------
def safe_name(s: str) -> str:
    s = (s or "UnknownVendor").strip()
    return s.replace("/", "-").replace("\\", "-")

def extract_unique_variant_option(product: dict, option_name: str) -> Optional[str]:
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

def normalize_color(color: Optional[str]) -> str:
    if not color:
        return ""
    return " ".join(color.split())

def build_alt(product_title: str, color: str, label: str) -> str:
    view = LABEL_TO_TEXT.get(label, "product photo")
    parts = [product_title]
    if color:
        parts.append(color)
    parts.append(view)
    return " ".join(parts)

def predict_via_classifier(vendor: str, image_url: str):
    """
    Calls the external classifier service and returns (label, confidence).
    """
    if not CLASSIFIER_URL:
        raise RuntimeError("Missing CLASSIFIER_URL env var in Render.")

    headers = {"Content-Type": "application/json"}
    if CLASSIFIER_SECRET:
        headers["x-classifier-secret"] = CLASSIFIER_SECRET

    resp = requests.post(
        CLASSIFIER_URL,
        json={"vendor": vendor, "image_url": image_url},
        headers=headers,
        timeout=120,  # classifier + image download time
    )

    # Provide useful error messages in logs/UI
    if resp.status_code != 200:
        raise RuntimeError(f"Classifier error {resp.status_code}: {resp.text}")

    data = resp.json()
    return data["label"], float(data["confidence"])


# ---------- Model cache ----------
_MODEL_CACHE: Dict[str, Tuple[torch.nn.Module, List[str], int]] = {}

def load_vendor_model(vendor: str) -> Optional[Tuple[torch.nn.Module, List[str], int]]:
    if vendor in _MODEL_CACHE:
        return _MODEL_CACHE[vendor]

    ckpt_path = pathlib.Path("models") / f"{vendor}.pt"
    if not ckpt_path.exists():
        return None

    ckpt = torch.load(ckpt_path, map_location="cpu")
    model_name = ckpt["model_name"]
    classes = ckpt["classes"]
    img_size = ckpt.get("img_size", 224)

    net = timm.create_model(model_name, pretrained=False, num_classes=len(classes))
    net.load_state_dict(ckpt["state_dict"])
    net.eval()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    net.to(device)

    _MODEL_CACHE[vendor] = (net, classes, img_size)
    return _MODEL_CACHE[vendor]

def require_secret(request: Request):
    if not RUN_SECRET:
        return
    supplied = request.headers.get("x-run-secret", "") or request.query_params.get("secret", "")
    if supplied != RUN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

def predict_one(net, classes, img_size, url: str) -> Tuple[str, float]:
    device = next(net.parameters()).device

    tfm = transforms.Compose([
        transforms.Resize((img_size, img_size)),
        transforms.ToTensor(),
        transforms.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
    ])

    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    pil = Image.open(r.raw).convert("RGB")
    x = tfm(pil).unsqueeze(0).to(device)

    with torch.no_grad():
        logits = net(x)
        prob = F.softmax(logits, dim=1)[0]
        conf, idx = torch.max(prob, dim=0)

    label = classes[int(idx)]
    return label, float(conf)

def tag_product(product_id: int, min_conf: float) -> Dict[str, Any]:
    data = gql(GET_PRODUCT, {"id": product_gid(product_id)})
    p = data.get("product")
    if not p:
        return {"ok": False, "message": "Product not found", "updated": 0, "skipped": 0, "details": []}

    vendor = safe_name(p.get("vendor"))
    title = p.get("title") or "Product"
    color = normalize_color(extract_unique_variant_option(p, "Color")) or ""

    try:
        label, conf = predict_via_classifier(vendor, url)
    except RuntimeError as e:
        details.append({"media_id": node["id"], "action": "classifier_error", "error": str(e)})
        skipped += 1
        continue

    net, classes, img_size = loaded

    updates = []
    details = []
    skipped = 0

    for node in p["media"]["nodes"]:
        if node.get("__typename") != "MediaImage":
            continue
        if node.get("fileStatus") != "READY":
            skipped += 1
            continue
        img = node.get("image") or {}
        url = img.get("url")
        if not url:
            skipped += 1
            continue

        label, conf = predict_via_classifier(vendor, url)

        if conf < min_conf:
            details.append({
                "media_id": node["id"],
                "label": label,
                "confidence": conf,
                "action": "skipped_low_conf"
            })
            skipped += 1
            continue

        alt = build_alt(title, color, label)
        updates.append({"id": node["id"], "alt": alt})
        details.append({
            "media_id": node["id"],
            "label": label,
            "confidence": conf,
            "alt": alt,
            "action": "update"
        })

    if not updates:
        return {
            "ok": True,
            "message": "No updates (all low confidence / not READY / non-image).",
            "vendor": vendor,
            "title": title,
            "color": color,
            "updated": 0,
            "skipped": skipped,
            "details": details,
        }

    resp = gql(FILE_UPDATE, {"files": updates})
    errs = resp["fileUpdate"]["userErrors"]
    if errs:
        return {
            "ok": False,
            "message": "Shopify userErrors",
            "vendor": vendor,
            "title": title,
            "color": color,
            "updated": 0,
            "skipped": skipped,
            "errors": errs,
            "details": details,
        }

    return {
        "ok": True,
        "message": f"Updated {len(updates)} images.",
        "vendor": vendor,
        "title": title,
        "color": color,
        "updated": len(updates),
        "skipped": skipped,
        "details": details,
    }

# ---------- FastAPI ----------
app = FastAPI()

HOME_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Shoe ALT Tagger</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 40px; }
      input, button { font-size: 16px; padding: 10px; }
      .row { margin: 12px 0; }
      pre { background: #f6f8fa; padding: 12px; border-radius: 8px; overflow: auto; }
      .hint { color: #555; }
      .status { margin-top: 16px; font-weight: 600; }
    </style>
  </head>
  <body>
    <h1>Shoe ALT Tagger</h1>
    <p class="hint">Enter a numeric Shopify Product ID and click Run.</p>

    <div class="row">
      <input id="product_id" placeholder="Product ID (e.g. 123456789)" />
    </div>
    <div class="row">
      <input id="min_conf" placeholder="min_conf (default 0.80)" />
    </div>
    <div class="row">
      <input id="secret" placeholder="secret (only if enabled)" />
    </div>

    <button id="runBtn">Run</button>

    <div class="status" id="status"></div>
    <pre id="out" style="display:none;"></pre>

    <script>
      const runBtn = document.getElementById("runBtn");
      const statusEl = document.getElementById("status");
      const outEl = document.getElementById("out");

      runBtn.addEventListener("click", async () => {
        const productId = document.getElementById("product_id").value.trim();
        const minConfRaw = document.getElementById("min_conf").value.trim();
        const secret = document.getElementById("secret").value.trim();

        if (!productId) {
          statusEl.textContent = "Please enter a product ID.";
          return;
        }

        const payload = { product_id: Number(productId) };
        if (minConfRaw) payload.min_conf = Number(minConfRaw);

        statusEl.textContent = "Running… (this can take ~10–60s depending on image count)";
        outEl.style.display = "none";
        outEl.textContent = "";
        runBtn.disabled = true;

        try {
          const res = await fetch("/api/run", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              ...(secret ? {"x-run-secret": secret} : {})
            },
            body: JSON.stringify(payload)
          });

          const text = await res.text();
          let data;
          try { data = JSON.parse(text); } catch { data = { raw: text }; }

          statusEl.textContent = res.ok ? "Done." : `Error (${res.status})`;
          outEl.style.display = "block";
          outEl.textContent = JSON.stringify(data, null, 2);
        } catch (e) {
          statusEl.textContent = "Request failed (network / timeout).";
          outEl.style.display = "block";
          outEl.textContent = String(e);
        } finally {
          runBtn.disabled = false;
        }
      });
    </script>
  </body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HOME_HTML

@app.post("/run", response_class=HTMLResponse)
def run_form(
    request: Request,
    product_id: int = Form(...),
    min_conf: str = Form(""),
    secret: str = Form(""),
):
    # secret support for browser form
    if RUN_SECRET:
        if (secret or "") != RUN_SECRET:
            return HTMLResponse("<h2>Unauthorized</h2>", status_code=401)

    # parse min_conf safely
    min_conf_val = DEFAULT_MIN_CONF
    if min_conf.strip():
        try:
            min_conf_val = float(min_conf)
        except ValueError:
            return HTMLResponse("<h2>Invalid min_conf</h2><p>Use a number like 0.80</p><p><a href='/'>Back</a></p>", status_code=400)

    result = tag_product(product_id, min_conf_val)
    return HTMLResponse(f"<h2>Result</h2><pre>{result}</pre><p><a href='/'>Back</a></p>")
    
    # secret support for browser form
    if RUN_SECRET:
        if (secret or "") != RUN_SECRET:
            return HTMLResponse("<h2>Unauthorized</h2>", status_code=401)

    result = tag_product(product_id, min_conf if min_conf is not None else DEFAULT_MIN_CONF)
    return HTMLResponse(f"<h2>Result</h2><pre>{result}</pre><p><a href='/'>Back</a></p>")

class RunRequest(BaseModel):
    product_id: int
    min_conf: Optional[float] = None

@app.post("/api/run")
def run_api(req: RunRequest, _=Depends(require_secret)):
    result = tag_product(req.product_id, req.min_conf if req.min_conf is not None else DEFAULT_MIN_CONF)
    return JSONResponse(result)



