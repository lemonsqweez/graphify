"""Salesforce SFDX metadata (``*-meta.xml``) → graph structure.

Deterministic XML parse (stdlib ElementTree, no LLM); every edge is EXTRACTED.
SObject and field node IDs are global — ``make_id(object_api)`` and
``make_id(object_api, field_api)`` — so they converge with the SObject / field
stubs ``extract_apex`` mints from SOQL and trigger headers, wiring Apex code to
the schema it touches.

Handles: object (incl. legacy inline fields/rules); field (Lookup / MasterDetail
relationships, formulas, roll-up summaries); flow (record-trigger object,
referenced objects, field reads/writes, invocable Apex actions);
validationRule; recordType; permissionset (field, object, record type, Apex
class and flow grants); permissionsetgroup; quickAction; flexipage; DLRS
rollups. Relation vocabulary: ``has_field``, ``looks_up_to``,
``master_detail_to``, ``triggers_on``, ``references``, ``validates``,
``reads_field``, ``writes_field``, ``has_record_type``, ``calls_apex``,
``calls_flow``, ``uses_component``, ``grants_fls``, ``grants_access``,
``grants_record_type``, ``grants_apex``, ``grants_flow``,
``includes_permset``.

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
    ".quickAction-meta.xml",
    ".flexipage-meta.xml",
    ".permissionsetgroup-meta.xml",
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
# `$Record.Field` or a one-hop `$Record.Rel__r.Field__c`; longer paths match
# with group(3) set and are skipped, as are standard relationship hops.
_RECORD_PATH_RE = re.compile(r"\$Record(?:__Prior)?\.(\w+)(?:\.(\w+))?(\.\w+)*")


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

    def named_ref(kind: str, api: str) -> str:
        # Stub for a flow / permission set defined in its own file; folded into
        # that definition by label in resolve_salesforce_references.
        nid = make_id(kind, api)
        add_node(nid, api, stub=True)
        return nid

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
        # Roll-up summary: "Child.Field" aggregated over the "Child.Lookup" master-detail.
        for tag, relation in (("summarizedField", "reads_field"), ("summaryForeignKey", "references")):
            ref = _child_text(field_el, tag)
            if ref and "." in ref:
                add_edge(f_nid, field_node(*ref.split(".", 1), stub=True), relation)
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
                nodes[-1]["active"] = _child_text(vr, "active") == "true"
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
            flow_node["status"] = _child_text(root, "status")
            flow_node["active"] = flow_node["status"] == "Active"

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
        for dcs in _find_all(root, "dynamicChoiceSets"):
            obj = _child_text(dcs, "object") or _child_text(dcs, "picklistObject")
            if not obj:
                continue
            fields = [_child_text(dcs, t) for t in
                      ("displayField", "valueField", "picklistField", "sortField")]
            fields += [_child_text(c, "field") for c in dcs
                       if _local(c.tag) in ("filters", "outputAssignments")]
            for field in fields:
                if field:
                    add_edge(flow_nid, field_node(obj, field, stub=True), "reads_field")
        if start_obj:
            for flt in (f for start in _find_all(root, "start") for f in _find_all(start, "filters")):
                field = _child_text(flt, "field")
                if field:
                    add_edge(flow_nid, field_node(start_obj, field, stub=True), "reads_field")
            # $Record references anywhere: element references, formulas, start
            # filter formulas, text templates. Assigning to $Record.X is a write
            # (the before-save field update idiom), not a read.
            for el in root.iter():
                text = (el.text or "").strip()
                if _local(el.tag) == "assignToReference":
                    m = _RECORD_REF_RE.match(text)
                    if m:
                        add_edge(flow_nid, field_node(start_obj, m.group(1), stub=True),
                                 "writes_field")
                    continue
                for m in _RECORD_PATH_RE.finditer(text):
                    head, tail = m.group(1), m.group(2)
                    if m.group(3):
                        continue
                    if tail is None:
                        add_edge(flow_nid, field_node(start_obj, head, stub=True), "reads_field")
                    elif head.lower().endswith("__r") and tail.lower().endswith("__c"):
                        nid = make_id(start_obj, head, tail)
                        add_node(nid, f"{start_obj}.{head}.{tail}", stub=True)
                        add_edge(flow_nid, nid, "reads_field")
        # Invocable Apex actions. The class stub folds into the class via the
        # generic rewire; _prune_unresolved_apex_edges drops it if no class exists.
        for ac in _find_all(root, "actionCalls"):
            cls_api = _child_text(ac, "actionName")
            if cls_api and _child_text(ac, "actionType") == "apex":
                cls_nid = make_id(cls_api)
                add_node(cls_nid, cls_api, stub=True)
                add_edge(flow_nid, cls_nid, "calls_apex")
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
            nodes[-1]["active"] = _child_text(root, "active") == "true"
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
        for fa in _find_all(root, "flowAccesses"):
            flow_api = _child_text(fa, "flow")
            if flow_api and _child_text(fa, "enabled") == "true":
                add_edge(ps_nid, named_ref("flow", flow_api), "grants_flow")

    # PERMISSION SET GROUP: permissionsetgroups/<G>.permissionsetgroup-meta.xml
    elif name.endswith(".permissionsetgroup-meta.xml"):
        group_nid = make_id("permsetgroup", strip_suffix(".permissionsetgroup-meta.xml"))
        add_node(group_nid, strip_suffix(".permissionsetgroup-meta.xml"))
        for ps in _find_all(root, "permissionSets"):
            ps_api = (ps.text or "").strip()
            if ps_api:
                add_edge(group_nid, named_ref("permset", ps_api), "includes_permset")

    # QUICK ACTION: quickActions/<Obj>.<Action>.quickAction-meta.xml (global
    # actions have no object prefix)
    elif name.endswith(".quickAction-meta.xml"):
        action_api = strip_suffix(".quickAction-meta.xml")
        action_nid = make_id("quickaction", action_api)
        add_node(action_nid, action_api)
        if "." in action_api:
            add_edge(action_nid, obj_ref(action_api.split(".", 1)[0]), "references")
        flow_api = _child_text(root, "flowDefinition")
        if flow_api:
            add_edge(action_nid, named_ref("flow", flow_api), "calls_flow")

    # LIGHTNING PAGE: flexipages/<Page>.flexipage-meta.xml. Custom components
    # (`c:name`) become stubs that _link_lwc_components binds to the LWC bundle.
    elif name.endswith(".flexipage-meta.xml"):
        page_nid = make_id("flexipage", strip_suffix(".flexipage-meta.xml"))
        add_node(page_nid, strip_suffix(".flexipage-meta.xml"))
        for el in root.iter():
            comp = (el.text or "").strip() if _local(el.tag) == "componentName" else ""
            if comp.startswith("c:") and len(comp) > 2:
                lwc_nid = make_id("lwc", comp[2:])
                add_node(lwc_nid, comp, stub=True)
                add_edge(page_nid, lwc_nid, "uses_component")

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


_APEX_TARGET_RELATIONS = frozenset({"tests", "calls_apex"})


def _prune_unresolved_apex_edges(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Keep a ``tests`` / ``calls_apex`` / Apex ``uses`` edge only when it lands on Apex code.

    ``extract_apex`` emits a ``tests`` edge per capitalized type a test class
    touches and a ``uses`` edge per one any other class or trigger mentions, and
    a flow emits ``calls_apex`` per invocable action. After the
    generic stub rewire those naming real Apex classes point at the class node,
    while ``System``/``Test``, SObjects (``new Account(...)``) and actions whose
    class is not in the project are not, so their edges are dropped, and then
    any stub left with no edge."""
    by_id = {str(n.get("id")): n for n in all_nodes}

    def is_apex(node: dict | None) -> bool:
        return node is not None and str(node.get("source_file") or "").endswith((".cls", ".trigger"))

    dropped_targets: set[str] = set()
    kept: list[dict] = []
    for edge in all_edges:
        relation = edge.get("relation")
        apex_only = relation in _APEX_TARGET_RELATIONS or (
            relation == "uses" and str(edge.get("source_file") or "").endswith((".cls", ".trigger")))
        if apex_only and not is_apex(by_id.get(str(edge.get("target")))):
            dropped_targets.add(str(edge.get("target")))
            continue
        kept.append(edge)
    if len(kept) == len(all_edges):
        return
    all_edges[:] = kept
    linked = {str(e.get("source")) for e in kept} | {str(e.get("target")) for e in kept}
    orphans = {t for t in dropped_targets
               if t not in linked and not (by_id.get(t) or {}).get("source_file")}
    all_nodes[:] = [n for n in all_nodes if str(n.get("id")) not in orphans]


