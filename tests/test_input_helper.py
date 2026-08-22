"""Tests for the input transform helpers. Pure functions — no network, no browser."""

from datetime import datetime, timedelta

import pytest

from badminton_bot.utils.input_helper import (
    cast_court_no_to_int_and_check_is_valid,
    check_if_target_datetime_is_outdated,
    get_valid_input,
    parse_input_booking_periods_str,
    transform_offset_milliseconds_param,
    transform_yes_no_input,
)


class TestTransformYesNoInput:
    def test_accepts_upper_case_y_and_n(self):
        assert transform_yes_no_input("Y") is True
        assert transform_yes_no_input("N") is False

    @pytest.mark.parametrize("bad_input", ["y", "n", "yes", "", "Ｙ"])
    def test_rejects_anything_else(self, bad_input):
        with pytest.raises(AssertionError):
            transform_yes_no_input(bad_input)


class TestTransformOffsetMillisecondsParam:
    def test_empty_input_means_no_offset(self):
        assert transform_offset_milliseconds_param("") == 0

    @pytest.mark.parametrize("value", [-1000, -500, 0, 500, 1000])
    def test_accepts_the_whole_allowed_range_inclusive(self, value):
        assert transform_offset_milliseconds_param(str(value)) == value

    @pytest.mark.parametrize("value", ["1001", "-1001", "5000"])
    def test_rejects_out_of_range(self, value):
        with pytest.raises(ValueError):
            transform_offset_milliseconds_param(value)

    @pytest.mark.parametrize("value", ["abc", "1.5", " "])
    def test_rejects_non_integer(self, value):
        with pytest.raises(ValueError):
            transform_offset_milliseconds_param(value)


class TestCheckIfTargetDatetimeIsOutdated:
    def test_returns_the_datetime_untouched_when_in_the_future(self):
        future = datetime.now() + timedelta(days=1)
        assert check_if_target_datetime_is_outdated(target_datetime=future) == future

    def test_raises_when_already_in_the_past(self):
        past = datetime.now() - timedelta(seconds=1)
        with pytest.raises(ValueError):
            check_if_target_datetime_is_outdated(target_datetime=past)


class TestParseInputBookingPeriodsStr:
    def test_parses_a_single_period(self):
        assert parse_input_booking_periods_str("2099-04-12T20:00:00") == (
            datetime(2099, 4, 12, 20, 0, 0),
        )

    def test_parses_multiple_comma_separated_periods(self):
        assert parse_input_booking_periods_str(
            "2099-04-12T20:00:00,2099-04-12T21:00:00"
        ) == (datetime(2099, 4, 12, 20, 0, 0), datetime(2099, 4, 12, 21, 0, 0))

    def test_rejects_periods_in_the_past(self):
        with pytest.raises(ValueError):
            parse_input_booking_periods_str("2000-01-01T20:00:00")

    @pytest.mark.parametrize(
        "bad_input",
        [
            "2099-04-12 20:00:00",  # space instead of T
            "2099-04-12T20:00:00.000",  # dev-mode format, not the period format
            "2099-04-12T20:00:00, 2099-04-12T21:00:00",  # space after the comma
        ],
    )
    def test_rejects_malformed_input(self, bad_input):
        with pytest.raises(ValueError):
            parse_input_booking_periods_str(bad_input)


class TestCastCourtNoToIntAndCheckIsValid:
    MAPPING = {0: object(), 1: object()}

    def test_returns_int_for_a_registered_court_no(self):
        assert (
            cast_court_no_to_int_and_check_is_valid("1", mapping_dict=self.MAPPING) == 1
        )

    def test_rejects_a_court_no_missing_from_the_mapping(self):
        with pytest.raises(ValueError):
            cast_court_no_to_int_and_check_is_valid("9", mapping_dict=self.MAPPING)

    def test_rejects_non_numeric_input(self):
        with pytest.raises(ValueError):
            cast_court_no_to_int_and_check_is_valid("abc", mapping_dict=self.MAPPING)


class TestGetValidInput:
    def test_returns_the_transformed_value_on_first_valid_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "Y")
        assert get_valid_input(prompt="", transform_func=transform_yes_no_input) is True

    def test_reprompts_until_the_transform_stops_raising(self, monkeypatch):
        answers = iter(["nope", "still nope", "N"])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))
        assert get_valid_input(prompt="", transform_func=transform_yes_no_input) is False

    def test_does_not_swallow_unexpected_errors(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "Y")

        def boom(_):
            raise KeyError("not a validation failure")

        with pytest.raises(KeyError):
            get_valid_input(prompt="", transform_func=boom)
