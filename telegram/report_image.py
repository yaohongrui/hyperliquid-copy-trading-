from io import BytesIO


def render_report(title, text):
    """Render a structured Telegram report as a readable PNG card."""
    from PIL import Image, ImageDraw

    font = _font()
    bold = _font(bold=True)
    rows = _parse_rows(text)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    width = max(720, min(1800, int(max((_row_width(probe, row, font, bold) for row in rows), default=0) + 64)))
    line_height = 28
    height = 28 + sum(42 if row[0] == "section" else line_height for row in rows) + 48
    image = Image.new("RGB", (width, height), "#0b1220")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((10, 10, width - 10, height - 10), radius=16, fill="#111c2f", outline="#334766", width=2)
    draw.text((30, 24), title, font=bold, fill="#f8fafc")
    draw.line((30, 60, width - 30, 60), fill="#334766", width=2)

    y = 76
    for kind, value in rows:
        if kind == "section":
            y += 4
            draw.rounded_rectangle((24, y, width - 24, y + 32), radius=7, fill="#203450")
            draw.text((36, y + 5), value, font=bold, fill="#7dd3fc")
            y += 42
            continue
        if kind == "key":
            key, val = value
            draw.text((38, y), key, font=bold, fill="#94a3b8")
            draw.text((220, y), val, font=font, fill="#f1f5f9")
        elif kind == "account":
            draw.text((38, y), value, font=bold, fill="#fbbf24")
        elif kind == "position":
            symbol, side, rest = value
            side_color = "#34d399" if side == "LONG" else "#fb7185"
            draw.text((52, y), symbol, font=bold, fill="#f8fafc")
            draw.text((190, y), side, font=bold, fill=side_color)
            draw.text((300, y), rest, font=font, fill="#dbeafe")
        elif kind == "order":
            symbol, side, rest = value
            side_color = "#34d399" if side == "BUY" else "#fb7185"
            draw.text((52, y), symbol, font=bold, fill="#f8fafc")
            draw.text((190, y), side, font=bold, fill=side_color)
            draw.text((300, y), rest, font=font, fill="#fde68a")
        else:
            draw.text((38, y), value, font=font, fill="#cbd5e1")
        y += line_height

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _parse_rows(text):
    rows = []
    for raw in (text.splitlines() or ["No data"]):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper in {"OPEN POSITIONS", "OPEN ORDERS"} or line == "Open orders":
            rows.append(("section", upper))
        elif ": " in line and not raw.startswith(" "):
            rows.append(("key", tuple(line.split(": ", 1))))
        elif not raw.startswith(" "):
            rows.append(("account", line))
        elif " | entry " in line or " | mark " in line:
            parts = line.split()
            symbol = parts[0]
            side = next((p for p in parts if p in {"LONG", "SHORT"}), "")
            rest = line.replace(symbol, "", 1).replace(side, "", 1).strip()
            rows.append(("position", (symbol, side, rest)))
        elif " @ " in line:
            parts = line.split()
            symbol = parts[0]
            side = next((p for p in parts if p in {"BUY", "SELL"}), "")
            rest = line.replace(symbol, "", 1).replace(side, "", 1).strip()
            rows.append(("order", (symbol, side, rest)))
        else:
            rows.append(("text", line))
    return rows


def _row_width(draw, row, font, bold):
    if row[0] in {"position", "order"}:
        return 300 + draw.textlength(row[1][2], font=font)
    if row[0] == "key":
        return 220 + draw.textlength(row[1][1], font=font)
    return draw.textlength(str(row[1]), font=bold if row[0] in {"section", "account"} else font)


def _font(bold=False):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, 18)
        except OSError:
            pass
    return ImageFont.load_default()
