# Salesforce SFDX Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make graphify build an accurate, merged graph of a Salesforce SFDX repo. Field-level read/write, automation and permission edges must answer five golden questions on `salesforce-crm`.

**Architecture:** A deterministic stdlib-XML extractor (`graphify/salesforce_ingest.py`) handles the `*-meta.xml` types, routed by filename before suffix dispatch. The Apex regex extractor emits shared sourceless stubs for objects and fields. One registered resolver (`resolve_salesforce_references`) runs after the generic stub rewire and folds every stub into its unique definition: case-insensitive labels, relationship hops, unique-field-name fallback and LWC→Apex links. The GivePower-only pieces (data dictionary, refresh hook, skill config) live in `salesforce-crm`.

**Tech Stack:** Python 3.14, `xml.etree.ElementTree`, `re`, pytest, networkx (via graphify).

**Spec:** `docs/superpowers/specs/2026-10-17-salesforce-sfdx-design.md`

## Global Constraints

- **Branch:** `salesforce-sfdx` in `C:\Users\JasonHensley\Projects\graphify`. Commit only after the user approves each task's diff. Never push.
- **Starting state:** the branch already carries uncommitted partial work. `graphify/salesforce_ingest.py` is new, and `graphify/extractors/apex.py` already has the stub `add_node`, multi-line SOQL and DML removal. Task 1 and Task 2 adopt this work. They do not rewrite it from scratch.
- **Dependencies:** no new ones. The extractors use only the stdlib.
- **Import direction:** `graphify/salesforce_ingest.py` must not import `graphify.extract`. `graphify.detect` imports it lazily, inside `classify_file`.
- **Node ids:**
  - Object: `make_id(Obj)`
  - Field: `make_id(Obj, Field)`
  - Record type: `make_id(Obj, "rt", DevName)`
  - Validation rule: `make_id(Obj, "vr", Rule)`
  - Flow: `make_id("flow", Name)`
  - Permission set: `make_id("permset", Name)`
  - Rollup: `make_id("rollup", RecordName)`
- **Labels:**
  - Object: `Obj`
  - Field: `Obj.Field`
  - Record type, rule: `Obj.Name`
  - Unresolved Apex field: `*.Field__c`
  - Unresolved relationship field: `Obj.Rel__r.Field__c`
- **Stubs:** a reference stub has `source_file: ""`, no `origin_file` key, and `source_location: None`.
- **Relations:** `has_field`, `looks_up_to`, `master_detail_to`, `has_record_type`, `validates`, `triggers_on`, `references`, `writes_field`, `reads_field`, `queries`, `grants_fls`, `grants_access`, `grants_record_type`, `grants_apex`, `calls_apex`, `tests`.
- **Confidence:** XML edges are EXTRACTED. Apex SOQL, field and test edges are INFERRED.
- **Python:** native `python` (3.14, `C:\Users\JasonHensley\AppData\Local\Programs\Python\Python314\python.exe`). graphify is editable-installed from this repo.
- **Running graphify on salesforce-crm:** do not run `graphify update` there until Task 11.
- **Run tests with:** `python -m pytest <target> -q`

## Review Focus

1. **XML tags split across lines.** For example `<leftValueReference\n  >X</leftValueReference>`. These must parse correctly, so use ElementTree and never regex on XML. Pinned in the Task 1 fixtures and in `test_flow_split_tag_parsed`.
2. **Trigger declarations spanning lines,** including a header that opens with a comment block, or has `on Account` with the events on the next line. Pinned in `test_apex_trigger_multiline_header`.
3. **Field names on several objects.** `Revenue_Period__c` is both an object and a field on two objects. A write must never bind to the object node or to the wrong object. Pinned in `test_unresolved_field_ambiguous_dropped` and `test_field_stub_never_binds_object`.
4. **Objects with no `.object-meta.xml`** (Contact, Lead, Payment_Schedule__c) must still be one shared node, not N per-file nodes. Pinned in `test_undefined_object_single_node`.
5. **LWC `__tests__` mocks** naming methods that don't exist (`PaymentWorkspaceController.saveVolume`) must not create `calls_apex` edges. Pinned in `test_lwc_tests_dir_ignored`.

---

### Task 1: Route SFDX metadata and finish the core extractor

**Files:**
- Modify: `graphify/salesforce_ingest.py` (existing partial)
- Modify: `graphify/detect.py:518-528` (`classify_file`)
- Modify: `graphify/extract.py`:
  - imports near :19-20
  - `_get_extractor` near :8798-8804
  - resolver registration near :7373-7384
