# 头部视差 · 离轴投影 3DGS 查看器

用摄像头算出眼睛在屏幕前的位置，把屏幕四个角对应的视线塞进投影矩阵，网页里的内容就会随头部移动产生「绕进屏幕」的视差。渲染走 WebGPU，3D 内容可以是「一张照片 + 逐像素深度」的深度网格、老的照片分层，也可以是 3D 高斯泼溅（3DGS）。

单文件 `index.html`，不需要构建，CDN importmap 直接跑。

```
┌─ 摄像头 ─→ MediaPipe FaceLandmarker（478 关键点）
│                ↓ 虹膜 468/473 → 瞳距 + 位置
│            眼睛坐标 (x, y, z) 单位米
│                ↓ One Euro 滤波
└─→ 离轴投影矩阵（l/r/t/b 四边不对称）→ 相机 → 渲染

离线：照片 ─→ Depth Anything V2 ─→ 深度图 ─→ 剪影 + 背景底版 ─→ 两层网格
```

## 运行

```bash
cd off-axis-demo
python3 -m http.server 8123
# 打开 http://127.0.0.1:8123/index.html
```

必须用 http（或 https）打开，**不要双击 `index.html`**：`file://` 下浏览器会拦掉所有同目录文件，照片加载不出来、人脸模型也会报 `NetworkError`。页面检测到 `file://` 时会自己弹出醒目提示。

线上版（GitHub Pages）同样是 https，摄像头和本地模型都正常。

## 两种投影模式

| 模式 | 做法 | 特征 |
| --- | --- | --- |
| 真离轴 | 屏幕轴对齐，光轴 = 屏幕法线，四边不对称全塞进投影矩阵 | 屏幕四角在任意眼位都精确落在 NDC (±1, ±1)，像透过真实的窗框看 |
| 相机平移 | 相机跟着眼睛平移后 `lookAt` 场景中心 | 便宜，但灭点被锁在画面中央，房间会跟着「转」，丢掉了离轴的那部分视差 |

判断依据看走廊的灭点：近似模式下灭点始终在屏幕中心。

核心代码（`applyOffAxisCamera`）：

```js
const d = eye.z;                 // 眼到屏幕平面的距离
const k = NEAR / d;
const l = (-screen.w / 2 - eye.x) * k;
const r = ( screen.w / 2 - eye.x) * k;
const b = (-screen.h / 2 - eye.y) * k;
const t = ( screen.h / 2 - eye.y) * k;

camera.projectionMatrix.makePerspective(l, r, t, b, NEAR, FAR, renderer.coordinateSystem);
camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
camera.position.set(eye.x, eye.y, d);
camera.quaternion.identity();    // 不 lookAt
```

`renderer.coordinateSystem` 不能省：WebGPU 的 NDC 深度是 `0..1`，WebGL 是 `-1..1`，传错整幅画面会消失。

## 眼睛位置怎么算

1. 优先用虹膜中心 468 / 473，取不到回退到眼角 33 / 133 / 362 / 263。
2. 水平位置由双眼中点相对画面中心的偏移换算（按假定水平视场 60°），正负号取决于前置摄像头镜像，面板可切。
3. 深度用针孔模型 `z = z_ref × ipd_ref / ipd`，比「魔法常数除以眼距」稳定得多——瞳距是唯一随距离单调变化的量。
4. 滤波用 One Euro（低速重平滑、快速转头低延迟），不是固定指数平滑。
5. 按 `C` 把当前头部位置记为中立位。

## 内容

- **深度网格（默认）**：一张照片 + 逐像素深度，每个顶点都有自己的 z，头一转就按像素连续地产生视差。
- **分层贴片**：三层照片卡片（0.48 / 1.05 / 1.85 m），切过去对比能直接看出"贴纸感"差在哪 —— 卡片之间没有中间过渡，边缘永远是那块方角。
- **3DGS**：点「载入 3DGS 样本」加载 `model.spz`；也可以把 `.spz / .ply / .splat / .ksplat` 直接拖进窗口。扫描件朝向不对时点「旋转 90°」。
- **没摄像头也能用**：鼠标 = 头部（映射到 ±14 cm），滚轮 = 前后。

## 深度网格：把一张照片变成能转头看的东西

### 顶点怎么摆

参考眼位 `(0, 0, zRef)` 下，屏幕上 `(sx, 0)` 这条视线延伸到深度 `z` 时落在 `sx · (zRef + z) / zRef`，所以：

