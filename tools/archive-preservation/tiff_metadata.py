"""Frozen metadata mappings for metadata-enriched preservation TIFFs."""

from __future__ import annotations

import re
from typing import Any


PROFILE_V1 = "iss-tiff-v1"
PROFILE_V2 = "iss-tiff-v2"
V2_MAPPING_VERSION = "iss-tiff-v2-workbook-context-2026-07-23"
SUPPORTED_PROFILES = (PROFILE_V1, PROFILE_V2)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IPTC_OBJECT_NAME_MAX_BYTES = 64
IPTC_BYLINE_MAX_BYTES = 32
IPTC_COPYRIGHT_MAX_BYTES = 128
IPTC_CREDIT_MAX_BYTES = 32


class TiffMetadataError(ValueError):
    """Raised when an embedding profile lacks its required provenance."""


def _text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    return str(value).strip() if value is not None else ""


def _required_text(mapping: dict[str, Any], key: str, label: str) -> str:
    value = _text(mapping, key)
    if not value:
        raise TiffMetadataError(f"{label} is required for TIFF embedding")
    return value


def _required_digest(
    mapping: dict[str, Any],
    key: str,
    label: str,
) -> str:
    value = _required_text(mapping, key, label)
    if not DIGEST_RE.fullmatch(value):
        raise TiffMetadataError(
            f"{label} must use sha256:<lowercase-hex> form"
        )
    return value


def _fits_iptc_limit(value: str, maximum_bytes: int) -> bool:
    """Return whether UTF-8 text fits one legacy IPTC byte limit."""

    return len(value.encode("utf-8")) <= maximum_bytes


def expected_v1(metadata: dict[str, Any]) -> dict[str, str]:
    """Return the frozen first-pilot mapping without changing its contract."""

    entity_id = _required_text(metadata, "entity_id", "ISS identifier")
    archival_label = _required_text(
        metadata,
        "archival_label",
        "archival label",
    )
    title = _required_text(metadata, "descriptive_title", "title")
    description = _required_text(metadata, "description", "description")
    material = _required_text(
        metadata,
        "material_technique",
        "material or technique",
    )
    dimensions = _required_text(metadata, "dimensions", "physical dimensions")
    physical = f"Material/Technik: {material}; Maße: {dimensions}"
    return {
        "XMP-dc:Identifier": entity_id,
        "XMP-dc:Title": title,
        "XMP-dc:Description": description,
        "IPTC:CodedCharacterSet": "UTF8",
        "IPTC:ObjectName": title,
        "IPTC:Caption-Abstract": description,
        "IFD0:ImageDescription": description,
        "XMP-iptcExt:ArtworkSourceInventoryNo": archival_label,
        "XMP-iptcExt:ArtworkTitle": title,
        "XMP-iptcExt:ArtworkContentDescription": description,
        "XMP-iptcExt:ArtworkPhysicalDescription": physical,
    }


