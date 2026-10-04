from tests.synthetic.run_synthetic import args_match, score


def test_weekdays_are_not_interchangeable():
    assert not args_match({"date": "Monday"}, {"date": "Friday"})[0]


def test_identifier_prefix_is_not_identity():
    assert not args_match({"order_id": "ABC123"}, {"order_id": "ABC1234"})[0]


def test_identifier_punctuation_is_significant():
    assert not args_match({"order_id": "AB-12"}, {"order_id": "AB12"})[0]


def test_date_year_and_empty_values_are_significant():
    assert not args_match({"date": "June 4 2025"}, {"date": "June 4 2026"})[0]
    assert not args_match({"date": "Monday"}, {"date": ""})[0]


def test_extra_argument_is_not_silently_accepted():
    assert not args_match({"quantity": 2}, {"quantity": 2, "product_id": "unexpected"})[0]


def test_wildcard_does_not_steal_a_literal_match():
    expected = [{"tool": "t", "args": {"x": "*"}}, {"tool": "t", "args": {"x": "A"}}]
    assert score(expected, [("t", {"x": "A"}), ("t", {"x": "B"})])[0]


def test_duplicate_invocations_fail():
    assert not score([{"tool": "t", "args": {}}], [("t", {}), ("t", {})])[0]
