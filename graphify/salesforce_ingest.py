"""Salesforce SFDX metadata (``*-meta.xml``) → graph structure.

Deterministic XML parse (stdlib ElementTree, no LLM); every edge is EXTRACTED.
SObject and field node IDs are global — ``make_id(object_api)`` and
``make_id(object_api, field_api)`` — so they converge with the SObject / field
stubs ``extract_apex`` mints from SOQL and trigger headers, wiring Apex code to
the schema it touches.

Handles: object (incl. legacy inline fields/rules); field (Lookup / MasterDetail
relationships); flow (record-trigger object + referenced objects);
validationRule; permissionset (field- and object-level grants). Relation
vocabulary: ``has_field``, ``looks_up_to``, ``master_detail_to``,
``triggers_on``, ``references``, ``validates``, ``grants_fls``,
``grants_access``.

Code-sidecar metadata (``.cls-meta.xml``, ``.trigger-meta.xml``,
``.js-meta.xml``) is intentionally not matched — the companion source file
already covers it.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from graphify.ids import make_id

SF_METADATA_SUFFIXES = (
    ".object-meta.xml",
    ".field-meta.xml",
    ".flow-meta.xml",
    ".validationRule-meta.xml",
    ".permissionset-meta.xml",
    ".recordType-meta.xml",
)

# Only DLRS rollup records are custom metadata worth parsing; other CMDT types
# (e.g. mapping tables) share the .md-meta.xml suffix and are ignored.
_DLRS_PREFIX = "dlrs__LookupRollupSummary2."
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def is_salesforce_metadata_path(path: Path) -> bool:
    """True for the SFDX metadata types :func:`extract_salesforce_metadata` parses.

    Matched by compound type suffix: these all share the bare ``.xml`` suffix,
    which is otherwise unclassified, so generic XML stays untouched."""
    name = Path(path).name
    if name.startswith(_DLRS_PREFIX) and name.endswith(".md-meta.xml"):
        return True
    return any(name.endswith(suffix) for suffix in SF_METADATA_SUFFIXES)


_FORMULA_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_FORMULA_STRING_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
_FORMULA_TOKEN_RE = re.compile(r"(?<![\w$.])([A-Za-z]\w*(?:\.[A-Za-z]\w*)*)(\s*\()?")
_REL_STUB_RE = re.compile(r"^(\w+)\.(\w+__r)\.(\w+__c)$", re.I)
_FORMULA_LITERALS = frozenset({"true", "false", "null"})


def _formula_refs(formula: str) -> list[tuple[str | None, str]]:
    """Custom-field references in a formula, as ``(relationship, field)`` pairs.

    ``relationship`` is None for a same-object field (``Region__c``) and the
    ``X__r`` segment for a one-hop reference (``Parent__r.Region__c``). Comments,
    string literals, functions, ``$Global`` variables and standard relationship
    paths (``RecordType.DeveloperName``) are skipped."""
    text = _FORMULA_STRING_RE.sub(" ", _FORMULA_COMMENT_RE.sub(" ", formula))
    refs: list[tuple[str | None, str]] = []
    for m in _FORMULA_TOKEN_RE.finditer(text):
        token, call = m.group(1), m.group(2)
        if call or token.lower() in _FORMULA_LITERALS:
            continue
        parts = token.split(".")
        if len(parts) == 1 and parts[0].lower().endswith("__c"):
            refs.append((None, parts[0]))
        elif len(parts) == 2 and parts[0].lower().endswith("__r") and parts[1].lower().endswith("__c"):
            refs.append((parts[0], parts[1]))
    return refs


_RECORD_REF_RE = re.compile(r"^\$Record(?:__Prior)?\.(\w+)$")


def _local(tag: str) -> str:
    # Strip the {namespace} prefix ElementTree prepends to every tag.
    return tag.rsplit("}", 1)[-1]


def _child_text(el, child_name: str) -> str | None:
    for c in el:
        if _local(c.tag) == child_name:
            return (c.text or "").strip() or None
    return None


def _find_all(el, child_name: str) -> list:
    return [c for c in el if _local(c.tag) == child_name]


def extract_salesforce_metadata(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        root = ET.fromstring(text)
    except (OSError, ET.ParseError):
        return {"nodes": [], "edges": []}

    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    def add_node(nid: str, label: str, stub: bool = False) -> None:
        # stub=True emits a reference node with no source_file (and no
        # origin_file), so the same object/field referenced from many files keeps
        # one shared id. The unique-stub rewire and resolve_salesforce_references
        # then fold it into the single real definition.
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid, "label": label, "file_type": "code",
                "source_file": "" if stub else str_path, "source_location": None,
            })

    def obj_ref(obj_api: str) -> str:
        nid = make_id(obj_api)
        add_node(nid, obj_api, stub=True)
        return nid

    def add_edge(src: str, tgt: str, relation: str, **attrs: Any) -> None:
        key = (src, tgt, relation)
        if not src or not tgt or key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({
            "source": src, "target": tgt, "relation": relation,
            "confidence": "EXTRACTED", "source_file": str_path,
            "source_location": None, "weight": 1.0, **attrs,
        })

    def field_node(obj_api: str, field_api: str, stub: bool) -> str:
        f_nid = make_id(obj_api, field_api)
        add_node(f_nid, f"{obj_api}.{field_api}", stub=stub)
        add_edge(obj_ref(obj_api), f_nid, "has_field")
        return f_nid

    def add_formula_refs(owner_nid: str, obj_api: str, formula: str | None) -> None:
        for rel, fld in _formula_refs(formula or ""):
            if rel is None:
                add_edge(owner_nid, field_node(obj_api, fld, stub=True), "reads_field")
            else:
                # One-hop `Rel__r.Field__c`: resolve_salesforce_references maps the
                # stub through the `Rel__c` lookup to the target object's field.
                nid = make_id(obj_api, rel, fld)
                add_node(nid, f"{obj_api}.{rel}.{fld}", stub=True)
                add_edge(owner_nid, nid, "reads_field")

    def process_field(field_el, obj_api: str, field_api: str | None = None) -> None:
        field_api = field_api or _child_text(field_el, "fullName")
        if not field_api:
            return
        f_nid = field_node(obj_api, field_api, stub=False)
        add_formula_refs(f_nid, obj_api, _child_text(field_el, "formula"))
        ftype = _child_text(field_el, "type") or ""
        if ftype in ("Lookup", "MasterDetail", "Hierarchy"):
            relation = "master_detail_to" if ftype == "MasterDetail" else "looks_up_to"
            for ref in _find_all(field_el, "referenceTo"):
                target = (ref.text or "").strip()
                if target:
                    add_edge(f_nid, obj_ref(target), relation)

    name = path.name

    def strip_suffix(suffix: str) -> str:
        return name[: -len(suffix)]

    # OBJECT: objects/<Obj>/<Obj>.object-meta.xml — the object's one real definition.
    if name.endswith(".object-meta.xml"):
        obj_api = strip_suffix(".object-meta.xml")
        obj_nid = make_id(obj_api)
        add_node(obj_nid, obj_api)
        # Decomposed SFDX keeps fields/rules in their own files; the legacy
        # combined format inlines them.
        for fld in _find_all(root, "fields"):
            process_field(fld, obj_api)
        for vr in _find_all(root, "validationRules"):
            vr_name = _child_text(vr, "fullName")
            if vr_name:
                vr_nid = make_id(obj_api, "vr", vr_name)
                add_node(vr_nid, f"{obj_api}.{vr_name}")
                add_edge(obj_nid, vr_nid, "validates")
                add_formula_refs(vr_nid, obj_api, _child_text(vr, "errorConditionFormula"))

    # FIELD: objects/<Obj>/fields/<Field>.field-meta.xml
    elif name.endswith(".field-meta.xml"):
        if path.parent.name == "fields":
            process_field(root, path.parent.parent.name, strip_suffix(".field-meta.xml"))

    # FLOW: flows/<Flow>.flow-meta.xml
    elif name.endswith(".flow-meta.xml"):
        flow_nid = make_id("flow", strip_suffix(".flow-meta.xml"))
        add_node(flow_nid, strip_suffix(".flow-meta.xml"))
        flow_node = nodes[-1] if nodes and nodes[-1]["id"] == flow_nid else None
        trigger_type = record_trigger_type = None
        start_obj: str | None = None
        triggered: set[str] = set()
        for start in _find_all(root, "start"):
            obj = _child_text(start, "object")
            trigger_type = _child_text(start, "triggerType")
            record_trigger_type = _child_text(start, "recordTriggerType")
            if obj:
                start_obj = obj
                o_nid = obj_ref(obj)
                add_edge(flow_nid, o_nid, "triggers_on",
                         trigger_type=trigger_type, record_trigger_type=record_trigger_type)
                triggered.add(o_nid)
        if flow_node is not None:
            flow_node["trigger_type"] = trigger_type
            flow_node["record_trigger_type"] = record_trigger_type
            flow_node["process_type"] = _child_text(root, "processType")

        # Field-level access. A record element names its object directly
        # (<object>), through a variable (<inputReference> -> the variable's
        # <objectType>), or implicitly as the triggering record ($Record).
        var_types = {
            _child_text(v, "name"): _child_text(v, "objectType")
            for v in _find_all(root, "variables") if _child_text(v, "objectType")
        }
        for kind in ("recordUpdates", "recordCreates", "recordLookups", "recordDeletes"):
            for el in _find_all(root, kind):
                obj = _child_text(el, "object")
                if not obj:
                    ref = _child_text(el, "inputReference")
                    obj = var_types.get(ref) if ref and ref != "$Record" else start_obj
                if not obj:
                    continue
                if kind in ("recordUpdates", "recordCreates"):
                    for ia in _find_all(el, "inputAssignments"):
                        field = _child_text(ia, "field")
                        if field:
                            add_edge(flow_nid, field_node(obj, field, stub=True), "writes_field")
                for flt in _find_all(el, "filters"):
                    field = _child_text(flt, "field")
                    if field:
                        add_edge(flow_nid, field_node(obj, field, stub=True), "reads_field")
                for qf in _find_all(el, "queriedFields"):
                    field = (qf.text or "").strip()
                    if field:
                        add_edge(flow_nid, field_node(obj, field, stub=True), "reads_field")
        if start_obj:
            for el in root.iter():
                if _local(el.tag) in ("leftValueReference", "elementReference"):
                    m = _RECORD_REF_RE.match((el.text or "").strip())
                    if m:
                        add_edge(flow_nid, field_node(start_obj, m.group(1), stub=True),
                                 "reads_field")
        # Objects touched by record elements (lookups/creates/updates/deletes).
        for el in root.iter():
            if _local(el.tag) == "object":
                obj = (el.text or "").strip()
                if obj and make_id(obj) not in triggered:
                    add_edge(flow_nid, obj_ref(obj), "references")

    # VALIDATION RULE: objects/<Obj>/validationRules/<R>.validationRule-meta.xml
    elif name.endswith(".validationRule-meta.xml"):
        if path.parent.name == "validationRules":
            obj_api = path.parent.parent.name
            rule_api = strip_suffix(".validationRule-meta.xml")
            rule_nid = make_id(obj_api, "vr", rule_api)
            add_node(rule_nid, f"{obj_api}.{rule_api}")
            add_edge(obj_ref(obj_api), rule_nid, "validates")
            add_formula_refs(rule_nid, obj_api, _child_text(root, "errorConditionFormula"))

    # PERMISSION SET: permissionsets/<PS>.permissionset-meta.xml
    elif name.endswith(".permissionset-meta.xml"):
        ps_nid = make_id("permset", strip_suffix(".permissionset-meta.xml"))
        add_node(ps_nid, strip_suffix(".permissionset-meta.xml"))
        for fp in _find_all(root, "fieldPermissions"):
            field_ref = _child_text(fp, "field")  # "Object.Field"
            if field_ref and "." in field_ref:
                obj_api, field_api = field_ref.split(".", 1)
                add_edge(
                    ps_nid, field_node(obj_api, field_api, stub=True), "grants_fls",
                    readable=_child_text(fp, "readable") == "true",
                    editable=_child_text(fp, "editable") == "true",
                )
        for op in _find_all(root, "objectPermissions"):
            obj_api = _child_text(op, "object")
            if obj_api:
                add_edge(ps_nid, obj_ref(obj_api), "grants_access")
        for rv in _find_all(root, "recordTypeVisibilities"):
            rt_ref = _child_text(rv, "recordType")  # "Object.DeveloperName"
            if rt_ref and "." in rt_ref and _child_text(rv, "visible") == "true":
                obj_api, rt_api = rt_ref.split(".", 1)
                rt_nid = make_id(obj_api, "rt", rt_api)
                add_node(rt_nid, rt_ref, stub=True)
                add_edge(ps_nid, rt_nid, "grants_record_type")
        for ca in _find_all(root, "classAccesses"):
            cls_api = _child_text(ca, "apexClass")
            if cls_api and _child_text(ca, "enabled") == "true":
                cls_nid = make_id(cls_api)
                add_node(cls_nid, cls_api, stub=True)  # the generic rewire folds it into the class
                add_edge(ps_nid, cls_nid, "grants_apex")

    # RECORD TYPE: objects/<Obj>/recordTypes/<RT>.recordType-meta.xml
    elif name.endswith(".recordType-meta.xml"):
        if path.parent.name == "recordTypes":
            obj_api = path.parent.parent.name
            rt_api = strip_suffix(".recordType-meta.xml")
            rt_nid = make_id(obj_api, "rt", rt_api)
            add_node(rt_nid, f"{obj_api}.{rt_api}")
            picklists: dict[str, list[str]] = {}
            for pv in _find_all(root, "picklistValues"):
                picklist = _child_text(pv, "picklist")
                if picklist:
                    picklists[picklist] = [
                        v for v in (_child_text(val, "fullName") for val in _find_all(pv, "values")) if v
                    ]
            nodes[-1]["picklists"] = picklists
            nodes[-1]["business_process"] = _child_text(root, "businessProcess")
            add_edge(obj_ref(obj_api), rt_nid, "has_record_type")

    # DLRS ROLLUP: customMetadata/dlrs__LookupRollupSummary2.<Name>.md-meta.xml
    elif name.startswith(_DLRS_PREFIX) and name.endswith(".md-meta.xml"):
        rollup_api = name[len(_DLRS_PREFIX): -len(".md-meta.xml")]
        values: dict[str, str | None] = {}
        for v in _find_all(root, "values"):
            key = _child_text(v, "field")
            val_el = next((c for c in v if _local(c.tag) == "value"), None)
            if key and val_el is not None and val_el.get(_XSI_NIL) != "true":
                values[key] = (val_el.text or "").strip() or None
            elif key:
                values[key] = None
        roll_nid = make_id("rollup", rollup_api)
        add_node(roll_nid, rollup_api)
        nodes[-1]["operation"] = values.get("dlrs__AggregateOperation__c")
        nodes[-1]["active"] = values.get("dlrs__Active__c") == "true"
        nodes[-1]["criteria"] = values.get("dlrs__RelationshipCriteria__c")
        parent = values.get("dlrs__ParentObject__c")
        child = values.get("dlrs__ChildObject__c")
        if child and values.get("dlrs__FieldToAggregate__c"):
            add_edge(roll_nid, field_node(child, values["dlrs__FieldToAggregate__c"], stub=True),
                     "reads_field")
        if parent and values.get("dlrs__AggregateResultField__c"):
            add_edge(roll_nid, field_node(parent, values["dlrs__AggregateResultField__c"], stub=True),
                     "writes_field")
        if child and values.get("dlrs__RelationshipField__c"):
            add_edge(roll_nid, field_node(child, values["dlrs__RelationshipField__c"], stub=True),
                     "references")

    return {"nodes": nodes, "edges": edges}


def resolve_salesforce_references(per_file: list[dict], all_nodes: list[dict],
                                  all_edges: list[dict]) -> None:
    """Fold sourceless SObject/field stubs into their ``*-meta.xml`` definitions.

    The generic unique-stub rewire already binds exact-case *object* stubs, but
    it skips dotted labels (``Account.Industry__c``) and only case-folds for
    case-insensitive languages. Salesforce API names are case-insensitive and
    fields are dotted, so without this pass a field granted by a permission set
    or read in SOQL stays a separate node from its definition (the definition's
    id is salted apart by the per-file disambiguation). Matching is by
    case-folded label against definitions in SFDX metadata files only, and only
    when that definition is unique.
    """
    real_by_label: dict[str, list[dict]] = {}
    for node in all_nodes:
        src = str(node.get("source_file") or "")
        if src and is_salesforce_metadata_path(Path(src)):
            label = str(node.get("label", "")).strip().casefold()
            if label:
                real_by_label.setdefault(label, []).append(node)

    remap: dict[str, str] = {}
    for node in all_nodes:
        if node.get("source_file"):
            continue
        label = str(node.get("label", "")).strip().casefold()
        # Dot guard: an object stub (no dot) folds only into an object, a field
        # stub (dotted) only into a field; a field never binds to its namesake object.
        candidates = [c for c in real_by_label.get(label, [])
                      if ("." in str(c.get("label", ""))) == ("." in label)]
        if len(candidates) == 1 and candidates[0].get("id") != node.get("id"):
            remap[str(node.get("id"))] = str(candidates[0]["id"])

    # Apex field accesses whose object could not be typed are `*.Field__c` stubs.
    # Bind one only when exactly one SFDX object defines that field name;
    # an ambiguous name is dropped, never guessed.
    dropped: set[str] = set()
    for node in all_nodes:
        label = str(node.get("label", "")).strip()
        if node.get("source_file") or not label.startswith("*."):
            continue
        suffix = label[1:].casefold()  # ".field__c"
        candidates = [c for key, nodes in real_by_label.items()
                      if key.endswith(suffix) and key.count(".") == 1 for c in nodes]
        if len(candidates) == 1:
            remap[str(node.get("id"))] = str(candidates[0]["id"])
        else:
            dropped.add(str(node.get("id")))

    # One-hop formula references `Obj.Rel__r.Field__c`: follow the `Obj.Rel__c`
    # lookup to its target object `T` and bind to the unique field `T.Field__c`;
    # drop the stub when the hop or the target field can't be resolved.
    by_id = {str(n.get("id")): n for n in all_nodes}
    lookup_target: dict[str, str] = {}
    for edge in all_edges:
        if edge.get("relation") in ("looks_up_to", "master_detail_to"):
            tgt = by_id.get(str(edge.get("target")))
            if tgt is not None:
                lookup_target.setdefault(str(edge.get("source")), str(tgt.get("label", "")))
    for node in all_nodes:
        m = _REL_STUB_RE.match(str(node.get("label", "")).strip())
        if node.get("source_file") or not m:
            continue
        obj, rel, fld = m.groups()
        rel_field = real_by_label.get(f"{obj}.{rel[:-3]}__c".casefold(), [])
        target_obj = lookup_target.get(str(rel_field[0].get("id"))) if len(rel_field) == 1 else None
        hit = real_by_label.get(f"{target_obj}.{fld}".casefold(), []) if target_obj else []
        if len(hit) == 1:
            remap[str(node.get("id"))] = str(hit[0]["id"])
        else:
            dropped.add(str(node.get("id")))
    if not remap and not dropped:
        return

    all_nodes[:] = [n for n in all_nodes
                    if str(n.get("id")) not in remap and str(n.get("id")) not in dropped]
    all_edges[:] = [e for e in all_edges
                    if e.get("source") not in dropped and e.get("target") not in dropped]
    seen: set[tuple] = set()
    kept: list[dict] = []
    for edge in all_edges:
        edge["source"] = remap.get(edge.get("source"), edge.get("source"))
        edge["target"] = remap.get(edge.get("target"), edge.get("target"))
        key = (edge["source"], edge["target"], edge.get("relation"), edge.get("source_file"))
        if key in seen:
            continue
        seen.add(key)
        kept.append(edge)
    all_edges[:] = kept
