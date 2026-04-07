"""
Service layer for GitDR.

Services contain the business logic that sits between the HTTP routes (api/)
and the database models (database/). All database I/O goes through a SQLModel
Session passed in as a dependency; service functions are plain synchronous
functions so they can be called from both FastAPI route handlers and the
APScheduler job runner.
"""
