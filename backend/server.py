"""图片炸开编辑器 · 后端服务。

启动：./venv/bin/python backend/server.py  （默认 http://127.0.0.1:8770）
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import sys
import time
import uuid
from typing import Dict, List, Optional

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BACKEND_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

FRONTEND_DIR = os.path.join(PROJECT_DIR, "frontend")
WORKSPACE_DIR = os.path.join(PROJECT_DIR, "workspace")
SAMPLES_DIR = os.path.join(PROJECT_DIR, "samples")
# 成品统一输出到项目根目录（cursor_team）
OUTPUT_DIR = os.path.dirname(PROJECT_DIR)

os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(SAMPLES_DIR, exist_ok=True)

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import exploder
import ocr_engines

app = FastAPI(title="Image Explode Editor", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

@app.middleware("http")
async def no_cache_frontend(request, call_next):
    """前端代码禁用缓存。

    HTML 上加版本号是没用的：index.html 里的 <script src="/static/js/main.js"> 是固定路径，
    浏览器照旧给缓存里的旧文件，于是「改了代码没生效」，很容易误判成逻辑 bug。
    炸开产物（/files）反而可以放心缓存——每个任务一个目录，内容不会变。
    """
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._\- ]+")


def job_path(job_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{6,32}", job_id or ""):
        raise HTTPException(status_code=400, detail="非法的任务 ID")
    path = os.path.join(WORKSPACE_DIR, job_id)
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    return path


def find_source(job_dir: str) -> str:
    for name in os.listdir(job_dir):
        if name.startswith("source"):
            return os.path.join(job_dir, name)
    raise HTTPException(status_code=404, detail="找不到原图")


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health() -> Dict:
    return {
        "ok": True,
        "ocrEngines": ocr_engines.available_engine_names(),
        "outputDir": OUTPUT_DIR,
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> Dict:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400,
                            detail=f"不支持的格式 {ext}，请上传 PNG/JPG/WEBP/BMP/TIFF")
    data = await file.read()
    if len(data) > 40 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片超过 40MB")

    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(WORKSPACE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    src = os.path.join(job_dir, f"source{ext}")
    with open(src, "wb") as fh:
        fh.write(data)

    from PIL import Image
    try:
        with Image.open(src) as im:
            width, height = im.size
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"图片无法解析：{exc}")

    stem = SAFE_NAME.sub("", os.path.splitext(os.path.basename(file.filename or "image"))[0])
    return {"jobId": job_id, "width": width, "height": height,
            "originalName": stem or "image", "bytes": len(data)}


@app.post("/api/explode")
def do_explode(payload: Dict = Body(...)) -> Dict:
    job_id = payload.get("jobId", "")
    job_dir = job_path(job_id)
    src = find_source(job_dir)

    options = payload.get("options") or {}
    try:
        layout = exploder.explode(
            image_path=src,
            out_dir=WORKSPACE_DIR,
            job_id=job_id,
            ocr_engine=options.get("ocrEngine", "auto"),
            detect_text=bool(options.get("detectText", True)),
            detect_shapes=bool(options.get("detectShapes", True)),
            detect_images=bool(options.get("detectImages", True)),
            strength=options.get("strength", "standard"),
            max_side=int(options.get("maxSide", exploder.MAX_ANALYZE_SIDE)),
            min_text_conf=float(options.get("minTextConf", 0.3)),
        )
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"炸开失败：{exc}")
    return layout


@app.post("/api/extract")
def do_extract(payload: Dict = Body(...)) -> Dict:
    job_id = payload.get("jobId", "")
    job_dir = job_path(job_id)
    src = find_source(job_dir)
    rect = payload.get("rect") or []
    if len(rect) != 4:
        raise HTTPException(status_code=400, detail="rect 需为 [x, y, w, h]")
    mode = payload.get("mode", "grabcut")
    try:
        layer = exploder.extract_region(src, job_dir, tuple(float(v) for v in rect), mode=mode)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"提取失败：{exc}")
    return layer


@app.post("/api/reread-text")
def reread_text(payload: Dict = Body(...)) -> Dict:
    """把一行字单独放大重认一遍，专治糊字被认错。

    整图 OCR 是按整张图的尺度跑的，图小或字被压花时容易读错（「光刻机」读成「光翅机」）。
    单独把这一块裁出来放大到足够高，再让所有可用引擎各认一次择优，识别率明显更高。
    """
    job_id = payload.get("jobId", "")
    job_dir = job_path(job_id)
    src = find_source(job_dir)
    rect = payload.get("rect") or []
    if len(rect) != 4:
        raise HTTPException(status_code=400, detail="rect 需为 [x, y, w, h]")
    try:
        result = exploder.reread_text(
            src, tuple(float(v) for v in rect),
            engine=payload.get("engine", "auto"),
            target_height=float(payload.get("targetHeight", 96)),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"重认失败：{exc}")
    return result


@app.post("/api/erase")
def do_erase(payload: Dict = Body(...)) -> Dict:
    """按矩形列表重算干净背景，用于手动补擦除。"""
    job_id = payload.get("jobId", "")
    job_dir = job_path(job_id)
    src = find_source(job_dir)
    rects = payload.get("rects") or []
    if not rects:
        raise HTTPException(status_code=400, detail="rects 不能为空")
    name = exploder.rebuild_clean(src, job_dir, rects)
    return {"clean": name}


@app.post("/api/save-image")
def save_image(payload: Dict = Body(...)) -> Dict:
    """把前端导出的图片写入项目根目录。"""
    data_url = payload.get("dataUrl") or ""
    filename = SAFE_NAME.sub("", payload.get("filename") or "")
    if not filename:
        filename = f"explode_export_{time.strftime('%Y%m%d_%H%M%S')}.png"
    if not data_url.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="dataUrl 格式不正确")

    header, _, b64 = data_url.partition(",")
    ext = ".png"
    if "jpeg" in header or "jpg" in header:
        ext = ".jpg"
    elif "webp" in header:
        ext = ".webp"
    elif "svg" in header:
        ext = ".svg"
    if not filename.lower().endswith(ext):
        filename = os.path.splitext(filename)[0] + ext

    try:
        blob = base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"图片数据解码失败：{exc}")

    target = os.path.join(OUTPUT_DIR, filename)
    stem, suffix = os.path.splitext(target)
    counter = 1
    while os.path.exists(target):
        target = f"{stem}_{counter}{suffix}"
        counter += 1
    with open(target, "wb") as fh:
        fh.write(blob)
    return {"path": target, "bytes": len(blob), "name": os.path.basename(target)}


@app.post("/api/save-project")
def save_project(payload: Dict = Body(...)) -> Dict:
    job_id = payload.get("jobId", "")
    job_dir = job_path(job_id)
    doc = payload.get("doc")
    if doc is None:
        raise HTTPException(status_code=400, detail="缺少 doc")
    path = os.path.join(job_dir, "project.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    return {"ok": True, "path": path}


@app.get("/api/project/{job_id}")
def load_project(job_id: str) -> Dict:
    job_dir = job_path(job_id)
    path = os.path.join(job_dir, "project.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="该任务没有已保存的工程")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/api/samples")
def list_samples() -> Dict:
    items = []
    if os.path.isdir(SAMPLES_DIR):
        for name in sorted(os.listdir(SAMPLES_DIR)):
            if os.path.splitext(name)[1].lower() in ALLOWED_EXT:
                items.append({"name": name, "url": f"/samples/{name}"})
    return {"samples": items}


@app.post("/api/use-sample")
def use_sample(payload: Dict = Body(...)) -> Dict:
    name = os.path.basename(payload.get("name") or "")
    src = os.path.join(SAMPLES_DIR, name)
    if not os.path.isfile(src):
        raise HTTPException(status_code=404, detail="示例图不存在")
    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(WORKSPACE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    ext = os.path.splitext(name)[1].lower()
    shutil.copy2(src, os.path.join(job_dir, f"source{ext}"))
    from PIL import Image
    with Image.open(src) as im:
        width, height = im.size
    return {"jobId": job_id, "width": width, "height": height,
            "originalName": os.path.splitext(name)[0]}


@app.get("/files/{job_id}/{path:path}")
def get_file(job_id: str, path: str):
    job_dir = job_path(job_id)
    target = os.path.normpath(os.path.join(job_dir, path))
    if not target.startswith(job_dir) or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, headers={"Cache-Control": "no-cache"})


MODULE_IMPORT = re.compile(r"""((?:from|import)\s*\(?\s*['"])(\./[^'"?]+\.js)(['"])""")


@app.get("/static/js/{name}")
def frontend_js(name: str):
    """前端模块：把相对 import 也带上版本号。

    浏览器不会把父模块 URL 上的 query 继承给它 import 的兄弟模块，所以只给 index.html 里的
    main.js 打版本号是不够的——state.js、render.js 这些照旧走缓存。
    """
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.js", name):
        raise HTTPException(status_code=404, detail="文件不存在")
    path = os.path.join(FRONTEND_DIR, "js", name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="文件不存在")
    with open(path, "r", encoding="utf-8") as fh:
        code = fh.read()
    version = frontend_version()
    code = MODULE_IMPORT.sub(
        lambda m: f"{m.group(1)}{m.group(2)}?v={version}{m.group(3)}", code)
    return Response(code, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-store, must-revalidate"})


def frontend_version() -> str:
    """前端所有源文件里最新的修改时间，用作静态资源版本号。"""
    newest = 0.0
    for root, _dirs, files in os.walk(FRONTEND_DIR):
        for name in files:
            if os.path.splitext(name)[1].lower() in (".js", ".css", ".html"):
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
    return str(int(newest))


@app.get("/")
def index():
    """首页现场给静态资源打版本号。

    只发 Cache-Control: no-store 挡不住浏览器：Electron 内嵌的 WebView 会把 ES 模块留在
    内存缓存里照旧复用，于是改完 render.js 刷新页面跑的还是旧代码——排查时极易误判成
    逻辑 bug。带上按文件修改时间生成的版本号，URL 一变缓存自然失效。

    注意 main.js 里 import 的那些同级模块也会跟着带上同一个 query，浏览器因此一并重取。
    """
    path = os.path.join(FRONTEND_DIR, "index.html")
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    version = frontend_version()
    html = re.sub(r'(href|src)="(/static/[^"?]+)"',
                  lambda m: f'{m.group(1)}="{m.group(2)}?v={version}"', html)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


app.mount("/samples", StaticFiles(directory=SAMPLES_DIR), name="samples")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"可用 OCR 引擎: {ocr_engines.available_engine_names()}")
    print(f"导出目录: {OUTPUT_DIR}")
    print(f"打开浏览器访问: http://{args.host}:{args.port}/")
    uvicorn.run("server:app" if args.reload else app, host=args.host,
                port=args.port, reload=args.reload)