_LWC_APEX_IMPORT_RE = re.compile(r"@salesforce/apex/(\w+)\.(\w+)")


def _link_lwc_to_apex(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Add ``calls_apex`` edges from an LWC file to the Apex method it imports.

    The JS extractor collapses every ``@salesforce/apex/Class.method`` import to
    one shared ``ref_salesforce_apex`` node, so the class and method are recovered
    by reading the importing ``.js`` (components only; ``__tests__`` mocks name
    methods that need not exist). The target is the method node, else the class."""
    classes: dict[str, list[dict]] = {}
    for node in all_nodes:
        if str(node.get("source_file") or "").endswith(".cls") and not str(node.get("label", "")).startswith("."):
            classes.setdefault(str(node.get("label", "")).casefold(), []).append(node)
    by_id = {str(n.get("id")): n for n in all_nodes}
    methods: dict[str, dict[str, dict]] = {}
    for edge in all_edges:
        if edge.get("relation") == "method":
            child = by_id.get(str(edge.get("target")))
            label = str(child.get("label", "")) if child else ""
            if label.startswith(".") and label.endswith("()"):
                methods.setdefault(str(edge.get("source")), {})[label[1:-2].casefold()] = child

    seen: set[tuple[str, str]] = set()
    new_edges: list[dict] = []
    for edge in all_edges:
        if edge.get("relation") != "imports_from" or edge.get("target") != "ref_salesforce_apex":
            continue
        src_file = str(edge.get("source_file") or "")
        if not src_file or "__tests__" in Path(src_file).parts:
            continue
        try:
            text = Path(src_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _LWC_APEX_IMPORT_RE.finditer(text):
            cands = classes.get(m.group(1).casefold(), [])
            if len(cands) != 1:
                continue
            cls = cands[0]
            target = methods.get(str(cls["id"]), {}).get(m.group(2).casefold(), cls)
            key = (str(edge["source"]), str(target["id"]))
            if key in seen:
                continue
            seen.add(key)
            new_edges.append({
                "source": edge["source"], "target": target["id"], "relation": "calls_apex",
                "confidence": "EXTRACTED", "source_file": src_file,
                "source_location": None, "weight": 1.0,
            })
    all_edges.extend(new_edges)


_LWC_CHILD_TAG_RE = re.compile(r"<c-([a-z0-9]+(?:-[a-z0-9]+)*)[\s/>]")


def _link_lwc_components(all_nodes: list[dict], all_edges: list[dict],
                         fresh_ids: set[str]) -> None:
    """Bind ``c:name`` component stubs to their LWC bundle and add LWC composition.

    A bundle is represented by its ``name.js`` file node. Flexipage stubs that
    match exactly one bundle are rewired to it, others dropped. A bundle whose
    ``name.html`` template renders ``<c-child-name>`` gets ``uses_component`` to
    the ``childName.js`` node (the template itself is not extracted, so it is
    read beside the ``.js`` path recorded on that file's edges). Only bundles in
    ``fresh_ids`` are read, so an incremental run never re-emits the edges of an
    unchanged bundle that reached it as context."""
    bundles: dict[str, list[dict]] = {}
    for node in all_nodes:
        label = str(node.get("label", ""))
        if node.get("source_file") and label.endswith(".js"):
            bundles.setdefault(label[:-3].casefold(), []).append(node)

    def bundle(name: str) -> dict | None:
        cands = bundles.get(name.casefold(), [])
        return cands[0] if len(cands) == 1 else None

    remap: dict[str, str] = {}
    dropped: set[str] = set()
    for node in all_nodes:
        label = str(node.get("label", ""))
        if node.get("source_file") or not label.startswith("c:"):
            continue
        target = bundle(label[2:])
        if target is not None:
            remap[str(node["id"])] = str(target["id"])
        else:
            dropped.add(str(node["id"]))

    js_paths: dict[str, str] = {}
    for edge in all_edges:
        src_file = str(edge.get("source_file") or "")
        if src_file.endswith(".js") and "__tests__" not in Path(src_file).parts:
            js_paths.setdefault(str(edge.get("source")), src_file)
    new_edges: list[dict] = []
    for name, nodes in bundles.items():
        nid = str(nodes[0]["id"])
        if len(nodes) != 1 or nid not in js_paths or nid not in fresh_ids:
            continue
        js_path = Path(js_paths[nid])
        if js_path.parent.name.casefold() != name:
            continue
        try:
            html = js_path.with_suffix(".html").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for tag in dict.fromkeys(_LWC_CHILD_TAG_RE.findall(html)):
            first, *rest = tag.split("-")
            child = bundle(first + "".join(p[:1].upper() + p[1:] for p in rest))
            if child is not None and child is not nodes[0]:
                new_edges.append({
                    "source": nodes[0]["id"], "target": child["id"], "relation": "uses_component",
                    "confidence": "EXTRACTED", "source_file": str(js_path.with_suffix(".html")),
                    "source_location": None, "weight": 1.0,
                })
    if remap or dropped:
        all_nodes[:] = [n for n in all_nodes
                        if str(n.get("id")) not in remap and str(n.get("id")) not in dropped]
        all_edges[:] = [e for e in all_edges
                        if e.get("source") not in dropped and e.get("target") not in dropped]
        for edge in all_edges:
            edge["target"] = remap.get(edge.get("target"), edge.get("target"))
    all_edges.extend(new_edges)


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
    _link_lwc_to_apex(all_nodes, all_edges)
    _link_lwc_components(all_nodes, all_edges,
                         {str(n.get("id")) for r in per_file for n in r.get("nodes", [])})
    _prune_unresolved_apex_edges(all_nodes, all_edges)
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
