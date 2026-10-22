#!/usr/bin/env python3
"""gen_cards_qwen.py — 用本地 qwenimage-studio(:8630) 把一张照片/一句描述变成 cards.html 三件套资产

产出（对齐 CARDS.md v3 资产规范）:
  <out>/bg.jpg     背景板：去主体 + 四周外扩（qwen inpaint 两步）
  <out>/subj.png   主体卡：RGBA（qwen multiref rgba=true 透明提取；或原像素×alpha 合成）
  <out>/meta.json  层/深度/裁剪框/QA

用法:
  python3 gen_cards_qwen.py --photo img/near.jpg --out cards/gen1
  python3 gen_cards_qwen.py --describe "一台贴着贴纸的银色笔记本电脑，桌上有一杯咖啡" --out cards/gen2
  可选: --server http://127.0.0.1:8630  --subj-source qwen|composite  --pad 0.10

依赖: 标准库 + Pillow（仅 alpha→mask 与合成用；缺失时 subj-source=composite 不可用）
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

try:
    from PIL import Image, ImageFilter
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

SERVER = "http://127.0.0.1:8630"
TIMEOUT = 60
JOB_TIMEOUT = 900          # 透明抠图实测 ~7 分钟
POLL = 3.0

CUTOUT_PROMPT = ("把 <image1> 里的主体抠出来，背景变透明，保持主体外观颜色细节不变")
REMOVE_PROMPT = ("完全移除被遮罩覆盖区域内的主体对象，用周围背景自然延续填补，"
                 "不留残影和边界，透视光线与原图一致")
EXTEND_PROMPT = ("将被遮罩覆盖的画面边缘区域用背景内容自然向外延展，"
                 "透视、砖缝走向、光影与原图连续，不留接缝")


def api(path, payload=None, files=None, timeout=TIMEOUT):
    url = SERVER + path
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in payload.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (fname, data, ctype) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(payload or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_job(job_id, label):
    t0 = time.time()
    while True:
        with urllib.request.urlopen(f"{SERVER}/api/job/{job_id}", timeout=TIMEOUT) as r:
            st = json.loads(r.read())
        state = st.get("status") or st.get("state")
        prog = st.get("progress", "")
        print(f"  [{label}] {state} {prog} ({time.time()-t0:.0f}s)", end="\r", flush=True)
        if state in ("done", "completed", "finished"):
            print(f"  [{label}] 完成 ({time.time()-t0:.0f}s)            ")
            return st
        if state in ("error", "failed"):
            raise RuntimeError(f"{label} 失败: {st}")
        if time.time() - t0 > JOB_TIMEOUT:
            raise RuntimeError(f"{label} 超时 ({JOB_TIMEOUT}s)")
        time.sleep(POLL)


def fetch_image(filename, subfolder, out_path):
    url = f"{SERVER}/api/image?filename={urllib.request.quote(filename)}&subfolder={urllib.request.quote(subfolder or '')}"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        out_path.write_bytes(r.read())


def upload(path):
    data = Path(path).read_bytes()
    r = api("/api/upload", payload={"overwrite": "true"},
            files={"file": (Path(path).name, data, "image/png" if str(path).endswith("png") else "image/jpeg")})
    return r["name"]


def subject_cutout(photo_name):
    """multiref + rgba=true：真照片 → 透明主体 PNG（配方铁律：槽位引用/strength1.0）"""
    r = api("/api/multiref", payload={
        "prompt": CUTOUT_PROMPT,
        "refs": [photo_name],
        "rgba": True,
        "strength": 1.0,
    })
    st = wait_job(r["job_id"], "主体透明提取")
    return st.get("filename"), st.get("subfolder")


def inpaint(photo_path, mask_img, instruction, label):
    name = upload(photo_path)
    mask_path = Path(str(photo_path) + ".mask.png")
    mask_img.save(mask_path)
    mask_name = upload(mask_path)
    r = api("/api/inpaint", payload={
        "image": name, "mask": mask_name, "instruction": instruction, "denoise": 1.0,
    })
    st = wait_job(r["job_id"], label)
    return st.get("filename"), st.get("subfolder")


def pad_canvas(img, pad_frac):
    """四周外扩 pad_frac，返回(新画布, 原图在画布中的偏移)"""
    pw, ph = int(img.width * pad_frac), int(img.height * pad_frac * 0.6)
    canvas = Image.new("RGB", (img.width + pw*2, img.height + ph*2))
    canvas.paste(img, (pw, ph))
    return canvas, (pw, ph)


def ring_mask(size, inner_rect, feather=6):
    """外扩环带 mask（只修露出带，不修整块——CARDS.md 教训 #2）"""
    m = Image.new("L", size, 255)
    inner = Image.new("L", size, 0)
    inner.paste(255, inner_rect)
    m.paste(inner, (0, 0))
    return m.filter(ImageFilter.GaussianBlur(feather))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", help="真实照片路径（走抠主体+去主体+外扩）")
    ap.add_argument("--describe", help="纯文字描述（走 /api/rgba 生成主体 + t2i 生成背景）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--bg-desc", default=None, help="背景板描述（默认从主描述派生空镜）")
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--subj-source", choices=["qwen", "composite"], default="qwen",
                    help="qwen=生成主体整卡（素材场景）; composite=原像素×生成alpha（保真实身份，需 Pillow）")
    ap.add_argument("--pad", type=float, default=0.10)
    args = ap.parse_args()
    if not args.photo and not args.describe:
        ap.error("--photo 或 --describe 必须二选一")
    globals()["SERVER"] = args.server
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ── 0. 描述模式：先文生图出完整场景，之后与照片走同一条管线（像素同源 → 天然对齐） ──
    if args.describe:
        print("[0/3] 文生图生成完整场景 (/api/t2i)")
        bg_desc = getattr(args, "bg_desc", None) or (args.describe + "，空无一人的场景空镜，主体位置留空，柔和光线，浅景深虚化")
        r = api("/api/t2i", payload={
            "prompt": bg_desc, "width": 1152, "height": 768,
        })
        st = wait_job(r["job_id"], "t2i 场景")
        scene_path = out / "_scene.png"
        fetch_image(st["filename"], st.get("subfolder"), scene_path)
        args.photo = str(scene_path)
        if args.subj_source == "qwen":
            args.subj_source = "composite"   # 描述模式强制 composite：场景像素 × 生成 alpha，杜绝双影

    # ── 1. 主体卡 subj.png ──
    print("[1/3] 透明抠主体 (/api/multiref rgba)")
    name = upload(args.photo)
    fname, sub = subject_cutout(name)
    fetch_image(fname, sub, out / "subj_qwen.png")
    if args.subj_source == "composite":
        if not HAS_PIL:
            sys.exit("composite 需要 Pillow")
        orig = Image.open(args.photo).convert("RGB")
        q = Image.open(out / "subj_qwen.png").convert("RGBA")
        q = q.resize(orig.size)
        comp = Image.new("RGBA", orig.size)
        comp.paste(orig, (0, 0))
        comp.putalpha(q.getchannel("A"))
        comp.save(out / "subj.png")
        print("  composite：原像素 × 生成 alpha（身份零漂移）")
    else:
        import shutil
        shutil.copy(out / "subj_qwen.png", out / "subj.png")
        print("  注意：qwen 主体为重绘（姿势可能漂移），素材场景可接受")

    # ── 2. 背景板 bg.jpg（去主体 + 外扩） ──
    subj_alpha = Image.open(out / "subj.png").getchannel("A") if HAS_PIL else None
    print("[2/3] 背景板：去主体 + 外扩 (/api/inpaint ×2)")
    base = Image.open(args.photo).convert("RGB")
    if HAS_PIL and subj_alpha is not None:
        a = subj_alpha.resize(base.size)
        mask = a.point(lambda v: 255 if v > 40 else 0).filter(ImageFilter.MaxFilter(9))
    else:
        mask = Image.new("L", base.size, 255)
    fname, sub = inpaint(args.photo, mask, REMOVE_PROMPT, "去主体")
    plate_path = out / "_plate.png"
    fetch_image(fname, sub, plate_path)
    plate = Image.open(plate_path).convert("RGB")
    padded, (offx, offy) = pad_canvas(plate, args.pad)
    if HAS_PIL:
        rmask = ring_mask(padded.size, (offx, offy, offx + plate.width, offy + plate.height))
        fname2, sub2 = inpaint(padded, rmask, EXTEND_PROMPT, "外扩")
        padded = out / "_padded_inpaint.png"
        fetch_image(fname2, sub2, padded)
        padded = Image.open(padded).convert("RGB")
    padded.save(out / "bg.jpg", quality=90)

    # ── 3. meta.json（v3 规范 + QA） ──
    subj = Image.open(out / "subj.png")
    bg = Image.open(out / "bg.jpg")
    pad_x = int(bg.width * args.pad * 0.5)
    pad_y = int(bg.height * args.pad * 0.3)
    meta = {
        "version": 3, "mode": "cards",
        "image": {"w": subj.width, "h": subj.height},
        "scene": {"zRef": 0.60, "halfWM": 0.35, "eyeMax": 0.075},
        "pad": {"x": pad_x, "y": pad_y},
        "layers": [
            {"id": "bg",   "file": "bg.jpg",   "z": 1.60, "order": 0,
             "rect": [-pad_x, -pad_y, subj.width + pad_x, subj.height + pad_y]},
            {"id": "subj", "file": "subj.png", "z": 0.35, "order": 1,
             "rect": [0, 0, subj.width, subj.height]},
        ],
        "qa": {"source": "qwen-image-2.1-turbo", "note": "生成式素材；pad 需 ≥ reveal×1.15"}
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[3/3] 完成 → {out}/ (bg.jpg / subj.png / meta.json)")
    print("验证: 打开 cards.html?cards=" + str(out))


if __name__ == "__main__":
    main()
