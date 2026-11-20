"""音视频时间轴的核心逻辑：时间基换算、对齐、交错、空洞、时长、edit list、校验与批量封装。

数据模型
--------
一条轨道（track）是这样一个 dict::

    {
        "track_id": str,          # 轨道标识，同一批次内唯一
        "timescale": int,         # 时间基：每秒多少个 tick（> 0）
        "samples": [              # 按 DTS（解码顺序）递增排列的样本
            {"pts": int, "dur": int, "keyframe": bool},
            ...
        ],
        "duration_ticks": int,    # 可选；轨道声明的总长（tick），用于越界校验
    }

* 时间基（timescale）是整数 tick/秒。``pts`` 是显示时间戳（PTS），
  ``dur`` 是该样本占用的 tick 数，``pts + dur`` 是样本结束点；DTS 只由
  样本在列表中的位置体现（越靠后解码越晚），本模块所有时间计算只看 PTS。
* ``keyframe`` 可选，缺省视为 ``False``，只有视频轨会用到。
* 跨轨比较统一换算到整数毫秒。换算一律用整数运算（``divmod`` / 交叉相乘），
  不得先转 float：29.97 素材（如 30000/1001）用浮点累计会漂移约 1 帧/分钟。
* 判定容差为 1 帧（约 1/30 秒 ≈ 33 ms）：长度、偏移这类整体量允许 1 帧误差，
  单个 tick 换算允许 1 tick 误差；除此之外必须精确。
"""


class MuxError(Exception):
    """封装过程中不可恢复的错误（非法配置、越界偏移、非法时间轴、批量回滚等）。"""


def _round_div(num, den):
    """round(num / den)，四舍五入、半个向上，纯整数运算，支持负数分子。"""
    return (2 * num + den) // (2 * den)


def _ms(ticks, timescale):
    """把 tick 换算成整数毫秒（四舍五入），纯整数运算。"""
    return _round_div(ticks * 1000, timescale)


def _require_timescale(timescale):
    if not isinstance(timescale, int) or timescale <= 0:
        raise MuxError(f"非法时间基 timescale={timescale!r}：必须为正整数")


def _as_track_list(tracks):
    """单条轨道 dict 归一化成列表；列表原样返回。"""
    if isinstance(tracks, dict):
        return [tracks]
    return list(tracks)


def to_common_timescale(track, target):
    """把整条轨道的样本换算到目标时间基。

    参数
    ----
    track : dict
        轨道，见模块文档。
    target : int
        目标时间基（tick/秒），必须为正整数。

    返回
    ----
    list[dict]
        与 ``track["samples"]`` 等长、同序的新列表，每项含
        ``{"pts": int, "dur": int}``（原样本带 ``"keyframe"`` 时原样保留），
        单位是 target 时间基下的 tick。

    契约
    ----
    * 完全用整数运算：``pts`` 换算成 ``round(pt * target / timescale)``（四舍五入，
      半个 tick 向上），``dur`` 不能对每帧独立取整后累加——必须由相邻两个边界
      ``round(pts * target / timescale)`` 与 ``round((pts + dur) * target / timescale)``
      相减得到，保证 1800 帧这类长片的累计误差不超过 1 tick（1 帧量级）。
    * 单个样本误差允许 1 tick，整轨末端误差允许 1 tick；不得出现累计漂移。
    * ``target <= 0`` 或 ``timescale <= 0`` 抛 :class:`MuxError`。
    * ``samples`` 为空返回 ``[]``。
    """
    _require_timescale(target)
    timescale = track["timescale"]
    _require_timescale(timescale)

    out = []
    for sample in track["samples"]:
        pts = sample["pts"]
        start = _round_div(pts * target, timescale)
        end = _round_div((pts + sample["dur"]) * target, timescale)
        item = {"pts": start, "dur": end - start}
        if "keyframe" in sample:
            item["keyframe"] = sample["keyframe"]
        out.append(item)
    return out


