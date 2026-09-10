#!/usr/bin/env python3
"""
generate-icons.py  —  Bauhaus app-icon pipeline for bambu-bridge-app

Usage:
    python3 scripts/generate-icons.py <light-source.png> <dark-source.png>

Both sources must be 1254x1254 RGB PNG.
Outputs every Android mipmap / splash and Expo asset image.
No ImageMagick required; Pillow 10.2.0+ only.
"""

import sys
import os
import collections

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT   = os.path.dirname(SCRIPT_DIR)
ASSETS_DIR = os.path.join(APP_ROOT, "assets", "images")
RES_BASE   = os.path.join(APP_ROOT, "android", "app", "src", "main", "res")

# ---------------------------------------------------------------------------
# Canonical colours (measured, verified)
# ---------------------------------------------------------------------------

LIGHT_BG     = (241, 237, 231)   # #F1EDE7 — cream tile bg
DARK_BG      = (17,  17,  19)    # #111113 — near-black tile bg

# Colours OUTSIDE the rounded corners (what we are eliminating)
LIGHT_OUTER  = (255, 255, 254)   # near-white
DARK_OUTER   = (0,   0,   0)     # near-black

# ---------------------------------------------------------------------------
# Tolerances
# ---------------------------------------------------------------------------

FLOOD_TOL = 40   # BFS flood from corners: connected pixels within this sum-abs-diff
FLAT_TOL  = 30   # global flatten: any pixel within this of bg -> exact bg

# ---------------------------------------------------------------------------
# Low-level colour helpers
# ---------------------------------------------------------------------------


def sad3(a, b):
    """Sum of absolute differences across RGB channels."""
    return abs(int(a[0]) - int(b[0])) \
         + abs(int(a[1]) - int(b[1])) \
         + abs(int(a[2]) - int(b[2]))


# ---------------------------------------------------------------------------
# Step 1 — background unification
# ---------------------------------------------------------------------------


def bfs_flood(pix, w, h, seed_x, seed_y, ref_color, tolerance, fill_color):
    """
    4-connected BFS flood-fill starting at (seed_x, seed_y).

    A pixel is eligible if sad3(pixel, ref_color) <= tolerance.
    Eligible pixels are replaced in-place with fill_color.
    Returns count of filled pixels.
    """
    if sad3(pix[seed_x, seed_y], ref_color) > tolerance:
        return 0  # seed itself outside tolerance — nothing to do

    visited = {(seed_x, seed_y)}
    queue   = collections.deque([(seed_x, seed_y)])
    count   = 0

    while queue:
        x, y = queue.popleft()
        if sad3(pix[x, y], ref_color) <= tolerance:
            pix[x, y] = fill_color
            count += 1
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in visited:
                    visited.add((nx, ny))
                    queue.append((nx, ny))

    return count


def unify_background(src_img, bg_color):
    """
    Return a new RGB Image with fully unified flat background.

    1a. BFS flood from each of the 4 corners using the corner pixel's own
        colour as the flood reference (captures the near-white / near-black
        exterior zone); replaces reached pixels with exact bg_color.
    1b. Globally flatten every pixel within FLAT_TOL of bg_color to exact
        bg_color (catches AA fringe pixels the BFS did not reach).
    """
    out = src_img.convert("RGB").copy()
    w, h = out.size
    pix  = out.load()

    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]

    for sx, sy in corners:
        ref = pix[sx, sy]           # each corner may differ slightly
        n   = bfs_flood(pix, w, h, sx, sy, ref, FLOOD_TOL, bg_color)
        print(f"    BFS corner ({sx},{sy})  ref={ref}  filled={n}")

    flat_count = 0
    for y in range(h):
        for x in range(w):
            if pix[x, y] != bg_color and sad3(pix[x, y], bg_color) <= FLAT_TOL:
                pix[x, y] = bg_color
                flat_count += 1
    print(f"    global flatten: {flat_count} pixels adjusted")

    return out


