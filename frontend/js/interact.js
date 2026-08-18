/**
 * 画布交互：选择、拖拽、缩放、旋转、框选、平移、内联改字、以及抠取/擦除的框选。
 */

import {
  clamp, hitLayer, layerBox, markDirty, toLocal, rectsIntersect,
} from './state.js';
import { HANDLES, handlePositions, fontString } from './render.js';

const SNAP_PX = 6;          // 吸附阈值（屏幕像素）
const HANDLE_HIT = 7;       // 控制点命中半径（屏幕像素）

export class CanvasController {
  constructor(app) {
    this.app = app;
    this.view = { scale: 1, ox: 0, oy: 0 };
    this.mode = 'select';        // select | text | rect | ellipse | cutout | erase
    this.selected = [];
    this.hover = null;
    this.guides = [];
    this.marquee = null;
    this.drag = null;
    this.spacePan = false;
    this.editing = null;
    this.bind();
  }

  get doc() { return this.app.doc; }
  get stage() { return this.app.el.stage; }

  /* ---------------- 坐标换算 ---------------- */

  toDoc(clientX, clientY) {
    const rect = this.stage.getBoundingClientRect();
    return {
      x: (clientX - rect.left - this.view.ox) / this.view.scale,
      y: (clientY - rect.top - this.view.oy) / this.view.scale,
    };
  }

  toScreen(x, y) {
    return { x: x * this.view.scale + this.view.ox, y: y * this.view.scale + this.view.oy };
  }

  fit(margin = 48) {
    const doc = this.doc;
    if (!doc.isReady) return;
    const wrap = this.app.el.stageScroll;
    const sw = wrap.clientWidth - margin;
    const sh = wrap.clientHeight - margin;
    const scale = Math.min(sw / doc.width, sh / doc.height, 4);
    this.view.scale = Math.max(0.02, scale);
    this.center();
  }

  center() {
    const doc = this.doc;
    const wrap = this.app.el.stageScroll;
    this.view.ox = (wrap.clientWidth - doc.width * this.view.scale) / 2;
    this.view.oy = (wrap.clientHeight - doc.height * this.view.scale) / 2;
    this.app.requestRender();
  }

  zoomTo(scale, anchor) {
    const doc = this.doc;
    if (!doc.isReady) return;
    const next = clamp(scale, 0.02, 16);
    const wrap = this.app.el.stageScroll;
    const a = anchor || { x: wrap.clientWidth / 2, y: wrap.clientHeight / 2 };
    const before = {
      x: (a.x - this.view.ox) / this.view.scale,
      y: (a.y - this.view.oy) / this.view.scale,
    };
    this.view.scale = next;
    this.view.ox = a.x - before.x * next;
    this.view.oy = a.y - before.y * next;
    this.app.requestRender();
    this.app.updateZoomLabel();
  }

  /* ---------------- 命中测试 ---------------- */

  hitTest(pt) {
    const layers = this.doc.layers;
    for (let i = layers.length - 1; i >= 0; i -= 1) {
      const l = layers[i];
      if (!l.visible || l.locked) continue;
      if (hitLayer(l, pt.x, pt.y, 1)) return l;
    }
    return null;
  }

  hitHandle(pt) {
    if (this.selected.length !== 1) return null;
    const l = this.selected[0];
    if (l.locked) return null;
    const tol = HANDLE_HIT / this.view.scale;
    const local = toLocal(l, pt.x, pt.y);
    const pos = handlePositions(l);
    for (const key of [...HANDLES, 'rot']) {
      const [hx, hy] = pos[key];
      if (Math.abs(local.x - hx) <= tol && Math.abs(local.y - hy) <= tol) return key;
    }
    return null;
  }

  /* ---------------- 选择 ---------------- */

  select(layers, additive = false) {
    const list = Array.isArray(layers) ? layers : (layers ? [layers] : []);
    if (additive) {
      for (const l of list) {
        const i = this.selected.indexOf(l);
        if (i >= 0) this.selected.splice(i, 1);
        else this.selected.push(l);
      }
    } else {
      this.selected = list.slice();
    }
    this.app.onSelectionChange();
  }

