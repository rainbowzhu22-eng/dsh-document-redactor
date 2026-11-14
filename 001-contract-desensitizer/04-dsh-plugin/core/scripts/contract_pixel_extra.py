#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附加脱敏检测：证件照片、红手印、签名行、跨页折断案号片段（插件内纯检测模块）"""
import re
from pathlib import Path
import numpy as np
from PIL import Image

SIGN_WORDS = ("签字", "签名", "盖章", "签章", "签收", "手印", "捺印", "指印", "按印")


def load(path, max_side=900):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    s = min(1.0, max_side / max(w, h))
    if s < 1.0:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    a = np.asarray(im).astype(np.float32)
    return im, a, s


def skin_mask(a):
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    return ((r > 100) & (r > g) & (g >= b - 6) & ((r - b) >= 18) & ((r - g) >= 5) &
            ((r - g) <= 95) & ((r - b) <= 115))


def red_mask(a):
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mx = np.maximum(g, b)
    return (mx <= 240) & ((r - mx) >= 25) & (r >= 120)


def blocks_from_mask(mask, W, H, step, cell_thr):
    gw, gh = max(1, W // step), max(1, H // step)
    grid = np.zeros((gh, gw), dtype=np.float32)
    ys, xs = np.nonzero(mask)
    if len(xs):
        np.add.at(grid, (np.clip(ys // step, 0, gh - 1), np.clip(xs // step, 0, gw - 1)), 1)
    grid = grid / (step * step)
    cells = {(x, y) for y in range(gh) for x in range(gw) if grid[y, x] >= cell_thr}
    seen = set(); out = []
    for c in list(cells):
        if c in seen:
            continue
        stack = [c]; seen.add(c); comp = []
        while stack:
            x, y = stack.pop(); comp.append((x, y))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in cells and n not in seen:
                        seen.add(n); stack.append(n)
        out.append(comp)
    return out


def line_bbox(line, W, H):
    boxes = line.get("boxes") or []
    if not boxes:
        return None
    xs0 = min(b[0] for b in boxes); ys0 = min(b[1] for b in boxes)
    xs1 = max(b[2] for b in boxes); ys1 = max(b[3] for b in boxes)
    return [round(xs0 * W), round(ys0 * H), round(xs1 * W), round(ys1 * H)]


def char_span_bbox(line, s_idx, e_idx, W, H):
    boxes = line.get("boxes") or []
    text = line.get("text") or ""
    if not boxes:
        return None
    if len(boxes) == len(text):
        sel = boxes[s_idx:e_idx]
        if not sel:
            return None
        return [round(min(b[0] for b in sel) * W), round(min(b[1] for b in sel) * H),
                round(max(b[2] for b in sel) * W), round(max(b[3] for b in sel) * H)]
    n = len(boxes)
    b0 = int(s_idx * n / max(1, len(text))); b1 = max(b0 + 1, int(e_idx * n / max(1, len(text))))
    sel = boxes[b0:b1]
    if not sel:
        return None
    return [round(min(b[0] for b in sel) * W), round(min(b[1] for b in sel) * H),
            round(max(b[2] for b in sel) * W), round(max(b[3] for b in sel) * H)]




def _line_boxes(lines, W, H):
    out = []
    for ln in lines:
        bb = line_bbox(ln, W, H)
        if bb:
            out.append(bb)
    return out


def _bbox_gap(a, b):
    dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return max(dx, dy)


def handwriting_boxes(a, lines, W, H, s2):
    """找"远离所有 OCR 文字行"的孤立墨迹块，视作手写签名/手写字迹。"""
    gy = a.mean(axis=2)
    dark = gy < 128
    H2, W2 = dark.shape
    # 降采样 2 倍做连通域，控制耗时
    import numpy as np
    d2 = dark[::2, ::2]
    visited = np.zeros_like(d2, dtype=bool)
    boxes = []
    ys, xs = np.nonzero(d2)
    from collections import deque
    ocr = _line_boxes(lines, W, H)
    ocr_scaled = [[v * s2 for v in bb] for bb in ocr]
    for y0, x0 in zip(ys, xs):
        if visited[y0, x0]:
            continue
        q = deque([(y0, x0)]); visited[y0, x0] = True
        pts = []
        while q:
            y, x = q.popleft()
            pts.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < d2.shape[0] and 0 <= xx < d2.shape[1] and d2[yy, xx] and not visited[yy, xx]:
                        visited[yy, xx] = True
                        q.append((yy, xx))
            if len(pts) > 200000:
                break
        n = len(pts)
        if n < 60 or n > 60000:
            continue
        xs_ = [p[1] for p in pts]; ys_ = [p[0] for p in pts]
        x0, y0b = min(xs_) * 2, min(ys_) * 2
        x1, y1b = (max(xs_) + 1) * 2, (max(ys_) + 1) * 2
        w, h = x1 - x0, y1b - y0b
        if w < 12 or h < 12:
            continue
        ar = w / max(1, h)
        fill = n / max(1, w * h / 4)
        if ar > 9 or ar < 1 / 9:            # 直线/表格线
            continue
        if fill > 0.82 and max(w, h) > 70:  # 实心黑块（已有遮盖）
            continue
        if fill > 0.55 and 40 <= max(w, h) <= 340 and 0.7 <= ar <= 1.4:  # 二维码类
            continue
        # 已遮白区 / 红色章残留 跳过
        crop = dark[y0b // 2:max(y0b // 2 + 1, y1b // 2), x0 // 2:max(x0 // 2 + 1, x1 // 2)]
        if crop.size and crop.mean() > 0.92:
            continue
        rgb = a[y0b:y1b, x0:x1]
        if rgb.size:
            rr = rgb[:, :, 0]; gg = rgb[:, :, 1]; bb = rgb[:, :, 2]
            red_ratio = ((rr - np.maximum(gg, bb) >= 30) & (rr >= 120)).mean()
            if red_ratio > 0.30:
                continue
        # 远离 OCR 行
        bb = [x0, y0b, x1, y1b]
        if ocr_scaled:
            gap = min(_bbox_gap(ob, bb) for ob in ocr_scaled)
        else:
            gap = 9999
        if gap < 28:
            continue
        # 面积与尺寸限制
        if not (300 <= n * 4 <= 40000):
            continue
        if max(w, h) > 0.6 * (W * s2):
            continue
        boxes.append({"bbox": [round(x0 / s2), round(y0b / s2), round(x1 / s2), round(y1b / s2)], "area": int(n), "gap": int(gap)})
    # 合并相邻
    boxes.sort(key=lambda b: -b["area"])
    merged = []
    for b in boxes:
        for m in merged:
            if _bbox_gap(m["bbox"], b["bbox"]) < 60:
                m["bbox"] = [min(m["bbox"][0], b["bbox"][0]), min(m["bbox"][1], b["bbox"][1]),
                             max(m["bbox"][2], b["bbox"][2]), max(m["bbox"][3], b["bbox"][3])]
                m["area"] += b["area"]
                break
        else:
            merged.append(dict(b))
    return merged[:8]


def detect(png, W, H, lines):
    im, a, s2 = load(png)
    gw, gh = im.size
    res = {"photos": [], "prints": [], "signs": [], "handwriting": []}
    # 1) 证件照片
    sm = skin_mask(a)
    total = sm.sum() / max(1, sm.size)
    comps = blocks_from_mask(sm, gw, gh, 10, 0.5)
    for comp in comps:
        xs = [c[0] for c in comp]; ys = [c[1] for c in comp]
        x0, y0 = min(xs) * 10 / s2, min(ys) * 10 / s2
        x1, y1 = (max(xs) + 1) * 10 / s2, (max(ys) + 1) * 10 / s2
        w, h = x1 - x0, y1 - y0
        if w < 0.035 * W or h < 0.03 * H or w > 0.6 * W or h > 0.95 * H:
            continue
        if not (0.3 <= w / max(1, h) <= 3.0):
            continue
        area_ratio = (len(comp) * 100 / (s2 * s2)) / max(1, w * h)
        if area_ratio < 0.18:
            continue
        res["photos"].append({"bbox": [round(x0), round(y0), round(x1), round(y1)],
                              "skin_page": round(float(total), 2)})
    # 2) 红手印
    rm = red_mask(a)
    for comp in blocks_from_mask(rm, gw, gh, 10, 0.28):
        xs = [c[0] for c in comp]; ys = [c[1] for c in comp]
        x0, y0 = min(xs) * 10 / s2, min(ys) * 10 / s2
        x1, y1 = (max(xs) + 1) * 10 / s2, (max(ys) + 1) * 10 / s2
        w, h = x1 - x0, y1 - y0
        if w < 40 or h < 40 or w > 0.5 * W or h > 0.5 * H:
            continue
        sub = rm[int(y0 * s2):int(y1 * s2), int(x0 * s2):int(x1 * s2)]
        ratio = float(sub.sum()) / max(1, sub.size)
        if not (0.03 <= ratio <= 0.55):
            continue
        res["prints"].append({"bbox": [round(x0), round(y0), round(x1), round(y1)], "ratio": round(ratio, 2)})
    # 3) OCR：签名行 + 案号片段
    res["handwriting"] = handwriting_boxes(a, lines, W, H, s2)
    for idx, line in enumerate(lines):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        bb = line_bbox(line, W, H)
        if bb is None:
            continue
        # 签名/盖章标签
        for w0 in SIGN_WORDS:
            pos = text.find(w0)
            if pos >= 0:
                end = pos + len(w0)
                tail = len(text)
                while tail > end and text[tail - 1] in "：: 　":
                    tail -= 1
                tag_bb = char_span_bbox(line, max(0, end - 1), end, W, H) or bb
                start_x = tag_bb[2]
                end_x = min(round(W * 0.96), start_x + round(W * 0.55))
                res["signs"].append({"label": w0, "text": text[:40], "line_bbox": bb,
                                     "mask": [start_x, max(0, bb[1] - 6), end_x, min(H, bb[3] + 6)]})
                break
    return res

