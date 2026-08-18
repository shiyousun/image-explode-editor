/**
 * 应用入口：串联上传 → 炸开 → 编辑 → 导出。
 */

import {
  Doc, History, clamp, layerFromLayout, makeShapeLayer, makeTextLayer,
  markDirty, pickSerial, uid,
} from './state.js';
import {
  buildDrawSet, eraseKey, eraseRects, fidelityStats,
  renderDoc, renderOverlay, renderToCanvas,
} from './render.js';
import { CanvasController } from './interact.js';
import { LayerPanel, PropPanel } from './panels.js';
import { calibrateFonts } from './fontmatch.js';

const $ = (id) => document.getElementById(id);

/**
 * 是否隔着网络访问（部署到服务器后就是常态）。
 *
 * 「同时保存到项目目录」写的是**服务端**磁盘：本机自用时那就是项目根目录，很方便；
 * 但远程访问时那个文件躺在服务器上，用的人在自己电脑里根本找不到，还会把成品越堆越多。
 * 所以远程一律只走浏览器下载 —— 上传从本机选图，下载回本机「下载」文件夹，两头都对着本地硬盘。
 */
const IS_REMOTE = !['localhost', '127.0.0.1', '::1', '[::1]', ''].includes(location.hostname);

class App {
  constructor() {
    this.el = {
      stage: $('stage'), overlay: $('overlay'), stageWrap: $('stageWrap'),
      stageScroll: $('stageScroll'), inlineEditor: $('inlineEditor'),
      dropzone: $('dropzone'), fileInput: $('fileInput'),
      layerList: $('layerList'), layerCount: $('layerCount'),
      layerSearch: $('layerSearch'), layerFilter: $('layerFilter'),
      propBody: $('propBody'), selInfo: $('selInfo'),
      zoomLabel: $('zoomLabel'), statusHint: $('statusHint'), canvasMeta: $('canvasMeta'),
      engineInfo: $('engineInfo'), toastHost: $('toastHost'),
      busy: $('busy'), busyText: $('busyText'),
    };
    this.doc = new Doc();
    this.history = new History();
    this.canvas = new CanvasController(this);
    this.layerPanel = new LayerPanel(this);
    this.propPanel = new PropPanel(this);
    this.showOriginal = false;
    this.eyedropper = null;
    this.renderPending = false;
    this.originalName = 'image';

    this.history.onChange(() => {
      $('btnUndo').disabled = !this.history.canUndo;
      $('btnRedo').disabled = !this.history.canRedo;
    });

    this.bindUI();
    this.resizeCanvas();
    window.addEventListener('resize', () => { this.resizeCanvas(); this.requestRender(); });
    this.checkHealth();
  }

  /* ---------------- 基础 UI ---------------- */

