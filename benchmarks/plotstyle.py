"""Shared look for the report's figures: one surface, quiet axes, a fixed colour order.

The three series colours were checked for colour-blind separation as a set; the green one
is light against the surface, so every figure that uses it also varies the marker and comes
with a table.
"""

from __future__ import annotations

from typing import Any

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
# Assigned in this order, never cycled.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
MARKERS = ("o", "s", "^")


def style_axes(ax: Any, *, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.set_xlabel(xlabel, color=INK_SECONDARY)
    ax.set_ylabel(ylabel, color=INK_SECONDARY)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=8, which="both")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)


def legend(ax: Any, **kwargs: Any) -> None:
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY, **kwargs)
