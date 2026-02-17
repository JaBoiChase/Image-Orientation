import argparse
import json
import pathlib
import random
import numpy as np
from PIL import Image
from tqdm import tqdm

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
import timm

from sklearn.metrics import classification_report, confusion_matrix

CLASSES = ["left", "right", "upper", "outsole", "rear", "angled"]

def set_seed(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

class FolderDataset(Dataset):
    def __init__(self, root: pathlib.Path, class_to_idx: dict, tfm, files=None):
        self.root = root
        self.class_to_idx = class_to_idx
        self.tfm = tfm
        self.samples = files if files is not None else self._scan()

    def _scan(self):
        samples = []
        for cls, idx in self.class_to_idx.items():
            d = self.root / cls
            if not d.exists():
                continue
            for p in d.glob("*"):
                if p.suffix.lower() not in [".jpg", ".jpeg", ".png", ".webp"]:
                    continue
                samples.append((p, idx))
        return samples

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, i):
        path, y = self.samples[i]
        img = Image.open(path).convert("RGB")
        x = self.tfm(img)
        return x, y

def split_samples(samples, val_frac=0.15, seed=42):
    rnd = random.Random(seed)
    samples = samples[:]
    rnd.shuffle(samples)
    n_val = int(len(samples) * val_frac)
    return samples[n_val:], samples[:n_val]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", required=True, help="Vendor name (folder under data/)")
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--img", type=int, default=224)
    ap.add_argument("--model", default="efficientnet_b0")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    set_seed(args.seed)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    vendor_dir = pathlib.Path("data") / args.vendor
    if not vendor_dir.exists():
        raise SystemExit(f"Missing dataset folder: {vendor_dir}")

    class_to_idx = {c: i for i, c in enumerate(CLASSES)}

    train_tfm = transforms.Compose([
        transforms.Resize((args.img, args.img)),
        transforms.RandomHorizontalFlip(p=0.5),
        transforms.RandomRotation(5),
        transforms.ColorJitter(brightness=0.15, contrast=0.15, saturation=0.10, hue=0.02),
        transforms.ToTensor(),
        transforms.Normalize(mean=(0.485,0.456,0.406), std=(0.229,0.224,0.225)),
    ])
    val_tfm = transforms.Compose([
        transforms.Resize((args.img, args.img)),
        transforms.ToTensor(),
        transforms.Normalize(mean=(0.485,0.456,0.406), std=(0.229,0.224,0.225)),
    ])

    # Scan & split
    all_ds = FolderDataset(vendor_dir, class_to_idx, tfm=val_tfm)
    if len(all_ds) < 50:
        raise SystemExit(f"Not enough images to train ({len(all_ds)} found).")

    train_files, val_files = split_samples(all_ds.samples, val_frac=0.15, seed=args.seed)
    train_ds = FolderDataset(vendor_dir, class_to_idx, tfm=train_tfm, files=train_files)
    val_ds = FolderDataset(vendor_dir, class_to_idx, tfm=val_tfm, files=val_files)

    train_dl = DataLoader(train_ds, batch_size=args.batch, shuffle=True, num_workers=2, pin_memory=True)
    val_dl = DataLoader(val_ds, batch_size=args.batch, shuffle=False, num_workers=2, pin_memory=True)

    # Model
    net = timm.create_model(args.model, pretrained=True, num_classes=len(CLASSES))
    net.to(device)

    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)
    loss_fn = nn.CrossEntropyLoss()

    best_acc = 0.0
    out_dir = pathlib.Path("models")
    out_dir.mkdir(exist_ok=True)

    for epoch in range(1, args.epochs + 1):
        net.train()
        train_loss = 0.0
        for x, y in tqdm(train_dl, desc=f"Epoch {epoch}/{args.epochs} train"):
            x, y = x.to(device), torch.tensor(y).to(device)
            opt.zero_grad()
            logits = net(x)
            loss = loss_fn(logits, y)
            loss.backward()
            opt.step()
            train_loss += loss.item()

        net.eval()
        correct = 0
        total = 0
        ys, ps = [], []
        with torch.no_grad():
            for x, y in tqdm(val_dl, desc=f"Epoch {epoch}/{args.epochs} val"):
                x = x.to(device)
                y = torch.tensor(y).to(device)
                logits = net(x)
                pred = torch.argmax(logits, dim=1)
                correct += (pred == y).sum().item()
                total += y.numel()
                ys.extend(y.cpu().tolist())
                ps.extend(pred.cpu().tolist())

        acc = correct / max(1, total)
        print(f"\nEpoch {epoch}: train_loss={train_loss/len(train_dl):.4f} val_acc={acc:.4f}\n")
        print(classification_report(ys, ps, target_names=CLASSES, zero_division=0))
        print("Confusion matrix:\n", confusion_matrix(ys, ps))

        if acc > best_acc:
            best_acc = acc
            ckpt = {
                "model_name": args.model,
                "classes": CLASSES,
                "state_dict": net.state_dict(),
                "img_size": args.img,
            }
            torch.save(ckpt, out_dir / f"{args.vendor}.pt")
            (out_dir / f"{args.vendor}.json").write_text(json.dumps({
                "classes": CLASSES,
                "img_size": args.img,
                "model_name": args.model
            }, indent=2))
            print(f"Saved best model: models/{args.vendor}.pt (val_acc={best_acc:.4f})")

    print(f"Done. Best val accuracy: {best_acc:.4f}")

if __name__ == "__main__":
    main()
