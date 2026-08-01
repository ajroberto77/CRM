"""The one generic REST surface for every registered entity (R4).

There is no `api/persons.py`, no `api/deals.py`, no `api/funds.py`. Every
entity in `server/core/registry.py` — core's and every module's — is reachable
through exactly these routes, parameterized by `{entity}`:

    GET    /records                          registry metadata, readable entities only
    GET    /records/{entity}/schema          one entity's fields + custom fields
    GET    /records/{entity}                 list (querystring filter/sort/select)
    POST   /records/{entity}/query           list (JSON body — for filter trees too
                                              large or complex for a querystring)
    GET    /records/{entity}/{id}            one record
    POST   /records/{entity}                 create
    PATCH  /records/{entity}/{id}            update
    DELETE /records/{entity}/{id}             delete
    GET    /records/{entity}/{id}/related    associations, grouped and hydrated

    POST   /associations                     create a relationship
    POST   /associations/{id}/end            end one (dated, preserved)
    DELETE /associations/{id}                delete one outright

`modules/funds` proved this at the repository layer (`test_vertical_funds.py`):
a module entity gets full CRUD with no new route. This file is where that
promise becomes reachable over HTTP — adding `fund`/`commitment` here required
zero lines changed, because the router is driven by the registry, not a
per-entity switch.

## Errors

`repository`/`registry`/`query`/`associations` raise typed exceptions; they are
mapped to HTTP status once, via exception handlers registered on the app in
`server/api/app.py`, not repeated in every route body here.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from server.api.auth import current_principal
from server.core import associations, permissions, registry, repository
from server.core.permissions import Principal

router = APIRouter(prefix="/records", tags=["records"])
association_router = APIRouter(prefix="/associations", tags=["associations"])


# ── Request bodies ───────────────────────────────────────────────────────────
# Create has no model: the body IS the field values, and the shape varies per
# entity — that variability is the entire point of a generic router, so a
# fixed pydantic model here would just be the per-entity route this file
# exists to avoid, one level down.

class UpdateBody(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)
    if_unmodified_since: Optional[str] = None


class QueryBody(BaseModel):
    filters: Optional[dict[str, Any]] = None
    sort: Optional[list[Any]] = None
    select: Optional[list[str]] = None
    limit: Optional[int] = None
    offset: int = 0


class AssociateBody(BaseModel):
    role: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None


class EndAssociationBody(BaseModel):
    on: Optional[str] = None


def _parse_json_param(name: str, raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"{name} is not valid JSON"
        ) from exc


# ── Metadata ─────────────────────────────────────────────────────────────────

@router.get("")
def list_entities(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Every entity this principal can at least read — what the sidebar/nav is
    built from. Not a security boundary on its own (actual access is enforced
    per-request regardless), just a UI that never dangles a link to nothing."""
    entities = []
    for spec in registry.all_entities():
        if not principal.for_object(spec.name).allows("read"):
            continue
        entities.append({
            "name": spec.name, "label": spec.label or spec.name,
            "label_field": spec.label_field, "admin_only": spec.admin_only,
            "module": spec.module,
        })
    return {"entities": entities}


