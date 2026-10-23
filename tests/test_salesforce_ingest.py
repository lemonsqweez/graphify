"""Salesforce SFDX metadata extraction and reference resolution."""
from __future__ import annotations

import shutil
from pathlib import Path

from graphify.build import build_from_json
from graphify.detect import FileType, classify_file
from graphify.extract import _get_extractor, extract
from graphify.ids import make_id
from graphify.salesforce_ingest import extract_salesforce_metadata, is_salesforce_metadata_path

SFDX_FIXTURE = Path(__file__).parent / "fixtures" / "sfdx"


def build_sfdx(tmp_path: Path, files: dict[str, str] | None = None):
    """Copy the SFDX fixture tree into tmp_path, add ``files`` (relpath -> text),
    run a full extract() and return (extraction dict, graph)."""
    root = tmp_path / "proj"
    shutil.copytree(SFDX_FIXTURE, root)
    for rel, text in (files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    ext = extract(paths, root=root, parallel=False)
    return ext, build_from_json(ext, directed=True)


def _by_label(ext, label):
    return [n for n in ext["nodes"] if n["label"].casefold() == label.casefold()]


def _edges(ext, relation):
    return [e for e in ext["edges"] if e["relation"] == relation]


def _one_file(rel):
    return SFDX_FIXTURE / "force-app" / "main" / "default" / rel


def test_classify_meta_xml_as_code():
    assert classify_file(Path("x/Account.object-meta.xml")) == FileType.CODE
    assert classify_file(Path("x/Foo.cls-meta.xml")) is None
    assert classify_file(Path("x/data.xml")) is None


def test_get_extractor_routes_meta_xml():
    assert _get_extractor(Path("a/Region__c.field-meta.xml")) is extract_salesforce_metadata


def test_object_and_field_nodes():
    obj = extract_salesforce_metadata(_one_file("objects/Account/Account.object-meta.xml"))
    node = next(n for n in obj["nodes"] if n["id"] == make_id("Account"))
    assert node["source_file"]
    fld = extract_salesforce_metadata(_one_file("objects/Account/fields/Region__c.field-meta.xml"))
    assert "Account.Region__c" in [n["label"] for n in fld["nodes"]]
    assert "has_field" in {e["relation"] for e in fld["edges"]}


def test_lookup_edge():
    r = extract_salesforce_metadata(
        _one_file("objects/npe01__OppPayment__c/fields/Revenue_Period__c.field-meta.xml"))
    targets = {e["target"] for e in r["edges"] if e["relation"] == "looks_up_to"}
    assert targets == {make_id("Revenue_Period__c")}


def test_permset_field_ref_is_stub():
    r = extract_salesforce_metadata(
        _one_file("permissionsets/Fundraising_Full_Access.permissionset-meta.xml"))
    stub = next(n for n in r["nodes"] if n["label"] == "Account.Region__c")
    assert stub["source_file"] == ""
    edge = next(e for e in r["edges"] if e["relation"] == "grants_fls")
    assert edge["readable"] is True and edge["editable"] is False


def test_full_extract_merges_field(tmp_path):
    ext, _ = build_sfdx(tmp_path)
    fields = _by_label(ext, "Account.Region__c")
    assert len(fields) == 1 and fields[0]["source_file"]
    assert len(_by_label(ext, "Account")) == 1
    grant = _edges(ext, "grants_fls")
    assert [e["target"] for e in grant] == [fields[0]["id"]]


def test_undefined_object_single_node(tmp_path):
    ext, _ = build_sfdx(tmp_path)
    assert len(_by_label(ext, "Contact")) == 1


def test_field_stub_never_binds_object(tmp_path):
    ext, _ = build_sfdx(tmp_path)
    ids = {n["id"] for n in _by_label(ext, "Revenue_Period__c")}
    ids |= {n["id"] for n in _by_label(ext, "npe01__OppPayment__c.Revenue_Period__c")}
    ids |= {n["id"] for n in _by_label(ext, "tccxero__Invoice_Line_Item__c.Revenue_Period__c")}
    assert len(ids) == 3


def _apex_class(body: str) -> str:
    return "public class Svc {\n  public void run() {\n" + body + "\n  }\n}\n"


def _svc_edges(ext, relation):
    """Edges of ``relation`` produced by the Svc.cls Apex class (not by other fixtures)."""
    return [e for e in _edges(ext, relation) if str(e.get("source_file", "")).endswith("Svc.cls")]


def test_unresolved_field_unique_binds(tmp_path):
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/Svc.cls": _apex_class("    Object x = rec.Region__c;"),
    })
    real = _by_label(ext, "Account.Region__c")
    assert len(real) == 1 and real[0]["source_file"]
    assert [e["target"] for e in _svc_edges(ext, "reads_field")] == [real[0]["id"]]
    assert not _by_label(ext, "*.Region__c")


def test_unresolved_field_ambiguous_dropped(tmp_path):
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/Svc.cls": _apex_class("    rec.Revenue_Period__c = x;"),
    })
    assert not _by_label(ext, "*.Revenue_Period__c")
    obj_id = _by_label(ext, "Revenue_Period__c")[0]["id"]
    assert not [e for e in _svc_edges(ext, "writes_field") if e["target"] == obj_id]
    assert not _svc_edges(ext, "writes_field")


