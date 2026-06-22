"""Repo → GitHub App installation resolution, cached.

The GitHubAppClient needs an installation id to mint a token for a repo, but
the GitHubClient ABC operates on `owner/repo` names and stays DB-agnostic. This
seam bridges that: the client asks a resolver, and only falls back to the
GitHub API (the source of truth) on a miss — caching the answer for next time.

`DbInstallationResolver` persists the cache in the installation_repos table, so
it survives restarts and is kept current by the installation* webhooks. Tests
use an in-memory resolver instead.
"""

import logging
from abc import ABC, abstractmethod

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from gojira.models import InstallationRepo

log = logging.getLogger(__name__)


class InstallationResolver(ABC):
    """Cache of repo → installation id, written through to durable storage."""

    @abstractmethod
    def resolve(self, repo: str) -> int | None:
        """Cached installation id for repo, or None on a miss."""
        ...

    @abstractmethod
    def remember(self, repo: str, installation_id: int) -> None:
        """Record (or update) the installation id for repo."""
        ...

    @abstractmethod
    def forget(self, repo: str) -> None:
        """Drop repo from the cache (installation uninstalled / repo removed)."""
        ...


class DbInstallationResolver(InstallationResolver):
    """Table-backed resolver over installation_repos."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sf = session_factory

    def resolve(self, repo: str) -> int | None:
        with self._sf() as s:
            row = s.scalar(
                select(InstallationRepo).where(InstallationRepo.repo_full_name == repo)
            )
            return row.installation_id if row else None

    def remember(self, repo: str, installation_id: int) -> None:
        with self._sf() as s:
            row = s.scalar(
                select(InstallationRepo).where(InstallationRepo.repo_full_name == repo)
            )
            if row is None:
                s.add(InstallationRepo(repo_full_name=repo, installation_id=installation_id))
            else:
                row.installation_id = installation_id
            s.commit()
        log.debug("cached installation %s for %s", installation_id, repo)

    def forget(self, repo: str) -> None:
        with self._sf() as s:
            s.execute(delete(InstallationRepo).where(InstallationRepo.repo_full_name == repo))
            s.commit()
        log.debug("forgot installation mapping for %s", repo)
