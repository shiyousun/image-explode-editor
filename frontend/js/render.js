/**
 * 渲染引擎。
 *
 * 渲染分三步，顺序很关键：
 *   1. 铺原图（底图）——所有没被编辑过的内容都由它提供，因此天然逐像素保真；
 *   2. 对每个「要重画」的图层，用干净背景把它在底图上的原始位置补掉；
 *   3. 按图层顺序重画这些图层。
 *
 * 「要重画」不只包含被用户改过的图层：如果补丁擦到了别的图层，或某个被移动的图层
 * 压住了上方图层，那些图层也必须一起重画，否则画面会缺块或叠错。buildDrawSet 负责
 * 把这种连带关系传播完整。
 */

import {
  CJK_FONTS, LATIN_FONTS, clamp, originBox, layerBox, rectsIntersect, unionRect,
} from './state.js';

const PATCH_PAD = 1.5;   // 补丁外扩，盖住原像素的抗锯齿边缘

const inflate = (r, pad = PATCH_PAD) => ({
  x: r.x - pad, y: r.y - pad, w: r.w + pad * 2, h: r.h + pad * 2,
});

export function defaultFont(layer) {
  return layer.isCJK ? CJK_FONTS[0].value : LATIN_FONTS[0].value;
}

/* --------------------------------------------------------------------- */
/* 需要重画的图层集合                                                     */
/* --------------------------------------------------------------------- */

export function buildDrawSet(doc, cleanSet = null) {
  const clean = cleanSet || buildCleanSliceSet(doc);
  const set = new Set();
  for (const l of doc.layers) {
    if (l.dirty || !l.fromSource) set.add(l);
  }
  if (!set.size) return set;

  const exact = !!activeCleanImage(doc);
  const idx = new Map(doc.layers.map((l, i) => [l.id, i]));
  for (let guard = 0; guard < 24; guard += 1) {
    const additions = [];
    for (const l of doc.layers) {
      if (set.has(l) || !l.fromSource) continue;
      const box = contentBox(l);
      for (const d of set) {
        // 补丁擦掉了这个图层在底图里的像素 -> 必须重画
        const erased = needsPatch(d) && patchDamages(d, l, exact) && patchHits(box, d);
        // 下层图层重画后变了样，压在它上面的这层必须跟着重画回来
        const covered = d.visible && idx.get(d.id) < idx.get(l.id)
          && altersPixels(d, clean) && rectsIntersect(box, layerBox(d));
        if (erased || covered) { additions.push(l); break; }
      }
    }
    if (!additions.length) break;
    additions.forEach((l) => set.add(l));
  }
  return set;
}

/**
 * 这个图层在底图里的原始像素必须被擦掉吗？只有真的动过的图层才需要——被别人的补丁
 * 波及、只是原位重画一遍的图层不需要，擦了反而会把它周围一整块背景抹掉，逼着里面
 * 所有东西跟着重画。
 *
 * 换用擦字切片的元素（clean 集合）也不需要：原位盖上去就行，底图里那行旧文字由文字
 * 图层自己的补丁负责擦掉，两个补丁都在绘制之前统一打完。
 */
function needsPatch(l) {
  return l.fromSource && (l.dirty || !l.visible || isDisplaced(l));
}

/** 重画后画面会和原图不同吗？原位、用原始切片补回来的图层不算。 */
function altersPixels(l, clean) {
  return l.dirty || !l.fromSource || isDisplaced(l) || clean.has(l);
}

/**
 * 图层实际有内容的范围。文字图层的切片带了留白，用墨迹框判断才不会把只是「留白
 * 蹭到了」的文字也拖进重画名单——那会让同一行字被叠画两遍，边缘变脏。
 */
function contentBox(l) {
  if (l.type === 'text' && l.inkBox) {
    const sx = l.ow > 0 ? l.w / l.ow : 1;
    const sy = l.oh > 0 ? l.h / l.oh : 1;
    return {
      x: l.x + (l.inkBox[0] - l.ox) * sx,
      y: l.y + (l.inkBox[1] - l.oy) * sy,
      w: l.inkBox[2] * sx,
      h: l.inkBox[3] * sy,
    };
  }
  return layerBox(l);
}

