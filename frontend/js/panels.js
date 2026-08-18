/**
 * 左侧图层面板 + 右侧属性面板。
 */

import { markDirty, uid } from './state.js';
import { defaultFont, textWidthDrift } from './render.js';
import { fontGroups } from './fontlib.js';

const TYPE_LABEL = { text: '文字', shape: '形状', image: '图像' };

/**
 * 字体下拉：分组列出本机装了的字体，当前用的那个单独置顶。
 *
 * 自动识别出的字体名（如 "PingFang SC"）和候选表里的写法（带同族备选和通用兜底的整条
 * font-family 链）文本上并不相同，所以不能靠字符串去候选表里找位置——置顶一份最省事，
 * 也顺带让人一眼看到「现在用的是识别出来的哪个」。
 */
function fontOptions(l, current) {
  const esc = (s) => escapeHtml(s);
  const head = l.fontMatch
    ? `识别字体 · ${l.fontMatch.label}（吻合 ${l.fontMatch.iou}%）`
    : '当前字体';
  let html = `<optgroup label="${esc(head)}"><option value='${esc(current)}' selected>`
    + `${esc(current.replace(/"/g, '').split(',')[0])} ✓</option></optgroup>`;
  for (const g of fontGroups(!!l.isCJK)) {
    html += `<optgroup label="${esc(g.name)}">`
      + g.fonts.map((f) => `<option value='${esc(f.value)}' style="font-family:${
        esc(f.value)}">${esc(f.label)}</option>`).join('')
      + '</optgroup>';
  }
  return html;
}

/* --------------------------------------------------------------------- */
/* 图层面板                                                              */
/* --------------------------------------------------------------------- */

export class LayerPanel {
  constructor(app) {
    this.app = app;
    this.list = app.el.layerList;
    this.search = '';
    this.filter = 'all';
    this.thumbCache = new Map();

    app.el.layerSearch.addEventListener('input', (e) => {
      this.search = e.target.value.trim().toLowerCase();
      this.render();
    });
    app.el.layerFilter.addEventListener('change', (e) => {
      this.filter = e.target.value;
      this.render();
    });
  }

  visibleLayers() {
    return this.app.doc.layers.filter((l) => {
      if (this.filter !== 'all' && l.type !== this.filter) return false;
      if (!this.search) return true;
      const hay = `${l.name} ${l.text || ''}`.toLowerCase();
      return hay.includes(this.search);
    });
  }

  thumb(layer) {
    if (layer.type === 'shape' && layer.shapeMode === 'vector') {
      const svg = layer.shape === 'ellipse'
        ? `<circle cx='15' cy='15' r='13' fill='${layer.fill}'/>`
        : `<rect x='2' y='4' width='26' height='22' rx='${Math.min(8, layer.radius || 0)}' fill='${layer.fill}'/>`;
      return `data:image/svg+xml,${encodeURIComponent(
        `<svg xmlns='http://www.w3.org/2000/svg' width='30' height='30'>${svg}</svg>`)}`;
    }
    return layer.srcUrl || layer.sliceUrl || '';
  }

  render() {
    const doc = this.app.doc;
    const sel = new Set(this.app.canvas.selected.map((l) => l.id));
    const layers = this.visibleLayers();
    this.app.el.layerCount.textContent = String(doc.layers.length);

    if (!layers.length) {
      this.list.innerHTML = `<div class="empty-prop" style="padding:14px 10px">${
        doc.layers.length ? '没有匹配的图层' : '还没有图层，先上传一张图片'
      }</div>`;
      return;
    }

    const frag = document.createDocumentFragment();
    // 顶层显示在最上面，符合设计工具的习惯
    for (let i = layers.length - 1; i >= 0; i -= 1) {
      const l = layers[i];
      const row = document.createElement('div');
      row.className = 'layer-item';
      if (sel.has(l.id)) row.classList.add('selected');
      if (!l.visible) row.classList.add('hidden-layer');
      row.dataset.id = l.id;
      row.draggable = true;

      const src = this.thumb(l);
      const meta = l.type === 'text'
        ? `${Math.round(l.fontSize)}px · ${l.fontWeight}`
        : (l.type === 'shape' ? (l.fill || '形状')
          : `${Math.round(l.w)}×${Math.round(l.h)}`);

      row.innerHTML = `
        ${src ? `<img class="li-thumb" src="${src}" alt="">`
              : '<div class="li-thumb"></div>'}
        <div class="li-body">
          <div class="li-name">${escapeHtml(l.name || '图层')}</div>
          <div class="li-meta">
            <span class="li-tag ${l.type}">${TYPE_LABEL[l.type] || l.type}</span>
            <span>${escapeHtml(meta)}</span>
            ${(l.dirty || !l.fromSource) ? '<span class="li-dot" title="已编辑，导出时会重新绘制"></span>' : ''}
          </div>
        </div>
        <div class="li-actions">
          <button class="li-btn ${l.visible ? 'on' : ''}" data-act="vis" title="显示/隐藏">${l.visible ? '◉' : '○'}</button>
          <button class="li-btn ${l.locked ? 'on' : ''}" data-act="lock" title="锁定">${l.locked ? '🔒' : '🔓'}</button>
        </div>`;
      frag.appendChild(row);
    }
    this.list.innerHTML = '';
    this.list.appendChild(frag);
    this.bindRows();
  }

