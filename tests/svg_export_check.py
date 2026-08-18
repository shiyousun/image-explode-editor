"""SVG 导出验收：矢量图渲染出来必须和编辑器画布一致。

跑法：python3 tests/svg_export_check.py [样张路径]
需要 playwright（chromium）和已经在 8770 端口跑起来的服务。
"""
from __future__ import annotations

import base64
import json
import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "workspace", "svg_check")
URL = os.environ.get("EDITOR_URL", "http://127.0.0.1:8770/")


def main(sample: str) -> int:
    os.makedirs(OUT, exist_ok=True)
    logs: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000},
                                device_scale_factor=1)
        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))

        page.goto(URL, wait_until="networkidle")
        page.set_input_files("#fileInput", sample)
        page.wait_for_selector("#explodeModal:not([hidden])", timeout=60_000)
        page.click("#btnExplodeRun")
        page.wait_for_function("window.app?.doc?.isReady === true", timeout=180_000)
        page.wait_for_function("document.getElementById('busy').hidden === true",
                               timeout=180_000)

        info = page.evaluate("""() => ({
            layers: app.doc.layers.length,
            text: app.doc.layers.filter(l => l.type === 'text').length,
        })""")
        print(f"炸开完成：{info['layers']} 层（文字 {info['text']}）")

        # 1) 未编辑状态：SVG 里应当只有一张原图像素，且和画布逐像素一致
        base = compare(page, 1)
        print(f"未编辑  矢量层={base['vectorCount']:2d}  差异像素={base['diff']}"
              f"（{base['ratio']:.4f}%）  SVG {base['bytes'] / 1024:.0f}KB")

        # 2) 全部文字转清晰 + 改字 + 挪元素 + 形状换色，再比一次
        page.evaluate("app.sharpenAllText()")
        # 转清晰之后还有一轮后台放大重认，等它落定再改字，否则改的内容会被识别结果盖掉
        page.wait_for_timeout(12_000)
        edits = page.evaluate("""async () => {
            const s = await import('/static/js/state.js');
            const done = [];
            const t = app.doc.layers.find(l => l.type === 'text' && l.visible);
            if (t) { t.text = '矢量导出测试标题'; s.markDirty(t); done.push('改字'); }
            const el = app.doc.layers.find(l => l.type !== 'text' && l.visible
                                                && l.w > 40 && l.h > 40);
            if (el) { el.x += 40; el.y += 24; s.markDirty(el); done.push('挪元素'); }
            const sh = app.doc.layers.find(l => l.type === 'shape');
            if (sh) { sh.shapeMode = 'vector'; sh.fill = '#ff5577'; s.markDirty(sh);
                      done.push('形状换色'); }
            app.render();
            return done;
        }""")
        print("编辑动作：" + "、".join(edits))
        page.wait_for_timeout(800)

        edited = compare(page, 1)
        print(f"编辑后  矢量层={edited['vectorCount']:2d}  差异像素={edited['diff']}"
              f"（{edited['ratio']:.4f}%）  SVG {edited['bytes'] / 1024:.0f}KB")

        svg = page.evaluate("""(scale) => {
            return import('/static/js/svgexport.js')
              .then(m => m.buildSvg(app.doc, { rasterScale: scale }).svg);
        }""", 1)
        print("改过的文字进了 SVG：" + ("是" if "矢量导出测试标题" in svg else "否"))
        name = os.path.splitext(os.path.basename(sample))[0]
        with open(os.path.join(OUT, f"{name}.svg"), "w", encoding="utf-8") as fh:
            fh.write(svg)
        for key, shot in (("canvas", edited["canvasPng"]), ("svg", edited["svgPng"])):
            with open(os.path.join(OUT, f"{name}_{key}.png"), "wb") as fh:
                fh.write(base64.b64decode(shot.split(",", 1)[1]))

        print(f"文件写到 {OUT}")
        print("控制台：" + (json.dumps(logs[:10], ensure_ascii=False) if logs else "无报错"))
        browser.close()

    worst = max(base["ratio"], edited["ratio"])
    return 0 if worst < 1.0 else 1


COMPARE_JS = """
async (scale) => {
  const svgmod = await import('/static/js/svgexport.js');
  const render = await import('/static/js/render.js');
  const { svg, vectorCount } = svgmod.buildSvg(app.doc, { rasterScale: scale });

  const W = app.doc.width, H = app.doc.height;
  const ref = render.renderToCanvas(app.doc, 1, '#ffffff');

  const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
  const img = new Image();
  await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
  const got = document.createElement('canvas');
  got.width = W; got.height = H;
  const g = got.getContext('2d');
  g.fillStyle = '#ffffff'; g.fillRect(0, 0, W, H);
  g.drawImage(img, 0, 0, W, H);
  URL.revokeObjectURL(url);

  const a = ref.getContext('2d').getImageData(0, 0, W, H).data;
  const b = g.getImageData(0, 0, W, H).data;
  let diff = 0;
  const heat = document.createElement('canvas');
  heat.width = W; heat.height = H;
  const hd = heat.getContext('2d').createImageData(W, H);
  for (let i = 0; i < a.length; i += 4) {
    const d = Math.max(Math.abs(a[i] - b[i]), Math.abs(a[i+1] - b[i+1]),
                       Math.abs(a[i+2] - b[i+2]));
    if (d > 24) {                       // 抗锯齿的一两个色阶不算
      diff++;
      hd.data[i] = 255; hd.data[i+3] = 255;
    }
  }
  heat.getContext('2d').putImageData(hd, 0, 0);
  return {
    vectorCount, diff, ratio: (diff * 100) / (W * H),
    bytes: new Blob([svg]).size,
    canvasPng: ref.toDataURL('image/png'),
    svgPng: got.toDataURL('image/png'),
    heatPng: heat.toDataURL('image/png'),
  };
}
"""


def compare(page, scale: int) -> dict:
    return page.evaluate(COMPARE_JS, scale)


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "samples",
                                                             "real_infographic.png")
    sys.exit(main(src))