def align_offset(video, audio, max_shift_ms):
    """按视频首个关键帧对齐音视频，返回音频相对视频的起始偏移。

    参数
    ----
    video : dict
        视频轨。参考点是**第一个 keyframe=True 的样本**的 pts；整条轨都没有
        关键帧时才退回首个样本。
    audio : dict
        音频轨。参考点是首个样本的 pts。
    max_shift_ms : int
        允许的最大偏移（毫秒，绝对值）。``max_shift_ms == 0`` 时只允许零偏移。

    返回
    ----
    int
        ``round_ms(audio_pts) - round_ms(video_pts)``，单位毫秒；正数表示音频
        比视频晚，负数表示音频更早。

    契约
    ----
    * 参考点必须取关键帧，不能只取首个样本（首个样本常落在关键帧之前）。
    * ``max_shift_ms < 0`` 是非法配置，必须抛 :class:`MuxError`，且错误消息里
      含有 ``"max_shift_ms"``；不得把它当成“无限宽松”或“放大偏移”。
    * ``abs(offset) > max_shift_ms`` 抛 :class:`MuxError`，消息里含有轨道 id。
    * 任一轨为空或没有可用参考点抛 :class:`MuxError`。
    * 结果落在 1 帧（≈33 ms）容差内即视为对齐成功。
    """
    if max_shift_ms < 0:
        raise MuxError(f"max_shift_ms 必须 >= 0，当前为 {max_shift_ms}")

    video_samples = video["samples"]
    audio_samples = audio["samples"]
    if not video_samples:
        raise MuxError(f"视频轨 {video.get('track_id')} 为空，没有可用参考点")
    if not audio_samples:
        raise MuxError(f"音频轨 {audio.get('track_id')} 为空，没有可用参考点")

    _require_timescale(video["timescale"])
    _require_timescale(audio["timescale"])

    ref_video = None
    for sample in video_samples:
        if sample.get("keyframe", False):
            ref_video = sample
            break
    if ref_video is None:
        ref_video = video_samples[0]

    offset = _ms(audio_samples[0]["pts"], audio["timescale"]) - _ms(
        ref_video["pts"], video["timescale"]
    )
    if abs(offset) > max_shift_ms:
        raise MuxError(
            f"音视频偏移 {offset} ms 超出 max_shift_ms={max_shift_ms}："
            f"video={video.get('track_id')} audio={audio.get('track_id')}"
        )
    return offset


def interleave(video, audio, window_ms):
    """把视频轨和音频轨的样本按时间交错成一个播放列表。

    参数
    ----
    video, audio : dict
        两条轨道，可以是空轨（``samples == []``）。
    window_ms : int
        交错窗口（毫秒，> 0）。先按 ``t_ms // window_ms`` 分桶，桶内按
        ``(t_ms, 轨道序号, 样本序号)`` 升序，桶之间按桶号升序；轨道序号
        video=0、audio=1，用于同一时刻的稳定排序。

    返回
    ----
    list[dict]
        每项 ``{"track_id": str, "pts": int, "dur": int, "t_ms": int}``，
        ``pts``/``dur`` 保持原轨时间基，``t_ms`` 是整数毫秒位置。

    契约
    ----
    * 排序依据是时间，不是轨道顺序：``[item["t_ms"] for item in result]``
      必须单调不减，播放器只缓冲一个窗口就能出画。
    * 输出长度等于两轨样本数之和，每个样本恰好出现一次，不丢不重。
    * 同一 ``t_ms`` 的视频样本排在音频样本之前。
    * ``window_ms <= 0`` 抛 :class:`MuxError`；两轨都空返回 ``[]``。
    """
    if window_ms <= 0:
        raise MuxError(f"window_ms 必须 > 0，当前为 {window_ms}")

    keyed = []
    for track_order, track in enumerate((video, audio)):
        timescale = track["timescale"]
        _require_timescale(timescale)
        for index, sample in enumerate(track["samples"]):
            t_ms = _ms(sample["pts"], timescale)
            keyed.append(
                (
                    t_ms // window_ms,
                    t_ms,
                    track_order,
                    index,
                    {
                        "track_id": track["track_id"],
                        "pts": sample["pts"],
                        "dur": sample["dur"],
                        "t_ms": t_ms,
                    },
                )
            )
    keyed.sort(key=lambda entry: entry[:4])
    return [entry[4] for entry in keyed]


def detect_gaps(track, max_gap_ms):
    """检测时间轴上的空洞（无任何样本覆盖的时间段）。

    参数
    ----
    track : dict | list[dict]
        单条轨道，或轨道列表。给列表时先把所有样本的毫秒区间合并（重叠或
        首尾相接视为同一段覆盖），再在合并后的覆盖区间之间找洞——同一时刻
        另一条轨有样本，就不算空洞。
    max_gap_ms : int
        阈值（毫秒，>= 0）；严格大于阈值才算空洞。

    返回
    ----
    list[tuple]
        ``(gap_start_ms, gap_end_ms, gap_ms)``，按时间升序；三者都是整数，
        ``gap_ms == gap_end_ms - gap_start_ms``。

    契约
    ----
    * 判断依据是“上一个样本的结束点”与“下一个样本的开始点”，不是简单的
      相邻 pts 差（那样会把样本自身的 dur 算成洞）。
    * 传轨道列表时必须跨轨合并，不能逐轨独立检测（否则两条轨各自的空白
      互相填不上时被漏检）。
    * 样本按 pts 排序后再检测，输入顺序不影响结果。
    * ``max_gap_ms < 0`` 抛 :class:`MuxError`；空轨道返回 ``[]``。
    """
    if max_gap_ms < 0:
        raise MuxError(f"max_gap_ms 必须 >= 0，当前为 {max_gap_ms}")

    intervals = []
    for one in _as_track_list(track):
        timescale = one["timescale"]
        _require_timescale(timescale)
        for sample in one["samples"]:
            start = _ms(sample["pts"], timescale)
            end = _ms(sample["pts"] + sample["dur"], timescale)
            intervals.append((start, end))
    if not intervals:
        return []

    intervals.sort()
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    gaps = []
    for prev, nxt in zip(merged, merged[1:]):
        gap_ms = nxt[0] - prev[1]
        if gap_ms > max_gap_ms:
            gaps.append((prev[1], nxt[0], gap_ms))
    return gaps


