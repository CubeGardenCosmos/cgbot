"""OmpService: machines, tasks, prompts, queue control, reads, answers, reports, redaction.

Background watchers are disabled (``quiet_watchers``) so every assertion is a direct,
deterministic service call against ``FakeOmpweb``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from omp_fakes import FakeOmpweb, quiet_watchers

from cgbot.omp.client import MSG_LOGIN_REJECTED, OmpError
from cgbot.omp.service import UNKNOWN_TASK_MSG, OmpService
from cgbot.omp.store import OmpStore, TrackedTask

OWNER = "u1"
PW = "pw-secret-123"


@dataclass
class Env:
    service: OmpService
    store: OmpStore
    fake: FakeOmpweb


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    quiet_watchers(monkeypatch)
    store = OmpStore(tmp_path / "omp.sqlite")
    fake = FakeOmpweb()
    service = OmpService(store, http_client_factory=fake.factory())
    yield Env(service=service, store=store, fake=fake)
    await service.close()
    store.close()


async def add_box(e: Env, owner: str = OWNER) -> dict:
    return await e.service.add_machine(owner, host="127.0.0.1", port=8080, password=PW, alias="box")


async def new_task(e: Env, prompt: str = "", path: str = "/work") -> str:
    created = await e.service.create_task(OWNER, machine="box", path=path, prompt=prompt)
    return created["task_id"]


def last_command(e: Env) -> dict:
    return e.fake.commands[-1][1]


async def test_add_machine_output_never_contains_password(env: Env) -> None:
    result = await add_box(env)
    assert result == {"ok": True, "machine": "box", "replaced": False, "auth": "cookie"}
    assert PW not in json.dumps(result)
    rendered = await env.service.render(OWNER, await env.service.list_machines(OWNER))
    assert PW not in rendered


async def test_add_machine_with_wrong_password_saves_nothing(env: Env) -> None:
    with pytest.raises(OmpError) as info:
        await env.service.add_machine(
            OWNER, host="127.0.0.1", port=8080, password="not-the-pw", alias="box"
        )
    assert info.value.message == MSG_LOGIN_REJECTED
    assert (await env.service.list_machines(OWNER))["machines"] == []


async def test_add_machine_rejects_out_of_range_port(env: Env) -> None:
    with pytest.raises(OmpError):
        await env.service.add_machine(OWNER, host="127.0.0.1", port=0, password=PW, alias="box")


async def test_add_machine_replacing_alias_reports_replaced(env: Env) -> None:
    await add_box(env)
    again = await add_box(env)
    assert again["replaced"] is True
    machines = (await env.service.list_machines(OWNER))["machines"]
    assert [m["alias"] for m in machines] == ["box"]


async def test_machines_are_isolated_between_users(env: Env) -> None:
    await add_box(env, owner=OWNER)
    assert (await env.service.list_machines("u2"))["machines"] == []
    with pytest.raises(OmpError):
        await env.service.delete_machine("u2", "box")


async def test_delete_machine_forgets_its_tasks(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env)
    await env.service.delete_machine(OWNER, "box")
    with pytest.raises(OmpError) as info:
        await env.service.send_prompt(OWNER, task_id, "hi", "auto")
    assert info.value.message == UNKNOWN_TASK_MSG


async def test_listdir_fuzzy_filters_directories_and_projects(env: Env) -> None:
    await add_box(env)
    env.fake.dirs["/work"] = ["alpha", "beta"]
    env.fake.projects = [
        {"alias": "alpha-proj", "path": "/work/alpha"},
        {"alias": "gamma", "path": "/work/gamma"},
    ]
    result = await env.service.listdir(OWNER, "box", "/work", "alp")
    assert [d["name"] for d in result["directories"]] == ["alpha"]
    assert [p["alias"] for p in result["projects"]] == ["alpha-proj"]
    assert result["truncated"] is False


async def test_create_task_with_prompt_is_active_and_tracked(env: Env) -> None:
    await add_box(env)
    created = await env.service.create_task(OWNER, machine="box", path="/work", prompt="go")
    assert created["ok"] is True
    assert created["machine"] == "box"
    assert created["cwd"] == "/work"
    [tracked] = await env.store.all_tasks()
    assert tracked.task_id == created["task_id"]
    assert tracked.active is True


async def test_create_task_without_prompt_is_not_active(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    [tracked] = await env.store.all_tasks()
    assert tracked.task_id == task_id
    assert tracked.active is False


async def test_task_access_is_owner_scoped(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env)
    with pytest.raises(OmpError) as info:
        await env.service.peek("u2", task_id, 0)
    assert info.value.message == UNKNOWN_TASK_MSG


async def test_list_tasks_shows_running_sessions_untracked(env: Env) -> None:
    await add_box(env)
    env.fake.add_session("external-1", cwd="/elsewhere")
    env.fake.sessions["external-1"].running = True
    listing = await env.service.list_tasks(OWNER, "box")
    [task] = listing["tasks"]
    assert task["task_id"] == "external-1"
    assert task["tracked"] is False


async def test_send_prompt_auto_while_streaming_uses_follow_up(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="first")
    env.fake.sessions[task_id].streaming = True
    result = await env.service.send_prompt(OWNER, task_id, "more", "auto")
    assert result == {"ok": True, "task_id": task_id, "accepted": True}
    body = last_command(env)
    assert body == {"type": "prompt", "message": "more", "streamingBehavior": "followUp"}
    assert env.fake.sessions[task_id].queued_follow_up == ["more"]


async def test_send_prompt_auto_when_idle_has_no_follow_up_flag(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    await env.service.send_prompt(OWNER, task_id, "start", "auto")
    assert last_command(env) == {"type": "prompt", "message": "start"}


async def test_send_prompt_rejects_unknown_mode(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env)
    with pytest.raises(OmpError) as info:
        await env.service.send_prompt(OWNER, task_id, "x", "bogus")
    assert "mode 必须为" in info.value.message


async def test_steer_is_rejected_when_task_is_idle(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    count_before = len(env.fake.commands)
    with pytest.raises(OmpError) as info:
        await env.service.steer(OWNER, task_id, "change course")
    assert info.value.message.startswith("任务当前未运行或处于空闲状态")
    assert len(env.fake.commands) == count_before


async def test_steer_posts_when_task_is_busy(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="go")
    result = await env.service.steer(OWNER, task_id, "change course")
    assert result["mode"] == "steer"
    assert last_command(env) == {"type": "steer", "message": "change course"}


async def test_stop_clear_queue_restores_queued_messages(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="go")
    session = env.fake.sessions[task_id]
    session.queued_steering.append("s1")
    session.queued_follow_up.append("f1")
    result = await env.service.stop(OWNER, task_id, clear_queue=True)
    assert result["aborted"] is True
    assert result["restored"] == {"steering": ["s1"], "followUp": ["f1"]}
    assert session.queued_steering == [] and session.queued_follow_up == []


async def test_stop_without_clear_sends_abort(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="go")
    result = await env.service.stop(OWNER, task_id, clear_queue=False)
    assert result["aborted"] is True
    assert "restored" not in result
    assert last_command(env) == {"type": "abort"}


async def test_peek_reports_idle_phase_for_idle_task(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    peek = await env.service.peek(OWNER, task_id, 200)
    assert peek["task_id"] == task_id
    assert peek["running"] is False
    assert peek["phase"] == "idle"


async def test_read_pages_with_cursor(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    env.fake.sessions[task_id].messages = [
        {"id": f"m{i}", "role": "user", "content": f"msg {i}"} for i in range(4)
    ]
    first = await env.service.read(
        OWNER, task_id, limit=2, cursor="", include_thinking=False, max_chars=2000
    )
    assert [m["text"] for m in first["messages"]] == ["msg 0", "msg 1"]
    assert first["has_more"] is True
    assert first["next_cursor"]

    second = await env.service.read(
        OWNER,
        task_id,
        limit=2,
        cursor=first["next_cursor"],
        include_thinking=False,
        max_chars=2000,
    )
    assert [m["text"] for m in second["messages"]] == ["msg 2", "msg 3"]
    assert second["has_more"] is False
    assert second["next_cursor"] == ""


async def test_read_validates_limit_chars_and_cursor(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    base = {"include_thinking": False}
    with pytest.raises(OmpError) as info:
        await env.service.read(OWNER, task_id, limit=0, cursor="", max_chars=2000, **base)
    assert info.value.message == "limit 必须在 1 到 50 之间。"
    with pytest.raises(OmpError) as info:
        await env.service.read(OWNER, task_id, limit=5, cursor="", max_chars=50, **base)
    assert info.value.message == "max_chars 必须在 100 到 20000 之间。"
    with pytest.raises(OmpError) as info:
        await env.service.read(OWNER, task_id, limit=5, cursor="not-json", max_chars=2000, **base)
    assert info.value.message == "cursor 无效，请使用上一页返回的 next_cursor。"


async def test_answer_without_pending_request_is_refused(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    with pytest.raises(OmpError) as info:
        await env.service.answer(OWNER, task_id, "yes", "")
    assert info.value.message == "没有对应的待输入请求，请先读取任务报告。"


async def test_answer_select_posts_value_and_clears_request(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="go")
    task = env.service._task_record(OWNER, task_id)
    task.pending_ui["r1"] = {"method": "select", "options": ["A", "B"]}
    result = await env.service.answer(OWNER, task_id, "B", "")
    assert result == {"ok": True, "task_id": task_id, "request_id": "r1", "accepted": True}
    assert last_command(env) == {"type": "extension_ui_response", "id": "r1", "value": "B"}
    assert task.pending_ui == {}


async def test_answer_select_rejects_option_not_offered(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="go")
    env.service._task_record(OWNER, task_id).pending_ui["r1"] = {
        "method": "select",
        "options": ["A"],
    }
    with pytest.raises(OmpError) as info:
        await env.service.answer(OWNER, task_id, "Z", "r1")
    assert info.value.message == "请选择待输入请求中的一个精确选项。"


async def test_report_returns_then_marks_read(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    task = env.service._task_record(OWNER, task_id)
    await env.service.record_report(task, "error", "boom", {"k": 1}, context_percent=None)

    first = await env.service.report(OWNER, task_id="", mark_read=True)
    assert [r["kind"] for r in first["reports"]] == ["error"]
    assert first["reports"][0]["summary"] == "boom"
    assert (await env.service.report(OWNER, task_id="", mark_read=True))["reports"] == []


async def test_report_is_private_to_its_owner(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    task = env.service._task_record(OWNER, task_id)
    await env.service.record_report(task, "error", "boom", None, context_percent=None)
    assert (await env.service.report("u2", task_id="", mark_read=False))["reports"] == []


async def test_identical_non_progress_reports_are_deduplicated(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    task = env.service._task_record(OWNER, task_id)
    for _ in range(2):
        await env.service.record_report(task, "error", "same", None, context_percent=None)
    assert len((await env.service.report(OWNER, task_id="", mark_read=False))["reports"]) == 1


async def test_progress_reports_are_not_deduplicated(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    task = env.service._task_record(OWNER, task_id)
    for _ in range(2):
        await env.service.record_report(task, "progress", "tick", None, context_percent=10.0)
    assert len((await env.service.report(OWNER, task_id="", mark_read=False))["reports"]) == 2


async def test_reports_are_redacted_before_storage(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    task = env.service._task_record(OWNER, task_id)
    await env.service.record_report(
        task, "error", f"leaked {PW}", {"echo": PW}, context_percent=None
    )
    [report] = (await env.service.report(OWNER, task_id="", mark_read=False))["reports"]
    assert PW not in report["summary"]
    assert PW not in json.dumps(report["detail"])


async def test_report_rejects_unknown_task(env: Env) -> None:
    await add_box(env)
    with pytest.raises(OmpError) as info:
        await env.service.report(OWNER, task_id="nope", mark_read=False)
    assert info.value.message == UNKNOWN_TASK_MSG


async def test_usage_task_scope_requires_task_id(env: Env) -> None:
    with pytest.raises(OmpError) as info:
        await env.service.usage(OWNER, task_id="", scope="task", machine="")
    assert info.value.message.startswith("scope=task 需要 task_id")


async def test_usage_rejects_unknown_scope(env: Env) -> None:
    with pytest.raises(OmpError):
        await env.service.usage(OWNER, task_id="", scope="everything", machine="")


async def test_account_usage_is_fetched_from_ompweb(env: Env) -> None:
    await add_box(env)
    usage = await env.service.usage(OWNER, task_id="", scope="account", machine="box")
    assert isinstance(usage, dict)
    assert PW not in json.dumps(usage)


async def test_compact_returns_token_figures(env: Env) -> None:
    await add_box(env)
    task_id = await new_task(env, prompt="")
    result = await env.service.compact(OWNER, task_id, "keep decisions")
    assert result["tokensBefore"] == 9000
    assert result["summary"] == "ok"
    assert last_command(env) == {"type": "compact", "customInstructions": "keep decisions"}


async def test_render_redacts_saved_password(env: Env) -> None:
    await add_box(env)
    text = await env.service.render(OWNER, {"echo": f"contains {PW} inside"})
    assert PW not in text


async def test_save_task_persists_tracked_state(env: Env) -> None:
    task = TrackedTask(owner=OWNER, task_id="t9", alias="box", cwd="/w", created_at=1.0)
    env.service._tasks[(OWNER, "t9")] = task
    await env.service.save_task(task)
    assert [t.task_id for t in await env.store.all_tasks()] == ["t9"]
