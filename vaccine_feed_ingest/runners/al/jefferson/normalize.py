#!/usr/bin/env python

import json
import pathlib
import re
import sys
from hashlib import md5
from typing import Dict, List, Optional, Text

from vaccine_feed_ingest_schema import location as schema

from vaccine_feed_ingest.utils.log import getLogger
from vaccine_feed_ingest.utils.normalize import (
    normalize_phone,
    normalize_url,
    normalize_zip,
)

logger = getLogger(__file__)

# Regexes to match different pieces of vaccine site info.
_STREET_ADDRESS_REGEX_STRING = r"(\d+\s+[A-Za-z0-9# .,-]+)"
_STREET_ADDRESS_REGEX = re.compile(r"^" + _STREET_ADDRESS_REGEX_STRING + r"$")
# Handle AL/Al, and variation in spacing and commas.
_CITY_STATE_ZIP_REGEX_STRING = r"([A-Za-z0-9 -]+)\s*,?\s*AL,?\s*([\d-]+)"
_CITY_STATE_ZIP_REGEX = re.compile(
    r"^" + _CITY_STATE_ZIP_REGEX_STRING + r"$", re.IGNORECASE
)
_PHONE_NUMBER_REGEX = re.compile(r"\(?\d+\)?\s*\d+[ -]?\d+")
# An address in a single string: street address, city, state, zip
_COMBINED_ADDRESS_REGEX = re.compile(
    r"^" + _STREET_ADDRESS_REGEX_STRING + r",\s+" + _CITY_STATE_ZIP_REGEX_STRING + r"$",
    re.IGNORECASE,
)
_DROP_IN_REGEX = re.compile(r"No appointments? necessary.*")
_APPOINTMENTS_REGEX = re.compile(r"Make an appointment here.*")
# Regex matching the names of entries to be ignored entirely.
_IGNORE_REGEX = re.compile(
    r"(Alabama Department of Public Health \(ADPH\)|Centers for Disease Control \(CDC\)):?"
)
# Regex matching text that should not be used as the site name,
# although we want to include the site itself.
_IGNORE_NAME_REGEX = re.compile(r".*Locations to be determined.*")


def _make_placeholder_location(entry: dict) -> schema.NormalizedLocation:
    """Returns a normalized location with a placeholder ID,
    the given `entry` as source data, and all other fields empty."""
    source = _make_placeholder_source(entry)
    return schema.NormalizedLocation(id=_make_site_id(source), source=source)


def _start_new_site(
    current_site: schema.NormalizedLocation,
    sites: List[schema.NormalizedLocation],
    entry: dict,
) -> schema.NormalizedLocation:
    """Adds `current_site` to `sites` and returns a fresh site object."""
    sites.append(current_site)
    logger.debug("Recording current site and starting a new one: %s", current_site)
    return _make_placeholder_location(entry)


def _add_id(site: schema.NormalizedLocation) -> None:
    """Generates source and site IDs for the given `site` object
    and updates the object in place.
    """
    # We don't have a stable site ID or name from the source document,
    # so generate one ID by hashing whatever name and address info we do have.
    # These are likely to be more stable than the phone or website info.
    # Avoid using the `page` and `provider` numbers from `entry`,
    # because those are sensitive to layout changes in the source document.
    candidate_data: List[Optional[Text]] = list(
        filter(
            None,
            [
                site.name,
                getattr(site.address, "street1", None),
                getattr(site.address, "city", None),
                getattr(site.address, "state", None),
                getattr(site.address, "zip", None),
            ],
        )
    )
    # Fall back to website or phone info if we don't have concrete name or location info.
    if not candidate_data:
        candidate_data.extend([c.website or c.phone for c in site.contact])

    site.source.id = _md5_hash(candidate_data)
    site_id = _make_site_id(site.source)
    logger.debug("Site ID: %s", site_id)
    site.id = site_id


_URL_HOST_TO_PROVIDER: Dict[Text, schema.VaccineProvider] = {
    "www.cvs.com": schema.VaccineProvider.CVS,
    "www.samsclub.com": schema.VaccineProvider.SAMS,
    "www.walmart.com": schema.VaccineProvider.WALMART,
    "www.winndixie.com": schema.VaccineProvider.WINN_DIXIE,
}