def compute_duration(tracks):
    """计算一批轨道的整体时长：取最长的那条轨。

    参数
    ----
    tracks : list[dict] | dict
        轨道列表（也接受单条轨道 dict）。

    返回
    ----
    int
        毫秒整数：所有轨道中最大的 ``round((pts + dur) * 1000 / timescale)``，
        即封装的最终时长；空输入返回 0。

    契约
    ----
    * 必须遍历所有轨道取最大值，不能只取视频轨（音频更长时会被截断）。
    * 误差允许 1 tick（≈1 ms 量级），换算用整数运算。
    * ``timescale <= 0`` 抛 :class:`MuxError`。
    """
    longest = 0
    for track in _as_track_list(tracks):
        timescale = track["timescale"]
        _require_timescale(timescale)
        for sample in track["samples"]:
            end_ms = _ms(sample["pts"] + sample["dur"], timescale)
            if end_ms > longest:
                longest = end_ms
    return longest


def edit_list(track, cuts):
    """按切点把轨道切成片段，生成累计偏移不漂移的 edit list。

    参数
    ----
    track : dict
        轨道，见模块文档。
    cuts : list[int]
        切点，元素是**样本索引**：升序、非负、首元素为 0、末元素小于
        ``len(samples)``。例如 ``[0, 250, 500, 750]`` 表示 4 个片段。

    返回
    ----
    list[dict]
        与 ``cuts`` 等长，每项
        ``{"start_index": int, "duration_ms": int, "media_offset_ms": int}``。

    契约
    ----
    * ``duration_ms`` 由片段两端在原始时间轴上的累计边界换算后相减：
      ``ms(cum_end) - ms(cum_start)``，其中 ``ms(t) = round(t * 1000 / timescale)``、
      ``cum`` 是样本 dur 的前缀和。**不能**每段独立换算再累加（每段独立取整
      会让长片累计漂移上百毫秒）。
    * ``media_offset_ms`` 是前序片段 ``duration_ms`` 的前缀和，第一段为 0；
      末段满足 ``media_offset_ms + duration_ms == ms(总 tick 数)``，全程无漂移。
    * ``cuts`` 非升序、含负数、越界或首元素不是 0 抛 :class:`MuxError`。
    * ``samples`` 为空或 ``cuts`` 为空返回 ``[]``。
    """
    samples = track["samples"]
    if not samples or not cuts:
        return []

    timescale = track["timescale"]
    _require_timescale(timescale)

    if cuts[0] != 0:
        raise MuxError(f"cuts 首元素必须是 0，当前为 {cuts[0]}")
    prev = -1
    for cut in cuts:
        if cut < 0:
            raise MuxError(f"cuts 含负数切点：{cut}")
        if cut <= prev:
            raise MuxError(f"cuts 必须严格升序：{cuts}")
        if cut >= len(samples):
            raise MuxError(f"cuts 越界：{cut} >= 样本数 {len(samples)}")
        prev = cut

    # 样本 dur 的前缀和：cum[i] 是第 i 个样本的起点（tick）。
    cum = [0] * (len(samples) + 1)
    for i, sample in enumerate(samples):
        cum[i + 1] = cum[i] + sample["dur"]

    boundaries = list(cuts) + [len(samples)]
    entries = []
    media_offset = 0
    for start, end in zip(boundaries, boundaries[1:]):
        duration_ms = _ms(cum[end], timescale) - _ms(cum[start], timescale)
        entries.append(
            {
                "start_index": start,
                "duration_ms": duration_ms,
                "media_offset_ms": media_offset,
            }
        )
        media_offset += duration_ms
    return entries