/**
 * 哪些元素要改用「已擦字」切片重绘。
 *
 * 横幅、按钮这类底板的切片是从原图裁的，压在上面的文字也一起烘进去了。平时这正是我们
 * 想要的（重绘也和原图一致），但只要那行字被改写、挪走或隐藏，就必须换成擦过字的那份，
 * 否则旧文字会跟着底板一起被画回来。元素自己被移动或缩放时同理。
 */
function buildCleanSliceSet(doc) {
  const set = new Set();
  for (const l of doc.layers) {
    if (!l.sliceCleanUrl) continue;
    const ob = originBox(l);
    const moved = isDisplaced(l);
    const hit = doc.layers.some((t) => t.type === 'text' && t.fromSource
      && (moved || t.dirty || !t.visible) && rectsIntersect(originBox(t), ob));
    if (hit) set.add(l);
  }
  return set;
}

function isDisplaced(l) {
  return l.x !== l.ox || l.y !== l.oy || l.w !== l.ow || l.h !== l.oh;
}

/* --------------------------------------------------------------------- */
/* 精确擦除底图                                                          */
/* --------------------------------------------------------------------- */

/**
 * 需要现场擦掉原位像素的元素，各自要擦的那块矩形（就是补丁实际画的范围）。
 *
 * 后端按这批矩形现算一张「只擦掉这几个元素」的底图，画面里其他一切都还在。这比预生成的
 * clean_all 准得多：图标压在侧栏面板上时，clean_all 把面板也一起擦了，补出来是页面
 * 背景色，面板破一个洞；而只擦图标时，缺口由四周的面板像素补上，面板自然还在。
 *
 * 文字不走这条路——它有现成的字形掩码，clean_text 只擦笔画、周围背景分毫不动；按矩形
 * 擦反而会把整块背景换成插值出来的平滑版本，留下一个看得见的方块。
 */
export function eraseRects(doc) {
  const rects = [];
  for (const l of doc.layers) {
    if (l.type === 'text' || !needsPatch(l)) continue;
    const r = patchRect(doc, l);
    if (r.w > 0 && r.h > 0) rects.push([r.x, r.y, r.w, r.h]);
  }
  return rects;
}

/** 擦除区域的指纹，用来判断已有的那张擦除底图还算不算数 */
export function eraseKey(doc) {
  const rects = eraseRects(doc);
  return rects.length ? rects.map((r) => r.map(Math.round).join(',')).join(';') : '';
}

/** 当前这张按需擦除底图还对得上吗？对不上就先用预生成的那两张顶着 */
function activeCleanImage(doc) {
  if (!doc.activeCleanImg || !doc.activeCleanKey) return null;
  return doc.activeCleanKey === eraseKey(doc) ? doc.activeCleanImg : null;
}

function patchRect(doc, layer) {
  const x = Math.max(0, layer.ox - PATCH_PAD);
  const y = Math.max(0, layer.oy - PATCH_PAD);
  return {
    x,
    y,
    w: Math.min(doc.width - x, layer.ow + PATCH_PAD * 2),
    h: Math.min(doc.height - y, layer.oh + PATCH_PAD * 2),
  };
}

/** 编辑波及的图层数量，用于在界面上提示保真状态 */
export function fidelityStats(doc) {
  const drawSet = buildDrawSet(doc);
  const edited = doc.layers.filter((l) => l.dirty || !l.fromSource).length;
  return { edited, redrawn: drawSet.size, total: doc.layers.length };
}

/* --------------------------------------------------------------------- */
/* 文字排版                                                              */
/* --------------------------------------------------------------------- */

export function fontString(layer, sizePx) {
  const family = layer.fontFamily || defaultFont(layer);
  const style = layer.italic ? 'italic ' : '';
  return `${style}${layer.fontWeight || 400} ${sizePx}px ${family}`;
}

function measureRun(ctx, text, spacing) {
  if (!text) return 0;
  if (!spacing) return ctx.measureText(text).width;
  const chars = [...text];
  let w = 0;
  for (const ch of chars) w += ctx.measureText(ch).width + spacing;
  return w - spacing;
}

/**
 * 一行字「看得见的那部分」有多宽。
 *
 * 排版宽度含首尾两侧的字符边距，拿它去对原图量出来的墨迹宽度会系统性偏大：全角括号的
 * 字面只占 em 框的一半，「（2024-2032）」两头就白搭进去七十多像素，照排版宽度贴合会把
 * 中间的数字挤瘦一圈。墨迹对墨迹才是同一把尺子。
 */
