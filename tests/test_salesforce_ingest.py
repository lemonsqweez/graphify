"""Salesforce SFDX metadata extraction and reference resolution."""
from __future__ import annotations

import shutil
from pathlib import Path

from graphify.build import build_from_json
from graphify.detect import FileType, classify_file
from graphify.extract import _get_extractor, extract
from graphify.ids import make_id
from graphify.salesforce_ingest import extract_salesforce_metadata

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


def test_unresolved_field_unique_binds(tmp_path):
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/Svc.cls": _apex_class("    Object x = rec.Region__c;"),
    })
    real = _by_label(ext, "Account.Region__c")
    assert len(real) == 1 and real[0]["source_file"]
    assert [e["target"] for e in _edges(ext, "reads_field")] == [real[0]["id"]]
    assert not _by_label(ext, "*.Region__c")


def test_unresolved_field_ambiguous_dropped(tmp_path):
    ext, _ = build_sfdx(tmp_path, {
        "force-app/main/default/classes/Svc.cls": _apex_class("    rec.Revenue_Period__c = x;"),
    })
    assert not _by_label(ext, "*.Revenue_Period__c")
    obj_id = _by_label(ext, "Revenue_Period__c")[0]["id"]
    assert not [e for e in _edges(ext, "writes_field") if e["target"] == obj_id]
    assert not _edges(ext, "writes_field")