```js
const z = depthToZ(d);              // 0..1 深度 → 米（按 1/z 插值，视差和 1/z 成正比）
const s = (zRef + z) / zRef;        // 近处物体在世界里是变大的
pos = [nx * halfW * s, ny * halfH * s, -z];
```

写成 `sx * z / zRef`（把"眼到屏幕"当成"眼到物体"）是个很常见、很好看的错误：整张照片会缩小、四周漏边，看起来像被拉丝的贴纸。改对之后，眼睛在参考距离时渲染结果跟原图逐像素一致（实测 MSE < 1），转头才开始产生视差。

### 为什么要拆两层

一张连续网格上，「笔电 0.5 m → 墙 3 m」那条深度断层会被三角面拉成一张橡皮布，头一偏整个画面就液化。所以拆成：

| 层 | 内容 | 深度区间 |
| --- | --- | --- |
| 远景（满幅） | 补过洞的背景板 `depth/near_plate.jpg` | 0.66 – 3.20 m |
| 近景（剪影） | 桌面 + 笔电，alpha 来自 `depth/near_mask_near.png` | 0.40 – 0.66 m |

两层贴的是同一张照片、同一套 UV，交接处颜色天然一致；远景整幅都画，近景挪开的地方永远有底版兜着（单目深度看不到被挡住的像素，只能用同一张图的像素补）。

### 离线烘焙

```bash
# 1) 单目深度（Depth Anything V2 small, ONNX，784×490 约 0.5s/张）
python3 tools/estimate_depth.py model/depth-anything-v2-small.onnx img/near.jpg

# 2) 剪影遮罩 + 背景底版 + 两份分层深度
python3 tools/bake_layers.py img/near.jpg img/near.bin depth near
```

产出四张图，总共约 90 KB：

| 文件 | 作用 |
| --- | --- |
| `near_mask_near.png` | 近景剪影，导向滤波把边缘吸附到原图轮廓上 |
| `near_plate.jpg` | 背景底版，近景挡住的区域补成虚焦背景 |
| `near_plate_depth.png` | 底版深度（压到远景区间内，自身不再有断层） |
| `near_layer_depth.png` | 近景层深度（近景值向外补平，剪影处没有落差） |

换照片就改 `DEPTH_SCENE` 里的尺寸/近远距离，再跑一遍上面两步。

## 人脸模型的四个来源

按顺序尝试，任意一条通就能起：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `models/face_landmarker.task` | `fetch` 读取，有下载百分比 |
| 2 | `models/face_landmarker.b64.js` | 普通 `<script>` 加载 base64 副本，**不走 CORS**，`file://`、断网、iframe 里都能用 |
| 3 | Google 存储 | 境外网络可达时的兜底，实测 281 KB/s，3.5MB 要 13 秒，国内多半连不上 |
| 4 | 拖拽 / 「选择模型文件」 | 手动指定 `.task`，`file://` 下唯一能自洽救回来的办法 |

`models/face_landmarker.b64.js` 重新生成：

```bash
python3 - <<'PY'
import base64, pathlib
raw = pathlib.Path('models/face_landmarker.task').read_bytes()
b64 = base64.b64encode(raw).decode('ascii')
pathlib.Path('models/face_landmarker.b64.js').write_text(
    'window.__MP_FACE_MODEL_B64="' + b64 + '";\n', encoding='utf-8')
PY
```

追踪初始化分四段，每段独立超时：摄像头 12s → 运行时脚本 15s → 模型 45s（带百分比）→ wasm 下载 15/25s（同源）、30/60s（跨源），带百分比 → 建图 60s → 全败后读内置副本。

实测（`--use-fake-device-for-media-stream`）：

| 场景 | 点击到「等待人脸」 | wasm 来源 |
| --- | --- | --- |
| 本地 http（首次） | 493 ms | 本地副本 |
| GitHub Pages（首次） | 6.0 s | Pages 同源，`Content-Encoding: gzip` 压到 2.9 MB |
| 改之前（直连 CDN） | 28.4 s | jsDelivr，本地档 20 s 超时被打断后被迫换源 |
| fetch 全线失败（模拟受限环境） | 2.6 s | 内置副本 `wasm/vision_wasm_pack.js` |
| `file://` 直接打开 | 2.8 s | CDN 兜底（跨源 `import` 放行） |

