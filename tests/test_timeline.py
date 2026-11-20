"""mux.timeline 的验收测试：15 个用例，全部调用 mux/timeline.py 的 8 个函数。

约定（与 mux/timeline.py 的 docstring 一致）：
* 轨道 = {"track_id", "timescale", "samples": [{"pts", "dur"}, ...]}；
* pts/dur 是该轨时间基下的整数 tick，跨轨换算到整数毫秒；
* 整体量允许 1 帧（≈33 ms）/ 1 tick 的容差，其余必须精确。
"""

import pytest

from mux.timeline import (
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


def track(track_id, timescale, samples, **extra):
    data = {"track_id": track_id, "timescale": timescale, "samples": samples}
    data.update(extra)
    return data


def s(pairs):
    """[(pts, dur), ...] -> [{"pts": ..., "dur": ...}, ...]"""
    return [{"pts": pts, "dur": dur} for pts, dur in pairs]


# ---------------------------------------------------------------- 缺陷 1：浮点换算漂移
def test_to_common_timescale_no_drift_on_2997():
    # 29.97 fps：30000 时间基、每帧 1001 tick，1800 帧 = 60.06 秒。
    # 换算到毫秒后，每帧 dur 只应是 33 或 34，且末端必须落在 60060 ms。
    video = track("v", 30000, s([(i * 1001, 1001) for i in range(1800)]))
    out = to_common_timescale(video, 1000)

    assert len(out) == 1800
    assert out[0]["pts"] == 0
    assert all(item["dur"] in (33, 34) for item in out)
    assert out[-1]["pts"] + out[-1]["dur"] == 60060  # 误差 <= 1 tick
    pts = [item["pts"] for item in out]
    assert pts == sorted(pts)
    assert len(set(pts)) == 1800

    # 再换到 90000 时间基应完全精确（30000 -> 90000 是 3 倍）
    out90 = to_common_timescale(video, 90000)
    assert out90[0]["pts"] == 0 and out90[0]["dur"] == 3003
    assert out90[777]["pts"] == 777 * 3003


# ---------------------------------------------------------------- 缺陷 2：对齐不看关键帧
def test_align_offset_uses_first_keyframe():
    # 首个样本 pts=0 不是关键帧，真正关键帧在 120 ms；音频从 0 开始。
    video = track(
        "v",
        1000,
        [
            {"pts": 0, "dur": 40, "keyframe": False},
            {"pts": 40, "dur": 40, "keyframe": False},
            {"pts": 80, "dur": 40, "keyframe": False},
            {"pts": 120, "dur": 40, "keyframe": True},
        ],
    )
    audio = track("a", 1000, s([(0, 40)]))

    # 音频相对视频关键帧早 120 ms；按首个样本算会得到 0。
    assert align_offset(video, audio, max_shift_ms=500) == -120


# ---------------------------------------------------------------- 缺陷 3：交错按轨道顺序
def test_interleave_orders_by_time_not_track_order():
    video = track("v", 1000, s([(0, 10), (100, 10), (200, 10)]))
    audio = track("a", 1000, s([(0, 10), (50, 10), (150, 10)]))

    out = interleave(video, audio, window_ms=1000)

    # 0(v) 0(a) 50(a) 100(v) 150(a) 200(v)：同一时刻视频在前。
    assert [item["track_id"] for item in out] == ["v", "a", "a", "v", "a", "v"]
    assert [item["t_ms"] for item in out] == [0, 0, 50, 100, 150, 200]
    assert len(out) == 6


# ---------------------------------------------------------------- 缺陷 4：空洞跨轨漏检
def test_detect_gaps_across_tracks():
    # 视频覆盖 0-1000 ms，音频覆盖 2000-3000 ms，中间 1000 ms 无人覆盖。
    video = track("v", 1000, s([(0, 1000)]))
    audio = track("a", 1000, s([(2000, 1000)]))

    assert detect_gaps([video, audio], max_gap_ms=500) == [(1000, 2000, 1000)]
    # 单轨检测同一批数据返回空：洞只在跨轨合并后才暴露。
    assert detect_gaps(video, max_gap_ms=500) == []


# ---------------------------------------------------------------- 缺陷 5：时长只看视频轨
def test_compute_duration_takes_longest_track():
    video = track("v", 1000, s([(0, 1000)]))
    audio = track(
        "a", 48000, [{"pts": 0, "dur": 72000}]
    )  # 72000 / 48000 s = 1500 ms

    assert compute_duration([video, audio]) == 1500
    # 反过来放也一样：取最长轨，与顺序无关。
    assert compute_duration([audio, video]) == 1500


# ---------------------------------------------------------------- 缺陷 6：edit list 每段独立累加
def test_edit_list_offsets_do_not_drift():
    # 1000 帧、每帧 3003 tick @90000，总长 33366.67 ms；按 250 帧一段切 4 段。
    video = track("v", 90000, s([(i * 3003, 3003) for i in range(1000)]))

    entries = edit_list(video, [0, 250, 500, 750])

    assert [e["start_index"] for e in entries] == [0, 250, 500, 750]
    assert [e["duration_ms"] for e in entries] == [8342, 8341, 8342, 8342]
    assert [e["media_offset_ms"] for e in entries] == [0, 8342, 16683, 25025]
    # 末段收尾必须精确等于整轨时长 33367 ms，不许多出 1 ms。
    assert entries[-1]["media_offset_ms"] + entries[-1]["duration_ms"] == 33367
    assert sum(e["duration_ms"] for e in entries) == 33367


# ---------------------------------------------------------------- 缺陷 7：负 PTS / 越界不校验
def test_validate_timeline_reports_negative_pts_and_out_of_range():
    bad = track(
        "v",
        1000,
        [{"pts": -40, "dur": 40}, {"pts": 980, "dur": 40}],
        duration_ticks=1000,
    )

    assert validate_timeline([bad]) == ["negative_pts:v:0", "out_of_range:v:1"]


# ---------------------------------------------------------------- 缺陷 8：时间基不一致不报错
def test_validate_timeline_reports_timescale_mismatch():
    video = track("v", 90000, s([(0, 3000)]))
    audio = track("a", 48000, s([(0, 1600)]))

    assert validate_timeline([video, audio]) == ["timescale_mismatch:a"]
    # 时间基一致、样本合法时没有任何问题。
    assert validate_timeline([video, track("a2", 90000, s([(0, 3000)]))]) == []


# ---------------------------------------------------------------- 缺陷 9：批量共用时间轴缓冲
def test_mux_batch_isolates_jobs_and_rolls_back():
    job1 = {
        "job_id": "j1",
        "tracks": [
            track("v1", 1000, s([(0, 1000)])),
            track("a1", 1000, s([(0, 1000)])),
        ],
    }
    job2 = {
        "job_id": "j2",
        "tracks": [
            track("v2", 1000, s([(0, 500), (500, 500)])),
            track("a2", 1000, s([(0, 2000)])),
        ],
    }
    config = {"window_ms": 1000, "max_gap_ms": 500, "max_shift_ms": 1000}

    result = mux_batch([job1, job2], config)
    jobs = result["jobs"]

    assert [j["job_id"] for j in jobs] == ["j1", "j2"]
    assert [j["duration_ms"] for j in jobs] == [1000, 2000]
    assert [j["item_count"] for j in jobs] == [2, 3]
    assert [j["gap_count"] for j in jobs] == [0, 0]
    assert [j["tracks"] for j in jobs] == [["v1", "a1"], ["v2", "a2"]]
    assert result["total_duration_ms"] == 3000

    # 单独跑 j2 必须与批量里的 j2 完全一致：没有上一个任务残留的样本。
    solo = mux_batch([job2], config)["jobs"][0]
    assert solo == jobs[1]

    # 任一任务非法 -> 整体回滚：抛错、不返回部分结果，后续调用仍然干净。
    bad = {"job_id": "j3", "tracks": [track("v3", 1000, [{"pts": -1, "dur": 40}])]}
    snapshot = [dict(job) for job in (job1, job2, bad)]
    with pytest.raises(MuxError):
        mux_batch([job1, bad], config)
    assert [dict(job) for job in (job1, job2, bad)] == snapshot
    assert mux_batch([job2], config)["jobs"][0] == jobs[1]


# ---------------------------------------------------------------- 缺陷 10：max_shift_ms 为负
def test_align_offset_rejects_negative_limit_even_at_zero_shift():
    # 即使当前偏移正好是 0，负上限也是非法配置，必须抛 MuxError。
    video = track("v", 1000, [{"pts": 0, "dur": 40, "keyframe": True}])
    audio = track("a", 1000, s([(0, 40)]))

    for limit in (-1, -50, -480, -1000):
        with pytest.raises(MuxError) as excinfo:
            align_offset(video, audio, max_shift_ms=limit)
        assert "max_shift_ms" in str(excinfo.value)


# ---------------------------------------------------------------- 单轨时长不变
def test_compute_duration_single_track_unchanged():
    video = track("v", 1000, s([(0, 40), (40, 40), (80, 40)]))
    assert compute_duration([video]) == 120
    assert compute_duration(video) == 120  # 也接受单条轨道 dict

    # 非 1000 时间基：3003 tick @90000 ≈ 33.37 ms，容差 1 tick（≈1 ms）。
    odd = track("x", 90000, s([(0, 3003)]))
    assert compute_duration([odd]) in (33, 34)


# ---------------------------------------------------------------- max_shift_ms=0 只允许零偏移
def test_align_offset_zero_limit_allows_only_zero_shift():
    video = track("v", 1000, [{"pts": 0, "dur": 40, "keyframe": True}])
    same = track("a", 1000, s([(0, 40)]))
    late = track("a", 1000, s([(1, 40)]))

    assert align_offset(video, same, max_shift_ms=0) == 0
    with pytest.raises(MuxError):
        align_offset(video, late, max_shift_ms=0)


# ---------------------------------------------------------------- 空输入
def test_empty_tracks_return_empty_results():
    empty_v = track("v", 1000, [])
    empty_a = track("a", 1000, [])

    assert to_common_timescale(empty_v, 90000) == []
    assert interleave(empty_v, empty_a, window_ms=500) == []
    assert detect_gaps(empty_v, max_gap_ms=100) == []
    assert detect_gaps([empty_v, empty_a], max_gap_ms=100) == []
    assert compute_duration([]) == 0
    assert compute_duration([empty_v]) == 0
    assert edit_list(empty_v, []) == []
    assert validate_timeline([]) == []
    assert mux_batch([], {}) == {"jobs": [], "total_duration_ms": 0}


# ---------------------------------------------------------------- max_shift_ms<0 放大了偏移
def test_align_offset_negative_limit_raises():
    # 音频比视频关键帧晚 300 ms，上限为负：不能返回、也不能夹到别的值。
    video = track("v", 1000, [{"pts": 0, "dur": 40, "keyframe": True}])
    audio = track("a", 1000, s([(300, 40)]))

    with pytest.raises(MuxError):
        align_offset(video, audio, max_shift_ms=-5)
    # 正上限下同一对轨道是可对齐的，说明失败来自配置而不是数据。
    assert align_offset(video, audio, max_shift_ms=1000) == 300


# ---------------------------------------------------------------- 批量全成功：顺序不变
def test_mux_batch_preserves_job_order():
    jobs = [
        {"job_id": "first", "tracks": [track("v1", 1000, s([(0, 1000)]))]},
        {"job_id": "second", "tracks": [track("v2", 1000, s([(0, 2500)]))]},
        {"job_id": "third", "tracks": [track("v3", 1000, s([(0, 500)]))]},
    ]

    result = mux_batch(jobs, config={})

    assert result["jobs"][0]["job_id"] == "first"
    assert result["jobs"][1]["job_id"] == "second"
    assert result["jobs"][2]["job_id"] == "third"
    assert [j["job_id"] for j in result["jobs"]] == ["first", "second", "third"]
    assert [j["duration_ms"] for j in result["jobs"]] == [1000, 2500, 500]
    assert result["total_duration_ms"] == 4000