  selectAll() {
    this.selected = this.doc.layers.filter((l) => l.visible && !l.locked);
    this.app.onSelectionChange();
  }

  /* ---------------- 事件绑定 ---------------- */

  bind() {
    const el = this.app.el.stage;
    el.addEventListener('pointerdown', (e) => this.onDown(e));
    window.addEventListener('pointermove', (e) => this.onMove(e));
    window.addEventListener('pointerup', (e) => this.onUp(e));
    el.addEventListener('dblclick', (e) => this.onDblClick(e));
    el.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
    el.addEventListener('contextmenu', (e) => e.preventDefault());

    window.addEventListener('keydown', (e) => this.onKeyDown(e));
    window.addEventListener('keyup', (e) => this.onKeyUp(e));
  }

  onWheel(e) {
    e.preventDefault();
    const rect = this.stage.getBoundingClientRect();
    const anchor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    if (e.ctrlKey || e.metaKey) {
      const factor = Math.exp(-e.deltaY * 0.0022);
      this.zoomTo(this.view.scale * factor, anchor);
    } else {
      this.view.ox -= e.deltaX;
      this.view.oy -= e.deltaY;
      this.app.requestRender();
    }
  }

  onDown(e) {
    if (!this.doc.isReady || e.button === 2) return;
    this.stage.setPointerCapture?.(e.pointerId);
    const pt = this.toDoc(e.clientX, e.clientY);
    this.commitEdit();

    if (this.spacePan || e.button === 1) {
      this.drag = { kind: 'pan', sx: e.clientX, sy: e.clientY,
                    ox: this.view.ox, oy: this.view.oy };
      return;
    }

    if (this.mode === 'cutout' || this.mode === 'erase') {
      this.drag = { kind: 'region', start: pt, rect: null };
      return;
    }

    if (this.mode === 'text' || this.mode === 'rect' || this.mode === 'ellipse') {
      this.drag = { kind: 'create', start: pt, rect: null, shape: this.mode };
      return;
    }

    const handle = this.hitHandle(pt);
    if (handle) {
      const l = this.selected[0];
      this.drag = handle === 'rot'
        ? { kind: 'rotate', layer: l, start: pt, startRotation: l.rotation || 0,
            cx: l.x + l.w / 2, cy: l.y + l.h / 2 }
        : { kind: 'resize', layer: l, handle, start: pt, box: layerBox(l),
            ratio: l.w / Math.max(1e-6, l.h) };
      return;
    }

    const target = this.hitTest(pt);
    if (!target) {
      if (!e.shiftKey) this.select([]);
      this.drag = { kind: 'marquee', start: pt, additive: e.shiftKey };
      return;
    }

    if (e.shiftKey) {
      this.select([target], true);
    } else if (!this.selected.includes(target)) {
      this.select([target]);
    }

    this.drag = {
      kind: 'move',
      start: pt,
      items: this.selected.filter((l) => !l.locked)
        .map((l) => ({ layer: l, x: l.x, y: l.y })),
      moved: false,
      alt: e.altKey,
    };
  }

