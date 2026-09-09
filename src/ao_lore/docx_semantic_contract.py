"""Immutable vocabulary shared by independent DOCX semantic implementations."""

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"

HYPERLINK_REL = R_NS + "/hyperlink"
IMAGE_REL = R_NS + "/image"
DIAGRAM_RELATIONSHIPS = (
    ("dm", R_NS + "/diagramData"),
    ("lo", R_NS + "/diagramLayout"),
    ("qs", R_NS + "/diagramQuickStyle"),
    ("cs", R_NS + "/diagramColors"),
)

PICTURE_GRAPHIC_URI = "http://schemas.openxmlformats.org/drawingml/2006/picture"
DIAGRAM_GRAPHIC_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
WORDPROCESSING_SHAPE_GRAPHIC_URI = WPS_NS
GRAPHIC_URIS = frozenset(
    {
        PICTURE_GRAPHIC_URI,
        DIAGRAM_GRAPHIC_URI,
        WORDPROCESSING_SHAPE_GRAPHIC_URI,
    }
)

HYPERLINK_HISTORY_TRUE = "1"
MAXIMUM_ANCHOR_CHARACTERS = 1024

COUNT_KEYS = (
    "paragraphs",
    "headings",
    "lists",
    "tables",
    "rows",
    "cells",
    "links",
    "footnotes",
    "endnotes",
    "drawings",
    "media",
)
