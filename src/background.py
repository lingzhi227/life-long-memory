"""Background job processor for memory operations."""

from __future__ import annotations

import asyncio
import logging
import traceback

from src.db import MemoryDB
from src.entities import extract_entities_for_session

logger = logging.getLogger(__name__)


async def process_job(db: MemoryDB, job: dict) -> None:
    """Process a single job from the queue."""
    job_type = job["job_type"]
    target_id = job["target_id"]

    if job_type == "extract_entities":
        count = extract_entities_for_session(db, target_id)
        logger.info(f"Extracted {count} entities from session {target_id}")

    elif job_type == "summarize":
        from src.summarize import summarize_session_anthropic
        result = await summarize_session_anthropic(db, target_id)
        if result:
            logger.info(f"Summarized session {target_id}")
        else:
            logger.warning(f"Failed to summarize session {target_id}")

    elif job_type == "promote":
        from src.promote import promote_project_knowledge_anthropic
        entries = await promote_project_knowledge_anthropic(db, target_id)
        logger.info(f"Promoted {len(entries)} knowledge entries for {target_id}")

    else:
        logger.warning(f"Unknown job type: {job_type}")


async def run_worker(db: MemoryDB, max_jobs: int | None = None) -> int:
    """Process jobs from the queue until empty or max_jobs reached."""
    processed = 0
    while max_jobs is None or processed < max_jobs:
        job = db.claim_job()
        if not job:
            break

        try:
            await process_job(db, job)
            db.finish_job(job["id"])
            processed += 1
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            db.finish_job(job["id"], error=error_msg)
            logger.error(f"Job {job['id']} failed: {e}")

    return processed


async def run_background_loop(db: MemoryDB, poll_interval: float = 5.0) -> None:
    """Continuously process jobs, polling for new ones."""
    logger.info("Background worker started")
    while True:
        processed = await run_worker(db, max_jobs=10)
        if processed == 0:
            await asyncio.sleep(poll_interval)
