# video-mux-sync

把分离的音视频重新封装后，长片播到后面口型就对不上：时间基换算用浮点，29.97 素材累计漂移 1 帧/分钟；对齐只按首个样本，不看关键帧，偏移 3 帧；交错按轨道顺序而不是时间，播放器要缓冲全部；空洞检测用相邻差，跨轨漏检；时长取视频轨，音频更长时被截断；edit list 每段独立累加，长片漂移 200 ms；负 PTS 不校验，封装器拒绝；时间基不一致不报错，输出时间错乱；批量共用时间轴缓冲，任务之间串；`max_shift_ms` 为负不校验，对齐反而放大偏移。mux/timeline.py 是核心，tests/test_timeline.py 15个红。要修的是：实现 mux/timeline.py 里的 8 个函数（时间基换算、关键帧对齐、按时间交错、跨轨空洞、最长轨时长、无漂移 edit list、时间轴校验、批量封装与回滚），把 10 个缺陷逐一修掉，让 15 个测试全绿。

## 运行方式

```bash
python -m pytest tests -q
```

初始状态（未实现任何函数）必须恰好是 `15 failed`，并且没有任何 collection error / import error / syntax error：

```text
$ python -m pytest tests -q
15 failed in 0.0Xs
```

修复完成的判据是同一条命令输出 `15 passed`。只允许使用标准库（`bisect`、`math`）与 pytest，禁止联网，禁止 `sleep`，禁止随机数。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 注意

`tests/test_timeline.py` 不可修改：它就是验收标准。只能改 `mux/timeline.py`（以及必要时 `mux/__init__.py` 的导出），不要为了让测试通过而改动测试文件或删改断言。