def assert_unification(img, bg_color, old_outer_color, label):
    """
    Post-unification contract:
      - All 4 corner pixels must equal bg_color exactly.
      - No pixel in each 200x200 corner box should be 'near' old_outer_color
        (meaning: sad3(px, old_outer_color) <= FLAT_TOL AND px != bg_color).
    """
    w, h = img.size
    pix  = img.load()

    for cx, cy, name in ((0, 0, "TL"), (w-1, 0, "TR"),
                          (0, h-1, "BL"), (w-1, h-1, "BR")):
        actual = pix[cx, cy]
        assert actual == bg_color, \
            f"{label} corner {name} ({cx},{cy}) = {actual}, expected {bg_color}"

    BOX = 200
    regions = [
        (0,       0,       BOX,   BOX),
        (w - BOX, 0,       w,     BOX),
        (0,       h - BOX, BOX,   h),
        (w - BOX, h - BOX, w,     h),
    ]
    for x0, y0, x1, y1 in regions:
        for y in range(y0, y1):
            for x in range(x0, x1):
                px = pix[x, y]
                if px != bg_color and sad3(px, old_outer_color) <= FLAT_TOL:
                    raise AssertionError(
                        f"{label} corner-box pixel ({x},{y}) = {px} "
                        f"still resembles old outer colour {old_outer_color}"
                    )
    print(f"    [{label}] unification assertions passed")


# ---------------------------------------------------------------------------
# Step 2 — artwork mask
# ---------------------------------------------------------------------------


def compute_artwork_mask(img, bg_color):
    """
    Returns:
      mask  — PIL Image mode 'L': 255 = artwork pixel, 0 = background
      bbox  — (left, top, right, bottom) of the artwork bounding box
    """
    w, h    = img.size
    pix     = img.load()
    mask    = Image.new("L", (w, h), 0)
    mpix    = mask.load()
    min_x = min_y = 999999
    max_x = max_y = 0

    for y in range(h):
        for x in range(w):
            if sad3(pix[x, y], bg_color) > FLAT_TOL:
                mpix[x, y] = 255
                if x < min_x: min_x = x
                if y < min_y: min_y = y
                if x > max_x: max_x = x
                if y > max_y: max_y = y

    if max_x < min_x:
        raise ValueError("Artwork mask is empty — no artwork pixels found")
    bbox = (min_x, min_y, max_x + 1, max_y + 1)
    print(f"    artwork bbox: {bbox}  "
          f"({bbox[2]-bbox[0]} x {bbox[3]-bbox[1]} px)")
    return mask, bbox


# ---------------------------------------------------------------------------
# Canvas compositing
# ---------------------------------------------------------------------------


def artwork_on_transparent(src_img, mask, canvas_size, fill_frac,
                             white_silhouette=False):
    """
    Build a canvas_size x canvas_size RGBA image.

    The artwork bounding box (from mask.getbbox()) is scaled so its
    LARGER dimension equals fill_frac * canvas_size, then centred.

    If white_silhouette=True: RGB = (255,255,255), alpha = thresholded mask.
    Otherwise: RGB from src_img, alpha = thresholded mask.
    """
    bbox = mask.getbbox()
    if bbox is None:
        raise ValueError("Artwork mask is empty")
    left, top, right, bottom = bbox
    art_w = right - left
    art_h = bottom - top

    target = int(round(fill_frac * canvas_size))
    scale  = target / max(art_w, art_h)
    new_w  = max(1, int(round(art_w * scale)))
    new_h  = max(1, int(round(art_h * scale)))

    art_crop  = src_img.crop(bbox).resize((new_w, new_h), Image.LANCZOS)
    mask_crop = mask.crop(bbox).resize((new_w, new_h), Image.LANCZOS)
    # Hard-threshold after resize to avoid semi-transparent AA fringe
    # at the boundary between artwork and transparent bg.
    mask_hard = mask_crop.point(lambda v: 255 if v > 64 else 0)

    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    px = (canvas_size - new_w) // 2
    py = (canvas_size - new_h) // 2

    if white_silhouette:
        layer      = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
        layer_pix  = layer.load()
        mask_h_pix = mask_hard.load()
        for y in range(new_h):
            for x in range(new_w):
                if mask_h_pix[x, y]:
                    layer_pix[x, y] = (255, 255, 255, 255)
    else:
        art_rgba   = art_crop.convert("RGBA")
        art_pix    = art_rgba.load()
        mask_h_pix = mask_hard.load()
        for y in range(new_h):
            for x in range(new_w):
                if not mask_h_pix[x, y]:
                    r, g, b, _ = art_pix[x, y]
                    art_pix[x, y] = (r, g, b, 0)
        layer = art_rgba

    canvas.paste(layer, (px, py), layer)
    return canvas


