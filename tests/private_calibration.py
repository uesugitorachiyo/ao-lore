"""Explicit opt-in semantics for sealed, ignored calibration evidence."""

import os
import unittest

PRIVATE_CALIBRATION_REASON = "private calibration assets are not installed"

def private_calibration(test):
    return unittest.skipUnless(os.environ.get("AO_LORE_PRIVATE_CALIBRATION") == "1", PRIVATE_CALIBRATION_REASON)(test)
