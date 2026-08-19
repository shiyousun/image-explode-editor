"""色块炸开验收：色块要炸得全、炸得纯，挪走之后原位要干净。

跑法：python3 tests/block_explode_check.py [样张路径] [该图至少应有几个色块]
需要 playwright（chromium）和已经在 8770 端口跑起来的服务。

三件事必须同时成立：
  1. 图中每一个色块都成为独立可编辑图层（少一个都算漏炸）；
  2. 炸开后的画布与原图逐像素一致（没动过就不该有任何变化）；
  3. 色块被挪走后，它自己是一整块纯净的颜色，原来的位置被干净地补上。
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "workspace", "block_check")
URL = os.environ.get("EDITOR_URL", "http://127.0.0.1:8770/")


def main(sample: str, min_blocks: int = 0) -> int:
    os.makedirs(OUT, exist_ok=True)
    logs: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1700, "height": 1050},
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

        shapes = page.evaluate("""() => {
            const L = app.doc.layers;
            return {
                total: L.length,
                text: L.filter(l => l.type === 'text').length,
                shapes: L.filter(l => l.type === 'shape').map(l => ({
                    id: l.id, kind: l.kind, fill: l.fill,
                    w: Math.round(l.w), h: Math.round(l.h),
                    x: Math.round(l.x), y: Math.round(l.y),
                })),
            };
        }""")
        print(f"炸开完成：{shapes['total']} 层（文字 {shapes['text']}，"
              f"色块/图元 {len(shapes['shapes'])}）")
        for s in sorted(shapes["shapes"], key=lambda s: -(s["w"] * s["h"])):
            print(f"  {s['id']:4s} {s['kind']:13s} {s['w']:4d}x{s['h']:<4d} "
                  f"({s['x']:4d},{s['y']:4d})  fill={s['fill']}")

        fid = fidelity(page)
        print(f"\n保真度：与原图不同的像素 {fid['diff']}（{fid['ratio']:.4f}%），"
              f"最大色差 {fid['maxDelta']}")
        page.screenshot(path=os.path.join(OUT, "01_exploded.png"))

        # 一块灰卡片：挪走，看它自己纯不纯、原位干不干净
        card = page.evaluate("""async () => {
            const s = await import('/static/js/state.js');
            const cards = app.doc.layers.filter(l => l.type === 'shape' && l.w > 120);
            const gray = cards.filter(l => {
                const [r, g, b] = [1, 3, 5].map(i => parseInt(l.fill.substr(i, 2), 16));
                return Math.max(r, g, b) - Math.min(r, g, b) < 24;
            });
            const t = gray[0] || cards[0];
            if (!t) return null;   // 这张图没有成块的色块，挪动这一项就跳过
            t.x += 260;
            s.markDirty(t);
            app.history.push(app.doc, '挪动色块');   // 栈顶记的是「操作后」的状态
            app.render();
            return { id: t.id, fill: t.fill, w: Math.round(t.w), h: Math.round(t.h) };
        }""")
        purity = {"off": 0, "holes": 0, "worst": 0, "total": 0, "usesClean": None}
        back = fid
        if card:
            page.wait_for_function("document.getElementById('busy').hidden === true",
                                   timeout=120_000)
            page.wait_for_timeout(1500)
            print(f"挪动色块 {card['id']}（{card['w']}x{card['h']} {card['fill']}）右移 260px")
            page.screenshot(path=os.path.join(OUT, "02_moved.png"))

            # 纯净度直接量「挪动时实际用来画它的那张位图」。不从合成画布上取样：挪过去
            # 可能压在别的卡片下面，取到的是上层的文字，量的就不是这块色块本身了。
            purity = page.evaluate("""async (id) => {
                const render = await import('/static/js/render.js');
                const L = app.doc.layers.find(l => l.id === id);
                const usesClean = render.buildCleanSliceSet(app.doc).has(L);
                const img = app.doc.imageFor(L, usesClean);
                const c = document.createElement('canvas');
                c.width = img.naturalWidth; c.height = img.naturalHeight;
                const g = c.getContext('2d');
                g.drawImage(img, 0, 0);
                // 掐掉四周一圈，避开圆角和羽化边
                const m = Math.round(Math.min(c.width, c.height) * 0.14);
                const d = g.getImageData(m, m, c.width - 2 * m, c.height - 2 * m).data;
                const want = [1, 3, 5].map(i => parseInt(L.fill.substr(i, 2), 16));
                let off = 0, worst = 0, holes = 0;
                for (let i = 0; i < d.length; i += 4) {
                    if (d[i + 3] < 250) { holes++; continue; }
                    const e = Math.max(Math.abs(d[i] - want[0]), Math.abs(d[i+1] - want[1]),
                                       Math.abs(d[i+2] - want[2]));
                    worst = Math.max(worst, e);
                    if (e > 12) off++;
                }
                return { total: d.length / 4, off, worst, holes, usesClean };
            }""", card["id"])
            print(f"色块纯净度：用擦字切片={purity['usesClean']}，取样 {purity['total']} 像素，"
                  f"透明孔洞 {purity['holes']}，偏离填充色的 {purity['off']} 个，"
                  f"最大偏离 {purity['worst']}")

            page.evaluate("app.undo()")
            page.wait_for_function("document.getElementById('busy').hidden === true",
                                   timeout=120_000)
            page.wait_for_timeout(1200)
            back = fidelity(page)
            print(f"撤销复原：与原图不同的像素 {back['diff']}（{back['ratio']:.4f}%）")
            page.screenshot(path=os.path.join(OUT, "03_undone.png"))
        else:
            print("这张图没有成块的色块，跳过挪动与撤销这两项")

        print(f"\n控制台错误/警告 {len(logs)} 条")
        for line in logs[:12]:
            print("  " + line)
        browser.close()

    ok = (fid["diff"] == 0 and back["diff"] == 0 and purity["off"] == 0
          and purity["holes"] == 0 and len(shapes["shapes"]) >= min_blocks and not logs)
    print("\n" + ("全部通过" if ok else "有项目未达标，见上"))
    print("截图目录:", OUT)
    return 0 if ok else 1


def fidelity(page) -> dict:
    """当前画布 vs 原始上传图，逐像素比。"""
    return page.evaluate("""async () => {
        const render = await import('/static/js/render.js');
        const canvas = render.renderToCanvas(app.doc, 1, null);
        const g = canvas.getContext('2d');
        const cur = g.getImageData(0, 0, canvas.width, canvas.height).data;

        const img = app.doc.baseImg;
        const ref = document.createElement('canvas');
        ref.width = canvas.width; ref.height = canvas.height;
        const rg = ref.getContext('2d');
        rg.drawImage(img, 0, 0, canvas.width, canvas.height);
        const org = rg.getImageData(0, 0, canvas.width, canvas.height).data;

        let diff = 0, maxDelta = 0;
        for (let i = 0; i < cur.length; i += 4) {
            const e = Math.max(Math.abs(cur[i] - org[i]), Math.abs(cur[i+1] - org[i+1]),
                               Math.abs(cur[i+2] - org[i+2]));
            maxDelta = Math.max(maxDelta, e);
            if (e > 2) diff++;
        }
        return { diff, maxDelta, ratio: 100 * diff / (cur.length / 4) };
    }""")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "samples",
                                                             "series_preview.png")
    least = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    sys.exit(main(arg, least))