  onMove(e) {
    if (!this.doc.isReady) return;
    const pt = this.toDoc(e.clientX, e.clientY);

    if (!this.drag) {
      const hit = this.hitTest(pt);
      if (hit !== this.hover) {
        this.hover = hit;
        this.app.requestRender();
      }
      this.updateCursor(pt);
      this.app.updateCoords(pt);
      return;
    }

    const d = this.drag;
    if (d.kind === 'pan') {
      this.view.ox = d.ox + (e.clientX - d.sx);
      this.view.oy = d.oy + (e.clientY - d.sy);
      this.app.requestRender();
      return;
    }

    if (d.kind === 'marquee' || d.kind === 'create' || d.kind === 'region') {
      d.rect = normRect(d.start, pt);
      this.marquee = d.rect;
      this.app.requestRender();
      return;
    }

    if (d.kind === 'move') {
      let dx = pt.x - d.start.x;
      let dy = pt.y - d.start.y;
      if (e.shiftKey) {
        if (Math.abs(dx) > Math.abs(dy)) dy = 0; else dx = 0;
      }
      const snap = this.computeSnap(d.items, dx, dy);
      dx += snap.dx; dy += snap.dy;
      this.guides = snap.guides;
      for (const it of d.items) {
        it.layer.x = it.x + dx;
        it.layer.y = it.y + dy;
        markDirty(it.layer);
      }
      d.moved = Math.abs(dx) > 0.01 || Math.abs(dy) > 0.01;
      this.app.requestRender();
      this.app.syncPropPanel();
      return;
    }

    if (d.kind === 'resize') {
      // 文字默认等比缩放（避免被拉变形），按住 Alt 才自由拉伸
      const keepRatio = e.shiftKey || (d.layer.type === 'text' && !e.altKey);
      this.applyResize(d, pt, keepRatio);
      this.app.requestRender();
      this.app.syncPropPanel();
      return;
    }

    if (d.kind === 'rotate') {
      const a0 = Math.atan2(d.start.y - d.cy, d.start.x - d.cx);
      const a1 = Math.atan2(pt.y - d.cy, pt.x - d.cx);
      let deg = d.startRotation + ((a1 - a0) * 180) / Math.PI;
      if (e.shiftKey) deg = Math.round(deg / 15) * 15;
      d.layer.rotation = Math.round(deg * 10) / 10;
      markDirty(d.layer);
      this.app.requestRender();
      this.app.syncPropPanel();
    }
  }

  onUp(e) {
    const d = this.drag;
    this.drag = null;
    this.guides = [];
    this.marquee = null;
    if (!d) return;

    if (d.kind === 'marquee' && d.rect) {
      const picked = this.doc.layers.filter(
        (l) => l.visible && !l.locked && rectsIntersect(layerBox(l), d.rect));
      this.select(picked, d.additive);
    } else if (d.kind === 'create' && d.rect) {
      this.app.createLayerFromDrag(d.shape, d.rect);
    } else if (d.kind === 'create' && !d.rect) {
      const pt = this.toDoc(e.clientX, e.clientY);
      this.app.createLayerFromDrag(d.shape, { x: pt.x, y: pt.y, w: 0, h: 0 });
    } else if (d.kind === 'region' && d.rect && d.rect.w > 4 && d.rect.h > 4) {
      if (this.mode === 'cutout') this.app.extractRegion(d.rect);
      else this.app.eraseRegion(d.rect);
    } else if (d.kind === 'move' && d.moved) {
      this.app.history.push(this.doc, '移动');
    } else if (d.kind === 'resize' || d.kind === 'rotate') {
      this.app.history.push(this.doc, d.kind === 'resize' ? '缩放' : '旋转');
    }
    this.app.requestRender();
    this.app.syncPropPanel();
  }

  /* ---------------- 缩放 / 吸附 ---------------- */

  applyResize(d, pt, keepRatio) {
    const l = d.layer;
    const b = d.box;
    const local = toLocal(l, pt.x, pt.y);
    let { x, y, w, h } = b;
    const right = b.x + b.w;
    const bottom = b.y + b.h;
    const hd = d.handle;

    if (hd.includes('w')) { x = local.x; w = right - x; }
    if (hd.includes('e')) { w = local.x - b.x; }
    if (hd.includes('n')) { y = local.y; h = bottom - y; }
    if (hd.includes('s')) { h = local.y - b.y; }

    if (keepRatio && w !== 0 && h !== 0) {
      const target = d.ratio;
      if (Math.abs(w / h) > target) w = Math.sign(w) * Math.abs(h * target);
      else h = Math.sign(h) * Math.abs(w / target);
      if (hd.includes('w')) x = right - w;
      if (hd.includes('n')) y = bottom - h;
    }

    const min = 2;
    if (Math.abs(w) < min) w = Math.sign(w || 1) * min;
    if (Math.abs(h) < min) h = Math.sign(h || 1) * min;
    if (w < 0) { x += w; w = -w; }
    if (h < 0) { y += h; h = -h; }

    l.x = x; l.y = y; l.w = w; l.h = h;
    markDirty(l);
  }

