#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""印章检测（红章 + 灰/黑章圆环）：输入 PNG，输出 bbox 清单 JSON

由插件环境调用，仅返回待人工确认的候选。
"""
import math
import numpy as np
from PIL import Image


def load_gray(path, max_side=900):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    s = min(1.0, max_side / max(w, h))
    if s < 1.0:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    a = np.asarray(im).astype(np.float32)
    return im, a, s


def red_mask_rgb(a, d_min=60, r_min=130, gmax=170):
    """高饱和红 + 低饱和但色相纯正的红（G≈B）两种判据，排除肤色/黄色纸张。"""
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mx = np.maximum(g, b); mn = np.minimum(g, b)
    strong = (mx <= gmax) & ((r - mx) >= d_min) & (r >= r_min)
    pale = (mx <= 238) & ((r - mx) >= 18) & (np.abs(g - b) <= 14) & (r >= 150) & ((r - mn) >= 30)
    return strong | pale


def box_blur(g, k=3):
    if k < 2:
        return g
    c = np.cumsum(np.cumsum(np.pad(g, ((1, 1), (1, 1)), mode="edge"), axis=0), axis=1)
    off = k // 2
    out = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    pad = k // 2
    return np.pad(out, ((pad, pad), (pad, pad)), mode="edge")[: g.shape[0], : g.shape[1]]


def gradients(g):
    gx = np.zeros_like(g); gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1, :] = g[2:, :] - g[:-2, :]
    return gx, gy


def hough_circles(g, rmin, rmax, thr_ratio=0.22, max_edges=60000, step_r=2):
    gb = np.pad(box_blur(g, 3), ((1, 1), (1, 1)), mode="edge")[: g.shape[0], : g.shape[1]]
    gx, gy = gradients(gb)
    mag = np.hypot(gx, gy)
    if mag.max() < 1e-6:
        return []
    edges = mag >= max(16.0, float(np.percentile(mag, 96.5)))
    ys, xs = np.nonzero(edges)
    if len(xs) > max_edges:
        idx = np.random.default_rng(0).choice(len(xs), max_edges, replace=False)
        xs, ys = xs[idx], ys[idx]
    theta = np.arctan2(gy[ys, xs], gx[ys, xs])
    ct, st = np.cos(theta), np.sin(theta)
    H, W = g.shape
    found = []
    for r in range(rmin, rmax + 1, step_r):
        cx = np.rint(xs - r * ct).astype(np.int32)
        cy = np.rint(ys - r * st).astype(np.int32)
        m = (cx >= 0) & (cx < W) & (cy >= 0) & (cy < H)
        if not m.any():
            continue
        flat = cy[m] * W + cx[m]
        acc = np.bincount(flat, minlength=H * W).reshape(H, W).astype(np.float32)
        if acc.max() < max(8.0, thr_ratio * 2 * math.pi * r * 0.18):
            continue
        # 局部极大值
        cand = np.argwhere(acc > max(6, acc.max() * 0.55))
        if len(cand) == 0:
            continue
        for cy0, cx0 in cand:
            found.append((float(acc[cy0, cx0]), int(cx0), int(cy0), int(r)))
    # NMS
    found.sort(reverse=True)
    keep = []
    for score, cx, cy, r in found:
        if any(abs(cx - k[1]) < 0.6 * r and abs(cy - k[2]) < 0.6 * r and abs(r - k[3]) < 0.35 * r
               for k in keep):
            continue
        keep.append([score, cx, cy, r])
        if len(keep) > 40:
            break
    return keep


def ring_coverage(g, cy, cx, r):
    """圆环（r 88%-108%）上的暗线覆盖角度比例：与环外参考亮度的局部对比判定。"""
    if r < 6:
        return 0.0
    n = 120
    angs = np.linspace(0, 2 * math.pi, n, endpoint=False)
    hit = 0
    H, W = g.shape
    for a0 in angs:
        vals, refs = [], []
        for rr in (r * 0.88, r * 0.95, r * 1.0, r * 1.06):
            y = int(round(cy + rr * math.sin(a0))); x = int(round(cx + rr * math.cos(a0)))
            if 0 <= y < H and 0 <= x < W:
                vals.append(g[y, x])
        for rr in (r * 1.32, r * 1.45):
            y = int(round(cy + rr * math.sin(a0))); x = int(round(cx + rr * math.cos(a0)))
            if 0 <= y < H and 0 <= x < W:
                refs.append(g[y, x])
        if not vals:
            continue
        ref = np.median(refs) if refs else np.median(g)
        if min(vals) < ref - 30:
            hit += 1
    return hit / n


def locality_stats(g, a, cy, cx, r):
    y0 = max(0, cy - r); y1 = min(g.shape[0], cy + r + 1)
    x0 = max(0, cx - r); x1 = min(g.shape[1], cx + r + 1)
    sub = g[y0:y1, x0:x1]
    sub_rgb = a[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    if inside.sum() == 0:
        return {}
    page_med = float(np.median(g))
    dark_thr = min(page_med - 12, float(np.percentile(sub, 30)))
    dark = (sub <= dark_thr) & inside
    fill = dark.sum() / max(1, inside.sum())
    # 肤色比例（暖低饱和）
    rr, gg, bb = sub_rgb[:, :, 0], sub_rgb[:, :, 1], sub_rgb[:, :, 2]
    skin = inside & (rr > 130) & (rr - gg >= 28) & (rr - bb >= 55) & (gg - bb <= 45) & \
           (sub_rgb.max(axis=2) > 160) & (sub_rgb.min(axis=2) > 40)
    skin_ratio = skin.sum() / max(1, inside.sum())
    return {"fill": float(fill), "skin": float(skin_ratio)}


def detect(path, max_side=900):
    im, a, s = load_gray(path, max_side)
    g = a.mean(axis=2)
    H, W = g.shape
    out = []
    rr_, gg_, bb_ = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    face = ((rr_ - bb_) >= 12) & ((gg_ - bb_) <= 22) & (rr_ < 238) & (rr_ > 120)
    face_ratio = float(face.mean())
    # A. 红章
    rm = red_mask_rgb(a)
    if rm.sum() > 200:
        # 红色区域聚类（粗网格）
        step = 8
        gw, gh = max(1, W // step), max(1, H // step)
        grid = np.zeros((gh, gw), dtype=np.float32)
        ys, xs = np.nonzero(rm)
        np.add.at(grid, (np.clip(ys // step, 0, gh - 1), np.clip(xs // step, 0, gw - 1)), 1)
        grid = np.clip(grid / (step * step) * 255 * 6, 0, 255)
        cells = {(x, y) for y in range(gh) for x in range(gw) if grid[y, x] >= 60}
        seen = set()
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
            xs_ = [p[0] for p in comp]; ys_ = [p[1] for p in comp]
            x0 = max(0, (min(xs_) - 1) * step); y0 = max(0, (min(ys_) - 1) * step)
            x1 = min(W, (max(xs_) + 2) * step); y1 = min(H, (max(ys_) + 2) * step)
            bw, bh = x1 - x0, y1 - y0
            if bw < 30 or bh < 30 or bw > 0.90 * W or bh > 0.90 * H:
                continue
            sub = rm[y0:y1, x0:x1]
            red_px = int(sub.sum())
            ratio = red_px / max(1, bw * bh)
            if red_px < 220 or not (0.02 <= ratio <= 0.50):
                continue
            # 红色章印与肤色启发式相交；此处仅生成候选，必须由用户确认。
            out.append({"kind": "red", "bbox": [x0, y0, x1, y1], "red_px": red_px, "ratio": round(ratio, 3)})
    # A2. 合并相邻红章框（同一枚章被文字行切断时）
    merged = []
    for b in sorted(out, key=lambda d: d.get("red_px", 0), reverse=True):
        x0, y0, x1, y1 = b["bbox"]
        for m0 in merged:
            ax0, ay0, ax1, ay1 = m0["bbox"]
            gap = 14
            if (x0 - gap <= ax1 and y0 - gap <= ay1 and x1 + gap >= ax0 and y1 + gap >= ay0):
                m0["bbox"] = [min(ax0, x0), min(ay0, y0), max(ax1, x1), max(ay1, y1)]
                m0["red_px"] = m0.get("red_px", 0) + b.get("red_px", 0)
                m0["merged"] = True
                break
        else:
            merged.append(dict(b))
    out = merged

    # B. 灰/黑章：Hough 圆（人像照片页跳过，避免面部误检）
    if face_ratio > 0.92:
        return {"path": path, "scale": s, "size": [im.size[0], im.size[1]], "seals": out,
                "skip_gray_reason": f"near-photo page (face {face_ratio:.2f})"}
    rmin = max(14, int(0.040 * W)); rmax = int(0.185 * W)
    circles = hough_circles(g, rmin, rmax)
    picked = []
    for score, cx, cy, r in circles:
        if cx - r < 4 or cy - r < 4 or cx + r > W - 4 or cy + r > H - 4:
            continue
        cov = ring_coverage(g, cy, cx, r)
        if cov < 0.65:
            continue
        st = locality_stats(g, a, cy, cx, r)
        if st.get("skin", 0) > 0.30:
            continue
        y0_, y1_ = max(0, cy - r), min(H, cy + r + 1)
        x0_, x1_ = max(0, cx - r), min(W, cx + r + 1)
        if float(face[y0_:y1_, x0_:x1_].mean()) > 0.45:
            continue
        if not (0.008 <= st.get("fill", 0) <= 0.60):
            continue
        # 与已选圆去重
        if any(abs(cx - p[0]) < 0.5 * r and abs(cy - p[1]) < 0.5 * r for p in picked):
            continue
        # 与红章去重
        dup = False
        for bb in out:
            bx0, by0, bx1, by1 = bb["bbox"]
            if bx0 - 0.3 * (bx1 - bx0) < cx < bx1 + 0.3 * (bx1 - bx0) and \
               by0 - 0.3 * (by1 - by0) < cy < by1 + 0.3 * (by1 - by0):
                dup = True; break
        if dup:
            continue
        picked.append((cx, cy, r))
        out.append({"kind": "gray", "bbox": [int(cx - r), int(cy - r), int(cx + r), int(cy + r)],
                    "score": round(float(score), 1), "coverage": round(cov, 2), **st})
    return {"path": path, "scale": s, "size": [im.size[0], im.size[1]], "seals": out}


