"""Background generation of revision-bound search aliases."""
import logging

from .paid_llm import paid_ready
from .write_enrichment import generate_questions

_LOGGER = logging.getLogger(__name__)


def enrich_memory_aliases(service, memory_id: str, revision_id: str, project: str, content: str, title: str = "") -> None:
    if not paid_ready():
        return
    try:
        aliases = generate_questions(title, content)
        if aliases:
            service.save_memory_aliases(memory_id, revision_id, project, aliases)
    except Exception as exc:
        _LOGGER.warning("Alias enrichment failed (%s)", type(exc).__name__)


def enrich_written_record(service_factory, record: dict) -> None:
    """Resolve the authoritative revision off the event loop before enrichment."""
    try:
        service = service_factory()
        memory_id = str(record.get("source_id") or record.get("id") or "")
        project = str(record.get("project") or "")
        memory = service.repository.get_memory(memory_id, project=project)
        if memory is None or memory.current_revision.content != record.get("content"):
            return
        enrich_memory_aliases(service, memory_id, memory.current_revision.revision_id, project,
                              memory.current_revision.content, memory.title or "")
    except Exception as exc:
        _LOGGER.warning("Alias enrichment failed (%s)", type(exc).__name__)
