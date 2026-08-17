"""Attribution rules, and turning the ids a caller sent into names.

No CKAN and no database: ``EntityLookups`` and Redis are both replaced with stubs
handed to the resolver, so nothing here is patched into place::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -o addopts='' \
      ckanext/analytics/tests/test_entity.py
"""
from __future__ import annotations

import pytest

from ckanext.analytics.entity import TTL_SECONDS, EntityCache, EntityResolver
from ckanext.analytics.event import Attribution


ATTRIBUTION = Attribution()


class TestIdMeans:
    """What ``id`` refers to, by action family."""

    @pytest.mark.parametrize(
        "action, expected",
        [
            ("package_show", "dataset"),
            ("package_patch", "dataset"),
            ("package_activity_list", "dataset"),
            ("resource_show", "resource"),
            ("resource_update", "resource"),
            ("organization_show", "organization"),
            ("organization_member_create", "organization"),
            ("group_show", "group"),
            ("group_member_create", "group"),
            ("member_list", "group"),
        ],
    )
    def test_families(self, action, expected):
        assert ATTRIBUTION.id_means(action) == expected

    def test_organization_is_matched_before_group_could_be(self):
        """Both families exist; the longer, more specific prefix has to win."""
        assert ATTRIBUTION.id_means("organization_show") == "organization"
        assert ATTRIBUTION.id_means("group_show") == "group"

    @pytest.mark.parametrize(
        "action, expected",
        [
            ("follow_dataset", "dataset"),
            ("unfollow_dataset", "dataset"),
            ("follow_group", "group"),
        ],
    )
    def test_actions_whose_name_does_not_carry_their_family(self, action, expected):
        assert ATTRIBUTION.id_means(action) == expected

    def test_an_unknown_action_claims_nothing(self):
        assert ATTRIBUTION.id_means("status_show") is None
        assert ATTRIBUTION.id_means(None) is None

    def test_user_actions_are_not_attributed(self):
        """Only the caller is recorded, as the ``user`` field - never who an
        action acted on."""
        assert ATTRIBUTION.id_means("user_show") is None
        assert ATTRIBUTION.id_means("follow_user") is None


class TestEntityRefs:
    """Both layers: named parameters, and what ``id`` means."""

    def test_a_dataset_is_read_from_id(self):
        assert ATTRIBUTION.refs("package_show", {"id": "costs-2026"}) == {"dataset": "costs-2026"}

    def test_a_resource_is_read_from_id(self):
        assert ATTRIBUTION.refs("resource_show", {"id": "res-uuid"}) == {"resource": "res-uuid"}

    def test_a_named_parameter_is_read_whatever_the_action(self):
        """How actions nobody enumerated here still get attributed."""
        assert ATTRIBUTION.refs("xloader_submit", {"resource_id": "res-uuid"}) == {
            "resource": "res-uuid"
        }

    def test_datastore_actions_are_covered_by_resource_id_alone(self):
        for action in ("datastore_upsert", "datastore_search", "datastore_delete"):
            assert ATTRIBUTION.refs(action, {"resource_id": "res-uuid"}) == {"resource": "res-uuid"}

    def test_a_search_that_names_a_resource_is_still_attributed(self):
        """datastore_search is a search and yet names exactly one resource."""
        assert ATTRIBUTION.refs("datastore_search", {"resource_id": "res-uuid"})["resource"]

    def test_resource_create_names_its_dataset(self):
        assert ATTRIBUTION.refs("resource_create", {"package_id": "costs-2026"}) == {
            "dataset": "costs-2026"
        }

    def test_package_create_names_its_organization_before_it_exists(self):
        assert ATTRIBUTION.refs("package_create", {"name": "new", "owner_org": "org-uuid"}) == {
            "organization": "org-uuid"
        }

    def test_the_acted_on_user_is_not_a_reference(self):
        refs = ATTRIBUTION.refs(
            "organization_member_create", {"id": "neso", "username": "analyst_jane"}
        )

        assert refs == {"organization": "neso"}

    def test_an_explicit_parameter_beats_a_guess_from_id(self):
        refs = ATTRIBUTION.refs("resource_view_list", {"id": "view-uuid", "resource_id": "res-uuid"})

        assert refs["resource"] == "res-uuid"

    @pytest.mark.parametrize(
        "action, params",
        [
            ("package_search", {"q": "costs"}),
            ("package_list", {}),
            ("organization_list", {"all_fields": True}),
            ("user_autocomplete", {"q": "jane"}),
            ("datastore_search_sql", {"sql": "SELECT * FROM res-uuid"}),
            ("status_show", {}),
        ],
    )
    def test_calls_that_name_nothing_are_not_attributed(self, action, params):
        """Not by a rule about their names - they simply carry no reference."""
        assert ATTRIBUTION.refs(action, params) == {}