# ---------------------------------------------------------------------------
# Other image builders
# ---------------------------------------------------------------------------


def full_square(img, size):
    """RGB, flat bg, no rounding."""
    return img.resize((size, size), Image.LANCZOS).convert("RGB")


def solid_rgba(size, rgb):
    """Solid RGBA fill."""
    return Image.new("RGBA", (size, size), rgb + (255,))


def rounded_rect_icon(img, size, r_frac=0.17):
    """
    RGBA: full square scaled, rounded-rect alpha mask applied.
    r_frac = corner radius as fraction of side length.
    """
    out  = img.resize((size, size), Image.LANCZOS).convert("RGBA")
    r    = max(1, int(round(r_frac * size)))
    amsk = Image.new("L", (size, size), 0)
    ImageDraw.Draw(amsk).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=r, fill=255
    )
    out.putalpha(amsk)
    return out


def circle_icon(img, size):
    """RGBA: full square scaled, circular alpha mask."""
    out  = img.resize((size, size), Image.LANCZOS).convert("RGBA")
    amsk = Image.new("L", (size, size), 0)
    ImageDraw.Draw(amsk).ellipse([0, 0, size - 1, size - 1], fill=255)
    out.putalpha(amsk)
    return out


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def save_png(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")


def save_webp(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "WEBP", lossless=True, quality=100)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <light-src.png> <dark-src.png>")
        sys.exit(1)

    light_path, dark_path = sys.argv[1], sys.argv[2]

    # Load
    print("Loading source images...")
    light_raw = Image.open(light_path).convert("RGB")
    dark_raw  = Image.open(dark_path).convert("RGB")
    assert light_raw.size == (1254, 1254), f"Light size wrong: {light_raw.size}"
    assert dark_raw.size  == (1254, 1254), f"Dark size wrong: {dark_raw.size}"
    print(f"  light: {light_raw.size} {light_raw.mode}")
    print(f"  dark:  {dark_raw.size}  {dark_raw.mode}")

    # ----- Step 1: unify -----
    print("\nStep 1: Unifying backgrounds...")
    print("  Light:")
    light_u = unify_background(light_raw, LIGHT_BG)
    assert_unification(light_u, LIGHT_BG, LIGHT_OUTER, "light")

    print("  Dark:")
    dark_u  = unify_background(dark_raw,  DARK_BG)
    assert_unification(dark_u,  DARK_BG,  DARK_OUTER,  "dark")

    # ----- Step 2: masks -----
    print("\nStep 2: Computing artwork masks...")
    print("  Light mask:")
    light_mask, _lbbox = compute_artwork_mask(light_u, LIGHT_BG)
    print("  Dark mask:")
    dark_mask,  _dbbox = compute_artwork_mask(dark_u,  DARK_BG)

    # ----- Step 3: generate -----
    print("\nStep 3: Generating output files...")
    generated = []   # list of (abs_path, PIL.Image)

    def emit_png(img, path):
        save_png(img, path)
        generated.append((path, img))

    def emit_webp(img, path):
        save_webp(img, path)
        generated.append((path, img))

    F58 = 0.58
    F80 = 0.80

    # ---- Expo assets/images/ ----

    emit_png(full_square(light_u, 1024),
             os.path.join(ASSETS_DIR, "icon.png"))

    emit_png(full_square(dark_u, 1024),
             os.path.join(ASSETS_DIR, "icon-dark.png"))

    emit_png(solid_rgba(512, LIGHT_BG),
             os.path.join(ASSETS_DIR, "android-icon-background.png"))

    emit_png(artwork_on_transparent(light_u, light_mask, 512, F58),
             os.path.join(ASSETS_DIR, "android-icon-foreground.png"))

    emit_png(artwork_on_transparent(light_u, light_mask, 432, F58,
                                    white_silhouette=True),
             os.path.join(ASSETS_DIR, "android-icon-monochrome.png"))

    emit_png(artwork_on_transparent(light_u, light_mask, 1024, F80),
             os.path.join(ASSETS_DIR, "splash-icon.png"))

    emit_png(full_square(light_u, 48),
             os.path.join(ASSETS_DIR, "favicon.png"))

    # ---- mipmap adaptive (background / foreground / monochrome) ----
    # sizes: 108 162 216 324 432 (confirmed from existing files)
    ADAPTIVE = [("mdpi", 108), ("hdpi", 162), ("xhdpi", 216),
                ("xxhdpi", 324), ("xxxhdpi", 432)]

    for dpi, sz in ADAPTIVE:
        base = os.path.join(RES_BASE, f"mipmap-{dpi}")

        emit_webp(solid_rgba(sz, LIGHT_BG),
                  os.path.join(base, "ic_launcher_background.webp"))

        emit_webp(artwork_on_transparent(light_u, light_mask, sz, F58),
                  os.path.join(base, "ic_launcher_foreground.webp"))

        emit_webp(artwork_on_transparent(light_u, light_mask, sz, F58,
                                         white_silhouette=True),
                  os.path.join(base, "ic_launcher_monochrome.webp"))

    # ---- mipmap legacy (ic_launcher + ic_launcher_round) ----
    # sizes: 48 72 96 144 192 (confirmed from existing files)
    LEGACY = [("mdpi", 48), ("hdpi", 72), ("xhdpi", 96),
              ("xxhdpi", 144), ("xxxhdpi", 192)]

    for dpi, sz in LEGACY:
        base = os.path.join(RES_BASE, f"mipmap-{dpi}")

        emit_webp(rounded_rect_icon(light_u, sz, r_frac=0.17),
                  os.path.join(base, "ic_launcher.webp"))

        emit_webp(circle_icon(light_u, sz),
                  os.path.join(base, "ic_launcher_round.webp"))

    # ---- splash logos — light (day) and dark (night) ----
    # Sizes read from existing files (do not guess):
    #   mdpi=288, hdpi=432, xhdpi=576, xxhdpi=864, xxxhdpi=1152
    SPLASH = [("mdpi", 288), ("hdpi", 432), ("xhdpi", 576),
              ("xxhdpi", 864), ("xxxhdpi", 1152)]

    for dpi, sz in SPLASH:
        emit_png(artwork_on_transparent(light_u, light_mask, sz, F80),
                 os.path.join(RES_BASE, f"drawable-{dpi}",
                              "splashscreen_logo.png"))

        emit_png(artwork_on_transparent(dark_u, dark_mask, sz, F80),
                 os.path.join(RES_BASE, f"drawable-night-{dpi}",
                              "splashscreen_logo.png"))

    # ---- Summary table ----
    print(f"\n{'=' * 105}")
    print(f"  {'File (relative to project root)':<72}  {'Size':>12}  {'Mode':>6}  {'Bytes':>9}")
    print(f"{'=' * 105}")
    for path, img in sorted(generated, key=lambda t: t[0]):
        rel    = os.path.relpath(path, APP_ROOT)
        # Re-open to get the saved mode (WEBP may differ from creation mode)
        saved  = Image.open(path)
        nbytes = os.path.getsize(path)
        print(f"  {rel:<72}  {str(saved.size):>12}  {saved.mode:>6}  {nbytes:>9}")

    # ---- Count assertions ----
    n_expo     = 7                          # icon, icon-dark, bg, fg, mono, splash, favicon
    n_adaptive = len(ADAPTIVE) * 3          # bg + fg + mono = 15
    n_legacy   = len(LEGACY) * 2            # launcher + round = 10
    n_splash   = len(SPLASH) * 2            # light + dark = 10
    expected   = n_expo + n_adaptive + n_legacy + n_splash   # 42

    print(f"\nFile count breakdown:")
    print(f"  Expo assets/images:          {n_expo}")
    print(f"  Mipmap adaptive (5×3):       {n_adaptive}")
    print(f"  Mipmap legacy (5×2):         {n_legacy}")
    print(f"  Splash logos (5×2):          {n_splash}")
    print(f"  Expected total:              {expected}")
    print(f"  Generated total:             {len(generated)}")

    assert len(generated) == expected, \
        f"Count mismatch: generated {len(generated)}, expected {expected}"
    print(f"  [OK] File count assertion passed")

    print("\nDone.")


if __name__ == "__main__":
    main()
