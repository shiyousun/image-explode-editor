/**
 * 字体识别：让改字后的字体和原图一致。
 *
 * 思路是「渲染比对」而不是从像素里凭空猜字体名：把每个候选系统字体用同一段文字渲染出来，
 * 和原图切片里的笔画做形状比对，挑最像的。这么做有个决定性的好处——比对用的渲染器就是
 * 最终真正绘制文字的那个 canvas，所以匹配出来的结果必然能被复现出来；后端用 PIL 猜的话，
 * 一是 PingFang 这类字体在新版 macOS 藏在带哈希的资产库里加载不到，二是 FreeType 和
 * 浏览器 CoreText 的字形渲染本来就有差异，猜中了也画不出来。
 *
 * 三段式流程，兼顾准确与速度：
 *   1. 用墨迹面积最大的若干层（字大、像素多、最好认）逐个候选家族打分；
 *   2. 按字符体系（中日韩 / 拉丁）分组投票，得出整篇的主字体——真实文档极少超过两种字体，
 *      投票能把小字上的噪声抹掉；
 *   3. 每层再在「主字体 + 票数第二的家族」里定最终家族，并单独定字重——字重是逐层的，
 *      标题粗正文细，不能全篇统一。
 *
 * 字重从此不再靠笔画粗细的像素测量（抗锯齿会把小字系统性地测粗，一张图里最粗的大标题
 * 反而可能被判成最细），而是直接比对「同一字重渲染出来像不像」，测的就是最终效果。
 */

/* 候选字体池。value 是 CSS font-family，probe 用于探测该字体在本机是否真的存在。 */
export const CJK_CANDIDATES = [
  { value: '"PingFang SC"', label: '苹方 PingFang' },
  { value: '"Hiragino Sans GB"', label: '冬青黑体' },
  { value: '"Heiti SC"', label: '黑体 Heiti' },
  { value: '"Songti SC"', label: '宋体 Songti' },
  { value: '"STSong"', label: '华文宋体' },
  { value: '"Kaiti SC"', label: '楷体 Kaiti' },
  { value: '"Yuanti SC"', label: '圆体 Yuanti' },
  { value: '"Noto Sans SC"', label: '思源黑体' },
  { value: '"Source Han Serif SC"', label: '思源宋体' },
  { value: '"Microsoft YaHei"', label: '微软雅黑' },
  { value: '"Hannotate SC"', label: '手札体' },
];

export const LATIN_CANDIDATES = [
  { value: '"Helvetica Neue"', label: 'Helvetica Neue' },
  { value: 'Helvetica', label: 'Helvetica' },
  { value: 'Arial', label: 'Arial' },
  { value: '"SF Pro Display"', label: 'SF Pro' },
  { value: '-apple-system', label: '系统字体' },
  { value: '"Avenir Next"', label: 'Avenir Next' },
  { value: 'Futura', label: 'Futura' },
  { value: 'Optima', label: 'Optima' },
  { value: 'Verdana', label: 'Verdana' },
  { value: 'Tahoma', label: 'Tahoma' },
  { value: '"Trebuchet MS"', label: 'Trebuchet MS' },
  { value: 'Georgia', label: 'Georgia' },
  { value: '"Times New Roman"', label: 'Times New Roman' },
  { value: 'Palatino', label: 'Palatino' },
  { value: 'Baskerville', label: 'Baskerville' },
  { value: 'Didot', label: 'Didot' },
  { value: 'Impact', label: 'Impact' },
  { value: '"Arial Narrow"', label: 'Arial Narrow' },
  { value: '"Arial Black"', label: 'Arial Black' },
  { value: '"Courier New"', label: 'Courier New' },
  { value: 'Menlo', label: 'Menlo' },
  // 中文排版里西文常跟着中文字体一起设，所以中文字体也得进拉丁候选池
  { value: '"PingFang SC"', label: '苹方（西文）' },
  { value: '"Hiragino Sans GB"', label: '冬青黑体（西文）' },
  { value: '"Songti SC"', label: '宋体（西文）' },
];

/**
 * 字重候选按「离常规多远」排序，而不是从小到大。
 * 因为多数字体并没有九档字面：宋体只有 Light 和 Regular，CSS 的 200 和 300 会渲染成
 * 同一个字面、拿到完全相同的分数。从小到大遍历时并列取到的是 200，于是常规字被标成
 * 超细体——这是纯粹的标注伪影。按这个顺序遍历，并列时留下的就是最接近常规的那一档。
 */
