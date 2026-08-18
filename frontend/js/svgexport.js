/**
 * 导出 SVG 矢量图。
 *
 * 一张炸开后的图里，改过的只是少数几层，其余仍是原图像素——那部分没有矢量原稿可言，
 * 硬描边缘只会把照片描成一团噪点。所以这里走「混合」：没动过的像素整体嵌成一张底图，
 * 改写过的文字导成真 <text>、改过的形状导成真 <rect>/<ellipse>，在 Illustrator、
 * Figma、Inkscape 里点开就能继续改字改色，放大也不糊。
 *
 * 底图里必须挖掉旧文字，否则新文字会盖在旧文字上叠成双影：把矢量层的不透明度临时设成 0
 * 再渲染一遍即可——渲染管线照样会给它们打「擦除补丁」，只是不画新内容，于是底图正好是
 * 「擦干净、等着放矢量文字」的样子。
 */

import { renderToCanvas, drawLayer, textPlan } from './render.js';
import { layerBox, rectsIntersect } from './state.js';

const XML_ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => XML_ESC[c]);
const num = (n) => String(Math.round((Number(n) || 0) * 100) / 100);

export function isVectorText(l) {
  return l.type === 'text' && l.visible && l.textMode !== 'pixel'
    && String(l.text ?? '').trim() !== '';
}

export function isVectorShape(l) {
  return l.type === 'shape' && l.visible && l.shapeMode !== 'pixel';
}

/** 能导成真矢量的图层（按图层顺序） */
export function vectorLayers(doc) {
  return doc.layers.filter((l) => isVectorText(l) || isVectorShape(l));
}

/**
 * @param {object} doc
 * @param {{rasterScale?: number}} opts  底图倍率；矢量部分与倍率无关，永远无级缩放
 * @returns {{svg: string, vectorCount: number}}
 */
export function buildSvg(doc, { rasterScale = 1 } = {}) {
  const vecs = vectorLayers(doc);
  const body = [imageTag(rasterBase(doc, vecs, rasterScale), 0, 0, doc.width, doc.height,
                         'background', '底图（原始像素）')];

  const ctx = scratchCtx();
  for (const l of vecs) {
    body.push(l.type === 'text' ? textTag(ctx, l) : shapeTag(l));
  }
  for (const r of coveringRasters(doc, vecs)) {
    const box = layerBox(r);
    body.push(imageTag(rasterLayer(doc, r, rasterScale), box.x, box.y, box.w, box.h,
                       `raster-${r.id}`, r.name || '位图图层'));
  }

  const svg = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"`
      + ` width="${doc.width}" height="${doc.height}"`
      + ` viewBox="0 0 ${doc.width} ${doc.height}">`,
    `<title>${esc(doc.name || 'image')}</title>`,
    ...body,
    '</svg>',
    '',
  ].join('\n');

  return { svg, vectorCount: vecs.length };
}

/* --------------------------------------------------------------------- */
/* 位图部分                                                               */
/* --------------------------------------------------------------------- */

/** 整张图去掉矢量层之后的样子（旧文字已被擦除补丁抹掉） */
function rasterBase(doc, vecs, scale) {
  const saved = vecs.map((l) => l.opacity ?? 1);
  vecs.forEach((l) => { l.opacity = 0; });
  try {
    return renderToCanvas(doc, scale).toDataURL('image/png');
  } finally {
    vecs.forEach((l, i) => { l.opacity = saved[i]; });
  }
}

/** 单独一层位图，裁到它自己的包围盒 */
function rasterLayer(doc, layer, scale) {
  const box = layerBox(layer);
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(box.w * scale));
  canvas.height = Math.max(1, Math.round(box.h * scale));
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingQuality = 'high';
  ctx.setTransform(scale, 0, 0, scale, -box.x * scale, -box.y * scale);
  drawLayer(ctx, doc, layer);
  return canvas.toDataURL('image/png');
}

/**
 * 被用户调到矢量层上方、又和它重叠的位图层。
 *
 * 这些层已经烘进底图了，但底图整张躺在最下面，矢量文字会反压在它们上面。把它们在文字
 * 之后再画一遍就恢复了正确的前后关系——画两遍像素完全相同，看不出接缝。
 */
function coveringRasters(doc, vecs) {
  const vecSet = new Set(vecs);
  const idx = new Map(doc.layers.map((l, i) => [l.id, i]));
  const out = [];
  for (const r of doc.layers) {
    if (!r.visible || vecSet.has(r)) continue;
    const hit = vecs.some((v) => idx.get(r.id) > idx.get(v.id)
      && rectsIntersect(layerBox(r), layerBox(v)));
    if (hit) out.push(r);
  }
  return out;
}

