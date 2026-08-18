"""生成测试用信息图（中英文混排 + 色块 + 图标 + 渐变照片区），用于验证炸开效果。"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

MAC_FONTS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def load_font(size: int, index: int = 0):
    for path in MAC_FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=index)
            except Exception:
                continue
    return ImageFont.load_default()


def make_sample(path: str, width: int = 1080, height: int = 1350) -> str:
    img = Image.new("RGB", (width, height), (247, 245, 240))
    draw = ImageDraw.Draw(img)

    # 顶部渐变横幅
    for y in range(0, 300):
        t = y / 300.0
        draw.line([(0, y), (width, y)],
                  fill=(int(28 + t * 20), int(52 + t * 60), int(120 + t * 70)))

    draw.text((70, 96), "AI 眼镜行业深度观察", font=load_font(66, 4), fill=(255, 255, 255))
    draw.text((72, 196), "2026 Market Landscape Report", font=load_font(34), fill=(196, 214, 245))

    # 三个数据卡片（圆角矩形 + 数字 + 说明）
    cards = [
        ("1420", "万台出货", (233, 90, 74)),
        ("38.6%", "同比增长", (46, 158, 122)),
        ("¥2199", "均价下探", (58, 106, 214)),
    ]
    cx, cy, cw, ch, gap = 70, 370, 300, 190, 20
    for i, (num, label, color) in enumerate(cards):
        x = cx + i * (cw + gap)
        draw.rounded_rectangle([x, cy, x + cw, cy + ch], radius=18, fill=(255, 255, 255),
                               outline=(226, 222, 214), width=2)
        draw.rounded_rectangle([x, cy, x + cw, cy + 8], radius=4, fill=color)
        draw.text((x + 28, cy + 44), num, font=load_font(52), fill=color)
        draw.text((x + 30, cy + 118), label, font=load_font(30, 2), fill=(96, 96, 104))

    # 正文段落
    body = [
        "消费级 AI 眼镜在 2026 年迎来关键转折点，光波导良率提升",
        "带来整机成本快速下降，头部厂商开始把重心从参数竞赛转向",
        "真实使用场景的打磨，语音助手与拍摄成为最高频的两个入口。",
    ]
    ty = 620
    for row in body:
        draw.text((70, ty), row, font=load_font(31, 2), fill=(58, 58, 64))
        ty += 52

    # 圆形图标 + 条目
    items = ["光学显示模组", "端侧大模型", "整机散热结构"]
    iy = 810
    for i, item in enumerate(items):
        color = [(58, 106, 214), (233, 90, 74), (46, 158, 122)][i]
        draw.ellipse([74, iy + 4, 74 + 34, iy + 38], fill=color)
        draw.text((84, iy + 8), str(i + 1), font=load_font(24), fill=(255, 255, 255))
        draw.text((130, iy + 4), item, font=load_font(32, 4), fill=(34, 34, 40))
        iy += 62

    # 渐变"照片"区域
    photo = Image.new("RGB", (940, 200))
    pdraw = ImageDraw.Draw(photo)
    for x in range(940):
        for_y = x / 940.0
        pdraw.line([(x, 0), (x, 200)],
                   fill=(int(40 + for_y * 120), int(90 + for_y * 60), int(150 - for_y * 40)))
    for i in range(6):
        pdraw.ellipse([60 + i * 150, 40 + (i % 3) * 30, 60 + i * 150 + 70,
                       40 + (i % 3) * 30 + 70], fill=(255, 255, 255, 40),
                      outline=(255, 255, 255), width=3)
    img.paste(photo, (70, 1010))
    draw.text((78, 1018), "Fig.1 供应链结构变化", font=load_font(26, 2), fill=(255, 255, 255))

    # 底部 CTA 按钮 + 页脚
    draw.rounded_rectangle([70, 1246, 420, 1310], radius=32, fill=(233, 90, 74))
    draw.text((132, 1262), "查看完整报告", font=load_font(32, 4), fill=(255, 255, 255))
    draw.text((470, 1268), "friendsun.ai / 2026", font=load_font(26), fill=(150, 150, 158))
    draw.line([(70, 1210), (1010, 1210)], fill=(222, 218, 210), width=2)

    img.save(path, quality=96)
    return path


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples", "sample_infographic.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    print(make_sample(out))
