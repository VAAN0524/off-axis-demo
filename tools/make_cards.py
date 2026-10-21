#!/usr/bin/env python3
"""照片 → 卡片栈（2 张前后景片 + 生成式补边），替掉 LDI 网格。

    python3 tools/make_cards.py img/near.jpg img/near.bin cards/near

为什么换掉 LDI 网格
------------------
网格是把整张照片拆成 480×300 个顶点，每个顶点按**自己那一像素的深度**算
位置。这条路上有三个躲不掉的毛病：

1. 同一块面内深度有微差 → 每个顶点缩放不同 → 面被剪切成"扭曲"；
2. 轮廓两侧相邻顶点深度差一大截 → 它们之间的那个三角形被拉成橡皮布，
   颜色跨着轮廓互相渗透 → 剪影一圈"锯齿 + 亮毛"；
3. 网格只覆盖原图，眼睛一移，边缘就露底 → 需要凭空补一整圈。

卡片栈把"面片"当成**刚体**：每层只有一个深度值，整层做同一个位移。
一块正对镜头的平面在针孔相机下本来就只有平移（没有变形），
所以这个简化不是偷工，是**物理上正确的那一半**：

    屏幕位移(px) = eye · k · zRef / (zRef + z)

层内零形变 → 没有扭曲；轮廓是真 alpha 遮罩整体平移 → 边缘就是原照片的边缘；
背景板四周先做生成式外扩 → 怎么移都不会露白。

代价：层内没有次生视差（背景墙自己不会微微变形）。眼位 7.5cm 这个量级下，
相对位移只有几十像素，这点损失换掉整片扭曲，非常划算。

产出
----
    bg.jpg        背景板：主体已抹掉，四周外扩 pad（默认 10%）
    subj.png      主体卡片：原图真实像素 + 软 alpha（已做去边色）
    meta.json     每层的 z、偏移、裁剪框

用法补充
--------
    --pad 0.10        四周外扩比例
    --z-subj 0.25     主体卡片相对屏幕的深度（米），越小越"贴脸"
    --z-bg 1.60       背景板深度
    --mirror-hf 0.5   外扩区高频借用镜像原图的比例（0=纯生成，1=纯镜像质地）
    --no-lama         不跑模型，只做低频外推（秒出，用于调渲染）
    --plate <图>      外部生成好的背景板（主体已抹掉、四周外扩过），
                      有它就不跑本地补全 —— 这是"云模型生图"的接入口
    --layers 1|2      强制层数。默认 auto：有独立主体就 2 层，风景照 1 层
"""
import json
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bake_ldi as B  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
Z_REF = B.Z_REF
HALF_W_M = B.HALF_W_M
EYE_MAX = B.EYE_MAX


def argval(flag, default, cast=float):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return default


# ── 去边色：把半透明像素里混进去的旧背景剥出来 ────────────────────────
def decontaminate(rgb, alpha, bg, lo=0.12, hi=0.995):
    """
    抠图边缘一圈像素，颜色其实是 F·a + B·(1-a)——里面掺了**旧背景**的颜色。
    这张卡片一旦滑到别的位置，那圈旧背景色就成了一道亮/暗边，也就是
    "毛边"在合成阶段的真正来源。已知新背景 B（就是背景板），就能反解：

        F = (I - (1-a)·B) / a

    a 太小的像素反解会炸，所以只处理 0.03 < a < 0.995 的这一圈，结果裁到 [0,1]。
    """
    a = alpha[..., None]
    f = (rgb - (1.0 - a) * bg) / np.maximum(a, 1e-2)
    m = ((alpha > lo) & (alpha < hi))[..., None]
    return np.where(m, np.clip(f, 0, 1), rgb).astype(np.float32)


# ── 外扩：LaMa 定调 + 镜像借高频 ──────────────────────────────────────
def outpaint(lama, rgb, pad_x, pad_y, mirror_hf=0.5, sigma=9.0):
    """
    只靠 LaMa 外扩，出来是一条糊边（模型在 512 里看不到足够上下文）；
    只靠镜像，出来是一次明显的翻折（边缘有强结构时像照镜子）。

    合起来：LaMa 负责"颜色和走势对"，镜像的**高频**负责"质地是砖就还是砖"。

        ext = LaMa + (mirror - blur(mirror))

    镜像用 symmetric（沿边框翻折，边界连续），所以高频那一项在接缝处为 0，
    不会在接缝上画出硬线。
    """
    H, W = rgb.shape[:2]
    canvas = np.pad(rgb, ((pad_y, pad_y), (pad_x, pad_x), (0, 0)), mode='symmetric')
    ring = np.ones(canvas.shape[:2], bool)
    ring[pad_y:pad_y + H, pad_x:pad_x + W] = False
    if lama is None:
        ext = canvas.copy()
    else:
        ext = B.align_low(lama(canvas, ring), canvas, ring)
    if mirror_hf > 0:
        low = np.stack([ndi.gaussian_filter(canvas[..., c], sigma) for c in range(3)], -1)
        hf = (canvas - low) * mirror_hf
        ext = np.clip(ext + hf, 0, 1)
    ext[pad_y:pad_y + H, pad_x:pad_x + W] = rgb
    return ext.astype(np.float32)


