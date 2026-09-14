"""Compact AMS camera panel, using reported values only."""

import math
import re
from datetime import datetime
from typing import Any

from PIL import Image, ImageDraw, ImageFont


def measured(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and low <= result <= high else None


def ams_panel(snapshot: dict[str, Any], now: float) -> dict[str, Any] | None:
    state = snapshot.get("_raw", {}).get("ams", {})
    units = state.get("ams", [])
    if not units:
        return None
    selected = str(state.get("tray_now", "255"))
    unit = (
        next((a for a in units if str(a.get("id")) == str(int(selected) // 4)), units[0])
        if selected.isdecimal() and int(selected) < 254
        else units[0]
    )
    unit_id = str(unit.get("id", "0"))
    active = (
        int(selected) % 4
        if selected.isdecimal() and int(selected) < 254 and str(int(selected) // 4) == unit_id
        else None
    )
    session = snapshot.get("session", {})
    try:
        stamp = datetime.fromisoformat(session.get("last_telemetry_at", ""))
        stale = not session.get("connected") or stamp.tzinfo is None or now - stamp.timestamp() > 60
    except (ValueError, TypeError):
        stale = True
    title = [
        "AMS" if len(units) == 1 else f"AMS {int(unit_id) + 1}" if unit_id.isdecimal() else "AMS"
    ]
    humidity = measured(unit.get("humidity_raw"), 0, 100)
    temperature = measured(unit.get("temp"), -20, 100)
    if humidity is not None:
        title.append(f"{humidity:g}% RH")
    if temperature is not None:
        title.append(f"{temperature:.1f}°C")
    trays = {str(t.get("id")): t for t in unit.get("tray", [])}
    material = str(trays.get(str(active), {}).get("tray_type") or "")
    subtitle = (
        f"Slot {active + 1}"
        if active is not None
        else "External spool"
        if selected == "254"
        else "No active slot"
    )
    if active is not None and material:
        subtitle += " · " + material
    colors = []
    for i in range(4):
        color = str(trays.get(str(i), {}).get("tray_color") or "")
        colors.append("#" + color[:6] if re.fullmatch(r"[0-9a-fA-F]{8}", color) else None)
    fault = next(
        (
            h
            for h in snapshot.get("hms", [])
            if h.get("category") == "ams"
            and not h.get("stale")
            and h.get("severity") in {"warn", "error"}
        ),
        None,
    )
    return {
        "title": " · ".join(title),
        "subtitle": subtitle,
        "colors": colors,
        "active": active,
        "stale": stale,
        "fault": str(fault.get("text") or fault.get("hex") or "AMS fault") if fault else None,
    }


def draw_ams(canvas: Image.Image, data: dict[str, Any]) -> None:
    width, _ = canvas.size
    size = max(12, round(width / 64))
    font = ImageFont.load_default(size=size)
    margin, padding, pitch = max(10, size), max(8, size // 2), size + 7
    max_width = min(width - 2 * margin, max(270, int(width * 0.48)))
    measure = ImageDraw.Draw(canvas)

    def fit(text: str, limit: int) -> str:
        text = " ".join(text.split())[:240]
        if measure.textlength(text, font=font) <= limit:
            return text
        while text and measure.textlength(text + "…", font=font) > limit:
            text = text[:-1]
        return text + "…"

    dots = 4 * size
    title = fit(data["title"], max_width - 2 * padding - 6)
    subtitle = fit(data["subtitle"], max_width - 2 * padding - dots - 12)
    extra = "Telemetry stale" if data["stale"] else data["fault"]
    extra = fit(extra, max_width - 2 * padding - 6) if extra else None
    panel_width = min(
        max_width,
        int(
            max(
                measure.textlength(title, font=font),
                measure.textlength(subtitle, font=font) + dots + 12,
                measure.textlength(extra or "", font=font),
            )
        )
        + 2 * padding
        + 6,
    )
    panel_height = (3 if extra else 2) * pitch + 2 * padding
    panel = Image.new("RGBA", (panel_width, panel_height))
    draw = ImageDraw.Draw(panel)
    accent = "#a1a9b0" if data["stale"] else "#ffc27c" if data["fault"] else "#9ee8cf"
    draw.rounded_rectangle(
        (0, 0, panel_width - 1, panel_height - 1), radius=10, fill=(12, 19, 26, 175)
    )
    draw.rounded_rectangle((0, 9, 3, panel_height - 10), radius=2, fill=accent)
    for index, text in enumerate([title, subtitle] + ([extra] if extra else [])):
        draw.text(
            (padding + 3, padding + index * pitch),
            text,
            font=font,
            fill=accent if index == 0 or data["stale"] or index == 2 else "#eef3fa",
        )
    for i, color in enumerate(data["colors"]):
        x = panel_width - padding - dots + i * size + size // 2
        y = padding + pitch + size // 2 + 2
        r = max(3, size // 5)
        draw.ellipse(
            (x - r, y - r, x + r, y + r),
            fill=("#747c83" if data["stale"] else color) or "#35414a",
            outline="#a1a9b0",
        )
        if i == data["active"]:
            draw.ellipse((x - r - 3, y - r - 3, x + r + 3, y + r + 3), outline=accent, width=2)
    canvas.paste(panel, (width - margin - panel_width, margin), panel)
