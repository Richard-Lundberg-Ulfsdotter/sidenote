"""Shared fixtures."""

import pytest

from odf import dc
from odf.namespaces import TEXTNS, XMLNS
from odf.office import ChangeInfo
from odf.opendocument import OpenDocumentText
from odf.text import (
    Change,
    ChangedRegion,
    ChangeEnd,
    ChangeStart,
    Deletion,
    Insertion,
    P,
    TrackedChanges,
)


def _region(rid, body):
    region = ChangedRegion(check_grammar=False)
    region.setAttrNS(XMLNS, "id", rid)
    region.setAttrNS(TEXTNS, "id", rid)
    region.addElement(body)
    return region


def _change_info(author, date):
    info = ChangeInfo(check_grammar=False)
    info.addElement(dc.Creator(text=author))
    info.addElement(dc.Date(text=date))
    return info


@pytest.fixture
def tracked_sample(tmp_path):
    """A document with one tracked insertion and one tracked deletion.

    Body text reads "Hello new words world" / "Start finish", where
    "new words" is an insertion by Anna and "old text" was deleted by
    Magnus between "Start " and "finish".
    """
    doc = OpenDocumentText()

    insertion = Insertion(check_grammar=False)
    insertion.addElement(_change_info("Anna", "2026-06-16T08:31:00"))
    deletion = Deletion(check_grammar=False)
    deletion.addElement(_change_info("Magnus", "2026-06-17T09:00:00"))
    deletion.addElement(P(text="old text"))

    tracked = TrackedChanges()
    tracked.addElement(_region("ct1", insertion))
    tracked.addElement(_region("ct2", deletion))
    doc.text.addElement(tracked)

    p1 = P(text="Hello ")
    p1.addElement(ChangeStart(changeid="ct1"))
    p1.addText("new words")
    p1.addElement(ChangeEnd(changeid="ct1"))
    p1.addText(" world")
    doc.text.addElement(p1)

    p2 = P(text="Start ")
    p2.addElement(Change(changeid="ct2"))
    p2.addText("finish")
    doc.text.addElement(p2)

    path = tmp_path / "tracked.odt"
    doc.save(str(path))
    return path