  bindRows() {
    const app = this.app;
    this.list.querySelectorAll('.layer-item').forEach((row) => {
      const layer = app.doc.layerById(row.dataset.id);
      if (!layer) return;

      row.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-act]');
        if (btn) {
          e.stopPropagation();
          if (btn.dataset.act === 'vis') {
            layer.visible = !layer.visible;
            markDirty(layer);
          } else {
            layer.locked = !layer.locked;
          }
          app.history.push(app.doc, '图层属性');
          app.requestRender();
          this.render();
          return;
        }
        app.canvas.select([layer], e.shiftKey || e.metaKey);
      });

      row.addEventListener('dblclick', (e) => {
        if (e.target.closest('[data-act]')) return;
        if (layer.type === 'text') app.canvas.beginEdit(layer);
      });

      row.addEventListener('dragstart', (e) => {
        row.classList.add('dragging');
        e.dataTransfer.setData('text/plain', layer.id);
        e.dataTransfer.effectAllowed = 'move';
      });
      row.addEventListener('dragend', () => {
        row.classList.remove('dragging');
        this.list.querySelectorAll('.layer-item').forEach(
          (r) => r.classList.remove('drop-above', 'drop-below'));
      });
      row.addEventListener('dragover', (e) => {
        e.preventDefault();
        const rect = row.getBoundingClientRect();
        const above = e.clientY < rect.top + rect.height / 2;
        row.classList.toggle('drop-above', above);
        row.classList.toggle('drop-below', !above);
      });
      row.addEventListener('dragleave', () => {
        row.classList.remove('drop-above', 'drop-below');
      });
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        const dragId = e.dataTransfer.getData('text/plain');
        const rect = row.getBoundingClientRect();
        const above = e.clientY < rect.top + rect.height / 2;
        app.reorderLayer(dragId, layer.id, above);
      });
    });
  }
}

/* --------------------------------------------------------------------- */
/* 属性面板                                                              */
/* --------------------------------------------------------------------- */

export class PropPanel {
  constructor(app) {
    this.app = app;
    this.body = app.el.propBody;
    this.measureCtx = document.createElement('canvas').getContext('2d');
  }

  render() {
    const sel = this.app.canvas.selected;
    this.app.el.selInfo.textContent = sel.length
      ? (sel.length === 1 ? (TYPE_LABEL[sel[0].type] || sel[0].type) : `${sel.length} 个图层`)
      : '未选中';

    if (!sel.length) {
      this.body.innerHTML = this.docSection();
      this.bindDoc();
      return;
    }
    if (sel.length > 1) {
      this.body.innerHTML = this.multiSection(sel);
      this.bindMulti(sel);
      return;
    }

    const l = sel[0];
    let html = this.commonSection(l);
    if (l.type === 'text') html += this.textSection(l);
    else if (l.type === 'shape') html += this.shapeSection(l);
    else html += this.imageSection(l);
    html += this.fidelitySection(l);
    this.body.innerHTML = html;
    this.bindCommon(l);
    if (l.type === 'text') this.bindText(l);
    else if (l.type === 'shape') this.bindShape(l);
    else this.bindImage(l);
    this.bindFidelity(l);
  }

