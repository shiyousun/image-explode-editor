/**
 * 文档模型与撤销历史。
 *
 * 保真的核心约定：每个图层都记着自己的「原始位置」(ox/oy/ow/oh)，以及一个 dirty 标记。
 * 只要 dirty 为 false，这个图层的像素就还原封不动地留在底图里，渲染时既不打补丁也不
 * 重绘——所以没动过的地方与原图逐像素相同。只有被改过的图层才会：先用干净背景把它
 * 在底图上的原始位置补掉，再按新状态重新画一遍。
 */

export const CJK_FONTS = [
  { value: '"PingFang SC", "Heiti SC", sans-serif', label: '苹方 PingFang' },
  { value: '"Hiragino Sans GB", sans-serif', label: '冬青黑体' },
  { value: '"Songti SC", "SimSun", serif', label: '宋体 Songti' },
  { value: '"Kaiti SC", "STKaiti", serif', label: '楷体 Kaiti' },
  { value: '"Yuanti SC", sans-serif', label: '圆体 Yuanti' },
  { value: '"Heiti SC", "Microsoft YaHei", sans-serif', label: '黑体 Heiti' },
  { value: '"Noto Sans SC", "Source Han Sans SC", sans-serif', label: '思源黑体' },
  { value: '"STSong", "Songti SC", serif', label: '华文宋体' },
];

export const LATIN_FONTS = [
  { value: '"Helvetica Neue", Helvetica, Arial, sans-serif', label: 'Helvetica' },
  { value: 'Arial, sans-serif', label: 'Arial' },
  { value: '"SF Pro Display", -apple-system, sans-serif', label: 'SF Pro' },
  { value: 'Georgia, serif', label: 'Georgia' },
  { value: '"Times New Roman", Times, serif', label: 'Times' },
  { value: 'Impact, "Arial Black", sans-serif', label: 'Impact' },
  { value: '"Courier New", monospace', label: 'Courier' },
  { value: 'Verdana, sans-serif', label: 'Verdana' },
];

let uidCounter = 0;
export const uid = (prefix = 'l') => `${prefix}${Date.now().toString(36)}${(uidCounter++).toString(36)}`;

/** 需要参与历史记录的字段（图片对象等运行时引用不进历史） */
const SERIAL_KEYS = [
  'id', 'type', 'name', 'visible', 'locked', 'opacity', 'rotation',
  'x', 'y', 'w', 'h', 'ox', 'oy', 'ow', 'oh', 'dirty', 'fromSource',
  'text', 'fontFamily', 'fontSize', 'fontWeight', 'italic', 'color',
  'letterSpacing', 'lineHeight', 'align', 'baselineOffset', 'inkBox',
  'textMode', 'autoFit', 'fitSpacing', 'strokeColor', 'strokeWidth', 'fontMatch',
  'shape', 'fill', 'radius', 'shapeMode',
  'filters', 'sliceUrl', 'sliceCleanUrl', 'srcUrl', 'kind', 'isCJK', 'confidence', 'quality',
  'bgColor', 'paragraph',
];

export class Doc {
  constructor() {
    this.jobId = null;
    this.name = 'untitled';
    this.width = 0;
    this.height = 0;
    this.layers = [];
    this.images = new Map(); // url -> HTMLImageElement
    this.baseImg = null;
    this.cleanTextImg = null;
    this.cleanAllImg = null;
    this.stats = null;
  }

  get isReady() { return this.width > 0 && !!this.baseImg; }

  layerById(id) { return this.layers.find((l) => l.id === id) || null; }
  indexOf(id) { return this.layers.findIndex((l) => l.id === id); }

  /**
   * 取图层用于绘制的位图。clean=true 时优先用「已擦掉文字」的那份切片：
   * 压在横幅、按钮上的文字一旦被改写或挪走，就不能再用带原文字的切片重绘底板。
   */
  imageFor(layer, clean = false) {
    if (layer.srcUrl && this.images.has(layer.srcUrl)) return this.images.get(layer.srcUrl);
    if (clean && layer.sliceCleanUrl && this.images.has(layer.sliceCleanUrl)) {
      return this.images.get(layer.sliceCleanUrl);
    }
    if (layer.sliceUrl && this.images.has(layer.sliceUrl)) return this.images.get(layer.sliceUrl);
    return null;
  }