- Modify: `graphify/cache.py:40`. Bump `_AST_CACHE_SCHEMA` to `18` and add a short note.
- Create: `tests/fixtures/sfdx/force-app/main/default/...`, a minimal tree with:
  - `objects/Account/Account.object-meta.xml`
  - `objects/Account/fields/Region__c.field-meta.xml` (Text)
  - `objects/npe01__OppPayment__c/npe01__OppPayment__c.object-meta.xml`
  - `objects/npe01__OppPayment__c/fields/Revenue_Period__c.field-meta.xml` (Lookup → Revenue_Period__c)
  - `objects/Revenue_Period__c/Revenue_Period__c.object-meta.xml`
  - `objects/tccxero__Invoice_Line_Item__c/fields/Revenue_Period__c.field-meta.xml` (Lookup)
  - `objects/Contact/fields/Donor_Level__c.field-meta.xml` (no Contact object file)
  - `objects/Account/validationRules/Region_Required.validationRule-meta.xml`
  - `permissionsets/Fundraising_Full_Access.permissionset-meta.xml`, granting `Account.Region__c` (readable, not editable) and object `Account`

  Every file uses the `http://soap.sforce.com/2006/04/metadata` namespace, and at least one has a split tag (`<description\n  >`).
- Create: `tests/test_salesforce_ingest.py`

**Interfaces:**
- Produces:
  - `is_salesforce_metadata_path(path: Path) -> bool`
  - `extract_salesforce_metadata(path: Path) -> dict`
  - `resolve_salesforce_references(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None`
  - `SF_METADATA_SUFFIXES: tuple[str, ...]`
- Produces: the fixture helper `build_sfdx(tmp_path, files: dict[str, str]) -> Path` in `tests/test_salesforce_ingest.py`. It writes `files` under `tmp_path`, runs `graphify.extract.extract(list_of_paths, root=tmp_path)` and returns nothing extra. Tests then build `G` with `graphify.build.build_from_json`. Check the actual `extract`/`build_from_json` signatures before writing the helper.

- [ ] **Step 1: Write failing tests** in `tests/test_salesforce_ingest.py`:
  - `test_classify_meta_xml_as_code`:
    - `classify_file(Path("x/Account.object-meta.xml")) == FileType.CODE`
    - `classify_file(Path("x/Foo.cls-meta.xml")) is None`
    - `classify_file(Path("x/data.xml")) is None`
  - `test_get_extractor_routes_meta_xml`: `_get_extractor(Path("a/Region__c.field-meta.xml")) is extract_salesforce_metadata`
  - `test_object_and_field_nodes`: running the extractor on the Account object and the Region field gives:
    - a node with id `make_id("Account")` and a real `source_file`
    - a field node with label `Account.Region__c`
    - a `has_field` edge
  - `test_lookup_edge`: `npe01__OppPayment__c.Revenue_Period__c` gets a `looks_up_to` edge to stub id `make_id("Revenue_Period__c")`
  - `test_permset_field_ref_is_stub`: the permset's `Account.Region__c` node has `source_file == ""`, and its `grants_fls` edge carries `readable=True, editable=False`
  - `test_full_extract_merges_field`: a full `extract()` over the fixture gives:
    - exactly one node labeled `Account.Region__c` (case-insensitive)
    - exactly one node labeled `Account`
    - the `grants_fls` edge targeting the real field node
  - `test_undefined_object_single_node`: the full extract has exactly one node labeled `Contact`
  - `test_field_stub_never_binds_object`: both `Revenue_Period__c` field nodes stay distinct from the `Revenue_Period__c` object node (3 distinct ids)
- [ ] **Step 2:** Run `python -m pytest tests/test_salesforce_ingest.py -q`. Expected: FAIL. The routing tests fail on import or assert, and the merge tests fail because the extractor isn't wired.
- [ ] **Step 3: Wire routing.**
  - In `classify_file`, right after the `.blade.php` check, add a lazy `from graphify.salesforce_ingest import is_salesforce_metadata_path` that returns `FileType.CODE`.
  - In `_get_extractor`, after the manifest check, return `extract_salesforce_metadata` for matching paths.
  - Register `LanguageResolver("salesforce", frozenset({".xml", ".cls", ".trigger", ".js"}), resolve_salesforce_references)` alongside the other `register(...)` calls.
  - Bump `_AST_CACHE_SCHEMA` to 18.