const WEIGHTS = [400, 500, 300, 600, 700, 800, 200, 900];
const PROBE_CJK = '永国字体的书';
const PROBE_LATIN = 'mmmiiiwwwOo08';

/* --------------------------------------------------------------------- */
/* 字体可用性探测                                                        */
/* --------------------------------------------------------------------- */

const scratch = document.createElement('canvas');
const sctx = scratch.getContext('2d', { willReadFrequently: true });
const availCache = new Map();

/**
 * 按渲染指纹判断候选字体是否可用。
 *
 * 不能比字宽：中日韩字符在所有中文字体里都是全角方块，字宽恒等于字号，
 * 十一个候选字体量出来的宽度会一模一样（实测 6 字 × 72px = 432.0，无一例外）。
 * 所以改成真渲染一遍取像素指纹。
 */
function rasterSignature(family, probe, weight = 400) {
  const key = `${weight}|${family}|${probe}`;
  if (availCache.has(key)) return availCache.get(key);
  const size = 40;
  const spec = `${weight} ${size}px ${family}, monospace`;
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.font = spec;
  c.width = Math.ceil(ctx.measureText(probe).width) + 8;
  c.height = Math.ceil(size * 1.7);
  const ctx2 = c.getContext('2d', { willReadFrequently: true });
  ctx2.font = spec;
  ctx2.textBaseline = 'alphabetic';
  ctx2.fillStyle = '#000';
  ctx2.fillText(probe, 4, Math.round(size * 1.2));
  const px = ctx2.getImageData(0, 0, c.width, c.height).data;
  let hash = 2166136261;
  for (let i = 3; i < px.length; i += 4) {
    hash ^= px[i] >> 6;                  // 量化到 4 级，容忍亚像素抖动
    hash = (hash * 16777619) >>> 0;
  }
  const sig = `${c.width}x${c.height}:${hash}`;
  availCache.set(key, sig);
  return sig;
}

/**
 * 渲染一致的候选只保留偏好顺序里的第一个：装不上的字体会退回系统默认，
 * 于是自动并入默认那一组被丢掉；真正装了且长得不一样的字体则各自留下。
 */
export function availableCandidates(isCJK) {
  const probe = isCJK ? PROBE_CJK : PROBE_LATIN;
  const pool = isCJK ? CJK_CANDIDATES : LATIN_CANDIDATES;
  const seen = new Set();
  const out = [];
  for (const c of pool) {
    const sig = rasterSignature(c.value, probe);
    if (seen.has(sig)) continue;
    seen.add(sig);
    out.push(c);
  }
  return out;
}

/* --------------------------------------------------------------------- */
/* 掩码提取与归一化                                                      */
/* --------------------------------------------------------------------- */

/** 单通道软掩码：0~1 浮点，w×h */
function makeMask(w, h) {
  return { w, h, d: new Float32Array(w * h) };
}

/** 取出原图切片的 alpha 通道（就是笔画墨迹），裁到 inkBox */
function refMaskFromSlice(doc, layer) {
  const img = doc.images.get(layer.sliceUrl);
  if (!img || !img.naturalWidth) return null;
  const ink = layer.inkBox;
  if (!ink || ink[2] < 3 || ink[3] < 3) return null;

  // 切片图像对应 sliceRect（ox,oy,ow,oh），可能因分析降采样而与文档坐标有比例差
  const kx = img.naturalWidth / Math.max(1, layer.ow);
  const ky = img.naturalHeight / Math.max(1, layer.oh);
  const sx = Math.max(0, Math.round((ink[0] - layer.ox) * kx));
  const sy = Math.max(0, Math.round((ink[1] - layer.oy) * ky));
  const sw = Math.min(img.naturalWidth - sx, Math.round(ink[2] * kx));
  const sh = Math.min(img.naturalHeight - sy, Math.round(ink[3] * ky));
  if (sw < 3 || sh < 3) return null;

  scratch.width = sw; scratch.height = sh;
  sctx.clearRect(0, 0, sw, sh);
  sctx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
  const px = sctx.getImageData(0, 0, sw, sh).data;
  const m = makeMask(sw, sh);
  for (let i = 0, j = 3; i < m.d.length; i++, j += 4) m.d[i] = px[j] / 255;
  return tighten(m);
}