## wasm 运行时：为什么必须放进仓库

`@mediapipe/tasks-vision` 的 npm 包里，`vision_bundle.mjs` 只有 200 KB 量级，真正的算力在 `vision_wasm_internal.wasm` 里 —— 这个文件 **9.4 MB**。

> ⚠️ `curl -w "%{size_download}"` 报的是**压缩后**大小（3.3 MB），别拿它判断体积。用 `node -e 'WebAssembly.compile(fs.readFileSync(f))'` 验证完整性，文件被截断会报 `section (code 11, "Data") extends past end of the module`。

从 CDN 拉这 9.4 MB，在国内几乎必然断在中途，断点处 emscripten 抛的就是：

```
Aborted(both async and sync fetching of the wasm failed). Build with -sASSERTIONS for more info.
```

这句话跟模型、跟 CORS 都没关系，**纯粹是 wasm 二进制没下全**。所以 `wasm/` 目录跟着仓库一起走：

```js
const MP_WASM_BASES = [
  'wasm',                    // 同源副本，秒开、断网可用
  `${NPMMIRROR}/wasm`,       // registry.npmmirror.com，国内实测 900 KB/s
  `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MP_VERSION}/wasm`,
  `https://unpkg.com/@mediapipe/tasks-vision@${MP_VERSION}/wasm`,
];
```

顺带实测了三个公网源的速度（同一个 137 KB 的 `vision_bundle.mjs`，深圳家宽）：

| 源 | 速度 |
| --- | --- |
| `registry.npmmirror.com` | **538 KB/s** |
| `cdn.jsdelivr.net` | 242 KB/s |
| `unpkg.com` | 60 KB/s |

差一个量级，9.4 MB 的 wasm 上更明显 —— 所以国内镜像排第一。GitHub Pages 上的实测也印证：同源 9.4 MB 在弱网下 34 秒只跑到 38%。

`FilesetResolver.forVisionTasks(base)` 只是拼路径 + 探测 SIMD 能力，**不会真下载 wasm**。实测它返回的就是两个字符串：

```js
{ wasmLoaderPath: 'wasm/vision_wasm_internal.js',
  wasmBinaryPath: 'wasm/vision_wasm_internal.wasm' }
```

所以下载权可以收回自己手里 —— 换成 `blob:` URL 它照样认：

```js
const paths  = await mp.FilesetResolver.forVisionTasks(base);   // 让它判断 SIMD / nosimd
const js     = await fetchBytes(paths.wasmLoaderPath, { timeout: 30000 });
const bin    = await fetchBytes(paths.wasmBinaryPath, { timeout: 120000, onProgress });
// \0asm 魔数不对就是拿到了 404 页面或半截文件，在这里拦下
const fileset = {
  wasmLoaderPath: URL.createObjectURL(new Blob([js],  { type: 'text/javascript' })),
  wasmBinaryPath: URL.createObjectURL(new Blob([bin], { type: 'application/wasm' })),
};
```

这么做换来三件事：

1. **能显示进度**。9.4 MB 在慢网络下要几十秒，不给百分比就是白屏。
2. **超时不会污染 emscripten**。直接交给它下载时，超时打断能让它的内部状态变坏，之后只会抛 `Aborted`；现在超时只 abort 我们自己的 `fetch`，可以干净地换下一个源重来。
3. **半截文件当场识别**，不用等它在 `createFromOptions` 里炸出一句看不懂的英文。

还有一处不做会白烧一倍流量：页面加载 350 ms 时会自动起一次追踪，用户点「重启追踪」又是一次完整流程。不缓存的话这 9.4 MB 会**实实在在下载两遍**（`performance.transferSize` 两条都是 9424286）。所以 `filesetFromBlob` 按 base 缓存 blob URL。

目录里只留 SIMD 版：

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `vision_wasm_internal.js` | 210 KB | emscripten glue |
| `vision_wasm_internal.wasm` | 9.4 MB | 真正的算力 |
| `vision_wasm_nosimd_internal.js` | 210 KB | 非 SIMD 浏览器的 glue（wasm 走 CDN 兜底） |
| `vision_wasm_pack.js` | 3.7 MB | gzip+base64 副本，fetch 被禁时的最后一道（`tools/pack_wasm.py` 生成） |

非 SIMD 浏览器的 9.3 MB `vision_wasm_nosimd_internal.wasm` 没有入库：现代浏览器（Chrome 91+ / Safari 16.4+ / Firefox 89+）都支持 SIMD，真遇上老浏览器会 404，然后自动落到 CDN 那一档。

删掉 nosimd wasm 之前确认过 SIMD 版能独立编译：

```bash
node -e 'const fs=require("fs"); WebAssembly.compile(fs.readFileSync("wasm/vision_wasm_internal.wasm"))
  .then(()=>console.log("OK")).catch(e=>console.log("FAIL",e.message))'