function imageTag(href, x, y, w, h, id, label) {
  // href 是 SVG2 写法，xlink:href 是 Illustrator 等老解析器认的那个，两个都写上
  return `<image id="${esc(id)}" data-name="${esc(label)}" x="${num(x)}" y="${num(y)}"`
    + ` width="${num(w)}" height="${num(h)}" preserveAspectRatio="none"`
    + ` xlink:href="${href}" href="${href}"/>`;
}

/* --------------------------------------------------------------------- */
/* 矢量部分                                                               */
/* --------------------------------------------------------------------- */

let scratch = null;

function scratchCtx() {
  if (!scratch) {
    const c = document.createElement('canvas');
    c.width = 8;
    c.height = 8;
    scratch = c.getContext('2d');
  }
  return scratch;
}

/** 图层自身的变换：旋转（绕中心）→ 平移到图层原点 → 横向拉伸 */
function transformOf(layer, plan = null) {
  const t = [];
  if (layer.rotation) {
    t.push(`rotate(${num(layer.rotation)} ${num(layer.x + layer.w / 2)}`
      + ` ${num(layer.y + layer.h / 2)})`);
  }
  if (plan) {
    t.push(`translate(${num(layer.x)} ${num(layer.y)})`);
    if (Math.abs(plan.stretch - 1) > 0.01) {
      t.push(`translate(${num(plan.stretchAnchor)} 0) scale(${num(plan.stretch)} 1)`
        + ` translate(${num(-plan.stretchAnchor)} 0)`);
    }
  }
  return t.join(' ');
}

function groupAttrs(layer, transform) {
  const a = [`id="${esc(layer.id)}"`, `data-name="${esc(layer.name || layer.id)}"`];
  if (transform) a.push(`transform="${transform}"`);
  const op = layer.opacity ?? 1;
  if (op !== 1) a.push(`opacity="${num(op)}"`);
  return a.join(' ');
}

function textTag(ctx, layer) {
  const plan = textPlan(ctx, layer);
  const attrs = [
    `font-family="${esc(plan.family)}"`,
    `font-size="${num(plan.size)}"`,
    `font-weight="${layer.fontWeight || 400}"`,
    `fill="${esc(layer.color || '#000')}"`,
  ];
  if (layer.italic) attrs.push('font-style="italic"');
  if (layer.strokeWidth > 0) {
    // paint-order 让描边留在字面下方，和 Canvas 先描边后填充的叠法一致
    attrs.push(`stroke="${esc(layer.strokeColor || '#fff')}"`,
               `stroke-width="${num(layer.strokeWidth * plan.scaleY)}"`,
               'stroke-linejoin="round"', 'paint-order="stroke"');
  }

  const lines = plan.lines.map((line) => {
    // 有字距时逐字给坐标：letter-spacing 属性各家软件解释不一，逐字坐标是 SVG 1.1
    // 的核心特性，Illustrator/Inkscape/浏览器都认，位置也和屏幕上完全一致
    const x = line.glyphs ? line.glyphs.map(num).join(' ') : num(line.x);
    return `  <text x="${x}" y="${num(line.y)}" xml:space="preserve">${esc(line.text)}</text>`;
  });

  return [`<g ${groupAttrs(layer, transformOf(layer, plan))} ${attrs.join(' ')}>`,
          ...lines, '</g>'].join('\n');
}

function shapeTag(layer) {
  const x = Math.min(layer.x, layer.x + layer.w);
  const y = Math.min(layer.y, layer.y + layer.h);
  const w = Math.abs(layer.w);
  const h = Math.abs(layer.h);
  const paint = [`fill="${esc(layer.fill || '#888')}"`];
  if (layer.strokeWidth > 0) {
    paint.push(`stroke="${esc(layer.strokeColor || '#000')}"`,
               `stroke-width="${num(layer.strokeWidth)}"`);
  }

  let node;
  if (layer.shape === 'ellipse') {
    node = `<ellipse cx="${num(x + w / 2)}" cy="${num(y + h / 2)}"`
      + ` rx="${num(w / 2)}" ry="${num(h / 2)}" ${paint.join(' ')}/>`;
  } else {
    const r = layer.shape === 'rounded-rect' && layer.radius > 0
      ? Math.min(layer.radius, w / 2, h / 2) : 0;
    node = `<rect x="${num(x)}" y="${num(y)}" width="${num(w)}" height="${num(h)}"`
      + (r > 0 ? ` rx="${num(r)}"` : '') + ` ${paint.join(' ')}/>`;
  }

  const t = transformOf(layer);
  return `<g ${groupAttrs(layer, t)}>\n  ${node}\n</g>`;
}
