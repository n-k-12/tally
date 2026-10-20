"""Tally's icon, drawn rather than stored: a lamp whose glow shows the current state."""
from PIL import Image, ImageDraw, ImageFilter

COLOURS = {
    "idle": (138, 132, 124),
    "working": (246, 179, 107),
    "needs": (255, 110, 30),
    "paused": (90, 86, 80),
}


def lamp(colour, size=64, glow=True, backdrop=False):
    s = 4  # draw large and shrink, for smooth edges
    w = size * s
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if backdrop:
        d.rounded_rectangle((0, 0, w - 1, w - 1), radius=w // 5, fill=(24, 22, 20, 255))
    if glow:
        halo = Image.new("RGBA", (w, w), (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse((w * .1, w * .18, w * .9, w * .9), fill=(*colour, 150))
        img.alpha_composite(halo.filter(ImageFilter.GaussianBlur(w * .08)))
    d = ImageDraw.Draw(img)
    # dome: the top half of an ellipse, then the base under it
    d.pieslice((w * .16, w * .26, w * .84, w * .94), 180, 360, fill=colour)
    d.rounded_rectangle((w * .14, w * .58, w * .86, w * .7), radius=w * .05, fill=(72, 78, 90, 255))
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path):
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [lamp(COLOURS["working"], n, backdrop=True) for n in sizes]
    images[-1].save(path, format="ICO", sizes=[(n, n) for n in sizes], append_images=images[:-1])
