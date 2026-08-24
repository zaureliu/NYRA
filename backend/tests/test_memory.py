from pathlib import Path

import pytest

from app.memory.models import MemoryCategory, MemoryCreate
from app.memory.repository import MemoryRepository


@pytest.mark.asyncio
async def test_memory_persists_and_fts_searches(tmp_path: Path):
    path = tmp_path / "nyra.db"
    repository = MemoryRepository(path)
    await repository.initialize()
    created = await repository.add(MemoryCreate(category=MemoryCategory.SEMANTIC, content="O Proxmox hospeda as máquinas virtuais", importance=8))
    assert created.id > 0
    second_instance = MemoryRepository(path)
    await second_instance.initialize()
    results = await second_instance.search("máquinas Proxmox")
    assert [item.content for item in results] == ["O Proxmox hospeda as máquinas virtuais"]
    assert await second_instance.health()


@pytest.mark.asyncio
async def test_memory_delete_and_importance(tmp_path: Path):
    repository = MemoryRepository(tmp_path / "nyra.db")
    await repository.initialize()
    created = await repository.add(MemoryCreate(category=MemoryCategory.PREFERENCES, content="Prefere respostas objetivas"))
    assert await repository.set_importance(created.category, created.id, 9)
    assert (await repository.get(created.category, created.id)).importance == 9
    assert await repository.delete(created.category, created.id)
    assert await repository.get(created.category, created.id) is None