def _lookup_provider(website: schema.Contact) -> Optional[schema.Organization]:
    """Gets the vaccine provider for the given website, if known."""
    url = website.website
    provider = _URL_HOST_TO_PROVIDER.get(url.host, None) if url else None
    return schema.Organization(id=provider) if provider else None


def _add_website_and_provider(site: schema.NormalizedLocation, entry: dict) -> None:
    """Adds website and provider information from `entry`, if any,
    to the given `site` object."""
    # Create a fresh object each time, though many sites may have the same website.
    website = _make_website_contact(entry["link"])
    if website is not None:
        site.contact = site.contact or []
        site.contact.append(website)
        # Try to work out well-known providers from the URL.
        site.parent_organization = _lookup_provider(website)


def normalize(entry: dict) -> List[schema.NormalizedLocation]:
    """Gets a list of normalized vaccine site objects from a single parsed JSON entry."""
    details = entry.get("details", [])
    # The details list can be in one of the following forms,
    # and may contain info about multiple sites:
    # [combined address, optional phone]+
    # [optional name, street address, city state zip, optional phone]+
    # The parsed JSON has relatively little information about
    # provider names, because these are images in the original document.

    # Process each detail, maintaining a current site (whose details we update)
    # and a running list of processed sites.
    sites: List[schema.NormalizedLocation] = []
    current_site: schema.NormalizedLocation = _make_placeholder_location(entry)
    for detail in details:
        # Trim whitespace and commas
        detail = detail.strip(" \t\n\r,")
        # Might be the entire address
        if (combined_match := _COMBINED_ADDRESS_REGEX.match(detail)) is not None:
            [street_address, city, zip] = combined_match.groups()[0:3]
            logger.debug(
                "One-line address '%s' split into address: '%s' city: '%s' zip: '%s'",
                detail,
                street_address,
                city,
                zip,
            )
            if current_site.address:
                current_site = _start_new_site(current_site, sites, entry)
            current_site.address = schema.Address(
                street1=street_address, city=city, state="AL", zip=normalize_zip(zip)
            )
        # Or one component of site info
        elif _STREET_ADDRESS_REGEX.match(detail) is not None:
            logger.debug("Street address: %s", detail)
            if current_site.address and current_site.address.street1:
                current_site = _start_new_site(current_site, sites, entry)
            current_site.address = current_site.address or schema.Address(state="AL")
            current_site.address.street1 = detail
        elif (city_state_zip_match := _CITY_STATE_ZIP_REGEX.match(detail)) is not None:
            logger.debug("City, state, zip: %s", detail)
            # Assume the state is always AL
            [city, zip] = city_state_zip_match.groups()[0:2]
            if current_site.address and (
                current_site.address.city or current_site.address.zip
            ):
                current_site = _start_new_site(current_site, sites, entry)
            current_site.address = current_site.address or schema.Address(state="AL")
            current_site.address.city = city
            current_site.address.zip = normalize_zip(zip)
        elif _PHONE_NUMBER_REGEX.match(detail) is not None:
            logger.debug("Phone %s", detail)
            current_site.contact = current_site.contact or []
            current_site.contact.extend(normalize_phone(detail, contact_type="booking"))
            # Usually the last detail, so start a new site object
            current_site = _start_new_site(current_site, sites, entry)
        elif _DROP_IN_REGEX.match(detail) is not None:
            logger.debug("Availability = drop in: %s", detail)
            current_site.availability = (
                current_site.availability or schema.Availability()
            )
            current_site.availability.drop_in = True
            # Usually the last detail, so start a new site object
            current_site = _start_new_site(current_site, sites, entry)
        elif _APPOINTMENTS_REGEX.match(detail) is not None:
            logger.debug("Availability = appt: %s", detail)
            current_site.availability = (
                current_site.availability or schema.Availability()
            )
            current_site.availability.appointments = True
            # Usually the last detail, so start a new site object
            current_site = _start_new_site(current_site, sites, entry)
        elif _IGNORE_REGEX.match(detail) is not None:
            # Ignore these entries entirely, and do not add to sites.
            # These are usually the Dept of Public Health or CDC website links.
            logger.debug("Ignoring generic entry: %s", detail)
            current_site = _make_placeholder_location(entry)
        elif _IGNORE_NAME_REGEX.match(detail) is not None or current_site.name:
            # Keep the site, but don't treat this string as the name.
            logger.debug("Additional note: %s", detail)
            current_site.notes = current_site.notes or []
            current_site.notes.append(detail)
        else:
            # Everything else gets treated as the site name, if we don't already have one.
            logger.debug("Site name: %s", detail)
            current_site.name = current_site.name or detail
    # Include the last processed site.
    if (
        current_site.address
        or current_site.availability
        or current_site.name
        or current_site.contact
    ):
        sites.append(current_site)

    for site in sites:
        # Add the website and provider name, if we have it.
        _add_website_and_provider(site, entry)
        # Generate real IDs, now we have all the site information.
        _add_id(site)
    return sites