def _flow():
    return extract_salesforce_metadata(_one_file("flows/Stamp_Region.flow-meta.xml"))


def _rel_targets(r, relation, source=None):
    return {e["target"] for e in r["edges"]
            if e["relation"] == relation and (source is None or e["source"] == source)}


def test_flow_trigger_attrs():
    r = _flow()
    flow = next(n for n in r["nodes"] if n["id"] == make_id("flow", "Stamp_Region"))
    assert flow["trigger_type"] == "RecordAfterSave"
    assert flow["record_trigger_type"] == "CreateAndUpdate"
    assert flow["process_type"] == "AutoLaunchedFlow"
    edge = next(e for e in r["edges"] if e["relation"] == "triggers_on")
    assert edge["target"] == make_id("Opportunity")
    assert edge["trigger_type"] == "RecordAfterSave"
    assert edge["record_trigger_type"] == "CreateAndUpdate"


def test_flow_writes_explicit_object():
    assert make_id("Account", "Region__c") in _rel_targets(_flow(), "writes_field")


def test_flow_writes_via_variable():
    assert make_id("npe01__OppPayment__c", "Revenue_Period__c") in _rel_targets(
        _flow(), "writes_field")


def test_flow_reads_lookup_filter():
    r = _flow()
    reads = _rel_targets(r, "reads_field")
    assert make_id("npe01__OppPayment__c", "Status__c") in reads
    assert make_id("npe01__OppPayment__c", "Name") in reads


def test_flow_split_tag_parsed():
    assert make_id("Opportunity", "StageName") in _rel_targets(_flow(), "reads_field")


def _meta(rel):
    return extract_salesforce_metadata(_one_file(rel))


def test_record_type_node_and_picklists():
    r = _meta("objects/Opportunity/recordTypes/Volume_Recurring.recordType-meta.xml")
    rt = next(n for n in r["nodes"] if n["id"] == make_id("Opportunity", "rt", "Volume_Recurring"))
    assert rt["label"] == "Opportunity.Volume_Recurring"
    assert rt["picklists"] == {"StageName": ["Pledged", "Closed Won"]}
    assert rt["business_process"] == "Volume_Process"
    assert any(e["relation"] == "has_record_type" and e["target"] == rt["id"] for e in r["edges"])


def test_permset_grants_record_type():
    r = _meta("permissionsets/Fundraising_Full_Access.permissionset-meta.xml")
    assert make_id("Opportunity", "rt", "Volume_Recurring") in _rel_targets(r, "grants_record_type")


def test_permset_grants_apex_folds_to_class(tmp_path):
    cls = ("public class PaymentWorkspaceController {\n"
           "  public static void getWorkspace() {}\n}\n")
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/PaymentWorkspaceController.cls": cls})
    classes = _by_label(ext, "PaymentWorkspaceController")
    assert len(classes) == 1 and classes[0]["source_file"].endswith(".cls")
    assert [e["target"] for e in _edges(ext, "grants_apex")] == [classes[0]["id"]]


def test_dlrs_rollup_edges():
    r = _meta("customMetadata/dlrs__LookupRollupSummary2.Acct_Lifetime.md-meta.xml")
    roll = next(n for n in r["nodes"] if n["id"] == make_id("rollup", "Acct_Lifetime"))
    assert roll["label"] == "Acct_Lifetime"
    assert roll["active"] is True and roll["operation"] == "Sum" and roll["criteria"] is None
    assert _rel_targets(r, "reads_field") == {
        make_id("tccxero__Invoice_Line_Item__c", "tccxero__Total_Amount__c")}
    assert _rel_targets(r, "writes_field") == {
        make_id("Account", "Total_Lifetime_Giving_Invoiced__c")}
    assert make_id("tccxero__Invoice_Line_Item__c", "Account__c") in _rel_targets(r, "references")


def test_other_custom_metadata_ignored():
    assert is_salesforce_metadata_path(Path("Xero_Program_Mapping.Code_4010.md-meta.xml")) is False
    assert is_salesforce_metadata_path(
        Path("dlrs__LookupRollupSummary2.Acct_Lifetime.md-meta.xml")) is True


def _labels_of_targets(r, relation, source):
    by_id = {n["id"]: n["label"] for n in r["nodes"]}
    return {by_id[e["target"]] for e in r["edges"]
            if e["relation"] == relation and e["source"] == source}


_REGION_LABEL = make_id("Account", "Region_Label__c")


def test_formula_reads_same_object_field():
    r = _meta("objects/Account/fields/Region_Label__c.field-meta.xml")
    assert make_id("Account", "Region__c") in _rel_targets(r, "reads_field", _REGION_LABEL)


def test_formula_relationship_hop(tmp_path):
    ext, _ = build_sfdx(tmp_path)
    region = _by_label(ext, "Account.Region__c")
    assert len(region) == 1 and region[0]["source_file"]
    label_field = _by_label(ext, "Account.Region_Label__c")[0]
    reads = {e["target"] for e in _edges(ext, "reads_field") if e["source"] == label_field["id"]}
    assert region[0]["id"] in reads
    assert not _by_label(ext, "Account.Parent__r.Region__c")


