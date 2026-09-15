# Saved Searches

Saved searches are matter-scoped Core records that preserve a controlled search definition for later execution. They do not store document IDs or cache a result set.

## Data model

`matter_saved_search` owns the saved definition:

- `matter_id` provides the authorization and data boundary.
- `owner_user_id` identifies the user who may change or delete the record.
- `name` and `normalized_name` provide a user-facing label and a case-insensitive uniqueness key per matter and owner.
- `description` is optional supporting text.
- `visibility` is `PRIVATE`, `PUBLIC`, or `SHARED`.
- `search_definition` is the JSON representation of `MatterSearchRequest`.
- `created_at` and `updated_at` support ordering and auditing.

`matter_saved_search_user_share` contains explicit user grants for `SHARED` searches. Keeping grants outside the search row allows a future `matter_saved_search_group_share` relation to be added without changing the search-definition format. Group sharing is not implemented yet.

## Visibility and authorization

Every operation first requires ordinary access to the saved search's matter.

- `PRIVATE`: owner only.
- `PUBLIC`: any user authorized to access the matter.
- `SHARED`: owner plus explicitly selected active users in the matter tenant who are authorized to access the matter.

Only the owner may update or delete a saved search. An inaccessible search is returned as not found so the API does not disclose private search identifiers. These rules are enforced by the FastAPI service; frontend controls are only a presentation convenience.

## Save and execution behavior

The API accepts the same typed request used by matter search, including query text, search mode, searchable fields, filters, facets, sort, and page size. It resets `offset` to zero before storing the request.

Execution deserializes the stored definition, optionally applies an offset and page-size override, and passes it through the ordinary matter-search command. The request therefore receives the same authorization scoping, validation, embedding behavior, and OpenSearch execution as an unsaved query. Because execution uses the current active index, results can change after ingestion, coding, reindexing, or other corpus changes.

## API

All routes are below `/v1/matters/{matter_id}/saved-searches`:

- `GET /` lists searches accessible to the caller.
- `POST /` creates a saved search owned by the caller.
- `GET /{saved_search_id}` returns one accessible saved search.
- `PUT /{saved_search_id}` replaces an owner-controlled saved search definition and its shares.
- `DELETE /{saved_search_id}` deletes an owner-controlled saved search.
- `POST /{saved_search_id}/execute` re-executes the stored request with optional pagination overrides.

Create, update, and delete actions are written to the Core audit log. Search execution remains read-only and is not audited as a configuration change.