_SOURCE_NAME = "al_jefferson"


def _make_placeholder_source(entry: dict) -> schema.Source:
    """Returns a `schema.Source` object referring to the original `entry` data,
    with placeholder ID information."""
    return schema.Source(
        source=_SOURCE_NAME,
        id="PLACEHOLDER",
        fetched_from_uri=entry.get("fetched_from_uri", None),
        fetched_at=None,
        published_at=entry.get("published_at", None),
        data=entry,
    )


def _make_site_id(source: schema.Source) -> Text:
    """Returns a site ID compatible with `source`, according to the schema validation rules."""
    return f"{source.source}:{source.id}"


def _md5_hash(inputs: List[Optional[Text]]) -> Text:
    """Generates an md5 checksum from the truthy inputs."""
    return md5("".join(filter(None, inputs)).encode("utf-8")).hexdigest()


def _make_website_contact(url: Optional[Text]) -> Optional[schema.Contact]:
    """Returns a `schema.Contact` object for the given booking URL, if any."""
    normalized_url = normalize_url(url)
    if normalized_url:
        return schema.Contact(contact_type="booking", website=normalized_url)
    return None


def main():
    output_dir = pathlib.Path(sys.argv[1])
    input_dir = pathlib.Path(sys.argv[2])

    json_filepaths = input_dir.glob("*.ndjson")

    for in_filepath in json_filepaths:
        filename = in_filepath.name.split(".", maxsplit=1)[0]
        out_filepath = output_dir / f"{filename}.normalized.ndjson"
        logger.info(
            "Normalizing: %s => %s",
            in_filepath,
            out_filepath,
        )

        with in_filepath.open() as fin:
            with out_filepath.open("w") as fout:
                locations: Dict[Text, schema.NormalizedLocation] = dict()
                for site_json in fin:
                    parsed_site = json.loads(site_json)
                    # One line of parsed json may describe
                    # multiple vaccine sites after normalization.
                    normalized_sites = normalize(parsed_site)
                    if not normalized_sites:
                        # These entries are usually Dept of Public Health
                        # or CDC website links, not info about vaccine sites.
                        logger.info("Entry has no vaccine site info: %s", parsed_site)
                    for normalized_site in normalized_sites:
                        # Handle duplicates. These may come from the
                        # English and Spanish sections of the same document,
                        # which mostly list the same sites.
                        existing = locations.get(normalized_site.id, None)
                        if existing:
                            # If we've seen this site before, make sure the content is identical.
                            # Don't compare the `source` fields:
                            # they will always have different document locations.
                            existing_data = existing.dict(
                                exclude_none=True,
                                exclude_unset=True,
                                exclude={"source"},
                            )
                            new_data = normalized_site.dict(
                                exclude_none=True,
                                exclude_unset=True,
                                exclude={"source"},
                            )
                            if existing_data != new_data:
                                logger.warning(
                                    "Found different locations with the ID %s, ignoring the second:\n%s\n%s",
                                    normalized_site.id,
                                    existing_data,
                                    new_data,
                                )
                            else:
                                # Not a problem, but useful to know while developing.
                                logger.debug(
                                    "Found duplicates of site with ID %s, will record only one",
                                    normalized_site.id,
                                )
                        else:
                            locations[normalized_site.id] = normalized_site
                            json.dump(normalized_site.dict(exclude_unset=True), fout)
                            fout.write("\n")


if __name__ == "__main__":
    main()