function measureInkRun(ctx, text, spacing) {
  const adv = measureRun(ctx, text, spacing);
  const chars = [...text];
  if (!chars.length) return adv;
  const first = ctx.measureText(chars[0]);
  const last = ctx.measureText(chars[chars.length - 1]);
  const leftGap = Number.isFinite(first.actualBoundingBoxLeft)
    ? Math.max(0, -first.actualBoundingBoxLeft) : 0;
  const rightGap = Number.isFinite(last.actualBoundingBoxRight)
    ? Math.max(0, last.width - last.actualBoundingBoxRight) : 0;
  return Math.max(1, adv - leftGap - rightGap);
}

function drawRun(ctx, text, x, y, spacing, mode) {
  if (!text) return;
  if (!spacing) {
    if (mode === 'stroke') ctx.strokeText(text, x, y);
    else ctx.fillText(text, x, y);
    return;
  }
  let cx = x;
  for (const ch of [...text]) {
    if (mode === 'stroke') ctx.strokeText(ch, cx, y);
    else ctx.fillText(ch, cx, y);
    cx += ctx.measureText(ch).width + spacing;
  }
}

/**
 * 自动字距贴合：OCR 识别出的文字重新排版后宽度往往和原图差几个百分点
 * （中英文之间的空格常被 OCR 吞掉，字体也不可能完全一致）。这里按原始墨迹宽度
 * 反算需要补多少字距，让矢量文字仍然严丝合缝地落在原来的位置上。
 */
function computeFitSpacing(ctx, layer, targetWidth) {
  const lines = String(layer.text ?? '').split('\n');
  const longest = lines.reduce((a, b) => (b.length > a.length ? b : a), '');
  const count = [...longest].length;
  if (count < 2 || targetWidth <= 0) return 0;
  const natural = measureInkRun(ctx, longest, layer.letterSpacing || 0);
  if (natural <= 0) return 0;
  const delta = (targetWidth - natural) / (count - 1);
  // 钳位收在 0.12 字号（约合排版上的 ±120‰ 字距）以内。原先放到 0.4 字号，
  // 一旦 OCR 少认一个字（全角括号最常漏），就会把余下的字硬塞进原来的宽度，
  // 挤成一行相互压边的窄体。宁可宽度差几个百分点，也不能把字形挤变形。
  const limit = layer.fontSize * 0.12;
  return Math.max(-limit, Math.min(limit, delta));
}

/**
 * 字距贴合到顶还是明显超出原始宽度时，把字号往回压一点。
 *
 * 字号是拿墨迹高度反推的，行里混进全角括号、引号这类比数字高的字符时会被顶大一截：
 * 「（2024-2032）」量出 61px，其实数字只有 47px 的字面，重画出来整行比原文长 50px。
 * 与其让它涨出去压到旁边的字，不如按宽度比例缩回来，上限 14%，超出说明是别的问题
 * （字体差异或 OCR 少认了字），那就宁可留着宽度差，不要把字号越改越小。
 */
function fitScale(ctx, layer, g, spacing) {
  const lines = String(layer.text ?? '').split('\n');
  const longest = lines.reduce((a, b) => (b.length > a.length ? b : a), '');
  if (!longest || g.inkW <= 0) return 1;
  const w = measureInkRun(ctx, longest, spacing);
  if (w <= g.inkW * 1.03) return 1;
  return Math.max(0.86, g.inkW / w);
}

/** 矢量文字的绘制信息（含缩放与对齐锚点） */
function textGeometry(layer) {
  const sx = layer.ow > 0 ? layer.w / layer.ow : 1;
  const sy = layer.oh > 0 ? layer.h / layer.oh : 1;
  const ink = layer.inkBox || [layer.ox, layer.oy, layer.ow, layer.oh];
  const ix = (ink[0] - layer.ox) * sx;
  const iy = (ink[1] - layer.oy) * sy;
  const inkW = ink[2] * sx;
  const inkH = ink[3] * sy;
  const size = Math.max(1, layer.fontSize * sy);
  let anchorX = ix;
  if (layer.align === 'center') anchorX = ix + inkW / 2;
  else if (layer.align === 'right') anchorX = ix + inkW;
  return { sx, sy, ix, iy, inkW, inkH, size, anchorX,
           baseline: iy + (layer.baselineOffset || layer.fontSize) * sy };
}

