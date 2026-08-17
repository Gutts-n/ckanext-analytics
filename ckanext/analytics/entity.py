"""Turning the references a caller sent into names.

Callers send whatever they have - ``id=3f9c-4a11-…`` as often as
``id=daily-balancing-costs`` - and never send the organization at all. So references
are looked up: a resource leads to its dataset, a dataset to its organization.

Names, not uuids. A uuid in an event is unresolvable at read time, since Loki cannot
join and BigQuery would need a dimension table kept in step with CKAN. Renaming a
dataset therefore splits its history, which is rare and worth it.

``EntityLookups`` is the database, ``EntityCache`` is Redis, ``EntityResolver`` is
the rules. All of it runs while a user waits, so nothing here raises and nothing
hangs: a failure leaves a field empty.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

#: The entities an event can be attributed to.
ENTITIES = ("dataset", "resource", "organization", "group")

#: How long a name is trusted. Longer means fewer queries and staler names after a
#: rename; there is no correctness cost either way.
TTL_SECONDS = 300

#: Waiting longer than this for a cache would cost more than the query it avoids.
SOCKET_TIMEOUT = 0.25


def ckan_model() -> Any:
    """CKAN's model, imported late so this module can be imported without CKAN."""
    import ckan.model

    return ckan.model


class EntityLookups:
    """The database: a reference in, the names it establishes out.

    Method names match the entity keys, so the resolver can ask for a kind of thing
    without knowing how it is found. Each returns everything it learned on the way,
    so one resource lookup fills three fields.
    """

    def resource(self, ref: str) -> dict[str, Any]:
        resource = ckan_model().Resource.get(ref)
        if resource is None:
            return {}
        return {
            "resource": resource.name or resource.id,
            **self.dataset(resource.package_id),
        }

    def dataset(self, ref: str) -> dict[str, Any]:
        """``ref`` may be a name or a uuid; CKAN accepts either."""
        package = ckan_model().Package.get(ref)
        if package is None:
            return {}

        names: dict[str, Any] = {"dataset": package.name or package.id}
        if package.owner_org:
            names.update(self.organization(package.owner_org))
        return names

    def organization(self, ref: str) -> dict[str, Any]:
        return {"organization": self._group_name(ref)}

    def group(self, ref: str) -> dict[str, Any]:
        return {"group": self._group_name(ref)}

    @staticmethod
    def _group_name(ref: str) -> str | None:
        """Organizations and groups share one table."""
        group = ckan_model().Group.get(ref)
        return (group.name or group.id) if group is not None else None


class EntityCache:
    """Names in Redis, keyed by the kind of thing and the reference.

    Uses CKAN's own ``ckan.redis.url``, so there is nothing new to configure. Every
    method swallows its failures: an unreachable Redis means doing the lookup
    ourselves, which is slower and still correct.
    """

    #: Namespaced, because this Redis also holds CKAN's sessions and job queues.
    PREFIX = "ckanext-analytics:entity:"

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._client_built = client is not None

    def get(self, kind: str, ref: str) -> dict[str, Any] | None:
        """The cached names, or None for a miss."""
        client = self.client()
        if client is None:
            return None

        try:
            raw = client.get(self._key(kind, ref))
        except Exception:
            # No traceback: a Redis outage would fill the log, and the only
            # consequence is doing the lookup ourselves.
            log.warning("analytics: could not read the entity cache")
            return None

        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def set(self, kind: str, ref: str, names: dict[str, Any]) -> None:
        """Cache these names, including when they are empty.

        A reference that resolves to nothing is worth caching: otherwise a bad id in
        a loop reaches the database on every request, which is the load this cache
        exists to prevent.
        """
        client = self.client()
        if client is None:
            return

        try:
            client.setex(self._key(kind, ref), TTL_SECONDS, json.dumps(names))
        except Exception:
            log.warning("analytics: could not write the entity cache")

    def client(self) -> Any:
        """The Redis client, built once. None if there is none to be had.

        No lock: two threads racing here would build two clients and keep one, which
        costs nothing.
        """
        if not self._client_built:
            self._client = self._build_client()
            self._client_built = True
        return self._client

    def _key(self, kind: str, ref: str) -> str:
        return f"{self.PREFIX}{kind}:{ref}"

    def _build_client(self) -> Any:
        """CKAN's Redis, but with timeouts.

        ``ckan.lib.redis.connect_to_redis`` shares a connection pool, which is what
        we want, but sets no socket timeout - so a Redis that accepts connections and
        then stops answering would hold requests open rather than erroring. Same URL,
        own timeouts.
        """
        try:
            from ckan.common import config
            from redis import Redis

            url = config.get("ckan.redis.url")
            if not url:
                log.warning("analytics: ckan.redis.url is not set, entities are not cached")
                return None

            return Redis.from_url(
                url,
                socket_timeout=SOCKET_TIMEOUT,
                socket_connect_timeout=SOCKET_TIMEOUT,
                decode_responses=True,
            )
        except Exception:
            log.exception("analytics: could not reach Redis, entities are not cached")
            return None


class EntityResolver:
    """References in, names out."""

    #: Entities a caller can name outright. Anything else is reached by walking up
    #: from one of these.
    DIRECT = ("organization", "group")

    def __init__(self, lookups: Any = None, cache: Any = None) -> None:
        self.lookups = lookups if lookups is not None else EntityLookups()
        self.cache = cache if cache is not None else EntityCache()

    def resolve(self, refs: dict[str, Any]) -> dict[str, Any]:
        """Names for everything ``refs`` points at, plus whatever sits above it.

        Answers from the database win over what the caller sent, since the caller may
        have sent a uuid where a name is wanted.
        """
        found = {key: value for key, value in refs.items() if value}

        # What the caller named directly: one reference, one lookup.
        for kind in self.DIRECT:
            if refs.get(kind):
                self._fill(found, kind, refs[kind])

        # Then upwards, for what a request never carries.
        if found.get("resource"):
            self._fill(found, "resource", found["resource"])

        if found.get("dataset") and not found.get("organization"):
            self._fill(found, "dataset", found["dataset"])

        return {name: found.get(name) for name in ENTITIES}

    def _fill(self, found: dict[str, Any], kind: str, ref: str) -> None:
        """Merge in the names for one reference, from the cache or the database."""
        names = self.cache.get(kind, ref)

        if names is None:
            try:
                names = getattr(self.lookups, kind)(ref)
            except Exception:
                # Deliberately not cached. "Not found" is worth remembering; a
                # database that was briefly unwell is not, or the field would stay
                # blank for the whole TTL.
                log.exception("analytics: %s lookup failed for %s", kind, ref)
                return
            self.cache.set(kind, ref, names)

        found.update({key: value for key, value in names.items() if value})


_resolver: EntityResolver | None = None


def resolve_entities(refs: dict[str, Any]) -> dict[str, Any]:
    """Names for whatever a call referred to. The one function ``event.py`` needs.

    Something module-level has to own the resolver: the ``request_finished`` listener
    is a blinker receiver, so its signature is fixed and there is nowhere to hand it
    one.

    Built on first use rather than at import, because at import the Redis client would
    belong to whichever process loaded the module - under ``gunicorn --preload`` the
    master - and then be inherited by every worker through the fork, leaving them
    sharing one socket.
    """
    global _resolver
    if _resolver is None:
        _resolver = EntityResolver()
    return _resolver.resolve(refs)