  serialize() {
    return {
      jobId: this.jobId,
      name: this.name,
      width: this.width,
      height: this.height,
      stats: this.stats,
      layers: this.layers.map((l) => pickSerial(l)),
    };
  }
}

export function pickSerial(layer) {
  const out = {};
  for (const key of SERIAL_KEYS) {
    if (layer[key] !== undefined) {
      const v = layer[key];
      out[key] = (v && typeof v === 'object') ? JSON.parse(JSON.stringify(v)) : v;
    }
  }
  return out;
}

/** 把后端 layout.json 的一层转成编辑器图层 */
export function layerFromLayout(raw, jobId) {
  const base = {
    id: raw.id,
    type: raw.type,
    name: raw.name || '图层',
    visible: raw.visible !== false,
    locked: !!raw.locked,
    opacity: raw.opacity ?? 1,
    rotation: raw.rotation || 0,
    x: raw.x, y: raw.y, w: raw.w, h: raw.h,
    ox: raw.x, oy: raw.y, ow: raw.w, oh: raw.h,
    dirty: false,
    fromSource: true,           // 该图层的像素本来就在底图里
    kind: raw.kind || raw.type,
    sliceUrl: raw.slice ? `/files/${jobId}/${raw.slice}` : null,
    sliceCleanUrl: raw.sliceClean ? `/files/${jobId}/${raw.sliceClean}` : null,
    srcUrl: null,
  };

  if (raw.type === 'text') {
    Object.assign(base, {
      text: raw.text ?? '',
      fontFamily: raw.fontFamily || null,   // null = 按 isCJK 自动选默认字体
      fontSize: raw.fontSize || 16,
      fontWeight: raw.fontWeight || 400,
      italic: Math.abs(raw.italicDeg || 0) > 6,
      color: raw.color || '#000000',
      bgColor: raw.bgColor || '#ffffff',
      letterSpacing: raw.letterSpacing || 0,
      lineHeight: raw.lineHeight || 1.25,
      align: raw.align || 'left',
      baselineOffset: raw.baselineOffset || (raw.fontSize || 16),
      inkBox: raw.inkBox || [raw.x, raw.y, raw.w, raw.h],
      textMode: 'pixel',        // 先用原始像素，改动后自动切矢量
      autoFit: true,            // 自动微调字距贴合原始宽度
      fitSpacing: 0,
      strokeColor: '#ffffff',
      strokeWidth: 0,
      isCJK: !!raw.isCJK,
      confidence: raw.confidence ?? 1,
      quality: raw.quality ?? 1,
      paragraph: raw.paragraph ?? 0,
    });
  } else if (raw.type === 'shape') {
    Object.assign(base, {
      shape: raw.shape || 'rect',
      fill: raw.fill || '#888888',
      radius: raw.radius || 0,
      strokeColor: '#000000',
      strokeWidth: 0,
      shapeMode: 'pixel',
    });
  } else {
    Object.assign(base, {
      filters: { brightness: 100, contrast: 100, saturate: 100, blur: 0, grayscale: 0 },
    });
  }
  return base;
}

export function makeTextLayer({ x, y, fontSize = 32, isCJK = true }) {
  return {
    id: uid('t'), type: 'text', name: '新文字',
    visible: true, locked: false, opacity: 1, rotation: 0,
    x, y, w: fontSize * 6, h: fontSize * 1.5,
    ox: x, oy: y, ow: fontSize * 6, oh: fontSize * 1.5,
    dirty: true, fromSource: false, kind: 'manual',
    sliceUrl: null, srcUrl: null,
    text: '双击编辑文字', fontFamily: null,
    fontSize, fontWeight: 400, italic: false,
    color: '#111111', bgColor: '#ffffff',
    letterSpacing: 0, lineHeight: 1.3, align: 'left',
    baselineOffset: fontSize, inkBox: [x, y, fontSize * 6, fontSize],
    textMode: 'vector', autoFit: false, fitSpacing: 0,
    strokeColor: '#ffffff', strokeWidth: 0,
    isCJK, confidence: 1, quality: 1, paragraph: -1,
  };
}

