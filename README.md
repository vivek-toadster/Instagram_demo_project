# Instagram Clone

A simple Instagram-inspired web application built using **FastAPI and PostgreSQL**. It allows users to create an account, log in securely, manage their profiles, and update their personal information.

## Features

- **User Registration & Login** – Create an account and log in securely.
- **Password Hashing** – Store passwords securely using hashing.
- **User Profile** – View personal profile information.
- **Edit Profile** – Update profile details.
- **Session Authentication** – Maintain user login sessions.

## Tech Stack

- **Backend:** Python, FastAPI
- **Database:** PostgreSQL
- **ORM:** SQLAlchemy
- **Migrations:** Alembic
- **Templating:** Jinja2
- **Frontend:** HTML, CSS

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

