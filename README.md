# ckanext-analytics

Tracks CKAN Action API usage and resource downloads with the entities each
request touches. Every `/api/{version}/action/{action}` request and every
resource download produces one JSON event on the `ckanext.analytics.event`
logger.

## Event fields

Each event contains request metadata (`timestamp`, `request_id`, `service`,
`method`, `action_type`, `status_code`, `user_agent`, `request_ip`, and
`user`) plus these entity names when they can be resolved:

- `dataset`
- `resource`
- `organization`
- `group`

Downloads are recorded with `action_type` set to `resource_download`,
attributed to the resource and dataset in the URL - including redirects to
externally linked resources (302) and refused attempts. Failed API calls are
recorded with their final HTTP status. Web pages and static files are not
tracked.

## Installation

```sh
pip install -e .
```

Enable the plugin in the CKAN configuration:

```ini
ckan.plugins = ... analytics
```

## How it works

The plugin subscribes one listener to CKAN's `request_finished` signal. The
listener filters Action API and resource download requests, extracts entity
references from request parameters, resolves their names through CKAN's
model, and writes the event.
Entity lookup results are cached in each CKAN process for five minutes.

## Tests

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -o addopts='' \
  ckanext/analytics/tests
```

## License

AGPL-3.0-or-later