@router.get("/{entity}/schema")
def entity_schema(
    entity: str, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """Field metadata for one entity, masked fields already excluded — the
    frontend builds its table columns, filter menu and create/edit form
    entirely from this, never from a hardcoded per-entity component."""
    spec = registry.entity(entity)  # UnknownEntity -> 404 via the app handler
    perms = principal.require("read", entity)

    fields = {}
    for name, fspec in spec.fields.items():
        if not perms.readable_field(name):
            continue
        fields[name] = {
            "kind": fspec.kind, "filterable": fspec.filterable,
            "sortable": fspec.sortable,
            "writable": fspec.writable and perms.writable_field(name),
            "required": fspec.required, "options": list(fspec.options),
        }

    custom_fields = []
    if spec.supports_custom_fields:
        for row in repository.custom_field_defs(principal, entity):
            ref = f"custom.{row['key']}"
            if not perms.readable_field(ref):
                continue
            custom_fields.append({
                "key": row["key"], "kind": row["kind"], "label": row["label"],
                "options": row["options"], "indexed": row["indexed"],
                "writable": perms.writable_field(ref),
            })

    return {
        "name": spec.name, "label": spec.label or spec.name,
        "label_field": spec.label_field,
        "default_sort": [{"field": f, "direction": d} for f, d in spec.default_sort],
        "searchable": list(spec.searchable),
        "supports_custom_fields": spec.supports_custom_fields,
        "admin_only": spec.admin_only,
        "fields": fields, "custom_fields": custom_fields,
        "can_create": perms.can_create,
        "read_level": perms.read_level, "edit_level": perms.edit_level,
        "delete_level": perms.delete_level,
    }


@router.get("/{entity}/roles")
def entity_roles(
    entity: str, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """Every association role `entity` can take part in, from either side —
    what a "link record" control builds its role picker from, so a new role
    (registered by any module) is reachable with no frontend change (R4)."""
    registry.entity(entity)  # UnknownEntity -> 404 via the app handler
    principal.require("read", entity)

    available = []
    for role_spec in registry.roles():
        if entity in role_spec.from_types:
            available.append({
                "role": role_spec.name, "direction": "from",
                "label": role_spec.name.replace("_", " "),
                "target_types": list(role_spec.to_types),
            })
        if entity in role_spec.to_types and not role_spec.symmetric:
            available.append({
                "role": role_spec.name, "direction": "to",
                "label": (role_spec.inverse_label or role_spec.name).replace("_", " "),
                "target_types": list(role_spec.from_types),
            })
    return {"roles": available}


# ── Read ─────────────────────────────────────────────────────────────────────

@router.get("/{entity}")
def list_records(
    entity: str,
    filter: Optional[str] = Query(default=None, description="JSON filter tree"),
    sort: Optional[str] = Query(default=None, description="JSON sort list"),
    select: Optional[str] = Query(default=None, description="JSON field-name list"),
    limit: Optional[int] = Query(default=None),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return repository.list_records(
        principal, entity,
        filters=_parse_json_param("filter", filter),
        sort=_parse_json_param("sort", sort),
        select=_parse_json_param("select", select),
        limit=limit, offset=offset,
    )


@router.post("/{entity}/query")
def query_records(
    entity: str, body: QueryBody, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """Same semantics as `GET /records/{entity}`, body-based — for a filter
    tree too large or complex to comfortably URL-encode, and what a saved
    view's stored filters are replayed through (recompiled and
    re-permission-checked by the executing user on every call, never cached
    as SQL — see `core.saved_views` in `docs/DESIGN.md`)."""
    return repository.list_records(
        principal, entity, filters=body.filters, sort=body.sort,
        select=body.select, limit=body.limit, offset=body.offset,
    )


@router.get("/{entity}/{record_id}")
def get_record(
    entity: str, record_id: str, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    return {"record": repository.get_record(principal, entity, record_id)}


@router.get("/{entity}/{record_id}/related")
def related_records(
    entity: str, record_id: str,
    roles: Optional[str] = Query(default=None, description="JSON list of role names"),
    as_of: Optional[str] = Query(default=None),
    include_history: bool = Query(default=False),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    role_list = _parse_json_param("roles", roles)
    blocks = associations.related_blocks(
        principal, entity, record_id,
        roles=role_list, as_of=as_of, include_history=include_history,
    )
    return {"related": blocks}


# ── Write ────────────────────────────────────────────────────────────────────

@router.post("/{entity}", status_code=status.HTTP_201_CREATED)
def create_record(
    entity: str, body: dict[str, Any], principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    return {"record": repository.create(principal, entity, body)}


@router.patch("/{entity}/{record_id}")
def update_record(
    entity: str, record_id: str, body: UpdateBody,
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    record = repository.update(
        principal, entity, record_id, body.changes,
        clear=body.clear, if_unmodified_since=body.if_unmodified_since,
    )
    return {"record": record}


@router.delete("/{entity}/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_record(
    entity: str, record_id: str, principal: Principal = Depends(current_principal)
) -> None:
    repository.delete(principal, entity, record_id)


# ── Associations ─────────────────────────────────────────────────────────────
# Not registered entities (core.associations has no owner_id -- see
# server/core/associations.py) so they get their own small route group rather
# than /records/association, which would imply CRUD semantics that do not
# apply the same way here.

@association_router.post("", status_code=status.HTTP_201_CREATED)
def create_association(
    body: AssociateBody, principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    edge = associations.associate(
        principal, role=body.role, from_type=body.from_type, from_id=body.from_id,
        to_type=body.to_type, to_id=body.to_id, attributes=body.attributes,
        valid_from=body.valid_from, valid_to=body.valid_to,
    )
    return {"association": edge}


@association_router.post("/{association_id}/end")
def end_association(
    association_id: str, body: EndAssociationBody,
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    edge = associations.end_association(principal, association_id, on=body.on)
    return {"association": edge}


@association_router.delete("/{association_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_association(
    association_id: str, principal: Principal = Depends(current_principal)
) -> None:
    associations.dissociate(principal, association_id)
