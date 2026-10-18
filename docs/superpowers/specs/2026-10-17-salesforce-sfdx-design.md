# Salesforce SFDX support for graphify: design

Status: agreed 2026-10-17 (grilling session). Target repo: `salesforce-crm` (GivePower), decomposed SFDX, API 67.0.

## Purpose

The graph maps what the Salesforce code and metadata **are**. claude-mem and `salesforce-engineering-docs/` hold the **why**. The graph will also be the authoring source for future fundraiser Cowork skills, which query through the Salesforce MCP connector. Those skills are out of scope here.

Primary questions:
1. **Impact analysis:** what reads or writes field X?
2. **Automation per object:** what fires on save?
3. **Field-level-security check:** which permission sets grant field X?

## Coverage

| Source | Nodes | Edges (relation) |
|---|---|---|
| `*.object-meta.xml` | object (the real definition) | — |
| `*.field-meta.xml` | field `Obj.Field` | `has_field`, `looks_up_to`, `master_detail_to`; a formula field `reads_field` the fields it references |
| `*.validationRule-meta.xml` | rule `Obj.Rule` | `validates`; the rule `reads_field` |
| `*.recordType-meta.xml` | record type `Obj.DevName` (attrs: `picklists`, `business_process`) | `has_record_type` |
| `*.flow-meta.xml` | flow (attrs: `trigger_type`, `record_trigger_type`, `process_type`) | `triggers_on`, `writes_field`, `reads_field`, `references` |
| `*.permissionset-meta.xml` | permission set | `grants_fls` (attrs `readable`/`editable`), `grants_access`, `grants_record_type`, `grants_apex` |
| `dlrs__LookupRollupSummary2.*.md-meta.xml` | rollup (attrs: `operation`, `active`, `criteria`) | `reads_field` (child aggregate field), `writes_field` (parent result field), `references` (child relationship field) |
| Apex `.trigger` | trigger (attr `trigger_events`) | `triggers_on` |
| Apex `.cls` | class, method (`is_test` attr on `@isTest` classes) | `queries`, `reads_field`, `writes_field`, `tests` |
| LWC `lwc/**/*.js` (excluding `__tests__`) | — | `calls_apex` (component file → Apex method) |

Deferred: flexipages, list views, layouts, compact layouts, web links, profiles, other custom-metadata types, Aura.

## Rules

- **Node ids are global.**
  - Object: `make_id(Obj)`.
  - Field: `make_id(Obj, Field)`.
  - Record type: `make_id(Obj, "rt", DevName)`.
  - Validation rule: `make_id(Obj, "vr", Rule)`.
- **References are stubs.** A reference is `source_file=""` with no `origin_file`. A definition has a real `source_file`. A Salesforce resolver folds stubs into their unique `*-meta.xml` definition, using case-insensitive labels.
- **Apex field binding.**
  1. Find the object from `new Type(Field__c = …)`, or from the variable's declared type (`Type v`, `List<Type> v`, `Map<K, Type> v`, `for (Type v : …)`), nearest preceding declaration first.
  2. If that fails, bind to the field only when exactly one object defines that field name. Mark the edge INFERRED.
  3. If the name is ambiguous, emit nothing.
  - Only custom fields (`__c`) are bound.
- **SOQL** is matched across lines, both static `[SELECT …]` and quoted string queries.
  - Child-relationship subqueries in the SELECT list are skipped.
  - Semi-join objects count.
- **DML** verb nodes are not emitted.
- **Graph limit:** one edge per node pair. When a method both reads and writes a field, `writes_field` wins.
- **Tests:** Apex test classes are tagged `is_test` and kept in the graph. They're excluded from god nodes and hub community labels.

## Placement

- **Generic support goes to the fork** (`lemonsqweez/graphify`, branch `salesforce-sfdx`) and later becomes an upstream PR to `Graphify-Labs/graphify`. It lives in a standalone `graphify/salesforce_ingest.py` plus small hooks in `detect.py`, `extract.py`, `extractors/apex.py`, `analyze.py`, `cluster.py` and `affected.py`.
- **GivePower-specific pieces live in salesforce-crm:**
  - The data-dictionary script writes `graphify-out/wiki/objects/*.md` and `graphify-out/sf_dictionary.json`.
  - A refresh script plus a post-commit hook rebuild the graph. A failed rebuild leaves a visible marker.
  - Re-enable the graphify skill and add a CLAUDE.md rule.
  - Add docs from `docs/adr/` and `salesforce-engineering-docs/architecture/` only.
- **No MCP server.**

## Acceptance: golden questions

The user confirms the expected answers before the build. The draft answers come from reading the source directly.

1. **What writes `npe01__OppPayment__c.Revenue_Period__c` / `tccxero__Invoice_Line_Item__c.Revenue_Period__c`?**
   - Apex: ClosedLostWriteoffService, InvoiceRevenuePeriodLinker, MultiProgramInvoiceMatcher, PaymentPeriodRepointer, RecurringScheduleBuilder.
   - Plus any flows and DLRS found.
2. **What writes `Volume_Comm_Status__c`?** EmailService and PaymentScheduleLineHandler, plus any flows.
3. **What fires when an Opportunity saves?**
   - `dlrs_OpportunityTrigger`.
   - The record-triggered flows whose start object is Opportunity, with their trigger type.
   - The Opportunity validation rules.
4. **Which permission sets grant `npe01__OppPayment__c` fields?** The set is read from the permset XML.
5. **Which tests cover PaymentScheduleLineHandler?** BulkRecordServiceTest, InvoiceProcessingHandlerTest, PaymentScheduleLineHandlerTest and PaymentWorkspaceControllerTest.
