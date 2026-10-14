# Luma

Luma is a small Instagram-style social web application built with FastAPI. It supports authentication, profiles, posts, likes, comments, follows, a personalized feed, and a PostgreSQL-to-Neo4j graph projection.

PostgreSQL is the source of truth. Neo4j is a rebuildable read-side graph used for relationship-aware feed candidates. This keeps normal app operations safe even when Neo4j is temporarily unavailable.

## Features

- User registration and login with username or email
- Secure password hashing and session authentication
- User profiles, bios, follower/following counts, and profile editing
- Image posts with captions and hashtag extraction
- Personalized feed with ranking and PostgreSQL fallback
- Likes, comments, follows, and post/comment deletion
- AJAX actions that update the UI without a full-page refresh
- Transactional PostgreSQL outbox and Neo4j graph projection
- Health endpoints, metrics, retries, idempotency, and dead-letter handling

## Application flow

```text
Browser
  |
  | HTML pages + AJAX actions
  v
FastAPI routes (app/routes.py)
  |
  +--> PostgreSQL / SQLAlchemy  <-- source of truth
  |       users, posts, likes,
  |       comments, follows,
  |       graph_outbox_events
  |
  +--> Transactional graph event
              |
              v
       graph_outbox_worker
              |
              v
          Neo4j graph projection
              |
              v
       Relationship-aware feed candidates
```

For a user action, the route commits the business record and an outbox event to PostgreSQL in the same transaction. The worker later applies the event to Neo4j. The feed asks Neo4j for relationship candidates, loads the real posts from PostgreSQL, and ranks them. If Neo4j is unavailable, the feed falls back to PostgreSQL.

## Technology stack

- Python, FastAPI, and Uvicorn
- PostgreSQL, SQLAlchemy, and Alembic
- Neo4j Python driver
- Jinja2 templates
- Vanilla HTML, CSS, and JavaScript
- Pytest

## Installation & Setup

1. Clone the repository:

   ```bash
   git clone <repository-url>
   cd <project-folder>
   ```

2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Configure your PostgreSQL database and environment variables.

4. Apply database migrations:

   ```bash
   alembic upgrade head
   ```

5. Start the development server:

   ```bash
   uvicorn main:app --reload
   ```

6. Open `http://127.0.0.1:8000` in your browser.

## Project Goal

The goal of this project is to build a basic Instagram-style application while learning user authentication, database integration, profile management, and backend development with FastAPI.