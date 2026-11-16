import pytest

from pointlog.quality import (
    BAD_CRC_REQUEST,
    BAD_CRC_RESPONSE,
    BAD_FRAME,
    DUPLICATE,
    EXCEPTION_RESPONSE,
    GOOD,
    QUALITY_ORDER,
    QualityFlags,
    determine_quality,
    is_pending,
    is_quality_code,
    quality_meta,
    severity,
)


def test_catalogue_is_complete_and_consistent():
    assert len(QUALITY_ORDER) == 13
    assert len(set(QUALITY_ORDER)) == len(QUALITY_ORDER)
    for code in QUALITY_ORDER:
        assert is_quality_code(code)
        meta = quality_meta(code)
        assert meta["severity"] in {"info", "warning", "error"}
        assert isinstance(meta["pending_recollect"], bool)
        assert str(meta["description"])
    assert not is_quality_code("made_up")
    assert severity("made_up") == "error"
    assert is_pending("made_up")


def test_clean_flags_are_good():
    assert determine_quality(QualityFlags()) == GOOD


def test_request_crc_outranks_range_and_enum():
    flags = QualityFlags(bad_crc_request=True, overflow=True, not_in_enum=True)
    assert determine_quality(flags) == BAD_CRC_REQUEST


def test_response_crc_outranks_range():
    assert determine_quality(
        QualityFlags(bad_crc_response=True, under_range=True)
    ) == BAD_CRC_RESPONSE


def test_exception_outranks_decode_error():
    assert determine_quality(
        QualityFlags(exception_response=True, decode_error=True)
    ) == EXCEPTION_RESPONSE


def test_bad_frame_has_highest_priority():
    flags = QualityFlags(
        bad_frame=True,
        bad_crc_request=True,
        bad_crc_response=True,
        exception_response=True,
        overflow=True,
    )
    assert determine_quality(flags) == BAD_FRAME


def test_duplicate_outranks_value_quality():
    assert determine_quality(QualityFlags(duplicate=True, overflow=True)) == DUPLICATE


def test_individual_value_codes():
    assert determine_quality(QualityFlags(overflow=True)) == "overflow"
    assert determine_quality(QualityFlags(under_range=True)) == "under_range"
    assert determine_quality(QualityFlags(not_in_enum=True)) == "not_in_enum"
    assert determine_quality(QualityFlags(decode_error=True)) == "decode_error"


@pytest.mark.parametrize(
    ("code", "pending"),
    [
        ("good", False),
        ("overflow", False),
        ("under_range", False),
        ("not_in_enum", False),
        ("duplicate", False),
        ("no_contract", False),
        ("bad_frame", True),
        ("bad_crc_request", True),
        ("bad_crc_response", True),
        ("exception_response", True),
        ("missing_response", True),
        ("decode_error", True),
        ("calibration_unknown", True),
    ],
)
def test_pending_recollect_flags(code, pending):
    assert is_pending(code) is pending
