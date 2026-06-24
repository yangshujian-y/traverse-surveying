from __future__ import annotations

import csv
import os
import re
import shutil
import sys
import threading
import time
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
DEFAULT_MARGIN = 0.06


def natural_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTS


def safe_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def parse_color(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return "#000000"


@dataclass
class Box:
    left: float
    top: float
    right: float
    bottom: float

    def normalized(self) -> "Box":
        return Box(
            min(self.left, self.right),
            min(self.top, self.bottom),
            max(self.left, self.right),
            max(self.top, self.bottom),
        )

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        box = self.normalized()
        return (
            int(box.left * width),
            int(box.top * height),
            int(box.right * width),
            int(box.bottom * height),
        )


@dataclass(frozen=True)
class Settings:
    source_dir: str
    output_dir: str
    copy_file_0005: str
    copy_file_0007: str
    copy_serial: str
    paginate_recursive: bool
    paginate_include_root: bool
    paginate_each_folder: bool
    font_size: str
    font_color: str
    start_page: str
    page_prefix: str
    page_suffix: str
    file_suffix: str
    erase_old_page_numbers: bool
    worker_count: str
    open_output_after: bool


def default_boxes() -> dict[str, Box]:
    return {
        "portrait_front": Box(0.88, 0.025, 0.965, 0.065),
        "portrait_back": Box(0.035, 0.025, 0.12, 0.065),
        "landscape_front": Box(0.90, 0.03, 0.975, 0.085),
        "landscape_back": Box(0.025, 0.03, 0.10, 0.085),
    }


class ArchiveImageTool(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("归档图片批量复制与页码工具")
        self.geometry("1180x760")
        self.minsize(1060, 680)

        self.source_dir = tk.StringVar()
        self.copy_image = tk.StringVar()
        self.copy_file_0005 = tk.StringVar()
        self.copy_file_0007 = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.sample_image = tk.StringVar()

        self.copy_serial = tk.StringVar(value="-0002")

        self.paginate_recursive = tk.BooleanVar(value=True)
        self.paginate_include_root = tk.BooleanVar(value=False)
        self.paginate_each_folder = tk.BooleanVar(value=False)
        self.keep_original_format = tk.BooleanVar(value=True)
        self.open_output_after = tk.BooleanVar(value=False)

        self.font_size = tk.StringVar(value="36")
        self.font_color = tk.StringVar(value="#000000")
        self.start_page = tk.StringVar(value="1")
        self.page_prefix = tk.StringVar(value="")
        self.page_suffix = tk.StringVar(value="")
        self.file_suffix = tk.StringVar(value="_页码")
        self.erase_old_page_numbers = tk.BooleanVar(value=True)
        self.worker_count = tk.StringVar(value="4")

        self.boxes = default_boxes()
        self.active_box_key = tk.StringVar(value="portrait_front")
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.preview_image_size = (1, 1)
        self.preview_draw_rect: int | None = None
        self.preview_image_id: int | None = None
        self.preview_zoom = tk.DoubleVar(value=1.6)
        self.show_page_preview = tk.BooleanVar(value=True)
        self.preview_page_text = tk.StringVar(value="1")
        self.preview_source_image: Image.Image | None = None
        self.preview_render_size = (1, 1)
        self.preview_offset = (0, 0)
        self.drag_start: tuple[int, int] | None = None

        self.status_var = tk.StringVar(value="请选择源文件夹、0005文件、0007文件和输出文件夹。")
        self.progress = tk.DoubleVar(value=0)
        self.log_rows: list[dict[str, object]] = []
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self._build_ui()
        self.bind_preview_traces()
        self.process_ui_queue()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self._build_path_panel(top)

        main_pane = ttk.PanedWindow(self, orient="vertical")
        main_pane.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 8))

        notebook_frame = ttk.Frame(main_pane)
        notebook_frame.rowconfigure(0, weight=1)
        notebook_frame.columnconfigure(0, weight=1)
        main_pane.add(notebook_frame, weight=4)

        notebook = ttk.Notebook(notebook_frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        delivery_outer, delivery_tab = self.scrollable_tab(notebook)
        delivery_tab.columnconfigure(0, weight=1)
        notebook.add(delivery_outer, text="0005 / 0007 投放")
        self._build_copy_panel(delivery_tab)
        self._build_suffix_copy_panel(delivery_tab)

        paginate_outer, paginate_tab = self.scrollable_tab(notebook)
        paginate_tab.columnconfigure(0, weight=1)
        notebook.add(paginate_outer, text="图片编页")
        self._build_paginate_panel(paginate_tab)

        preview_tab = ttk.Frame(notebook, padding=12)
        preview_tab.rowconfigure(2, weight=1)
        preview_tab.columnconfigure(0, weight=1)
        notebook.add(preview_tab, text="预览框选")
        self._build_preview_panel(preview_tab)

        stats_outer, stats_tab = self.scrollable_tab(notebook)
        stats_tab.columnconfigure(0, weight=1)
        notebook.add(stats_outer, text="统计")
        self._build_action_panel(stats_tab)

        log_area = ttk.Frame(main_pane)
        log_area.columnconfigure(0, weight=1)
        main_pane.add(log_area, weight=1)
        self._build_log_panel(log_area)
        self._build_status_bar(self)

    def scrollable_tab(self, notebook: ttk.Notebook) -> tuple[ttk.Frame, ttk.Frame]:
        outer = ttk.Frame(notebook)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = ttk.Frame(canvas, padding=12)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def update_scroll_region(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def update_inner_width(event: tk.Event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def on_mousewheel(event: tk.Event) -> None:
            delta = int(-1 * (event.delta / 120))
            if delta == 0 and event.delta:
                delta = -1 if event.delta > 0 else 1
            canvas.yview_scroll(delta, "units")

        inner.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", update_inner_width)
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_mousewheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
        return outer, inner

    def bind_preview_traces(self) -> None:
        for variable in (self.font_size, self.font_color, self.preview_page_text):
            variable.trace_add("write", lambda *_args: self.render_preview_image())

    def _build_path_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="路径")
        panel.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        panel.columnconfigure(1, weight=1)

        self._path_row(panel, 0, "源文件夹", self.source_dir, self.choose_source_dir)
        self._path_row(panel, 1, "0005文件", self.copy_file_0005, self.choose_copy_file_0005)
        self._path_row(panel, 2, "0007文件", self.copy_file_0007, self.choose_copy_file_0007)
        self._path_row(panel, 3, "输出文件夹", self.output_dir, self.choose_output_dir)

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Callable[[], None],
    ) -> None:
        ttk.Label(parent, text=label, width=10).grid(row=row, column=0, sticky="w", padx=8, pady=5)
        ttk.Entry(parent, textvariable=variable, width=42).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(parent, text="选择", command=command).grid(row=row, column=2, padx=6, pady=5)

    def _build_copy_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="文件命名设置")
        panel.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text="编号").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        ttk.Entry(panel, textvariable=self.copy_serial, width=12).grid(row=0, column=1, sticky="ew", pady=8)
        ttk.Label(panel, text="例：文件夹名称-0002.jpg").grid(row=0, column=2, sticky="w", padx=6, pady=8)

    def _build_suffix_copy_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="按文件夹后缀投放 0005 / 0007")
        panel.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(
            panel,
            text="先把源目录完整复制到输出文件夹，再遍历所有第 3 级、名称以 0005 或 0007 结尾的文件夹。",
            wraplength=360,
        ).pack(fill="x", padx=8, pady=(6, 3))
        ttk.Button(panel, text="执行 0005 / 0007 投放", command=self.copy_suffix_files_only).pack(
            fill="x", padx=8, pady=8
        )

    def _build_paginate_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="图片编页：正面右上角，反面左上角")
        panel.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        panel.columnconfigure(1, weight=1)

        ttk.Checkbutton(panel, text="递归处理所有子文件夹", variable=self.paginate_recursive).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=3
        )
        ttk.Checkbutton(panel, text="同时处理源文件夹本身", variable=self.paginate_include_root).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=3
        )
        ttk.Label(panel, text="编号规则：按第 3 级文件夹名称顺序，再按其中图片名称顺序，所有图片连续编号。").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=3
        )
        ttk.Checkbutton(panel, text="编页前智能擦除四角旧页码", variable=self.erase_old_page_numbers).grid(
            row=3, column=0, columnspan=3, sticky="w", padx=8, pady=3
        )

        row = 4
        for label, var in (
            ("起始页码", self.start_page),
            ("字体大小", self.font_size),
            ("并行线程数", self.worker_count),
            ("页码前缀", self.page_prefix),
            ("页码后缀", self.page_suffix),
            ("输出文件名后缀", self.file_suffix),
        ):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
            ttk.Entry(panel, textvariable=var, width=16).grid(row=row, column=1, sticky="ew", pady=5)
            row += 1

        ttk.Label(panel, text="颜色").grid(row=row, column=0, sticky="w", padx=8, pady=5)
        ttk.Entry(panel, textvariable=self.font_color, width=16).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(panel, text="选色", command=self.choose_color).grid(row=row, column=2, padx=6, pady=5)
        row += 1

        ttk.Label(panel, text="页码位置").grid(row=row, column=0, sticky="w", padx=8, pady=5)
        values = (
            "portrait_front 竖版正面右上角",
            "portrait_back 竖版反面左上角",
            "landscape_front 横版正面右上角",
            "landscape_back 横版反面左上角",
        )
        combo = ttk.Combobox(panel, values=values, state="readonly")
        combo.current(0)
        combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 6), pady=5)
        combo.bind("<<ComboboxSelected>>", self.on_box_choice)
        row += 1

        ttk.Button(panel, text="恢复默认页码位置", command=self.reset_boxes).grid(
            row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=6
        )
        row += 1
        ttk.Button(panel, text="执行编页", command=self.paginate_only).grid(
            row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=6
        )

    def _build_action_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="校核统计")
        panel.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(panel, text="只统计文件夹文件数量", command=self.stats_only).pack(fill="x", padx=8, pady=6)
        ttk.Button(panel, text="复制 + 编页 + 统计", command=self.run_all).pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(panel, text="处理完成后尝试打开输出文件夹", variable=self.open_output_after).pack(
            anchor="w", padx=8, pady=4
        )

    def _build_preview_panel(self, parent: ttk.Frame) -> None:
        path_panel = ttk.LabelFrame(parent, text="预览图片")
        path_panel.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        path_panel.columnconfigure(1, weight=1)
        self._path_row(path_panel, 0, "预览图片", self.sample_image, self.choose_sample_image)

        top = ttk.Frame(parent)
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(
            top,
            text="在预览图上拖拽框选页码区域；可分别调整竖版/横版、正面右上角/反面左上角。",
        ).grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 6))
        ttk.Button(top, text="使用0005文件预览", command=self.use_0005_file_as_sample).grid(row=1, column=0, padx=(0, 6))
        ttk.Button(top, text="放大", command=lambda: self.change_preview_zoom(1.25)).grid(row=1, column=1, sticky="w", padx=3)
        ttk.Button(top, text="缩小", command=lambda: self.change_preview_zoom(0.8)).grid(row=1, column=2, padx=3)
        ttk.Button(top, text="适应窗口", command=self.fit_preview_to_canvas).grid(row=1, column=3, padx=3)
        ttk.Checkbutton(top, text="显示页码效果", variable=self.show_page_preview, command=self.render_preview_image).grid(row=1, column=4, padx=8)
        ttk.Label(top, text="示例页码").grid(row=1, column=5, padx=(8, 3))
        ttk.Entry(top, textvariable=self.preview_page_text, width=8).grid(row=1, column=6, padx=3)
        ttk.Button(top, text="刷新预览", command=self.render_preview_image).grid(row=1, column=7, padx=3)

        self.canvas = tk.Canvas(parent, bg="#f2f2f2", highlightthickness=1, highlightbackground="#cccccc")
        self.canvas.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_press)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Configure>", lambda _event: self.render_preview_image())

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="运行日志")
        panel.grid(row=0, column=0, sticky="ew")
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        self.log = tk.Text(panel, height=5, wrap="word")
        self.log.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(panel, orient="vertical", command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.insert("end", "运行日志会显示在这里。点击执行后，会实时显示扫描、复制、编页和完成情况。\n")

    def _build_status_bar(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, padding=(12, 0, 12, 10))
        panel.grid(row=3, column=0, sticky="ew")
        panel.columnconfigure(0, weight=1)
        ttk.Progressbar(panel, variable=self.progress, maximum=100).grid(row=0, column=0, sticky="ew")
        ttk.Label(panel, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(4, 0))

    def choose_source_dir(self) -> None:
        path = filedialog.askdirectory(title="选择源文件夹")
        if path:
            self.source_dir.set(path)
            if not self.output_dir.get():
                self.output_dir.set(str(Path(path).parent / f"{Path(path).name}_输出"))

    def choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="选择输出文件夹")
        if path:
            self.output_dir.set(path)

    def choose_copy_image(self) -> None:
        path = filedialog.askopenfilename(title="选择要批量复制的图片", filetypes=[("图片", "*.jpg *.jpeg *.png *.tif *.tiff *.bmp *.webp"), ("全部文件", "*.*")])
        if path:
            self.copy_image.set(path)
            if not self.sample_image.get():
                self.sample_image.set(path)
                self.load_preview_image()

    def choose_copy_file_0005(self) -> None:
        path = filedialog.askopenfilename(title="选择要复制到 0005 文件夹的文件", filetypes=[("全部文件", "*.*")])
        if path:
            self.copy_file_0005.set(path)

    def choose_copy_file_0007(self) -> None:
        path = filedialog.askopenfilename(title="选择要复制到 0007 文件夹的文件", filetypes=[("全部文件", "*.*")])
        if path:
            self.copy_file_0007.set(path)

    def choose_sample_image(self) -> None:
        path = filedialog.askopenfilename(title="选择用于框选页码位置的预览图片", filetypes=[("图片", "*.jpg *.jpeg *.png *.tif *.tiff *.bmp *.webp"), ("全部文件", "*.*")])
        if path:
            self.sample_image.set(path)
            self.load_preview_image()

    def choose_color(self) -> None:
        color = colorchooser.askcolor(color=self.font_color.get())[1]
        if color:
            self.font_color.set(color)

    def use_0005_file_as_sample(self) -> None:
        if self.copy_file_0005.get():
            self.sample_image.set(self.copy_file_0005.get())
            self.load_preview_image()

    def reset_boxes(self) -> None:
        self.boxes = default_boxes()
        self.draw_saved_box()

    def on_box_choice(self, event: tk.Event) -> None:
        selected = event.widget.get()
        key = selected.split()[0] if selected else "portrait_front"
        if key in self.boxes:
            self.active_box_key.set(key)
            self.render_preview_image()

    def change_preview_zoom(self, factor: float) -> None:
        current = self.preview_zoom.get()
        self.preview_zoom.set(max(0.4, min(6.0, current * factor)))
        self.render_preview_image()

    def fit_preview_to_canvas(self) -> None:
        self.preview_zoom.set(1.0)
        self.render_preview_image()

    def load_preview_image(self, redraw_only: bool = False) -> None:
        path = self.sample_image.get()
        if not path:
            return
        try:
            self.preview_source_image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        except Exception as exc:
            if not redraw_only:
                messagebox.showerror("预览失败", str(exc))
            return
        self.render_preview_image()

    def render_preview_image(self) -> None:
        if self.preview_source_image is None:
            return
        source = self.preview_source_image
        self.preview_image_size = source.size
        canvas_width = max(self.canvas.winfo_width() - 24, 100)
        canvas_height = max(self.canvas.winfo_height() - 24, 100)
        fit_scale = min(canvas_width / source.width, canvas_height / source.height)
        scale = max(0.1, fit_scale * self.preview_zoom.get())
        render_size = (max(1, int(source.width * scale)), max(1, int(source.height * scale)))
        image = source.resize(render_size, Image.Resampling.LANCZOS)
        self.draw_page_text_preview(image)
        self.preview_render_size = render_size
        self.preview_photo = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        x = max((self.canvas.winfo_width() - render_size[0]) // 2, 10)
        y = max((self.canvas.winfo_height() - render_size[1]) // 2, 10)
        self.preview_offset = (x, y)
        self.preview_image_id = self.canvas.create_image(x, y, anchor="nw", image=self.preview_photo)
        self.draw_saved_box()

    def draw_page_text_preview(self, image: Image.Image) -> None:
        if not self.show_page_preview.get():
            return
        key = self.active_box_key.get()
        if key not in self.boxes:
            key = "portrait_front"
        x0, y0, x1, y1 = self.boxes[key].to_pixels(image.width, image.height)
        draw = ImageDraw.Draw(image)
        font_size = max(8, int(safe_int(self.font_size.get(), 36) * image.width / max(self.preview_image_size[0], 1)))
        font = self.load_font(font_size)
        text = self.preview_page_text.get().strip() or "1"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = x0 + max(0, (x1 - x0 - text_width) // 2)
        y = y0 + max(0, (y1 - y0 - text_height) // 2)
        draw.text((x, y), text, fill=parse_color(self.font_color.get()), font=font)

    def draw_saved_box(self) -> None:
        self.canvas.delete("page_box")
        key = self.active_box_key.get()
        if key not in self.boxes:
            key = "portrait_front"
            self.active_box_key.set(key)
        box = self.boxes[key].normalized()
        width, height = self.preview_render_size
        x0, y0 = self.image_to_canvas(box.left * width, box.top * height)
        x1, y1 = self.image_to_canvas(box.right * width, box.bottom * height)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline="#e53935", width=2, tags="page_box")

    def image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        ox, oy = self.preview_offset
        return ox + x, oy + y

    def canvas_to_ratio(self, x: int, y: int) -> tuple[float, float]:
        ox, oy = self.preview_offset
        width, height = self.preview_render_size
        rx = max(0.0, min(1.0, (x - ox) / max(width, 1)))
        ry = max(0.0, min(1.0, (y - oy) / max(height, 1)))
        return rx, ry

    def on_canvas_press(self, event: tk.Event) -> None:
        self.drag_start = (event.x, event.y)
        self.canvas.delete("drag_box")

    def on_canvas_drag(self, event: tk.Event) -> None:
        if not self.drag_start:
            return
        self.canvas.delete("drag_box")
        x0, y0 = self.drag_start
        self.canvas.create_rectangle(x0, y0, event.x, event.y, outline="#1565c0", width=2, tags="drag_box")

    def on_canvas_release(self, event: tk.Event) -> None:
        if not self.drag_start:
            return
        x0, y0 = self.drag_start
        x1, y1 = event.x, event.y
        self.drag_start = None
        rx0, ry0 = self.canvas_to_ratio(x0, y0)
        rx1, ry1 = self.canvas_to_ratio(x1, y1)
        if abs(rx1 - rx0) < 0.01 or abs(ry1 - ry0) < 0.01:
            return
        self.boxes[self.active_box_key.get()] = Box(rx0, ry0, rx1, ry1).normalized()
        self.canvas.delete("drag_box")
        self.render_preview_image()

    def settings_snapshot(self) -> Settings:
        return Settings(
            source_dir=self.source_dir.get(),
            output_dir=self.output_dir.get(),
            copy_file_0005=self.copy_file_0005.get(),
            copy_file_0007=self.copy_file_0007.get(),
            copy_serial=self.copy_serial.get(),
            paginate_recursive=self.paginate_recursive.get(),
            paginate_include_root=self.paginate_include_root.get(),
            paginate_each_folder=self.paginate_each_folder.get(),
            font_size=self.font_size.get(),
            font_color=self.font_color.get(),
            start_page=self.start_page.get(),
            page_prefix=self.page_prefix.get(),
            page_suffix=self.page_suffix.get(),
            file_suffix=self.file_suffix.get(),
            erase_old_page_numbers=self.erase_old_page_numbers.get(),
            worker_count=self.worker_count.get(),
            open_output_after=self.open_output_after.get(),
        )

    def run_in_thread(self, title: str, worker: Callable[[Settings], None]) -> None:
        settings = self.settings_snapshot()

        def wrapped() -> None:
            try:
                self.set_status(f"{title}中...")
                self.set_progress(0)
                worker(settings)
                self.set_progress(100)
                self.set_status(f"{title}完成。")
                self.log_step(f"{title}已全部执行完毕。")
                if settings.open_output_after and settings.output_dir:
                    self.open_output_folder(settings.output_dir)
            except Exception as exc:
                error_text = str(exc)
                self.set_status(f"{title}失败。")
                self.log_step(f"{title}失败：{error_text}")
                self.after(0, lambda: messagebox.showerror(f"{title}失败", error_text))

        threading.Thread(target=wrapped, daemon=True).start()

    def copy_suffix_files_only(self) -> None:
        self.run_in_thread("0005 / 0007 投放", lambda settings: self.copy_files_to_suffix_folders(settings, write_report=True))

    def paginate_only(self) -> None:
        self.run_in_thread("编页", lambda settings: self.paginate_images(settings, write_report=True))

    def stats_only(self) -> None:
        self.run_in_thread("统计", lambda settings: self.write_stats_report(settings))

    def run_all(self) -> None:
        def worker(settings: Settings) -> None:
            if settings.copy_file_0005 or settings.copy_file_0007:
                self.copy_files_to_suffix_folders(settings, write_report=False)
            self.paginate_images(settings, write_report=False)
            self.write_stats_report(settings)
        self.run_in_thread("批量处理", worker)

    def validate_source_output(self, settings: Settings) -> tuple[Path, Path]:
        source = Path(settings.source_dir).expanduser()
        output = Path(settings.output_dir).expanduser()
        if not source.is_dir():
            raise ValueError("请先选择有效的源文件夹。")
        if self.looks_like_output_folder(source):
            raise ValueError("当前源文件夹看起来是已生成的输出目录（名称包含“_输出”）。请改选原始源目录，避免输出目录套输出目录。")
        if not output:
            raise ValueError("请先选择输出文件夹。")
        if output == source or source in output.parents:
            raise ValueError("输出文件夹不能放在源文件夹内部，也不能和源文件夹相同。请选源文件夹外部的位置，避免重复套娃输出。")
        output.mkdir(parents=True, exist_ok=True)
        return source, output

    def looks_like_output_folder(self, path: Path) -> bool:
        return "_输出" in path.name

    def recursive_image_count(self, folder: Path) -> int:
        return sum(1 for path in folder.rglob("*") if is_image(path))

    def target_folders(self, source: Path, recursive: bool, include_root: bool) -> list[Path]:
        folders: list[Path] = []
        if include_root:
            folders.append(source)
        if recursive:
            folders.extend(path for path in source.rglob("*") if path.is_dir())
        else:
            folders.extend(path for path in source.iterdir() if path.is_dir())
        return sorted(dict.fromkeys(folders), key=lambda p: str(p).lower())

    def mirror_folder(self, source: Path, folder: Path, output: Path) -> Path:
        rel = folder.relative_to(source)
        return output / rel

    def copy_files_to_suffix_folders(self, settings: Settings, write_report: bool) -> None:
        source, output = self.validate_source_output(settings)
        output_root = output / source.name
        self.log_step(f"开始 0005 / 0007 投放。源目录：{source}")
        self.log_step(f"成果目录：{output_root}")
        source_image_count = self.recursive_image_count(source)
        self.log_step(f"源目录图片检查：共发现图片 {source_image_count} 张。")
        if source_image_count == 0:
            self.log_step("警告：源目录内未发现图片文件，只能复制已有表格等文件，后续编页不会有图片可处理。")
        self.copy_tree_preserve_existing(source, output_root, self.worker_count_value(settings))

        copy_map = {
            "0005": Path(settings.copy_file_0005).expanduser(),
            "0007": Path(settings.copy_file_0007).expanduser(),
        }
        missing = [suffix for suffix, path in copy_map.items() if not path.is_file()]
        if missing:
            raise ValueError(f"请先选择要复制到 {', '.join(missing)} 文件夹的文件。")

        target_rows: list[dict[str, object]] = []
        targets: list[tuple[str, Path, Path, Path]] = []
        for folder in sorted((p for p in source.rglob("*") if p.is_dir()), key=lambda p: str(p).lower()):
            if self.folder_depth(source, folder) != 3:
                continue
            suffix = self.folder_numeric_suffix(folder)
            if suffix in copy_map:
                output_folder = output_root / folder.relative_to(source)
                targets.append((suffix, copy_map[suffix], folder, output_folder))

        if not targets:
            raise ValueError("没有找到第 3 级且名称以 0005 或 0007 结尾的文件夹。")

        count_0005 = sum(suffix == "0005" for suffix, *_ in targets)
        count_0007 = sum(suffix == "0007" for suffix, *_ in targets)
        self.log_step(f"扫描完成：找到 0005 文件夹 {count_0005} 个，0007 文件夹 {count_0007} 个，共 {len(targets)} 个目标。")

        worker_count = self.worker_count_value(settings)

        def deliver_one(item: tuple[str, Path, Path, Path]) -> dict[str, object]:
            suffix, source_file, original_folder, target_folder = item
            target_folder.mkdir(parents=True, exist_ok=True)
            before_count = self.direct_file_count(target_folder)
            target_name = f"{target_folder.name}{self.copy_number_suffix(settings)}{source_file.suffix.lower()}"
            target_file = target_folder / target_name
            status = "copied"
            if target_file.exists():
                status = "overwritten"
            shutil.copy2(source_file, target_file)
            after_count = self.direct_file_count(target_folder)
            return {
                "type": "suffix_copy",
                "target_suffix": suffix,
                "source_file": str(source_file),
                "original_folder": str(original_folder),
                "target_folder": str(target_folder),
                "target_file": str(target_file),
                "target_file_name": target_name,
                "before_file_count": before_count,
                "after_file_count": after_count,
                "file_count_change": after_count - before_count,
                "status": status,
            }

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(deliver_one, target) for target in targets]
            for index, future in enumerate(as_completed(futures), start=1):
                row = future.result()
                target_rows.append(row)
                self.set_status(f"投放中 {index}/{len(targets)}：{Path(str(row['target_folder'])).name}")
                if index == 1 or index == len(targets) or index % 10 == 0:
                    self.log_step(f"投放进度 {index}/{len(targets)}：{row['target_folder']}")
                self.set_progress(index / len(targets) * 100)

        self.log_rows.extend(target_rows)
        copied = sum(row["status"] == "copied" for row in target_rows)
        overwritten = sum(row["status"] == "overwritten" for row in target_rows)
        self.log_step(f"0005 / 0007 投放完成：新增 {copied} 个，覆盖更新 {overwritten} 个。")
        if write_report:
            self.write_report(output / "0005-0007投放统计.csv", target_rows)
            self.log_step(f"投放统计已生成：{output / '0005-0007投放统计.csv'}")

    def copy_tree_preserve_existing(self, source: Path, output_root: Path, worker_count: int) -> None:
        self.log_step("开始复制源目录到成果目录，原目录不会被修改。")
        output_root.mkdir(parents=True, exist_ok=True)
        files: list[tuple[Path, Path]] = []
        for item in source.rglob("*"):
            target = output_root / item.relative_to(source)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                files.append((item, target))
        image_total = sum(1 for item, _target in files if is_image(item))
        self.log_step(f"源目录扫描完成：待同步文件 {len(files)} 个，其中图片 {image_total} 张，并行线程 {worker_count}。")

        copied = 0
        updated = 0
        unchanged = 0
        completed = 0

        def copy_one(pair: tuple[Path, Path]) -> str:
            item, target = pair
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and self.same_file_snapshot(item, target):
                return "unchanged"
            status = "updated" if target.exists() else "copied"
            shutil.copy2(item, target)
            return status

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(copy_one, pair) for pair in files]
            for future in as_completed(futures):
                result = future.result()
                completed += 1
                if result == "copied":
                    copied += 1
                elif result == "updated":
                    updated += 1
                else:
                    unchanged += 1
                if completed == 1 or completed == len(files) or completed % 100 == 0:
                    self.log_step(f"复制成果目录进度：{completed}/{len(files)}，新复制 {copied} 个，覆盖更新 {updated} 个，无变化 {unchanged} 个。")
        self.log_step(f"成果目录复制完成：新复制 {copied} 个文件，覆盖更新 {updated} 个文件，无变化 {unchanged} 个文件。")

    def same_file_snapshot(self, source: Path, target: Path) -> bool:
        try:
            source_stat = source.stat()
            target_stat = target.stat()
        except OSError:
            return False
        return source_stat.st_size == target_stat.st_size and int(source_stat.st_mtime) <= int(target_stat.st_mtime)

    def copy_number_suffix(self, settings: Settings) -> str:
        return settings.copy_serial.strip() or "-0002"

    def folder_numeric_suffix(self, folder: Path) -> str:
        match = re.search(r"(\d{4})$", folder.name)
        return match.group(1) if match else ""

    def folder_depth(self, source: Path, folder: Path) -> int:
        return len(folder.relative_to(source).parts)

    def direct_file_count(self, folder: Path) -> int:
        return sum(1 for path in folder.iterdir() if path.is_file())

    def worker_count_value(self, settings: Settings) -> int:
        return max(1, min(16, safe_int(settings.worker_count, 4)))

    def paginate_images(self, settings: Settings, write_report: bool) -> None:
        source, output = self.validate_source_output(settings)
        self.log_step(f"开始编页。源目录：{source}")
        output_root = output / source.name
        output_root.mkdir(parents=True, exist_ok=True)
        self.log_step(f"编页输出目录：{output_root}")
        folders = self.target_folders(source, settings.paginate_recursive, settings.paginate_include_root)
        if not folders:
            folders = [source]

        images_by_folder = self.paginate_image_groups(source, folders)
        total = sum(len(items) for _, items in images_by_folder)
        if total == 0 and not settings.paginate_recursive:
            self.log_step("当前未勾选递归处理，直属文件夹未发现图片，自动改为扫描所有子文件夹。")
            folders = self.target_folders(source, True, settings.paginate_include_root)
            images_by_folder = self.paginate_image_groups(source, folders)
            total = sum(len(items) for _, items in images_by_folder)
        if total == 0:
            raise ValueError("没有找到可编页的图片文件。程序已经扫描源文件夹及所有子文件夹，请确认里面有 jpg/png/tif/bmp/webp 图片。")
        folders_with_images = sum(1 for _folder, items in images_by_folder if items)
        self.log_step(f"扫描完成：扫描文件夹 {len(images_by_folder)} 个，有图片文件夹 {folders_with_images} 个，图片 {total} 张。")

        font_size = max(8, safe_int(settings.font_size, 36))
        start_page = max(1, safe_int(settings.start_page, 1))
        color = parse_color(settings.font_color)
        suffix = settings.file_suffix
        tasks: list[tuple[Path, Path, Path, int]] = []
        page = start_page

        for group_folder, images in images_by_folder:
            if images:
                self.log_step(f"开始处理第3级文件夹：{group_folder}，图片 {len(images)} 张。")
            for image_path in images:
                target_folder = self.mirror_folder(source, image_path.parent, output_root)
                target_folder.mkdir(parents=True, exist_ok=True)
                tasks.append((group_folder, target_folder, image_path, page))
                page += 1

        rows = []

        def paginate_task(task: tuple[Path, Path, Path, int]) -> dict[str, object]:
            folder, target_folder, image_path, page = task
            page_text = f"{settings.page_prefix}{page}{settings.page_suffix}"
            try:
                output_file = self.paginate_one_image(
                    image_path,
                    target_folder,
                    page_text,
                    page,
                    font_size,
                    color,
                    suffix,
                    folder,
                    settings.erase_old_page_numbers,
                )
                status = "paged"
            except Exception as exc:
                output_file = target_folder / image_path.name
                status = f"error: {exc}"
            return {
                "type": "paginate",
                "source_folder": str(folder),
                "source_file": str(image_path),
                "output_file": str(output_file),
                "page": page,
                "erase_old_page_numbers": settings.erase_old_page_numbers,
                "status": status,
            }

        worker_count = self.worker_count_value(settings)
        self.log_step(f"开始并行编页：图片 {len(tasks)} 张，并行线程 {worker_count}。")
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(paginate_task, task) for task in tasks]
            for processed, future in enumerate(as_completed(futures), start=1):
                row = future.result()
                rows.append(row)
                self.set_status(f"编页中 {processed}/{total}：{Path(str(row['source_file'])).name}")
                if processed == 1 or processed == total or processed % 20 == 0:
                    self.log_step(f"编页进度 {processed}/{total}：{row['source_file']}")
                self.set_progress(processed / total * 100)

        self.log_rows.extend(rows)
        success = sum(r["status"] == "paged" for r in rows)
        failed = total - success
        self.log_step(f"编页完成：处理图片 {total} 张，成功 {success} 张，失败 {failed} 张。")
        if write_report:
            self.write_report(output / "编页统计.csv", rows)
            self.log_step(f"编页统计已生成：{output / '编页统计.csv'}")

    def paginate_image_groups(self, source: Path, folders: list[Path]) -> list[tuple[Path, list[Path]]]:
        third_level_folders = [folder for folder in folders if self.folder_depth(source, folder) == 3]
        groups: list[tuple[Path, list[Path]]] = []
        for folder in sorted(third_level_folders, key=lambda p: [natural_key(Path(part)) for part in p.relative_to(source).parts]):
            images = sorted(
                [path for path in folder.rglob("*") if is_image(path)],
                key=lambda path: [natural_key(Path(part)) for part in path.relative_to(folder).parts],
            )
            groups.append((folder, images))
        if groups:
            return groups
        return [
            (folder, sorted([p for p in folder.iterdir() if is_image(p)], key=natural_key))
            for folder in folders
        ]

    def paginate_one_image(
        self,
        image_path: Path,
        target_folder: Path,
        page_text: str,
        page_number: int,
        font_size: int,
        color: str,
        suffix: str,
        source_folder: Path,
        erase_old_page_numbers: bool,
    ) -> Path:
        image = ImageOps.exif_transpose(Image.open(image_path))
        mode = image.mode
        if mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        if erase_old_page_numbers:
            self.erase_corner_page_numbers(image, font_size)
        draw = ImageDraw.Draw(image)
        font = self.load_font(font_size)
        orientation = "landscape" if image.width > image.height else "portrait"
        side = self.page_side_for_folder(source_folder, page_number)
        box = self.boxes[f"{orientation}_{side}"].to_pixels(image.width, image.height)
        x0, y0, x1, y1 = box
        bbox = draw.textbbox((0, 0), page_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = x0 + max(0, (x1 - x0 - text_width) // 2)
        y = y0 + max(0, (y1 - y0 - text_height) // 2)
        draw.text((x, y), page_text, fill=color, font=font)

        output_name = f"{image_path.stem}{suffix}{image_path.suffix}" if suffix else image_path.name
        output_file = target_folder / output_name
        save_kwargs: dict[str, object] = {}
        if image_path.suffix.lower() in {".jpg", ".jpeg"}:
            save_kwargs.update({"quality": 95, "subsampling": 0})
            if image.mode == "RGBA":
                image = image.convert("RGB")
        image.save(output_file, **save_kwargs)
        return output_file

    def page_side_for_folder(self, folder: Path, page_number: int) -> str:
        if self.folder_numeric_suffix(folder) in {"0005", "0007"}:
            return "front" if page_number % 2 else "back"
        return "front"

    def erase_corner_page_numbers(self, image: Image.Image, font_size: int) -> int:
        width, height = image.size
        corner_width = max(int(width * 0.22), font_size * 4)
        corner_height = max(int(height * 0.16), font_size * 3)
        corners = [
            (0, 0, min(corner_width, width), min(corner_height, height)),
            (max(width - corner_width, 0), 0, width, min(corner_height, height)),
            (0, max(height - corner_height, 0), min(corner_width, width), height),
            (max(width - corner_width, 0), max(height - corner_height, 0), width, height),
        ]
        erased = 0
        for corner in corners:
            erased += self.erase_dark_components_in_region(image, corner, font_size)
        return erased

    def erase_dark_components_in_region(self, image: Image.Image, region: tuple[int, int, int, int], font_size: int) -> int:
        x0, y0, x1, y1 = region
        crop = image.crop(region).convert("L")
        pixels = crop.load()
        width, height = crop.size
        if width <= 0 or height <= 0:
            return 0

        threshold = self.dark_pixel_threshold(crop)
        visited: set[tuple[int, int]] = set()
        components: list[tuple[int, int, int, int, int]] = []
        for y in range(height):
            for x in range(width):
                if (x, y) in visited or pixels[x, y] > threshold:
                    continue
                stack = [(x, y)]
                visited.add((x, y))
                min_x = max_x = x
                min_y = max_y = y
                count = 0
                while stack:
                    cx, cy = stack.pop()
                    count += 1
                    min_x = min(min_x, cx)
                    max_x = max(max_x, cx)
                    min_y = min(min_y, cy)
                    max_y = max(max_y, cy)
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in visited and pixels[nx, ny] <= threshold:
                            visited.add((nx, ny))
                            stack.append((nx, ny))
                comp_width = max_x - min_x + 1
                comp_height = max_y - min_y + 1
                if self.looks_like_page_number_component(comp_width, comp_height, count, font_size, width, height):
                    components.append((min_x, min_y, max_x, max_y, count))

        if not components:
            return 0

        draw = ImageDraw.Draw(image)
        erased = 0
        for min_x, min_y, max_x, max_y, _count in self.merge_nearby_components(components, font_size):
            pad = max(2, font_size // 6)
            box = (
                max(x0 + min_x - pad, 0),
                max(y0 + min_y - pad, 0),
                min(x0 + max_x + pad, image.width),
                min(y0 + max_y + pad, image.height),
            )
            fill = self.estimate_background_color(image, box)
            draw.rectangle(box, fill=fill)
            erased += 1
        return erased

    def dark_pixel_threshold(self, crop: Image.Image) -> int:
        hist = crop.histogram()
        total = sum(hist)
        cumulative = 0
        for value, count in enumerate(hist):
            cumulative += count
            if cumulative >= total * 0.12:
                return min(max(value + 20, 80), 180)
        return 140

    def looks_like_page_number_component(
        self,
        width: int,
        height: int,
        count: int,
        font_size: int,
        region_width: int,
        region_height: int,
    ) -> bool:
        if count < 3:
            return False
        if height > max(font_size * 2.2, region_height * 0.55):
            return False
        if width > max(font_size * 5, region_width * 0.75):
            return False
        if height < max(3, font_size * 0.18):
            return False
        density = count / max(width * height, 1)
        return 0.04 <= density <= 0.85

    def merge_nearby_components(
        self,
        components: list[tuple[int, int, int, int, int]],
        font_size: int,
    ) -> list[tuple[int, int, int, int, int]]:
        merged: list[tuple[int, int, int, int, int]] = []
        gap = max(3, font_size // 2)
        for comp in sorted(components, key=lambda item: (item[1], item[0])):
            min_x, min_y, max_x, max_y, count = comp
            merged_into_existing = False
            for index, existing in enumerate(merged):
                ex_min_x, ex_min_y, ex_max_x, ex_max_y, ex_count = existing
                horizontally_close = min_x <= ex_max_x + gap and max_x >= ex_min_x - gap
                vertically_close = min_y <= ex_max_y + gap and max_y >= ex_min_y - gap
                if horizontally_close and vertically_close:
                    merged[index] = (
                        min(ex_min_x, min_x),
                        min(ex_min_y, min_y),
                        max(ex_max_x, max_x),
                        max(ex_max_y, max_y),
                        ex_count + count,
                    )
                    merged_into_existing = True
                    break
            if not merged_into_existing:
                merged.append(comp)
        return merged

    def estimate_background_color(self, image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
        x0, y0, x1, y1 = box
        pad = 4
        sample_box = (
            max(x0 - pad, 0),
            max(y0 - pad, 0),
            min(x1 + pad, image.width),
            min(y1 + pad, image.height),
        )
        crop = image.crop(sample_box).convert("RGB")
        pixels = list(crop.getdata())
        if not pixels:
            return (255, 255, 255)
        pixels.sort(key=lambda rgb: rgb[0] + rgb[1] + rgb[2])
        bright = pixels[int(len(pixels) * 0.65) :]
        if not bright:
            bright = pixels
        r = sum(pixel[0] for pixel in bright) // len(bright)
        g = sum(pixel[1] for pixel in bright) // len(bright)
        b = sum(pixel[2] for pixel in bright) // len(bright)
        return (r, g, b)

    def load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/arial.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                try:
                    return ImageFont.truetype(candidate, size=size)
                except Exception:
                    continue
        return ImageFont.load_default()

    def write_stats_report(self, settings: Settings) -> None:
        source, output = self.validate_source_output(settings)
        self.log_step(f"开始统计文件夹数量。源目录：{source}")
        folders = self.target_folders(source, True, True)
        rows = []
        for index, folder in enumerate(folders, start=1):
            if index == 1 or index == len(folders) or index % 50 == 0:
                self.set_status(f"统计中 {index}/{len(folders)}：{folder.name}")
                self.log_step(f"统计进度 {index}/{len(folders)}：{folder}")
            all_files = [p for p in folder.iterdir() if p.is_file()]
            image_files = [p for p in all_files if is_image(p)]
            rel = "." if folder == source else str(folder.relative_to(source))
            rows.append(
                {
                    "folder": rel,
                    "file_count": len(all_files),
                    "image_count": len(image_files),
                    "subfolder_count": len([p for p in folder.iterdir() if p.is_dir()]),
                }
            )
        self.write_report(output / "文件夹数量统计.csv", rows)
        if self.log_rows:
            self.write_report(output / "总处理统计.csv", self.log_rows)
        self.log_step(f"统计完成：文件夹 {len(rows)} 个。统计表已写入输出文件夹。")

    def write_report(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            return
        fieldnames: list[str] = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def append_log(self, text: str) -> None:
        if hasattr(self, "ui_queue"):
            self.ui_queue.put(("log", text))

    def log_step(self, text: str) -> None:
        self.append_log(text)

    def set_status(self, text: str) -> None:
        if hasattr(self, "ui_queue"):
            self.ui_queue.put(("status", text))
        self.append_log(text)

    def set_progress(self, value: float) -> None:
        if hasattr(self, "ui_queue"):
            self.ui_queue.put(("progress", value))

    def process_ui_queue(self) -> None:
        try:
            while True:
                kind, value = self.ui_queue.get_nowait()
                if kind == "log" and hasattr(self, "log"):
                    stamp = time.strftime("%H:%M:%S")
                    self.log.insert("end", f"[{stamp}] {value}\n")
                    self.log.see("end")
                elif kind == "status":
                    self.status_var.set(str(value))
                elif kind == "progress":
                    self.progress.set(float(value))
        except queue.Empty:
            pass
        self.after(100, self.process_ui_queue)

    def open_output_folder(self, path: str) -> None:
        if not path:
            return
        if sys.platform == "darwin":
            os.system(f"open {shlex_quote(path)}")
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            os.system(f"xdg-open {shlex_quote(path)}")


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    app = ArchiveImageTool()
    app.mainloop()