- [ ] **Step 4: Fix the extractor and resolver.**
  - In `salesforce_ingest.py`, `grants_fls` edges carry the `readable`/`editable` booleans parsed from `fieldPermissions`.
  - In `resolve_salesforce_references`, a stub labeled `X` (no dot) may only fold into a definition whose label has no dot, and a dotted stub only into a dotted definition. That's what enforces `test_field_stub_never_binds_object`.
- [ ] **Step 5:** Run `python -m pytest tests/test_salesforce_ingest.py tests/test_extractors_registry.py tests/test_detect.py -q`. Expected: PASS. If `tests/test_detect.py` doesn't exist, run the files that `grep -l classify_file tests/` lists.
- [ ] **Step 6: Commit** after user approval: `feat(salesforce): route SFDX metadata and merge object/field stubs`

### Task 2: Apex triggers, SOQL and DML (adopt the partial work)

**Files:**
- Modify: `graphify/extractors/apex.py`
- Modify: `tests/test_languages.py:4419-4447` (the existing Apex asserts)
- Modify: `tests/fixtures/sample.cls`. Add one multi-line SOQL with a child subquery and a semi-join.

**Interfaces:**
- Produces:
  - trigger nodes with a `trigger_events: list[str]` attribute, lowercased (e.g. `["before insert", "after update"]`)
  - `triggers_on` edges from trigger to object stub
  - `queries` edges from class (or file) to object stub

- [ ] **Step 1: Update the existing tests in `test_languages.py`:**
  - `test_apex_soql_uses_edge`: assert `"queries"` in relations (was `"uses"`)
  - `test_apex_trigger_uses_sobject`: assert `"triggers_on"` in relations
  - Replace `test_apex_dml_uses_edge` with `test_apex_no_dml_nodes`: no node label is in `{"insert","update","delete","upsert","merge","undelete"}`

  Add:
  - `test_apex_trigger_multiline_header(tmp_path)`: source `"/** doc */\ntrigger dlrs_AccountTrigger on Account\n  (before delete, after insert)\n{\n}\n"` gives:
    - a node labeled `dlrs_AccountTrigger` with `trigger_events == ["before delete", "after insert"]`
    - a `triggers_on` edge to `make_id("Account")`
  - `test_apex_soql_multiline_subquery(tmp_path)`: in a class, the query `[SELECT Id, (SELECT Id FROM Contacts) FROM Account\n WHERE Id IN (SELECT AccountId FROM Opportunity)]` gives `queries` targets equal to `{make_id("Account"), make_id("Opportunity")}`, and no node labeled `Contacts`
  - `test_apex_dynamic_soql(tmp_path)`: `Database.query('SELECT Id FROM Revenue_Period__c')` gives a `queries` edge to `make_id("Revenue_Period__c")`
  - `test_apex_sobject_stub_sourceless`: every `queries`/`triggers_on` target node has `source_file == ""`
- [ ] **Step 2:** Run `python -m pytest tests/test_languages.py -q -k apex`. Expected: the trigger-multiline and events tests FAIL. The others may already pass from the partial work.
- [ ] **Step 3:** In `extract_apex`, match the trigger header against the whole source with `re.compile(r"^\s*trigger\s+(\w+)\s+on\s+(\w+)\s*\(([^)]*)\)", re.I | re.M)`, before the line loop. Split the events on commas, collapse whitespace and lowercase them. Remove the per-line `trigger_re` branch, but keep `current_class_nid = trig_nid` for lines after the header line.
- [ ] **Step 4:** Run `python -m pytest tests/test_languages.py -q -k apex`. Expected: PASS.
- [ ] **Step 5: Commit** after approval: `feat(apex): multi-line triggers with events, multi-line SOQL, drop DML hubs`

### Task 3: Apex field reads and writes

**Files:**
- Modify: `graphify/extractors/apex.py`
- Modify: `graphify/salesforce_ingest.py` (resolver fallback)
- Test: `tests/test_languages.py`, `tests/test_salesforce_ingest.py`

**Interfaces:**
- Consumes: the Task 1 resolver.
- Produces:
  - `writes_field` / `reads_field` edges (INFERRED) from the enclosing method, or the class when no method is open
  - The target is the field stub `make_id(Obj, Field)` / `Obj.Field` when the object is known. Otherwise it's `make_id("sf_unresolved_field", Field)` with label `*.Field`.
  - Track `current_method_nid`: set it on a method match, and reset it on a class, interface or enum match.