/**
 * 让重绘的文字落回原来的高度。
 *
 * 原先按「基线 = 墨迹上沿 + 字号」推算。拉丁字母够用，但中日韩字形的 em 框比实际墨迹高出
 * 一成多，整行于是往下坐了几个像素——实测中文标题差 5px，小字号差到 8px，改完字看着就像
 * 掉了一行。Canvas 的 actualBoundingBox 系列直接给出这串字在当前字体字号下的真实上下沿，
 * 拿它对齐与字体、字号、语言都无关。
 *
 * 不直接把上沿钉在原位，而是按上沿/下沿的比例落位：行首若是全角括号这类又高又瘦的字符，
 * 它会把整串的上沿单独顶高（实测「（2024-2032」因此下坠 9px），按比例分摊就稳当得多。
 */
function snapBaseline(ctx, layer, g, firstLine) {
  if (!firstLine) return g.baseline;
  const m = ctx.measureText(firstLine);
  const ascent = m.actualBoundingBoxAscent;
  const descent = m.actualBoundingBoxDescent;
  if (!Number.isFinite(ascent) || ascent <= 0) return g.baseline;
  const drawn = ascent + (Number.isFinite(descent) ? Math.max(0, descent) : 0);
  if (drawn <= 0 || g.inkH <= 0) return g.iy + ascent;
  return g.iy + g.inkH * (ascent / drawn);
}

/**
 * 让重绘的文字左边缘落回原位（左对齐时）。
 *
 * 原图量出来的墨迹框是「看得见的笔画」的边界，而 Canvas 从笔位开始画字，中间还隔着字符
 * 自己的左边距。全角括号这类字符的字面只占 em 框右半边，左边距能有半个字宽：实测
 * 「（2024-2032）」按墨迹框左沿开画，视觉上整串右移 41px，标题和括号之间凭空多出一道缝。
 * 用 actualBoundingBoxLeft 把这段边距抵掉即可，和字体、字号无关。
 */
function snapInkLeft(ctx, layer, g, firstLine) {
  if (!firstLine || (layer.align && layer.align !== 'left')) return g.anchorX;
  const bearing = ctx.measureText(firstLine[0]).actualBoundingBoxLeft;
  if (!Number.isFinite(bearing)) return g.anchorX;
  // actualBoundingBoxLeft 以「向左为正」计量：为负说明字面落在笔位右边，把这段空档收掉；
  // 为正是 f、J 这类字面向左悬挑的西文字符，原样画反而更贴原图
  return bearing < 0 ? g.anchorX + bearing : g.anchorX;
}

/** 每个字的落笔横坐标（手动字距时逐字画，位置得自己累计） */
function glyphXs(ctx, text, x, spacing) {
  const xs = [];
  let cx = x;
  for (const ch of [...text]) {
    xs.push(cx);
    cx += ctx.measureText(ch).width + spacing;
  }
  return xs;
}

/**
 * 矢量文字排版的唯一出处：字号、字距、每行每字落在哪。
 *
 * Canvas 绘制和 SVG 导出都读这一份。自动贴合会连着改字号和字距，基线和左沿还各有一套
 * 补正，两边各算一遍迟早对不上——导出的矢量图就会和屏幕上差半个字。
 *
 * 传进来的 ctx 只用于测量，函数结束时状态复原。
 */
export function textPlan(ctx, layer) {
  const g = textGeometry(layer);
  const lines = String(layer.text ?? '').split('\n');
  ctx.save();
  let size = g.size;
  ctx.font = fontString(layer, size);
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';

  let spacing = (layer.letterSpacing || 0) * g.sy;
  if (layer.autoFit) {
    const base = (layer.letterSpacing || 0) * g.sy;
    spacing = base + computeFitSpacing(ctx, { ...layer, fontSize: size,
      letterSpacing: base }, g.inkW);
    const scale = fitScale(ctx, layer, g, spacing);
    if (scale < 1) {
      size *= scale;
      ctx.font = fontString(layer, size);
      spacing = base * scale;
      spacing += computeFitSpacing(ctx, { ...layer, fontSize: size,
        letterSpacing: spacing }, g.inkW);
    }
  }

  const lineStep = size * (layer.lineHeight || 1.25);
  const baseline = snapBaseline(ctx, layer, g, lines[0]);
  const anchorX = snapInkLeft(ctx, layer, g, lines[0]);
  const plan = lines.map((line, i) => {
    // 一律自己算行首，不借 textAlign：手动字距下它本来就失效，两条路子并存容易走岔
    let x = anchorX;
    if (layer.align === 'center' || layer.align === 'right') {
      const w = measureRun(ctx, line, spacing);
      x = layer.align === 'center' ? g.anchorX - w / 2 : g.anchorX - w;
    }
    return {
      text: line,
      x,
      y: baseline + i * lineStep,
      glyphs: spacing ? glyphXs(ctx, line, x, spacing) : null,
    };
  });
  ctx.restore();

  return {
    size,
    spacing,
    lineStep,
    lines: plan,
    scaleY: g.sy,
    inkW: g.inkW,
    family: layer.fontFamily || defaultFont(layer),
    // 非等比缩放时横向再补一次比例，避免文字被压扁/拉长得不自然
    stretch: g.sy > 0 ? g.sx / g.sy : 1,
    stretchAnchor: g.anchorX,
  };
}

