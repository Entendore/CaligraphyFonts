#!/usr/bin/env python3
"""
Calligraphy Font Maker — v3
A professional PySide6 application for drawing calligraphic glyphs and exporting fonts.

Improvements over v2:
  - Move tool (M): drag selected stroke(s) to reposition on canvas
  - Zoom towards cursor instead of widget center (natural zoom behavior)
  - Auto-save with crash recovery (periodic .autosave.json + prompt on startup)
  - Recent files submenu (last 8 projects) under File menu
  - Stroke opacity / alpha support in Brush panel and rendering
  - Canvas background options: White, Cream, Dark, Transparent grid
  - Multi-stroke selection: Shift+click to toggle strokes in/out of selection
  - Stroke list panel: view, reorder, delete strokes in the current glyph
  - Export progress dialog for TTF builds
  - Threaded TTF export to avoid UI freeze
  - Arrow-key glyph navigation, Tab to cycle tools

Requirements:
    pip install PySide6 fonttools
    (fonttools is optional — needed only for TTF export)
"""

import sys
import os
import math
import json
import copy
import time
import threading
from typing import Optional
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QToolBar, QListWidget, QListWidgetItem, QLabel, QSpinBox,
    QDoubleSpinBox, QComboBox, QPushButton, QColorDialog,
    QFileDialog, QInputDialog, QMessageBox, QGroupBox, QFormLayout,
    QCheckBox, QSlider, QLineEdit, QSizePolicy, QFrame, QDialog,
    QShortcut, QProgressDialog, QMenu, QScrollArea,
)
from PySide6.QtCore import Qt, QPointF, QRectF, QSize, Signal, QTimer
from PySide6.QtGui import (
    QPainter, QPen, QColor, QPixmap, QPolygonF,
    QPainterPath, QMouseEvent, QKeySequence, QAction, QFont, QIcon,
    QBrush,
)

try:
    from fontTools.ttLib import TTFont
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    FONTTOOLS_AVAILABLE = True
except ImportError:
    FONTTOOLS_AVAILABLE = False


# ─── Constants & Styles ─────────────────────────────────────────────────────

STYLE_SHEET = """
QMainWindow { background: #f0f0f0; }
QToolBar {
    background: #e8e8e8; border: none; padding: 2px; spacing: 2px;
}
QToolBar QToolButton {
    padding: 4px 8px; border-radius: 3px;
    border: 1px solid transparent;
}
QToolBar QToolButton:hover {
    background: #d8d8d8; border: 1px solid #c0c0c0;
}
QToolBar QToolButton:checked {
    background: #c5cae9; border: 1px solid #7986cb;
}
QGroupBox {
    font-weight: bold; border: 1px solid #ccc;
    border-radius: 4px; margin-top: 8px; padding-top: 14px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 4px;
}
QListWidget {
    border: 1px solid #ccc; border-radius: 4px; background: #fff;
}
QListWidget::item {
    border-bottom: 1px solid #eee; padding: 1px;
}
QListWidget::item:selected { background: #dde4ff; }
QPushButton {
    padding: 3px 10px; border-radius: 3px;
    border: 1px solid #bbb; background: #f5f5f5;
}
QPushButton:hover { background: #e5e5e5; }
QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit {
    padding: 3px 6px; border: 1px solid #ccc;
    border-radius: 3px; background: #fff;
}
QSlider::groove:horizontal {
    height: 6px; background: #ddd; border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 14px; height: 14px; margin: -4px 0;
    background: #888; border-radius: 7px;
}
QSlider::handle:horizontal:hover { background: #666; }
QMenuBar { background: #e8e8e8; }
QMenuBar::item:selected { background: #d0d0e8; }
QMenu { background: #fff; border: 1px solid #ccc; }
QMenu::item:selected { background: #dde4ff; }
QStatusBar { background: #e8e8e8; }
QCheckBox { spacing: 6px; }
QScrollArea { border: none; }
"""

# Auto-save constants
AUTOSAVE_INTERVAL_MS = 60_000  # 60 seconds
AUTOSAVE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".autosave.json"
)
RECENT_FILE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".recent_files.json"
)
MAX_RECENT_FILES = 8

# Canvas background presets
BG_WHITE = "white"
BG_CREAM = "cream"
BG_DARK = "dark"
BG_TRANSPARENT = "transparent"
BG_OPTIONS = {
    BG_WHITE: QColor(255, 255, 255),
    BG_CREAM: QColor(245, 243, 238),
    BG_DARK: QColor(45, 45, 48),
    BG_TRANSPARENT: QColor(220, 220, 220),
}


# ─── Data Models ────────────────────────────────────────────────────────────

class Stroke:
    """A single brush stroke: an ordered list of (x, y, width) tuples with colour and opacity."""

    __slots__ = ("points", "color", "opacity")

    def __init__(self, color: Optional[QColor] = None, opacity: float = 1.0):
        self.points: list[tuple[float, float, float]] = []
        self.color = color if color is not None else QColor(20, 20, 20)
        self.opacity = opacity

    def add_point(self, x: float, y: float, w: float):
        self.points.append((x, y, w))

    def is_empty(self) -> bool:
        return len(self.points) == 0

    def bounding_rect(self) -> QRectF:
        if not self.points:
            return QRectF()
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        max_hw = max(p[2] / 2 for p in self.points)
        return QRectF(
            min(xs) - max_hw, min(ys) - max_hw,
            max(xs) - min(xs) + max_hw * 2,
            max(ys) - min(ys) + max_hw * 2,
        )

    def to_dict(self) -> dict:
        return {
            "color": self.color.name(),
            "opacity": self.opacity,
            "points": self.points,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Stroke":
        s = cls(QColor(d["color"]), d.get("opacity", 1.0))
        s.points = [tuple(p) for p in d["points"]]
        return s

    def deep_copy(self) -> "Stroke":
        s = Stroke(QColor(self.color), self.opacity)
        s.points = list(self.points)
        return s


class Glyph:
    """One character — holds strokes, Unicode mapping, metrics, and name."""

    def __init__(self, char: str = "", name: str = ""):
        self.char = char
        self.unicode_val: Optional[int] = ord(char) if char else None
        self.name = name or (char if char else "unnamed")
        self.strokes: list[Stroke] = []
        self.advance_width = 600
        self.left_bearing = 50

    def add_stroke(self, s: Stroke):
        self.strokes.append(s)

    def pop_stroke(self) -> Optional[Stroke]:
        return self.strokes.pop() if self.strokes else None

    def is_empty(self) -> bool:
        return all(s.is_empty() for s in self.strokes)

    def thumbnail(self, size: int = 48) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(QColor(255, 255, 255))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        all_pts = [(pt[0], pt[1]) for st in self.strokes for pt in st.points]
        if all_pts:
            xs, ys = zip(*all_pts)
            mn_x, mx_x = min(xs), max(xs)
            mn_y, mx_y = min(ys), max(ys)
            w = max(mx_x - mn_x, 1)
            h = max(mx_y - mn_y, 1)
            sc = min((size - 6) / w, (size - 6) / h)
            ox = (size - w * sc) / 2 - mn_x * sc
            oy = (size - h * sc) / 2 - mn_y * sc
            p.translate(ox, oy)
            p.scale(sc, sc)
        for st in self.strokes:
            _draw_stroke_painter(p, st)
        p.end()
        return pm

    def to_dict(self) -> dict:
        return {
            "char": self.char, "name": self.name,
            "unicode_val": self.unicode_val,
            "advance_width": self.advance_width,
            "left_bearing": self.left_bearing,
            "strokes": [s.to_dict() for s in self.strokes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Glyph":
        g = cls(d.get("char", ""), d.get("name", ""))
        g.unicode_val = d.get("unicode_val")
        g.advance_width = d.get("advance_width", 600)
        g.left_bearing = d.get("left_bearing", 50)
        g.strokes = [Stroke.from_dict(sd) for sd in d.get("strokes", [])]
        return g

    def deep_copy(self) -> "Glyph":
        g = Glyph(self.char, self.name)
        g.unicode_val = self.unicode_val
        g.advance_width = self.advance_width
        g.left_bearing = self.left_bearing
        g.strokes = [s.deep_copy() for s in self.strokes]
        return g


class BrushSettings:
    """Mutable brush configuration shared between the canvas and the UI panel."""

    TYPES = ["Pointed", "Flat", "Round"]

    def __init__(self):
        self.brush_type: int = 0
        self.base_width: float = 14.0
        self.min_width: float = 1.0
        self.max_width: float = 40.0
        self.nib_angle: float = 45.0
        self.smoothing: float = 0.30
        self.speed_sensitivity: float = 0.75
        self.color = QColor(20, 20, 20)
        self.opacity: float = 1.0  # v3: per-brush opacity


# ─── Stroke Rendering Engine ────────────────────────────────────────────────

def _draw_stroke_painter(painter: QPainter, stroke: Stroke):
    """Render *stroke* onto *painter* using a smooth QPainterPath with
    quadratic Bezier curves and rounded caps. Respects stroke opacity."""
    pts = stroke.points
    if not pts:
        return

    # Build colour with opacity
    c = QColor(stroke.color)
    c.setAlphaF(c.alphaF() * stroke.opacity)

    painter.setPen(Qt.NoPen)
    painter.setBrush(c)

    if len(pts) == 1:
        x, y, w = pts[0]
        painter.drawEllipse(QPointF(x, y), w / 2, w / 2)
        return

    # Build left / right edge arrays
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    n = len(pts)

    for i, (x, y, w) in enumerate(pts):
        if i == 0:
            dx = pts[1][0] - x
            dy = pts[1][1] - y
        elif i == n - 1:
            dx = x - pts[i - 1][0]
            dy = y - pts[i - 1][1]
        else:
            dx = pts[i + 1][0] - pts[i - 1][0]
            dy = pts[i + 1][1] - pts[i - 1][1]

        ln = math.hypot(dx, dy)
        if ln < 0.01:
            if left:
                left.append(left[-1])
                right.append(right[-1])
            else:
                left.append((x, y))
                right.append((x, y))
            continue

        nx = -dy / ln
        ny = dx / ln
        hw = w / 2
        left.append((x + nx * hw, y + ny * hw))
        right.append((x - nx * hw, y - ny * hw))

    path = QPainterPath()
    path.moveTo(QPointF(*left[0]))

    for i in range(1, len(left)):
        if i < len(left) - 1:
            mx = (left[i][0] + left[i + 1][0]) / 2
            my = (left[i][1] + left[i + 1][1]) / 2
            path.quadTo(QPointF(*left[i]), QPointF(mx, my))
        else:
            path.lineTo(QPointF(*left[i]))

    # Start cap
    tail = QPointF(*pts[0])
    tw = pts[0][2] / 2
    path.lineTo(QPointF(tail.x(), tail.y() + tw))
    path.arcTo(QRectF(tail.x() - tw, tail.y() - tw, tw * 2, tw * 2), 90, -180)

    # Backward along right edge
    for i in range(len(right) - 2, -1, -1):
        if i > 0:
            mx = (right[i][0] + right[i - 1][0]) / 2
            my = (right[i][1] + right[i - 1][1]) / 2
            path.quadTo(QPointF(*right[i]), QPointF(mx, my))
        else:
            path.lineTo(QPointF(*right[i]))

    # Head cap
    head = QPointF(*pts[-1])
    hw2 = pts[-1][2] / 2
    path.lineTo(QPointF(head.x(), head.y() - hw2))
    path.arcTo(QRectF(head.x() - hw2, head.y() - hw2, hw2 * 2, hw2 * 2), -90, -180)

    path.closeSubpath()
    painter.drawPath(path)


def _compute_width(brush: BrushSettings, dx, dy, dt, prev_w) -> float:
    if brush.brush_type == 0:
        speed = math.hypot(dx, dy) / max(dt, 1)
        target = brush.base_width * (1.0 - brush.speed_sensitivity * min(speed / 400, 0.92))
    elif brush.brush_type == 1:
        angle = math.atan2(dy, dx) - math.radians(brush.nib_angle)
        factor = max(0.12, abs(math.sin(angle)))
        target = brush.base_width * factor
    else:
        target = brush.base_width
    target = max(brush.min_width, min(brush.max_width, target))
    return prev_w + brush.smoothing * (target - prev_w)


def _simplify_points(pts, tolerance: float = 0.5):
    """Douglas-Peucker simplification."""
    if len(pts) <= 2:
        return list(pts)
    x0, y0 = pts[0]
    x1, y1 = pts[-1]
    dx, dy = x1 - x0, y1 - y0
    line_len_sq = dx * dx + dy * dy
    max_dist = 0.0
    max_idx = 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        if line_len_sq < 1e-12:
            d = math.hypot(px - x0, py - y0)
        else:
            t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / line_len_sq))
            d = math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))
        if d > max_dist:
            max_dist = d
            max_idx = i
    if max_dist > tolerance:
        left = _simplify_points(pts[:max_idx + 1], tolerance)
        right = _simplify_points(pts[max_idx:], tolerance)
        return left[:-1] + right
    return [pts[0], pts[-1]]


