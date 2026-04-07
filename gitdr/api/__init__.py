"""
FastAPI router package for GitDR.

Each sub-module exposes an APIRouter that is included in gitdr.main.app.
Routers added here in later phases:
  - sources  (/api/v1/sources)
  - repos    (/api/v1/repos)
  - jobs     (/api/v1/jobs)
  - runs     (/api/v1/runs)
  - system   (/api/v1/system) - health check lives in main.py for Phase 1
"""
