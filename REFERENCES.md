# 开源参考项目

按「能不能直接解决我们当前的问题」排序，不是按 star 数。

## 一、同题：头部追踪 + 离轴投影

### 1. RmaNMetaverse/Cam2Hologram-Window-into-3D-reality-

- https://github.com/RmaNMetaverse/Cam2Hologram-Window-into-3D-reality-
- JavaScript / 2026-08 / 2★
- 技术栈与本项目完全一致：MediaPipe FaceLandmarker + three.js 离轴投影。

**最值得抄的是它的加载策略**（`js/tracker.js`）：

```js
const LOCAL_BASE = new URL('../vendor/', import.meta.url).href;

const BUNDLE_SOURCES = [
  { label: 'local',    module: `${LOCAL_BASE}mediapipe/vision_bundle.js`, wasm: `${LOCAL_BASE}mediapipe/wasm` },
  { label: 'jsdelivr', module: 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.0.1/vision_bundle.mjs', ... },
  { label: 'unpkg',    ... },
];
```

它把 `vision_bundle` + wasm + 模型全部 vendored 进仓库（仓库 39MB 里大头就是这些），正常安装下**一次网络请求都不发**；CDN 条目只作为残缺 checkout 的兜底。而且路径用 `import.meta.url` 锚定，所以部署在 GitHub Pages 的 `/repo-name/` 子路径下也不会 404。

> 本项目的模型已经走「本地 task → base64 副本 → Google」三路，但 **wasm 运行时仍只走 CDN**。把它一起 vendor 掉，断网时才算真的能跑。

### 2. icurtis1/off-axis-sneaker

- https://github.com/icurtis1/off-axis-sneaker
- TypeScript / React / 435★
- Johnny Chung Lee 式 head-coupled perspective，把球鞋「放在屏幕后面」。

值得借鉴的产品化细节：

- `CalibrationWizard.tsx`：引导用户输入屏幕尺寸、眼睛高度、观看距离，算出物理单位，存本地
- `CdnStatusIndicator.tsx`：把 CDN 可达性做成可见状态，而不是让用户对着白屏猜
- `useFaceLandmarker` / `useFaceMesh` 拆成两个 hook：媒体流与推理分离，推理失败可以单独重试
- 独立渲染循环：摄像头帧率与渲染帧率解耦

### 3. Dushannavodya/depthShift

- https://github.com/Dushannavodya/depthShift
- Vue / 真离轴矩阵

离轴公式与本项目**完全一致**，可以互相验证：

```js
const left   = near * (screenLeft   - eye.x) / eye.z;
const right  = near * (screenRight  - eye.x) / eye.z;
const bottom = near * (screenBottom - eye.y) / eye.z;
const top    = near * (screenTop    - eye.y) / eye.z;
projectionMatrix.makePerspective(left, right, top, bottom, near, far);
```

差别是它没有传 `coordinateSystem`——因为它只跑 WebGL（NDC 深度 `-1..1`）。我们跑 WebGPU，必须传 `renderer.coordinateSystem`（深度 `0..1`），否则画面直接消失。它另外把 `near/far` 按房间深度动态放宽，避免远处被裁。

### 4. charliegerard/interactive-frame-tfjs

- https://github.com/charliegerard/interactive-frame-tfjs
- JavaScript / Three.js + TensorFlow.js / 107★

艺术装置式实现，代码量小、可读性高。不含 3DGS，纯 head-coupled perspective，适合当最小可运行骨架读。TensorFlow.js 版人脸关键点比 MediaPipe 慢，但依赖更少。

### 5. 其它小体量实现（想吃透数学时读）

| 仓库 | 说明 |
| --- | --- |
| https://github.com/SpaceT1934/virtual-window-tracker | MediaPipe + Three.js 虚拟窗，2026-09，结构清晰 |
| https://github.com/fenollp/uniVR | 纯 GLSL + 单摄像头，软件实现的 head-coupled perspective |
| https://github.com/jsimonson2013/aframe-head-tracker-controls-component | A-Frame 组件形态，接口设计可参考 |
| https://github.com/sangil1018/off-axis-projection | React Three Fiber + MediaPipe，含校准向导与麦克风电平驱动的抖动 |
| https://github.com/MindDock/off-axis-demo | 34★ 本项目的前身：GLTF 模型 + 赛博朋克网格 + 控制面板 |

## 二、3DGS 渲染器（当前用 three 内置，可替换）

three r186 的 `GaussianSplat` 有个硬限制：NodeMaterial 来自 `three/webgpu`，**只有 WebGPU 能渲染**。没有 WebGPU 的机器（老 Safari、部分集显）会直接看不到模型。下面这些能解决：

| 仓库 | 星 | 许可 | 关键点 |
| --- | --- | --- | --- |
| https://github.com/sparkjsdev/spark | 3784 | MIT | World Labs 出品。主打「98%+ WebGL2 覆盖」，支持 PLY/SPZ/SPLAT/KSPLAT/SOG，**多 splat 正确排序、多视角同时渲染** |
| https://github.com/mkkellogg/GaussianSplats3D | 2920 | MIT | three.js 老牌实现，社区案例最多 |
| https://github.com/WilliamLiu-1997/Gaussian-Splat-Lite | 215 | Apache-2.0 | WebGL2 + WebGPU 双后端，SPZ/SOG/RAD、流式 LOD，要求 three ≥ 0.186 |
| https://github.com/playcanvas/supersplat | 10434 | MIT | 编辑器而非运行时：裁剪、压缩、导出 SOG，用来预处理素材 |
| https://github.com/KeKsBoTer/web-splat | 307 | MIT | Rust + WGPU 实现，性能上限参考 |

**Spark 的「多视角同时渲染」和离轴投影是天然搭配**：离轴的本质就是让屏幕四角各自对应一条视线，一个场景同时被多个视锥采样。

## 三、追踪基础件

| 项目 | 说明 |
| --- | --- |
| https://github.com/google-ai-edge/mediapipe | FaceLandmarker 478 点，含虹膜 468/473（我们取眼睛位置就靠这两个点） |
| https://github.com/jeeliz/jeelizFaceFilter | 老牌 WebGL 人脸追踪，模型小、启动快，适合低端机 |
| https://github.com/brownhci/WebGazer | 纯眼动（不动头）方案。与本项目互补：头动给视差，眼动给交互焦点 |

## 四、三条可以立刻落地的改进

1. **vendor MediaPipe 运行时**（抄 Cam2Hologram）：把 `vision_bundle.mjs` 与 `wasm/` 放进 `vendor/`，CDN 只作兜底。这样「断网也能跑」才成立——现在只本地化了模型。
2. **3DGS 换 Spark**：`GaussianSplat` 在无 WebGPU 的机器上完全不可用，Spark 覆盖 WebGL2，并且原生支持多视角，与离轴的思路更契合。
3. **加物理校准向导**（抄 off-axis-sneaker）：让用户输入屏幕对角线、观看距离、眼睛高度，存 `localStorage`。目前屏幕尺寸靠对角线滑杆 + 视口比例推算，精度够用但不够「开箱即准」。