```

顺带一个反直觉的结论：**换版本救不了这个体积**。1.1.1 的 wasm 约 12 MB，0.10.14 是 9.4 MB，都是 9 MB 量级。

## 第三层：fetch 被整体禁用时（`NetworkError`）

现象：三个源全部报

```
wasm 运行时加载失败（本地 wasm：NetworkError when attempting to fetch resource.；
CDN wasm：NetworkError when attempting to fetch resource.；…）
```

`NetworkError when attempting to fetch resource.` 是 WebKit / Firefox 的措辞（Chrome 写 `Failed to fetch`）。**连同源相对路径都失败**，说明不是源的问题，是这台环境把 fetch 通道整体掐了。

实测两种受限环境（`file://` 打开、`sandbox` iframe 预览框，两者 `location.origin` 都是 `"null"`）：

| 通道 | 结果 |
| --- | --- |
| `fetch()` 同源 / 跨源 | ✗ NetworkError |
| `XMLHttpRequest` 同源 | ✗ |
| `import()` 同源模块 | ✗ |
| **classic `<script src>`** | ✓ |
| **`fetch(blob:…)`** | ✓ |
| **`DecompressionStream`** | ✓ |

所以兜底只要用「script 送字节 + blob 送文件」这一条：

```bash
python tools/pack_wasm.py    # wasm 9.0MB → gzip 2.7MB → base64 3.7MB 的普通脚本
```

页面在 fetch 三源全败（或 `navigator.onLine === false`）后，注入 `wasm/vision_wasm_pack.js`，把 base64 解码、`gunzip` 还原、转 `blob:` URL 交给 MediaPipe —— 全程不碰网络型 fetch。

验证：把 `window.fetch` 对 `*.wasm` 全部改成抛 `TypeError('NetworkError…')`，点击后 **2.6 s** 到「等待人脸」，`state.wasmBase === 'pack'`。

顺带一个附带收获：`file://` 打开现在也能跑（`import` 跨域 CDN 模块放行，wasm 走内置副本），不必再起本地服务。


## 第四层：真正的凶手是 `blob:`，不是 fetch

前面几层都在猜"哪个源不通"。最后用 `window.fetch` 包一层记录才发现：**下载全都成功了，挂在最后一步 —— 把 `blob:` URL 交给 emscripten。**

WorkBuddy 预览服务器的 CSP 长这样（逐字抄自响应头）：

```
default-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:;
connect-src 'self' http://127.0.0.1:* https://*.map.qq.com …
```

`default-src` 里明明有 `blob:`，但 **`connect-src` 显式列了源、却没有 `blob:`** —— 而 `connect-src` 是独立生效的，不继承 `default-src` 的 `blob:`。于是：

| 动作 | 结果 |
| --- | --- |
| `fetch('wasm/…wasm')` 同源下载 9.4 MB | ✓ 成功 |
| `URL.createObjectURL(bytes)` | ✓ |
| **`fetch(blob:…)`（emscripten 去取 wasm）** | ✗ Chrome `Failed to fetch` / Firefox `NetworkError…` |

因为失败发生在最后一步，**五个源报的是同一句话**，看起来就像"全都不通"。我第一次看到这个报错时把源换了一轮，方向从一开始就偏了。

修法：探测一次 blob 通道（一个 1 字节的 blob，代价可忽略），

- `blob` 通 → 保持原方案（自己下载 + 进度条 + 魔数校验，交给 MediaPipe 的文件是 blob URL）
- `blob` 堵 → 把 `FilesetResolver` 给的**原始同源 URL** 直接交给 emscripten，让它自己按 `'self'` 去取（这一步是 CSP 放行的）

面板会把这四件事分开显示，一眼能看出卡在哪：

```
通道 fetch 通 · xhr 通 · script 通 · blob 堵
```

