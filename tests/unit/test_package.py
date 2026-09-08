from importlib.metadata import metadata

import fastapi_faults


def test_package_is_importable() -> None:
    assert fastapi_faults.__name__ == "fastapi_faults"


def test_package_declares_mit_license() -> None:
    package = metadata("fastapi-faults")

    assert package["License-Expression"] == "MIT"
    assert "License :: OSI Approved :: MIT License" in package.get_all("Classifier", [])