- [ ] **Step 1: Write failing tests.**
  - `test_apex_field_write_constructor`: `new npe01__OppPayment__c(Id = p.Id, Revenue_Period__c = periodId)` gives `writes_field` to `make_id("npe01__OppPayment__c","Revenue_Period__c")`
  - `test_apex_field_write_declared_var`: `npe01__OppPayment__c upd = x;\n upd.Revenue_Period__c = y;` gives `writes_field` to the same id
  - `test_apex_field_write_list_index`: `List<npe01__OppPayment__c> ups = x;\n ups[i].Revenue_Period__c = y;` gives the same
  - `test_apex_field_write_for_var`: `for (Payment_Schedule_Line__c l : lines) { l.Status__c = 'X'; }` gives `writes_field` to `make_id("Payment_Schedule_Line__c","Status__c")`
  - `test_apex_field_read_dotted`: `Id x = rec.Volume_Comm_Status__c;` with no declaration of `rec` gives `reads_field` to `make_id("sf_unresolved_field","Volume_Comm_Status__c")`, label `*.Volume_Comm_Status__c`
  - `test_apex_write_preferred_over_read`: the same method both reads and writes `p.Volume_Comm_Status__c`, and the edges include `writes_field`
  - In `test_salesforce_ingest.py`:
    - `test_unresolved_field_unique_binds`: an Apex class reads `rec.Region__c` (undeclared) plus the Task 1 fixture. After `extract()`, the edge targets the real `Account.Region__c` node.
    - `test_unresolved_field_ambiguous_dropped`: undeclared `rec.Revenue_Period__c = x` (two definitions). After `extract()`, no node labeled `*.Revenue_Period__c` remains and no `writes_field` edge targets a `Revenue_Period__c` node.
- [ ] **Step 2:** Run both test files with `-k "field"`. Expected: FAIL.
- [ ] **Step 3: Implement in `apex.py`.** Build the declaration list for the whole file first, then classify each match.

  Declarations, with the regexes run over the source:
  - `(?:List|Set)<\s*(\w+)\s*>\s+(\w+)`
  - `Map<\s*\w+\s*,\s*(\w+)\s*>\s+(\w+)`
  - `\b(\w+__c|Account|Contact|Opportunity|Lead|Campaign|Case|User)\s+(\w+)\s*[=;:,)]`
  - `for\s*\(\s*(\w+)\s+(\w+)\s*:`

  The type of `var` is that of the nearest declaration at a lower offset.

  Constructor writes: `new\s+(\w+)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)`, then `(\w+__c)\s*=` inside the arguments.

  Accesses: `(\w+)(?:\[[^\]]*\])?\.(\w+__c)\b`.
  - It's a write when followed by `\s*=(?!=)`, otherwise a read.
  - Skip accesses inside SOQL brackets and `__r.` chains, because Task 2's SOQL handles those.
  - Skip the match when `var` is itself a field (preceded by `.`).
- [ ] **Step 4: Implement in the resolver.** For each node labeled `*.F`, find SFDX-defined field nodes whose label casefold ends with `"." + F.casefold()`.
  - Exactly one match: remap the edges to it.
  - Otherwise: drop the stub and its edges.
- [ ] **Step 5:** Run `python -m pytest tests/test_languages.py tests/test_salesforce_ingest.py -q`. Expected: PASS.
- [ ] **Step 6: Commit** after approval: `feat(apex): typed field read/write edges with unique-name fallback`

### Task 4: Flow field reads, writes and trigger type

**Files:**
- Modify: `graphify/salesforce_ingest.py` (the flow branch)
- Create: `tests/fixtures/sfdx/force-app/main/default/flows/Stamp_Region.flow-meta.xml`. It's record-triggered on Opportunity (`RecordAfterSave`, `CreateAndUpdate`) and contains:
  - a `recordUpdates` with `<object>Account</object>` assigning `Region__c`
  - a `recordUpdates` using `<inputReference>varPayments</inputReference>`, where `varPayments` has `<objectType>npe01__OppPayment__c</objectType>` and the update assigns `Revenue_Period__c`
  - a `recordLookups` filtering on `Status__c` with `<queriedFields>Name</queriedFields>`
  - a decision with a split `<leftValueReference\n >$Record.StageName</leftValueReference>`
