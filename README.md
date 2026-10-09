# VintaSend Templates API

REST API that exposes a
[`ManagedTemplateService`](https://github.com/vintasoftware/vintasend-managed-templates)
over HTTP, built with Django and [django-ninja](https://django-ninja.dev/).

It exists so a template-management UI does not have to embed a template store: the UI
becomes a pure API client, and any implementation of this contract can serve it. It is
the templates-side sibling of
[`vintasend-api`](https://github.com/vintasoftware/vintasend-api), which does the same
for notifications, and follows the same conventions — bearer-token auth, a `{ data }` /
`{ error }` envelope, 1-indexed pages, camelCase on the wire.

**[`openapi.yaml`](./openapi.yaml) is generated, not maintained.** It is produced from
the route declarations in
[`api.py`](./vintasend_templates_management_api/templates_manager/api.py) and the schemas in
[`contract.py`](./vintasend_templates_management_api/templates_manager/contract.py) and
[`query.py`](./vintasend_templates_management_api/templates_manager/query.py) by
`manage.py export_openapi`. A test asserts the committed copy matches the live routes, so
a change that was not regenerated fails the suite rather than shipping a stale contract.

```
┌─────────────────────┐   HTTPS + API key    ┌───────────────────────┐
│  Template mgmt UI   │ ───────────────────▶ │ vintasend-templates-  │
│  server-side only   │ ◀─────────────────── │ api (this project)    │
└─────────────────────┘     JSON contract    └───────────┬───────────┘
                                                         │
                                          ┌──────────────┴──────────────┐
                                          │  Your ManagedTemplateService│
                                          │  template manager backend + │
                                          │  managed template renderer  │
                                          └─────────────────────────────┘
```

The API owns everything that needs backend credentials — database access, template
rendering. The UI owns presentation and user authentication.

## Why Django, for a library with no web framework

The same reason as `vintasend-api`: the template store decides. The reference backend,
`vintasend-django-templates-manager`, persists templates through the Django ORM, and
reading them needs a Django app registry and connection handling — another framework's
process would have to boot a half-configured Django anyway.

Nothing is lost for non-Django deployments. `BaseTemplateManagerBackend` is a pluggable
seam, so a FastAPI application storing templates through SQLAlchemy is served by this
same API: point `MANAGED_TEMPLATE_SERVICE_FACTORY` at a factory that builds a
SQLAlchemy-backed service and the HTTP layer neither knows nor cares.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness probe (unauthenticated) |
| GET | `/api/v1/capabilities` | Filter capabilities of the configured backend |
| GET | `/api/v1/templates` | List template versions with filters and pagination |
| POST | `/api/v1/templates` | Create a template's first version |
| GET | `/api/v1/templates/{key}` | One version — the latest unless `?version=` is given |
| DELETE | `/api/v1/templates/{key}` | Delete one never-published version (the latest unless `?version=`); a published one is a 409 `CONFLICT` |
| GET | `/api/v1/templates/{key}/versions` | Every version, newest first |
| POST | `/api/v1/templates/{key}/versions` | Create a new version from the latest |
| GET | `/api/v1/templates/{key}/versions/{version}` | One pinned version |
| DELETE | `/api/v1/templates/{key}/versions/{version}` | Delete one pinned, never-published version; a published one is a 409 `CONFLICT` |
| GET | `/api/v1/templates/{key}/composition` | One version assembled — what the engine actually receives |
| GET | `/api/v1/templates/{key}/status-history` | Status audit trail, most recent first |
| POST | `/api/v1/templates/{key}/status` | Move a version to an explicitly named status |
| POST | `/api/v1/templates/{key}/activate` | Publish a version |
| POST | `/api/v1/templates/{key}/deactivate` | Retire a version without archiving it |
| POST | `/api/v1/templates/{key}/archive` | Archive a version (terminal by default) |
| POST | `/api/v1/templates/{key}/preview` | Render a version against a supplied context |
| PUT | `/api/v1/templates/{key}/tags` | Replace one version's tags, in place |
| GET | `/api/v1/tags` | List tags with filters and pagination |
| POST | `/api/v1/tags` | Create a tag ahead of any template using it |
| GET | `/api/v1/tags/{slug}` | One tag |
| PATCH | `/api/v1/tags/{slug}` | Rename a tag (its slug is regenerated) |
| DELETE | `/api/v1/tags/{slug}` | Delete a tag, stripping it from every template |
| POST | `/api/v1/tags/{slug}/archive` | Retire a tag from the pickers |
| POST | `/api/v1/tags/{slug}/restore` | Put an archived tag back on offer |

A browsable version of the generated schema is served at `/api/v1/docs`.

Conventions a client can rely on:

- `page` is **1-indexed**.
- `hasMore` is `true` when the next page has at least one row, so a list that exactly fills
  its last page never offers an empty one. The template-manager seam has no count method,
  so no total is available: after a full page the API reads the one row that would follow
  it.
- **Request bodies are JSON.** A request that declares `application/json` (or any
  `application/*+json`) must carry valid JSON, so an empty body there is a 400. A request
  that declares no media type, or another one, counts as an omitted body when its body is
  empty, which is how an all-optional body (`activate`, `deactivate`, `archive`, `preview`,
  `POST /templates/{key}/versions`) is left out. Anything else is a 400 rather than a guess.
  `curl -d` sends form encoding unless told otherwise, so pass
  `-H 'Content-Type: application/json'`.
- **`GET /templates` lists one row per key by default.** A row in the store is a *version*,
  so the raw read returns a key once per version it has ever had. `mostRecentActiveVersion`
  defaults to `true` and narrows that to each key's current version — the highest-numbered
  `active` or `draft` one. Send `mostRecentActiveVersion=false` to list every version.
- **`version` omitted means "the latest"** everywhere except `status-history`, where it
  means "every version" — that endpoint forwards it to the backend, which returns the
  whole key's trail.
- **`GET /templates` orders only when asked, and only by what the backend can sort.**
  `orderByField` takes `key`, `name`, `version`, `status`, `createdAt` or `updatedAt`;
  `orderByDirection` takes `asc` or `desc` and defaults to `asc`. Neither has a default
  field, so omitting them asks for the backend's own order. See
  [Ordering](#ordering-the-listing).
- Every template payload carries `isAbstract`: whether that version is a base to build on rather than one to send. See [Composition](#composition-templates-built-on-templates).
- Every template payload carries `allowedTransitions`: the statuses that version can move
  to right now, as the *configured service* answers it. A UI enables buttons from that
  rather than reimplementing the lifecycle or discovering it by catching a 409.
- **A send renders the newest `active` version, never a draft.** Reads here are the editing
  view: `version` omitted returns the newest version whatever its status, and a preview with no
  `version` renders that. When several versions are active, an unpinned send renders the
  highest-numbered one, so activating an older version does not change what is sent.
- **Only a never-published version can be deleted.** A version that was ever activated is
  refused with a 409 `CONFLICT`: a notification may be pinned to it, and its status history
  records who published it. Archive it instead. That applies to `DELETE /templates/{key}` with no
  `version` too, which resolves to the latest version, so prefer naming the version you mean.
  Status history is never deleted.
- **`changedBy` in a status body is only a fallback.** A host that knows who is calling sets
  `MANAGED_TEMPLATE_ACTOR_RESOLVER` (see [Attribution](#attribution)), and its answer replaces
  whatever the body says.
- Timestamps are ISO-8601 UTC strings, `null` when unset — never absent.
- Errors always use the envelope `{ "error": { "code", "message", "details"? } }`.

### Tags

Tags label template versions so they can be found. A template carries any number of tags
and a tag is carried by any number of templates.

- **The slug is the identity.** It is normalized from the text — lowercased, accents
  folded, everything else collapsed to `-` — and is unique store-wide, so `Black Friday`,
  `black friday` and `BLACK-FRIDAY` are one tag. Store the `slug`, not the `text`.
- **Anywhere a tag is named, its text works too.** `GET /tags/Black%20Friday` and
  `GET /tags/black-friday` are the same lookup, and the same holds for the filters.
- **Tags are created on the fly.** Send `tags` on `POST /templates` or
  `POST /templates/{key}/versions` and any tag that does not exist yet is created. Use
  `POST /tags` only when a collision should be reported rather than resolved — it is a
  409 there and silent everywhere else.
- **Renaming changes the slug.** `PATCH /tags/{slug}` regenerates it, so a stored filter
  naming the old slug stops matching. Read the new `slug` off the response. Renaming onto
  another tag's text is allowed and yields `-2`: two tags may read the same, and the slug
  is what tells them apart.
- **Archive hides the tag; delete removes the label.** An archived tag drops out of
  `GET /tags?status=active` but stays on the templates carrying it, and filtering by it
  still finds them. Deleting is what strips the label, and it is not reversible.
- **Searching by tag** uses `includesAllTags` (carries *every* tag listed) or
  `includesAnyOfTags` (carries *at least one*). Repeat the parameter per tag. Sending both
  combines them with AND, like every other filter pair. A template matching several of the
  tags listed is returned once, not once per match.

  ```
  GET /api/v1/templates?includesAllTags=billing&includesAllTags=urgent
  GET /api/v1/templates?includesAnyOfTags=billing&includesAnyOfTags=marketing
  ```
- **Tags belong to a version, not a key**, so two versions of one template can be labelled
  differently — a draft can be tagged for review without relabelling what is live.

### Versions in the listing

The store holds a row per version, and a template-management UI almost always wants a list of
*templates*. `GET /api/v1/templates` therefore defaults to `mostRecentActiveVersion=true`:

```
GET /api/v1/templates                                # one row per key — the current version
GET /api/v1/templates?mostRecentActiveVersion=false  # every version of every key
GET /api/v1/templates/{key}/versions                 # every version of one key, newest first
```

Current means the **highest-numbered `active` or `draft` version**: what is published, plus the
draft on its way to replacing it. A key whose versions are all `inactive` or `archived` has no
current version and does not appear in the default listing — ask for
`mostRecentActiveVersion=false` to see it.

A `status` filter applies on top of the default, so `?status=archived` on its own finds
nothing: the one row kept per key is never `inactive` or `archived`. Send both:

```
GET /api/v1/templates?status=archived&mostRecentActiveVersion=false
```

`false` lifts the restriction rather than inverting it: it lists everything, not only the rows
the default hides. The complement is a legitimate query in the library's filter vocabulary
(`{"most_recent_active_version": False}`) but there is no wire parameter for it, because no UI
has asked to list *only* superseded versions.

The narrowing happens in the store, so pagination still counts what the backend counted. A
backend declining `fields.mostRecentActiveVersion` has the filter dropped like any other
unsupported one, and its listing shows every version.

### Ordering the listing

```
GET /api/v1/templates?orderByField=name&orderByDirection=asc
```

`orderByField` accepts `key`, `name`, `version`, `status`, `createdAt` and `updatedAt` —
each a scalar the backend already stores per row, so a store can answer it from an index.
Tags are absent because ordering by a many-to-many has no single value to compare, and
`mostRecentActiveVersion` because it is a filter rather than a field.

`orderByDirection` is `asc` or `desc`, and defaults to `asc` when a field is given without
one. Sent on its own it is a `400`: there is nothing to order by, and ignoring it would
look exactly like a backend that cannot sort.

**Ask `/capabilities` first.** Every `orderBy.*` key defaults to `false`, so a backend that
has not declared a field cannot sort by it and the request is a `400` naming the key:

```jsonc
// GET /api/v1/capabilities
{
  "data": {
    "orderBy.key": true,
    "orderBy.name": true,
    "orderBy.version": true,
    "orderBy.status": false,   // this backend cannot sort by status
    "orderBy.createdAt": true,
    "orderBy.updatedAt": true
    // ... plus the fields.*, logical.* and stringLookups.* keys
  }
}
```

Build the sortable columns of a UI from that report and the `400` never happens. Unlike an
unsupported *filter*, which is dropped so the request still succeeds, an unsupported
*order* is refused — see [Design notes](#design-notes) for why the two differ.

The order is applied by the backend to the whole result set before a page is chosen, so
page 2 of an ordered listing is the second page of that order rather than the backend's
own second page re-sorted.

### Composition: templates built on templates

A managed template can build on another one, and all of it is resolved **before** any
template engine runs — so the stored `bodyTemplate` is only half of what renders:

```
base-email    <html><body>
                {% managed_block header %}<h1>Acme</h1>{% managed_endblock %}
                {% managed_children %}
                {% managed_include "footer" %}
              </body></html>

welcome       {% managed_extends "base-email" %}
              {% managed_block header %}<h1>Welcome!</h1>{% managed_endblock %}
              <p>Hi {{ name }}, welcome aboard.</p>
```

The tag language belongs to
[`vintasend-managed-templates`](https://github.com/vintasoftware/vintasend-managed-templates#composition-bases-blocks-and-includes)
— `managed_extends`, `managed_children`, `managed_block` / `managed_endblock`,
`managed_super`, `managed_include`, with `version=2` to pin a reference. This API stores those templates as written and exposes what they assemble to.

**`GET /api/v1/templates/{key}/composition`** returns the assembled sources, plus what the
version directly references and whether it is abstract:

```
GET /api/v1/templates/welcome/composition          # the latest version
GET /api/v1/templates/welcome/composition?version=3

{ "data": {
    "key": "welcome",
    "version": 3,
    "isAbstract": false,
    "references": [
      { "kind": "extends", "key": "base-email", "version": null, "field": "bodyTemplate" }
    ],
    "composedBodyTemplate": "<html><body><h1>Welcome!</h1>...",
    "composedSubjectTemplate": null,
    "composedPreheaderTemplate": null
} }
```

Nothing there is rendered against a context: `{{ name }}` and every other engine tag
survives untouched. `POST /templates/{key}/preview` is what renders — and it composes
first, so a preview shows the base's chrome around the child's content, exactly as a real
send would.

A template that cannot be assembled — a base that does not exist, a chain that loops, a
malformed tag — is a **409 `TEMPLATE_COMPOSITION_ERROR`** carrying the library's message,
which names the reference chain that broke. It is a 409 and not a 404 because the template
asked for is there; what it names is not.

Previewing tells the two ways a template can be broken apart by code, so a UI knows whether
to send the editor to the chain or to the template:

- A template that cannot be assembled is the same **409 `TEMPLATE_COMPOSITION_ERROR`** the
  composition endpoint gives.
- A template that assembles but fails to render is a **409 `PREVIEW_UNAVAILABLE`** carrying
  the renderer's message.
- A store that fails while assembling is a plain **500 `INTERNAL_ERROR`**: its message stays
  on the server, and the error goes to the unhandled-error hook.

Two more things worth knowing:

- **Each of the three sources composes against the same field of what it references.** A
  child's body extends the base's body, its subject extends the base's subject. `field` on
  every reference says which one it was written in.
- **References are direct only.** What the referenced templates themselves reference is not
  followed, and nothing is resolved — a reference to a template that does not exist is
  reported here rather than raising.

#### `isAbstract`

Every template payload carries `isAbstract`: `true` when the version is a base to build on
rather than a template to send — it declares a `{% managed_children %}` hole, or blocks
without extending anything.

```
GET /api/v1/templates?isAbstract=false   # what a "pick a template to send" screen lists
GET /api/v1/templates?isAbstract=true    # the bases, for a "pick a base to extend" screen
GET /api/v1/templates                    # both
```

On a listing it is read from the backend's stored flag, so filtering is a column lookup
rather than a parse of every row, and serializing a page costs nothing. The composition
endpoint recomputes it from the source instead, which makes that copy the authority — worth
asking for when the flag cannot be trusted, such as a backend that predates it.

A backend declining `fields.isAbstract` has the filter dropped like any other unsupported
one, and its listing shows both.

### Error codes

| Code | Status | Means |
| --- | --- | --- |
| `BAD_REQUEST` | 400 | Invalid input. See below. |
| `UNAUTHORIZED` | 401 | Missing or wrong API key. |
| `FORBIDDEN` | 403 | The caller was authenticated and is not allowed to do this. Declared on every route for hosts that check permissions in front of this API; the API key alone never produces it. |
| `NOT_FOUND` | 404 | No such template key, or no such version of it. |
| `CONFLICT` | 409 | The request cannot be applied in the current state: a tag whose text already slugs onto an existing one, or deleting a published version. |
| `INVALID_STATUS_TRANSITION` | 409 | The lifecycle does not allow that status change. |
| `PREVIEW_UNAVAILABLE` | 409 | The template could not be rendered; the message says why. |
| `TEMPLATE_COMPOSITION_ERROR` | 409 | The template could not be assembled — a missing base, a loop, a malformed tag. The message names the chain. |
| `INTERNAL_ERROR` | 500 | Unexpected failure. Reported generically, with an `X-Request-Id` header; logged as one redacted line. |

Every 400 carries `details.issues`, a list of `{ "path", "message" }`, whatever the mistake
was, so a client reads one shape:

| Mistake | `path` |
| --- | --- |
| An invalid query parameter or body field | the field, dotted when nested |
| A version in the path that is not a positive integer (`/versions/1abc`) | `version` |
| A body that is not valid JSON, not a JSON object, or not sent as JSON | empty |
| An order the backend cannot apply | `orderByField` / `orderByDirection` |
| A value the backend refused, such as tag text with nothing sluggable in it | empty, repeating the message |

An unexpected error is logged as one line: its class name, the request id, the method and the
route pattern (`api/v1/templates/<key>/preview`, not the path). Its message, its traceback, the
request body and a preview's context are never logged: errors from the template store or the
template engine can carry template content and context values, which in the applications this
API serves can be health data. Django's own `django.request` record for the 500 is suppressed
too, since it would repeat the concrete path and attach the request object. The request id is
the client's `X-Request-Id` when it matches `[A-Za-z0-9._-]{1,128}`, and a fresh UUID otherwise,
so a client cannot forge a log line through it. Set `MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER` to send errors somewhere with its own
scrubbing instead (see [Unexpected errors](#unexpected-errors)).

## Authentication

Every `/api/v1` request must carry the shared secret:

```
Authorization: Bearer $VINTASEND_API_KEY
```

Call this API from your UI's own server side so the key never reaches a browser. If you
do need to call it from a browser, set `VINTASEND_API_CORS_ORIGINS` to the allowed
origins — and put a per-user auth layer in front of it first.

## Getting started

```bash
poetry install
cp .env.example .env
```

Then configure the service the API should read from (below), and run:

```bash
poetry run python manage.py runserver 0.0.0.0:3334
```

## Configuring your service

The API ships no template store of its own. Point `MANAGED_TEMPLATE_SERVICE_FACTORY` at a
callable that returns a configured service:

```python
# vintasend_templates_management_api/vintasend_config.py
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateEmailRenderer
from vintasend_managed_templates.managed_template_service import ManagedTemplateService


def create_template_service():
    backend = ...   # your BaseTemplateManagerBackend
    renderer = ManagedTemplateEmailRenderer(backend, ...)  # wraps an ordinary renderer

    return ManagedTemplateService(
        template_manager_backend=backend,
        template_renderer=renderer,
    )
```

Start from
[`vintasend_config.example.py`](./vintasend_templates_management_api/vintasend_config.example.py),
copying it to `vintasend_templates_management_api/vintasend_config.py` (gitignored). The factory is
called once per process and its result reused, so it must be safe to call once and the
service it returns must be safe to share across requests.

Only the preview endpoint uses the renderer, so a deployment that never previews can pass
one that raises. A preview never uses a fallback registered on the renderer: it renders a stored
version or reports a 404.

### Attribution

Status routes record a `changedBy` in the audit trail. By default it comes from the request
body, which is only safe when everyone holding the API key is trusted to attribute honestly.
When the host knows who is calling (a gateway header, a session it validated), point
`MANAGED_TEMPLATE_ACTOR_RESOLVER` at a callable that answers it:

```python
# vintasend_templates_management_api/vintasend_config.py
def resolve_actor(request) -> str | None:
    return request.headers.get("X-Authenticated-User")   # set by your trusted proxy
```

```bash
MANAGED_TEMPLATE_ACTOR_RESOLVER=vintasend_templates_management_api.vintasend_config.resolve_actor
```

When it is set, its answer is what `/status`, `/activate`, `/deactivate` and `/archive` all
record, and any `changedBy` in the body is ignored. `None` records the change as unattributed.
The resolver may be `async`. The body field stays optional in the contract, so existing clients
keep working. In `settings.py` you can also assign the callable itself rather than a dotted path.

### Unexpected errors

`MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER` names a callable `(exc, request, request_id) -> None`
that receives every error the API does not map to a contract error, in place of the default
one-line log. It may be `async`. It gets the exception itself, so keeping health data out of
wherever it sends it is your responsibility. If it raises, the default line is logged instead
and what it raised is not; the client gets the same generic 500 with the same `X-Request-Id`
either way.

```python
def report_unhandled_error(exc, request, request_id):
    error_tracker.capture(exc, tags={"request_id": request_id})   # with its own scrubbing
```

The lifecycle is your service's, not this API's. A `ManagedTemplateService` subclass with
its own `ALLOWED_STATUS_TRANSITIONS`, or one built with
`validate_status_transitions=False`, is reported accurately through `allowedTransitions`
and enforced through the same 409.

## Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `VINTASEND_API_KEY` | yes | Shared secret clients must send as a bearer token. |
| `MANAGED_TEMPLATE_SERVICE_FACTORY` | yes | Dotted path to the callable building your service. |
| `VINTASEND_API_CORS_ORIGINS` | no | Comma-separated browser origins allowed to call the API. |
| `MANAGED_TEMPLATE_ACTOR_RESOLVER` | no | Dotted path to `(request) -> str \| None`, who made a status change. See [Attribution](#attribution). |
| `MANAGED_TEMPLATE_UNHANDLED_ERROR_HANDLER` | no | Dotted path to `(exc, request, request_id) -> None`. See [Unexpected errors](#unexpected-errors). |
| `DJANGO_SECRET_KEY` | no | Django requires one; this API signs nothing. |
| `DJANGO_DEBUG` / `DJANGO_ALLOWED_HOSTS` / `DJANGO_LOG_LEVEL` | no | Standard Django knobs. |
| `DJANGO_DB_*` | no | Only needed by backends that resolve their models through Django. |

The first two are enforced by a Django system check, so a deployment missing either fails
on `manage.py check` and on `runserver` rather than on the first request. The two hooks are
checked the same way: a dotted path that does not import, or names something that is not
callable, fails the check. Run
`manage.py check` in your release step if you serve with gunicorn.

## Development

```bash
poetry run python manage.py runserver          # dev server
poetry run pytest                              # tests
poetry run ruff check .                        # lint
poetry run ruff format .                       # format
poetry run mypy                                # type-check
poetry run python manage.py export_openapi     # regenerate openapi.yaml
poetry run python manage.py export_openapi --check   # fail if it is stale
```

Tests drive the real Django application through the test client over a real
`ManagedTemplateService` composed of in-memory seams. Faking the *seams* rather than the
service is deliberate: version resolution, the transition table and filter validation are
the service's behaviour, and this API's job is to expose it faithfully — a fake service
would let a route drift from the thing it is meant to be exposing without the suite
noticing.

## Design notes

Worth knowing if you are implementing this contract elsewhere, or wondering why something
is missing.

**Filters are dropped; orders are refused.** Both are negotiated against the same
capability report, and they resolve in opposite directions. That is deliberate:

| | Unsupported filter | Unsupported order |
|---|---|---|
| What happens | dropped, request succeeds | `400 BAD_REQUEST` |
| If it were ignored | more rows than asked for | the same rows, arbitrary sequence |
| Can the client tell? | yes, from the rows | **no** |

A client rendering an unordered page under a highlighted "sorted by name" column header is
displaying a sort that never happened, and nothing in the response says so. Sending
`orderByDirection` without `orderByField` is a 400 for the same reason — ignoring it looks
exactly like a backend that cannot sort, which hides the client bug.

**Neither ordering parameter has a default.** Every `orderBy.*` capability defaults to
`false`, so a default field would make the ordinary listing a 400 against most backends.
Omitted asks for the backend's own order — which is what an unordered listing has always
returned. Note this differs from `vintasend-api`, which *does* default its order; there,
every notification backend can sort.

Read `GET /api/v1/capabilities` and offer only the columns it reports as sortable.

**Pagination needs no negotiation.** `vintasend-api` reads `pagination.oneIndexed` off
each backend because notification backends genuinely differ. Here `ManagedTemplateService`
validates `page >= 1` itself and no call reaches a backend without passing through that
validation, so the wire's 1-indexing *is* the service's convention.

**Capabilities are declared, not required.** `BaseTemplateManagerBackend.get_filter_capabilities`
is concrete and returns `{}`, so a backend that says nothing keeps working. As with
notifications, a backend declares only what it *cannot* do, and `ManagedTemplateService`
merges its report over the library's default.

The `orderBy.*` keys are the one exception to "a missing key means supported": they
default to `false`. Ordering is newer vocabulary than the filters, so a `true` default
would have every backend written before it existed claim an order it silently ignores. A
backend that can sort declares it — and should verify each claim by *running* the sort, not
by reading its store's documentation.

**Composition is reported, not enforced on write.** A template that extends a base which
does not exist yet is stored without complaint: a UI drafting a set of templates would
otherwise have to create them in dependency order, and a base can legitimately be written
after the child that names it. The cost is that a broken reference is found on read rather
than on save — which is why `GET /templates/{key}/composition` exists and returns the
library's message verbatim, and why a save-then-check round trip is the pattern for an
editor that wants to warn before publishing. Publishing is where it matters: a template
that cannot be assembled cannot be sent, so check before `POST /templates/{key}/activate`.

**Preview contexts are plain dicts.** The renderer seam types a context as
`NotificationContextDict`, but that class cannot represent an ordinary nested object: its
validation requires every value inside a nested `dict` to itself be a
`NotificationContextDict`, with no base case for a scalar, so `{"user": {"name": "Ana"}}`
is inexpressible. `None` values and lists of strings are refused for related reasons, and
both appear in real stored contexts. Since a notification's stored `context_used` is a
plain dict and renderers only read from a context, the caller's object is passed through —
the same thing `vintasend-api` does. A preview that refused what a real send carries would
be a worse guide to production than no preview.

**Updates create versions.** `POST /templates/{key}/versions` rather than a `PATCH`,
because the backend copies the latest version forward and applies the non-`None` fields to
the copy. An already-published version is never modified, and the route shape says so.

**Deletion is per version.** The seam has no operation removing every version of a key, so
neither does this API; doing it as a loop here would be a multi-step deletion with no
transaction around it.

**Retagging is the one write that does not create a version.** `PUT /templates/{key}/tags`
edits a version in place, unlike every other write on a template. Tags describe how a
template is *found*, not what it renders, so relabelling one for search should not fork a
version and drop it back to draft. To change tags and content together, send `tags` on
`POST /templates/{key}/versions` instead — where an omitted `tags` carries the previous
version's forward and `[]` clears them.

**Tag listing is paged in this API, not in the store.** The template-manager seam has no
paginated tag read, so `GET /tags` slices the page in process. That is sound in a way
in-process *ordering* would not be: `get_tags` returns the whole set in one stable order,
so a page is a slice of a complete list rather than a re-sort of an arbitrary window.

**`fields.mostRecentActiveVersion` is a capability of its own.** It is not a column test like
every other field: a backend answers it by comparing a row against the other versions of its
key, which a store keeping no version history cannot do. Declining it means the list endpoint
drops it and returns a row per version — the honest answer for a store that has only one.

**The two tag filters are separate capabilities.** `fields.includesAllTags` and
`fields.includesAnyOfTags` are different queries — the first needs a per-template count
over the tags asked for, the second only needs membership — so a backend can support one
without the other. As with every filter, a declined one is dropped rather than failing the
request.

## License

MIT
