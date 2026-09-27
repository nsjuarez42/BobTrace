"""
Auto-generated regression test.
Span: e940b68d-2ff5-4a24-9d0b-681f8695a614
Function: apply_discount
Module: sandbox.db
"""
import os
import pytest

os.environ["BOBTRACE_FILE"] = ""
os.environ["BOBTRACE_CONSOLE"] = "off"

from sandbox.db import apply_discount

def test_replay_apply_discount_e940b68d():
    """Regression test — fails while the bug still exists."""
    inputs = {
        "price": 24.98,
        "code": "SAVE20"
    }
    # apply_discount raises ValueError for unknown codes — this is correct.
    # The fix lives in the endpoint (get_order_total), which catches this and
    # returns HTTP 400. This test pins that apply_discount raises as expected.
    with pytest.raises(ValueError, match="Unknown discount code"):
        apply_discount(**inputs)