- Test: `tests/test_salesforce_ingest.py`

**Interfaces:**
- Produces:
  - flow node attributes `trigger_type`, `record_trigger_type`, `process_type` (strings or None)
  - `triggers_on` edges with the same `trigger_type`/`record_trigger_type` attributes

- [ ] **Step 1: Write failing tests:**
  - `test_flow_trigger_attrs`
  - `test_flow_writes_explicit_object`: `writes_field` to `make_id("Account","Region__c")`
  - `test_flow_writes_via_variable`: `writes_field` to `make_id("npe01__OppPayment__c","Revenue_Period__c")`
  - `test_flow_reads_lookup_filter`: `reads_field` to `make_id(<lookup object>,"Status__c")`
  - `test_flow_split_tag_parsed`: `reads_field` to `make_id("Opportunity","StageName")`
- [ ] **Step 2:** Run `-k flow`. Expected: FAIL.
- [ ] **Step 3: Implement.** Resolve each record element's object in this order:
  1. `<object>`
  2. `<inputReference>` → the `<variables>` entry with that `<name>` → `<objectType>`
  3. `$Record` / no reference → the start object

  Then emit edges:
  - `inputAssignments/field` → `writes_field` (on recordUpdates/recordCreates)
  - `filters/field` and `queriedFields` → `reads_field`
  - `leftValueReference`/`elementReference` values matching `^\$Record(?:__Prior)?\.(\w+)$` → `reads_field` on the start object

  Field nodes are stubs. Keep the existing `references` edges for objects that aren't written.
- [ ] **Step 4:** Run `python -m pytest tests/test_salesforce_ingest.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** after approval: `feat(salesforce): flow field-level reads/writes and trigger type`

### Task 5: Record types, DLRS rollups, and permission-set record-type and Apex grants

**Files:**
- Modify: `graphify/salesforce_ingest.py`. Add `.recordType-meta.xml` and the DLRS record match to the routing predicate: a name starting with `dlrs__LookupRollupSummary2.` and ending in `.md-meta.xml`.
- Create the fixtures:
  - `objects/Opportunity/recordTypes/Volume_Recurring.recordType-meta.xml`, with `<businessProcess>` and one `<picklistValues>` block (picklist `StageName`, values `Pledged`, `Closed Won`)
  - `customMetadata/dlrs__LookupRollupSummary2.Acct_Lifetime.md-meta.xml`: Parent `Account`, Child `tccxero__Invoice_Line_Item__c`, RelationshipField `Account__c`, FieldToAggregate `tccxero__Total_Amount__c`, AggregateResultField `Total_Lifetime_Giving_Invoiced__c`, Operation `Sum`, Active `true`, plus one `<value xsi:nil="true"/>`
  - Add `<recordTypeVisibilities>` (`Opportunity.Volume_Recurring`) and `<classAccesses>` (`PaymentWorkspaceController`) to the permset fixture
- Test: `tests/test_salesforce_ingest.py`

**Interfaces:**
- Produces:
  - record-type node `make_id("Opportunity","rt","Volume_Recurring")`, label `Opportunity.Volume_Recurring`, attrs `picklists: dict[str, list[str]]` and `business_process: str | None`
  - rollup node `make_id("rollup","Acct_Lifetime")`, label `Acct_Lifetime`, attrs `operation`, `active: bool`, `criteria: str | None`
  - `grants_apex` edge target: a stub labeled with the class name. The generic rewire folds it into the Apex class.

- [ ] **Step 1: Write failing tests:**
  - `test_record_type_node_and_picklists`: `picklists == {"StageName": ["Pledged","Closed Won"]}`, plus a `has_record_type` edge
  - `test_permset_grants_record_type`
  - `test_permset_grants_apex_folds_to_class`: a full `extract()` with a minimal `PaymentWorkspaceController.cls` gives a `grants_apex` edge whose target is that class node
  - `test_dlrs_rollup_edges`:
    - `reads_field` to `make_id("tccxero__Invoice_Line_Item__c","tccxero__Total_Amount__c")`
    - `writes_field` to `make_id("Account","Total_Lifetime_Giving_Invoiced__c")`
    - `references` to `make_id("tccxero__Invoice_Line_Item__c","Account__c")`
    - `active is True`
  - `test_other_custom_metadata_ignored`: `is_salesforce_metadata_path(Path("Xero_Program_Mapping.Code_4010.md-meta.xml")) is False`
- [ ] **Step 2:** Run `-k "record_type or dlrs or grants or custom_metadata"`. Expected: FAIL.
- [ ] **Step 3:** Implement. CMDT `<values>` pairs are `<field>` and `<value>`; treat `xsi:nil` as None.
- [ ] **Step 4:** Run `python -m pytest tests/test_salesforce_ingest.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** after approval: `feat(salesforce): record types, DLRS rollups, permset RT/Apex grants`