function drawVectorText(ctx, layer) {
  const plan = textPlan(ctx, layer);
  ctx.save();
  ctx.translate(layer.x, layer.y);
  if (Math.abs(plan.stretch - 1) > 0.01) {
    ctx.translate(plan.stretchAnchor, 0);
    ctx.scale(plan.stretch, 1);
    ctx.translate(-plan.stretchAnchor, 0);
  }
  ctx.font = fontString(layer, plan.size);
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';

  for (const line of plan.lines) {
    if (layer.strokeWidth > 0) {
      ctx.strokeStyle = layer.strokeColor || '#fff';
      ctx.lineWidth = layer.strokeWidth * plan.scaleY;
      ctx.lineJoin = 'round';
      drawRun(ctx, line.text, line.x, line.y, plan.spacing, 'stroke');
    }
    ctx.fillStyle = layer.color || '#000';
    drawRun(ctx, line.text, line.x, line.y, plan.spacing, 'fill');
  }
  ctx.restore();
}

/** 供属性面板显示：矢量重排后的宽度与原始宽度的差异百分比 */
export function textWidthDrift(ctx, layer) {
  if (layer.type !== 'text') return 0;
  const plan = textPlan(ctx, layer);
  const lines = String(layer.text ?? '').split('\n');
  const longest = lines.reduce((a, b) => (b.length > a.length ? b : a), '');
  ctx.save();
  ctx.font = fontString(layer, plan.size);
  const w = measureInkRun(ctx, longest, plan.spacing);
  ctx.restore();
  return plan.inkW > 0 ? (w - plan.inkW) / plan.inkW : 0;
}

/* --------------------------------------------------------------------- */
/* 图层绘制                                                              */
/* --------------------------------------------------------------------- */

function shapePath(ctx, layer) {
  const { x, y, w, h } = layer;
  ctx.beginPath();
  if (layer.shape === 'ellipse') {
    ctx.ellipse(x + w / 2, y + h / 2, Math.abs(w / 2), Math.abs(h / 2), 0, 0, Math.PI * 2);
  } else if (layer.shape === 'rounded-rect' && layer.radius > 0) {
    const r = Math.min(layer.radius, Math.abs(w) / 2, Math.abs(h) / 2);
    ctx.roundRect(x, y, w, h, r);
  } else {
    ctx.rect(x, y, w, h);
  }
}

function filterString(f) {
  if (!f) return 'none';
  const parts = [];
  if (f.brightness !== 100) parts.push(`brightness(${f.brightness}%)`);
  if (f.contrast !== 100) parts.push(`contrast(${f.contrast}%)`);
  if (f.saturate !== 100) parts.push(`saturate(${f.saturate}%)`);
  if (f.blur > 0) parts.push(`blur(${f.blur}px)`);
  if (f.grayscale > 0) parts.push(`grayscale(${f.grayscale}%)`);
  return parts.length ? parts.join(' ') : 'none';
}

/**
 * 只是被连带波及、本身没动过的图层，直接从原图切一块像素盖回去。
 *
 * 用切片重新混色是做不到无损的：文字切片的 alpha 是从原图反解出来的近似值，
 * 半透明边缘重新混一次就会差几个色阶，放大看是一层薄雾。而原图那块像素本身就是
 * 正确答案。只有下方真被改过的那一小块除外——那里背景已经变了，只能重新混。
 */
