"""video-mux-sync：音视频时间轴对齐、交错与批量封装。"""

from .timeline import (
    MuxError,
    align_offset,
    compute_duration,
    detect_gaps,
    edit_list,
    interleave,
    mux_batch,
    to_common_timescale,
    validate_timeline,
)

__all__ = [
    "MuxError",
    "to_common_timescale",
    "align_offset",
    "interleave",
    "detect_gaps",
    "compute_duration",
    "edit_list",
    "validate_timeline",
    "mux_batch",
]