`tools/csp_server.py` 就是用来复刻这个环境的：它逐字带上预览服务器的 CSP，起在 `127.0.0.1:8127`，用来验证"blob 被拦时能不能退回直连"。实测同一份代码：

| 环境 | 通道 | `state.trackerMode` |
| --- | --- | --- |
| 普通 `python3 -m http.server` | `… · blob 通` | `mp`（走 blob，有进度） |
| 带预览 CSP | `… · blob 堵` | `mp`（走同源直连） |


## 第五层：三条通道先探测，再决定怎么走

Firefox + `http:` 的实例里连 5 个来源都报同一句 `NetworkError when attempting to fetch resource.` —— 这时候再往下堆源是白费力气，得先回答"这台环境的哪条通道是活的"。

`fetch` / `XHR` / `<script src>` / `blob:` 是**四条互相独立**的通道：

| 谁在拦 | fetch | XHR | `<script src>` | `blob:` | `import()` |
| --- | --- | --- | --- | --- | --- |
| CSP `connect-src` | ✗ | ✗ | ✓ | ✗ | ✓（`script-src`） |
| CSP `connect-src` 没列 `blob:`（本项目真凶） | ✓ | ✓ | ✓ | ✗ | ✓ |
| 只改写了 `window.fetch` 的插件 | ✗ | ✓ | ✓ | ✓ | ✓ |
| `file://` / `sandbox` iframe | ✗ | ✗ | ✓ | ✓ | ✗ |
| 服务器把 9.4 MB 掐断 | ✗ | ✗ | — | — | — |

所以页面在取摄像头的同时，用两个 2 字节的侦察文件（`wasm/ping.txt`、`wasm/ping.js`）加一个 1 字节的 blob 各探一次，结论显示在面板的一行小字里：

```
通道 fetch 通 · xhr 通 · script 通 · blob 堵
```

判定刻意放宽 —— **只要拿到响应就算通**。404 说明网络好、文件不对；只有抛异常才是通道被锁。这个区别决定后面要不要白等 4 次 30 s 超时。

- fetch 通 + blob 通：本地 `wasm/` → 国内镜像 → jsDelivr → unpkg，自己下载并校验（有进度条）
- fetch 堵、XHR 通：同一批源自动改走 XHR（`downloadBytes` 里兜的），进度条照旧
- blob 堵：直接让 emscripten 读同源 URL，跳过 blob 转交（见上一节）
- 网络与内置副本全堵：**不再停在一句报错上**，见下一节

## 第六层：免下载追踪（纯 JS，不碰 wasm）

wasm、模型、CDN 全部不可达时，页面自动切到一个 40 行就能讲完的追踪器，永远能起来：

1. 摄像头画面缩到 160×120，按 YCbCr 取肤色（`Cr∈[133,178]`、`Cb∈[77,130]`、`Y>45`、`R>B+8`）
2. 连通域标记，按「面积 / 填充率 / 长宽比 / 离上一帧的距离 / 离画面中心的距离」打分，挑最像脸的那块
3. 中心与尺寸做指数平滑，单帧跳变超过画面 12% 视为误检，只轻推 8%

它输出的 (cx, cy, size) 和 MediaPipe 的 (虹膜中心, 瞳距) 走同一个 `applyHeadMetrics()`，所以**换追踪器不会改变视差手感**（比例由校准时锁定，只有绝对基准不同）。精度不如 478 点，但「头平移 → 视差」这件事成立，且不会因为加载失败而消失。

按钮也留了直通口：「免下载追踪（不依赖 wasm，立即可用）」。

三条路径的实测（Chromium + fake camera，`Fetch.failRequest` 只掐 `Fetch`/`XHR` 两类资源，精确复刻 CSP 场景）：

| 场景 | `state.trackerMode` | `state.wasmBase` | 结果 |
| --- | --- | --- | --- |
| 正常 | `mp` | `wasm`（本地） | 追踪启用 |
| fetch + XHR 全堵 | `mp` | `pack`（内置副本） | 追踪启用 |
| 再堵掉内置副本 | `simple` | `simple` | 追踪启用（降级但有得用） |


## 已知的坑