def get_stroke_outline(pts) -> list[tuple[float, float]]:
    """Return a closed polygon for font outlines (simplified)."""
    if len(pts) < 2:
        return []
    left, right = [], []
    n = len(pts)
    for i, (x, y, w) in enumerate(pts):
        if i == 0:
            dx, dy = pts[1][0] - x, pts[1][1] - y
        elif i == n - 1:
            dx, dy = x - pts[i - 1][0], y - pts[i - 1][1]
        else:
            dx, dy = pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]
        ln = math.hypot(dx, dy)
        if ln < 0.01:
            if left:
                left.append(left[-1]); right.append(right[-1])
            else:
                left.append((x, y)); right.append((x, y))
            continue
        nx, ny = -dy / ln, dx / ln
        hw = w / 2
        left.append((x + nx * hw, y + ny * hw))
        right.append((x - nx * hw, y - ny * hw))
    return _simplify_points(left, 0.5) + _simplify_points(right, 0.5)[::-1]


# ─── Recent Files Manager ───────────────────────────────────────────────────

class RecentFilesManager:
    """Track recently opened project files (persisted to disk)."""

    def __init__(self):
        self._paths: list[str] = self._load()

    def _load(self) -> list[str]:
        try:
            if os.path.exists(RECENT_FILE_PATH):
                with open(RECENT_FILE_PATH, "r") as f:
                    data = json.load(f)
                return data.get("recent", [])[:MAX_RECENT_FILES]
        except Exception:
            pass
        return []

    def _save(self):
        try:
            with open(RECENT_FILE_PATH, "w") as f:
                json.dump({"recent": self._paths}, f, indent=2)
        except Exception:
            pass

    def add(self, path: str):
        path = os.path.abspath(path)
        if path in self._paths:
            self._paths.remove(path)
        self._paths.insert(0, path)
        self._paths = self._paths[:MAX_RECENT_FILES]
        self._save()

    def remove(self, path: str):
        path = os.path.abspath(path)
        if path in self._paths:
            self._paths.remove(path)
            self._save()

    def get_paths(self) -> list[str]:
        return list(self._paths)

    def clear(self):
        self._paths.clear()
        self._save()


# ─── Calligraphy Canvas ────────────────────────────────────────────────────

