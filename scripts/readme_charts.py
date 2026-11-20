"""Draw the README charts as plain SVG (stdlib only).

The numbers come from the author's own fleet of four profiles on 2026-11-20:
SKILL.md files on disk before setup (from the pre-setup backups) and after
(each profile's own skills plus the one shared folder), and the setup audit.
"""

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"

BEFORE = {"server": 243, "desktop": 167, "pikvm": 193, "handheld": 228}
AFTER_OWN = {"server": 46, "desktop": 42, "pikvm": 39, "handheld": 45}
SHARED = 199
AUDIT = [
    ("no differences between copies", 193, "#3fb950"),
    ("one copy contains the other", 11, "#d29922"),
    ("real conflicts", 42, "#f85149"),
]

TEXT = "#8b949e"
FONT = "font-family='-apple-system,Segoe UI,Helvetica,Arial,sans-serif'"


def _svg(width, height, body):
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' {FONT}>\n{body}\n</svg>\n"
    )


def copies_chart():
    w, left, bar_w, row = 760, 110, 560, 34
    scale = bar_w / max(BEFORE.values())
    parts = [
        (
            f"<text x='0' y='22' font-size='17' font-weight='600' fill='{TEXT}'>"
            "SKILL.md copies on disk, per profile</text>"
        )
    ]
    y = 50
    for name, b in BEFORE.items():
        a = AFTER_OWN[name]
        parts.append(
            f"<text x='{left - 10}' y='{y + 19}' font-size='14' text-anchor='end' fill='{TEXT}'>{name}</text>"
        )
        parts.append(
            f"<rect x='{left}' y='{y}' width='{b * scale:.0f}' height='12' rx='3' "
            "fill='#6e7681' opacity='0.55'/>"
        )
        parts.append(
            f"<text x='{left + b * scale + 8:.0f}' y='{y + 11}' font-size='12' "
            f"fill='{TEXT}'>{b} before</text>"
        )
        parts.append(
            f"<rect x='{left}' y='{y + 15}' width='{a * scale:.0f}' height='12' rx='3' fill='#58a6ff'/>"
        )
        parts.append(
            f"<text x='{left + a * scale + 8:.0f}' y='{y + 26}' font-size='12' fill='{TEXT}'>{a} after</text>"
        )
        y += row + 10
    parts.append(
        f"<text x='{left - 10}' y='{y + 19}' font-size='14' text-anchor='end' "
        f"font-weight='600' fill='{TEXT}'>shared</text>"
    )
    parts.append(
        f"<rect x='{left}' y='{y + 8}' width='{SHARED * scale:.0f}' height='14' rx='3' fill='#a371f7'/>"
    )
    parts.append(
        f"<text x='{left + SHARED * scale + 8:.0f}' y='{y + 20}' font-size='12' "
        f"fill='{TEXT}'>{SHARED} in one folder, read by all four</text>"
    )
    total_b, total_a = sum(BEFORE.values()), sum(AFTER_OWN.values()) + SHARED
    y += 52
    parts.append(
        f"<text x='{left}' y='{y}' font-size='13' fill='{TEXT}'>"
        f"Total: {total_b} copies before, {total_a} after.</text>"
    )
    return _svg(w, y + 12, "\n".join(parts))


def audit_chart():
    w, bar_w = 760, 740
    total = sum(n for _, n, _ in AUDIT)
    parts = [
        (
            f"<text x='0' y='22' font-size='17' font-weight='600' fill='{TEXT}'>"
            f"What setup found in {total} skills</text>"
        )
    ]
    x = 0
    for _, n, color in AUDIT:
        seg = bar_w * n / total
        parts.append(f"<rect x='{x:.1f}' y='38' width='{seg:.1f}' height='26' fill='{color}'/>")
        x += seg
    y = 92
    lx = 0
    for label, n, color in AUDIT:
        parts.append(f"<rect x='{lx}' y='{y - 11}' width='12' height='12' rx='2' fill='{color}'/>")
        text = f"{n} {label}"
        parts.append(f"<text x='{lx + 18}' y='{y}' font-size='13' fill='{TEXT}'>{text}</text>")
        lx += 18 + len(text) * 7.2 + 26
    return _svg(w, y + 10, "\n".join(parts))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "copies.svg").write_text(copies_chart(), encoding="utf-8")
    (OUT / "audit.svg").write_text(audit_chart(), encoding="utf-8")
    print("wrote", OUT / "copies.svg", OUT / "audit.svg")