/** 裁掉四周空白，只留墨迹 */
function tighten(m, thresh = 0.18) {
  let x0 = m.w, y0 = m.h, x1 = -1, y1 = -1;
  for (let y = 0; y < m.h; y++) {
    for (let x = 0; x < m.w; x++) {
      if (m.d[y * m.w + x] >= thresh) {
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    }
  }
  if (x1 < x0 || y1 < y0) return null;
  const w = x1 - x0 + 1, h = y1 - y0 + 1;
  const out = makeMask(w, h);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) out.d[y * w + x] = m.d[(y + y0) * m.w + (x + x0)];
  }
  return out;
}

/** 双线性缩放到指定尺寸 */
function resample(m, w, h) {
  const out = makeMask(w, h);
  const fx = m.w / w, fy = m.h / h;
  for (let y = 0; y < h; y++) {
    const sy = Math.min(m.h - 1, (y + 0.5) * fy - 0.5);
    const y0 = Math.max(0, Math.floor(sy)), y1 = Math.min(m.h - 1, y0 + 1);
    const wy = sy - y0;
    for (let x = 0; x < w; x++) {
      const sx = Math.min(m.w - 1, (x + 0.5) * fx - 0.5);
      const x0 = Math.max(0, Math.floor(sx)), x1 = Math.min(m.w - 1, x0 + 1);
      const wx = sx - x0;
      const a = m.d[y0 * m.w + x0], b = m.d[y0 * m.w + x1];
      const c = m.d[y1 * m.w + x0], e = m.d[y1 * m.w + x1];
      out.d[y * w + x] = (a * (1 - wx) + b * wx) * (1 - wy) + (c * (1 - wx) + e * wx) * wy;
    }
  }
  return out;
}

/**
 * 墨迹占比。按 0.5 二值化而不是把软 alpha 直接求和：原图里 30px 的字边缘很软
 * （半数墨迹像素的 alpha 还不到 0.75），而候选是重新渲染的、边缘更锐利，
 * 直接比软 alpha 的和会把「边缘软」误读成「笔画细」，于是常规字被判成细体。
 * 二值化后量到的是笔画的几何面积，与抗锯齿的软硬无关。
 */
function coverage(m) {
  let s = 0;
  for (let i = 0; i < m.d.length; i++) if (m.d[i] >= 0.5) s++;
  return s / m.d.length;
}

/** 软 IoU：抗锯齿的灰边用 min/max 处理比二值化更稳 */
function softIoU(a, b) {
  let inter = 0, union = 0;
  for (let i = 0; i < a.d.length; i++) {
    const x = a.d[i], y = b.d[i];
    inter += x < y ? x : y;
    union += x > y ? x : y;
  }
  return union > 0 ? inter / union : 0;
}

/* --------------------------------------------------------------------- */
/* 候选字体渲染                                                          */
/* --------------------------------------------------------------------- */

function setSpacing(ctx, px) {
  if ('letterSpacing' in ctx) ctx.letterSpacing = `${px}px`;
}

function measureWidth(ctx, text, spacing) {
  if ('letterSpacing' in ctx) return ctx.measureText(text).width;
  let w = 0;
  for (const ch of [...text]) w += ctx.measureText(ch).width + spacing;
  return w - spacing;
}

function drawText(ctx, text, x, y, spacing) {
  if ('letterSpacing' in ctx) { ctx.fillText(text, x, y); return; }
  let cx = x;
  for (const ch of [...text]) {
    ctx.fillText(ch, cx, y);
    cx += ctx.measureText(ch).width + spacing;
  }
}

/** 用指定字体渲染文字并取出墨迹掩码 */
function renderMask(text, family, weight, italic, size, spacing) {
  const pad = Math.ceil(size * 0.6);
  sctx.font = `${italic ? 'italic ' : ''}${weight} ${size}px ${family}`;
  setSpacing(sctx, spacing);
  const w = Math.ceil(measureWidth(sctx, text, spacing)) + pad * 2;
  const h = Math.ceil(size * 2.0) + pad;
  if (w < 4 || h < 4 || w > 4000) return null;

  scratch.width = w; scratch.height = h;
  const ctx = scratch.getContext('2d', { willReadFrequently: true });
  ctx.clearRect(0, 0, w, h);
  ctx.font = `${italic ? 'italic ' : ''}${weight} ${size}px ${family}`;
  setSpacing(ctx, spacing);
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = '#000';
  drawText(ctx, text, pad, Math.round(size * 1.25), spacing);

  const px = ctx.getImageData(0, 0, w, h).data;
  const m = makeMask(w, h);
  for (let i = 0, j = 3; i < m.d.length; i++, j += 4) m.d[i] = px[j] / 255;
  return tighten(m);
}

/* --------------------------------------------------------------------- */
/* 打分                                                                  */
/* --------------------------------------------------------------------- */