### Task 6: Formula-field and validation-rule references

**Files:**
- Modify: `graphify/salesforce_ingest.py`
- Fixtures:
  - formula field `objects/Account/fields/Region_Label__c.field-meta.xml`, with formula `/* c */ IF(ISPICKVAL(Region__c, &quot;West&quot;), Parent__r.Region__c, $User.Id)`
  - lookup field `objects/Account/fields/Parent__c.field-meta.xml`, referencing `Account`
  - give the validation-rule fixture `<errorConditionFormula>AND(ISBLANK(Region__c), RecordType.DeveloperName != &quot;X&quot;)</errorConditionFormula>`
- Test: `tests/test_salesforce_ingest.py`

**Interfaces:**
- Produces: `reads_field` edges from a formula field or validation rule. The targets are:
  - same-object field stubs `Obj.F`
  - relationship stubs labeled `Obj.Rel__r.F` with id `make_id(Obj, Rel__r, F)`, which the resolver maps through `Obj.Rel__c` (`looks_up_to` target) to `Target.F`, dropping the stub when it can't resolve

- [ ] **Step 1: Write failing tests:**
  - `test_formula_reads_same_object_field`: `reads_field` to `Account.Region__c`
  - `test_formula_relationship_hop`: after a full `extract()`, `Region_Label__c` has `reads_field` to the real `Account.Region__c` node via `Parent__r`, and no node labeled `Account.Parent__r.Region__c` remains
  - `test_formula_ignores_globals_strings_comments`: no edge target label contains `$User`, `West` or `c`
  - `test_validation_rule_reads_field`
- [ ] **Step 2:** Run `-k "formula or validation"`. Expected: FAIL.
- [ ] **Step 3: Implement the tokenizer.**
  1. Strip `/* … */` comments and `"…"` strings. ElementTree has already decoded entities.
  2. Tokens are `(?<![\w$.])([A-Za-z]\w*(?:\.[A-Za-z]\w*)*)`.
  3. Skip a token that is a function (followed by `\s*\(`), `TRUE`/`FALSE`/`NULL`, or starts with `$`.
  4. Emit a single-segment token only when it ends `__c`.
  5. Two segments `X__r.F`: a relationship stub. `RecordType.DeveloperName` and other standard relationship paths: skip.

  The resolver hop: find the field node `Obj.Rel__c` (label casefold) and its `looks_up_to`/`master_detail_to` target object label `T`, then remap to the field labeled `T.F` if it's unique; otherwise drop.
- [ ] **Step 4:** Run `python -m pytest tests/test_salesforce_ingest.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** after approval: `feat(salesforce): formula and validation-rule field references`

### Task 7: LWC → Apex links

**Files:**
- Modify: `graphify/salesforce_ingest.py` (resolver)
- Test: `tests/test_salesforce_ingest.py`

**Interfaces:**
- Consumes: the JS extractor's `imports_from` edge to `ref_salesforce_apex` from the component's file node, with the edge's `source_file` set to the `.js` path.
- Produces: `calls_apex` edges (EXTRACTED) from that file node to the Apex method node. The method node is found by:
  1. the class node with label == ClassName from a `.cls` `source_file`
  2. then its `method`-edge child labeled `.methodName()`

  If no method matches, the edge falls back to the class node.

- [ ] **Step 1: Write failing tests:**
  - `test_lwc_calls_apex_method`: a full `extract()` over `lwc/pw/pw.js` (`import getWorkspace from "@salesforce/apex/PaymentWorkspaceController.getWorkspace";`) and a `.cls` defining that method gives a `calls_apex` edge to the `.getWorkspace()` node
  - `test_lwc_tests_dir_ignored`: `lwc/pw/__tests__/pw.test.js` importing `...PaymentWorkspaceController.saveVolume` gives no `calls_apex` edge
- [ ] **Step 2:** Run `-k lwc`. Expected: FAIL.
- [ ] **Step 3: Implement.** For each distinct `source_file` among the `ref_salesforce_apex` edges, skip any path containing a `__tests__` segment. Read the file and match `@salesforce/apex/(\w+)\.(\w+)`.
- [ ] **Step 4:** Run `python -m pytest tests/test_salesforce_ingest.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** after approval: `feat(salesforce): link LWC components to Apex methods`