export function makeShapeLayer({ x, y, w, h, shape = 'rect', fill = '#5b8cff' }) {
  return {
    id: uid('s'), type: 'shape', name: shape === 'ellipse' ? '椭圆' : '矩形',
    visible: true, locked: false, opacity: 1, rotation: 0,
    x, y, w, h, ox: x, oy: y, ow: w, oh: h,
    dirty: true, fromSource: false, kind: shape,
    sliceUrl: null, srcUrl: null,
    shape, fill, radius: shape === 'rounded-rect' ? 12 : 0,
    strokeColor: '#000000', strokeWidth: 0, shapeMode: 'vector',
  };
}

/* --------------------------------------------------------------------- */
/* 撤销 / 重做                                                            */
/* --------------------------------------------------------------------- */

export class History {
  constructor(limit = 80) {
    this.limit = limit;
    this.past = [];
    this.future = [];
    this.listeners = [];
    this.doc = null;
  }

  attach(doc) { this.doc = doc; }

  onChange(fn) { this.listeners.push(fn); }
  notify() { this.listeners.forEach((fn) => fn(this)); }

  get canUndo() { return this.past.length > 1; }
  get canRedo() { return this.future.length > 0; }

  /** 记录当前状态。label 仅用于调试与提示。 */
  push(doc = this.doc, label = '') {
    if (!doc) return;
    const snap = { label, layers: doc.layers.map(pickSerial) };
    const last = this.past[this.past.length - 1];
    if (last && JSON.stringify(last.layers) === JSON.stringify(snap.layers)) return;
    this.past.push(snap);
    if (this.past.length > this.limit) this.past.shift();
    this.future.length = 0;
    this.notify();
  }

  undo(doc = this.doc) {
    if (!doc || !this.canUndo) return false;
    this.future.push(this.past.pop());
    this.restore(doc, this.past[this.past.length - 1]);
    this.notify();
    return true;
  }

  redo(doc = this.doc) {
    if (!doc || !this.canRedo) return false;
    const snap = this.future.pop();
    this.past.push(snap);
    this.restore(doc, snap);
    this.notify();
    return true;
  }

  restore(doc = this.doc, snap) {
    if (!doc || !snap) return;
    const byId = new Map(doc.layers.map((l) => [l.id, l]));
    doc.layers = snap.layers.map((data) => {
      const existing = byId.get(data.id);
      // 复用已有对象上的运行时字段（图片引用等），只覆盖被记录的属性
      return existing ? Object.assign(existing, data) : { ...data };
    });
  }

  reset() {
    this.past.length = 0;
    this.future.length = 0;
    this.notify();
  }
}

/* --------------------------------------------------------------------- */
/* 几何工具                                                              */
/* --------------------------------------------------------------------- */

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export function layerBox(l) { return { x: l.x, y: l.y, w: l.w, h: l.h }; }
export function originBox(l) { return { x: l.ox, y: l.oy, w: l.ow, h: l.oh }; }

export function rectsIntersect(a, b, pad = 0) {
  return !(a.x + a.w + pad <= b.x || b.x + b.w + pad <= a.x
        || a.y + a.h + pad <= b.y || b.y + b.h + pad <= a.y);
}

export function unionRect(rects) {
  if (!rects.length) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const r of rects) {
    x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y);
    x1 = Math.max(x1, r.x + r.w); y1 = Math.max(y1, r.y + r.h);
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** 把点从画布坐标变换到图层的本地坐标（消掉旋转） */
export function toLocal(l, px, py) {
  const cx = l.x + l.w / 2;
  const cy = l.y + l.h / 2;
  const rad = (-(l.rotation || 0) * Math.PI) / 180;
  const dx = px - cx;
  const dy = py - cy;
  return {
    x: cx + dx * Math.cos(rad) - dy * Math.sin(rad),
    y: cy + dx * Math.sin(rad) + dy * Math.cos(rad),
  };
}