def expected_v2(
    metadata: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, str]:
    """Return recovery-oriented V2 tags while omitting every unknown value."""

    entity_id = _required_text(metadata, "entity_id", "ISS identifier")
    archival_label = _required_text(
        metadata,
        "archival_label",
        "archival label",
    )
    case_id = _required_text(
        provenance,
        "case_id",
        "provenance restoration case ID",
    )
    source_file_id = _required_text(
        provenance,
        "source_file_id",
        "selected provenance source file ID",
    )
    source_digest = _required_digest(
        provenance,
        "source_digest",
        "selected source digest",
    )
    reviewed_digest = _required_digest(
        provenance,
        "reviewed_bundle_digest",
        "reviewed accession-bundle digest",
    )

    def accepted(key: str) -> str:
        return _text(metadata, key) or _text(provenance, key)

    title = accepted("descriptive_title")
    description = accepted("description")
    object_type = accepted("object_type")
    material = accepted("material_technique")
    dimensions = accepted("dimensions")
    creator = accepted("creator")
    date_created = accepted("date_created")
    rights_statement = accepted("rights_statement")
    rights_holder = accepted("rights_holder")
    usage_terms = accepted("rights_usage_terms")
    rights_url = accepted("rights_web_statement")
    credit_line = accepted("credit_line")
    keywords = accepted("keywords")
    coverage = accepted("coverage")
    contributor = accepted("contributor")
    source_collection = accepted("source_collection")
    previous_owner = accepted("previous_owner")
    image_reference = accepted("image_reference")
    certainty_context = accepted("certainty_context")
    workbook_source = accepted("workbook_source")
    workbook_sha256 = accepted("workbook_sha256")
    workbook_record_digest = accepted("workbook_record_digest")
    museum_digital_id = _text(provenance, "museum_digital_id")
    representation_role = _text(
        provenance,
        "representation_role",
    )
    if representation_role and representation_role not in {
        "preservation_master",
        "preservation_surrogate",
    }:
        raise TiffMetadataError(
            "unsupported TIFF preservation representation role"
        )
    raw_gaps = provenance.get("provenance_gaps") or []
    if not isinstance(raw_gaps, list) or any(
        not isinstance(gap, str) or not gap.strip()
        for gap in raw_gaps
    ):
        raise TiffMetadataError(
            "TIFF provenance gaps must be a list of non-empty text values"
        )
    provenance_gaps = [gap.strip() for gap in raw_gaps]

    source_statement = (
        f"ISS provenance restoration {case_id}; selected source "
        f"{source_file_id}; source {source_digest}; reviewed bundle "
        f"{reviewed_digest}"
    )
    if museum_digital_id:
        source_statement += f"; museum-digital object {museum_digital_id}"
    if representation_role:
        source_statement += f"; representation {representation_role}"
    if workbook_source:
        source_statement += f"; workbook {workbook_source}"
    if workbook_sha256:
        source_statement += f"; workbook file {workbook_sha256}"
    if source_collection:
        source_statement += f"; collection {source_collection}"
    if previous_owner:
        source_statement += f"; previous owner {previous_owner}"
    if image_reference:
        source_statement += f"; image reference {image_reference}"
    if certainty_context:
        source_statement += f"; confirmed workbook fields {certainty_context}"
    if provenance_gaps:
        source_statement += (
            "; provenance gaps " + ", ".join(provenance_gaps)
        )
    relations = (
        f"{case_id}; {source_digest}; {reviewed_digest}"
        + (
            f"; museum-digital:object:{museum_digital_id}"
            if museum_digital_id
            else ""
        )
    )
    if representation_role:
        relations += f"; iss:representation:{representation_role}"
    if workbook_record_digest:
        relations += f"; workbook-record:{workbook_record_digest}"
    for gap in provenance_gaps:
        relations += f"; iss:provenance-gap:{gap}"

    tags = {
        "XMP-dc:Identifier": entity_id,
        "XMP-iptcExt:ArtworkSourceInventoryNo": archival_label,
        "XMP-dc:Source": source_statement,
        "XMP-dc:Relation": relations,
    }
    iptc_text = False

    if title:
        tags.update(
            {
                "XMP-dc:Title": title,
                "XMP-iptcExt:ArtworkTitle": title,
            }
        )
        if _fits_iptc_limit(title, IPTC_OBJECT_NAME_MAX_BYTES):
            tags["IPTC:ObjectName"] = title
            iptc_text = True
    if description:
        tags.update(
            {
                "XMP-dc:Description": description,
                "IPTC:Caption-Abstract": description,
                "IFD0:ImageDescription": description,
                "XMP-iptcExt:ArtworkContentDescription": description,
            }
        )
        iptc_text = True
    if object_type:
        tags["XMP-dc:Type"] = object_type
    physical_parts = []
    if material:
        physical_parts.append(f"Material/Technik: {material}")
    if dimensions:
        physical_parts.append(f"Maße: {dimensions}")
    if physical_parts:
        tags["XMP-iptcExt:ArtworkPhysicalDescription"] = "; ".join(
            physical_parts
        )
    if creator:
        tags["XMP-dc:Creator"] = creator
        if _fits_iptc_limit(creator, IPTC_BYLINE_MAX_BYTES):
            tags["IPTC:By-line"] = creator
            iptc_text = True
    if date_created:
        tags["XMP-dc:Date"] = date_created
    if coverage:
        tags["XMP-dc:Coverage"] = coverage
    if contributor:
        tags["XMP-dc:Contributor"] = contributor
    if keywords:
        tags["XMP-dc:Subject"] = keywords

    rights_parts = []
    if rights_statement:
        rights_parts.append(rights_statement)
    if rights_holder:
        rights_parts.append(f"Rights holder: {rights_holder}")
    if rights_parts:
        rights_text = "; ".join(rights_parts)
        tags["XMP-dc:Rights"] = rights_text
        if _fits_iptc_limit(rights_text, IPTC_COPYRIGHT_MAX_BYTES):
            tags["IPTC:CopyrightNotice"] = rights_text
            iptc_text = True
    if usage_terms:
        tags["XMP-xmpRights:UsageTerms"] = usage_terms
    if rights_url:
        tags["XMP-xmpRights:WebStatement"] = rights_url
    if credit_line:
        tags["XMP-photoshop:Credit"] = credit_line
        if _fits_iptc_limit(credit_line, IPTC_CREDIT_MAX_BYTES):
            tags["IPTC:Credit"] = credit_line
            iptc_text = True
    if iptc_text:
        tags["IPTC:CodedCharacterSet"] = "UTF8"
    if any(not value for value in tags.values()):
        raise TiffMetadataError(
            "TIFF profile produced an empty metadata value"
        )
    return tags


def expected_tags(
    profile: str,
    metadata: dict[str, Any],
    provenance: dict[str, Any] | None = None,
) -> dict[str, str]:
    if profile == PROFILE_V1:
        return expected_v1(metadata)
    if profile == PROFILE_V2:
        return expected_v2(metadata, dict(provenance or {}))
    raise TiffMetadataError(f"unsupported TIFF embedding profile: {profile}")