### Task 8: Test classes, and making the new relations visible to impact queries

**Files:**
- Modify: `graphify/extractors/apex.py`. Add `is_test: True` on class nodes whose pending annotations include `istest`. In a test class, emit a `tests` edge (INFERRED) to a stub labeled `X` for each `\bnew\s+([A-Z]\w+)\s*\(` and `\b([A-Z]\w+)\s*\.\s*\w+\s*\(`.
- Modify: `graphify/salesforce_ingest.py`. After folding, the resolver drops `tests` edges whose target is still sourceless, and drops any stub left with no edges.
- Modify: `graphify/analyze.py:112-148` (`god_nodes`). Skip nodes with `is_test`.
- Modify: `graphify/cluster.py:185-209` (`label_communities_by_hub`). Choose the hub from the non-`is_test` members when there are any.
- Modify: `graphify/affected.py:12`. Add `queries`, `writes_field`, `reads_field`, `triggers_on`, `has_field`, `looks_up_to`, `master_detail_to`, `calls_apex`, `tests` to `DEFAULT_AFFECTED_RELATIONS`.
- Test: `tests/test_languages.py`, `tests/test_salesforce_ingest.py`, plus whichever existing tests cover `god_nodes`/`label_communities_by_hub` (find them with grep).

- [ ] **Step 1: Write failing tests:**
  - `test_apex_is_test_flag`
  - `test_tests_edge_resolves_to_class`: a full `extract()` of `HandlerTest.cls` (`@isTest`, which calls `Handler.run()` and `System.assert(...)`) plus `Handler.cls` gives exactly one `tests` edge, to the Handler class node. No node labeled `System` remains.
  - `test_god_nodes_skip_tests`: a graph where the highest-degree node has `is_test=True` doesn't list it
  - `test_hub_label_skips_tests`
  - `test_affected_includes_sf_relations`: `"writes_field" in DEFAULT_AFFECTED_RELATIONS`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement as listed under Files.
- [ ] **Step 4:** Run `python -m pytest tests -q -x`. Expected: PASS for the full suite. Report any failures that existed before this branch separately; the user decides on those.
- [ ] **Step 5: Commit** after approval: `feat(salesforce): test-class tagging and tests edges; SF relations in affected`

### Task 9: Data dictionary (in salesforce-crm)

**Files:**
- Create: `C:\Users\JasonHensley\Projects\salesforce-crm\scripts\graphify\sf_dictionary.py`
- Create: `C:\Users\JasonHensley\Projects\salesforce-crm\scripts\graphify\test_sf_dictionary.py`

**Interfaces:**
- Consumes: `graphify-out/graph.json`, with node attrs and relations from Tasks 1-8.
- Produces:
  - `build_dictionary(graph_json: dict) -> dict[str, dict]`, keyed by object API name. Each entry is `{"fields": [{"name","type"?,"looks_up_to"?,"written_by":[...],"read_by":[...],"granted_by":[{"permset","readable","editable"}]}], "record_types": [{"name","picklists","business_process"}], "automation": {"triggers":[{"name","events"}], "flows":[{"name","trigger_type","record_trigger_type"}], "validation_rules":[...], "rollups_into":[...]}, "queried_by": [...]}`
  - `render_markdown(obj: str, entry: dict) -> str`
  - CLI `python scripts/graphify/sf_dictionary.py [graphify-out]` writes `graphify-out/sf_dictionary.json`, `graphify-out/wiki/objects/<Obj>.md` and `graphify-out/wiki/objects/index.md`
  - Method writers roll up to their class via the `method` edge, as `Class.method`.

- [ ] **Step 1: Write failing tests.** Use a small inline graph JSON with one object, one field, one writer method, one flow and one permset grant.
  - `test_dictionary_field_writers`
  - `test_dictionary_automation`
  - `test_markdown_has_sections`: headings `## Fields`, `## Record types`, `## Automation`, `## Access`