const NORM_MIN = 28;
const NORM_MAX = 56;

function prepareRef(mask) {
  const h = Math.max(NORM_MIN, Math.min(NORM_MAX, mask.h));
  const w = Math.max(4, Math.round(mask.w * (h / mask.h)));
  return { norm: resample(mask, w, h), aspect: mask.w / mask.h, h, w,
           cover: coverage(mask) };
}

/**
 * 比对一个候选：先按参考的宽高比调一次字距，让候选有机会在宽度上对齐，
 * 这样后面的 IoU 就纯粹在比字形和粗细，不会被「宽度差太多」这一项吃掉。
 */
function scoreCandidate(ref, text, family, weight, italic, spacing0) {
  const size = Math.round(ref.h * 1.35);
  let m = renderMask(text, family, weight, italic, size, spacing0);
  if (!m) return null;

  const chars = [...text].length;
  if (chars > 1) {
    const want = ref.aspect * m.h;          // 想要的墨迹宽度
    const delta = (want - m.w) / (chars - 1);
    if (Math.abs(delta) > size * 0.02 && Math.abs(delta) < size * 0.5) {
      const m2 = renderMask(text, family, weight, italic, size, spacing0 + delta);
      if (m2) m = m2;
    }
  }

  const cand = { norm: resample(m, ref.w, ref.h), aspect: m.w / m.h,
                 cover: coverage(m) };
  const iou = softIoU(ref.norm, cand.norm);
  const aspSim = Math.min(ref.aspect, cand.aspect) / Math.max(ref.aspect, cand.aspect);
  const covSim = 1 - Math.min(1, Math.abs(ref.cover - cand.cover)
                                 / Math.max(0.02, ref.cover));
  return { score: 0.62 * iou + 0.20 * aspSim + 0.18 * covSim, iou, aspSim, covSim };
}

/** 某一层在某个家族下的最佳字重 */
function bestWeightFor(ref, layer, family, weights) {
  let best = null;
  for (const wt of weights) {
    const r = scoreCandidate(ref, layer.text, family, wt,
                             !!layer.italic, layer.letterSpacing || 0);
    // 要明显更好才换档：分差在千分之几的量级时纯属噪声，留在遍历顺序更靠前、
    // 也就是更接近常规的那一档，比来回跳档更接近真实
    if (r && (!best || r.score > best.score + 0.004)) best = { ...r, weight: wt };
  }
  return best;
}

/* --------------------------------------------------------------------- */
/* 对外：整篇字体标定                                                    */
/* --------------------------------------------------------------------- */

const COARSE_WEIGHTS = [300, 400, 700];
/** 量化某一层用指定字体渲染时与原图笔画的吻合度，用于对比取舍 */
export function scoreFor(doc, layer, family, weight) {
  const mask = refMaskFromSlice(doc, layer);
  if (!mask) return null;
  return scoreCandidate(prepareRef(mask), layer.text, family, weight,
                        !!layer.italic, layer.letterSpacing || 0);
}

/** 调参用：把某一层对所有候选字体的打分排名和参考掩码统计倒出来 */
export function diagnose(doc, layer) {
  const mask = refMaskFromSlice(doc, layer);
  if (!mask) return { error: '取不到参考掩码' };
  const vals = [...mask.d].filter((v) => v > 0.02).sort((a, b) => a - b);
  const ref = prepareRef(mask);
  const ranked = [];
  for (const cand of availableCandidates(!!layer.isCJK)) {
    const best = bestWeightFor(ref, layer, cand.value, WEIGHTS);
    if (best) ranked.push({ font: cand.label, weight: best.weight,
      score: +best.score.toFixed(3), iou: +best.iou.toFixed(3),
      asp: +best.aspSim.toFixed(3), cov: +best.covSim.toFixed(3) });
  }
  ranked.sort((a, b) => b.score - a.score);
  return {
    text: String(layer.text).slice(0, 16),
    refMask: { w: mask.w, h: mask.h, cover: +coverage(mask).toFixed(3),
               max: +(vals[vals.length - 1] || 0).toFixed(3),
               p98: +(vals[Math.floor(vals.length * 0.98)] || 0).toFixed(3),
               p50: +(vals[Math.floor(vals.length * 0.5)] || 0).toFixed(3) },
    ranked: ranked.slice(0, 6),
  };
}

function usableTextLayers(doc) {
  return doc.layers.filter((l) => l.type === 'text' && l.sliceUrl
    && String(l.text || '').trim().length > 0 && l.inkBox);
}

