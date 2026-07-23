import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

from kiro_api_proxy.config import Settings
from kiro_api_proxy.transports import (
    AdaptiveTransport,
    ErrorCategory,
    EventType,
    GenerationEvent,
    GenerationRequest,
    TransportError,
)
from kiro_api_proxy.transports.acp import AcpTransport
from kiro_api_proxy.sessions import SessionRecord


class FakeTransport:
    def __init__(self, name: str, error: TransportError | None = None):
        self.name = name
        self.error = error
        self.calls = 0

    async def start(self):
        return None

    async def close(self):
        return None

    async def models(self):
        return [{"model_id": "auto"}]

    async def generate(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.name

    async def stream(
        self, request
    ) -> AsyncIterator[GenerationEvent]:
        self.calls += 1
        if self.error:
            yield GenerationEvent(
                EventType.ERROR,
                text=str(self.error),
                data={"category": self.error.category.value},
            )
            return
        yield GenerationEvent(EventType.TEXT_DELTA, text=self.name)
        yield GenerationEvent(EventType.DONE)

    async def cancel(self, request_id):
        return None


async def test_adaptive_transport_falls_back_before_output():
    acp = FakeTransport(
        "acp",
        TransportError("协议损坏", ErrorCategory.PROTOCOL, retryable=True),
    )
    cli = FakeTransport("cli")
    router = AdaptiveTransport([acp, cli])
    result = await router.generate(GenerationRequest("auto", "提示"))
    assert result == "cli"
    assert router.actual_name == "cli"


async def test_adaptive_stream_does_not_mix_after_output():
    class Partial(FakeTransport):
        async def stream(self, request):
            yield GenerationEvent(EventType.TEXT_DELTA, text="部分")
            yield GenerationEvent(
                EventType.ERROR,
                text="EOF",
                data={"category": ErrorCategory.PROTOCOL.value},
            )

    cli = FakeTransport("cli")
    router = AdaptiveTransport([Partial("acp"), cli])
    events = [
        event
        async for event in router.stream(GenerationRequest("auto", "提示"))
    ]
    assert [event.text for event in events] == ["部分", "EOF"]
    assert cli.calls == 0


def test_acp_extracts_latest_user_turn():
    prompt = (
        "### 系统指令\n中文\n\n"
        "### 用户\n第一问\n\n"
        "### 助手\n第一答\n\n"
        "### 用户\n第二问\n\n### 助手\n"
    )
    assert AcpTransport._latest_user_turn(prompt) == "第二问"


def _acp_settings(**overrides):
    values = {
        "session_ttl_seconds": 60,
        "session_max_entries": 10,
        "acp_max_workers": 1,
        "acp_queue_size": 1,
        "timeout_seconds": 5,
        "session_reuse_enabled": True,
        "session_max_turns": 40,
        "session_max_context_chars": 200000,
        "session_compaction_ratio": 0.7,
        "working_directory": "/workspace",
        "extra_path": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeAcpWorker:
    def __init__(
        self,
        responses,
        *,
        worker_id="worker",
        model="auto",
        effort="",
        working_directory="/workspace",
    ):
        self.id = worker_id
        self.model = model
        self.effort = effort
        self.working_directory = working_directory
        self.healthy = True
        self.active = 0
        self.lock = asyncio.Lock()
        self.responses = list(responses)
        self.prompts = []
        self.new_sessions = 0
        self.session_directories = []
        self.closed = False

    async def close(self):
        self.closed = True

    async def new_session(self, working_directory=None):
        self.new_sessions += 1
        self.session_directories.append(working_directory)
        return f"new-session-{self.new_sessions}"

    async def prompt(self, session_id, prompt):
        self.prompts.append((session_id, prompt))
        for event in self.responses.pop(0):
            yield event


async def test_acp_saturation_falls_back_without_leaking_capacity():
    acp = AcpTransport(
        _acp_settings(acp_max_workers=1, acp_queue_size=0),
        FakeTransport("models"),
    )
    worker = FakeAcpWorker([])
    worker.active = 1
    acp.workers = [worker]
    cli = FakeTransport("cli")
    router = AdaptiveTransport([acp, cli])

    for _ in range(2):
        events = [
            event
            async for event in router.stream(
                GenerationRequest("auto", "提示")
            )
        ]
        assert [
            event.text
            for event in events
            if event.type is EventType.TEXT_DELTA
        ] == ["cli"]

    assert cli.calls == 2
    assert not acp._capacity.locked()


async def test_acp_replaces_idle_worker_when_project_changes():
    transport = AcpTransport(
        _acp_settings(acp_max_workers=1),
        FakeTransport("models"),
    )
    previous = FakeAcpWorker(
        [],
        worker_id="project-a",
        working_directory="/workspace/project-a",
    )
    replacement = FakeAcpWorker(
        [],
        worker_id="project-b",
        working_directory="/workspace/project-b",
    )
    transport.workers = [previous]

    async def spawn_worker(model, effort, working_directory):
        assert (model, effort, working_directory) == (
            "auto",
            "",
            "/workspace/project-b",
        )
        transport.workers.append(replacement)
        return replacement

    transport._spawn_worker = spawn_worker

    selected = await transport._select_worker(
        "auto",
        "",
        previous.id,
        "/workspace/project-b",
    )

    assert selected is replacement
    assert previous.closed
    assert transport.workers == [replacement]


async def test_acp_rebalances_busy_preferred_worker():
    transport = AcpTransport(
        _acp_settings(acp_max_workers=2),
        FakeTransport("models"),
    )
    busy = FakeAcpWorker(
        [],
        worker_id="busy-opus",
        model="claude-opus-4.8",
        effort="high",
        working_directory="/workspace/project",
    )
    busy.active = 1
    idle = FakeAcpWorker(
        [],
        worker_id="idle-sonnet",
        model="claude-sonnet-5",
        effort="high",
        working_directory="/workspace/other",
    )
    replacement = FakeAcpWorker(
        [
            [
                GenerationEvent(EventType.TEXT_DELTA, text="并行完成"),
                GenerationEvent(EventType.DONE),
            ]
        ],
        worker_id="new-opus",
        model="claude-opus-4.8",
        effort="high",
        working_directory="/workspace/project",
    )
    transport.workers = [busy, idle]

    async def spawn_worker(model, effort, working_directory):
        assert (model, effort, working_directory) == (
            "claude-opus-4.8",
            "high",
            "/workspace/project",
        )
        transport.workers.append(replacement)
        return replacement

    transport._spawn_worker = spawn_worker
    record = SessionRecord(
        "tenant:second-session",
        busy.id,
        "old-session",
        turn_count=2,
        last_prompt_chars=100,
        upstream_context_chars=150,
    )
    await transport.sessions.put(record)
    prompt = "### 用户\n继续第二个会话\n\n### 助手\n"

    events = [
        event
        async for event in transport.stream(
            GenerationRequest(
                "claude-opus-4.8",
                prompt,
                effort="high",
                session_id=record.key,
                working_directory="/workspace/project",
            )
        )
    ]

    assert idle.closed
    assert busy.active == 1
    assert replacement.prompts == [("new-session-1", prompt)]
    assert replacement.session_directories == ["/workspace/project"]
    assert events[0].data["session_rebuild_reason"] == "worker_changed"
    updated = await transport.sessions.get(record.key)
    assert updated.worker_id == replacement.id


async def test_acp_transport_serializes_same_session_with_record_lock():
    transport = AcpTransport(_acp_settings(), FakeTransport("models"))
    worker = FakeAcpWorker(
        [
            [
                GenerationEvent(EventType.TEXT_DELTA, text="完成"),
                GenerationEvent(EventType.DONE),
            ]
        ]
    )
    transport.workers = [worker]
    record = SessionRecord("tenant:session", worker.id, "upstream")
    await transport.sessions.put(record)
    await record.lock.acquire()

    async def collect():
        return [
            event
            async for event in transport.stream(
                GenerationRequest(
                    "auto",
                    "### 用户\n下一问\n\n### 助手\n",
                    session_id=record.key,
                )
            )
        ]

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    assert worker.prompts == []
    record.lock.release()

    events = await task
    assert [event.type for event in events] == [
        EventType.TEXT_DELTA,
        EventType.DONE,
    ]


async def test_acp_rotates_after_client_compaction():
    transport = AcpTransport(_acp_settings(), FakeTransport("models"))
    worker = FakeAcpWorker(
        [
            [
                GenerationEvent(EventType.TEXT_DELTA, text="完成"),
                GenerationEvent(EventType.DONE),
            ]
        ]
    )
    transport.workers = [worker]
    record = SessionRecord(
        "tenant:session",
        worker.id,
        "old-session",
        turn_count=3,
        last_prompt_chars=1000,
        upstream_context_chars=1200,
    )
    await transport.sessions.put(record)
    prompt = "### 系统指令\n摘要\n\n### 用户\n压缩后问题\n\n### 助手\n"

    events = [
        event
        async for event in transport.stream(
            GenerationRequest("auto", prompt, session_id=record.key)
        )
    ]

    assert [event.type for event in events] == [
        EventType.TEXT_DELTA,
        EventType.DONE,
    ]
    assert events[0].data["session_rebuild_reason"] == "client_compacted"
    assert worker.new_sessions == 1
    assert worker.prompts == [("new-session-1", prompt)]
    updated = await transport.sessions.get(record.key)
    assert updated.turn_count == 1
    assert updated.last_prompt_chars == len(prompt)
    assert updated.upstream_context_chars == len(prompt) + len("完成")
    assert updated.rebuilt is False


def test_acp_rotation_respects_turn_and_character_limits():
    transport = AcpTransport(
        _acp_settings(session_max_turns=2, session_max_context_chars=100),
        FakeTransport("models"),
    )
    prompt = "### 用户\n下一问\n\n### 助手\n"
    record = SessionRecord(
        "tenant:session",
        "worker",
        "session",
        turn_count=2,
        last_prompt_chars=len(prompt),
        upstream_context_chars=80,
    )
    latest_turn = transport._latest_user_turn(prompt)

    assert (
        transport._rotation_reason(record, prompt, latest_turn) == "max_turns"
    )

    record.turn_count = 1
    record.upstream_context_chars = 100
    assert (
        transport._rotation_reason(record, prompt, latest_turn)
        == "max_context_chars"
    )


async def test_acp_rebuilds_once_after_context_overflow():
    transport = AcpTransport(_acp_settings(), FakeTransport("models"))
    worker = FakeAcpWorker(
        [
            [
                GenerationEvent(
                    EventType.ERROR,
                    text="maximum context length exceeded",
                )
            ],
            [
                GenerationEvent(EventType.TEXT_DELTA, text="已恢复"),
                GenerationEvent(EventType.DONE),
            ],
        ]
    )
    transport.workers = [worker]
    prompt = (
        "### 用户\n第一问\n\n"
        "### 助手\n第一答\n\n"
        "### 用户\n第二问\n\n### 助手\n"
    )
    record = SessionRecord(
        "tenant:session",
        worker.id,
        "old-session",
        turn_count=2,
        last_prompt_chars=len(prompt),
        upstream_context_chars=150,
    )
    await transport.sessions.put(record)

    events = [
        event
        async for event in transport.stream(
            GenerationRequest("auto", prompt, session_id=record.key)
        )
    ]

    assert [event.type for event in events] == [
        EventType.TEXT_DELTA,
        EventType.DONE,
    ]
    assert events[0].data["session_rebuild_reason"] == "context_overflow"
    assert worker.new_sessions == 1
    assert worker.prompts == [
        ("old-session", "第二问"),
        ("new-session-1", prompt),
    ]
    updated = await transport.sessions.get(record.key)
    assert updated.turn_count == 1
    assert updated.upstream_session_id == "new-session-1"
    assert updated.upstream_context_chars == len(prompt) + len("已恢复")
    assert updated.rebuilt is False
