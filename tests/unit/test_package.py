import fastapi_faults


def test_package_is_importable() -> None:
    assert fastapi_faults.__name__ == "fastapi_faults"
