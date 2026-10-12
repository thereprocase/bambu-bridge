from bambu_bridge.hms import _CATALOG, catalog_message, decode_hms_entry, lookup


def test_all_published_codes_have_descriptions_and_hms_severity_is_preserved():
    assert len(_CATALOG) > 6000
    for key, text in _CATALOG.items():
        assert text and catalog_message(key) == text
        if len(key) == 16:
            entry = decode_hms_entry(int(key[:8], 16), int(key[8:], 16))
            assert entry["user_message"] != entry["hex"]
            assert entry["hex"].replace("_", "") == key
        else:
            assert lookup(int(key, 16))["user_message"] != key


def test_completed_refill_and_purge_are_readable_information_not_runout_errors():
    done = decode_hms_entry(0x07002000, 0x30002)
    pending = decode_hms_entry(0x07002000, 0x30001)
    assert done["severity"] == "info" and "automatically switched" in done["user_message"]
    assert pending["severity"] == "info" and "purged" in pending["user_message"]
    assert decode_hms_entry(0x07002000, 0x20001)["severity"] == "error"


def test_curated_remediation_wins_and_unknown_codes_remain_visible():
    assert lookup("0700_2000_0002_0001")["remediation"]
    assert lookup("ffff_ffff_ffff_ffff")["user_message"] == "ffff_ffff_ffff_ffff"


def test_model_specific_catalog_selects_matching_wording_and_falls_back_safely():
    from bambu_bridge.hms import _MODEL_CATALOGS

    assert "01P" in _MODEL_CATALOGS
    for model, entries in _MODEL_CATALOGS.items():
        for key, text in entries.items():
            assert catalog_message(key, model) == text
            if len(key) == 16:
                entry = decode_hms_entry(int(key[:8], 16), int(key[8:], 16), catalog_model=model)
                assert entry["user_message"] == text
            else:
                assert lookup(int(key, 16), model)["user_message"] == text
    assert catalog_message("0700200000030002", "unsupported") == catalog_message("0700200000030002")