def test_formula_ignores_globals_strings_comments():
    r = _meta("objects/Account/fields/Region_Label__c.field-meta.xml")
    labels = _labels_of_targets(r, "reads_field", _REGION_LABEL)
    assert labels, "formula should reference fields"
    for bad in ("$User", "West", "c", "Id"):
        assert not [lab for lab in labels if bad in lab.split(".")], (bad, labels)


def test_validation_rule_reads_field():
    r = _meta("objects/Account/validationRules/Region_Required.validationRule-meta.xml")
    rule = make_id("Account", "vr", "Region_Required")
    assert _rel_targets(r, "reads_field", rule) == {make_id("Account", "Region__c")}


_PWC_CLS = ("public class PaymentWorkspaceController {\n"
            "  @AuraEnabled\n  public static void getWorkspace() {}\n}\n")
_PWC_JS = ('import getWorkspace from "@salesforce/apex/PaymentWorkspaceController.getWorkspace";\n'
           "export default class Pw {}\n")
_PWC_TEST_JS = ('import saveVolume from "@salesforce/apex/PaymentWorkspaceController.saveVolume";\n'
                "export const x = saveVolume;\n")


def test_lwc_calls_apex_method(tmp_path):
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/PaymentWorkspaceController.cls": _PWC_CLS,
        "force-app/main/default/lwc/pw/pw.js": _PWC_JS,
    })
    by_id = {n["id"]: n for n in ext["nodes"]}
    calls = _edges(ext, "calls_apex")
    assert len(calls) == 1
    assert by_id[calls[0]["target"]]["label"] == ".getWorkspace()"
    assert by_id[calls[0]["source"]]["label"] == "pw.js"
    assert calls[0]["confidence"] == "EXTRACTED"


def test_lwc_tests_dir_ignored(tmp_path):
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/PaymentWorkspaceController.cls": _PWC_CLS,
        "force-app/main/default/lwc/pw/__tests__/pw.test.js": _PWC_TEST_JS,
    })
    assert not _edges(ext, "calls_apex")


def test_lwc_unknown_method_falls_back_to_class(tmp_path):
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/PaymentWorkspaceController.cls": _PWC_CLS,
        "force-app/main/default/lwc/pw/pw.js": _PWC_TEST_JS,
    })
    by_id = {n["id"]: n for n in ext["nodes"]}
    calls = _edges(ext, "calls_apex")
    assert [by_id[e["target"]]["label"] for e in calls] == ["PaymentWorkspaceController"]


_HANDLER_CLS = "public class Handler {\n  public static void run() {}\n}\n"
_HANDLER_TEST_CLS = (
    "@isTest\nprivate class HandlerTest {\n"
    "  @isTest\n  static void t() {\n    Handler.run();\n    System.assert(true);\n"
    "    Account a = new Account(Name = 'x');\n  }\n}\n")


def test_tests_edge_resolves_to_class(tmp_path):
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/Handler.cls": _HANDLER_CLS,
        "force-app/main/default/classes/HandlerTest.cls": _HANDLER_TEST_CLS,
    })
    handler = [n for n in _by_label(ext, "Handler") if n["source_file"].endswith("Handler.cls")]
    assert len(handler) == 1
    tests = _edges(ext, "tests")
    assert [e["target"] for e in tests] == [handler[0]["id"]]
    assert not _by_label(ext, "System")


def test_tests_edge_static_field_access_resolves(tmp_path):
    flag_test = ("@isTest\nprivate class FlagTest {\n  @isTest\n  static void t() {\n"
                 "    Handler.migrationMode = true;\n  }\n}\n")
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/Handler.cls": _HANDLER_CLS,
        "force-app/main/default/classes/FlagTest.cls": flag_test,
    })
    handler = [n for n in _by_label(ext, "Handler") if n["source_file"].endswith("Handler.cls")]
    assert [e["target"] for e in _edges(ext, "tests")] == [handler[0]["id"]]


def test_validation_rule_and_flow_active_attrs():
    rule = _meta("objects/Account/validationRules/Region_Required.validationRule-meta.xml")
    assert next(n for n in rule["nodes"] if n["id"] == make_id("Account", "vr", "Region_Required"))["active"] is True
    legacy = _meta("objects/Account/validationRules/Legacy_Rule.validationRule-meta.xml")
    assert next(n for n in legacy["nodes"] if n["id"] == make_id("Account", "vr", "Legacy_Rule"))["active"] is False
    flow = next(n for n in _flow()["nodes"] if n["id"] == make_id("flow", "Stamp_Region"))
    assert flow["status"] == "Active" and flow["active"] is True


def test_affected_includes_sf_relations():
    from graphify.affected import DEFAULT_AFFECTED_RELATIONS
    for rel in ("queries", "writes_field", "reads_field", "triggers_on", "has_field",
                "looks_up_to", "master_detail_to", "calls_apex", "tests"):
        assert rel in DEFAULT_AFFECTED_RELATIONS, rel
