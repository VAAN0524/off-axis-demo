#!/usr/bin/env python3
"""离线验证：把前景层平移 band 像素，看露出的地方是"修好的背景"还是"原前景残影"。

    python3 verify_ldi.py ldi img/near.jpg 37

输出 /tmp/verify.png（左：合成；右：露出区标记）+ 残影像素占比。
"""
import sys
import numpy as np
from PIL import Image
from scipy import ndimage as ndi

d, photo, shift = sys.argv[1], sys.argv[2], int(sys.argv[3])
fg = Image.open(d + '/fg.jpg').convert('RGB')
al = Image.open(d + '/fg_alpha.png').convert('L')
bg = Image.open(d + '/bg.jpg').convert('RGB')
rgb = Image.open(photo).convert('RGB')
W, H = fg.size
A = np.asarray(al, np.float32)[..., None] / 255
F = np.asarray(fg, np.float32) / 255
B = np.asarray(bg, np.float32) / 255
O = np.asarray(rgb, np.float32) / 255

# 前景层整体平移（近似"眼睛横移到极限"）
Fs = np.roll(F, shift, axis=1)
As = np.roll(A, shift, axis=1)
out = As * Fs + (1 - As) * B

# 露出区：原来被前景挡住、平移后没被挡住
reveal = (A[..., 0] > 0.5) & (As[..., 0] < 0.5)
# 残影：bg 在该处与原图几乎一样 → 说明没修，露出来的是原物体的像素
diff = np.abs(B - O).max(-1)
resid = reveal & (diff < 0.02)
print('露出区 %.2f%%  其中残影 %.2f%%（占露出区 %.1f%%）'
      % (100 * reveal.mean(), 100 * resid.mean(), 100 * resid.sum() / max(reveal.sum(), 1)))

vis = out.copy()
vis[resid] = [1, 0, 0.2]
Image.fromarray((np.clip(vis, 0, 1) * 255).astype(np.uint8)).save('/tmp/verify.png')
Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)).save('/tmp/composite.png')
print('saved /tmp/verify.png /tmp/composite.png')
