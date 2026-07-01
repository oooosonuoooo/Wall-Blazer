import math
from collections import namedtuple


VALID_FIT_MODES = {"cover", "contain", "stretch"}

FitGeometry = namedtuple("FitGeometry", "width height x y")
CropGeometry = namedtuple("CropGeometry", "width height x y")


def normalize_fit_mode(fit_mode):
    return fit_mode if fit_mode in VALID_FIT_MODES else "cover"


def _positive_int(value, default=1):
    try:
        return max(1, int(round(float(value))))
    except (TypeError, ValueError):
        return default


def calculate_fit_geometry(container_width, container_height, video_width, video_height, fit_mode):
    container_width = _positive_int(container_width)
    container_height = _positive_int(container_height)
    fit_mode = normalize_fit_mode(fit_mode)

    if not video_width or not video_height:
        return FitGeometry(container_width, container_height, 0, 0)

    video_width = _positive_int(video_width)
    video_height = _positive_int(video_height)
    if fit_mode == "stretch":
        return FitGeometry(container_width, container_height, 0, 0)

    scale_x = container_width / float(video_width)
    scale_y = container_height / float(video_height)
    scale = min(scale_x, scale_y) if fit_mode == "contain" else max(scale_x, scale_y)

    if fit_mode == "contain":
        render_width = min(container_width, max(1, int(math.floor(video_width * scale))))
        render_height = min(container_height, max(1, int(math.floor(video_height * scale))))
    else:
        render_width = max(container_width, int(math.ceil(video_width * scale)))
        render_height = max(container_height, int(math.ceil(video_height * scale)))

    offset_x = int(math.floor((container_width - render_width) / 2.0))
    offset_y = int(math.floor((container_height - render_height) / 2.0))
    return FitGeometry(render_width, render_height, offset_x, offset_y)


def calculate_center_crop_geometry(container_width, container_height, video_width, video_height):
    container_width = _positive_int(container_width)
    container_height = _positive_int(container_height)
    video_width = _positive_int(video_width)
    video_height = _positive_int(video_height)

    container_ratio = container_width / float(container_height)
    video_ratio = video_width / float(video_height)
    if abs(container_ratio - video_ratio) <= 1e-3:
        return None

    if video_ratio > container_ratio:
        crop_width = max(1, min(video_width, int(math.floor(video_height * container_ratio))))
        crop_height = video_height
    else:
        crop_width = video_width
        crop_height = max(1, min(video_height, int(math.floor(video_width / container_ratio))))

    offset_x = int(math.floor((video_width - crop_width) / 2.0))
    offset_y = int(math.floor((video_height - crop_height) / 2.0))
    return CropGeometry(crop_width, crop_height, offset_x, offset_y)


def format_vlc_crop_geometry(crop):
    if crop is None:
        return None
    return f"{crop.width}x{crop.height}+{crop.x}+{crop.y}"
