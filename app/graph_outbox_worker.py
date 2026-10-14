"""Run the PostgreSQL-to-Neo4j outbox processor as a simple worker."""

import argparse
import logging
import time

from app.database import SessionLocal
from app.graph_outbox import process_graph_outbox, requeue_dead_letter_events
from app.neo4j_client import initialize_neo4j, shutdown_neo4j
from app.observability import log_event

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="process one bounded batch and exit")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between batches")
    parser.add_argument(
        "--replay-dead-letter",
        action="store_true",
        help="requeue dead-lettered events before processing",
    )
    args = parser.parse_args()
    initialize_neo4j()
    try:
        if args.replay_dead_letter:
            requeue_dead_letter_events(SessionLocal)
        while True:
            result = process_graph_outbox(SessionLocal)
            log_event("outbox_worker_batch", **result)
            if args.once:
                return
            time.sleep(max(args.interval, 0.5))
    except KeyboardInterrupt:
        logger.info("Graph outbox worker stopping")
    finally:
        shutdown_neo4j()


if __name__ == "__main__":
    main()