  /** 只更新数值，不重建 DOM —— 拖拽过程中调用 */
  sync() {
    const sel = this.app.canvas.selected;
    if (sel.length !== 1) return;
    const l = sel[0];
    const set = (id, v) => {
      const el = this.body.querySelector(`#${id}`);
      if (el && document.activeElement !== el) el.value = v;
    };
    set('pX', Math.round(l.x));
    set('pY', Math.round(l.y));
    set('pW', Math.round(l.w));
    set('pH', Math.round(l.h));
    set('pRot', Math.round((l.rotation || 0) * 10) / 10);
    if (l.type === 'text') {
      const scaled = l.oh > 0 ? l.fontSize * (l.h / l.oh) : l.fontSize;
      set('tSize', Math.round(scaled * 10) / 10);
    }
  }

  /* ---------------- 片段 ---------------- */

  docSection() {
    const doc = this.app.doc;
    if (!doc.isReady) {
      return `<div class="empty-prop"><p>选中左侧图层或画布上的元素后，在这里修改它的文字、颜色、字号、位置等。</p></div>`;
    }
    const s = doc.stats || {};
    const f = this.app.fidelity();
    return `
      <div class="prop-section">
        <h4>画布</h4>
        <div class="field"><label>尺寸</label><div class="ctl">
          <span style="font-family:var(--mono);font-size:12px">${doc.width} × ${doc.height}</span></div></div>
        <div class="field"><label>图层</label><div class="ctl">
          <span style="font-size:12px">${doc.layers.length} 个（文字 ${s.textLayers ?? '-'} · 元素 ${s.elementLayers ?? '-'}）</span></div></div>
        <div class="field"><label>OCR</label><div class="ctl">
          <span style="font-size:12px">${(s.ocrEngines || []).join(', ') || '-'}</span></div></div>
      </div>
      <div class="prop-section">
        <h4>保真状态</h4>
        <div class="note ${f.edited ? 'warn' : 'ok'}">
          ${f.edited
            ? `已编辑 ${f.edited} 个图层，导出时会重绘 ${f.redrawn} 个图层所在区域；其余 ${doc.layers.length - f.redrawn} 个图层仍是原图像素。`
            : '当前没有任何编辑，导出结果与原图逐像素一致。'}
        </div>
      </div>
      <div class="prop-section">
        <h4>批量</h4>
        <div class="btn-row">
          <button class="tool" id="dSelectText">选中所有文字</button>
          <button class="tool" id="dResetAll">全部还原</button>
        </div>
      </div>`;
  }

  multiSection(sel) {
    return `
      <div class="prop-section">
        <h4>已选 ${sel.length} 个图层</h4>
        <div class="field"><label>不透明</label><div class="ctl">
          <input type="range" id="mOpacity" min="0" max="100" value="100"><output id="mOpacityOut">100</output>
        </div></div>
        <div class="btn-row" style="margin-top:8px">
          <button class="tool" id="mAlignL">左对齐</button>
          <button class="tool" id="mAlignC">水平居中</button>
          <button class="tool" id="mAlignR">右对齐</button>
        </div>
        <div class="btn-row" style="margin-top:6px">
          <button class="tool" id="mAlignT">顶对齐</button>
          <button class="tool" id="mAlignM">垂直居中</button>
          <button class="tool" id="mAlignB">底对齐</button>
        </div>
        <div class="btn-row" style="margin-top:6px">
          <button class="tool" id="mDistH">水平等距</button>
          <button class="tool" id="mDistV">垂直等距</button>
        </div>
      </div>
      <div class="prop-section">
        <h4>操作</h4>
        <div class="btn-row">
          <button class="tool" id="mHide">隐藏</button>
          <button class="tool" id="mDup">复制</button>
          <button class="tool" id="mDel">删除</button>
        </div>
      </div>`;
  }

  commonSection(l) {
    return `
      <div class="prop-section">
        <h4>${TYPE_LABEL[l.type] || l.type}<span class="badge">${escapeHtml(l.kind || '')}</span></h4>
        <div class="field"><label>名称</label><div class="ctl">
          <input type="text" id="pName" value="${escapeHtml(l.name || '')}"></div></div>
        <div class="field"><label>位置</label><div class="ctl grid2" style="display:grid">
          <input type="number" id="pX" value="${Math.round(l.x)}" step="1">
          <input type="number" id="pY" value="${Math.round(l.y)}" step="1"></div></div>
        <div class="field"><label>尺寸</label><div class="ctl grid2" style="display:grid">
          <input type="number" id="pW" value="${Math.round(l.w)}" step="1">
          <input type="number" id="pH" value="${Math.round(l.h)}" step="1"></div></div>
        <div class="field"><label>旋转</label><div class="ctl">
          <input type="number" id="pRot" value="${Math.round((l.rotation || 0) * 10) / 10}" step="0.5">
          <button class="mini" id="pRotReset" title="回到 0°">⟲</button></div></div>
        <div class="field"><label>不透明</label><div class="ctl">
          <input type="range" id="pOpacity" min="0" max="100" value="${Math.round((l.opacity ?? 1) * 100)}">
          <output id="pOpacityOut">${Math.round((l.opacity ?? 1) * 100)}</output></div></div>
      </div>`;
  }