def validate_timeline(tracks):
    """检查时间轴是否自洽，返回问题代码列表（空列表表示健康）。

    参数
    ----
    tracks : list[dict]
        轨道列表；同一批次内时间基必须一致。

    返回
    ----
    list[str]
        问题描述，按“输入轨道顺序 → 轨道内样本顺序”排列，格式固定：

        * ``"negative_pts:{track_id}:{index}"`` —— 样本 pts < 0（封装器会直接拒绝）
        * ``"invalid_duration:{track_id}:{index}"`` —— 样本 dur <= 0
        * ``"timescale_mismatch:{track_id}"`` —— 该轨时间基与第一条轨不同，
          一条轨只报一次，且排在该轨的样本问题之前
        * ``"out_of_range:{track_id}:{index}"`` —— 轨道带 ``duration_ticks`` 时，
          ``pts >= duration_ticks`` 或 ``pts + dur > duration_ticks``

    契约
    ----
    * 不做任何修复、不抛异常（空时间基除外），只报告。
    * 一条轨可以同时报多条问题；同一问题不重复报。
    * 空列表返回 ``[]``；``timescale <= 0`` 抛 :class:`MuxError`。
    """
    problems = []
    tracks = list(tracks)
    if not tracks:
        return problems

    reference = None
    for track in tracks:
        timescale = track["timescale"]
        _require_timescale(timescale)
        if reference is None:
            reference = timescale
        track_id = track["track_id"]
        if timescale != reference:
            problems.append(f"timescale_mismatch:{track_id}")
        duration_ticks = track.get("duration_ticks")
        for index, sample in enumerate(track["samples"]):
            pts = sample["pts"]
            dur = sample["dur"]
            if pts < 0:
                problems.append(f"negative_pts:{track_id}:{index}")
            if dur <= 0:
                problems.append(f"invalid_duration:{track_id}:{index}")
            if duration_ticks is not None and (
                pts >= duration_ticks or pts + dur > duration_ticks
            ):
                problems.append(f"out_of_range:{track_id}:{index}")
    return problems


def mux_batch(jobs, config):
    """批量封装：逐个任务计算，任一任务非法则整体回滚。

    参数
    ----
    jobs : list[dict]
        ``[{"job_id": str, "tracks": [track, ...]}, ...]``，顺序即输出顺序。
    config : dict
        ``{"window_ms": int, "max_gap_ms": int, "max_shift_ms": int}``，
        缺省值 ``window_ms=500``、``max_gap_ms=1000``、``max_shift_ms=1000``。

    返回
    ----
    dict
        ::

            {
                "jobs": [
                    {
                        "job_id": str,
                        "duration_ms": int,      # 该任务的整体时长
                        "item_count": int,       # 该任务样本总数
                        "gap_count": int,        # 该任务跨轨空洞数
                        "tracks": [track_id, ...] # 按输入顺序
                    },
                    ...
                ],
                "total_duration_ms": int,        # 各任务时长之和
            }

        任务顺序与 ``jobs`` 顺序一致，空 ``jobs`` 返回
        ``{"jobs": [], "total_duration_ms": 0}``。

    契约
    ----
    * 每个任务用独立的时间轴缓冲：任务之间不得共享/残留状态，逐个单独跑与
      一起跑的结果必须一致。
    * 先整体校验：任一任务 ``validate_timeline`` 非空即抛 :class:`MuxError`，
      消息里含有出错的 ``job_id`` 和第一个问题代码；此时不返回部分结果、
      不修改传入的 ``jobs``（幂等回滚），后续调用不受影响。
    * 时长取该任务最长轨，空洞按该任务全部轨道跨轨检测，样本数按全部轨道
      求和；同一 ``job_id`` 重复或 ``tracks`` 为空抛 :class:`MuxError`。
    * ``config`` 缺键用默认值，多余键忽略。
    """
    config = config or {}
    max_gap_ms = config.get("max_gap_ms", 1000)
    if max_gap_ms < 0:
        raise MuxError(f"max_gap_ms 必须 >= 0，当前为 {max_gap_ms}")

    jobs = list(jobs)
    seen_ids = set()
    for job in jobs:
        job_id = job["job_id"]
        if job_id in seen_ids:
            raise MuxError(f"重复的 job_id：{job_id}")
        seen_ids.add(job_id)
        if not job["tracks"]:
            raise MuxError(f"任务 {job_id} 没有任何轨道")

    # 先整体校验：任一任务有问题即整体回滚，不产生部分结果。
    for job in jobs:
        problems = validate_timeline(job["tracks"])
        if problems:
            raise MuxError(f"任务 {job['job_id']} 时间轴非法：{problems[0]}")

    results = []
    total = 0
    for job in jobs:
        tracks = job["tracks"]
        duration_ms = compute_duration(tracks)
        results.append(
            {
                "job_id": job["job_id"],
                "duration_ms": duration_ms,
                "item_count": sum(len(t["samples"]) for t in tracks),
                "gap_count": len(detect_gaps(tracks, max_gap_ms)),
                "tracks": [t["track_id"] for t in tracks],
            }
        )
        total += duration_ms
    return {"jobs": results, "total_duration_ms": total}