  computeSnap(items, dx, dy) {
    const result = { dx: 0, dy: 0, guides: [] };
    if (!items.length) return result;
    const tol = SNAP_PX / this.view.scale;
    const moving = items.map((it) => ({ x: it.x + dx, y: it.y + dy,
                                        w: it.layer.w, h: it.layer.h }));
    let bx0 = Infinity, by0 = Infinity, bx1 = -Infinity, by1 = -Infinity;
    for (const m of moving) {
      bx0 = Math.min(bx0, m.x); by0 = Math.min(by0, m.y);
      bx1 = Math.max(bx1, m.x + m.w); by1 = Math.max(by1, m.y + m.h);
    }
    const movingIds = new Set(items.map((it) => it.layer.id));

    const targetsX = [0, this.doc.width / 2, this.doc.width];
    const targetsY = [0, this.doc.height / 2, this.doc.height];
    for (const l of this.doc.layers) {
      if (movingIds.has(l.id) || !l.visible) continue;
      targetsX.push(l.x, l.x + l.w / 2, l.x + l.w);
      targetsY.push(l.y, l.y + l.h / 2, l.y + l.h);
    }

    const edgesX = [bx0, (bx0 + bx1) / 2, bx1];
    const edgesY = [by0, (by0 + by1) / 2, by1];
    let bestX = null, bestY = null;
    for (const e of edgesX) {
      for (const t of targetsX) {
        const diff = t - e;
        if (Math.abs(diff) <= tol && (!bestX || Math.abs(diff) < Math.abs(bestX.diff))) {
          bestX = { diff, pos: t };
        }
      }
    }
    for (const e of edgesY) {
      for (const t of targetsY) {
        const diff = t - e;
        if (Math.abs(diff) <= tol && (!bestY || Math.abs(diff) < Math.abs(bestY.diff))) {
          bestY = { diff, pos: t };
        }
      }
    }
    if (bestX) {
      result.dx = bestX.diff;
      result.guides.push({ axis: 'x', pos: bestX.pos, from: 0, to: this.doc.height });
    }
    if (bestY) {
      result.dy = bestY.diff;
      result.guides.push({ axis: 'y', pos: bestY.pos, from: 0, to: this.doc.width });
    }
    return result;
  }

  updateCursor(pt) {
    const el = this.stage;
    if (this.spacePan) { el.style.cursor = 'grab'; return; }
    if (this.mode === 'cutout' || this.mode === 'erase') { el.style.cursor = 'crosshair'; return; }
    if (this.mode !== 'select') { el.style.cursor = 'crosshair'; return; }
    const handle = this.hitHandle(pt);
    if (handle === 'rot') { el.style.cursor = 'grab'; return; }
    if (handle) {
      const map = { n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize',
                    nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize' };
      el.style.cursor = map[handle] || 'default';
      return;
    }
    el.style.cursor = this.hover ? 'move' : 'default';
  }

  /* ---------------- 内联文字编辑 ---------------- */

  onDblClick(e) {
    const pt = this.toDoc(e.clientX, e.clientY);
    const hit = this.hitTest(pt);
    if (hit && hit.type === 'text') this.beginEdit(hit);
  }

  beginEdit(layer) {
    this.commitEdit();
    this.select([layer]);
    const ta = this.app.el.inlineEditor;
    const ink = layer.inkBox || [layer.x, layer.y, layer.w, layer.h];
    const sx = layer.ow > 0 ? layer.w / layer.ow : 1;
    const sy = layer.oh > 0 ? layer.h / layer.oh : 1;
    const left = layer.x + (ink[0] - layer.ox) * sx;
    const top = layer.y + (ink[1] - layer.oy) * sy;
    const p = this.toScreen(left, top);
    const size = layer.fontSize * sy * this.view.scale;

    ta.value = layer.text ?? '';
    ta.style.display = 'block';
    ta.style.left = `${p.x - 3}px`;
    ta.style.top = `${p.y - size * 0.22}px`;
    ta.style.width = `${Math.max(60, ink[2] * sx * this.view.scale + 24)}px`;
    ta.style.height = `${Math.max(size * 1.5, size * (layer.text || '').split('\n').length * 1.35)}px`;
    ta.style.font = fontString(layer, Math.max(9, size));
    ta.style.color = layer.color;
    ta.style.letterSpacing = `${(layer.letterSpacing || 0) * sy * this.view.scale}px`;
    ta.style.textAlign = layer.align || 'left';
    this.editing = { layer, original: layer.text };
    requestAnimationFrame(() => { ta.focus(); ta.select(); });

    ta.oninput = () => {
      layer.text = ta.value;
      layer.textMode = 'vector';
      layer.autoFit = false;
      markDirty(layer);
      ta.style.height = `${Math.max(size * 1.5, ta.scrollHeight)}px`;
      this.app.requestRender();
    };
    ta.onkeydown = (ev) => {
      if (ev.key === 'Escape') {
        layer.text = this.editing.original;
        this.app.requestRender();
        this.commitEdit();
        ev.preventDefault();
      } else if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) {
        this.commitEdit();
        ev.preventDefault();
      }
      ev.stopPropagation();
    };
    ta.onblur = () => this.commitEdit();
  }

