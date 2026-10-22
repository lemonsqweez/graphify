"""Apex extractor. Moved verbatim from graphify/extract.py."""
from __future__ import annotations


from pathlib import Path
from graphify.extractors.base import _file_stem, _make_id, _read_source_text


def extract_apex(path: Path) -> dict:
    """Extract classes, interfaces, enums, methods, and Salesforce constructs from
    Apex .cls and .trigger files using regex (no tree-sitter grammar on PyPI)."""
    import re as _re
    try:
        source = _read_source_text(path)
    except OSError:
        return {"nodes": [], "edges": []}

    str_path = str(path)
    stem = _file_stem(path)
    file_nid = _make_id(str_path)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid: str, label: str, line: int, stub: bool = False) -> None:
        # stub=True: a cross-file SObject/field reference (SOQL FROM, trigger
        # header). It carries no source_file and, deliberately, no origin_file,
        # so _disambiguate_colliding_node_ids leaves the bare id shared across
        # every Apex file instead of salting it per file. The unique-stub rewire
        # and resolve_salesforce_references then fold it into the object's or
        # field's *-meta.xml definition.
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": "" if stub else str_path,
                "source_location": None if stub else f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED") -> None:
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    add_node(file_nid, path.name, 1)

    lines = source.splitlines()

    _ACCESS = r"(?:public|private|protected|global|webService)?"
    _SHARING = r"(?:\s+(?:with|without|inherited)\s+sharing)?"
    _MOD = r"(?:\s+(?:abstract|virtual|override|static|final|transient|testMethod))?"
    _ANNOTATION = r"(?:\s*@\w+(?:\s*\([^)]*\))?\s*)*"

    cls_re = _re.compile(
        rf"^{_ANNOTATION}\s*{_ACCESS}{_SHARING}{_MOD}\s*class\s+(\w+)"
        rf"(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s]+))?\s*\{{?",
        _re.IGNORECASE,
    )
    iface_re = _re.compile(
        rf"^{_ANNOTATION}\s*{_ACCESS}{_SHARING}{_MOD}\s*interface\s+(\w+)"
        rf"(?:\s+extends\s+([\w,\s]+))?\s*\{{?",
        _re.IGNORECASE,
    )
    enum_re = _re.compile(
        rf"^{_ANNOTATION}\s*{_ACCESS}{_SHARING}{_MOD}\s*enum\s+(\w+)\s*\{{?",
        _re.IGNORECASE,
    )
    # The header is matched over the whole source: real triggers open with a
    # comment block and put the events on a following line.
    trigger_re = _re.compile(
        r"^[ \t]*trigger\s+(\w+)\s+on\s+(\w+)\s*\(([^)]*)\)",
        _re.IGNORECASE | _re.MULTILINE,
    )
    trigger_header = trigger_re.search(source)
    trigger_line = (source.count("\n", 0, trigger_header.start(1)) + 1
                    if trigger_header else -1)
    method_re = _re.compile(
        rf"^{_ANNOTATION}\s*{_ACCESS}{_MOD}\s*(?:static\s+)?[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+\w+\s*)?\{{?",
        _re.IGNORECASE,
    )
    annotation_re = _re.compile(r"@(\w+)", _re.IGNORECASE)
    # SOQL spans lines in real code, so it is matched over the whole source after
    # the line loop: static `[SELECT ...]` queries and dynamic string queries.
    soql_static_re = _re.compile(r"\[\s*(SELECT\b[^\]]*)\]", _re.IGNORECASE)
    soql_string_re = _re.compile(r"'\s*(SELECT\b[^']*)'", _re.IGNORECASE)
    from_re = _re.compile(r"\bFROM\s+(\w+)", _re.IGNORECASE)
    custom_field_re = _re.compile(r"(?<![\w.])(\w+__c)\b", _re.IGNORECASE)

    _CONTROL_FLOW = frozenset({
        "if", "else", "for", "while", "do", "switch", "try", "catch",
        "finally", "return", "throw", "new", "void", "null",
        "true", "false", "this", "super", "class", "interface", "enum",
        "trigger", "on",
    })

    current_class_nid: str | None = None
    pending_annotations: list[str] = []
    # Owner (enclosing class/trigger, else the file) of each line, so SOQL
    # matched over the whole source afterwards attributes to the right class.
    line_owner: list[str] = [file_nid] * (len(lines) + 2)

    for lineno, line_text in enumerate(lines, start=1):
        line_owner[lineno] = current_class_nid or file_nid
        stripped = line_text.strip()

        if stripped.startswith("@"):
            for m in annotation_re.finditer(stripped):
                pending_annotations.append(m.group(1).lower())
            continue

        if lineno == trigger_line and trigger_header:
            trig_name, sobject = trigger_header.group(1), trigger_header.group(2)
            trig_nid = _make_id(stem, trig_name)
            add_node(trig_nid, trig_name, lineno)
            nodes[-1]["trigger_events"] = [
                " ".join(ev.split()).lower()
                for ev in trigger_header.group(3).split(",") if ev.strip()
            ]
            add_edge(file_nid, trig_nid, "contains", lineno)
            sob_nid = _make_id(sobject)
            add_node(sob_nid, sobject, lineno, stub=True)
            add_edge(trig_nid, sob_nid, "triggers_on", lineno)
            current_class_nid = trig_nid
            pending_annotations = []
            continue

        cm = cls_re.match(stripped)
        if cm:
            class_name = cm.group(1)
            if class_name.lower() in _CONTROL_FLOW:
                pending_annotations = []
                continue
            class_nid = _make_id(stem, class_name)
            add_node(class_nid, class_name, lineno)
            add_edge(file_nid, class_nid, "contains", lineno)
            if cm.group(2):
                base = cm.group(2).strip()
                base_nid = _make_id(stem, base)
                if base_nid not in seen_ids:
                    base_nid = _make_id(base)
                if base_nid not in seen_ids:
                    add_node(base_nid, base, lineno)
                add_edge(class_nid, base_nid, "extends", lineno, confidence="INFERRED")
            if cm.group(3):
                for iface in cm.group(3).split(","):
                    iface = iface.strip()
                    if iface:
                        iface_nid = _make_id(stem, iface)
                        if iface_nid not in seen_ids:
                            iface_nid = _make_id(iface)
                        if iface_nid not in seen_ids:
                            add_node(iface_nid, iface, lineno)
                        add_edge(class_nid, iface_nid, "implements", lineno, confidence="INFERRED")
            current_class_nid = class_nid
            pending_annotations = []
            continue

        im = iface_re.match(stripped)
        if im:
            iface_name = im.group(1)
            if iface_name.lower() in _CONTROL_FLOW:
                pending_annotations = []
                continue
            iface_nid = _make_id(stem, iface_name)
            add_node(iface_nid, iface_name, lineno)
            add_edge(file_nid if current_class_nid is None else current_class_nid,
                     iface_nid, "contains", lineno)
            if im.group(2):
                for parent in im.group(2).split(","):
                    parent = parent.strip()
                    if parent:
                        parent_nid = _make_id(stem, parent)
                        if parent_nid not in seen_ids:
                            parent_nid = _make_id(parent)
                        if parent_nid not in seen_ids:
                            add_node(parent_nid, parent, lineno)
                        add_edge(iface_nid, parent_nid, "extends", lineno, confidence="INFERRED")
            pending_annotations = []
            continue

        em = enum_re.match(stripped)
        if em:
            enum_name = em.group(1)
            if enum_name.lower() in _CONTROL_FLOW:
                pending_annotations = []
                continue
            enum_nid = _make_id(stem, enum_name)
            add_node(enum_nid, enum_name, lineno)
            add_edge(file_nid if current_class_nid is None else current_class_nid,
                     enum_nid, "contains", lineno)
            pending_annotations = []
            continue

        if current_class_nid is not None:
            mm = method_re.match(stripped)
            if mm:
                method_name = mm.group(1)
                if method_name.lower() not in _CONTROL_FLOW:
                    method_nid = _make_id(current_class_nid, method_name)
                    method_label = f".{method_name}()"
                    add_node(method_nid, method_label, lineno)
                    add_edge(current_class_nid, method_nid, "method", lineno)
                    if "auraenabled" in pending_annotations or "invocablemethod" in pending_annotations:
                        add_edge(file_nid, method_nid, "contains", lineno, confidence="INFERRED")
                    pending_annotations = []
                    continue

        pending_annotations = []

    # DML statements (insert/update/delete/...) deliberately emit no nodes: regex
    # can't resolve the operand's SObject type, so a `dml_<op>` verb node becomes
    # one global hub wired to every class doing DML. "This code touches
    # <SObject>" is captured by SOQL FROM edges and trigger headers instead.
    seen_soql_edges: set[tuple[str, str]] = set()

    def soql_edge(src: str, tgt: str, relation: str, line: int) -> None:
        if (src, tgt) not in seen_soql_edges:
            seen_soql_edges.add((src, tgt))
            add_edge(src, tgt, relation, line, confidence="INFERRED")

    for query_re in (soql_static_re, soql_string_re):
        for qm in query_re.finditer(source):
            query = qm.group(1)
            lineno = source.count("\n", 0, qm.start()) + 1
            src = line_owner[min(lineno, len(line_owner) - 1)]
            # Paren depth at each FROM: depth 0 is the outer object. A nested FROM
            # *before* it is a child-relationship subquery (`(SELECT Id FROM
            # Contacts)`, a relationship name, not an SObject) and is skipped; one
            # *after* it is a semi-join (`WHERE Id IN (SELECT AccountId FROM
            # Contact)`) and names a real SObject.
            depth_at = [0] * (len(query) + 1)
            depth = 0
            for i, ch in enumerate(query):
                depth_at[i] = depth
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth = max(0, depth - 1)
            outer_obj: str | None = None
            outer_pos = -1
            for fm in from_re.finditer(query):
                if depth_at[fm.start()] == 0:
                    outer_obj, outer_pos = fm.group(1), fm.start()
                    break
            if outer_obj is None:
                continue
            for fm in from_re.finditer(query):
                obj = fm.group(1)
                if fm.start() != outer_pos and fm.start() < outer_pos:
                    continue
                sob_nid = _make_id(obj)
                add_node(sob_nid, obj, lineno, stub=True)
                soql_edge(src, sob_nid, "queries", lineno)
            # Custom fields (`Foo__c`) read at the outer level bind to their
            # .field-meta.xml definitions. Standard fields (Id, Name) are left
            # out: they have no definition in the repo and would only add noise.
            for cf in custom_field_re.finditer(query):
                if depth_at[cf.start()] != 0:
                    continue
                field = cf.group(1)
                f_nid = _make_id(outer_obj, field)
                add_node(f_nid, f"{outer_obj}.{field}", lineno, stub=True)
                soql_edge(src, f_nid, "reads_field", lineno)

    return {"nodes": nodes, "edges": edges}
