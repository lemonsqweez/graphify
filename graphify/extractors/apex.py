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
    # Innermost owner (open method, else class/trigger, else file) of each line,
    # for field read/write edges.
    line_src: list[str] = [file_nid] * (len(lines) + 2)
    current_method_nid: str | None = None
    test_class_nid: str | None = None
    test_class_name: str | None = None
    seen_tests: set[tuple[str, str]] = set()

    # Brace depth at the start of each line (strings and comments stripped), so a
    # class or method scope ends where its braces close. Without it, the first
    # inner class (`RepointRequest`, an exception type, ...) would capture every
    # later member of its outer class.
    depth_before = [0] * (len(lines) + 2)
    depth = 0
    in_block_comment = False
    for i, raw in enumerate(lines, start=1):
        depth_before[i] = depth
        code, pos = [], 0
        while pos < len(raw):
            if in_block_comment:
                end = raw.find("*/", pos)
                if end < 0:
                    pos = len(raw)
                else:
                    in_block_comment, pos = False, end + 2
            elif raw.startswith("/*", pos):
                in_block_comment, pos = True, pos + 2
            elif raw.startswith("//", pos):
                break
            elif raw[pos] == "'":
                pos += 1
                while pos < len(raw) and raw[pos] != "'":
                    pos += 2 if raw[pos] == "\\" else 1
                pos += 1
            else:
                code.append(raw[pos])
                pos += 1
        text = "".join(code)
        depth += text.count("{") - text.count("}")
    depth_before[len(lines) + 1] = depth
    # [class nid, depth at its declaration line, body entered, enclosing test state]
    class_stack: list[list] = []
    method_scope: list | None = None  # [depth at declaration, body entered]
    # `new X(` or any static member access `X.member` (call, constant or field).
    tested_re = _re.compile(r"\bnew\s+([A-Z]\w*)\s*\(|\b([A-Z]\w*)\s*\.\s*\w+")

    for lineno, line_text in enumerate(lines, start=1):
        d = depth_before[lineno]
        if method_scope is not None:
            if d > method_scope[0]:
                method_scope[1] = True
            elif method_scope[1]:
                current_method_nid, method_scope = None, None
        while class_stack:
            top = class_stack[-1]
            if d > top[1]:
                top[2] = True
            if not (top[2] and d <= top[1]):
                break
            class_stack.pop()
            test_class_nid, test_class_name = top[3]
            current_method_nid, method_scope = None, None
            current_class_nid = class_stack[-1][0] if class_stack else None
        line_owner[lineno] = current_class_nid or file_nid
        line_src[lineno] = current_method_nid or current_class_nid or file_nid
        stripped = line_text.strip()

        # In an @isTest class, every capitalized type it instantiates or calls a
        # static member on is a candidate subject under test. Targets that don't
        # resolve to an Apex class (System, Test, SObjects) are pruned by
        # resolve_salesforce_references.
        if test_class_nid is not None and not stripped.startswith(("//", "/*", "*")):
            for tm_ in tested_re.finditer(stripped):
                tname = tm_.group(1) or tm_.group(2)
                if tname == test_class_name or (test_class_nid, tname) in seen_tests:
                    continue
                seen_tests.add((test_class_nid, tname))
                t_nid = _make_id(tname)
                add_node(t_nid, tname, lineno, stub=True)
                add_edge(test_class_nid, t_nid, "tests", lineno, confidence="INFERRED")

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
            class_stack.append([trig_nid, depth_before[lineno], False, (test_class_nid, test_class_name)])
            current_class_nid = trig_nid
            current_method_nid, method_scope = None, None
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
            prev_test = (test_class_nid, test_class_name)
            if "istest" in pending_annotations:
                nodes[-1]["is_test"] = True
                test_class_nid, test_class_name = class_nid, class_name
            else:
                test_class_nid = test_class_name = None
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
            class_stack.append([class_nid, depth_before[lineno], False, prev_test])
            current_class_nid = class_nid
            current_method_nid, method_scope = None, None
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
            current_method_nid = None
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
            current_method_nid = None
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
                    line_src[lineno] = method_nid
                    # A bodiless declaration (`void m();`) or a one-line body
                    # (`void m() { ... }`) never opens a multi-line scope.
                    one_liner = "{" in stripped and depth_before[lineno + 1] <= depth_before[lineno]
                    if stripped.endswith(";") or one_liner:
                        current_method_nid, method_scope = None, None
                    else:
                        current_method_nid = method_nid
                        method_scope = [depth_before[lineno], False]
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

    # --- Custom-field reads/writes outside SOQL ---------------------------------
    # Bind each `var.Field__c` / `new Type(Field__c = ...)` to its SObject. The
    # type comes from the nearest preceding declaration of `var`; with none, the
    # edge targets an unresolved `*.Field__c` stub that resolve_salesforce_references
    # binds only when exactly one object defines that field name.
    decl_res = (
        _re.compile(r"\b(?:List|Set)<\s*(\w+)\s*>\s+(\w+)"),
        _re.compile(r"\bMap<\s*\w+\s*,\s*(\w+)\s*>\s+(\w+)"),
        _re.compile(r"\b(\w+__c|Account|Contact|Opportunity|Lead|Campaign|Case|User)\s+(\w+)\s*[=;:,)]"),
        _re.compile(r"\bfor\s*\(\s*(\w+)\s+(\w+)\s*:"),
    )
    decls: list[tuple[int, str, str]] = []  # (offset, var, type)
    for dre in decl_res:
        for dm in dre.finditer(source):
            decls.append((dm.start(), dm.group(2), dm.group(1)))

    def declared_type(var: str, offset: int) -> str | None:
        best: tuple[int, str] | None = None
        for off, v, t in decls:
            if v == var and off < offset and (best is None or off > best[0]):
                best = (off, t)
        return best[1] if best else None

    soql_spans = [m.span() for rx in (soql_static_re, soql_string_re)
                  for m in rx.finditer(source)]

    def in_soql(pos: int) -> bool:
        return any(a <= pos < b for a, b in soql_spans)

    # (source nid, target nid) -> (relation, line); a write beats a read.
    field_edges: dict[tuple[str, str], tuple[str, int]] = {}

    def field_edge(src: str, obj: str | None, field: str, relation: str, line: int) -> None:
        if obj:
            tgt, label = _make_id(obj, field), f"{obj}.{field}"
        else:
            tgt, label = _make_id("sf_unresolved_field", field), f"*.{field}"
        add_node(tgt, label, line, stub=True)
        prev = field_edges.get((src, tgt))
        if prev is None or (relation == "writes_field" and prev[0] != "writes_field"):
            field_edges[(src, tgt)] = (relation, line)

    def src_at(pos: int) -> tuple[str, int]:
        ln = source.count("\n", 0, pos) + 1
        return line_src[min(ln, len(line_src) - 1)], ln

    ctor_re = _re.compile(r"\bnew\s+(\w+)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
    ctor_field_re = _re.compile(r"(?<![\w.])(\w+__c)\s*=(?!=)", _re.IGNORECASE)
    for cm_ in ctor_re.finditer(source):
        for fm in ctor_field_re.finditer(cm_.group(2)):
            src, ln = src_at(cm_.start())
            field_edge(src, cm_.group(1), fm.group(1), "writes_field", ln)

    access_re = _re.compile(r"(?<![\w.])(\w+)(?:\[[^\]]*\])?\.(\w+__c)\b", _re.IGNORECASE)
    for am in access_re.finditer(source):
        var, field = am.group(1), am.group(2)
        if in_soql(am.start()) or var.lower().endswith("__r"):
            continue
        is_write = _re.match(r"\s*=(?!=)", source[am.end():]) is not None
        src, ln = src_at(am.start())
        field_edge(src, declared_type(var, am.start()), field,
                   "writes_field" if is_write else "reads_field", ln)

    for (src, tgt), (relation, ln) in field_edges.items():
        add_edge(src, tgt, relation, ln, confidence="INFERRED")

    return {"nodes": nodes, "edges": edges}
