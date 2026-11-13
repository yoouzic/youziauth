"""lptiyu 验证码切分/槽位归一的纪律测试(合成图, 不碰网络)。"""
from __future__ import annotations

import io
import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from lptiyu_captcha import (CaptchaSegmentationError, compose_slots,
                            binarize, prepare, split_digits)


def _font(size=36):
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1 可缩放
    except TypeError:                              # pragma: no cover
        return ImageFont.load_default()


def synth_canvas(digits, positions=None, size=(200, 62)):
    """画一张「4 个深色数字 + 浅色干扰」的合成验证码, 返回 PNG 字节。"""
    image = Image.new('L', size, 245)
    draw = ImageDraw.Draw(image)
    font = _font()
    # 浅色干扰字符(灰度 160-220): 会被阈值切掉
    rng = np.random.default_rng(sum(ord(c) for c in digits))
    for _ in range(30):
        x = int(rng.integers(0, size[0] - 10))
        y = int(rng.integers(0, size[1] - 16))
        draw.text((x, y), chr(ord('a') + int(rng.integers(0, 26))),
                  fill=int(rng.integers(160, 220)), font=font)
    # 代码字符(深色 <150), 默认均匀摆放
    positions = positions or [(8 + i * 48, 6) for i in range(4)]
    for digit, (x, y) in zip(digits, positions):
        draw.text((x, y), digit, fill=60, font=font)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def digit_boxes(png):
    binary = binarize(Image.open(io.BytesIO(png)))
    return split_digits(binary)


class SplitTests(unittest.TestCase):
    def test_four_evenly_spaced_digits(self):
        png = synth_canvas('1234')
        boxes = digit_boxes(png)
        self.assertEqual(len(boxes), 4)
        centers = [(box[0] + box[1]) / 2 for box in boxes]
        self.assertTrue(all(b > a for a, b in zip(centers, centers[1:])),
                        '按 x 从左到右排序')

    def test_touching_digits_get_split(self):
        # 两个数字几乎贴在一起: 仍要切出 4 份
        png = synth_canvas('5678', positions=[(10, 8), (44, 8), (76, 8), (150, 8)])
        boxes = digit_boxes(png)
        self.assertEqual(len(boxes), 4)

    def test_compose_slots_places_four_glyphs(self):
        png = synth_canvas('9012')
        canvas = prepare(png)
        self.assertEqual(canvas.shape, (30, 100))
        # 每个槽位都该有深色像素(字形进槽了)
        for slot in range(4):
            column = canvas[:, slot * 25:(slot + 1) * 25]
            self.assertGreater(int((column < 100).sum()), 8, f'槽 {slot} 是空的')

    def test_binarize_kills_light_noise(self):
        png = synth_canvas('3456')
        binary = binarize(Image.open(io.BytesIO(png)))
        # 干扰字符(160-220)不该留在二值图里; 数字(60)留在
        self.assertLess(int(binary.sum()), int(binary.size * 0.25))

    def test_degenerate_image_raises_instead_of_guessing(self):
        blank = Image.new('L', (200, 62), 245)
        buffer = io.BytesIO()
        blank.save(buffer, format='PNG')
        with self.assertRaises(CaptchaSegmentationError):
            prepare(buffer.getvalue())


if __name__ == '__main__':
    unittest.main()