  toast(msg, kind = '') {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.textContent = msg;
    this.el.toastHost.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .3s, transform .3s';
      el.style.opacity = '0';
      el.style.transform = 'translateY(6px)';
      setTimeout(() => el.remove(), 320);
    }, kind === 'err' ? 4200 : 2400);
  }

  busy(on, text = '处理中…') {
    this.el.busyText.textContent = text;
    this.el.busy.hidden = !on;
  }

  async checkHealth() {
    try {
      const r = await fetch('/api/health').then((x) => x.json());
      const names = { 'macos-vision': 'macOS Vision', rapidocr: 'RapidOCR', tesseract: 'Tesseract' };
      const list = (r.ocrEngines || []).map((n) => names[n] || n);
      this.el.engineInfo.textContent = list.length
        ? `OCR：${list.join(' / ')}` : 'OCR 引擎未就绪';
    } catch {
      this.el.engineInfo.textContent = '后端未连接';
    }
  }

  bindUI() {
    const el = this.el;

    $('btnPick').onclick = () => el.fileInput.click();
    $('btnOpen').onclick = () => el.fileInput.click();
    el.fileInput.onchange = () => {
      const f = el.fileInput.files?.[0];
      if (f) this.upload(f);
      el.fileInput.value = '';
    };
    $('btnSample').onclick = () => this.useSample();

    ['dragenter', 'dragover'].forEach((ev) => {
      el.stageWrap.addEventListener(ev, (e) => {
        e.preventDefault();
        el.dropzone.classList.add('hot');
      });
    });
    ['dragleave', 'drop'].forEach((ev) => {
      el.stageWrap.addEventListener(ev, (e) => {
        e.preventDefault();
        el.dropzone.classList.remove('hot');
      });
    });
    el.stageWrap.addEventListener('drop', (e) => {
      const f = e.dataTransfer?.files?.[0];
      if (f) this.upload(f);
    });

    document.querySelectorAll('[data-mode]').forEach((btn) => {
      btn.onclick = () => this.setMode(btn.dataset.mode);
    });
    this.setMode('select');

    $('btnUndo').onclick = () => this.undo();
    $('btnRedo').onclick = () => this.redo();
    $('btnReexplode').onclick = () => this.openExplodeModal();

    $('btnLayerUp').onclick = () => this.moveSelection(1);
    $('btnLayerDown').onclick = () => this.moveSelection(-1);
    $('btnLayerDelete').onclick = () => this.deleteSelection();

    $('btnZoomIn').onclick = () => this.canvas.zoomTo(this.canvas.view.scale * 1.25);
    $('btnZoomOut').onclick = () => this.canvas.zoomTo(this.canvas.view.scale / 1.25);
    $('btnZoomFit').onclick = () => { this.canvas.fit(); this.updateZoomLabel(); };
    $('btnZoom100').onclick = () => { this.canvas.zoomTo(1); };

    const cmp = $('btnCompare');
    const setCompare = (on) => {
      if (!this.doc.isReady) return;
      this.showOriginal = on;
      cmp.classList.toggle('active', on);
      this.requestRender();
    };
    cmp.addEventListener('pointerdown', () => setCompare(true));
    window.addEventListener('pointerup', () => setCompare(false));
    cmp.addEventListener('pointerleave', () => setCompare(false));

    $('btnExport').onclick = () => this.openExport();
    $('btnSaveProject').onclick = () => this.saveProject();

    // 炸开参数弹窗
    $('btnExplodeCancel').onclick = () => { $('explodeModal').hidden = true; };
    $('btnExplodeRun').onclick = () => {
      $('explodeModal').hidden = true;
      this.explode();
    };

    // 导出弹窗
    $('expFormat').onchange = (e) => {
      $('expQualityRow').hidden = e.target.value === 'png';
    };
    $('expQuality').oninput = (e) => { $('expQualityOut').textContent = e.target.value; };
    $('btnExportCancel').onclick = () => { $('exportModal').hidden = true; };
    $('btnExportRun').onclick = () => this.runExport();

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        $('explodeModal').hidden = true;
        $('exportModal').hidden = true;
      }
    });
  }

  setMode(mode) {
    this.canvas.mode = mode;
    document.querySelectorAll('[data-mode]').forEach((b) => {
      b.classList.toggle('active', b.dataset.mode === mode);
    });
    const hints = {
      select: '拖拽移动 · 双击文字改内容 · ⌘滚轮缩放 · 空格拖拽平移',
      text: '在画布上拖出一个框来放置新文字',
      rect: '在画布上拖出矩形',
      ellipse: '在画布上拖出椭圆',
      cutout: '框住任意区域，把它抠成一个独立图层（自动去背）',
      erase: '框住要擦掉的区域，会按周围背景补干净',
    };
    this.el.statusHint.textContent = hints[mode] || '';
    this.canvas.updateCursor({ x: -1, y: -1 });
  }

  /* ---------------- 画布尺寸与渲染 ---------------- */

  resizeCanvas() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    for (const canvas of [this.el.stage, this.el.overlay]) {
      const w = this.el.stageScroll.clientWidth;
      const h = this.el.stageScroll.clientHeight;
      canvas.width = Math.max(1, Math.round(w * dpr));
      canvas.height = Math.max(1, Math.round(h * dpr));
    }
    this.dpr = dpr;
  }

  requestRender() {
    if (this.renderPending) return;
    this.renderPending = true;
    requestAnimationFrame(() => {
      this.renderPending = false;
      this.render();
    });
  }

  render() {
    const { stage, overlay } = this.el;
    const ctx = stage.getContext('2d');
    const octx = overlay.getContext('2d');
    const { scale, ox, oy } = this.canvas.view;
    const dpr = this.dpr || 1;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, stage.width, stage.height);
    octx.setTransform(1, 0, 0, 1, 0, 0);
    octx.clearRect(0, 0, overlay.width, overlay.height);

    if (!this.doc.isReady) return;

    const t = (c) => c.setTransform(scale * dpr, 0, 0, scale * dpr, ox * dpr, oy * dpr);

    // 画布边界与透明棋盘
    t(ctx);
    ctx.save();
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, this.doc.width, this.doc.height);
    ctx.restore();

    renderDoc(ctx, this.doc, { showOriginal: this.showOriginal });

    t(octx);
    octx.save();
    octx.strokeStyle = 'rgba(255,255,255,.16)';
    octx.lineWidth = 1 / scale;
    octx.strokeRect(0, 0, this.doc.width, this.doc.height);
    octx.restore();

    if (!this.showOriginal) {
      renderOverlay(octx, this.doc, {
        selected: this.canvas.selected,
        hover: this.canvas.hover,
        scale,
        guides: this.canvas.guides,
        marquee: this.canvas.marquee,
      });
    }

    this.updateMeta();
    this.ensureActiveClean();
  }

  /**
   * 让后端现算一张「只擦掉当前被改动元素」的底图。
   *
   * 预生成的 clean_all 把所有元素一起擦了，用它补一个压在面板上的图标，补出来的是面板
   * 外面的页面背景，面板会破一个洞；而只擦这一个图标时，缺口由四周的面板像素补上，看不
   * 出痕迹，也就不必再重画面板和面板上的其它东西。请求按需发起并防抖，拿到之前先用预
   * 生成的底图顶着，所以拖动过程始终是流畅的。
   */
  ensureActiveClean() {
    const doc = this.doc;
    if (!doc.isReady || !doc.jobId) return;
    const key = eraseKey(doc);
    if (!key) {
      doc.activeCleanKey = null;
      doc.activeCleanImg = null;
      return;
    }
    if (key === doc.activeCleanKey || key === this.cleanPending) return;
    clearTimeout(this.cleanTimer);
    this.cleanTimer = setTimeout(() => this.fetchActiveClean(key), 260);
  }

  async fetchActiveClean(key) {
    const doc = this.doc;
    if (key !== eraseKey(doc)) return;      // 等待期间又动了，等下一轮
    this.cleanPending = key;
    try {
      const res = await fetch('/api/erase', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jobId: doc.jobId, rects: eraseRects(doc) }),
      });
      const data = await this.parse(res);
      const img = await loadImage(`/files/${doc.jobId}/${data.clean}`);
      if (this.doc !== doc || key !== eraseKey(doc)) return;
      doc.activeCleanImg = img;
      doc.activeCleanKey = key;
      this.requestRender();
    } catch {
      /* 静默降级：继续用预生成的干净底图 */
    } finally {
      if (this.cleanPending === key) this.cleanPending = null;
    }
  }

  updateZoomLabel() {
    this.el.zoomLabel.textContent = `${Math.round(this.canvas.view.scale * 100)}%`;
  }

  updateCoords(pt) {
    if (!this.doc.isReady) return;
    this.el.canvasMeta.textContent =
      `${Math.round(pt.x)}, ${Math.round(pt.y)}`;
  }

  updateMeta() {
    const f = this.fidelity();
    const suffix = f.edited
      ? `已改 ${f.edited} 层 · 重绘 ${f.redrawn} 层`
      : '与原图一致';
    this.el.canvasMeta.textContent = `${this.doc.width}×${this.doc.height} · ${suffix}`;
  }

  fidelity() { return fidelityStats(this.doc); }

  /* ---------------- 上传与炸开 ---------------- */

  async upload(file) {
    this.busy(true, '上传中…');
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await fetch('/api/upload', { method: 'POST', body: form });
      const data = await this.parse(res);
      this.originalName = data.originalName || 'image';
      this.pendingJob = data;
      this.busy(false);
      this.openExplodeModal(true);
    } catch (err) {
      this.busy(false);
      this.toast(`上传失败：${err.message}`, 'err');
    }
  }

  async useSample() {
    this.busy(true, '准备示例图…');
    try {
      const list = await fetch('/api/samples').then((r) => r.json());
      const first = list.samples?.[0];
      if (!first) throw new Error('没有可用的示例图');
      const res = await fetch('/api/use-sample', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: first.name }),
      });
      const data = await this.parse(res);
      this.originalName = data.originalName || 'sample';
      this.pendingJob = data;
      this.busy(false);
      this.explode();
    } catch (err) {
      this.busy(false);
      this.toast(`加载示例失败：${err.message}`, 'err');
    }
  }

  openExplodeModal(isNew = false) {
    if (!isNew && !this.doc.jobId && !this.pendingJob) {
      this.toast('先上传一张图片', 'err');
      return;
    }
    $('explodeModal').hidden = false;
  }

  async explode() {
    const jobId = this.pendingJob?.jobId || this.doc.jobId;
    if (!jobId) { this.toast('没有可炸开的图片', 'err'); return; }

    this.busy(true, '正在炸开：识别文字、分割元素、修补背景…');
    try {
      const options = {
        strength: $('optStrength').value,
        ocrEngine: $('optEngine').value,
        detectText: $('optText').checked,
        detectShapes: $('optShapes').checked,
        detectImages: $('optImages').checked,
        maxSide: Number($('optMaxSide').value),
      };
      const res = await fetch('/api/explode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jobId, options }),
      });
      const layout = await this.parse(res);
      await this.loadLayout(layout);
      this.pendingJob = null;
      this.busy(false);
      const s = layout.stats || {};
      this.toast(`炸开完成：${s.textLayers || 0} 段文字 + ${s.elementLayers || 0} 个元素，用时 ${s.elapsed}s`, 'ok');
    } catch (err) {
      this.busy(false);
      this.toast(`炸开失败：${err.message}`, 'err');
    }
  }

  async loadLayout(layout) {
    const doc = new Doc();
    doc.jobId = layout.jobId;
    doc.width = layout.width;
    doc.height = layout.height;
    doc.stats = layout.stats;
    doc.name = this.originalName;
    doc.layers = layout.layers.map((raw) => layerFromLayout(raw, layout.jobId));

    const base = `/files/${layout.jobId}`;
    const [baseImg, cleanText, cleanAll] = await Promise.all([
      loadImage(`${base}/${layout.assets.base}`),
      loadImage(`${base}/${layout.assets.cleanText}`),
      loadImage(`${base}/${layout.assets.cleanAll}`),
    ]);
    doc.baseImg = baseImg;
    doc.cleanTextImg = cleanText;
    doc.cleanAllImg = cleanAll;

    // 切片并行加载，个别失败不影响整体（该图层退回矢量绘制）
    await Promise.all(doc.layers.map(async (l) => {
      if (!l.sliceUrl) return;
      try {
        doc.images.set(l.sliceUrl, await loadImage(l.sliceUrl));
        if (l.sliceCleanUrl) {
          doc.images.set(l.sliceCleanUrl, await loadImage(l.sliceCleanUrl));
        }
      } catch {
        l.textMode = 'vector';
        l.shapeMode = 'vector';
      }
    }));

    // 字体标定必须在建立历史快照之前完成，否则撤销回初始状态会把字体退回未标定
    await this.calibrate(doc);

    this.doc = doc;
    this.canvas.selected = [];
    this.canvas.hover = null;
    this.history.reset();
    this.history.attach(doc);
    this.history.push(doc, '炸开');

    this.el.dropzone.classList.add('hide');
    $('btnExport').disabled = false;
    $('btnReexplode').disabled = false;
    $('btnSaveProject').disabled = false;

    this.canvas.fit();
    this.updateZoomLabel();
    this.refreshLayerPanel();
    this.propPanel.render();
    this.requestRender();
  }

  /**
   * 字体标定：逐层把候选系统字体渲染出来和原图笔画比对，定下字体与字重。
   * 只写 fontFamily/fontWeight，不动 dirty，所以未编辑图层依旧从原图像素还原，保真不受影响。
   */
  async calibrate(doc) {
    const t0 = performance.now();
    try {
      this.busy(true, '正在识别字体…');
      const r = await calibrateFonts(doc, (p) => {
        this.el.busyText.textContent = `正在识别字体… ${Math.round(p * 100)}%`;
      });
      doc.fontReport = { ...r, ms: Math.round(performance.now() - t0) };
      const primary = Object.values(r.families || {})
        .map((g) => r.labelOf?.(g.primary) || g.primary);
      if (r.matched) {
        this.toast(`字体识别完成：${primary.join(' / ') || '—'}（${r.matched} 层，${
          doc.fontReport.ms}ms）`);
      }
    } catch (err) {
      console.warn('字体标定失败，退回默认字体', err);
    } finally {
      this.busy(false);
    }
  }

  /* ---------------- 图层操作 ---------------- */

  onSelectionChange() {
    this.layerPanel.render();
    this.propPanel.render();
    this.requestRender();
  }

  refreshLayerPanel() {
    this.layerPanel.render();
    this.propPanel.render();
  }

  syncPropPanel() { this.propPanel.sync(); }

  undo() {
    if (this.history.undo(this.doc)) {
      this.canvas.selected = this.canvas.selected
        .map((l) => this.doc.layerById(l.id)).filter(Boolean);
      this.refreshLayerPanel();
      this.requestRender();
    }
  }

  redo() {
    if (this.history.redo(this.doc)) {
      this.canvas.selected = this.canvas.selected
        .map((l) => this.doc.layerById(l.id)).filter(Boolean);
      this.refreshLayerPanel();
      this.requestRender();
    }
  }

  deleteSelection() {
    const sel = this.canvas.selected.filter((l) => !l.locked);
    if (!sel.length) return;
    const ids = new Set(sel.map((l) => l.id));
    // 源自原图的图层不真正移除，而是隐藏：这样它的原始位置仍会被背景补掉，
    // 也还能随时恢复回来。
    const removed = [];
    for (const l of sel) {
      if (l.fromSource) { l.visible = false; markDirty(l); }
      else removed.push(l.id);
    }
    if (removed.length) {
      this.doc.layers = this.doc.layers.filter((l) => !removed.includes(l.id));
    }
    this.canvas.selected = [];
    this.history.push(this.doc, '删除');
    this.refreshLayerPanel();
    this.requestRender();
    this.toast(`已移除 ${ids.size} 个图层${removed.length < ids.size ? '（原图图层转为隐藏，可在图层面板恢复）' : ''}`);
  }

  duplicateSelection() {
    const sel = this.canvas.selected;
    if (!sel.length) return;
    const copies = sel.map((l) => {
      const c = { ...pickSerial(l) };
      c.id = uid(l.type[0]);
      c.name = `${l.name} 副本`;
      c.x = l.x + 14; c.y = l.y + 14;
      c.ox = c.x; c.oy = c.y; c.ow = l.w; c.oh = l.h;
      c.dirty = true;
      c.fromSource = false;      // 副本不在原图里，必须自己绘制
      c.sliceUrl = l.sliceUrl;
      c.srcUrl = l.srcUrl;
      c.inkBox = l.inkBox
        ? [l.inkBox[0] + (c.x - l.ox), l.inkBox[1] + (c.y - l.oy), l.inkBox[2], l.inkBox[3]]
        : undefined;
      return c;
    });
    this.doc.layers.push(...copies);
    this.canvas.select(copies);
    this.history.push(this.doc, '复制');
    this.refreshLayerPanel();
    this.requestRender();
  }

  moveSelection(dir) {
    const sel = this.canvas.selected;
    if (sel.length !== 1) return;
    const layers = this.doc.layers;
    const i = layers.indexOf(sel[0]);
    const j = clamp(i + dir, 0, layers.length - 1);
    if (i === j) return;
    layers.splice(j, 0, layers.splice(i, 1)[0]);
    sel.forEach(markDirty);
    this.history.push(this.doc, '调整层级');
    this.refreshLayerPanel();
    this.requestRender();
  }

  reorderLayer(dragId, targetId, above) {
    const layers = this.doc.layers;
    const from = layers.findIndex((l) => l.id === dragId);
    const to = layers.findIndex((l) => l.id === targetId);
    if (from < 0 || to < 0 || from === to) return;
    const [moved] = layers.splice(from, 1);
    // 面板是倒序展示的：视觉上"放在上面"意味着 z 序更高
    let insert = layers.findIndex((l) => l.id === targetId);
    if (above) insert += 1;
    layers.splice(clamp(insert, 0, layers.length), 0, moved);
    markDirty(moved);
    this.history.push(this.doc, '调整层级');
    this.refreshLayerPanel();
    this.requestRender();
  }

  revertLayer(l) {
    l.x = l.ox; l.y = l.oy; l.w = l.ow; l.h = l.oh;
    l.rotation = 0; l.opacity = 1; l.visible = true;
    if (l.type === 'text') { l.textMode = 'pixel'; l.autoFit = true; }
    if (l.type === 'shape') l.shapeMode = 'pixel';
    if (l.type === 'image') {
      l.srcUrl = null;
      l.filters = { brightness: 100, contrast: 100, saturate: 100, blur: 0, grayscale: 0 };
    }
    if (this._originalSnapshot) {
      const snap = this._originalSnapshot.get(l.id);
      if (snap) Object.assign(l, snap);
    }
    l.dirty = !l.fromSource;
    this.history.push(this.doc, '还原图层');
    this.refreshLayerPanel();
    this.requestRender();
    this.toast('已还原为原图状态');
  }

  revertAll() {
    if (!this.doc.isReady) return;
    const first = this.history.past[0];
    if (!first) return;
    this.history.restore(this.doc, first);
    this.doc.layers = this.doc.layers.filter((l) => l.fromSource);
    this.canvas.selected = [];
    this.history.push(this.doc, '全部还原');
    this.refreshLayerPanel();
    this.requestRender();
    this.toast('已全部还原，当前与原图一致', 'ok');
  }

  createLayerFromDrag(kind, rect) {
    const doc = this.doc;
    if (!doc.isReady) return;
    let layer;
    if (kind === 'text') {
      const size = rect.h > 8 ? rect.h * 0.7 : 32;
      layer = makeTextLayer({ x: rect.x, y: rect.y, fontSize: size, isCJK: true });
      if (rect.w > 20) { layer.w = rect.w; layer.ow = rect.w; }
      layer.h = size * 1.4; layer.oh = layer.h;
      layer.inkBox = [rect.x, rect.y, layer.w, size];
      layer.baselineOffset = size;
    } else {
      const w = Math.max(12, rect.w || 140);
      const h = Math.max(12, rect.h || 90);
      layer = makeShapeLayer({
        x: rect.x, y: rect.y, w, h,
        shape: kind === 'ellipse' ? 'ellipse' : 'rect',
      });
    }
    doc.layers.push(layer);
    this.canvas.select([layer]);
    this.setMode('select');
    this.history.push(doc, '新建图层');
    this.refreshLayerPanel();
    this.requestRender();
    if (layer.type === 'text') this.canvas.beginEdit(layer);
  }

  async replaceImage(layer, file) {
    const url = URL.createObjectURL(file);
    try {
      const img = await loadImage(url);
      this.doc.images.set(url, img);
      layer.srcUrl = url;
      layer.name = `${file.name.slice(0, 16)}`;
      markDirty(layer);
      this.history.push(this.doc, '替换图片');
      this.refreshLayerPanel();
      this.requestRender();
      this.toast('图片已替换');
    } catch {
      this.toast('图片读取失败', 'err');
    }
  }

  /* ---------------- 抠取 / 擦除 ---------------- */

  async extractRegion(rect) {
    if (!this.doc.jobId) return;
    this.busy(true, '正在抠取该区域…');
    try {
      const res = await fetch('/api/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jobId: this.doc.jobId,
          rect: [rect.x, rect.y, rect.w, rect.h],
          mode: 'grabcut',
        }),
      });
      const raw = await this.parse(res);
      const layer = layerFromLayout(raw, this.doc.jobId);
      layer.fromSource = false;   // 新抠出来的是额外图层，原图里的那份仍在
      layer.dirty = true;
      if (layer.sliceUrl) {
        this.doc.images.set(layer.sliceUrl, await loadImage(layer.sliceUrl));
      }
      this.doc.layers.push(layer);
      this.canvas.select([layer]);
      this.setMode('select');
      this.history.push(this.doc, '抠取元素');
      this.refreshLayerPanel();
      this.requestRender();
      this.busy(false);
      this.toast('抠取完成，已作为新图层加到最上层', 'ok');
    } catch (err) {
      this.busy(false);
      this.toast(`抠取失败：${err.message}`, 'err');
    }
  }

  async eraseRegion(rect) {
    if (!this.doc.jobId) return;
    this.busy(true, '正在按背景补干净…');
    try {
      const res = await fetch('/api/erase', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jobId: this.doc.jobId,
          rects: [[rect.x, rect.y, rect.w, rect.h]],
        }),
      });
      const data = await this.parse(res);
      const img = await loadImage(`/files/${this.doc.jobId}/${data.clean}`);
      // 用一个「贴上修补结果」的图像图层来实现擦除，可撤销、可再挪动
      const url = `/files/${this.doc.jobId}/${data.clean}#${rect.x},${rect.y}`;
      const patch = document.createElement('canvas');
      patch.width = Math.max(1, Math.round(rect.w));
      patch.height = Math.max(1, Math.round(rect.h));
      patch.getContext('2d').drawImage(
        img, rect.x, rect.y, rect.w, rect.h, 0, 0, patch.width, patch.height);
      const dataUrl = patch.toDataURL('image/png');
      const patchImg = await loadImage(dataUrl);
      this.doc.images.set(url, patchImg);

      const layer = {
        id: uid('x'), type: 'image', name: '擦除补丁',
        visible: true, locked: false, opacity: 1, rotation: 0,
        x: rect.x, y: rect.y, w: rect.w, h: rect.h,
        ox: rect.x, oy: rect.y, ow: rect.w, oh: rect.h,
        dirty: true, fromSource: false, kind: 'erase',
        sliceUrl: url, srcUrl: null,
        filters: { brightness: 100, contrast: 100, saturate: 100, blur: 0, grayscale: 0 },
      };
      this.doc.layers.push(layer);
      this.canvas.select([layer]);
      this.setMode('select');
      this.history.push(this.doc, '擦除区域');
      this.refreshLayerPanel();
      this.requestRender();
      this.busy(false);
      this.toast('已擦除，结果是一个可撤销的补丁图层', 'ok');
    } catch (err) {
      this.busy(false);
      this.toast(`擦除失败：${err.message}`, 'err');
    }
  }

  startEyedropper(callback) {
    if (window.EyeDropper) {
      new window.EyeDropper().open()
        .then((r) => callback(r.sRGBHex))
        .catch(() => {});
      return;
    }
    this.toast('当前浏览器不支持取色器，请手动输入色值', 'err');
  }

  /* ---------------- 导出 ---------------- */

  openExport() {
    if (!this.doc.isReady) return;
    $('expName').value = `${this.doc.name || 'image'}_edited`;
    $('expToServerRow').hidden = IS_REMOTE;
    if (IS_REMOTE) $('expToServer').checked = false;
    $('expHint').innerHTML = IS_REMOTE
      ? `远程访问 <b>${location.host}</b>：图片直接下载到<b>你这台电脑</b>的「下载」文件夹，不写服务器磁盘。`
      : '图片会下载到本机「下载」文件夹。';
    $('exportModal').hidden = false;
  }

  async runExport() {
    $('exportModal').hidden = true;
    const format = $('expFormat').value;
    const scale = Number($('expScale').value) || 1;
    const quality = Number($('expQuality').value) / 100;
    const name = ($('expName').value || 'export').trim();
    const toServer = !IS_REMOTE && $('expToServer').checked;

    this.busy(true, '正在生成图片…');
    try {
      const bg = format === 'jpeg' ? '#ffffff' : null;
      const canvas = renderToCanvas(this.doc, scale, bg);
      const mime = { png: 'image/png', jpeg: 'image/jpeg', webp: 'image/webp' }[format];
      const quality2 = format === 'png' ? undefined : quality;
      const ext = { png: 'png', jpeg: 'jpg', webp: 'webp' }[format];
      const filename = `${name}.${ext}`;

      const blob = await canvasBlob(canvas, mime, quality2);
      downloadBlob(blob, filename);

      if (toServer) {
        const res = await fetch('/api/save-image', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dataUrl: canvas.toDataURL(mime, quality2), filename }),
        });
        const saved = await this.parse(res);
        this.toast(`已下载到本机，并存了一份到项目目录：${saved.name}`, 'ok');
      } else {
        this.toast(`已下载到本机：${filename}（${fmtSize(blob.size)}）`, 'ok');
      }
      this.busy(false);
    } catch (err) {
      this.busy(false);
      this.toast(`导出失败：${err.message}`, 'err');
    }
  }

  async saveProject() {
    if (!this.doc.jobId) return;
    const snapshot = this.doc.serialize();
    try {
      const res = await fetch('/api/save-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jobId: this.doc.jobId, doc: snapshot }),
      });
      await this.parse(res);
      if (IS_REMOTE) {
        // 服务器上的工程文件够用来「同一链接继续编辑」，但拿不回本机；顺手落一份到本地硬盘。
        downloadBlob(new Blob([JSON.stringify(snapshot)], { type: 'application/json' }),
                     `${this.doc.name || 'project'}_工程.json`);
        this.toast('工程已存到服务器，并下载了一份到本机', 'ok');
      } else {
        this.toast('工程已保存，可用同一链接继续编辑', 'ok');
      }
    } catch (err) {
      this.toast(`保存工程失败：${err.message}`, 'err');
    }
  }

  async parse(res) {
    if (!res.ok) {
      let detail = `${res.status}`;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch { /* 保留状态码 */ }
      throw new Error(detail);
    }
    return res.json();
  }
}

/**
 * 用 Blob 而不是 data: URL 触发下载。
 *
 * data: URL 要先把整张图 base64 化（体积涨 33%）再塞进 href，2× 导出的大图能顶到几十兆，
 * 部分浏览器对超长 URL 直接静默丢弃 —— 表现就是「点了下载没反应」。Blob 只递一个句柄，
 * 大小不进 URL，也顺带能报出真实字节数写进提示里。
 */
function canvasBlob(canvas, mime, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('浏览器生成图片失败'))),
                  mime, quality);
  });
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function fmtSize(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB`
                          : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`图片加载失败: ${src}`));
    img.src = src;
  });
}

window.app = new App();