# ── 露出带：分块送模型，别整幅缩到 512 ────────────────────────────────
def ring_fill(lama, img, ring, win=512, stride=256, feather=40):
    """
    整幅图（960×600）直接送进 512 的 LaMa，等于把画面缩到 0.53 倍再补 ——
    68px 宽的露出带在模型眼里只剩 36px，出来必然是一块平滑的米色，
    挪一下头就是一根"贴上去的柱子"（实测右移 7.5cm 时笔电右边立起一根）。

    改成：只在露出带附近开窗口，一块一块补。窗口 512 时模型是 1:1 甚至放大
    在看，砖缝、桌沿这些结构留得住；窗口重叠一半、按羽化权重融合，接缝不可见。
    """
    h, w = ring.shape
    acc = np.zeros_like(img)
    wsum = np.zeros((h, w), np.float32)
    ys, xs = np.nonzero(ring)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1

    def feath(shape):
        a, b = shape
        yy = np.minimum(np.arange(a), np.arange(a)[::-1]) + 1
        xx = np.minimum(np.arange(b), np.arange(b)[::-1]) + 1
        return np.clip(np.minimum(yy[:, None], xx[None, :]) / max(1, feather), 0, 1)

    def starts(a0, a1, limit):
        xs = list(range(a0, a1 + 1, stride))
        if xs[-1] + win < a1 or len(xs) == 1:
            xs.append(a1)
        return sorted({min(max(0, t), max(0, limit - win)) for t in xs})

    for ty in starts(y0, y1, h):
        for tx in starts(x0, x1, w):
            ry0, rx0 = max(0, ty), max(0, tx)
            ry1, rx1 = min(h, ty + win), min(w, tx + win)
            sub = ring[ry0:ry1, rx0:rx1]
            if not sub.any():
                continue
            out = lama(img[ry0:ry1, rx0:rx1], sub)
            wt = feath(sub.shape) * sub
            acc[ry0:ry1, rx0:rx1] += out * wt[..., None]
            wsum[ry0:ry1, rx0:rx1] += wt
    out = img.copy()
    m = wsum > 1e-6
    out[m] = acc[m] / wsum[m][..., None]
    return np.clip(out, 0, 1)


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return
    photo, dbin, outdir = sys.argv[1:4]
    pad = argval('--pad', 0.10)
    z_subj = argval('--z-subj', 0.35)
    z_bg = argval('--z-bg', 1.60)
    mirror_hf = argval('--mirror-hf', 0.5)
    max_side = argval('--max-side', 1600, int)
    use_lama = '--no-lama' not in sys.argv
    plate_path = None
    if '--plate' in sys.argv:
        plate_path = sys.argv[sys.argv.index('--plate') + 1]

    os.makedirs(outdir, exist_ok=True)

    color = Image.open(photo).convert('RGB')
    if max(color.size) > max_side:
        color = color.resize((max_side, round(color.height * max_side / color.width)), Image.LANCZOS)
    W, H = color.size
    rgb = np.asarray(color, dtype=np.float32) / 255.0
    lum = np.asarray(color.convert('L'), dtype=np.float32) / 255.0
    print(f'照片 {W}×{H}')

    raw = np.fromfile(dbin, dtype='<u2').astype(np.float32)
    side = int(round(np.sqrt(raw.size * 600 / 960)))
    d0 = raw.reshape(side, raw.size // side) / 65535.0
    d = B.sharpen_depth(d0, lum, (H, W))

    # ── 主体遮罩：语义优先，深度片兜底 ──
    sem_path, lama_path = os.path.join(ROOT, 'model', 'u2netp.onnx'), os.path.join(ROOT, 'model', 'lama_fp32.onnx')
    fg, _, _, _ = B.segment(d, lum)
    guard_hit = False
    obj = np.zeros((H, W), bool)
    if os.path.exists(sem_path):
        a_obj = B.Semantic(sem_path)(rgb)
        obj = a_obj > 0.5
        if obj.mean() > 0.85 or obj.mean() < 0.002:
            obj[:] = False
        elif obj.any():
            md = float(np.median(d[obj]))
            cand = fg & ndi.binary_dilation(obj, B.disk(30)) & (np.abs(d - md) < 0.13)
            obj |= ndi.binary_propagation(obj, mask=cand)
            # 语义守卫：u2netp 的"显著"不等于"最近"。风景照里它会把桥、
            # 水面这种横贯画面的东西整块当主体，用它抠图等于把照片撕掉半张。
            ys, xs = np.nonzero(obj)
            bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
            if (obj.mean() > 0.30 or (bw * bh) / float(W * H) > 0.55
                    or bw > W * 0.85 or bh > H * 0.9):
                print('语义守卫触发：没有可拆的独立主体')
                obj = np.zeros((H, W), bool)
                guard_hit = True

    # 单层 / 双层：这是整套方案里最重要的一个自适应判断。
    #
    # 双层（主体卡 + 背景卡）只有在"有一个**独立物体**"时才是对的 —— 物体的
    # 轮廓是真实的遮挡边界，卡片整体平移 = 物体在动，物理自洽。
    #
    # 而风景照里没有物体，深度片切出来的是一大块不规则的"近景板"（船头甲板、
    # 前景礁石）。把它当卡片平移 86px，等于把一大片真实像素整体拖走 —— 拖过去
    # 的内容既不属于那个深度，也不会带回它自己的次生视差，越看越怪。
    # 这种图正确的做法是**只做单层**：整张照片当一个卡片微微前移，没有相对位移，
    # 也就没有任何需要凭空生成的地方，零瑕疵。人在看远处风景时本来就几乎没有
    # 视差，这个选择在感知上也是对的。
    auto_single = guard_hit and '--layers' not in sys.argv
    forced_single = ('--layers' in sys.argv and sys.argv[sys.argv.index('--layers') + 1] == '1')
    if auto_single or forced_single:
        print('单层模式：整张照片当一块卡片，无相对位移 = 无露出带 = 无需生成')
        subject = np.zeros((H, W), bool)
    else:
        subject = obj if obj.mean() > 0.004 else fg
    if subject.mean() < 0.004:
        if not auto_single and not forced_single:
            print('!! 找不到主体，退化成单张卡片')
        subject = np.zeros((H, W), bool)

    alpha = B.soft_alpha(subject, lum) if subject.any() else np.zeros((H, W), np.float32)
    if subject.any():
        # soft_alpha 末尾那道大半径"吸附"偶尔会在主体内部打洞（实测船景
        # 有 0.7% 的主体像素被吸到 0.5 以下）。洞虽小，但背景板在那里被抹掉了，
        # 中心位就会从洞里漏出补出来的内容 —— 一条脏点。所以离轮廓 2px 以上的
        # 地方一律强制 a=1，软边只允许活在最外 2px。
        inner = ndi.distance_transform_edt(subject) > 2.0
        alpha = np.where(inner, 1.0, alpha).astype(np.float32)

    # ── 位移预算：决定"要修多宽的一圈"和"外扩够不够" ──
    def shift_px(z):
        return EYE_MAX * (W / (2 * HALF_W_M)) * Z_REF / (Z_REF + z)

    s_sub, s_bg = shift_px(z_subj), shift_px(z_bg)
    reveal = s_sub - s_bg                     # 主体相对背景多走的像素
    band = int(max(24, reveal * 1.35 + 8))    # 多留 35%，免得补出来的接缝正好卡在露出边界

    # ── 背景板：只修"轮廓内的一圈"，不修整块主体 ──
    #
    # 这是这张图之前最大的败因：把整台笔电（占画面 31%）框成一个大洞送进 LaMa。
    # 512 的模型面对 1/3 画面的洞、四周全是同色砖墙，只能靠"最省事"的方式填空，
    # 于是补出一台糊掉的笔电 —— 越修越像有东西。
    #
    # 但主体卡片一移，真正会露出来的只有**沿轮廓、宽度等于相对位移的那一圈**。
    # 圈里面永远被主体卡片盖着，补它毫无意义。所以只修这一圈：
    # 洞占总面积 5% 左右，外面全是真实像素，LaMa 在这里几乎不会失手。
    lama = None
    if use_lama and os.path.exists(lama_path):
        lama = B.Lama(lama_path)
    pad_x, pad_y = int(round(W * pad)), int(round(H * pad))
    bg = rgb.copy()
    if plate_path:
        # 外部生成好的背景板：等比铺满"照片 + 外扩"这块画布，居中裁切。
        # 它应该已经"没有主体、四周续过"，所以本地补全和外扩都跳过。
        plate = Image.open(plate_path).convert('RGB')
        tw, th = W + 2 * pad_x, H + 2 * pad_y
        k = max(tw / plate.width, th / plate.height)
        plate = plate.resize((max(tw, round(plate.width * k)),
                              max(th, round(plate.height * k))), Image.LANCZOS)
        cx, cy = (plate.width - tw) // 2, (plate.height - th) // 2
        bg = np.asarray(plate.crop((cx, cy, cx + tw, cy + th)), np.float32) / 255.0
        print(f'背景板：{plate_path}')
    else:
        if subject.any():
            hole_full = ndi.binary_dilation(subject, B.disk(7))
            filled = ring_fill(lama, rgb, hole_full) if lama is not None else B.push_pull(bg, ~hole_full)
            # 替换范围必须**和卡片的 alpha 对齐**（paint = alpha>0.05），不能用
            # 主体遮罩本身：遮罩边缘那 2px 里 alpha 可能是 0.02，一旦那里被抹掉，
            # 中心位就会从"卡片几乎透明"的地方漏出补出来的内容（实测船景有 832 个
            # 像素的误差超过 8/255）。卡片不画的像素，背景板就一定保留原样。
            paint = alpha > 0.12   # 与 decontaminate 的 lo 一致，见那里
            bg = np.where(paint[..., None], filled, rgb)
        bg = np.clip(bg, 0, 1)
        bg = outpaint(lama, bg, pad_x, pad_y, mirror_hf)

    subj_rgb = decontaminate(rgb, alpha, bg[pad_y:pad_y + H, pad_x:pad_x + W])

    # ── 主体卡片：裁到外接框，省掉大片全透明 ──
    if subject.any():
        ys, xs = np.nonzero(alpha > 0.01)
        x0, x1 = int(max(0, xs.min() - 6)), int(min(W, xs.max() + 7))
        y0, y1 = int(max(0, ys.min() - 6)), int(min(H, ys.max() + 7))
    else:
        x0, y0, x1, y1 = 0, 0, W, H

    Image.fromarray((bg * 255).astype(np.uint8)).save(
        os.path.join(outdir, 'bg.jpg'), quality=93, subsampling=1)
    if subject.any():
        card = np.dstack([subj_rgb[y0:y1, x0:x1], alpha[y0:y1, x0:x1]])
        Image.fromarray((np.clip(card, 0, 1) * 255).astype(np.uint8), 'RGBA').save(
            os.path.join(outdir, 'subj.png'), optimize=True)

    layers = [{
        'id': 'bg', 'file': 'bg.jpg', 'z': z_bg, 'order': 0,
        'rect': [-pad_x, -pad_y, W + 2 * pad_x, H + 2 * pad_y],
    }]
    if subject.any():
        layers.append({
            'id': 'subj', 'file': 'subj.png', 'z': z_subj, 'order': 1,
            'rect': [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
        })

    # 单层时没有相对位移，外扩只需要盖住自己那点位移（s_bg）；
    # 双层时最大位移来自主体卡，它决定了剪影旁要做多宽的一条带。
    need = reveal if subject.any() else s_bg
    have = min(pad_x, pad_y)
    meta = {
        'version': 3, 'mode': 'cards',
        'image': {'w': W, 'h': H},
        'scene': {'zRef': Z_REF, 'halfWM': HALF_W_M, 'eyeMax': EYE_MAX},
        'pad': {'x': pad_x, 'y': pad_y},
        'layers': layers,
        'qa': {
            'reveal_pct': round(need / W * 100, 2),
            'shift_subject_px': round(s_sub, 1),
            'shift_background_px': round(s_bg, 1),
            'reveal_px': round(need, 1),
            'pad_px': have,
            'pad_ok': bool(have >= need * 1.15),
        },
    }
    with open(os.path.join(outdir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f'主体占比 {subject.mean() * 100:.1f}%   z 主体 {z_subj}m / 背景 {z_bg}m')
    print(f'位移预算：主体 {s_sub:.0f}px  背景 {s_bg:.0f}px  露出 {need:.0f}px  '
          f'外扩 {have}px  {"OK" if meta["qa"]["pad_ok"] else "不足！"}')
    for p in ('bg.jpg', 'subj.png'):
        f = os.path.join(outdir, p)
        if os.path.exists(f):
            print(f'  {p:<10} {os.path.getsize(f) / 1024:6.0f} KB')
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    main()