function restoreFromBase(ctx, doc, layer, rc) {
  if (!doc.baseImg || layer.dirty || !layer.fromSource) return false;
  if (isDisplaced(layer) || (layer.opacity ?? 1) !== 1) return false;
  if (rc.cleanSet.has(layer)) return false;

  const r = originBox(layer);
  const blocker = rc.blockerFor(layer, r);
  const blit = () => ctx.drawImage(doc.baseImg, r.x, r.y, r.w, r.h, r.x, r.y, r.w, r.h);
  if (!blocker) { blit(); return true; }

  ctx.save();
  ctx.beginPath();
  ctx.rect(r.x, r.y, r.w, r.h);
  ctx.rect(blocker.x, blocker.y, blocker.w, blocker.h);
  ctx.clip('evenodd');
  blit();
  ctx.restore();

  const img = doc.imageFor(layer);
  if (img) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(blocker.x, blocker.y, blocker.w, blocker.h);
    ctx.clip();
    ctx.globalAlpha = layer.opacity ?? 1;
    ctx.drawImage(img, layer.x, layer.y, layer.w, layer.h);
    ctx.restore();
  }
  return true;
}

export function drawLayer(ctx, doc, layer, rc = null) {
  if (!layer.visible) return;
  if (rc && restoreFromBase(ctx, doc, layer, rc)) return;
  const useClean = !!rc && rc.cleanSet.has(layer);
  ctx.save();
  ctx.globalAlpha = layer.opacity ?? 1;

  if (layer.rotation) {
    const cx = layer.x + layer.w / 2;
    const cy = layer.y + layer.h / 2;
    ctx.translate(cx, cy);
    ctx.rotate((layer.rotation * Math.PI) / 180);
    ctx.translate(-cx, -cy);
  }

  if (layer.type === 'text') {
    const img = doc.imageFor(layer);
    if (layer.textMode === 'pixel' && img) {
      ctx.drawImage(img, layer.x, layer.y, layer.w, layer.h);
    } else {
      drawVectorText(ctx, layer);
    }
  } else if (layer.type === 'shape') {
    const img = doc.imageFor(layer, useClean);
    if (layer.shapeMode === 'pixel' && img && !layer.srcUrl) {
      ctx.drawImage(img, layer.x, layer.y, layer.w, layer.h);
    } else {
      shapePath(ctx, layer);
      ctx.fillStyle = layer.fill || '#888';
      ctx.fill();
      if (layer.strokeWidth > 0) {
        ctx.strokeStyle = layer.strokeColor || '#000';
        ctx.lineWidth = layer.strokeWidth;
        ctx.stroke();
      }
    }
  } else {
    const img = doc.imageFor(layer, useClean);
    if (img) {
      const filt = filterString(layer.filters);
      if (filt !== 'none') ctx.filter = filt;
      ctx.drawImage(img, layer.x, layer.y, layer.w, layer.h);
      ctx.filter = 'none';
    }
  }
  ctx.restore();
}

function patchLayer(ctx, doc, layer) {
  const src = layer.type === 'text'
    ? (doc.cleanTextImg || doc.cleanAllImg)
    : (activeCleanImage(doc) || doc.cleanAllImg || doc.cleanTextImg);
  if (!src) return;
  const { x, y, w, h } = patchRect(doc, layer);
  if (w <= 0 || h <= 0) return;

  // 圆形、圆角块只擦掉形状本身。按外接矩形擦会连四角一起抹掉，紧挨着的文字会被
  // 迫使重画一遍，边缘因此变毛。
  const path = originShapePath(layer, PATCH_PAD);
  if (path) {
    ctx.save();
    path(ctx);
    ctx.clip();
    ctx.drawImage(src, x, y, w, h, x, y, w, h);
    ctx.restore();
    return;
  }
  ctx.drawImage(src, x, y, w, h, x, y, w, h);
}

/** 形状图层在原始位置的轮廓（外扩 pad 盖住抗锯齿边缘）；其他类型返回 null 走矩形。 */
function originShapePath(layer, pad) {
  if (layer.type !== 'shape' || !layer.shape) return null;
  const { ox: x, oy: y, ow: w, oh: h } = layer;
  if (layer.shape === 'ellipse') {
    return (ctx) => {
      ctx.beginPath();
      ctx.ellipse(x + w / 2, y + h / 2,
        Math.abs(w / 2) + pad, Math.abs(h / 2) + pad, 0, 0, Math.PI * 2);
    };
  }
  if (layer.shape === 'rounded-rect' && layer.radius > 0) {
    const r = Math.min(layer.radius, Math.abs(w) / 2, Math.abs(h) / 2);
    return (ctx) => {
      ctx.beginPath();
      ctx.roundRect(x - pad, y - pad, w + pad * 2, h + pad * 2, r + pad);
    };
  }
  return null;
}