/**
 * 给全篇文字图层标定字体与字重。不会把任何图层标脏——未编辑的图层依旧从原图像素还原，
 * 标定结果只在用户真的改字时才生效，所以标定本身不影响保真。
 */
export async function calibrateFonts(doc, onProgress) {
  const layers = usableTextLayers(doc);
  if (!layers.length) return { matched: 0, families: {} };

  const refs = new Map();
  for (const l of layers) {
    const mask = refMaskFromSlice(doc, l);
    if (mask && mask.w >= 4 && mask.h >= 6) refs.set(l, prepareRef(mask));
  }

  const groups = { cjk: [], latin: [] };
  for (const l of layers) {
    if (!refs.has(l)) continue;
    groups[l.isCJK ? 'cjk' : 'latin'].push(l);
  }

  const chosen = new Map();
  const summary = {};
  let done = 0;
  const totalSteps = layers.length * 2 || 1;

  for (const [key, list] of Object.entries(groups)) {
    if (!list.length) continue;
    const pool = availableCandidates(key === 'cjk');
    if (!pool.length) continue;

    // 一、每层都过一遍全部候选（粗字重档），得到逐层排名，同时按墨迹面积和领先幅度投票。
    //     不能只让票数前二进入决赛：一篇文档完全可能同时用黑体和宋体，宋体在总票数上排第三
    //     被挤掉的话，那几层就永远匹配不对——而单独测它时宋体是以 0.68 对 0.48 的大幅优势胜出的。
    const votes = new Map();
    const ranking = new Map();
    for (const l of list) {
      const ref = refs.get(l);
      const ranked = [];
      for (const cand of pool) {
        const best = bestWeightFor(ref, l, cand.value, COARSE_WEIGHTS);
        if (best) ranked.push({ fam: cand.value, ...best });
      }
      if (ranked.length) {
        ranked.sort((a, b) => b.score - a.score);
        ranking.set(l, ranked);
        const lead = Math.max(0.02, ranked[0].score - (ranked[1]?.score ?? 0));
        votes.set(ranked[0].fam, (votes.get(ranked[0].fam) || 0)
          + Math.sqrt(l.inkBox[2] * l.inkBox[3]) * lead);
      }
      if (onProgress) onProgress(++done / totalSteps);
      await Promise.resolve();
    }
    if (!votes.size) continue;

    // 票数接近时曾试过「改选字面更全的家族」（冬青黑体只有常规和粗两个字面，苹方有四个，
    // 理论上更容易命中原图的 Medium）。实测反而更差：中西文都被拽到苹方后，年份标签的
    // 字形吻合度从 0.70 掉到 0.43，整篇均值 0.622 → 0.567。字面多寡终究不是像素证据，
    // 逐层打分才是，所以这里仍然认票数。
    const ordered = [...votes.entries()].sort((a, b) => b[1] - a[1]);
    const primary = ordered[0][0];
    summary[key] = { primary, votes: ordered };

    // 二、定稿：用本层自己的最优家族，但要偏离全篇主字体，得赢出足够的分差。
    //     门槛随字数收紧：四位数的年份标签在各候选间的分差只有 0.004 这个量级，纯属噪声，
    //     照着噪声走会把原图里一模一样的几个标签判成六种不同字体；长句子的分差才是真信号
    //     （宋体正文能以 0.68 对 0.48 压过黑体），那种幅度理应允许它自主判断。
    for (const l of list) {
      const ranked = ranking.get(l);
      if (!ranked) continue;
      const own = ranked[0];
      const prim = ranked.find((r) => r.fam === primary);
      const tol = 0.03 + 0.10 / Math.sqrt(Math.max(1, [...String(l.text).trim()].length));
      const fam = (prim && own.score - prim.score <= tol) ? primary : own.fam;
      const best = bestWeightFor(refs.get(l), l, fam, WEIGHTS);
      if (best) chosen.set(l, { ...best, fam });
      if (onProgress) onProgress(++done / totalSteps);
      await Promise.resolve();
    }
  }

  const labelOf = (value) => [...CJK_CANDIDATES, ...LATIN_CANDIDATES]
    .find((c) => c.value === value)?.label || value;

  for (const [l, best] of chosen.entries()) {
    l.fontFamily = best.fam;
    l.fontWeight = best.weight;
    l.fontMatch = { label: labelOf(best.fam), score: Math.round(best.score * 100),
                    iou: Math.round(best.iou * 100) };
  }
  return { matched: chosen.size, families: summary, labelOf };
}