- [ ] **Step 2:** Run `python -m pytest scripts/graphify/test_sf_dictionary.py -q` from salesforce-crm. Expected: FAIL.
- [ ] **Step 3:** Implement. Use the stdlib only and read the graph JSON with `json`. Use the node `label` for names.
- [ ] **Step 4:** Run it. Expected: PASS.
- [ ] **Step 5:** Don't commit in salesforce-crm. Show the diff to the user; they commit in that repo.

### Task 10: Wire graphify into salesforce-crm

**Files** (all in `C:\Users\JasonHensley\Projects\salesforce-crm`):
- Modify: `.graphifyignore`. Add `**/__tests__/`. Re-include `docs/adr/` and `salesforce-engineering-docs/architecture/` while keeping other `*.md` excluded. First confirm that graphify's ignore matcher supports `!` negation (grep `graphify/detect.py` for its ignore parser). If it doesn't, list the excluded md paths explicitly.
- Modify: `.claude/settings.local.json`. Remove `"graphify": "off"` from `skillOverrides`, and leave the rest untouched.
- Run: `graphify install --project` (it writes `.claude/skills/graphify`, the `## graphify` CLAUDE.md section and PreToolUse hooks). Then append one line to that section: `For Salesforce impact questions ("what writes/reads <Obj.Field>", "what fires on <Obj> save", "which permsets grant …") use graphify query/explain or graphify-out/wiki/objects/<Obj>.md before grepping. If graphify-out/REBUILD_FAILED exists, say so and run scripts/graphify/refresh.ps1.`
- Create: `scripts/graphify/refresh.ps1`. It runs `graphify update .`, then `graphify export wiki`, then `python scripts/graphify/sf_dictionary.py graphify-out`. On any non-zero exit, it writes the step name and stderr to `graphify-out/REBUILD_FAILED` and exits 1. On success, it deletes `REBUILD_FAILED`.
- Modify: `.git/hooks/post-commit`. Append a marker-delimited block (`# >>> graphify-refresh` / `# <<< graphify-refresh`) that starts `pwsh -NoProfile -File scripts/graphify/refresh.ps1` in the background, logging to `graphify-out/refresh.log`. Skip the run when only `graphify-out/` changed.
- Modify: `.claude/settings.local.json`. Add a SessionStart hook command that prints `graphify rebuild FAILED - see graphify-out/REBUILD_FAILED` when that file exists. Use the update-config skill for the hook format.

- [ ] **Step 1:** Show the user the exact proposed changes to `.graphifyignore`, `settings.local.json`, the CLAUDE.md addition and the hook block. Wait for approval.
- [ ] **Step 2:** Apply them.
- [ ] **Step 3: Verify the failure path.** Temporarily set `$env:PATH` so `graphify` isn't found, run `refresh.ps1`, and confirm that `REBUILD_FAILED` appears and the SessionStart command prints the warning. Restore PATH and delete the marker.

### Task 11: Full rebuild and golden questions

**Files:**
- Create: `C:\Users\JasonHensley\Projects\salesforce-crm\scripts\graphify\golden.py`. It loads `graphify-out/graph.json`, answers the five spec questions, and asserts them against `scripts/graphify/golden_expected.json`.
- Create: `scripts/graphify/golden_expected.json`, with the answers the user confirmed in Step 1.

- [ ] **Step 1: Confirm the expected answers before running anything.** Gather the expected answers by reading the salesforce-crm source directly, without using the graph:
  - Apex writers (spec lists)
  - flows with `inputAssignments` on the target fields
  - DLRS records whose `AggregateResultField` matches
  - Opportunity triggers, record-triggered flows and validation rules
  - permsets with `npe01__OppPayment__c.` in `fieldPermissions`
  - test classes referencing PaymentScheduleLineHandler

  Present them to the user, and write `golden_expected.json` only after the user confirms.
- [ ] **Step 2:** Delete `graphify-out/cache` in salesforce-crm, then run `pwsh scripts/graphify/refresh.ps1`. Expected: exit 0 and no `REBUILD_FAILED`.
- [ ] **Step 3:** Run `python scripts/graphify/golden.py`. Expected: all 5 PASS. On a failure, debug the extractor (superpowers:systematic-debugging). Never edit the expected file to match the output.
- [ ] **Step 4:** Spot-check `graphify-out/GRAPH_REPORT.md`:
  - The god nodes are domain objects, not test classes.
  - Exactly one node labeled `Account` and one labeled `Opportunity`: `graphify explain Account`.
- [ ] **Step 5:** Report the node/edge counts and the golden results to the user. The upstream PR is a separate, later step that the user starts.