/**
 * d 的补丁会破坏 l 吗？
 *
 * 文字的补丁用「只擦文字」的底图，横幅、卡片这些底板在里面完好，所以改一行字不该连底板
 * 一起重画——那会连锁到底板上所有文字，整张图被重新混一遍。
 *
 * 元素的补丁默认波及所有类型（预生成的底图把所有元素都擦了）。但用上按需擦除底图后，
 * 缺口是照四周像素补出来的：把补丁范围整块罩住的底板（面板、卡片、大背景图）补完看不出
 * 差别，不用重画；而内容落在补丁范围里的图层，像素确实被填掉了，必须重画。
 */
function patchDamages(d, l, exact = false) {
  if (d.type === 'text') return l.type === 'text';
  if (exact && contains(originBox(l), inflate(originBox(d)))) return false;
  return true;
}

function contains(outer, inner) {
  return outer.x <= inner.x && outer.y <= inner.y
    && outer.x + outer.w >= inner.x + inner.w
    && outer.y + outer.h >= inner.y + inner.h;
}

/** 补丁会不会擦到 box？圆形补丁按椭圆判定，否则按矩形。 */
function patchHits(box, layer, pad = PATCH_PAD) {
  const ob = originBox(layer);
  if (!rectsIntersect(box, ob, pad)) return false;
  if (layer.type === 'shape' && layer.shape === 'ellipse') {
    const rx = Math.abs(ob.w / 2) + pad;
    const ry = Math.abs(ob.h / 2) + pad;
    const cx = ob.x + ob.w / 2;
    const cy = ob.y + ob.h / 2;
    const dx = (clamp(cx, box.x, box.x + box.w) - cx) / (rx || 1);
    const dy = (clamp(cy, box.y, box.y + box.h) - cy) / (ry || 1);
    return dx * dx + dy * dy <= 1;
  }
  return true;
}

/* --------------------------------------------------------------------- */
/* 主渲染                                                                */
/* --------------------------------------------------------------------- */

export function renderDoc(ctx, doc, opts = {}) {
  const { showOriginal = false, drawSet = null } = opts;
  if (!doc.isReady) return;

  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(doc.baseImg, 0, 0, doc.width, doc.height);
  if (showOriginal) return;

  const cleanSet = buildCleanSliceSet(doc);
  const set = drawSet || buildDrawSet(doc, cleanSet);
  if (!set.size) return;

  const rc = renderContext(doc, cleanSet);
  for (const l of doc.layers) {
    if (set.has(l) && needsPatch(l)) patchLayer(ctx, doc, l);
  }
  for (const l of doc.layers) {
    if (set.has(l)) drawLayer(ctx, doc, l, rc);
  }
}

/** 一次渲染内共享的判断：谁用擦字切片、谁下方有真被改动的区域 */
function renderContext(doc, cleanSet) {
  const idx = new Map(doc.layers.map((l, i) => [l.id, i]));
  const altered = doc.layers.filter((l) => altersPixels(l, cleanSet));
  const exact = !!activeCleanImage(doc);
  return {
    cleanSet,
    /** 图层身上「不能再用原图像素填」的区域合成一个包围盒；没有则返回 null */
    blockerFor(layer, rect) {
      const boxes = [];
      const own = idx.get(layer.id);
      for (const d of altered) {
        if (d === layer) continue;
        // 补丁是在所有图层之前统一打的，所以不论 d 在这层上面还是下面，被它擦掉的
        // 那块都不能再从原图填回来——填了就等于把已经挪走的东西又画了一份。
        if (needsPatch(d) && patchDamages(d, layer, exact) && patchHits(rect, d)) {
          boxes.push(inflate(originBox(d)));
        }
        // 只有画在这层之前（更下面）的图层，新样子才会被这层的原图像素盖掉
        if (idx.get(d.id) < own && d.visible && rectsIntersect(rect, layerBox(d))) {
          boxes.push(layerBox(d));
        }
      }
      return boxes.length ? unionRect(boxes) : null;
    },
  };
}