export function hitLayer(l, px, py, pad = 0) {
  const p = toLocal(l, px, py);
  return p.x >= l.x - pad && p.x <= l.x + l.w + pad
      && p.y >= l.y - pad && p.y <= l.y + l.h + pad;
}

/* --------------------------------------------------------------------- */
/* 墨迹命中：点在包围盒里不算命中，要点在这个元素真正画了东西的地方          */
/* --------------------------------------------------------------------- */

const alphaCache = new WeakMap();

/**
 * 取图层切片的 alpha 位图（缓存）。抠出来的元素包围盒是矩形，但内容常常只占一小块：
 * 一整片图表区的墨迹只有 2%，全靠包围盒判命中的话，点画面上任何空白处都会选中它，
 * 底下真正想选的小图标反而永远点不着——这就是「颗粒度太大」体感的另一半来源。
 */
function alphaMapOf(doc, l) {
  const img = doc.images.get(l.sliceUrl);
  if (!img || !img.naturalWidth) return null;
  let m = alphaCache.get(l);
  if (m && m.src === l.sliceUrl) return m;

  // 别缩太狠：320px 的缩略图上，一条网格线的 alpha 会摊到周围三四个格子里，
  // 于是点在网格之间的空处也会被判成命中，实测 981×532 的图表层就是这样把点击吞掉的。
  const maxSide = 1024;
  const k = Math.min(1, maxSide / Math.max(img.naturalWidth, img.naturalHeight));
  const w = Math.max(1, Math.round(img.naturalWidth * k));
  const h = Math.max(1, Math.round(img.naturalHeight * k));
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, w, h);
  const px = ctx.getImageData(0, 0, w, h).data;
  const a = new Uint8Array(w * h);
  for (let i = 0, j = 3; i < a.length; i++, j += 4) a[i] = px[j];
  m = { src: l.sliceUrl, w, h, a };
  alphaCache.set(l, m);
  return m;
}

/**
 * 元素是否在该点画了东西。允许 slack 个文档像素的容差，好让一像素宽的曲线也点得中；
 * 文字层按墨迹框算（切片带了大片留白，标题的包围盒能左探一百多像素到黑边上），
 * 矢量图元没有稀疏问题，直接按包围盒算。
 */
export function inkHit(doc, l, px, py, tol = 1, slack = 3) {
  if (!hitLayer(l, px, py, tol)) return false;
  if (l.type === 'text') {
    if (!l.inkBox) return true;
    const sx = l.ow > 0 ? l.w / l.ow : 1;
    const sy = l.oh > 0 ? l.h / l.oh : 1;
    const p = toLocal(l, px, py);
    const bx = l.x + (l.inkBox[0] - l.ox) * sx;
    const by = l.y + (l.inkBox[1] - l.oy) * sy;
    return p.x >= bx - slack && p.x <= bx + l.inkBox[2] * sx + slack
        && p.y >= by - slack && p.y <= by + l.inkBox[3] * sy + slack;
  }
  if (l.type !== 'image' || l.dirty) return true;
  const m = alphaMapOf(doc, l);
  if (!m) return true;
  const p = toLocal(l, px, py);
  const kx = m.w / Math.max(1e-6, l.w);
  const ky = m.h / Math.max(1e-6, l.h);
  const x0 = Math.round((p.x - l.x) * kx);
  const y0 = Math.round((p.y - l.y) * ky);
  const rx = Math.max(1, Math.round(slack * kx));
  const ry = Math.max(1, Math.round(slack * ky));
  for (let dy = -ry; dy <= ry; dy += 1) {
    for (let dx = -rx; dx <= rx; dx += 1) {
      const x = x0 + dx, y = y0 + dy;
      if (x < 0 || y < 0 || x >= m.w || y >= m.h) continue;
      if (m.a[y * m.w + x] > 24) return true;
    }
  }
  return false;
}

/** 标记图层为已编辑：之后渲染会补掉它的原位并重新绘制 */
export function markDirty(l) {
  l.dirty = true;
  return l;
}
