"""Register API analytics on CKAN's request-finished signal."""
from __future__ import annotations

from typing import Any

import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit

from ckanext.analytics.event import record_request


class AnalyticsPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.ISignal)

    def get_signal_subscriptions(self) -> dict[Any, Any]:
        return {toolkit.signals.request_finished: [record_request]}
