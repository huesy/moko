from moko.agent.extract import extraction_from_obj


def test_normalises_good_object():
    ex = extraction_from_obj({"subjects": [{"kind": "person", "name": "Maya"}]})
    assert ex.subjects[0]["name"] == "Maya"


def test_keeps_nested_values_intact():
    ex = extraction_from_obj({"attributes": [{"subject": "A", "key": "meta", "value": "{x}"}]})
    assert ex.attributes[0]["value"] == "{x}"


def test_is_empty_distinguishes_a_noop_from_content():
    assert extraction_from_obj({}).is_empty  # nothing at all → no-op
    assert not extraction_from_obj({"subjects": [{"kind": "person", "name": "Mia"}]}).is_empty
    assert not extraction_from_obj({"speaker_sentiment": {"label": "glad"}}).is_empty
    assert not extraction_from_obj({"resolved_gaps": [1]}).is_empty


def test_non_dict_is_empty():
    ex = extraction_from_obj("not an object")
    assert ex.subjects == []
    assert ex.speaker_sentiment is None


def test_handles_bad_field_types():
    ex = extraction_from_obj({"subjects": "not a list", "speaker_sentiment": "nope"})
    assert ex.subjects == []
    assert ex.speaker_sentiment is None


def test_drops_non_dict_list_items():
    ex = extraction_from_obj({"thoughts": [{"body": "hm"}, "junk", 42]})
    assert ex.thoughts == [{"body": "hm"}]
