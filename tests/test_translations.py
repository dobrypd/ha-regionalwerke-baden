"""Translation files must exist, stay in sync, and actually drive the UI."""

import json
import pathlib

import pytest

COMPONENT = (
    pathlib.Path(__file__).parent.parent / "custom_components" / "regionalwerke_baden"
)
TRANSLATIONS = COMPONENT / "translations"
LANGUAGES = ["en", "de", "pl"]


def _keys(obj, prefix=""):
    """Flatten to dotted leaf paths."""
    if not isinstance(obj, dict):
        return {prefix}
    return {
        k
        for key, value in obj.items()
        for k in _keys(value, f"{prefix}.{key}" if prefix else key)
    }


def test_strings_and_english_translation_are_identical():
    """Custom integrations load translations/en.json, not strings.json — they must not drift."""
    assert json.loads((COMPONENT / "strings.json").read_text()) == json.loads(
        (TRANSLATIONS / "en.json").read_text()
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_file_exists_and_is_valid(language):
    path = TRANSLATIONS / f"{language}.json"
    assert path.exists(), f"missing {path.name}"
    json.loads(path.read_text())


@pytest.mark.parametrize("language", [lang for lang in LANGUAGES if lang != "en"])
def test_translations_have_the_same_keys_as_english(language):
    english = _keys(json.loads((TRANSLATIONS / "en.json").read_text()))
    other = _keys(json.loads((TRANSLATIONS / f"{language}.json").read_text()))
    assert other == english, (
        f"{language}.json key drift — missing: {sorted(english - other)}, "
        f"extra: {sorted(other - english)}"
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_translated_string_is_non_empty(language):
    def walk(obj, path=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                walk(value, f"{path}.{key}" if path else key)
        else:
            assert isinstance(obj, str) and obj.strip(), (
                f"{language}: empty value at {path}"
            )

    walk(json.loads((TRANSLATIONS / f"{language}.json").read_text()))


@pytest.mark.parametrize("language", LANGUAGES)
def test_entity_translation_keys_match_the_sensors(language):
    """The entity block is dead weight unless the sensors declare these translation keys."""
    data = json.loads((TRANSLATIONS / f"{language}.json").read_text())
    declared = set(data["entity"]["sensor"])
    source = (COMPONENT / "sensor.py").read_text()
    for key in declared:
        assert f'super().__init__(coord, "{key}")' in source, (
            f"no sensor uses translation_key {key}"
        )
    assert "_attr_has_entity_name = True" in source
