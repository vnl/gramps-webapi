#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2020      David Straub
# Copyright (C) 2020      Christopher Horn
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Gramps utility functions."""


import json
from hashlib import sha256
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple, Type, Union

import gramps
import jsonschema
from flask import abort, current_app
from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.db import KEY_TO_CLASS_MAP, DbTxn
from gramps.gen.db.base import DbReadBase, DbWriteBase
from gramps.gen.db.dbconst import TXNADD, TXNDEL, TXNUPD
from gramps.gen.display.name import NameDisplay
from gramps.gen.display.place import PlaceDisplay
from gramps.gen.errors import HandleError
from gramps.gen.lib import (
    Citation,
    Event,
    Family,
    GrampsType,
    Media,
    Note,
    Person,
    Place,
    PlaceType,
    Repository,
    Source,
    Span,
    Tag,
)
from gramps.gen.lib.primaryobj import BasicPrimaryObject as GrampsObject
from gramps.gen.lib.serialize import from_json, to_json
from gramps.gen.soundex import soundex
from gramps.gen.utils.db import (
    get_birth_or_fallback,
    get_death_or_fallback,
    get_divorce_or_fallback,
    get_marriage_or_fallback,
    get_participant_from_event,
)
from gramps.gen.utils.grampslocale import GrampsLocale
from gramps.gen.utils.id import create_id
from gramps.gen.utils.place import conv_lat_lon

from ...const import SEX_FEMALE, SEX_MALE, SEX_UNKNOWN
from ...types import Handle
from ..media import MediaHandler

pd = PlaceDisplay()
_ = glocale.translation.gettext


def get_person_by_handle(db_handle: DbReadBase, handle: Handle) -> Union[Person, Dict]:
    """Safe get person by handle."""
    try:
        return db_handle.get_person_from_handle(handle)
    except HandleError:
        return {}


def get_place_by_handle(db_handle: DbReadBase, handle: Handle) -> Union[Place, Dict]:
    """Safe get place by handle."""
    try:
        return db_handle.get_place_from_handle(handle)
    except HandleError:
        return {}


def get_family_by_handle(
    db_handle: DbReadBase, handle: Handle, args: Optional[Dict] = None
) -> Union[Family, Dict]:
    """Get a family and optional extended attributes."""
    try:
        obj = db_handle.get_family_from_handle(handle)
    except HandleError:
        return {}
    args = args or {}
    if "extend" in args:
        obj.extended = get_extended_attributes(db_handle, obj, args)
        if "all" in args["extend"] or "father" in args["extend"]:
            obj.extended["father"] = get_person_by_handle(db_handle, obj.father_handle)
        if "all" in args["extend"] or "mother" in args["extend"]:
            obj.extended["mother"] = get_person_by_handle(db_handle, obj.mother_handle)
    return obj


def get_source_by_handle(
    db_handle: DbReadBase, handle: Handle, args: Optional[Dict] = None
) -> Source:
    """Get a source and optional extended attributes."""
    args = args or {}
    obj = db_handle.get_source_from_handle(handle)
    if "extend" in args:
        obj.extended = get_extended_attributes(db_handle, obj, args)
    return obj


def get_sex_profile(person: Person) -> str:
    """Get character substitution for enumerated sex."""
    if person.gender == person.MALE:
        return SEX_MALE
    if person.gender == person.FEMALE:
        return SEX_FEMALE
    return SEX_UNKNOWN


def get_event_participants_for_handle(
    db_handle: DbReadBase,
    handle: Handle,
    locale: GrampsLocale = glocale,
) -> Dict:
    """Get event participants given a handle."""
    result = {"people": [], "families": []}
    seen = set()  # to avoid duplicates
    for class_name, backref_handle in db_handle.find_backlink_handles(
        handle, include_classes=["Person", "Family"]
    ):
        if backref_handle in seen:
            continue
        seen.add(backref_handle)
        if class_name == "Person":
            person = db_handle.get_person_from_handle(backref_handle)
            if not person:
                continue
            for event_ref in person.get_event_ref_list():
                if handle == event_ref.ref:
                    result["people"].append(
                        {
                            "role": locale.translation.sgettext(
                                event_ref.get_role().xml_str()
                            ),
                            "person": get_person_profile_for_handle(
                                db_handle, backref_handle, args=[], locale=locale
                            ),
                        }
                    )
        elif class_name == "Family":
            family = db_handle.get_family_from_handle(backref_handle)
            if not family:
                continue
            for event_ref in family.get_event_ref_list():
                if handle == event_ref.ref:
                    result["families"].append(
                        {
                            "role": locale.translation.sgettext(
                                event_ref.get_role().xml_str()
                            ),
                            "family": get_family_profile_for_handle(
                                db_handle, backref_handle, args=[], locale=locale
                            ),
                        }
                    )
    return result


def get_event_summary_from_object(
    db_handle: DbReadBase, event: Event, locale: GrampsLocale = glocale
):
    """Get a summary of an Event."""
    handle = event.get_handle()
    participant = get_participant_from_event(db_handle, handle)
    event_type = locale.translation.sgettext(event.type.xml_str())
    if not participant:
        return event_type
    return f"{event_type} - {participant}"


def get_event_profile_for_object(
    db_handle: DbReadBase,
    event: Event,
    args: List,
    base_event: Union[Event, None] = None,
    label: str = "span",
    locale: GrampsLocale = glocale,
    role: Optional[str] = None,
) -> Dict:
    """Get event profile given an Event."""
    result = {
        "type": locale.translation.sgettext(event.type.xml_str()),
        "date": locale.date_displayer.display(event.date),
        "place": pd.display_event(db_handle, event),
        "summary": get_event_summary_from_object(db_handle, event, locale=locale),
    }
    if role is not None:
        result["role"] = role
    if "all" in args or "participants" in args:
        result["participants"] = get_event_participants_for_handle(
            db_handle, event.handle, locale=locale
        )
    if "all" in args or "ratings" in args:
        count, confidence = get_rating(db_handle, event)
        result["citations"] = count
        result["confidence"] = confidence
    if base_event is not None:
        result[label] = (
            Span(base_event.date, event.date)
            .format(precision=3, dlocale=locale)
            .strip("()")
        )
    return result


def get_event_profile_for_handle(
    db_handle: DbReadBase,
    handle: Handle,
    args: List,
    base_event: Union[Event, None] = None,
    label: str = "span",
    locale: GrampsLocale = glocale,
    role: Optional[str] = None,
) -> Dict:
    """Get event profile given a handle."""
    try:
        obj = db_handle.get_event_from_handle(handle)
    except HandleError:
        return {}
    return get_event_profile_for_object(
        db_handle,
        obj,
        args=args,
        base_event=base_event,
        label=label,
        locale=locale,
        role=role,
    )


def get_birth_profile(
    db_handle: DbReadBase,
    person: Person,
    args: Union[List, None] = None,
    locale: GrampsLocale = glocale,
) -> Tuple[Dict, Union[Event, None]]:
    """Return best available birth information for a person."""
    event = get_birth_or_fallback(db_handle, person)
    if event is None:
        return {}, None
    args = args or []
    return (
        get_event_profile_for_object(db_handle, event, args=args, locale=locale),
        event,
    )


def get_death_profile(
    db_handle: DbReadBase,
    person: Person,
    args: Union[List, None] = None,
    locale: GrampsLocale = glocale,
) -> Tuple[Dict, Union[Event, None]]:
    """Return best available death information for a person."""
    event = get_death_or_fallback(db_handle, person)
    if event is None:
        return {}, None
    args = args or []
    return (
        get_event_profile_for_object(db_handle, event, args=args, locale=locale),
        event,
    )


def get_marriage_profile(
    db_handle: DbReadBase,
    family: Family,
    args: Union[List, None] = None,
    locale: GrampsLocale = glocale,
) -> Tuple[Dict, Union[Event, None]]:
    """Return best available marriage information for a couple."""
    event = get_marriage_or_fallback(db_handle, family)
    if event is None:
        return {}, None
    args = args or []
    return (
        get_event_profile_for_object(db_handle, event, args=args, locale=locale),
        event,
    )


def get_divorce_profile(
    db_handle: DbReadBase,
    family: Family,
    args: Union[List, None] = None,
    locale: GrampsLocale = glocale,
) -> Tuple[Dict, Union[Event, None]]:
    """Return best available divorce information for a couple."""
    event = get_divorce_or_fallback(db_handle, family)
    if event is None:
        return {}, None
    args = args or {}
    return (
        get_event_profile_for_object(db_handle, event, args=args, locale=locale),
        event,
    )


def _format_place_type(
    place_type: PlaceType, locale: GrampsLocale = glocale
) -> Dict[str, Any]:
    """Format a place type."""
    return locale.translation.sgettext(place_type.xml_str())


def get_place_profile_for_object(
    db_handle: DbReadBase,
    place: Place,
    locale: GrampsLocale = glocale,
    parent_places: bool = True,
) -> Dict[str, Any]:
    """Get place profile given a Place."""
    latitude, longitude = conv_lat_lon(place.lat, place.long, format="D.D8")
    profile = {
        "gramps_id": place.gramps_id,
        "type": _format_place_type(place.get_type(), locale=locale),
        "name": place.get_name().value,
        "alternate_names": [
            place_name.value for place_name in place.get_alternative_names()
        ],
        "lat": float(latitude) if (latitude and longitude) else None,
        "long": float(longitude) if (latitude and longitude) else None,
    }
    if parent_places:
        parent_places_handles = []
        _place = place
        handle = None
        while True:
            for placeref in _place.get_placeref_list():
                handle = placeref.ref
                break
            if handle is None or handle in parent_places_handles:
                break
            _place = db_handle.get_place_from_handle(handle)
            if _place is None:
                break
            parent_places_handles.append(handle)
        profile["parent_places"] = [
            get_place_profile_for_object(
                db_handle=db_handle,
                place=db_handle.get_place_from_handle(parent_place),
                locale=locale,
                parent_places=False,
            )
            for parent_place in parent_places_handles
        ]
    return profile


def get_place_profile_for_handle(
    db_handle: DbReadBase,
    handle: Handle,
    locale: GrampsLocale = glocale,
    parent_places: bool = True,
) -> Union[Media, Dict]:
    """Get place profile given a handle."""
    obj = get_place_by_handle(db_handle, handle)
    return get_place_profile_for_object(
        db_handle, obj, locale=locale, parent_places=parent_places
    )


def get_person_profile_for_object(
    db_handle: DbReadBase, person: Person, args: List, locale: GrampsLocale = glocale
) -> Person:
    """Get person profile given a Person."""
    options = []
    if "all" in args or "ratings" in args:
        options.append("ratings")
    name_display = NameDisplay(xlocale=locale)
    birth, birth_event = get_birth_profile(
        db_handle, person, args=options, locale=locale
    )
    death, death_event = get_death_profile(
        db_handle, person, args=options, locale=locale
    )
    if "all" in args or "age" in args:
        options.append("age")
        if birth_event is not None:
            birth["age"] = locale.translation.ngettext(
                "{number_of} day", "{number_of} days", 0
            ).format(number_of=0)
            if death_event is not None:
                death["age"] = (
                    Span(birth_event.date, death_event.date)
                    .format(precision=3, dlocale=locale)
                    .strip("()")
                )
    profile = {
        "handle": person.handle,
        "gramps_id": person.gramps_id,
        "sex": get_sex_profile(person),
        "birth": birth,
        "death": death,
        "name_given": name_display.display_given(person),
        "name_surname": person.primary_name.get_surname(),
    }
    if "all" in args or "span" in args:
        options.append("span")
    if "all" in args or "events" in args:
        options.append("events")
        if "age" not in args and "all" not in args:
            birth_event = None
        profile["events"] = [
            get_event_profile_for_handle(
                db_handle,
                event_ref.ref,
                args=options,
                base_event=birth_event,
                label="age",
                locale=locale,
                role=locale.translation.sgettext(event_ref.get_role().xml_str()),
            )
            for event_ref in person.event_ref_list
        ]
    if "all" in args or "families" in args:
        primary_parent_family_handle = person.get_main_parents_family_handle()
        profile["primary_parent_family"] = get_family_profile_for_handle(
            db_handle, primary_parent_family_handle, options, locale=locale
        )
        profile["other_parent_families"] = []
        for handle in person.parent_family_list:
            if handle != primary_parent_family_handle:
                profile["other_parent_families"].append(
                    get_family_profile_for_handle(
                        db_handle, handle, options, locale=locale
                    )
                )
        profile["families"] = [
            get_family_profile_for_handle(db_handle, handle, options, locale=locale)
            for handle in person.family_list
        ]
    return profile


def get_person_profile_for_handle(
    db_handle: DbReadBase, handle: Handle, args: List, locale: GrampsLocale = glocale
) -> Union[Person, Dict]:
    """Get person profile given a handle."""
    try:
        obj = db_handle.get_person_from_handle(handle)
    except HandleError:
        return {}
    return get_person_profile_for_object(db_handle, obj, args, locale=locale)


def get_family_profile_for_object(
    db_handle: DbReadBase, family: Family, args: List, locale: GrampsLocale = glocale
) -> Family:
    """Get family profile given a Family."""
    options = []
    if "all" in args or "ratings" in args:
        options.append("ratings")
    marriage, marriage_event = get_marriage_profile(
        db_handle, family, args=options, locale=locale
    )
    divorce, divorce_event = get_divorce_profile(
        db_handle, family, args=options, locale=locale
    )
    if "all" in args or "span" in args:
        if marriage_event is not None:
            marriage["span"] = locale.translation.ngettext(
                "{number_of} day", "{number_of} days", 0
            ).format(number_of=0)
            if divorce_event is not None:
                divorce["span"] = (
                    Span(marriage_event.date, divorce_event.date)
                    .format(precision=3, dlocale=locale)
                    .strip("()")
                )
    if "all" in args or "age" in args:
        options.append("age")
    profile = {
        "handle": family.handle,
        "gramps_id": family.gramps_id,
        "father": get_person_profile_for_handle(
            db_handle, family.father_handle, options, locale=locale
        ),
        "mother": get_person_profile_for_handle(
            db_handle, family.mother_handle, options, locale=locale
        ),
        "relationship": locale.translation.sgettext(family.type.xml_str()),
        "marriage": marriage,
        "divorce": divorce,
        "children": [
            get_person_profile_for_handle(
                db_handle, child_ref.ref, options, locale=locale
            )
            for child_ref in family.child_ref_list
        ],
    }
    if profile["father"]:
        if profile["father"]["name_surname"] or profile["father"]["name_given"]:
            profile["family_surname"] = profile["father"]["name_surname"]
        elif profile["mother"]:
            profile["family_surname"] = profile["mother"]["name_surname"]
    elif profile["mother"]:
        profile["family_surname"] = profile["mother"]["name_surname"]
    else:
        profile["family_surname"] = ""
    if "all" in args or "events" in args:
        if "span" not in args and "all" not in args:
            marriage_event = None
        profile["events"] = [
            get_event_profile_for_handle(
                db_handle,
                event_ref.ref,
                args=options,
                base_event=marriage_event,
                label="span",
                locale=locale,
            )
            for event_ref in family.event_ref_list
        ]
    return profile


def get_family_profile_for_handle(
    db_handle: DbReadBase, handle: Handle, args: List, locale: GrampsLocale = glocale
) -> Union[Family, Dict]:
    """Get family profile given a handle."""
    try:
        obj = db_handle.get_family_from_handle(handle)
    except HandleError:
        return {}
    return get_family_profile_for_object(db_handle, obj, args, locale=locale)


def get_citation_profile_for_object(
    db_handle: DbReadBase,
    citation: Citation,
    args: List,
    locale: GrampsLocale = glocale,
) -> Citation:
    """Get citation profile given a Citation."""
    source = db_handle.get_source_from_handle(citation.source_handle)
    return {
        "source": {
            "author": source.author,
            "title": source.title,
            "pubinfo": source.pubinfo,
            "gramps_id": source.gramps_id,
        },
        "gramps_id": citation.gramps_id,
        "date": locale.date_displayer.display(citation.date),
        "page": citation.page,
    }


def get_citation_profile_for_handle(
    db_handle: DbReadBase, handle: Handle, args: List, locale: GrampsLocale = glocale
) -> Union[Family, Dict]:
    """Get citation profile given a handle."""
    try:
        obj = db_handle.get_citation_from_handle(handle)
    except HandleError:
        return {}
    return get_citation_profile_for_object(db_handle, obj, args, locale=locale)


def get_media_profile_for_object(
    db_handle: DbReadBase, media: Media, args: List, locale: GrampsLocale = glocale
) -> Media:
    """Get media profile given Media."""
    return {
        "gramps_id": media.gramps_id,
        "date": locale.date_displayer.display(media.date),
    }


def get_media_profile_for_handle(
    db_handle: DbReadBase, handle: Handle, args: List, locale: GrampsLocale = glocale
) -> Union[Media, Dict]:
    """Get media profile given a handle."""
    try:
        obj = db_handle.get_media_from_handle(handle)
    except HandleError:
        return {}
    return get_media_profile_for_object(db_handle, obj, args, locale=locale)


def get_extended_attributes(
    db_handle: DbReadBase, obj: GrampsObject, args: Optional[Dict] = None
) -> Dict:
    """Get extended attributes for a GrampsObject."""
    args = args or {}
    result = {}
    do_all = False
    if "all" in args["extend"]:
        do_all = True
    if (do_all or "child_ref_list" in args["extend"]) and hasattr(
        obj, "child_ref_list"
    ):
        result["children"] = [
            db_handle.get_person_from_handle(child_ref.ref)
            for child_ref in obj.child_ref_list
        ]
    if (do_all or "citation_list" in args["extend"]) and hasattr(obj, "citation_list"):
        result["citations"] = [
            db_handle.get_citation_from_handle(handle) for handle in obj.citation_list
        ]
    if (do_all or "event_ref_list" in args["extend"]) and hasattr(
        obj, "event_ref_list"
    ):
        result["events"] = [
            db_handle.get_event_from_handle(event_ref.ref)
            for event_ref in obj.event_ref_list
        ]
    if (do_all or "media_list" in args["extend"]) and hasattr(obj, "media_list"):
        result["media"] = [
            db_handle.get_media_from_handle(media_ref.ref)
            for media_ref in obj.media_list
        ]
    if (do_all or "note_list" in args["extend"]) and hasattr(obj, "note_list"):
        result["notes"] = [
            db_handle.get_note_from_handle(handle) for handle in obj.note_list
        ]
    if (do_all or "person_ref_list" in args["extend"]) and hasattr(
        obj, "person_ref_list"
    ):
        result["people"] = [
            db_handle.get_person_from_handle(person_ref.ref)
            for person_ref in obj.person_ref_list
        ]
    if (do_all or "reporef_list" in args["extend"]) and hasattr(obj, "reporef_list"):
        result["repositories"] = [
            db_handle.get_repository_from_handle(repo_ref.ref)
            for repo_ref in obj.reporef_list
        ]
    if (do_all or "tag_list" in args["extend"]) and hasattr(obj, "tag_list"):
        result["tags"] = [
            db_handle.get_tag_from_handle(handle) for handle in obj.tag_list
        ]
    if (do_all or "backlinks" in args["extend"]) and hasattr(obj, "backlinks"):
        result["backlinks"] = {}
        for class_name, backlinks in obj.backlinks.items():
            result["backlinks"][class_name] = [
                db_handle.method("get_%s_from_handle", class_name.upper())(handle)
                for handle in backlinks
            ]
    return result


def get_backlinks(db_handle: DbReadBase, handle: Handle) -> Dict[str, List[Handle]]:
    """Get backlinks to a handle.

    Will return a dictionary of the form
    `{'object_type': ['handle1', 'handle2', ...], ...}`
    """
    backlinks = {}
    for obj_type, target_handle in db_handle.find_backlink_handles(handle):
        key = obj_type.lower()
        if key not in backlinks:
            backlinks[key] = []
        backlinks[key].append(target_handle)
    return backlinks


def get_soundex(
    db_handle: DbReadBase, obj: GrampsObject, gramps_class_name: str
) -> str:
    """Return soundex code."""
    if gramps_class_name == "Family":
        if obj.father_handle is not None:
            person = db_handle.get_person_from_handle(obj.father_handle)
        elif obj.mother_handle is not None:
            person = db_handle.get_person_from_handle(obj.mother_handle)
    else:
        person = obj
    return soundex(person.get_primary_name().get_surname())


def get_reference_profile_for_object(
    db_handle: DbReadBase,
    obj: GrampsObject,
    locale: GrampsLocale = glocale,
) -> Dict:
    """Return reference profiles for an object."""
    profile = {}
    # get backlink handles
    if hasattr(obj, "backlinks"):
        backlink_handles = obj.backlinks
    else:
        # if not computed yet, do it now
        backlink_handles = get_backlinks(db_handle, obj.handle)
    if "person" in backlink_handles:
        profile["person"] = [
            get_person_profile_for_handle(db_handle, handle, args=[], locale=locale)
            for handle in backlink_handles["person"]
        ]
    if "family" in backlink_handles:
        profile["family"] = [
            get_family_profile_for_handle(db_handle, handle, args=[], locale=locale)
            for handle in backlink_handles["family"]
        ]
    if "event" in backlink_handles:
        profile["event"] = [
            get_event_profile_for_handle(db_handle, handle, args=[], locale=locale)
            for handle in backlink_handles["event"]
        ]
    if "media" in backlink_handles:
        profile["media"] = [
            get_media_profile_for_handle(db_handle, handle, args=[], locale=locale)
            for handle in backlink_handles["media"]
        ]
    if "citation" in backlink_handles:
        profile["citation"] = [
            get_citation_profile_for_handle(db_handle, handle, args=[], locale=locale)
            for handle in backlink_handles["citation"]
        ]
    if "place" in backlink_handles:
        profile["place"] = [
            get_place_profile_for_handle(db_handle, handle, locale=locale)
            for handle in backlink_handles["place"]
        ]
    return profile


def get_rating(db_handle: DbReadBase, obj: GrampsObject) -> Tuple[int, int]:
    """Return rating based on citations."""
    count = 0
    confidence = 0
    if hasattr(obj, "citation_list"):
        count = len(obj.citation_list)
        if hasattr(obj, "extended") and "citations" in obj.extended:
            for citation in obj.extended["citations"]:
                if citation.confidence > confidence:
                    confidence = citation.confidence
        else:
            for handle in obj.citation_list:
                citation = db_handle.get_citation_from_handle(handle)
                if citation.confidence > confidence:
                    confidence = citation.confidence
    return count, confidence


def has_handle(
    db_handle: DbWriteBase,
    obj: GrampsObject,
) -> bool:
    """Check if an object with the same class and handle exists in the DB."""
    obj_class = obj.__class__.__name__.lower()
    method = db_handle.method("has_%s_handle", obj_class)
    return method(obj.handle)


def has_gramps_id(
    db_handle: DbWriteBase,
    obj: GrampsObject,
) -> bool:
    """Check if an object with the same class and handle exists in the DB."""
    if not hasattr(obj, "gramps_id"):  # needed for tags
        return False
    obj_class = obj.__class__.__name__.lower()
    method = db_handle.method("has_%s_gramps_id", obj_class)
    return method(obj.gramps_id)


def add_object(
    db_handle: DbWriteBase,
    obj: GrampsObject,
    trans: DbTxn,
    fail_if_exists: bool = False,
):
    """Commit a Gramps object to the database.

    If `fail_if_exists` is true, raises a ValueError if an object of
    the same type exists with the same handle or same Gramps ID.

    In the case of a family object, also updates the referenced handles
    in the corresponding person objects.
    """
    if db_handle.readonly:
        # adding objects is forbidden on a read-only db!
        abort(HTTPStatus.FORBIDDEN)
    obj_class = obj.__class__.__name__.lower()
    if fail_if_exists:
        if has_handle(db_handle, obj):
            raise ValueError("Handle already exists.")
        if has_gramps_id(db_handle, obj):
            raise ValueError("Gramps ID already exists.")
    try:
        add_method = db_handle.method("add_%s", obj_class)
        if obj_class == "family":
            # need to add handle if not present yet!
            if not obj.handle:
                obj.handle = create_id()
            add_family_update_refs(db_handle=db_handle, obj=obj, trans=trans)
        return add_method(obj, trans)
    except AttributeError:
        raise ValueError("Database does not support writing.")


def add_family_update_refs(
    db_handle: DbWriteBase,
    obj: Family,
    trans: DbTxn,
) -> None:
    """Update the `family_list` and `parent_family_list` of family members.

    Case where the family is new.
    """
    # add family handle to parents
    for handle in [obj.get_father_handle(), obj.get_mother_handle()]:
        if handle:
            parent = db_handle.get_person_from_handle(handle)
            parent.add_family_handle(obj.handle)
            db_handle.commit_person(parent, trans)
    # for each child, add the family handle to the child
    for ref in obj.get_child_ref_list():
        child = db_handle.get_person_from_handle(ref.ref)
        child.add_parent_family_handle(obj.handle)
        db_handle.commit_person(child, trans)


def validate_object_dict(obj_dict: Dict[str, Any]) -> bool:
    """Validate a dict representation of a Gramps object vs. its schema."""
    try:
        obj_cls = getattr(gramps.gen.lib, obj_dict["_class"])
    except (KeyError, AttributeError, TypeError):
        return False
    schema = obj_cls.get_schema()
    obj_dict_fixed = {k: v for k, v in obj_dict.items() if k != "complete"}
    try:
        jsonschema.validate(obj_dict_fixed, schema)
    except jsonschema.exceptions.ValidationError as exc:
        current_app.log_exception(exc)
        return False
    return True


def xml_to_locale(gramps_type_name: str, string: str) -> str:
    """Translate and XML string type name to a localized type name."""
    gramps_type = getattr(gramps.gen.lib, gramps_type_name)
    typ = gramps_type()
    typ.set_from_xml_str(string)
    return str(typ)


def fix_object_dict(object_dict: Dict, class_name: Optional[str] = None):
    """Restore a Gramps object in simplified representation to its full form.

    This restores in particular:
    - class names
    - Gramps types are turned from strings into dictionaries
    - Gramps type names are translated to the default Gramps locale
    """
    d_out = {}
    class_name = class_name or object_dict.get("_class")
    if not class_name:
        raise ValueError("No class name specified!")
    d_out["_class"] = class_name
    for k, v in object_dict.items():
        # convert type back to dict and translate type name
        if k in ["type", "place_type", "media_type", "frel", "mrel"] or (
            k == "name" and class_name == "StyledTextTag"
        ):
            if isinstance(v, str):
                if class_name == "Family":
                    d_out[k] = {
                        "_class": f"{class_name}RelType",
                        "string": xml_to_locale(f"{class_name}RelType", v),
                    }
                elif class_name == "RepoRef":
                    d_out[k] = {
                        "_class": "SourceMediaType",
                        "string": xml_to_locale("SourceMediaType", v),
                    }
                else:
                    d_out[k] = {
                        "_class": f"{class_name}Type",
                        "string": xml_to_locale(f"{class_name}Type", v),
                    }
            else:
                d_out[k] = v
        elif k == "role":
            if isinstance(v, str):
                d_out[k] = {
                    "_class": "EventRoleType",
                    "string": xml_to_locale("EventRoleType", v),
                }
            else:
                d_out[k] = v
        elif k == "origintype":
            if isinstance(v, str):
                d_out[k] = {
                    "_class": "NameOriginType",
                    "string": xml_to_locale("NameOriginType", v),
                }
            else:
                d_out[k] = v
        elif k in ["rect", "mother_handle", "father_handle", "famc"] and not v:
            d_out[k] = None
        elif isinstance(v, dict):
            d_out[k] = fix_object_dict(v, _get_class_name(class_name, k))
        elif isinstance(v, list):
            d_out[k] = [
                fix_object_dict(item, _get_class_name(class_name, k))
                if isinstance(item, dict)
                else item
                for item in v
            ]
        elif k in ["complete"]:
            pass
        else:
            d_out[k] = v
    return d_out


def _get_class_name(super_name, key_name) -> str:
    """Get the correct Gramps class name for a given key in a class dict."""
    if key_name == "date":
        return "Date"
    if key_name == "media_list":
        return "MediaRef"
    if key_name == "child_ref_list":
        return "ChildRef"
    if key_name == "event_ref_list":
        return "EventRef"
    if key_name == "address_list":
        return "Address"
    if key_name == "urls":
        return "Url"
    if key_name == "lds_ord_list":
        return "LdsOrd"
    if key_name == "person_ref_list":
        return "PersonRef"
    if key_name == "surname_list":
        return "Surname"
    if key_name == "text":
        return "StyledText"
    if key_name == "place_type":
        return "PlaceType"
    if key_name == "alt_loc":
        return "Location"
    if key_name == "reporef_list":
        return "RepoRef"
    if key_name == "placeref_list":
        return "PlaceRef"
    if key_name == "tags":
        return "StyledTextTag"
    if (key_name == "name" and super_name == "Place") or key_name == "alt_names":
        return "PlaceName"
    if key_name in ["primary_name", "alternate_names"]:
        return "Name"
    if key_name == "attribute_list" and super_name == "Citation":
        return "SrcAttribute"
    elif key_name == "attribute_list":
        return "Attribute"
    raise ValueError(f"Unknown classes: {super_name}, {key_name}")


def update_object(
    db_handle: DbWriteBase,
    obj: GrampsObject,
    trans: DbTxn,
):
    """Commit a modified Gramps object to the database.

    Fails with a ValueError if the object with this handle does not
    exist, or if another object of the same type exists with the
    same Gramps ID.
    """
    if db_handle.readonly:
        # updating objects is forbidden on a read-only db!
        abort(HTTPStatus.FORBIDDEN)
    obj_class = obj.__class__.__name__.lower()
    if not has_handle(db_handle, obj):
        raise ValueError("Cannot be used for new objects.")
    if not obj.gramps_id:
        # if the Gramps ID is empty, set it to the old one!
        handle_func = db_handle.method("get_%s_from_handle", obj_class)
        obj_old = handle_func(obj.handle)
        obj.set_gramps_id(obj_old.gramps_id)
    try:
        commit_method = db_handle.method("commit_%s", obj_class)
        if obj_class == "family":
            handle_func = db_handle.method("get_%s_from_handle", obj_class)
            obj_old = handle_func(obj.handle)
            update_family_update_refs(
                db_handle=db_handle, obj_old=obj_old, obj=obj, trans=trans
            )
        elif obj_class == "person":
            db_handle.set_birth_death_index(obj)
        return commit_method(obj, trans)
    except AttributeError:
        raise ValueError("Database does not support writing.")


def update_family_update_refs(
    db_handle: DbWriteBase,
    obj_old: Family,
    obj: Family,
    trans: DbTxn,
) -> None:
    """Update the `family_list` and `parent_family_list` of family members.

    Case where the family was modified.
    """
    _fix_parent_handles(
        db_handle, obj, obj_old.get_father_handle(), obj.get_father_handle(), trans
    )
    _fix_parent_handles(
        db_handle, obj, obj_old.get_mother_handle(), obj.get_mother_handle(), trans
    )
    # fix child handles
    orig_set = set(r.ref for r in obj_old.get_child_ref_list())
    new_set = set(r.ref for r in obj.get_child_ref_list())

    # remove the family from children which have been removed
    for ref in orig_set - new_set:
        person = db_handle.get_person_from_handle(ref)
        person.remove_parent_family_handle(obj.handle)
        db_handle.commit_person(person, trans)

    # add the family to children which have been added
    for ref in new_set - orig_set:
        person = db_handle.get_person_from_handle(ref)
        person.add_parent_family_handle(obj.handle)
        db_handle.commit_person(person, trans)


def _fix_parent_handles(
    db_handle: DbWriteBase, obj: Family, orig_handle, new_handle, trans
) -> None:
    if orig_handle != new_handle:
        if orig_handle:
            person = db_handle.get_person_from_handle(orig_handle)
            person.family_list.remove(obj.handle)
            db_handle.commit_person(person, trans)
        if new_handle:
            person = db_handle.get_person_from_handle(new_handle)
            person.family_list.append(obj.handle)
            db_handle.commit_person(person, trans)


def transaction_to_json(transaction: DbTxn) -> List[Dict[str, Any]]:
    """Return a JSON representation of a database transaction."""
    out = []
    for recno in transaction.get_recnos(reverse=False):
        key, action, handle, old_data, new_data = transaction.get_record(recno)
        try:
            obj_cls_name = KEY_TO_CLASS_MAP[key]
        except KeyError:
            continue  # this happens for references
        trans_dict = {TXNUPD: "update", TXNDEL: "delete", TXNADD: "add"}
        obj_cls = getattr(gramps.gen.lib, obj_cls_name)
        if old_data:
            old_data = obj_cls().unserialize(old_data)
        if new_data:
            new_data = obj_cls().unserialize(new_data)
        item = {
            "type": trans_dict[action],
            "handle": handle,
            "_class": obj_cls_name,
            "old": json.loads(to_json(old_data)),
            "new": json.loads(to_json(new_data)),
        }
        out.append(item)
    return out


def hash_object(obj: GrampsObject) -> str:
    """Generate a SHA256 hash for a Gramps object's data."""
    data = to_json(obj).encode()
    return sha256(data).hexdigest()


def filter_missing_files(objects: List[Media]) -> List[Media]:
    """Filter media objects returning only ones where the file is missing."""
    base_dir = current_app.config.get("MEDIA_BASE_DIR", "")
    handler = MediaHandler(base_dir)
    objects_existing = handler.filter_existing_files(objects)
    handles_existing = set(obj.handle for obj in objects_existing)
    return [obj for obj in objects if obj.handle not in handles_existing]


def get_missing_media_file_handles(
    db_handle: DbReadBase, handles: List[str]
) -> List[str]:
    """Filter media handles returning only ones where the file is missing."""
    objects = [db_handle.get_media_from_handle(handle) for handle in handles]
    objects_missing = filter_missing_files(objects)
    return [obj.handle for obj in objects_missing]