  textSection(l) {
    const current = l.fontFamily || defaultFont(l);
    const opts = fontOptions(l, current);
    const matchTag = l.fontMatch ? ` · 识别 ${l.fontMatch.label} ${l.fontMatch.score}%` : '';
    const scaled = l.oh > 0 ? l.fontSize * (l.h / l.oh) : l.fontSize;
    const drift = textWidthDrift(this.measureCtx, l);

    return `
      <div class="prop-section">
        <h4>文字</h4>
        <textarea id="tText" rows="3" placeholder="输入文字">${escapeHtml(l.text ?? '')}</textarea>
        <div class="field" style="margin-top:8px"><label>字体${matchTag ? '<sup>✓</sup>' : ''}</label><div class="ctl">
          <select id="tFont" title="${escapeHtml(matchTag.slice(3) || '')}">${opts}</select></div></div>
        <div class="field"><label>字号</label><div class="ctl">
          <input type="number" id="tSize" value="${Math.round(scaled * 10) / 10}" step="0.5" min="1">
          <select id="tWeight" style="flex:0 0 88px">
            ${[['300', '细'], ['400', '常规'], ['500', '中等'], ['600', '半粗'], ['700', '粗'], ['800', '特粗']]
              .map(([v, t]) => `<option value="${v}"${String(l.fontWeight) === v ? ' selected' : ''}>${t}</option>`).join('')}
          </select></div></div>
        <div class="field"><label>颜色</label><div class="ctl">
          <input type="color" id="tColor" value="${toHex(l.color)}">
          <input type="text" id="tColorHex" value="${toHex(l.color)}">
          <button class="mini" id="tPick" title="从图上取色">◐</button></div></div>
        <div class="field"><label>对齐</label><div class="ctl">
          <div class="seg" style="flex:1">
            ${['left', 'center', 'right'].map((a) => `<button data-align="${a}" class="${
              l.align === a ? 'on' : ''}">${{ left: '左', center: '中', right: '右' }[a]}</button>`).join('')}
          </div>
          <button class="mini ${l.italic ? 'on' : ''}" id="tItalic" title="斜体"><i>I</i></button>
        </div></div>
        <div class="field"><label>字距</label><div class="ctl">
          <input type="range" id="tSpacing" min="-20" max="60" step="0.5" value="${l.letterSpacing || 0}">
          <output id="tSpacingOut">${l.letterSpacing || 0}</output></div></div>
        <div class="field"><label>行距</label><div class="ctl">
          <input type="range" id="tLine" min="0.8" max="2.6" step="0.05" value="${l.lineHeight || 1.25}">
          <output id="tLineOut">${(l.lineHeight || 1.25).toFixed(2)}</output></div></div>
        <div class="field"><label>描边</label><div class="ctl">
          <input type="color" id="tStrokeColor" value="${toHex(l.strokeColor || '#ffffff')}">
          <input type="range" id="tStroke" min="0" max="12" step="0.5" value="${l.strokeWidth || 0}">
          <output id="tStrokeOut">${l.strokeWidth || 0}</output></div></div>
        <label class="chk"><input type="checkbox" id="tAutoFit" ${l.autoFit ? 'checked' : ''}>
          自动贴合原始宽度（微调字距）</label>
        <div class="note ${Math.abs(drift) > 0.06 ? 'warn' : ''}">
          ${l.fontMatch ? `识别字体：<strong>${l.fontMatch.label} ${l.fontWeight}</strong>
            （字形吻合度 ${l.fontMatch.iou}%）<br>` : ''}
          渲染模式：<strong>${l.textMode === 'pixel' ? '原始像素（未改动）' : '矢量文字（可编辑）'}</strong><br>
          ${l.textMode === 'pixel'
            ? '还没改动这行字，画面上用的是原图像素，与原图完全一致。改字或改样式后会自动切成矢量渲染。'
            : `重排宽度相对原始墨迹 ${(drift * 100).toFixed(1)}%${
                Math.abs(drift) > 0.06 ? '，差异偏大，可换字体或打开自动贴合' : '，贴合良好'}`}
        </div>
        ${l.textMode !== 'pixel' && l.sliceUrl ? `
        <div class="btn-row" style="margin-top:7px">
          <button class="tool" id="tRevertPixel">还原成原始像素</button>
        </div>` : ''}
      </div>`;
  }

  shapeSection(l) {
    return `
      <div class="prop-section">
        <h4>形状</h4>
        <div class="field"><label>类型</label><div class="ctl">
          <select id="sShape">
            ${[['rect', '矩形'], ['rounded-rect', '圆角矩形'], ['ellipse', '椭圆'], ['line', '线条']]
              .map(([v, t]) => `<option value="${v}"${l.shape === v ? ' selected' : ''}>${t}</option>`).join('')}
          </select></div></div>
        <div class="field"><label>填充</label><div class="ctl">
          <input type="color" id="sFill" value="${toHex(l.fill || '#888888')}">
          <input type="text" id="sFillHex" value="${toHex(l.fill || '#888888')}"></div></div>
        <div class="field"><label>圆角</label><div class="ctl">
          <input type="range" id="sRadius" min="0" max="120" step="1" value="${l.radius || 0}">
          <output id="sRadiusOut">${Math.round(l.radius || 0)}</output></div></div>
        <div class="field"><label>描边</label><div class="ctl">
          <input type="color" id="sStrokeColor" value="${toHex(l.strokeColor || '#000000')}">
          <input type="range" id="sStroke" min="0" max="24" step="0.5" value="${l.strokeWidth || 0}">
          <output id="sStrokeOut">${l.strokeWidth || 0}</output></div></div>
        <div class="note">
          渲染模式：<strong>${l.shapeMode === 'pixel' ? '原始像素' : '矢量形状'}</strong><br>
          ${l.shapeMode === 'pixel'
            ? '改颜色或圆角会自动切成矢量绘制，可能与原图有细微差别（比如原本带阴影或渐变）。'
            : '正在用矢量绘制，颜色与圆角完全可控。'}
        </div>
        ${l.shapeMode !== 'pixel' && l.sliceUrl ? `
        <div class="btn-row" style="margin-top:7px">
          <button class="tool" id="sRevertPixel">还原成原始像素</button>
        </div>` : ''}
      </div>`;
  }

  imageSection(l) {
    const f = l.filters || {};
    return `
      <div class="prop-section">
        <h4>图像</h4>
        <div class="btn-row">
          <button class="tool" id="iReplace">替换图片</button>
          <button class="tool" id="iFitBox">还原原始尺寸</button>
        </div>
        <input type="file" id="iFile" accept="image/*" hidden>
        <div class="field" style="margin-top:9px"><label>亮度</label><div class="ctl">
          <input type="range" id="fBright" min="20" max="220" value="${f.brightness ?? 100}">
          <output id="fBrightOut">${f.brightness ?? 100}</output></div></div>
        <div class="field"><label>对比</label><div class="ctl">
          <input type="range" id="fContrast" min="20" max="220" value="${f.contrast ?? 100}">
          <output id="fContrastOut">${f.contrast ?? 100}</output></div></div>
        <div class="field"><label>饱和</label><div class="ctl">
          <input type="range" id="fSat" min="0" max="240" value="${f.saturate ?? 100}">
          <output id="fSatOut">${f.saturate ?? 100}</output></div></div>
        <div class="field"><label>模糊</label><div class="ctl">
          <input type="range" id="fBlur" min="0" max="24" step="0.5" value="${f.blur ?? 0}">
          <output id="fBlurOut">${f.blur ?? 0}</output></div></div>
        <div class="field"><label>黑白</label><div class="ctl">
          <input type="range" id="fGray" min="0" max="100" value="${f.grayscale ?? 0}">
          <output id="fGrayOut">${f.grayscale ?? 0}</output></div></div>
      </div>`;
  }

  fidelitySection(l) {
    const touched = l.dirty || !l.fromSource;
    return `
      <div class="prop-section">
        <h4>状态</h4>
        <div class="note ${touched ? 'warn' : 'ok'}">
          ${touched
            ? '这个图层已被编辑，导出时它的原始位置会用修补后的背景填掉，再按当前设置重画。'
            : '这个图层还没被改动，导出时直接沿用原图像素。'}
        </div>
        ${touched && l.fromSource ? `
        <div class="btn-row" style="margin-top:8px">
          <button class="tool" id="pRevert">还原这个图层</button>
        </div>` : ''}
      </div>`;
  }

  /* ---------------- 绑定 ---------------- */

  bindDoc() {
    const app = this.app;
    this.body.querySelector('#dSelectText')?.addEventListener('click', () => {
      app.canvas.select(app.doc.layers.filter((l) => l.type === 'text' && l.visible));
    });
    this.body.querySelector('#dResetAll')?.addEventListener('click', () => app.revertAll());
  }

  bindMulti(sel) {
    const app = this.app;
    const q = (id) => this.body.querySelector(id);
    const commit = (label) => {
      app.history.push(app.doc, label);
      app.requestRender();
      app.refreshLayerPanel();
    };

    const opacity = q('#mOpacity');
    opacity?.addEventListener('input', () => {
      q('#mOpacityOut').textContent = opacity.value;
      sel.forEach((l) => { l.opacity = Number(opacity.value) / 100; markDirty(l); });
      app.requestRender();
    });
    opacity?.addEventListener('change', () => commit('不透明度'));

    const bounds = () => {
      const xs = sel.map((l) => l.x), ys = sel.map((l) => l.y);
      const xe = sel.map((l) => l.x + l.w), ye = sel.map((l) => l.y + l.h);
      return { x0: Math.min(...xs), y0: Math.min(...ys),
               x1: Math.max(...xe), y1: Math.max(...ye) };
    };
    const align = (fn) => { const b = bounds(); sel.forEach((l) => { fn(l, b); markDirty(l); }); commit('对齐'); };

    q('#mAlignL')?.addEventListener('click', () => align((l, b) => { l.x = b.x0; }));
    q('#mAlignR')?.addEventListener('click', () => align((l, b) => { l.x = b.x1 - l.w; }));
    q('#mAlignC')?.addEventListener('click', () => align((l, b) => { l.x = (b.x0 + b.x1) / 2 - l.w / 2; }));
    q('#mAlignT')?.addEventListener('click', () => align((l, b) => { l.y = b.y0; }));
    q('#mAlignB')?.addEventListener('click', () => align((l, b) => { l.y = b.y1 - l.h; }));
    q('#mAlignM')?.addEventListener('click', () => align((l, b) => { l.y = (b.y0 + b.y1) / 2 - l.h / 2; }));

    q('#mDistH')?.addEventListener('click', () => {
      const sorted = [...sel].sort((a, b) => a.x - b.x);
      const b = bounds();
      const totalW = sorted.reduce((s, l) => s + l.w, 0);
      const gap = (b.x1 - b.x0 - totalW) / Math.max(1, sorted.length - 1);
      let cx = b.x0;
      sorted.forEach((l) => { l.x = cx; cx += l.w + gap; markDirty(l); });
      commit('水平等距');
    });
    q('#mDistV')?.addEventListener('click', () => {
      const sorted = [...sel].sort((a, b) => a.y - b.y);
      const b = bounds();
      const totalH = sorted.reduce((s, l) => s + l.h, 0);
      const gap = (b.y1 - b.y0 - totalH) / Math.max(1, sorted.length - 1);
      let cy = b.y0;
      sorted.forEach((l) => { l.y = cy; cy += l.h + gap; markDirty(l); });
      commit('垂直等距');
    });

    q('#mHide')?.addEventListener('click', () => {
      sel.forEach((l) => { l.visible = false; markDirty(l); });
      commit('隐藏');
    });
    q('#mDup')?.addEventListener('click', () => app.duplicateSelection());
    q('#mDel')?.addEventListener('click', () => app.deleteSelection());
  }

  bindCommon(l) {
    const app = this.app;
    const q = (id) => this.body.querySelector(id);
    const commit = (label) => { app.history.push(app.doc, label); app.refreshLayerPanel(); };
    const live = (label) => { markDirty(l); app.requestRender(); };

    q('#pName')?.addEventListener('change', (e) => {
      l.name = e.target.value; commit('重命名');
    });

    for (const [id, key] of [['#pX', 'x'], ['#pY', 'y'], ['#pW', 'w'], ['#pH', 'h']]) {
      const el = q(id);
      el?.addEventListener('input', () => {
        const v = Number(el.value);
        if (!Number.isFinite(v)) return;
        l[key] = (key === 'w' || key === 'h') ? Math.max(1, v) : v;
        live();
      });
      el?.addEventListener('change', () => commit('尺寸位置'));
    }

    const rot = q('#pRot');
    rot?.addEventListener('input', () => { l.rotation = Number(rot.value) || 0; live(); });
    rot?.addEventListener('change', () => commit('旋转'));
    q('#pRotReset')?.addEventListener('click', () => {
      l.rotation = 0; live(); commit('旋转'); this.render();
    });

    const op = q('#pOpacity');
    op?.addEventListener('input', () => {
      q('#pOpacityOut').textContent = op.value;
      l.opacity = Number(op.value) / 100;
      live();
    });
    op?.addEventListener('change', () => commit('不透明度'));
  }

  bindText(l) {
    const app = this.app;
    const q = (id) => this.body.querySelector(id);
    const toVector = () => { l.textMode = 'vector'; markDirty(l); };
    const live = () => { app.requestRender(); };
    const commit = (label) => { app.history.push(app.doc, label); app.refreshLayerPanel(); };

    const ta = q('#tText');
    ta?.addEventListener('input', () => {
      l.text = ta.value;
      l.autoFit = false;
      toVector(); live();
    });
    ta?.addEventListener('change', () => { commit('改文字'); this.render(); });

    q('#tFont')?.addEventListener('change', (e) => {
      l.fontFamily = e.target.value; toVector(); live(); commit('字体'); this.render();
    });

    const size = q('#tSize');
    size?.addEventListener('input', () => {
      const shown = Number(size.value);
      if (!Number.isFinite(shown) || shown <= 0) return;
      // 面板里显示的是「屏幕上看到的字号」，需换算回原始字号
      const factor = l.oh > 0 ? l.h / l.oh : 1;
      l.fontSize = shown / (factor || 1);
      toVector(); live();
    });
    size?.addEventListener('change', () => commit('字号'));

    q('#tWeight')?.addEventListener('change', (e) => {
      l.fontWeight = Number(e.target.value); toVector(); live(); commit('字重');
    });

    const color = q('#tColor');
    const colorHex = q('#tColorHex');
    const applyColor = (v) => {
      if (!/^#[0-9a-fA-F]{6}$/.test(v)) return;
      l.color = v;
      if (color) color.value = v;
      if (colorHex) colorHex.value = v;
      toVector(); live();
    };
    color?.addEventListener('input', () => applyColor(color.value));
    color?.addEventListener('change', () => commit('文字颜色'));
    colorHex?.addEventListener('change', () => { applyColor(colorHex.value.trim()); commit('文字颜色'); });
    q('#tPick')?.addEventListener('click', () => app.startEyedropper((hex) => {
      applyColor(hex); commit('取色'); this.render();
    }));

    this.body.querySelectorAll('[data-align]').forEach((btn) => {
      btn.addEventListener('click', () => {
        l.align = btn.dataset.align;
        this.body.querySelectorAll('[data-align]').forEach((b) => b.classList.toggle('on', b === btn));
        toVector(); live(); commit('对齐');
      });
    });

    q('#tItalic')?.addEventListener('click', (e) => {
      l.italic = !l.italic;
      e.currentTarget.classList.toggle('on', l.italic);
      toVector(); live(); commit('斜体');
    });

    const bindRange = (id, outId, key, fmt = (v) => v, label = '') => {
      const el = q(id);
      el?.addEventListener('input', () => {
        const v = Number(el.value);
        q(outId).textContent = fmt(v);
        l[key] = v;
        toVector(); live();
      });
      el?.addEventListener('change', () => commit(label));
    };
    bindRange('#tSpacing', '#tSpacingOut', 'letterSpacing', (v) => v, '字距');
    bindRange('#tLine', '#tLineOut', 'lineHeight', (v) => v.toFixed(2), '行距');
    bindRange('#tStroke', '#tStrokeOut', 'strokeWidth', (v) => v, '描边');
    q('#tStrokeColor')?.addEventListener('input', (e) => {
      l.strokeColor = e.target.value; toVector(); live();
    });

    q('#tAutoFit')?.addEventListener('change', (e) => {
      l.autoFit = e.target.checked; toVector(); live(); commit('自动贴合'); this.render();
    });

    q('#tRevertPixel')?.addEventListener('click', () => {
      app.revertLayer(l);
      this.render();
    });
  }

  bindShape(l) {
    const app = this.app;
    const q = (id) => this.body.querySelector(id);
    const toVector = () => { l.shapeMode = 'vector'; markDirty(l); };
    const live = () => app.requestRender();
    const commit = (label) => { app.history.push(app.doc, label); app.refreshLayerPanel(); };

    q('#sShape')?.addEventListener('change', (e) => {
      l.shape = e.target.value; toVector(); live(); commit('形状'); this.render();
    });

    const fill = q('#sFill');
    const fillHex = q('#sFillHex');
    const applyFill = (v) => {
      if (!/^#[0-9a-fA-F]{6}$/.test(v)) return;
      l.fill = v;
      if (fill) fill.value = v;
      if (fillHex) fillHex.value = v;
      toVector(); live();
    };
    fill?.addEventListener('input', () => applyFill(fill.value));
    fill?.addEventListener('change', () => commit('填充色'));
    fillHex?.addEventListener('change', () => { applyFill(fillHex.value.trim()); commit('填充色'); });

    const radius = q('#sRadius');
    radius?.addEventListener('input', () => {
      q('#sRadiusOut').textContent = radius.value;
      l.radius = Number(radius.value);
      if (l.shape === 'rect' && l.radius > 0) l.shape = 'rounded-rect';
      toVector(); live();
    });
    radius?.addEventListener('change', () => commit('圆角'));

    const stroke = q('#sStroke');
    stroke?.addEventListener('input', () => {
      q('#sStrokeOut').textContent = stroke.value;
      l.strokeWidth = Number(stroke.value);
      toVector(); live();
    });
    stroke?.addEventListener('change', () => commit('描边'));
    q('#sStrokeColor')?.addEventListener('input', (e) => {
      l.strokeColor = e.target.value; toVector(); live();
    });

    q('#sRevertPixel')?.addEventListener('click', () => { app.revertLayer(l); this.render(); });
  }

  bindImage(l) {
    const app = this.app;
    const q = (id) => this.body.querySelector(id);
    const live = () => { markDirty(l); app.requestRender(); };
    const commit = (label) => { app.history.push(app.doc, label); app.refreshLayerPanel(); };

    const file = q('#iFile');
    q('#iReplace')?.addEventListener('click', () => file?.click());
    file?.addEventListener('change', async () => {
      const f = file.files?.[0];
      if (!f) return;
      await app.replaceImage(l, f);
      this.render();
    });
    q('#iFitBox')?.addEventListener('click', () => {
      l.x = l.ox; l.y = l.oy; l.w = l.ow; l.h = l.oh; l.rotation = 0;
      live(); commit('还原尺寸'); this.sync();
    });

    if (!l.filters) l.filters = { brightness: 100, contrast: 100, saturate: 100, blur: 0, grayscale: 0 };
    const bindF = (id, outId, key, label) => {
      const el = q(id);
      el?.addEventListener('input', () => {
        q(outId).textContent = el.value;
        l.filters[key] = Number(el.value);
        live();
      });
      el?.addEventListener('change', () => commit(label));
    };
    bindF('#fBright', '#fBrightOut', 'brightness', '亮度');
    bindF('#fContrast', '#fContrastOut', 'contrast', '对比度');
    bindF('#fSat', '#fSatOut', 'saturate', '饱和度');
    bindF('#fBlur', '#fBlurOut', 'blur', '模糊');
    bindF('#fGray', '#fGrayOut', 'grayscale', '黑白');
  }

  bindFidelity(l) {
    this.body.querySelector('#pRevert')?.addEventListener('click', () => {
      this.app.revertLayer(l);
      this.render();
    });
  }
}

/* --------------------------------------------------------------------- */

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toHex(color) {
  if (!color) return '#000000';
  if (/^#[0-9a-fA-F]{6}$/.test(color)) return color.toLowerCase();
  if (/^#[0-9a-fA-F]{3}$/.test(color)) {
    return `#${color.slice(1).split('').map((c) => c + c).join('')}`.toLowerCase();
  }
  const m = String(color).match(/rgba?\(([^)]+)\)/);
  if (m) {
    const [r, g, b] = m[1].split(',').map((v) => Math.round(Number(v)));
    return `#${[r, g, b].map((v) => Math.max(0, Math.min(255, v)).toString(16).padStart(2, '0')).join('')}`;
  }
  return '#000000';
}
