"""The lashing logo: a coiled rope seen from above. Regenerate with `python3 assets/make_logo.py assets`."""

import itertools
import math
import pathlib
import sys

W, GAP, TURNS, R0, TICK, SLANT = 14.0, 4.5, 1.85, 13.0, 10.0, 45
B = (W + GAP) / (2 * math.pi)


def point(t: float) -> tuple[float, float]:
    r = R0 + B * t
    return 60 + r * math.cos(t - math.pi / 2), 60 + r * math.sin(t - math.pi / 2)


PTS = [point(i * 0.02) for i in range(int(TURNS * 2 * math.pi / 0.02) + 1)]
D = "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in PTS)


def strands() -> str:
    out, since = [], TICK * 0.5
    for a, b in itertools.pairwise(PTS):
        since += math.dist(a, b)
        if since < TICK:
            continue
        since = 0.0
        tx, ty = b[0] - a[0], b[1] - a[1]
        n = math.hypot(tx, ty)
        tx, ty = tx / n, ty / n
        nx, ny = -ty, tx
        ang = math.radians(SLANT)
        dx, dy = nx * math.cos(ang) + tx * math.sin(ang), ny * math.cos(ang) + ty * math.sin(ang)
        h = (W / 2 + 0.8) / math.cos(ang)  # reaches just past this turn's edge, never the next turn
        x1, y1, x2, y2 = b[0] - dx * h, b[1] - dy * h, b[0] + dx * h, b[1] + dy * h
        out.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}"/>')
    return "".join(out)


def logo(rope: str, strand: str, ident: str, size: int = 120) -> str:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="{size}" height="{size}"'
    return f'''{svg} role="img" aria-label="lashing">
  <title>lashing</title>
  <defs>
    <mask id="{ident}" maskUnits="userSpaceOnUse" x="0" y="0" width="120" height="120">
      <path d="{D}" fill="none" stroke="#fff" stroke-width="{W}" stroke-linecap="round"/>
    </mask>
  </defs>
  <path d="{D}" fill="none" stroke="{rope}" stroke-width="{W}" stroke-linecap="round"/>
  <g mask="url(#{ident})" stroke="{strand}" stroke-width="2.4" stroke-linecap="round">{strands()}</g>
</svg>
'''


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1])
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    (out / "logo-light.svg").write_text(logo("#0e4a6e", "#5c93b8", "rope-light", size))
    (out / "logo-dark.svg").write_text(logo("#8cc4e6", "#4a8ab3", "rope-dark", size))
