"""六个色块逐块隔离验收。

和 block_explode_check.py 的区别：
  - 不是抽一个代表，而是六块逐个验；
  - 同时验「挪动」和「删除」；
  - 不只看图层数量，还逐像素确认：
      1) 被拿走的色块本体是纯色、没有烘进去的文字；
      2) 原位置与该色块的专用擦除补丁一致（文字层覆盖区除外）；
      3) 图元使用预生成补丁，拖动当帧即生效，不依赖异步 /api/erase。

跑法：
    python3 tests/block_isolation_check.py [样张路径]
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLE = (sys.argv[1] if len(sys.argv) > 1
          else os.path.join(ROOT, "samples", "series_preview.png"))
STRENGTH = sys.argv[2] if len(sys.argv) > 2 else "standard"
URL = os.environ.get("EDITOR_URL", "http://127.0.0.1:8770/")
OUT = os.path.join(ROOT, "workspace", "block_isolation")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    logs: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))

        page.goto(URL, wait_until="networkidle")
        page.set_input_files("#fileInput", SAMPLE)
        page.wait_for_selector("#explodeModal:not([hidden])", timeout=60_000)
        page.select_option("#optStrength", STRENGTH)
        page.click("#btnExplodeRun")
        page.wait_for_function("window.app?.doc?.isReady === true", timeout=180_000)
        page.wait_for_function("document.getElementById('busy').hidden === true",
                               timeout=180_000)

        result = page.evaluate("""async () => {
          const state = await import('/static/js/state.js');
          const render = await import('/static/js/render.js');
          const doc = app.doc;
          const cards = doc.layers
            .filter(l => l.type === 'shape' && l.w > 150 && l.w < 400 && l.h > 60)
            .sort((a, b) => (a.y - b.y) || (a.x - b.x));

          function textMask() {
            const c = document.createElement('canvas');
            c.width = doc.width; c.height = doc.height;
            const g = c.getContext('2d');
            for (const t of doc.layers) {
              if (t.type !== 'text' || !t.fromSource) continue;
              const img = doc.imageFor(t);
              if (img) g.drawImage(img, t.ox, t.oy, t.ow, t.oh);
            }
            return g.getImageData(0, 0, c.width, c.height).data;
          }

          const tm = textMask();
          const hasTextNear = (x, y, radius = 4) => {
            for (let yy = Math.max(0, y - radius); yy <= Math.min(doc.height - 1, y + radius); yy++) {
              for (let xx = Math.max(0, x - radius); xx <= Math.min(doc.width - 1, x + radius); xx++) {
                if (tm[(yy * doc.width + xx) * 4 + 3] > 8) return true;
              }
            }
            return false;
          };

          function sourcePurity(l) {
            const img = doc.imageFor(l, true);
            if (!img) return { total: 0, holes: -1, off: -1, worst: 255 };
            const c = document.createElement('canvas');
            c.width = img.naturalWidth; c.height = img.naturalHeight;
            const g = c.getContext('2d');
            g.drawImage(img, 0, 0);
            const d = g.getImageData(0, 0, c.width, c.height).data;
            const want = [1, 3, 5].map(i => parseInt(l.fill.slice(i, i + 2), 16));
            const margin = Math.max(5, Math.round(Math.min(c.width, c.height) * 0.12));
            let total = 0, holes = 0, off = 0, worst = 0;
            for (let y = margin; y < c.height - margin; y++) {
              for (let x = margin; x < c.width - margin; x++) {
                const i = (y * c.width + x) * 4;
                total++;
                if (d[i + 3] < 250) { holes++; continue; }
                const e = Math.max(Math.abs(d[i] - want[0]), Math.abs(d[i + 1] - want[1]),
                                   Math.abs(d[i + 2] - want[2]));
                worst = Math.max(worst, e);
                if (e > 3) off++;
              }
            }
            return { total, holes, off, worst };
          }

          function oldSpotAgainstPatch(l) {
            const patch = l.erasePatchUrl && doc.images.get(l.erasePatchUrl);
            if (!patch || !l.erasePatchRect) return { total: 0, diff: -1, worst: 255 };
            const canvas = render.renderToCanvas(doc, 1, null);
            const cur = canvas.getContext('2d')
              .getImageData(0, 0, doc.width, doc.height).data;
            const pc = document.createElement('canvas');
            pc.width = patch.naturalWidth; pc.height = patch.naturalHeight;
            const pg = pc.getContext('2d');
            pg.drawImage(patch, 0, 0);
            const ref = pg.getImageData(0, 0, pc.width, pc.height).data;
            const [px, py] = l.erasePatchRect.map(Math.round);
            const margin = 7; // 避开轮廓抗锯齿与补丁边沿
            let total = 0, diff = 0, worst = 0;
            for (let y = Math.round(l.oy) + margin; y < Math.round(l.oy + l.oh) - margin; y++) {
              for (let x = Math.round(l.ox) + margin; x < Math.round(l.ox + l.ow) - margin; x++) {
                if (hasTextNear(x, y)) continue; // 文字应留在原位，它是另一个图层，不算色块残留
                const a = (y * doc.width + x) * 4;
                const b = ((y - py) * pc.width + (x - px)) * 4;
                const e = Math.max(Math.abs(cur[a] - ref[b]), Math.abs(cur[a + 1] - ref[b + 1]),
                                   Math.abs(cur[a + 2] - ref[b + 2]));
                total++; worst = Math.max(worst, e);
                if (e > 3) diff++;
              }
            }
            return { total, diff, worst };
          }

          const rows = [];
          for (const l of cards) {
            const saved = { x: l.x, y: l.y, visible: l.visible, dirty: l.dirty };
            const purity = sourcePurity(l);

            // 挪动：不等待网络，直接在同一 JS 任务里渲染，验证专用补丁能当帧生效。
            l.x += 24; l.y -= 145; state.markDirty(l);
            const asyncRects = render.eraseRects(doc).length;
            const moved = oldSpotAgainstPatch(l);

            Object.assign(l, saved);
            l.visible = false; state.markDirty(l);
            const deleted = oldSpotAgainstPatch(l);

            Object.assign(l, saved);
            rows.push({
              id: l.id, fill: l.fill, w: Math.round(l.w), h: Math.round(l.h),
              hasClean: !!l.sliceCleanUrl, hasErase: !!l.erasePatchUrl,
              purity, moved, deleted, asyncRects,
            });
          }

          const pristine = render.renderToCanvas(doc, 1, null);
          const now = pristine.getContext('2d')
            .getImageData(0, 0, doc.width, doc.height).data;
          const refc = document.createElement('canvas');
          refc.width = doc.width; refc.height = doc.height;
          const rg = refc.getContext('2d');
          rg.drawImage(doc.baseImg, 0, 0);
          const org = rg.getImageData(0, 0, doc.width, doc.height).data;
          let pristineDiff = 0;
          for (let i = 0; i < now.length; i += 4) {
            if (Math.max(Math.abs(now[i] - org[i]), Math.abs(now[i + 1] - org[i + 1]),
                         Math.abs(now[i + 2] - org[i + 2])) > 2) pristineDiff++;
          }
          return { rows, pristineDiff };
        }""")

        print(f"颗粒度 {STRENGTH}：识别到 {len(result['rows'])} 个主色块")
        ok = len(result["rows"]) == 6 and result["pristineDiff"] == 0 and not logs
        for r in result["rows"]:
            p, m, d = r["purity"], r["moved"], r["deleted"]
            row_ok = (r["hasClean"] and r["hasErase"] and p["holes"] == 0 and p["off"] == 0
                      and m["diff"] == 0 and d["diff"] == 0 and r["asyncRects"] == 0)
            ok &= row_ok
            print(
                f"  {r['id']:4s} {r['w']}x{r['h']} {r['fill']} | "
                f"本体：孔{p['holes']} 偏色{p['off']} 最大差{p['worst']} | "
                f"挪走原位：差异{m['diff']}/{m['total']} 最大差{m['worst']} | "
                f"删除原位：差异{d['diff']}/{d['total']} 最大差{d['worst']} | "
                f"异步擦除矩形{r['asyncRects']} {'✓' if row_ok else '✗'}"
            )
        print(f"未编辑保真差异：{result['pristineDiff']} 像素")
        print(f"控制台错误/警告：{len(logs)} 条")
        for line in logs[:10]:
            print("  " + line)
        page.screenshot(path=os.path.join(OUT, "six_blocks.png"))
        browser.close()

    print("全部通过" if ok else "有项目未达标")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
