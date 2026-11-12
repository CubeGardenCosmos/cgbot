"""时间预算:软目标、临近窗口、硬取消与宽限、重要任务、重启偏移(全部确定性,无真实 sleep)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from manager_fakes import FakeCodexClient, Rig, build, settle

from cgbot.coding.manager import TIME_BUDGET_CANCELLED_TEXT, TIME_BUDGET_PREFIX, CodingError

# 所有提醒点都是 30 秒的整数倍;逐步推进才能让每个到期点被定时器独立唤醒。
STEP = 30.0


async def _run(rig: Rig, seconds: float) -> None:
    for _ in range(round(seconds / STEP)):
        rig.clock.advance(STEP)
        await settle()


def _steers(rig: Rig, user: str = "alice") -> list[str]:
    return [text for _, _, text in rig.server.user(user).steers]


async def test_ordinary_task_gets_code_generated_steer_notices(tmp_path: Path) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    await rig.manager.submit("alice", "a1")

    await _run(rig, 60)
    assert len(_steers(rig)) == 1
    assert "还剩 540 秒" in _steers(rig)[0]

    await _run(rig, 60)
    assert len(_steers(rig)) == 2
    assert "软目标" in _steers(rig)[1]
    assert rig.server.user("alice").interrupts == []
    assert rig.notifier.count("time_budget") == 2
    assert all(n.text.startswith(TIME_BUDGET_PREFIX) for n in rig.notifier.of("time_budget"))


async def test_approach_window_uses_shorter_interval_then_cap_then_hard_interrupt(
    tmp_path: Path,
) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    await rig.manager.submit("alice", "long")

    await _run(rig, 420)
    assert len(_steers(rig)) == 7  # 60/120(软目标)/180/240/300/360/420

    await _run(rig, 60)  # 480 秒:进入临近窗口
    assert len(_steers(rig)) == 8
    assert "还剩 120 秒" in _steers(rig)[-1]

    await _run(rig, 30)  # 510 秒:窗口内 30 秒间隔
    assert len(_steers(rig)) == 9
    assert "还剩 90 秒" in _steers(rig)[-1]

    await _run(rig, 90)  # 600 秒:cap 进入宽限
    assert len(_steers(rig)) == 12
    assert "硬上限" in _steers(rig)[-1]
    assert rig.server.user("alice").interrupts == []

    await _run(rig, 120)  # 720 秒 = cap + grace:硬取消
    assert len(rig.server.user("alice").interrupts) == 1
    assert rig.store.running_turns() == []
    assert rig.notifier.count("turn_interrupted") == 1
    [notice] = rig.notifier.of("turn_interrupted")
    assert notice.text.startswith(TIME_BUDGET_CANCELLED_TEXT)


async def test_hard_cancel_preserves_partial_text_from_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupt_with_partial(self: FakeCodexClient, thread_id: str, turn_id: str) -> None:
        self._sb.interrupts.append((thread_id, turn_id))
        self._server.complete(self._user_id, thread_id, status="interrupted", text="半成品结论")

    monkeypatch.setattr(FakeCodexClient, "turn_interrupt", interrupt_with_partial)
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    await rig.manager.submit("alice", "long")

    await _run(rig, 720)

    [notice] = rig.notifier.of("turn_interrupted")
    assert notice.text == f"{TIME_BUDGET_CANCELLED_TEXT}。\n已保留部分结果：半成品结论"


async def test_empty_interrupt_text_falls_back_to_previous_result_marked_as_such(
    tmp_path: Path,
) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    first = await rig.manager.submit("alice", "a1")
    rig.server.complete("alice", first.thread_id, text="上一轮结果")
    await settle()
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)

    await _run(rig, 720)

    [notice] = rig.notifier.of("turn_interrupted")
    assert "上一次记录的结果（非本次）：上一轮结果" in notice.text


async def test_important_task_gets_overrun_notices_but_is_never_interrupted(
    tmp_path: Path,
) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    await rig.manager.submit("alice", "serious", important=True, time_limit_minutes=2)

    await _run(rig, 720)

    assert rig.server.user("alice").interrupts == []
    assert rig.notifier.count("turn_interrupted") == 0
    assert any("超过 120 秒上限" in text for text in _steers(rig))
    assert len(rig.store.running_turns()) == 1


async def test_important_time_limit_is_bounded_by_config_maximum(tmp_path: Path) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    for bad in (0.0, -1.0, 61.0):
        with pytest.raises(CodingError):
            await rig.manager.submit("alice", "x", important=True, time_limit_minutes=bad)
    assert rig.server.user("alice").started == []


async def test_time_limit_is_ignored_for_ordinary_tasks(tmp_path: Path) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    await rig.manager.submit("alice", "x", important=False, time_limit_minutes=1)

    await _run(rig, 60)
    assert "600 秒上限" in _steers(rig)[0]

    await _run(rig, 300)  # 若误用 1 分钟上限,此处已经硬取消
    assert rig.server.user("alice").interrupts == []


async def test_each_executing_turn_is_budgeted_from_its_own_start(tmp_path: Path) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    first = await rig.manager.submit("alice", "a1")
    await _run(rig, 60)
    rig.server.complete("alice", first.thread_id, text="一")
    await settle()
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)  # 于 t=60 开始

    await _run(rig, 660)  # t=720:a2 仅运行 660 秒;旧计时器若未停止会在此误取消
    assert rig.server.user("alice").interrupts == []

    await _run(rig, 60)  # t=780:a2 达到 720 秒
    turn_two = rig.server.user("alice").started[-1][2]
    assert rig.server.user("alice").interrupts == [(first.thread_id, turn_two)]


async def test_queued_instruction_is_budgeted_from_dispatch(tmp_path: Path) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    first = await rig.manager.submit("alice", "a1")
    queued = await rig.manager.submit("alice", "a2", thread_id=first.thread_id)
    assert queued.state == "queued"

    await _run(rig, 720)  # a1 被硬取消,a2 在此刻才开始
    assert len(rig.server.user("alice").interrupts) == 1
    assert len(rig.server.user("alice").started) == 2

    await _run(rig, 300)  # a2 仅运行 300 秒,不得被取消
    assert len(rig.server.user("alice").interrupts) == 1


async def test_restart_resumes_budget_with_elapsed_time(tmp_path: Path) -> None:
    first = build(tmp_path / "coding.sqlite")
    await first.manager.start()
    await first.manager.submit("alice", "long")
    await _run(first, 300)
    await first.manager.close()

    first.server.user("alice").resume_status = "active"
    second = build(tmp_path / "coding.sqlite", clock=first.clock, server=first.server)
    await second.manager.start()
    await settle()

    await _run(second, 390)  # 累计 690 秒:尚未到硬取消点
    assert second.server.user("alice").interrupts == []

    await _run(second, 30)  # 累计 720 秒:恢复后仍按原始开始时间计时
    assert len(second.server.user("alice").interrupts) == 1


async def test_close_cancels_budget_timers(tmp_path: Path) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    await rig.manager.submit("alice", "long")
    await rig.manager.close()

    await _run(rig, 800)

    assert rig.server.user("alice").interrupts == []
    assert rig.notifier.count("time_budget") == 0