/** 导出：在离屏画布上按原分辨率（或倍率）重绘一遍 */
export function renderToCanvas(doc, scale = 1, background = null) {
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(doc.width * scale);
  canvas.height = Math.round(doc.height * scale);
  const ctx = canvas.getContext('2d');
  if (background) {
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  renderDoc(ctx, doc);
  return canvas;
}

/* --------------------------------------------------------------------- */
/* 覆盖层：选择框 / 控制点 / 辅助线                                        */
/* --------------------------------------------------------------------- */

export const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];

export function handlePositions(layer) {
  const { x, y, w, h } = layer;
  return {
    nw: [x, y], n: [x + w / 2, y], ne: [x + w, y],
    e: [x + w, y + h / 2], se: [x + w, y + h], s: [x + w / 2, y + h],
    sw: [x, y + h], w: [x, y + h / 2],
    rot: [x + w / 2, y - 22],
  };
}

export function renderOverlay(ctx, doc, opts) {
  const { selected = [], hover = null, scale = 1, guides = [], marquee = null } = opts;
  const px = 1 / scale;   // 1 屏幕像素在文档坐标下的长度

  if (hover && !selected.includes(hover)) {
    ctx.save();
    ctx.strokeStyle = 'rgba(122,162,255,.65)';
    ctx.lineWidth = px;
    withRotation(ctx, hover, () => ctx.strokeRect(hover.x, hover.y, hover.w, hover.h));
    ctx.restore();
  }

  for (const g of guides) {
    ctx.save();
    ctx.strokeStyle = '#ff4d9e';
    ctx.lineWidth = px;
    ctx.setLineDash([4 * px, 3 * px]);
    ctx.beginPath();
    if (g.axis === 'x') { ctx.moveTo(g.pos, g.from); ctx.lineTo(g.pos, g.to); }
    else { ctx.moveTo(g.from, g.pos); ctx.lineTo(g.to, g.pos); }
    ctx.stroke();
    ctx.restore();
  }

  if (selected.length > 1) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const l of selected) {
      x0 = Math.min(x0, l.x); y0 = Math.min(y0, l.y);
      x1 = Math.max(x1, l.x + l.w); y1 = Math.max(y1, l.y + l.h);
    }
    ctx.save();
    ctx.strokeStyle = 'rgba(91,140,255,.55)';
    ctx.lineWidth = px;
    ctx.setLineDash([5 * px, 4 * px]);
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    ctx.restore();
  }

  for (const l of selected) {
    ctx.save();
    withRotation(ctx, l, () => {
      ctx.strokeStyle = '#5b8cff';
      ctx.lineWidth = 1.4 * px;
      ctx.strokeRect(l.x, l.y, l.w, l.h);

      if (selected.length === 1 && !l.locked) {
        const hs = 4.5 * px;
        const pos = handlePositions(l);
        ctx.fillStyle = '#fff';
        ctx.strokeStyle = '#5b8cff';
        ctx.lineWidth = 1.2 * px;
        for (const key of HANDLES) {
          const [hx, hy] = pos[key];
          ctx.beginPath();
          ctx.rect(hx - hs, hy - hs, hs * 2, hs * 2);
          ctx.fill();
          ctx.stroke();
        }
        // 旋转手柄
        ctx.beginPath();
        ctx.moveTo(l.x + l.w / 2, l.y);
        ctx.lineTo(pos.rot[0], pos.rot[1]);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(pos.rot[0], pos.rot[1], hs * 1.15, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
    });
    ctx.restore();
  }

  if (marquee) {
    ctx.save();
    ctx.strokeStyle = '#5b8cff';
    ctx.fillStyle = 'rgba(91,140,255,.12)';
    ctx.lineWidth = px;
    ctx.fillRect(marquee.x, marquee.y, marquee.w, marquee.h);
    ctx.strokeRect(marquee.x, marquee.y, marquee.w, marquee.h);
    ctx.restore();
  }
}

function withRotation(ctx, layer, fn) {
  if (layer.rotation) {
    const cx = layer.x + layer.w / 2;
    const cy = layer.y + layer.h / 2;
    ctx.translate(cx, cy);
    ctx.rotate((layer.rotation * Math.PI) / 180);
    ctx.translate(-cx, -cy);
  }
  fn();
}
