from datetime import timedelta

import pytest

from telegram_userbot.application.ports import (
    AsyncUnitOfWork,
    Clock,
    EmbeddingGateway,
    IdFactory,
    JobQueue,
    ModelGateway,
    MonotonicClock,
    RandomSource,
    TelegramGateway,
)
from telegram_userbot.application.ports.model import EmbeddingRequest, ModelRequest
from telegram_userbot.application.ports.queue import JobEnvelope
from telegram_userbot.application.ports.telegram import TelegramTextRequest
from telegram_userbot.domain.shared.ids import AccountId, ConversationId, JobId, RunId
from telegram_userbot.domain.shared.redaction import SensitiveValue
from tests.support.fakes import (
    DeterministicIdFactory,
    DeterministicRandom,
    FakeEmbeddingGateway,
    FakeJobQueue,
    FakeModelGateway,
    FakeTelegramGateway,
    FakeUnitOfWork,
    VirtualClock,
    fixed_utc,
)


@pytest.mark.contract
def test_clock_random_and_id_fakes_satisfy_ports() -> None:
    clock = VirtualClock(fixed_utc())
    random_source = DeterministicRandom(7)
    id_factory = DeterministicIdFactory()
    assert isinstance(clock, Clock)
    assert isinstance(clock, MonotonicClock)
    assert isinstance(random_source, RandomSource)
    assert isinstance(id_factory, IdFactory)
    initial = clock.now()
    clock.advance(2.5)
    assert clock.now() == initial.add(timedelta(seconds=2.5))
    assert DeterministicRandom(7).random_bytes(8) == DeterministicRandom(7).random_bytes(8)
    assert id_factory.new(AccountId) != id_factory.new(AccountId)


@pytest.mark.contract
@pytest.mark.asyncio
async def test_transaction_and_queue_fakes_satisfy_ports() -> None:
    unit_of_work = FakeUnitOfWork()
    queue = FakeJobQueue()
    assert isinstance(unit_of_work, AsyncUnitOfWork)
    assert isinstance(queue, JobQueue)
    async with unit_of_work:
        await unit_of_work.commit()
    job = JobEnvelope(JobId.new(), "synthetic", "0" * 64, fixed_utc())
    await queue.enqueue(job)
    assert unit_of_work.commits == 1
    assert queue.jobs == [job]


@pytest.mark.contract
@pytest.mark.asyncio
async def test_external_gateway_fakes_satisfy_ports_without_network() -> None:
    telegram = FakeTelegramGateway()
    model = FakeModelGateway()
    embedding = FakeEmbeddingGateway()
    assert isinstance(telegram, TelegramGateway)
    assert isinstance(model, ModelGateway)
    assert isinstance(embedding, EmbeddingGateway)
    run_id = RunId.new()
    receipt = await telegram.send_text(
        TelegramTextRequest(
            AccountId.new(),
            ConversationId.new(),
            run_id,
            1,
            SensitiveValue("SYNTHETIC_OUTPUT"),
        )
    )
    response = await model.generate(ModelRequest(run_id, "main_ai", "1" * 64))
    vector = await embedding.embed(EmbeddingRequest(run_id, "embedding", "2" * 64))
    assert receipt.telegram_message_id == 1
    assert response.text.reveal_for_use() == "SYNTHETIC_MODEL_OUTPUT"
    assert vector.vector == (0.0, 1.0)