class FakeRedis:
    """Enough of redis.Redis for this cache, plus the ways it can misbehave."""

    def __init__(self, fail_get: bool = False, fail_setex: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.setex_calls: list[tuple[str, int, str]] = []
        self._fail_get = fail_get
        self._fail_setex = fail_setex

    def get(self, key):
        if self._fail_get:
            raise RuntimeError("Redis is unreachable")
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self._fail_setex:
            raise RuntimeError("Redis is read only")
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value



class FakeLookups:
    """Stands in for the database, and records what it was asked for."""

    def __init__(self, explode: bool = False, **answers) -> None:
        self.answers = answers
        self.explode = explode
        self.calls: list[tuple[str, str]] = []

    def _answer(self, kind, ref):
        self.calls.append((kind, ref))
        if self.explode:
            raise RuntimeError("no database here")
        return self.answers.get(kind, {})

    def resource(self, ref):
        return self._answer("resource", ref)

    def dataset(self, ref):
        return self._answer("dataset", ref)

    def organization(self, ref):
        return self._answer("organization", ref)

    def group(self, ref):
        return self._answer("group", ref)


class Uncached(EntityCache):
    """A cache with no Redis behind it."""

    def client(self):
        return None


DATASET_NAMES = {"dataset": "daily-balancing-costs", "organization": "neso"}


class TestEntityCache:
    """Reading and writing names, and every way Redis can let us down."""

    @pytest.fixture
    def redis(self) -> FakeRedis:
        return FakeRedis()

    @pytest.fixture
    def cache(self, redis) -> EntityCache:
        return EntityCache(client=redis)

    def test_a_key_is_namespaced_by_kind_and_reference(self, cache, redis):
        cache.set("dataset", "costs-2026", DATASET_NAMES)

        assert list(redis.store) == ["ckanext-analytics:entity:dataset:costs-2026"]

    def test_names_are_written_with_the_ttl(self, cache, redis):
        cache.set("dataset", "costs-2026", DATASET_NAMES)

        _, ttl, value = redis.setex_calls[0]
        assert ttl == TTL_SECONDS
        assert "daily-balancing-costs" in value

    def test_names_come_back_out(self, cache):
        cache.set("dataset", "costs-2026", DATASET_NAMES)

        assert cache.get("dataset", "costs-2026") == DATASET_NAMES

    def test_a_miss_is_none(self, cache):
        assert cache.get("dataset", "never-seen") is None

    def test_an_empty_answer_is_cached_and_is_not_a_miss(self, cache):
        """Distinguishing {} from None is what stops a bad id hitting the database."""
        cache.set("dataset", "does-not-exist", {})

        assert cache.get("dataset", "does-not-exist") == {}

    def test_two_kinds_do_not_collide_on_one_reference(self, cache):
        cache.set("group", "shared-name", {"group": "a group"})
        cache.set("organization", "shared-name", {"organization": "an org"})

        assert cache.get("group", "shared-name") == {"group": "a group"}

    def test_corrupt_content_reads_as_a_miss(self, cache, redis):
        redis.store["ckanext-analytics:entity:dataset:costs-2026"] = "not json"

        assert cache.get("dataset", "costs-2026") is None

    def test_a_redis_that_will_not_read_is_a_miss(self):
        cache = EntityCache(client=FakeRedis(fail_get=True))

        assert cache.get("dataset", "costs-2026") is None

    def test_a_redis_that_will_not_write_does_not_raise(self):
        cache = EntityCache(client=FakeRedis(fail_setex=True))

        cache.set("dataset", "costs-2026", DATASET_NAMES)  # must not raise

    def test_no_redis_at_all_is_simply_a_miss(self):
        cache = Uncached()

        cache.set("dataset", "costs-2026", DATASET_NAMES)

        assert cache.get("dataset", "costs-2026") is None



class TestEntityResolver:
    """The rules: what to look up, and what to walk upwards to."""

    def resolver_for(self, lookups, cache=None) -> EntityResolver:
        return EntityResolver(lookups=lookups, cache=cache or EntityCache(client=FakeRedis()))

    def test_a_uuid_becomes_a_name(self):
        """The whole point: callers send uuids, reports need names."""
        lookups = FakeLookups(dataset=DATASET_NAMES)

        resolved = self.resolver_for(lookups).resolve({"dataset": "3f9c-4a11-uuid"})

        assert resolved["dataset"] == "daily-balancing-costs"
        assert resolved["organization"] == "neso"

    def test_a_resource_brings_its_dataset_and_organization(self):
        lookups = FakeLookups(
            resource={
                "resource": "costs-2026.csv",
                "dataset": "daily-balancing-costs",
                "organization": "neso",
            }
        )

        assert self.resolver_for(lookups).resolve({"resource": "res-uuid"}) == {
            "dataset": "daily-balancing-costs",
            "resource": "costs-2026.csv",
            "organization": "neso",
            "group": None,
        }

    def test_one_lookup_is_enough_for_a_whole_chain(self):
        lookups = FakeLookups(
            resource={"resource": "costs-2026.csv", "dataset": "d", "organization": "neso"}
        )

        self.resolver_for(lookups).resolve({"resource": "res-uuid"})

        assert lookups.calls == [("resource", "res-uuid")]

    def test_an_organization_is_named(self):
        lookups = FakeLookups(organization={"organization": "neso"})

        resolved = self.resolver_for(lookups).resolve({"organization": "org-uuid"})

        assert resolved["organization"] == "neso"

    def test_a_group_is_named(self):
        lookups = FakeLookups(group={"group": "energy-data"})

        assert self.resolver_for(lookups).resolve({"group": "grp-uuid"})["group"] == (
            "energy-data"
        )

    def test_one_call_can_resolve_two_entities(self):
        lookups = FakeLookups(
            organization={"organization": "neso"}, group={"group": "energy-data"}
        )

        resolved = self.resolver_for(lookups).resolve(
            {"organization": "org-uuid", "group": "grp-uuid"}
        )

        assert resolved["organization"] == "neso"
        assert resolved["group"] == "energy-data"

    def test_a_repeat_reference_is_served_from_the_cache(self):
        lookups = FakeLookups(dataset=DATASET_NAMES)
        resolver = self.resolver_for(lookups)

        resolver.resolve({"dataset": "costs-2026"})
        resolver.resolve({"dataset": "costs-2026"})

        assert lookups.calls == [("dataset", "costs-2026")]

    def test_another_process_shares_the_cache(self):
        """Why Redis and not memory: one warm cache, not one per worker."""
        cache = EntityCache(client=FakeRedis())
        first_lookups = FakeLookups(dataset=DATASET_NAMES)
        second_lookups = FakeLookups(dataset=DATASET_NAMES)

        self.resolver_for(first_lookups, cache).resolve({"dataset": "costs-2026"})
        resolved = self.resolver_for(second_lookups, cache).resolve({"dataset": "costs-2026"})

        assert first_lookups.calls == [("dataset", "costs-2026")]
        assert second_lookups.calls == []
        assert resolved["dataset"] == "daily-balancing-costs"

    def test_a_reference_that_resolves_to_nothing_is_cached_too(self):
        """Otherwise a bad id in a loop reaches the database every single time."""
        lookups = FakeLookups()  # answers nothing for anything
        resolver = self.resolver_for(lookups)

        resolver.resolve({"dataset": "does-not-exist"})
        resolved = resolver.resolve({"dataset": "does-not-exist"})

        assert lookups.calls == [("dataset", "does-not-exist")]
        assert resolved["dataset"] == "does-not-exist"

    def test_without_a_cache_it_looks_up_every_time_rather_than_failing(self):
        lookups = FakeLookups(dataset=DATASET_NAMES)
        resolver = self.resolver_for(lookups, Uncached())

        first = resolver.resolve({"dataset": "costs-2026"})
        resolver.resolve({"dataset": "costs-2026"})

        assert first["dataset"] == "daily-balancing-costs"
        assert len(lookups.calls) == 2

    def test_a_failing_lookup_leaves_the_reference_as_sent(self):
        resolver = self.resolver_for(FakeLookups(explode=True))

        resolved = resolver.resolve({"dataset": "costs-2026"})

        assert resolved["dataset"] == "costs-2026"
        assert resolved["organization"] is None

    def test_a_failing_lookup_is_not_cached(self):
        """A database that was briefly unwell must not blank the field for the TTL."""
        lookups = FakeLookups(explode=True)
        cache = EntityCache(client=FakeRedis())
        resolver = self.resolver_for(lookups, cache)

        resolver.resolve({"dataset": "costs-2026"})
        lookups.explode = False
        lookups.answers = {"dataset": DATASET_NAMES}
        resolved = resolver.resolve({"dataset": "costs-2026"})

        assert resolved["dataset"] == "daily-balancing-costs"