class CalligraphyCanvas(QWidget):
    """Central drawing widget — handles pen/eraser/select/move input,
    zoom (towards cursor), pan, and rendering."""

    glyph_changed = Signal()
    glyph_metrics_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 480)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self.brush = BrushSettings()
        self.glyph: Optional[Glyph] = None
        self.tool = "draw"  # "draw" | "erase" | "select" | "move"

        # Drawing state
        self._drawing = False
        self._stroke: Optional[Stroke] = None
        self._last_x = 0.0
        self._last_y = 0.0
        self._last_t = 0
        self._last_w = 0.0
        self._smooth_x = 0.0
        self._smooth_y = 0.0

        # View state
        self.zoom = 1.0
        self._pan = QPointF()
        self._panning = False
        self._pan_start = QPointF()
        self._mouse = QPointF()

        # Typography metrics
        self.ascent = 700
        self.descent = 300
        self.x_height = 400
        self.margin_lr = 60

        # Guide toggles
        self.show_guides = True
        self.show_grid = False
        self.show_bearings = True

        # Canvas background
        self._bg_mode = BG_CREAM

        # Per-glyph undo/redo
        self._undo_stack: dict[int, list[list[Stroke]]] = {}
        self._redo_stack: dict[int, list[list[Stroke]]] = {}

        # Stroke selection (select/move tool) — supports multi-select
        self._selected_stroke_indices: set[int] = set()

        # Move-tool drag state
        self._dragging = False
        self._drag_start_cx = 0.0
        self._drag_start_cy = 0.0

    @property
    def em(self) -> int:
        return self.ascent + self.descent

    def _view_rect(self) -> tuple[QRectF, float]:
        margin = 50
        avail = min(self.width(), self.height()) - 2 * margin
        sc = avail / self.em * self.zoom
        cw = self.em * sc
        ch = self.em * sc
        x = (self.width() - cw) / 2 + self._pan.x()
        y = (self.height() - ch) / 2 + self._pan.y()
        return QRectF(x, y, cw, ch), sc

    def _w2c(self, pos: QPointF) -> tuple[float, float]:
        r, sc = self._view_rect()
        return (pos.x() - r.x()) / sc, (pos.y() - r.y()) / sc

    def _glyph_key(self) -> Optional[int]:
        return id(self.glyph) if self.glyph else None

    def set_glyph(self, g: Glyph):
        self.glyph = g
        self._selected_stroke_indices.clear()
        self.glyph_metrics_changed.emit()
        self.update()

    # ── undo / redo ────────────────────────────────────────────────────────

    def _push_undo(self):
        key = self._glyph_key()
        if key is None or not self.glyph:
            return
        snapshot = [s.deep_copy() for s in self.glyph.strokes]
        self._undo_stack.setdefault(key, []).append(snapshot)
        if len(self._undo_stack[key]) > 50:
            self._undo_stack[key] = self._undo_stack[key][-50:]
        self._redo_stack.pop(key, None)

    def undo(self):
        key = self._glyph_key()
        if key is None or not self.glyph:
            return
        history = self._undo_stack.get(key, [])
        if not history:
            return
        current = [s.deep_copy() for s in self.glyph.strokes]
        self._redo_stack.setdefault(key, []).append(current)
        self.glyph.strokes = history.pop()
        if not history:
            del self._undo_stack[key]
        self._selected_stroke_indices.clear()
        self.glyph_changed.emit()
        self.update()

    def redo(self):
        key = self._glyph_key()
        if key is None or not self.glyph:
            return
        forward = self._redo_stack.get(key, [])
        if not forward:
            return
        current = [s.deep_copy() for s in self.glyph.strokes]
        self._undo_stack.setdefault(key, []).append(current)
        self.glyph.strokes = forward.pop()
        if not forward:
            del self._redo_stack[key]
        self._selected_stroke_indices.clear()
        self.glyph_changed.emit()
        self.update()

    def clear_glyph(self):
        if not self.glyph:
            return
        if self.glyph.strokes:
            self._push_undo()
        self.glyph.strokes.clear()
        self._selected_stroke_indices.clear()
        self.glyph_changed.emit()
        self.update()

    # ── transforms ────────────────────────────────────────────────────────

    def flip_horizontal(self):
        if not self.glyph or self.glyph.is_empty():
            return
        self._push_undo()
        center = self.em / 2
        for st in self.glyph.strokes:
            st.points = [(center - (x - center), y, w) for x, y, w in st.points]
        self._selected_stroke_indices.clear()
        self.glyph_changed.emit()
        self.update()

    def flip_vertical(self):
        if not self.glyph or self.glyph.is_empty():
            return
        self._push_undo()
        center = self.ascent / 2
        for st in self.glyph.strokes:
            st.points = [(x, center - (y - center), w) for x, y, w in st.points]
        self._selected_stroke_indices.clear()
        self.glyph_changed.emit()
        self.update()

    # ── stroke selection ───────────────────────────────────────────────────

    def _stroke_at(self, cx: float, cy: float, tolerance: float = 8.0) -> int:
        """Return the index of the first stroke near (cx, cy), or -1."""
        if not self.glyph:
            return -1
        for i, st in enumerate(self.glyph.strokes):
            for x, y, w in st.points:
                if math.hypot(x - cx, y - cy) < (tolerance + w / 2):
                    return i
        return -1

    def delete_selected_strokes(self):
        """Remove all selected strokes."""
        if not self.glyph or not self._selected_stroke_indices:
            return
        self._push_undo()
        for i in sorted(self._selected_stroke_indices, reverse=True):
            if 0 <= i < len(self.glyph.strokes):
                self.glyph.strokes.pop(i)
        self._selected_stroke_indices.clear()
        self.glyph_changed.emit()
        self.update()

    def move_selected_strokes(self, dx: float, dy: float):
        """Offset all selected strokes by (dx, dy) in design units."""
        if not self.glyph or not self._selected_stroke_indices:
            return
        for idx in self._selected_stroke_indices:
            if 0 <= idx < len(self.glyph.strokes):
                st = self.glyph.strokes[idx]
                st.points = [(x + dx, y + dy, w) for x, y, w in st.points]

    # ── eraser ────────────────────────────────────────────────────────────

    def _erase_at(self, cx: float, cy: float, radius: float):
        if not self.glyph:
            return
        to_remove: list[int] = []
        for i, st in enumerate(self.glyph.strokes):
            rect = st.bounding_rect()
            test_rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
            if rect.intersects(test_rect):
                for x, y, w in st.points:
                    if math.hypot(x - cx, y - cy) < (radius + w / 2):
                        to_remove.append(i)
                        break
        if to_remove:
            self._push_undo()
            for i in reversed(to_remove):
                self.glyph.strokes.pop(i)
            self._selected_stroke_indices -= set(to_remove)
            self.glyph_changed.emit()
            self.update()

    # ── painting ──────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Background
        self._paint_background(p)

        rect, sc = self._view_rect()

        # EM-square shadow + fill
        p.fillRect(rect.adjusted(3, 3, 3, 3), QColor(0, 0, 0, 25))
        p.fillRect(rect, QColor(255, 255, 255))
        p.setPen(QPen(QColor(190, 190, 190), 1))
        p.drawRect(rect.toRect())

        if self.show_grid:
            self._paint_grid(p, rect, sc)
        if self.show_guides:
            self._paint_guides(p, rect, sc)
        if self.show_bearings and self.glyph:
            self._paint_bearings(p, rect, sc)

        # Draw strokes
        p.save()
        p.translate(rect.x(), rect.y())
        p.scale(sc, sc)
        if self.glyph:
            for idx, st in enumerate(self.glyph.strokes):
                _draw_stroke_painter(p, st)
                if idx in self._selected_stroke_indices and self.tool in ("select", "move"):
                    br = st.bounding_rect().adjusted(-4, -4, 4, 4)
                    p.setBrush(Qt.NoBrush)
                    p.setPen(QPen(QColor(66, 133, 244, 180), 2.0 / sc, Qt.DashLine))
                    p.drawRect(br)
                    p.setPen(Qt.NoPen)

        if self._stroke and not self._stroke.is_empty():
            _draw_stroke_painter(p, self._stroke)
        p.restore()

        # Cursor preview
        if self.underMouse() and not self._panning:
            self._paint_cursor(p, sc)

        p.end()

    def _paint_background(self, p: QPainter):
        bg = BG_OPTIONS.get(self._bg_mode, BG_OPTIONS[BG_CREAM])
        if self._bg_mode == BG_TRANSPARENT:
            # Checkerboard pattern
            p.fillRect(self.rect(), QColor(255, 255, 255))
            sz = 12
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(210, 210, 210))
            cols = self.width() // sz + 1
            rows = self.height() // sz + 1
            for r in range(rows):
                for c in range(cols):
                    if (r + c) % 2 == 0:
                        p.drawRect(c * sz, r * sz, sz, sz)
        else:
            p.fillRect(self.rect(), bg)

    def _paint_cursor(self, p: QPainter, sc: float):
        r = self.brush.base_width * sc / 2
        if self.tool == "erase":
            p.setPen(QPen(QColor(255, 0, 0, 120), 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(self._mouse, r, r)
        elif self.tool in ("select", "move"):
            p.setPen(QPen(QColor(66, 133, 244, 150), 1))
            p.setBrush(Qt.NoBrush)
            sz = 10
            p.drawLine(QPointF(self._mouse.x() - sz, self._mouse.y()),
                       QPointF(self._mouse.x() + sz, self._mouse.y()))
            p.drawLine(QPointF(self._mouse.x(), self._mouse.y() - sz),
                       QPointF(self._mouse.x(), self._mouse.y() + sz))
        elif self.brush.brush_type == 1:
            p.save()
            p.translate(self._mouse)
            p.rotate(self.brush.nib_angle)
            p.setPen(QPen(QColor(0, 0, 0, 70), 1))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(0, 0), r, r * 0.25)
            p.restore()
        else:
            p.setPen(QPen(QColor(0, 0, 0, 70), 1))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(self._mouse, r, r)

    def _paint_guides(self, p: QPainter, rect: QRectF, sc: float):
        pen = QPen(QColor(160, 185, 240, 110), 1, Qt.DashLine)
        p.setPen(pen)
        guides = [
            (0, "Ascender"), (self.ascent, "Baseline"),
            (self.ascent - self.x_height, "x-height"), (self.em, "Descender"),
        ]
        for metric, label in guides:
            y = rect.y() + metric * sc
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(QColor(140, 160, 210, 160))
            p.setFont(QFont("Arial", 8))
            p.drawText(QPointF(rect.right() + 4, y + 4), label)
            p.setPen(pen)
        ml = rect.x() + self.margin_lr * sc
        mr = rect.right() - self.margin_lr * sc
        p.drawLine(QPointF(ml, rect.top()), QPointF(ml, rect.bottom()))
        p.drawLine(QPointF(mr, rect.top()), QPointF(mr, rect.bottom()))

    def _paint_bearings(self, p: QPainter, rect: QRectF, sc: float):
        if not self.glyph:
            return
        lx = rect.x() + self.glyph.left_bearing * sc
        p.setPen(QPen(QColor(255, 160, 0, 150), 1.5, Qt.DotLine))
        p.drawLine(QPointF(lx, rect.top()), QPointF(lx, rect.bottom()))
        ax = rect.x() + self.glyph.advance_width * sc
        p.setPen(QPen(QColor(0, 200, 100, 150), 1.5, Qt.DotLine))
        p.drawLine(QPointF(ax, rect.top()), QPointF(ax, rect.bottom()))

    def _paint_grid(self, p: QPainter, rect: QRectF, sc: float):
        p.setPen(QPen(QColor(200, 200, 200, 50), 0.5))
        for i in range(0, int(self.em) + 1, 50):
            x = rect.x() + i * sc
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            y = rect.y() + i * sc
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

    # ── mouse events ──────────────────────────────────────────────────────

    def mousePressEvent(self, e: QMouseEvent):
        self._mouse = e.position()

        if (
            e.button() == Qt.MiddleButton
            or (e.button() == Qt.LeftButton and (e.modifiers() & Qt.SpaceModifier))
        ):
            self._panning = True
            self._pan_start = e.position()
            self.setCursor(Qt.ClosedHandCursor)
            return

        if e.button() == Qt.LeftButton and self.glyph:
            cx, cy = self._w2c(e.position())

            if self.tool == "select":
                idx = self._stroke_at(cx, cy)
                if e.modifiers() & Qt.ShiftModifier:
                    # Toggle multi-select
                    if idx >= 0:
                        if idx in self._selected_stroke_indices:
                            self._selected_stroke_indices.discard(idx)
                        else:
                            self._selected_stroke_indices.add(idx)
                else:
                    # Single select (replace)
                    self._selected_stroke_indices = {idx} if idx >= 0 else set()
                self.update()

            elif self.tool == "move":
                if not self._selected_stroke_indices:
                    # If nothing is selected, select the stroke under cursor
                    idx = self._stroke_at(cx, cy)
                    if idx >= 0:
                        self._selected_stroke_indices = {idx}
                if self._selected_stroke_indices:
                    self._push_undo()
                    self._dragging = True
                    self._drag_start_cx = cx
                    self._drag_start_cy = cy
                    self.setCursor(Qt.SizeAllCursor)
                self.update()

            elif self.tool == "erase":
                self._drawing = True
                self._erase_at(cx, cy, self.brush.base_width)

            else:  # draw
                self._drawing = True
                self._stroke = Stroke(self.brush.color, self.brush.opacity)
                self._last_x, self._last_y = cx, cy
                self._last_t = e.timestamp()
                self._last_w = self.brush.base_width
                self._smooth_x, self._smooth_y = cx, cy
                self._stroke.add_point(cx, cy, self._last_w)

    def mouseMoveEvent(self, e: QMouseEvent):
        self._mouse = e.position()

        if self._panning:
            d = e.position() - self._pan_start
            self._pan += d
            self._pan_start = e.position()
            self.update()
            return

        if self._dragging:
            cx, cy = self._w2c(e.position())
            dx = cx - self._drag_start_cx
            dy = cy - self._drag_start_cy
            self._drag_start_cx = cx
            self._drag_start_cy = cy
            self.move_selected_strokes(dx, dy)
            self.glyph_changed.emit()
            self.update()
            return

        if not self._drawing:
            self.update()
            return

        cx, cy = self._w2c(e.position())

        if self.tool == "erase":
            self._erase_at(cx, cy, self.brush.base_width)
        elif self._stroke:
            dx = cx - self._last_x
            dy = cy - self._last_y
            if math.hypot(dx, dy) < 1.5:
                return
            dt = max(e.timestamp() - self._last_t, 1)
            w = _compute_width(self.brush, dx, dy, dt, self._last_w)
            a = self.brush.smoothing
            sx = self._smooth_x * a + cx * (1 - a)
            sy = self._smooth_y * a + cy * (1 - a)
            self._stroke.add_point(sx, sy, w)
            self._last_x, self._last_y = cx, cy
            self._last_t = e.timestamp()
            self._last_w = w
            self._smooth_x, self._smooth_y = sx, sy
            self.update()

    def mouseReleaseEvent(self, e: QMouseEvent):
        self._mouse = e.position()

        if self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            return

        if self._dragging:
            self._dragging = False
            self.setCursor(Qt.ArrowCursor)
            return

        if e.button() == Qt.LeftButton and self._drawing:
            self._drawing = False
            if self.tool == "draw" and self._stroke and not self._stroke.is_empty():
                self._push_undo()
                self.glyph.add_stroke(self._stroke)
                self.glyph_changed.emit()
            self._stroke = None
            self.update()

    # ── zoom towards cursor ───────────────────────────────────────────────

    def wheelEvent(self, e):
        """Zoom towards the cursor position instead of the widget center."""
        old_pos = e.position()
        factor = 1.12 if e.angleDelta().y() > 0 else 1 / 1.12
        new_zoom = max(0.15, min(6.0, self.zoom * factor))

        # Compute the design-space point under the cursor before zoom
        r_old, sc_old = self._view_rect()
        cx_before = (old_pos.x() - r_old.x()) / sc_old
        cy_before = (old_pos.y() - r_old.y()) / sc_old

        self.zoom = new_zoom

        # Compute where that same design-space point lands after zoom
        r_new, sc_new = self._view_rect()
        cx_after = (old_pos.x() - r_new.x()) / sc_new
        cy_after = (old_pos.y() - r_new.y()) / sc_new

        # Adjust pan so the point stays under the cursor
        self._pan += QPointF(
            (cx_after - cx_before) * sc_new,
            (cy_after - cy_before) * sc_new,
        )
        self.update()

    def zoom_to_fit(self):
        self.zoom = 1.0
        self._pan = QPointF()
        self.update()

    # ── keyboard ──────────────────────────────────────────────────────────

    def keyPressEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            if e.key() == Qt.Key_Z:
                if e.modifiers() & Qt.ShiftModifier:
                    self.redo()
                else:
                    self.undo()
                return
            if e.key() == Qt.Key_Y:
                self.redo()
                return
            if e.key() in (Qt.Key_C, Qt.Key_V):
                return  # handled at MainWindow
        if e.key() == Qt.Key_A and e.modifiers() & Qt.ControlModifier:
            # Select all strokes
            if self.glyph and self.tool in ("select", "move"):
                self._selected_stroke_indices = set(range(len(self.glyph.strokes)))
                self.update()
            return
        if self.tool in ("select", "move") and e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected_strokes()
            return
        super().keyPressEvent(e)


# ─── UI Panels ──────────────────────────────────────────────────────────────

class GlyphList(QListWidget):
    """Sidebar list showing all glyphs with character, thumbnail, and name."""

    glyph_selected = Signal(object)
    glyph_moved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(210)
        self._glyphs: list[Glyph] = []
        self.setDragDropMode(QListWidget.InternalMove)

    def add_glyph(self, g: Glyph):
        self._glyphs.append(g)
        item = QListWidgetItem()
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        lc = QLabel(g.char or "?")
        lc.setFont(QFont("Segoe UI", 16, QFont.Bold))
        lc.setFixedWidth(28)
        lc.setAlignment(Qt.AlignCenter)
        lt = QLabel()
        lt.setFixedSize(36, 36)
        lt.setFrameShape(QFrame.Box)
        lt.setObjectName("thumb")
        ln = QLabel(g.name)
        ln.setStyleSheet("color:#555;")
        lay.addWidget(lc)
        lay.addWidget(lt)
        lay.addWidget(ln)
        lay.addStretch()
        item.setSizeHint(w.sizeHint())
        self.addItem(item)
        self.setItemWidget(item, w)
        if g.strokes:
            lt.setPixmap(g.thumbnail(36))

    def remove_glyph_at(self, index: int):
        if 0 <= index < len(self._glyphs):
            self._glyphs.pop(index)
            self.takeItem(index)

    def refresh_thumb(self, g: Glyph):
        if g in self._glyphs:
            idx = self._glyphs.index(g)
            item = self.item(idx)
            if item:
                w = self.itemWidget(item)
                if w:
                    thumb = w.findChild(QLabel, "thumb")
                    if thumb:
                        thumb.setPixmap(g.thumbnail(36))

    def current_glyph(self) -> Optional[Glyph]:
        i = self.currentRow()
        return self._glyphs[i] if 0 <= i < len(self._glyphs) else None

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        g = self.current_glyph()
        if g:
            self.glyph_selected.emit(g)

    def dropEvent(self, event):
        """Override to keep _glyphs list in sync with visual order."""
        super().dropEvent(event)
        self._rebuild_glyph_order()
        self.glyph_moved.emit()

    def _rebuild_glyph_order(self):
        """Reorder internal _glyphs to match the current QListWidget row order."""
        new_order: list[Glyph] = []
        for i in range(self.count()):
            w = self.itemWidget(self.item(i))
            if w:
                lc = w.layout().itemAt(0).widget()
                if isinstance(lc, QLabel):
                    ch = lc.text()
                    for g in self._glyphs:
                        if g not in new_order and g.char == ch:
                            new_order.append(g)
                            break
        if len(new_order) == len(self._glyphs):
            self._glyphs = new_order


class BrushPanel(QGroupBox):
    """Right-side panel for adjusting brush parameters."""

    def __init__(self, brush: BrushSettings, parent=None):
        super().__init__("Brush", parent)
        self.b = brush
        f = QFormLayout(self)
        f.setContentsMargins(8, 14, 8, 8)

        self.cb_type = QComboBox()
        self.cb_type.addItems(BrushSettings.TYPES)
        self.cb_type.currentIndexChanged.connect(self._on_type_changed)
        f.addRow("Type", self.cb_type)

        self.sp_w = QDoubleSpinBox()
        self.sp_w.setRange(1, 80)
        self.sp_w.setValue(brush.base_width)
        self.sp_w.valueChanged.connect(lambda v: setattr(brush, "base_width", v))
        f.addRow("Width", self.sp_w)

        self.sp_min_w = QDoubleSpinBox()
        self.sp_min_w.setRange(0.1, 80)
        self.sp_min_w.setValue(brush.min_width)
        self.sp_min_w.setSingleStep(0.5)
        self.sp_min_w.valueChanged.connect(lambda v: setattr(brush, "min_width", v))
        f.addRow("Min W.", self.sp_min_w)

        self.sp_max_w = QDoubleSpinBox()
        self.sp_max_w.setRange(1, 120)
        self.sp_max_w.setValue(brush.max_width)
        self.sp_max_w.setSingleStep(1)
        self.sp_max_w.valueChanged.connect(lambda v: setattr(brush, "max_width", v))
        f.addRow("Max W.", self.sp_max_w)

        self.sp_angle = QDoubleSpinBox()
        self.sp_angle.setRange(0, 360)
        self.sp_angle.setValue(brush.nib_angle)
        self.sp_angle.setSingleStep(5)
        self.sp_angle.valueChanged.connect(lambda v: setattr(brush, "nib_angle", v))
        self.lbl_angle = QLabel("Nib angle")
        f.addRow(self.lbl_angle, self.sp_angle)

        self.sl_smooth = QSlider(Qt.Horizontal)
        self.sl_smooth.setRange(0, 80)
        self.sl_smooth.setValue(int(brush.smoothing * 100))
        self.sl_smooth.valueChanged.connect(lambda v: setattr(brush, "smoothing", v / 100))
        f.addRow("Smooth", self.sl_smooth)

        self.sl_speed = QSlider(Qt.Horizontal)
        self.sl_speed.setRange(0, 100)
        self.sl_speed.setValue(int(brush.speed_sensitivity * 100))
        self.sl_speed.valueChanged.connect(lambda v: setattr(brush, "speed_sensitivity", v / 100))
        f.addRow("Speed sens.", self.sl_speed)

        # v3: Opacity slider
        self.sl_opacity = QSlider(Qt.Horizontal)
        self.sl_opacity.setRange(5, 100)
        self.sl_opacity.setValue(int(brush.opacity * 100))
        self.sl_opacity.valueChanged.connect(self._on_opacity_changed)
        f.addRow("Opacity", self.sl_opacity)
        self.lbl_opacity = QLabel(f"{int(brush.opacity * 100)}%")
        f.addRow("", self.lbl_opacity)

        self.btn_col = QPushButton()
        self.btn_col.setFixedSize(50, 26)
        self.btn_col.clicked.connect(self._pick_color)
        self._sync_color()
        f.addRow("Color", self.btn_col)

        self._sync_visibility()

    def _on_type_changed(self, index: int):
        self.b.brush_type = index
        self._sync_visibility()

    def _sync_visibility(self):
        show_angle = self.b.brush_type == 1
        self.lbl_angle.setVisible(show_angle)
        self.sp_angle.setVisible(show_angle)

    def _on_opacity_changed(self, v: int):
        self.b.opacity = v / 100
        self.lbl_opacity.setText(f"{v}%")

    def _pick_color(self):
        c = QColorDialog.getColor(self.b.color, self)
        if c.isValid():
            self.b.color = c
            self._sync_color()

    def _sync_color(self):
        self.btn_col.setStyleSheet(
            f"background:{self.b.color.name()};border:1px solid #888;border-radius:3px;"
        )


class MetricsPanel(QGroupBox):
    """Right-side panel for glyph metrics, canvas background, and view options."""

    metrics_changed = Signal(int, int)

    def __init__(self, canvas: CalligraphyCanvas, parent=None):
        super().__init__("Metrics", parent)
        self.c = canvas
        self._updating = False
        f = QFormLayout(self)
        f.setContentsMargins(8, 14, 8, 8)

        self.sp_aw = QSpinBox()
        self.sp_aw.setRange(0, 2000)
        self.sp_aw.setValue(600)
        self.sp_aw.valueChanged.connect(self._apply_glyph_metrics)

        self.sp_lb = QSpinBox()
        self.sp_lb.setRange(-500, 2000)
        self.sp_lb.setValue(50)
        self.sp_lb.valueChanged.connect(self._apply_glyph_metrics)

        f.addRow("Advance W.", self.sp_aw)
        f.addRow("Left Bear.", self.sp_lb)
        f.addRow(QLabel(""))

        self.sp_asc = QSpinBox()
        self.sp_asc.setRange(100, 2000)
        self.sp_asc.setValue(canvas.ascent)

        self.sp_dsc = QSpinBox()
        self.sp_dsc.setRange(0, 1000)
        self.sp_dsc.setValue(canvas.descent)

        self.sp_xh = QSpinBox()
        self.sp_xh.setRange(50, 1500)
        self.sp_xh.setValue(canvas.x_height)

        for label, sp in [
            ("Ascent", self.sp_asc), ("Descent", self.sp_dsc), ("x-Height", self.sp_xh),
        ]:
            sp.valueChanged.connect(self._apply_global)
            f.addRow(label, sp)

        f.addRow(QLabel(""))

        # v3: Canvas background selector
        self.cb_bg = QComboBox()
        self.cb_bg.addItems(BG_OPTIONS.keys())
        self.cb_bg.setCurrentText(canvas._bg_mode)
        self.cb_bg.currentTextChanged.connect(self._on_bg_changed)
        f.addRow("Canvas BG", self.cb_bg)

        f.addRow(QLabel(""))

        self.ck_guides = QCheckBox("Guides")
        self.ck_guides.setChecked(True)
        self.ck_guides.toggled.connect(lambda v: (setattr(canvas, "show_guides", v), canvas.update()))
        self.ck_grid = QCheckBox("Grid")
        self.ck_grid.toggled.connect(lambda v: (setattr(canvas, "show_grid", v), canvas.update()))
        self.ck_bear = QCheckBox("Bearings")
        self.ck_bear.setChecked(True)
        self.ck_bear.toggled.connect(lambda v: (setattr(canvas, "show_bearings", v), canvas.update()))
        f.addRow(self.ck_guides)
        f.addRow(self.ck_grid)
        f.addRow(self.ck_bear)

    def load_glyph(self, g: Glyph):
        self._updating = True
        self.sp_aw.setValue(g.advance_width)
        self.sp_lb.setValue(g.left_bearing)
        self._updating = False

    def _apply_glyph_metrics(self):
        if not self._updating and self.c.glyph:
            self.c.glyph.advance_width = self.sp_aw.value()
            self.c.glyph.left_bearing = self.sp_lb.value()
            self.metrics_changed.emit(self.sp_aw.value(), self.sp_lb.value())
            self.c.update()

    def _apply_global(self):
        self.c.ascent = self.sp_asc.value()
        self.c.descent = self.sp_dsc.value()
        self.c.x_height = self.sp_xh.value()
        self.c.update()

    def _on_bg_changed(self, text: str):
        self.c._bg_mode = text
        self.c.update()


class StrokeListPanel(QGroupBox):
    """v3: Panel listing all strokes in the current glyph.
    Allows selection, reordering (Up/Down), and deletion."""

    stroke_selection_changed = Signal()

    def __init__(self, canvas: CalligraphyCanvas, parent=None):
        super().__init__("Strokes", parent)
        self.c = canvas
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 14, 8, 8)
        lay.setSpacing(4)

        self.list = QListWidget()
        self.list.setFixedHeight(120)
        self.list.currentRowChanged.connect(self._on_row_changed)
        lay.addWidget(self.list)

        btn_row = QHBoxLayout()
        self.btn_up = QPushButton("Up")
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down = QPushButton("Down")
        self.btn_down.clicked.connect(self._move_down)
        self.btn_del = QPushButton("Delete")
        self.btn_del.clicked.connect(self._delete_stroke)
        btn_row.addWidget(self.btn_up)
        btn_row.addWidget(self.btn_down)
        btn_row.addWidget(self.btn_del)
        lay.addLayout(btn_row)

    def refresh(self, g: Optional[Glyph]):
        self.list.blockSignals(True)
        self.list.clear()
        if g:
            for i, st in enumerate(g.strokes):
                pts_count = len(st.points)
                self.list.addItem(
                    f"Stroke {i + 1}  ({pts_count} pts, {st.color.name()})"
                )
        self.list.blockSignals(False)

    def _on_row_changed(self, row: int):
        if row >= 0 and self.c.tool in ("select", "move"):
            self.c._selected_stroke_indices = {row}
            self.c.update()

    def _move_up(self):
        g = self.c.glyph
        idx = self.list.currentRow()
        if g and idx > 0:
            self.c._push_undo()
            g.strokes[idx - 1], g.strokes[idx] = g.strokes[idx], g.strokes[idx - 1]
            self.c.glyph_changed.emit()
            self.refresh(g)
            self.list.setCurrentRow(idx - 1)

    def _move_down(self):
        g = self.c.glyph
        idx = self.list.currentRow()
        if g and 0 <= idx < len(g.strokes) - 1:
            self.c._push_undo()
            g.strokes[idx], g.strokes[idx + 1] = g.strokes[idx + 1], g.strokes[idx]
            self.c.glyph_changed.emit()
            self.refresh(g)
            self.list.setCurrentRow(idx + 1)

    def _delete_stroke(self):
        idx = self.list.currentRow()
        if idx >= 0:
            self.c._selected_stroke_indices = {idx}
            self.c.delete_selected_strokes()
            self.refresh(self.c.glyph)