- **3DGS 只能走 WebGPU**：`GaussianSplat` 的 NodeMaterial 来自 `three/webgpu`，WebGL 后端渲染不出来。页面会自动降级到 WebGL 并说明原因。
- **WebGPU 后端不支持 `LineLoop`**：画矩形框要用 `LineSegments` 手工闭合。
- **`GaussianSplat` 的导入路径**是 `three/addons/objects/GaussianSplat.js`，不在 `loaders/` 下；PLY 要用 `GaussianSplatPLYLoader`，普通 `PLYLoader` 读出来没有协方差和球谐。
- **扫描件朝向不一致**：本例 `model.spz`（three.js 官方 `lion.v3.spz`）需要 `rotation.set(0, π, π)`。朝向参数只传在调用处，加载函数保持通用。
- **越远的内容位移越大**，看着反直觉但正确：屏幕是固定的窗框，内容都在窗后。眼位横移 16 cm 时，前景卡片移动约 8 cm、背景约 12 cm；但换算成视角，近处物体的角位移更大，这才是"转头"该有的观感。
- **剪影处别留深度坡**：近景层的深度必须一路"平"到剪影边缘。只要那里还有 0.9→0.55 的过渡值，侧看就是一条 16 cm 宽的拉伸带（俗称橡皮筋）。
- **遮罩别开 mipmap，三角面要双面**：剪影处 UV 变化极快，粗 mip 里的遮罩边缘是台阶，屏幕上就是"被啃掉一块"；坡道上的三角面还会翻面，`FrontSide` 会被背面剔除啃出锯齿。
- **裙边别压暗**：靠压暗藏边缘会在"近景裙边"和"背景板"之间露出一道亮度缝，比拉丝本身更显眼。
- **鼠标映射别给太大**：`±14 cm` 足够看出视差；映射到半屏时眼位会跑到屏幕边缘，单目深度根本补不出那块被挡住的像素。
- **`Aborted(both async and sync fetching of the wasm failed)` 不是模型问题**：是 9.4 MB 的 wasm 没下全，见上面「wasm 运行时」一节。同理，`-sASSERTIONS` 只会让错误更啰嗦，不会让它能跑。
- **同一页只能有一份 MediaPipe runtime**：同时引入 legacy `@mediapipe/pose` 和新版 `tasks-vision` 会撞出两个 emscripten 实例，症状同样是各种 `Aborted`（官方 issue #5495）。
- **`NetworkError when attempting to fetch resource.` 不是源坏了**：连本地相对路径都失败时，是这个环境把 fetch 通道整体掐了（`file://`、`sandbox` iframe 都这样）。此时 `import` 同源、`XHR` 也一起失败，但 classic `<script>` 和 `fetch(blob:)` 还通 —— 见上面「第三层」一节。
- **`blob:` 也要单独探**：`connect-src` 不继承 `default-src` 里的 `blob:`。预览类环境（CSP 显式列了源却没列 blob）会放行 fetch、放行 script，却在"把 blob 交给 emscripten"这一步拒绝 —— 表现就是**所有源报同一句 NetworkError**，像极了源全挂。遇到这种报错先看面板的 `blob` 那一栏。
- **别再靠"多加几个 CDN"解决 `NetworkError`**：五个源报同一句话时，问题在通道不在源。先看面板那行 `通道 fetch / xhr / script`，再去决定走哪条路。
- **降级要用户看得见**：面板会写清「已切到免下载追踪 + MediaPipe 未启用的原因」，而不是把一段红色报错丢给用户。追踪本身不该因为 wasm 加载失败而不可用。
- **GPU delegate 会静默卡死**：`baseOptions.delegate: 'GPU'` 在部分驱动上既不返回也不报错。检测本身是每 45 ms 一次，CPU 在 M 系列上 6–8 ms/帧完全够，所以只用 CPU。

## 素材

- 照片：`picsum.photos` id 48（书桌）作主场景，43 / 50 用于分层贴片对比；本地取图失败时自动回退到远端同款。
- 深度模型：`onnx-community/depth-anything-v2-small` 的 `model_quantized.onnx`（约 27 MB，仅离线用，不进仓库）。
- 3DGS 样本：three.js 官方示例 `examples/models/spz/lion.v3.spz`（195k splats，4.1MB）。
- 人脸模型：MediaPipe `face_landmarker` float16。
- wasm 运行时：`@mediapipe/tasks-vision@0.10.14` 的 `wasm/`（Apache-2.0），只留 SIMD 版。
- 开源参考与踩坑对照：见 `REFERENCES.md`。

## 授权

`model.spz` 与 `img/` 仅用于演示，版权归原作者；生产环境请替换为自己的素材。