  commitEdit() {
    if (!this.editing) return;
    const { layer, original } = this.editing;
    this.editing = null;
    const ta = this.app.el.inlineEditor;
    ta.style.display = 'none';
    ta.oninput = ta.onkeydown = ta.onblur = null;
    if (layer.text !== original) {
      this.app.history.push(this.doc, '改文字');
      this.app.refreshLayerPanel();
    }
    this.app.syncPropPanel();
  }

  /* ---------------- 键盘 ---------------- */

  onKeyDown(e) {
    if (this.editing) return;
    const tag = document.activeElement?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    const meta = e.metaKey || e.ctrlKey;

    if (e.code === 'Space' && !this.spacePan) {
      this.spacePan = true;
      this.app.el.stageWrap.classList.add('space-pan');
      this.stage.style.cursor = 'grab';
      e.preventDefault();
      return;
    }

    if (meta && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      if (e.shiftKey) this.app.redo(); else this.app.undo();
      return;
    }
    if (meta && e.key.toLowerCase() === 'a') { e.preventDefault(); this.selectAll(); return; }
    if (meta && e.key.toLowerCase() === 'd') { e.preventDefault(); this.app.duplicateSelection(); return; }
    if (meta && e.key.toLowerCase() === 's') { e.preventDefault(); this.app.openExport(); return; }
    if (meta && e.key === '0') { e.preventDefault(); this.fit(); this.app.updateZoomLabel(); return; }
    if (meta && (e.key === '=' || e.key === '+')) { e.preventDefault(); this.zoomTo(this.view.scale * 1.25); return; }
    if (meta && e.key === '-') { e.preventDefault(); this.zoomTo(this.view.scale / 1.25); return; }

    if (e.key === 'Delete' || e.key === 'Backspace') {
      if (this.selected.length) { e.preventDefault(); this.app.deleteSelection(); }
      return;
    }
    if (e.key === 'Escape') { this.select([]); this.app.setMode('select'); return; }
    if (e.key === 'Enter' && this.selected.length === 1 && this.selected[0].type === 'text') {
      e.preventDefault(); this.beginEdit(this.selected[0]); return;
    }

    if (!meta) {
      const keyMap = { v: 'select', t: 'text', r: 'rect', o: 'ellipse', c: 'cutout', e: 'erase' };
      const mode = keyMap[e.key.toLowerCase()];
      if (mode) { this.app.setMode(mode); return; }
    }

    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[e.key] && this.selected.length) {
      e.preventDefault();
      const step = e.shiftKey ? 10 : 1;
      const [dx, dy] = arrows[e.key];
      for (const l of this.selected) {
        if (l.locked) continue;
        l.x += dx * step; l.y += dy * step;
        markDirty(l);
      }
      this.app.requestRender();
      this.app.syncPropPanel();
      clearTimeout(this._nudgeTimer);
      this._nudgeTimer = setTimeout(() => this.app.history.push(this.doc, '微移'), 420);
    }
  }

  onKeyUp(e) {
    if (e.code === 'Space') {
      this.spacePan = false;
      this.app.el.stageWrap.classList.remove('space-pan');
      this.stage.style.cursor = 'default';
    }
  }
}

function normRect(a, b) {
  return {
    x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
    w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y),
  };
}