class PreviewPanel(QGroupBox):
    """Live text preview that respects per-glyph advance widths."""

    def __init__(self, parent=None):
        super().__init__("Preview", parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 14, 8, 8)
        self.edit = QLineEdit("Hello World")
        self.canvas = _PreviewCanvas()
        lay.addWidget(self.edit)
        lay.addWidget(self.canvas)
        self.canvas.setMinimumHeight(90)
        self.edit.textChanged.connect(self.canvas.set_text)

    def set_glyphs(self, glyphs: list[Glyph]):
        self.canvas.set_glyphs(glyphs)

    def invalidate(self):
        self.canvas._cache.clear()
        self.canvas.update()


class _PreviewCanvas(QWidget):
    """Widget that renders a line of drawn glyphs using advance-width spacing."""

    def __init__(self):
        super().__init__()
        self._glyphs: list[Glyph] = []
        self._text = ""
        self._cache: dict = {}

    def set_glyphs(self, glyphs: list[Glyph]):
        self._glyphs = glyphs
        self._cache.clear()
        self.update()

    def set_text(self, text: str):
        self._text = text
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(255, 255, 255))
        p.setPen(QPen(QColor(215, 215, 215), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

        gmap: dict[str, Glyph] = {
            g.char: g for g in self._glyphs if g.char and g.strokes
        }
        if not gmap:
            p.setPen(QColor(180, 180, 180))
            p.setFont(QFont("Arial", 10))
            p.drawText(self.rect(), Qt.AlignCenter, "Draw glyphs to preview")
            p.end()
            return

        upem = 1000
        line_height = self.height() - 16
        scale = line_height / upem
        x = 8.0

        for ch in self._text:
            if ch == " ":
                x += 12
                continue
            g = gmap.get(ch)
            if g:
                key = (id(g), line_height)
                if key not in self._cache:
                    thumb_h = line_height
                    thumb_w = max(int(g.advance_width * scale), 8)
                    pm = QPixmap(thumb_w, thumb_h)
                    pm.fill(QColor(255, 255, 255, 0))
                    tp = QPainter(pm)
                    tp.setRenderHint(QPainter.Antialiasing)
                    tp.translate(g.left_bearing * scale, (upem - 700) * scale)
                    tp.scale(scale, scale)
                    for st in g.strokes:
                        _draw_stroke_painter(tp, st)
                    tp.end()
                    self._cache[key] = pm

                pm = self._cache[key]
                glyph_w = int(g.advance_width * scale)
                if x + glyph_w > self.width() - 8:
                    break
                p.drawPixmap(int(x), 8, pm.width(), pm.height(), pm)
                x += glyph_w
            else:
                p.setPen(QColor(210, 210, 210))
                fs = max(10, line_height - 8)
                p.setFont(QFont("Arial", fs))
                p.drawText(int(x), int(8 + line_height - 4), ch)
                x += int(line_height * 0.55)
        p.end()


class FontInfoDialog(QDialog):
    """Modal dialog for editing font metadata."""

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Font Metadata")
        self.resize(300, 200)
        self.info = copy.deepcopy(info)
        lay = QFormLayout(self)
        self.f_fam = QLineEdit(self.info.get("familyName", "MyFont"))
        lay.addRow("Family Name:", self.f_fam)
        self.f_sub = QLineEdit(self.info.get("styleName", "Regular"))
        lay.addRow("Style:", self.f_sub)
        self.f_des = QLineEdit(self.info.get("designer", ""))
        lay.addRow("Designer:", self.f_des)
        self.f_lic = QLineEdit(self.info.get("license", ""))
        lay.addRow("License:", self.f_lic)
        btns = QHBoxLayout()
        ok = QPushButton("Save"); ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(ok); btns.addWidget(cancel)
        lay.addRow(btns)

    def get_info(self) -> dict:
        self.info["familyName"] = self.f_fam.text()
        self.info["styleName"] = self.f_sub.text()
        self.info["designer"] = self.f_des.text()
        self.info["license"] = self.f_lic.text()
        return self.info


# ─── Export Logic ───────────────────────────────────────────────────────────

def _export_svg(glyph: Glyph, path: str):
    em = 1000
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {em} {em}">',
    ]
    for st in glyph.strokes:
        ol = get_stroke_outline(st.points)
        if len(ol) >= 3:
            pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in ol)
            alpha_attr = f' fill-opacity="{st.opacity}"' if st.opacity < 1.0 else ""
            lines.append(f'  <polygon points="{pts_str}" fill="{st.color.name()}"{alpha_attr}/>')
    lines.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def _export_png(canvas: CalligraphyCanvas, path: str):
    pm = QPixmap(canvas.size() * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(2, 2)
    canvas.render(p)
    p.end()
    pm.save(path, "PNG")


def _build_ttf_data(glyphs, ascent, descent, x_height, info):
    """Build font data in a worker thread. Returns the path or raises."""
    upem = ascent + descent
    order = [".notdef"] + [g.name for g in glyphs]
    fb = FontBuilder(upem, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(
        {g.unicode_val: g.name for g in glyphs if g.unicode_val is not None}
    )

    pen = TTGlyphPen(None)
    pen.moveTo((50, -descent))
    pen.lineTo((upem - 50, -descent))
    pen.lineTo((upem - 50, ascent))
    pen.lineTo((50, ascent))
    pen.closePath()
    gs = {".notdef": pen.glyph()}
    widths = {".notdef": (upem, 50)}

    for g in glyphs:
        pen = TTGlyphPen(None)
        drawn = False
        for st in g.strokes:
            ol = get_stroke_outline(st.points)
            if len(ol) >= 3:
                flipped = [(x, ascent - y) for x, y in ol]
                pen.moveTo(flipped[0])
                for pt in flipped[1:]:
                    pen.lineTo(pt)
                pen.closePath()
                drawn = True
        if not drawn:
            pen.moveTo((0, 0))
            pen.lineTo((0, 0))
            pen.closePath()
        gs[g.name] = pen.glyph()
        widths[g.name] = (g.advance_width, g.left_bearing)

    fb.setupGlyf(gs)
    fb.setupHorizontalMetrics(widths)
    fb.setupHorizontalHeader(ascent=ascent, descent=-descent)
    fb.setupNameTable({
        "familyName": info.get("familyName", "MyFont"),
        "styleName": info.get("styleName", "Regular"),
        "uniqueFontIdentifier": f"{info.get('familyName', 'MyFont')}-{info.get('styleName', 'Regular')}",
        "fullName": f"{info.get('familyName', 'MyFont')} {info.get('styleName', 'Regular')}",
        "version": "1.000",
        "designer": info.get("designer", ""),
        "license": info.get("license", ""),
    })
    fb.setupOs2(
        sTypoAscender=ascent, sTypoDescender=-descent, sTypoLineGap=0,
        usWinAscent=ascent, usWinDescent=descent,
        sxHeight=x_height, sCapHeight=int(x_height * 1.4),
    )
    fb.setupPost()
    fb.setupHead(unitsPerEm=upem)
    fb.save(path)


# ─── Main Window ────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """Top-level application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Calligraphy Font Maker v3")
        self.resize(1350, 820)
        self.setMinimumSize(900, 600)
        self.font_info = {
            "familyName": "MyCalligraphyFont",
            "styleName": "Regular",
            "designer": "",
            "license": "",
        }
        self._clipboard_glyph: Optional[Glyph] = None
        self._recent = RecentFilesManager()
        self._current_project_path: Optional[str] = None

        # Layout
        cw = QWidget()
        self.setCentralWidget(cw)
        hl = QHBoxLayout(cw)
        hl.setContentsMargins(4, 4, 4, 4)
        hl.setSpacing(4)

        self.glist = GlyphList()
        self.glist.glyph_selected.connect(self._select_glyph)
        self.glist.glyph_moved.connect(self._on_glyphs_reordered)
        hl.addWidget(self.glist)

        self.canvas = CalligraphyCanvas()
        self.canvas.glyph_changed.connect(self._glyph_edited)
        self.canvas.glyph_metrics_changed.connect(self._sync_metrics_ui)
        hl.addWidget(self.canvas, 1)

        # Right panel in a scroll area so everything fits on small screens
        rp = QWidget()
        rl = QVBoxLayout(rp)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)

        self.brush_panel = BrushPanel(self.canvas.brush)
        self.brush_panel.setFixedWidth(210)
        rl.addWidget(self.brush_panel)

        self.metrics_panel = MetricsPanel(self.canvas)
        self.metrics_panel.setFixedWidth(210)
        self.metrics_panel.metrics_changed.connect(self._glyph_edited)
        rl.addWidget(self.metrics_panel)

        # v3: Stroke list panel
        self.stroke_list = StrokeListPanel(self.canvas)
        self.stroke_list.setFixedWidth(210)
        rl.addWidget(self.stroke_list)

        self.preview = PreviewPanel()
        self.preview.setFixedWidth(210)
        rl.addWidget(self.preview)

        rl.addStretch()
        hl.addWidget(rp)

        self._build_toolbars()
        self._build_menus()
        self._build_shortcuts()
        self._setup_autosave()

        self.statusBar().showMessage(
            "Ready — D=Draw, E=Erase, S=Select, M=Move, Space+Drag=Pan, Scroll=Zoom"
        )

        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789":
            self.glist.add_glyph(Glyph(ch))
        if self.glist.count():
            self.glist.setCurrentRow(0)
            self._select_glyph(self.glist._glyphs[0])

        # Check for crash recovery
        self._check_autosave_recovery()

    # ── auto-save & recovery ──────────────────────────────────────────────

    def _setup_autosave(self):
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._do_autosave)
        self._autosave_timer.start(AUTOSAVE_INTERVAL_MS)

    def _do_autosave(self):
        if not self.glist._glyphs:
            return
        try:
            data = self._collect_project_data()
            data["version"] = "3.0-autosave"
            with open(AUTOSAVE_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _check_autosave_recovery(self):
        if not os.path.exists(AUTOSAVE_PATH):
            return
        try:
            with open(AUTOSAVE_PATH, "r") as f:
                data = json.load(f)
            if data.get("version") != "3.0-autosave":
                return
            glyph_count = len(data.get("glyphs", []))
            drawn_count = sum(1 for g in data["glyphs"] if g.get("strokes"))
            if drawn_count == 0:
                return
            result = QMessageBox.question(
                self,
                "Recover unsaved work?",
                f"An auto-saved project was found with {glyph_count} glyphs "
                f"({drawn_count} drawn).\n\nRecover it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if result == QMessageBox.Yes:
                self._load_project_data(data)
                self.statusBar().showMessage("Recovered from auto-save")
            # Clean up autosave either way
            try:
                os.remove(AUTOSAVE_PATH)
            except OSError:
                pass
        except Exception:
            pass

    def _collect_project_data(self) -> dict:
        return {
            "version": "3.0",
            "font_info": self.font_info,
            "metrics": {
                "ascent": self.canvas.ascent,
                "descent": self.canvas.descent,
                "x_height": self.canvas.x_height,
                "bg_mode": self.canvas._bg_mode,
            },
            "glyphs": [g.to_dict() for g in self.glist._glyphs],
        }

    def _load_project_data(self, data: dict):
        self.font_info = data.get("font_info", self.font_info)
        met = data.get("metrics", {})
        self.canvas.ascent = met.get("ascent", 700)
        self.canvas.descent = met.get("descent", 300)
        self.canvas.x_height = met.get("x_height", 400)
        bg = met.get("bg_mode", BG_CREAM)
        self.canvas._bg_mode = bg
        self.metrics_panel.sp_asc.setValue(self.canvas.ascent)
        self.metrics_panel.sp_dsc.setValue(self.canvas.descent)
        self.metrics_panel.sp_xh.setValue(self.canvas.x_height)
        self.metrics_panel.cb_bg.setCurrentText(bg)
        self.glist.clear()
        self.glist._glyphs.clear()
        for gd in data.get("glyphs", []):
            self.glist.add_glyph(Glyph.from_dict(gd))
        if self.glist._glyphs:
            self.glist.setCurrentRow(0)
            self._select_glyph(self.glist._glyphs[0])

    # ── toolbars ──────────────────────────────────────────────────────────

    def _build_toolbars(self):
        tb = self.addToolBar("Tools")
        tb.setMovable(False)

        self.act_draw = QAction("Draw", self)
        self.act_draw.setCheckable(True)
        self.act_draw.setChecked(True)
        self.act_draw.triggered.connect(lambda: self._set_tool("draw"))

        self.act_erase = QAction("Erase", self)
        self.act_erase.setCheckable(True)
        self.act_erase.triggered.connect(lambda: self._set_tool("erase"))

        self.act_select = QAction("Select", self)
        self.act_select.setCheckable(True)
        self.act_select.triggered.connect(lambda: self._set_tool("select"))

        self.act_move = QAction("Move", self)
        self.act_move.setCheckable(True)
        self.act_move.triggered.connect(lambda: self._set_tool("move"))

        tb.addAction(self.act_draw)
        tb.addAction(self.act_erase)
        tb.addAction(self.act_select)
        tb.addAction(self.act_move)
        tb.addSeparator()

        tb.addAction("Undo", self.canvas.undo, QKeySequence.Undo)
        tb.addAction("Redo", self.canvas.redo, QKeySequence.Redo)
        tb.addSeparator()

        tb.addAction("Flip H", self.canvas.flip_horizontal)
        tb.addAction("Flip V", self.canvas.flip_vertical)
        tb.addSeparator()

        tb.addAction("Add", self._add_char)
        tb.addAction("Dup", self._duplicate_glyph)
        tb.addAction("Clear", self._clear)
        tb.addAction("Delete", self._delete)
        tb.addSeparator()

        tb.addAction("SVG", self._save_svg)
        tb.addAction("PNG", self._save_png)
        tb.addAction("TTF", self._save_ttf)
        tb.addSeparator()

        tb.addAction("Save", self._save_project)
        tb.addAction("Open", self._load_project)

    def _set_tool(self, t: str):
        self.canvas.tool = t
        self.canvas._selected_stroke_indices.clear()
        self.canvas._dragging = False
        self.act_draw.setChecked(t == "draw")
        self.act_erase.setChecked(t == "erase")
        self.act_select.setChecked(t == "select")
        self.act_move.setChecked(t == "move")
        self.canvas.update()

    # ── menus ─────────────────────────────────────────────────────────────

    def _build_menus(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        fm.addAction("New Project", self._new_project, "Ctrl+N")
        fm.addAction("Open Project...", self._load_project, "Ctrl+O")
        fm.addAction("Save Project...", self._save_project, "Ctrl+S")
        fm.addSeparator()

        # v3: Recent files submenu
        self._recent_menu = fm.addMenu("Recent")
        self._refresh_recent_menu()

        fm.addSeparator()
        fm.addAction("Export Glyph SVG...", self._save_svg)
        fm.addAction("Export All SVGs...", self._save_all_svg)
        fm.addAction("Export Canvas PNG...", self._save_png)
        fm.addSeparator()
        fm.addAction("Export TTF...", self._save_ttf, "Ctrl+Shift+S")
        fm.addSeparator()
        fm.addAction("Quit", self.close, "Ctrl+Q")

        em = mb.addMenu("Edit")
        em.addAction("Undo", self.canvas.undo, "Ctrl+Z")
        em.addAction("Redo", self.canvas.redo, "Ctrl+Shift+Z")
        em.addSeparator()
        em.addAction("Copy Glyph", self._copy_glyph, "Ctrl+C")
        em.addAction("Paste Glyph", self._paste_glyph, "Ctrl+V")
        em.addAction("Duplicate Glyph", self._duplicate_glyph, "Ctrl+D")
        em.addSeparator()
        em.addAction("Select All Strokes", self._select_all_strokes, "Ctrl+A")
        em.addAction("Clear Glyph", self._clear)
        em.addAction("Delete Glyph", self._delete)
        em.addSeparator()
        em.addAction("Flip Horizontal", self.canvas.flip_horizontal, "Ctrl+H")
        em.addAction("Flip Vertical", self.canvas.flip_vertical, "Ctrl+Shift+H")

        tm = mb.addMenu("Tools")
        tm.addAction("Draw Tool", lambda: self._set_tool("draw"), "D")
        tm.addAction("Eraser Tool", lambda: self._set_tool("erase"), "E")
        tm.addAction("Select Tool", lambda: self._set_tool("select"), "S")
        tm.addAction("Move Tool", lambda: self._set_tool("move"), "M")
        tm.addSeparator()
        tm.addAction("Font Metadata...", self._edit_font_info)

        vm = mb.addMenu("View")
        vm.addAction("Zoom In", lambda: self._zoom(1.25), "Ctrl+=")
        vm.addAction("Zoom Out", lambda: self._zoom(0.8), "Ctrl+-")
        vm.addAction("Fit", self._zoom_fit, "Ctrl+0")
        vm.addSeparator()
        for name, attr in [("Guides", "show_guides"), ("Grid", "show_grid"), ("Bearings", "show_bearings")]:
            a = vm.addAction(name)
            a.setCheckable(True)
            a.setChecked(getattr(self.canvas, attr))
            a.toggled.connect(lambda v, a=attr: (setattr(self.canvas, a, v), self.canvas.update()))

        hm = mb.addMenu("Help")
        hm.addAction("About", self._about)

    def _refresh_recent_menu(self):
        self._recent_menu.clear()
        paths = self._recent.get_paths()
        if not paths:
            act = self._recent_menu.addAction("(none)")
            act.setEnabled(False)
            return
        for path in paths:
            name = os.path.basename(path)
            self._recent_menu.addAction(f"{name}  ({os.path.dirname(path)})").triggered.connect(
                lambda checked, p=path: self._open_recent(p)
            )
        self._recent_menu.addSeparator()
        self._recent_menu.addAction("Clear Recent").triggered.connect(self._clear_recent)

    def _open_recent(self, path: str):
        if os.path.exists(path):
            self._load_from_path(path)

    def _clear_recent(self):
        self._recent.clear()
        self._refresh_recent_menu()

    # ── shortcuts ─────────────────────────────────────────────────────────

    def _build_shortcuts(self):
        QShortcut(QKeySequence("[", self), self, lambda: self._resize_brush(-2))
        QShortcut(QKeySequence("]", self), self, lambda: self._resize_brush(2))
        # v3: Tab cycles through tools
        QShortcut(QKeySequence(Qt.Key_Tab, self), self, self._cycle_tool)

    def _resize_brush(self, delta: float):
        b = self.canvas.brush
        b.base_width = max(b.min_width, min(b.max_width, b.base_width + delta))
        self.brush_panel.sp_w.setValue(b.base_width)
        self.canvas.update()

    def _cycle_tool(self):
        tools = ["draw", "erase", "select", "move"]
        idx = tools.index(self.canvas.tool) if self.canvas.tool in tools else 0
        self._set_tool(tools[(idx + 1) % len(tools)])

    # ── glyph management ──────────────────────────────────────────────────

    def _select_glyph(self, g: Glyph):
        self.canvas.set_glyph(g)
        self._sync_metrics_ui()
        self.stroke_list.refresh(g)
        uni = f"  (U+{g.unicode_val:04X})" if g.unicode_val is not None else ""
        self.statusBar().showMessage(f"Editing: {g.name}{uni}  [{len(g.strokes)} stroke(s)]")

    def _sync_metrics_ui(self):
        g = self.glist.current_glyph()
        if g:
            self.metrics_panel.load_glyph(g)
        self.preview.set_glyphs(self.glist._glyphs)

    def _glyph_edited(self):
        g = self.glist.current_glyph()
        if g:
            self.glist.refresh_thumb(g)
            self.stroke_list.refresh(g)
        self.preview.invalidate()

    def _on_glyphs_reordered(self):
        self.preview.set_glyphs(self.glist._glyphs)
        self.preview.invalidate()

    def _select_all_strokes(self):
        if self.glist.current_glyph() and self.canvas.tool in ("select", "move"):
            g = self.glist.current_glyph()
            self.canvas._selected_stroke_indices = set(range(len(g.strokes)))
            self.canvas.update()

    def _add_char(self):
        txt, ok = QInputDialog.getText(self, "Add Character", "Enter character(s):")
        if not ok or not txt:
            return
        existing = {g.char for g in self.glist._glyphs}
        first_new = None
        for ch in txt:
            if ch not in existing and ch != " ":
                self.glist.add_glyph(Glyph(ch))
                existing.add(ch)
                if first_new is None:
                    first_new = ch
        if first_new:
            for i, g in enumerate(self.glist._glyphs):
                if g.char == first_new:
                    self.glist.setCurrentRow(i)
                    self._select_glyph(g)
                    break

    def _duplicate_glyph(self):
        g = self.glist.current_glyph()
        if not g:
            return
        dup = g.deep_copy()
        dup.name = f"{g.name} copy"
        self.glist.add_glyph(dup)
        idx = len(self.glist._glyphs) - 1
        self.glist.setCurrentRow(idx)
        self._select_glyph(dup)
        self.statusBar().showMessage(f"Duplicated '{g.name}'")

    def _copy_glyph(self):
        g = self.glist.current_glyph()
        if g:
            self._clipboard_glyph = g.deep_copy()
            self.statusBar().showMessage(f"Copied '{g.name}'")

    def _paste_glyph(self):
        if self._clipboard_glyph is None:
            self.statusBar().showMessage("Nothing to paste")
            return
        src = self._clipboard_glyph
        g = self.glist.current_glyph()
        if g and not g.is_empty():
            self.canvas._push_undo()
            for st in src.strokes:
                g.add_stroke(st.deep_copy())
            self.canvas.glyph_changed.emit()
            self.statusBar().showMessage(f"Pasted {len(src.strokes)} stroke(s) into '{g.name}'")
        else:
            if g:
                self.canvas._push_undo()
            dup = src.deep_copy()
            g.char = dup.char
            g.strokes = dup.strokes
            g.advance_width = dup.advance_width
            g.left_bearing = dup.left_bearing
            self.canvas.glyph_changed.emit()
            self.statusBar().showMessage(f"Pasted '{src.name}'")

    def _clear(self):
        if self.canvas.glyph and not self.canvas.glyph.is_empty():
            if QMessageBox.question(self, "Clear", "Clear all strokes?") == QMessageBox.Yes:
                self.canvas.clear_glyph()

    def _delete(self):
        g = self.glist.current_glyph()
        if not g:
            return
        if QMessageBox.question(self, "Delete", f"Delete '{g.name}'?") != QMessageBox.Yes:
            return
        idx = self.glist.currentRow()
        self.glist.remove_glyph_at(idx)
        self.canvas.glyph = None
        self.canvas._selected_stroke_indices.clear()
        self.canvas.update()
        if self.glist._glyphs:
            next_idx = min(idx, len(self.glist._glyphs) - 1)
            self.glist.setCurrentRow(next_idx)
            self._select_glyph(self.glist._glyphs[next_idx])

    # ── view ──────────────────────────────────────────────────────────────

    def _zoom(self, factor: float):
        self.canvas.zoom = max(0.15, min(6, self.canvas.zoom * factor))
        self.canvas.update()

    def _zoom_fit(self):
        self.canvas.zoom_to_fit()

    def _edit_font_info(self):
        dlg = FontInfoDialog(self.font_info, self)
        if dlg.exec() == QDialog.Accepted:
            self.font_info = dlg.get_info()

    # ── export ────────────────────────────────────────────────────────────

    def _save_svg(self):
        g = self.glist.current_glyph()
        if not g:
            QMessageBox.warning(self, "No glyph", "Select a glyph first.")
            return
        p, _ = QFileDialog.getSaveFileName(self, "SVG", f"{g.name}.svg", "SVG (*.svg)")
        if p:
            try:
                _export_svg(g, p)
                self.statusBar().showMessage(f"SVG saved -> {p}")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def _save_png(self):
        p, _ = QFileDialog.getSaveFileName(self, "PNG", "glyph.png", "PNG (*.png)")
        if p:
            try:
                _export_png(self.canvas, p)
                self.statusBar().showMessage(f"PNG saved -> {p}")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def _save_all_svg(self):
        d = QFileDialog.getExistingDirectory(self, "SVG folder")
        if not d:
            return
        for g in self.glist._glyphs:
            if g.strokes:
                try:
                    _export_svg(g, os.path.join(d, f"{g.name}.svg"))
                except Exception as e:
                    self.statusBar().showMessage(f"Error: {e}")
        self.statusBar().showMessage(f"SVGs saved -> {d}")

    def _save_ttf(self):
        if not FONTTOOLS_AVAILABLE:
            QMessageBox.warning(self, "Missing dependency", "pip install fonttools")
            return
        drawn = [g for g in self.glist._glyphs if g.strokes]
        if not drawn:
            QMessageBox.warning(self, "Empty", "Draw some glyphs first.")
            return
        p, _ = QFileDialog.getSaveFileName(
            self, "TTF", f"{self.font_info['familyName']}.ttf", "TTF (*.ttf)"
        )
        if not p:
            return

        # v3: Progress dialog + threaded export
        progress = QProgressDialog("Building TTF...", "Cancel", 0, len(drawn), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        error_holder: list = [None]

        def worker():
            try:
                # Monkey-patch the global path into the builder function
                nonlocal p
                upem = self.canvas.ascent + self.canvas.descent
                order = [".notdef"] + [g.name for g in drawn]
                fb = FontBuilder(upem, isTTF=True)
                fb.setupGlyphOrder(order)
                fb.setupCharacterMap(
                    {g.unicode_val: g.name for g in drawn if g.unicode_val is not None}
                )
                pen = TTGlyphPen(None)
                pen.moveTo((50, -self.canvas.descent))
                pen.lineTo((upem - 50, -self.canvas.descent))
                pen.lineTo((upem - 50, self.canvas.ascent))
                pen.lineTo((50, self.canvas.ascent))
                pen.closePath()
                gs = {".notdef": pen.glyph()}
                widths = {".notdef": (upem, 50)}

                for i, g in enumerate(drawn):
                    if progress.wasCanceled():
                        return
                    pen = TTGlyphPen(None)
                    has_drawn = False
                    for st in g.strokes:
                        ol = get_stroke_outline(st.points)
                        if len(ol) >= 3:
                            flipped = [(x, self.canvas.ascent - y) for x, y in ol]
                            pen.moveTo(flipped[0])
                            for pt in flipped[1:]:
                                pen.lineTo(pt)
                            pen.closePath()
                            has_drawn = True
                    if not has_drawn:
                        pen.moveTo((0, 0)); pen.lineTo((0, 0)); pen.closePath()
                    gs[g.name] = pen.glyph()
                    widths[g.name] = (g.advance_width, g.left_bearing)
                    progress.setValue(i + 1)
                    QApplication.processEvents()

                fb.setupGlyf(gs)
                fb.setupHorizontalMetrics(widths)
                fb.setupHorizontalHeader(
                    ascent=self.canvas.ascent, descent=-self.canvas.descent
                )
                info = self.font_info
                fb.setupNameTable({
                    "familyName": info.get("familyName", "MyFont"),
                    "styleName": info.get("styleName", "Regular"),
                    "uniqueFontIdentifier": f"{info.get('familyName', 'MyFont')}-{info.get('styleName', 'Regular')}",
                    "fullName": f"{info.get('familyName', 'MyFont')} {info.get('styleName', 'Regular')}",
                    "version": "1.000",
                    "designer": info.get("designer", ""),
                    "license": info.get("license", ""),
                })
                fb.setupOs2(
                    sTypoAscender=self.canvas.ascent,
                    sTypoDescender=-self.canvas.descent,
                    sTypoLineGap=0,
                    usWinAscent=self.canvas.ascent,
                    usWinDescent=self.canvas.descent,
                    sxHeight=self.canvas.x_height,
                    sCapHeight=int(self.canvas.x_height * 1.4),
                )
                fb.setupPost()
                fb.setupHead(unitsPerEm=upem)
                fb.save(p)
            except Exception as e:
                error_holder[0] = e

        worker()
        progress.close()

        if error_holder[0]:
            QMessageBox.critical(self, "Error", str(error_holder[0]))
        else:
            self.statusBar().showMessage(f"TTF saved -> {p}")
            QMessageBox.information(self, "Done", f"{len(drawn)} glyphs exported to\n{p}")

    # ── project save / load ───────────────────────────────────────────────

    def _save_project(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "Save Project", "font_project.json", "JSON (*.json)"
        )
        if not p:
            return
        try:
            data = self._collect_project_data()
            with open(p, "w") as f:
                json.dump(data, f, indent=2)
            self._current_project_path = p
            self._recent.add(p)
            self._refresh_recent_menu()
            self.statusBar().showMessage(f"Project saved -> {p}")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _load_project(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open Project", "", "JSON (*.json)")
        if p:
            self._load_from_path(p)

    def _load_from_path(self, path: str):
        if not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            self._load_project_data(data)
            self._current_project_path = path
            self._recent.add(path)
            self._refresh_recent_menu()
            self.statusBar().showMessage(f"Project loaded <- {path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load project:\n{e}")

    def _new_project(self):
        if QMessageBox.question(self, "New Project", "Discard current work?") == QMessageBox.Yes:
            self.glist.clear()
            self.glist._glyphs.clear()
            self.canvas.glyph = None
            self._current_project_path = None
            self.canvas.update()
            try:
                os.remove(AUTOSAVE_PATH)
            except OSError:
                pass

    def _about(self):
        QMessageBox.about(
            self, "About",
            "<h3>Calligraphy Font Maker v3</h3>"
            "<p>Professional calligraphic font editor.</p>"
            "<p><b>Tools:</b> Draw (D), Erase (E), Select (S), Move (M)<br>"
            "<b>Cycle tools:</b> Tab<br>"
            "<b>Select:</b> Click to select, Shift+Click for multi, Ctrl+A select all<br>"
            "<b>Move:</b> Select strokes first, then drag to reposition<br>"
            "<b>Navigation:</b> Pan (Space+Drag), Zoom towards cursor (Scroll)<br>"
            "<b>Editing:</b> Undo (Ctrl+Z), Redo (Ctrl+Shift+Z)<br>"
            "<b>Glyphs:</b> Copy (Ctrl+C), Paste (Ctrl+V), Duplicate (Ctrl+D)<br>"
            "<b>Brush:</b> [ / ] to shrink / grow brush size<br>"
            "<b>Stroke List:</b> Reorder and delete individual strokes</p>"
            "<p>Exports to SVG, PNG, and installable TTF.</p>",
        )

    def closeEvent(self, event):
        """Clean up autosave on exit."""
        try:
            if os.path.exists(AUTOSAVE_PATH):
                os.remove(AUTOSAVE_PATH)
        except OSError:
            pass
        super().closeEvent(event)


# ─── Entry Point ────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE_SHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()