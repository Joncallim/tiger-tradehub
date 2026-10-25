import pytest

from tradehub_research.adapters.json_tokens import JsonTokenError, preserve_numeric_tokens


def test_preserves_exact_numeric_lexemes_and_paths_without_touching_strings():
    raw = b'{"hi":1.2300,"exp":1e-7,"zero":0,"neg":-2,"text":"1.2300","a":[3,{"x~/y":4}]}'
    manifest = preserve_numeric_tokens(raw)
    assert manifest.tokens_by_path == {
        "/hi": "1.2300",
        "/exp": "1e-7",
        "/zero": "0",
        "/neg": "-2",
        "/a/0": "3",
        "/a/1/x~0~1y": "4",
    }


@pytest.mark.parametrize(
    "raw",
    [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1,"x":2}', b'{"x":', b"\xff"],
)
def test_invalid_duplicate_or_nonfinite_json_fails_closed(raw):
    with pytest.raises(JsonTokenError):
        preserve_numeric_tokens(raw)


def test_root_and_resource_limits_fail_closed():
    assert preserve_numeric_tokens(b"7").tokens_by_path == {"": "7"}
    with pytest.raises(JsonTokenError):
        preserve_numeric_tokens(b"12345", max_bytes=4)
    with pytest.raises(JsonTokenError):
        preserve_numeric_tokens(b"[[[1]]]", max_depth=2)
