"""lptiyu H5 验证码识别: 二值化 -> 列切分 -> 槽位归一 -> 4 位数字。

lptiyu 的验证码(200x62 彩色)与 IDM 的(100x30 固定列)不同: 数字的位置、
字体、旋转都随机, 还有大量浅色干扰字符。策略:

  1. Otsu 二值化 —— 代码字符是深色饱和的, 干扰字符是浅色的, 一刀切掉;
  2. 列投影切分 —— 数字是密集列簇, 合并碎片、拆开粘连, 凑出 4 列;
  3. 槽位归一 —— 每个字形缩放后贴进固定槽位, 变成 IDM 同款「固定列」图;
  4. 用 captcha_ocr 同结构的 4 头 CNN(weights: data/captcha/lptiyu_model.npz,
     由 tools/captcha/train.py 训练)读出 4 位数字。

切分凑不出 4 列时抛 CaptchaSegmentationError —— 调用方换一张验证码重试即可
(实测这类样本占比很小, 且服务端验证码不要钱)。
"""
from __future__ import annotations

import io

from PIL import Image
import numpy as np

W, H = 100, 30
# 全分辨率中间画布: lptiyu 验证码是 200x62, 切分与槽位都在这个尺度做,
# 最后整图缩到 (W,H) 喂模型 —— 字形细节比直接在 100x30 上拼多一倍。
CANVAS_W, CANVAS_H = 200, 60
SLOT_W = 48                      # 4 槽 × 48 ≈ 192, 两侧留边
GLYPH_W, GLYPH_H = 42, 54        # 槽内字形缩放上限(保比例)
FG, BG = 50, 235                 # IDM 分布: 深字浅底
# 代码字符灰度 <150(深色饱和), 干扰字符 160-220, 背景 220+ —— 固定阈值比
# Otsu 干净(Otsu 会把「背景 vs 一切非背景」切开, 留下大量浅色干扰字)。
THRESHOLD = 145
DILATE_X = 2                     # 水平膨胀: 熔合同一字形的断裂笔画
MIN_AREA = 50                    # 小于此像素数的组件当残渣

# 归一化常数(与 captcha_ocr 一致): 槽位画布就按 IDM 分布合成(字 50/底 235),
# 归一化后落在与 IDM 模型相同的数值区间。
PIX_MEAN, PIX_STD = 158.0, 60.0


class CaptchaSegmentationError(ValueError):
    """列切分凑不出 4 个字符 —— 换一张验证码重试。"""


def otsu_threshold(gray: np.ndarray) -> int:
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    sum_all = float(np.dot(np.arange(256), hist))
    best, threshold, w_b, sum_b = -1.0, 128, 0, 0
    for t in range(256):
        w_b += int(hist[t])
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * int(hist[t])
        var = w_b * w_f * ((sum_b / w_b) - (sum_all - sum_b) / w_f) ** 2
        if var > best:
            best, threshold = var, t
    return threshold


def binarize(image: Image.Image) -> np.ndarray:
    """验证码 -> (62,200) 二值图(fg=1)。固定阈值, 干扰字符(浅色)被切掉。"""
    gray = np.asarray(image.convert('L'), dtype=np.uint8)
    return (gray < THRESHOLD).astype(np.uint8)


def _components(binary: np.ndarray, min_area: int):
    """8 邻域连通域, 返回按 x 排序的包围盒 [x0,x1,y0,y1]。纯 numpy BFS。"""
    h, w = binary.shape
    labels = np.zeros((h, w), dtype=np.int32)
    current = 0
    boxes = []
    for y in range(h):
        for x in range(w):
            if binary[y, x] and labels[y, x] == 0:
                current += 1
                stack = [(y, x)]
                labels[y, x] = current
                x0 = x1 = x
                y0 = y1 = y
                area = 0
                while stack:
                    cy, cx = stack.pop()
                    area += 1
                    x0, x1 = min(x0, cx), max(x1, cx)
                    y0, y1 = min(y0, cy), max(y1, cy)
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < h and 0 <= nx < w \
                                    and binary[ny, nx] and labels[ny, nx] == 0:
                                labels[ny, nx] = current
                                stack.append((ny, nx))
                if area >= min_area:
                    boxes.append([x0, x1, y0, y1])
    boxes.sort()
    return boxes


def _dilate_x(binary: np.ndarray, k: int) -> np.ndarray:
    out = binary.copy()
    for s in range(1, k + 1):
        out[:, s:] |= binary[:, :-s]
        out[:, :-s] |= binary[:, s:]
    return out


def _merge_overlapping(boxes):
    merged = []
    for box in sorted(boxes):
        if merged and box[0] <= merged[-1][1] - 2:
            last = merged[-1]
            last[0], last[1] = min(last[0], box[0]), max(last[1], box[1])
            last[2], last[3] = min(last[2], box[2]), max(last[3], box[3])
        else:
            merged.append(list(box))
    return merged


