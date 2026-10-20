import clean_databricks


def test_package_exposes_version() -> None:
    assert clean_databricks.__version__ == "0.1.0"

