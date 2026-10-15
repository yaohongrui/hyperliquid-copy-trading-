from io import BytesIO


def render_report(title, text):
    """Render a structured Telegram report as a readable PNG card."""
    from PIL import Image, ImageDraw

    scale = 2
    font = _font(size=18 * scale)
    bold = _font(bold=True, size=18 * scale)
    rows = _parse_rows(text)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    width = max(1440, min(3600, int(max((_row_width(probe, row, font, bold) for row in rows), default=0) + 128)))
    line_height = 56
    height = 56 + sum(84 if row[0] == "section" else (70 if row[0] in {"table_header", "table_row"} else line_height) for row in rows) + 96
    image = Image.new("RGB", (width, height), "#0b1220")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 20, width - 20, height - 20), radius=32, fill="#111c2f", outline="#334766", width=4)
    draw.text((60, 48), title, font=bold, fill="#f8fafc")
    draw.line((60, 120, width - 60, 120), fill="#334766", width=4)

    y = 152
    for kind, value in rows:
        if kind == "section":
            y += 4
            draw.rounded_rectangle((48, y, width - 48, y + 64), radius=14, fill="#203450")
            draw.text((72, y + 10), value, font=bold, fill="#7dd3fc")
            y += 84
            continue
        if kind in {"table_header", "table_row"}:
            columns = value
            col_width = (width - 128) // max(1, len(columns))
            if kind == "table_header":
                draw.rectangle((48, y - 8, width - 48, y + 54), fill="#294463")
            for index, column in enumerate(columns):
                x = 64 + index * col_width
                draw.text((x, y), column, font=bold if kind == "table_header" else font,
                          fill="#7dd3fc" if kind == "table_header" else "#f1f5f9")
            y += 70
            continue
        if kind == "key":
            key, val = value
            draw.text((76, y), key, font=bold, fill="#94a3b8")
            draw.text((440, y), val, font=font, fill="#f1f5f9")
        elif kind == "account":
            draw.text((76, y), value, font=bold, fill="#fbbf24")
        elif kind == "position":
            symbol, side, rest = value
            side_color = "#34d399" if side == "LONG" else "#fb7185"
            draw.text((104, y), symbol, font=bold, fill="#f8fafc")
            draw.text((380, y), side, font=bold, fill=side_color)
            draw.text((600, y), rest, font=font, fill="#dbeafe")
        elif kind == "order":
            symbol, side, rest = value
            side_color = "#34d399" if side == "BUY" else "#fb7185"
            draw.text((104, y), symbol, font=bold, fill="#f8fafc")
            draw.text((380, y), side, font=bold, fill=side_color)
            draw.text((600, y), rest, font=font, fill="#fde68a")
        else:
            draw.text((76, y), value, font=font, fill="#cbd5e1")
        y += line_height

    output = BytesIO()
    image.save(output, format="PNG", optimize=True, compress_level=1, dpi=(192, 192))
    return output.getvalue()


def _parse_rows(text):
    rows = []
    for raw in (text.splitlines() or ["No data"]):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if "|" in line:
            columns = [part.strip() for part in line.strip("|").split("|")]
            if all(not column or set(column) <= {"-"} for column in columns):
                continue
            rows.append(("table_header" if columns and columns[0].lower() == "symbol" else "table_row", columns))
        elif upper in {"OPEN POSITIONS", "OPEN ORDERS"} or line == "Open orders":
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
    if row[0] in {"table_header", "table_row"}:
        return sum(draw.textlength(str(value), font=bold if row[0] == "table_header" else font) for value in row[1]) + 128 * len(row[1])
    if row[0] in {"position", "order"}:
        return 300 + draw.textlength(row[1][2], font=font)
    if row[0] == "key":
        return 220 + draw.textlength(row[1][1], font=font)
    return draw.textlength(str(row[1]), font=bold if row[0] in {"section", "account"} else font)


def _font(bold=False, size=18):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()