def _fix_count(boxes, profile, count=4):
    """组件数凑成 count: 多了并掉 x 间隙最小的一对, 少了从最宽处切一刀。"""
    boxes = [list(box) for box in boxes]
    while len(boxes) > count:
        gaps = [boxes[i + 1][0] - boxes[i][1] for i in range(len(boxes) - 1)]
        i = int(np.argmin(gaps))
        boxes[i][1] = max(boxes[i][1], boxes[i + 1][1])
        boxes[i][0] = min(boxes[i][0], boxes[i + 1][0])
        boxes[i][2] = min(boxes[i][2], boxes[i + 1][2])
        boxes[i][3] = max(boxes[i][3], boxes[i + 1][3])
        del boxes[i + 1]
    while len(boxes) < count:
        if not boxes:
            raise CaptchaSegmentationError('二值图里没有任何字符组件')
        widths = [b[1] - b[0] + 1 for b in boxes]
        i = int(np.argmax(widths))
        x0, x1 = boxes[i][0], boxes[i][1]
        if x1 - x0 < 2:
            break
        section = profile[x0:x1 + 1]
        if len(section) <= 2:
            break
        cut = x0 + 1 + int(np.argmin(section[1:-1]))
        left = [x0, cut, boxes[i][2], boxes[i][3]]
        right = [cut + 1, x1, boxes[i][2], boxes[i][3]]
        boxes[i:i + 1] = [left, right]
    if len(boxes) != count:
        raise CaptchaSegmentationError(
            f'列切分只得 {len(boxes)} 个字符簇(需要 {count})')
    return boxes


def _split_wide(boxes, profile, factor=1.5, rounds=8):
    """宽度明显超过中位数的块按内部最低谷先切开(膨胀把相邻数字熔成一块的补救)。"""
    boxes = [list(box) for box in boxes]
    for _ in range(rounds):
        widths = [b[1] - b[0] + 1 for b in boxes]
        median = float(np.median(widths)) if widths else 0
        split_at = None
        for i, box in enumerate(boxes):
            width = box[1] - box[0] + 1
            if width > factor * median and width >= 4:
                section = profile[box[0]:box[1] + 1]
                if len(section) > 2:
                    cut = box[0] + 1 + int(np.argmin(section[1:-1]))
                    boxes[i:i + 1] = [[box[0], cut, box[2], box[3]],
                                      [cut + 1, box[1], box[2], box[3]]]
                    split_at = i
                    break
        if split_at is None:
            break
    return boxes


def split_digits(binary: np.ndarray, count: int = 4):
    """二值图 -> count 个字符包围盒 [(x0,x1,y0,y1), ...](按 x 排序)。"""
    dilated = _dilate_x(binary, DILATE_X)
    boxes = _merge_overlapping(_components(dilated, MIN_AREA))
    profile = dilated.sum(axis=0)
    boxes = _split_wide(boxes, profile)
    return _fix_count(boxes, profile, count)


def compose_canvas(binary: np.ndarray, boxes=None) -> np.ndarray:
    """二值图 -> 槽位归一的 (60,200) 画布(字 FG / 底 BG), 未缩放到模型输入尺寸。

    旋转增强等需要在全分辨率画布上做的变换, 拿这个; 喂模型请走 compose_slots。
    """
    if boxes is None:
        boxes = split_digits(binary)
    canvas = np.full((CANVAS_H, CANVAS_W), BG, dtype=np.uint8)
    for slot, (x0, x1, y0, y1) in enumerate(boxes[:4]):
        glyph = binary[y0:y1 + 1, x0:x1 + 1]
        gh, gw = glyph.shape
        if not gh or not gw:
            continue
        scale = min(GLYPH_W / gw, GLYPH_H / gh)
        new_w = max(1, min(GLYPH_W, int(round(gw * scale))))
        new_h = max(1, min(GLYPH_H, int(round(gh * scale))))
        resized = np.asarray(Image.fromarray(
            np.where(glyph, FG, BG).astype(np.uint8)
        ).resize((new_w, new_h), Image.BILINEAR))
        glyph = np.where(resized < 128, FG, BG).astype(np.uint8)
        x_start = 4 + slot * SLOT_W
        px = x_start + (SLOT_W - new_w) // 2
        py = (CANVAS_H - new_h) // 2
        canvas[py:py + new_h, px:px + new_w] = glyph
    return canvas


def compose_slots(binary: np.ndarray, boxes=None) -> np.ndarray:
    """槽位画布缩放到模型输入 (30,100)。"""
    canvas = compose_canvas(binary, boxes)
    return np.asarray(Image.fromarray(canvas).resize((W, H), Image.BILINEAR))


def prepare(png: bytes) -> np.ndarray:
    """验证码 PNG -> 槽位归一画布(模型输入 100x30)。"""
    return compose_slots(binarize(Image.open(io.BytesIO(png))))


class SlotCaptchaSolver:
    """槽位归一 + 4 头 CNN(weights 与 captcha_ocr.CaptchaModel 兼容)。"""

    def __init__(self, model_path, min_conf: float = 0.0):
        from captcha_ocr import CaptchaModel
        with np.load(model_path) as f:
            self.model = CaptchaModel({k: f[k] for k in f.files})
        self.min_conf = float(min_conf)

    def read(self, png: bytes) -> tuple[str, float]:
        canvas = prepare(png)
        x = ((canvas.astype(np.float32) - PIX_MEAN) / PIX_STD)[None, None, :, :]
        probs = self.model.predict_probs(x)[0]
        digits = probs.argmax(axis=1)
        conf = float(probs.max(axis=1).min())
        return ''.join(str(d) for d in digits), conf

    def solve(self, png: bytes) -> tuple[str | None, float]:
        try:
            code, conf = self.read(png)
        except CaptchaSegmentationError:
            return None, 0.0
        return (code if conf >= self.min_conf else None), conf
