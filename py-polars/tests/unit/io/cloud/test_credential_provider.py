import io
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import polars as pl
import polars.io.cloud.credential_provider
from polars.io.cloud._utils import LRUCache, NoPickleOption
from polars.io.cloud.credential_provider._builder import (
    AutoInit,
    _init_credential_provider_builder,
)
from polars.io.cloud.credential_provider._providers import (
    CachedCredentialProvider,
    CachingCredentialProvider,
    UserProvidedGCPToken,
)


@pytest.mark.parametrize(
    "io_func",
    [
        *[pl.scan_parquet, pl.read_parquet],
        pl.scan_csv,
        *[pl.scan_ndjson, pl.read_ndjson],
        pl.scan_ipc,
    ],
)
def test_credential_provider_scan(
    io_func: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    err_magic = "err_magic_3"

    def raises(*_: None, **__: None) -> None:
        raise AssertionError(err_magic)

    from polars.io.cloud.credential_provider._builder import CredentialProviderBuilder

    monkeypatch.setattr(CredentialProviderBuilder, "__init__", raises)

    with pytest.raises(AssertionError, match=err_magic):
        io_func("s3://bucket/path", credential_provider="auto")

    with pytest.raises(AssertionError, match=err_magic):
        io_func(
            "s3://bucket/path",
            credential_provider="auto",
            storage_options={"aws_region": "eu-west-1"},
        )

    # We can't test these with the `read_` functions as they end up executing
    # the query
    if io_func.__name__.startswith("scan_"):
        # Passing `None` should disable the automatic instantiation of
        # `CredentialProviderAWS`
        io_func("s3://bucket/path", credential_provider=None)

    err_magic = "err_magic_7"

    def raises_2() -> pl.CredentialProviderFunctionReturn:
        raise AssertionError(err_magic)

    with pytest.raises(AssertionError, match=err_magic):
        io_func("s3://bucket/path", credential_provider=raises_2).collect()


@pytest.mark.parametrize(
    ("provider_class", "path"),
    [
        (polars.io.cloud.credential_provider.CredentialProviderAWS, "s3://.../..."),
        (polars.io.cloud.credential_provider.CredentialProviderGCP, "gs://.../..."),
        (polars.io.cloud.credential_provider.CredentialProviderAzure, "az://.../..."),
    ],
)
def test_credential_provider_serialization_auto_init(
    provider_class: polars.io.cloud.credential_provider.CredentialProvider,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raises_1(*a: Any, **kw: Any) -> None:
        msg = "err_magic_1"
        raise AssertionError(msg)

    monkeypatch.setattr(provider_class, "__init__", raises_1)

    # If this is not set we will get an error before hitting the credential
    # provider logic when polars attempts to retrieve the region from AWS.
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    # Credential provider should not be initialized during query plan construction.
    q = pl.scan_parquet(path)

    # Check baseline - query plan is configured to auto-initialize the credential
    # provider.
    with pytest.raises(AssertionError, match="err_magic_1"):
        q.collect()

    q = pickle.loads(pickle.dumps(q))

    def raises_2(*a: Any, **kw: Any) -> None:
        msg = "err_magic_2"
        raise AssertionError(msg)

    monkeypatch.setattr(provider_class, "__init__", raises_2)

    # Check that auto-initialization happens upon executing the deserialized
    # query.
    with pytest.raises(AssertionError, match="err_magic_2"):
        q.collect()


def test_credential_provider_serialization_custom_provider() -> None:
    err_magic = "err_magic_3"

    class ErrCredentialProvider(pl.CredentialProvider):
        def __call__(self) -> pl.CredentialProviderFunctionReturn:
            raise AssertionError(err_magic)

    lf = pl.scan_parquet(
        "s3://bucket/path", credential_provider=ErrCredentialProvider()
    )

    serialized = lf.serialize()

    lf = pl.LazyFrame.deserialize(io.BytesIO(serialized))

    with pytest.raises(AssertionError, match=err_magic):
        lf.collect()


def test_credential_provider_gcp_skips_config_autoload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_PATH", "__non_existent")

    with pytest.raises(OSError, match="__non_existent"):
        pl.scan_parquet("gs://.../...", credential_provider=None).collect()

    err_magic = "err_magic_3"

    def raises() -> pl.CredentialProviderFunctionReturn:
        raise AssertionError(err_magic)

    with pytest.raises(AssertionError, match=err_magic):
        pl.scan_parquet("gs://.../...", credential_provider=raises).collect()


def test_credential_provider_aws_import_error_with_requested_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _session(self: Any) -> None:
        msg = "err_magic_3"
        raise ImportError(msg)

    monkeypatch.setattr(pl.CredentialProviderAWS, "_session", _session)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    q = pl.scan_parquet(
        "s3://.../...",
        credential_provider=pl.CredentialProviderAWS(profile_name="test_profile"),
    )

    with pytest.raises(
        pl.exceptions.ComputeError,
        match=(
            "cannot load requested aws_profile 'test_profile': ImportError: err_magic_3"
        ),
    ):
        q.collect()

    q = pl.scan_parquet(
        "s3://.../...",
        storage_options={"aws_profile": "test_profile"},
    )

    with pytest.raises(
        pl.exceptions.ComputeError,
        match=(
            "cannot load requested aws_profile 'test_profile': ImportError: err_magic_3"
        ),
    ):
        q.collect()


@pytest.mark.slow
@pytest.mark.write_disk
def test_credential_provider_aws_endpoint_url_scan_no_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    tmp_path.mkdir(exist_ok=True)

    _set_default_credentials(tmp_path, monkeypatch)
    cfg_file_path = tmp_path / "config"

    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg_file_path))
    monkeypatch.setenv("POLARS_VERBOSE", "1")

    cfg_file_path.write_text("""\
[default]
endpoint_url = http://localhost:333
""")

    # Scan with no parameters should load via CredentialProviderAWS
    q = pl.scan_parquet("s3://.../...")

    capfd.readouterr()

    with pytest.raises(IOError, match=r"Error performing HEAD http://localhost:333"):
        q.collect()

    capture = capfd.readouterr().err
    lines = capture.splitlines()

    assert "[CredentialProviderAWS]: Loaded endpoint_url: http://localhost:333" in lines


@pytest.mark.slow
@pytest.mark.write_disk
def test_credential_provider_aws_endpoint_url_serde(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    tmp_path.mkdir(exist_ok=True)

    _set_default_credentials(tmp_path, monkeypatch)
    cfg_file_path = tmp_path / "config"

    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg_file_path))
    monkeypatch.setenv("POLARS_VERBOSE", "1")

    cfg_file_path.write_text("""\
[default]
endpoint_url = http://localhost:333
""")

    q = pl.scan_parquet("s3://.../...")
    q = pickle.loads(pickle.dumps(q))

    cfg_file_path.write_text("""\
[default]
endpoint_url = http://localhost:777
""")

    capfd.readouterr()

    with pytest.raises(IOError, match=r"Error performing HEAD http://localhost:777"):
        q.collect()


@pytest.mark.slow
@pytest.mark.write_disk
def test_credential_provider_aws_endpoint_url_with_storage_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    tmp_path.mkdir(exist_ok=True)

    _set_default_credentials(tmp_path, monkeypatch)
    cfg_file_path = tmp_path / "config"

    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg_file_path))
    monkeypatch.setenv("POLARS_VERBOSE", "1")

    cfg_file_path.write_text("""\
[default]
endpoint_url = http://localhost:333
""")

    # Previously we would not initialize a credential provider at all if secrets
    # were given under `storage_options`. Now we always initialize so that we
    # can load the `endpoint_url`, but we decide at the very last second whether
    # to also retrieve secrets using the credential provider.
    q = pl.scan_parquet(
        "s3://.../...",
        storage_options={
            "aws_access_key_id": "...",
            "aws_secret_access_key": "...",
        },
    )

    with pytest.raises(IOError, match=r"Error performing HEAD http://localhost:333"):
        q.collect()

    capture = capfd.readouterr().err
    lines = capture.splitlines()

    assert (
        "[CredentialProviderAWS]: Will not be used as a provider: unhandled key "
        "in storage_options: 'aws_secret_access_key'"
    ) in lines
    assert "[CredentialProviderAWS]: Loaded endpoint_url: http://localhost:333" in lines


@pytest.mark.parametrize(
    "storage_options",
    [
        {"aws_endpoint_url": "http://localhost:777"},
        {
            "aws_access_key_id": "...",
            "aws_secret_access_key": "...",
            "aws_endpoint_url": "http://localhost:777",
        },
    ],
)
@pytest.mark.slow
@pytest.mark.write_disk
def test_credential_provider_aws_endpoint_url_passed_in_storage_options(
    storage_options: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.mkdir(exist_ok=True)

    _set_default_credentials(tmp_path, monkeypatch)
    cfg_file_path = tmp_path / "config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg_file_path))

    cfg_file_path.write_text("""\
[default]
endpoint_url = http://localhost:333
""")

    q = pl.scan_parquet("s3://.../...")

    with pytest.raises(IOError, match=r"Error performing HEAD http://localhost:333"):
        q.collect()

    # An endpoint_url passed in `storage_options` should take precedence.
    q = pl.scan_parquet(
        "s3://.../...",
        storage_options=storage_options,
    )

    with pytest.raises(IOError, match=r"Error performing HEAD http://localhost:777"):
        q.collect()


def _set_default_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds_file_path = tmp_path / "credentials"

    creds_file_path.write_text("""\
[default]
aws_access_key_id=Z
aws_secret_access_key=Z
""")

    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds_file_path))


@pytest.mark.slow
def test_credential_provider_python_builder_cache(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Tests caching of building credential providers.
    def dummy_static_aws_credentials(*a: Any, **kw: Any) -> Any:
        return {
            "aws_access_key_id": "...",
            "aws_secret_access_key": "...",
        }, None

    with monkeypatch.context() as cx:
        provider_init = Mock(wraps=pl.CredentialProviderAWS.__init__)

        cx.setattr(
            pl.CredentialProviderAWS,
            "__init__",
            lambda *a, **kw: provider_init(*a, **kw),
        )

        cx.setattr(
            pl.CredentialProviderAWS,
            "retrieve_credentials_impl",
            dummy_static_aws_credentials,
        )

        # Ensure we are building a new query every time.
        def get_q() -> pl.LazyFrame:
            return pl.scan_parquet(
                "s3://.../...",
                storage_options={
                    "aws_profile": "A",
                    "aws_endpoint_url": "http://localhost",
                },
                credential_provider="auto",
            )

        assert provider_init.call_count == 0

        with pytest.raises(OSError):
            get_q().collect()

        assert provider_init.call_count == 1

        with pytest.raises(OSError):
            get_q().collect()

        assert provider_init.call_count == 1

        with pytest.raises(OSError):
            pl.scan_parquet(
                "s3://.../...",
                storage_options={
                    "aws_profile": "B",
                    "aws_endpoint_url": "http://localhost",
                },
                credential_provider="auto",
            ).collect()

        assert provider_init.call_count == 2

        with pytest.raises(OSError):
            get_q().collect()

        assert provider_init.call_count == 2

        cx.setenv("POLARS_CREDENTIAL_PROVIDER_BUILDER_CACHE_SIZE", "0")

        with pytest.raises(OSError):
            get_q().collect()

        # Note: Increments by 2 due to Rust-side object store rebuilding.

        assert provider_init.call_count == 4

        with pytest.raises(OSError):
            get_q().collect()

        assert provider_init.call_count == 6

    with monkeypatch.context() as cx:
        cx.setenv("POLARS_VERBOSE", "1")
        builder = _init_credential_provider_builder(
            "auto",
            "s3://.../...",
            None,
            "test",
        )
        assert builder is not None

        capfd.readouterr()

        builder.build_credential_provider()
        builder.build_credential_provider()

        capture = capfd.readouterr().err

        # Ensure cache key is memoized on generation
        assert capture.count("AutoInit cache key") == 1

        pickle.loads(pickle.dumps(builder)).build_credential_provider()

        capture = capfd.readouterr().err

        # Ensure cache key is not serialized
        assert capture.count("AutoInit cache key") == 1


@pytest.mark.slow
def test_credential_provider_python_credentials_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials_func = Mock(
        wraps=lambda: (
            {
                "aws_access_key_id": "...",
                "aws_secret_access_key": "...",
            },
            None,
        )
    )

    monkeypatch.setattr(
        pl.CredentialProviderAWS,
        "retrieve_credentials_impl",
        credentials_func,
    )

    assert credentials_func.call_count == 0

    provider = pl.CredentialProviderAWS()

    provider()
    assert credentials_func.call_count == 1

    provider()
    assert credentials_func.call_count == 1

    monkeypatch.setenv("POLARS_DISABLE_PYTHON_CREDENTIAL_CACHING", "1")

    provider()
    assert credentials_func.call_count == 2

    provider()
    assert credentials_func.call_count == 3

    monkeypatch.delenv("POLARS_DISABLE_PYTHON_CREDENTIAL_CACHING")

    provider()
    assert credentials_func.call_count == 4

    provider()
    assert credentials_func.call_count == 4

    assert provider._cached_credentials.get() is not None
    assert pickle.loads(pickle.dumps(provider))._cached_credentials.get() is None

    assert provider() == (
        {
            "aws_access_key_id": "...",
            "aws_secret_access_key": "...",
        },
        None,
    )

    provider()[0]["A"] = "A"

    assert provider() == (
        {
            "aws_access_key_id": "...",
            "aws_secret_access_key": "...",
        },
        None,
    )


def test_no_pickle_option() -> None:
    v = NoPickleOption(3)
    assert v.get() == 3

    out = pickle.loads(pickle.dumps(v))

    assert out.get() is None


@pytest.mark.write_disk
def test_credential_provider_aws_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential_file_path = tmp_path / "credentials.json"

    credential_file_path.write_text(
        """\
{
    "Version": 1,
    "AccessKeyId": "123",
    "SecretAccessKey": "456",
    "SessionToken": "789",
    "Expiration": "2099-01-01T00:00:00+00:00"
}
"""
    )

    cfg_file_path = tmp_path / "config"

    credential_file_path_str = str(credential_file_path).replace("\\", "/")

    cfg_file_path.write_text(f"""\
[profile cred_process]
credential_process = "{sys.executable}" -c "from pathlib import Path; print(Path('{credential_file_path_str}').read_text())"
""")

    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg_file_path))

    creds, expiry = pl.CredentialProviderAWS(profile_name="cred_process")()

    assert creds == {
        "aws_access_key_id": "123",
        "aws_secret_access_key": "456",
        "aws_session_token": "789",
    }

    assert expiry is not None

    assert datetime.fromtimestamp(expiry, tz=timezone.utc) == datetime.fromisoformat(
        "2099-01-01T00:00:00+00:00"
    )

    credential_file_path.write_text(
        """\
{
    "Version": 1,
    "AccessKeyId": "...",
    "SecretAccessKey": "...",
    "SessionToken": "..."
}
"""
    )

    creds, expiry = pl.CredentialProviderAWS(profile_name="cred_process")()

    assert creds == {
        "aws_access_key_id": "...",
        "aws_secret_access_key": "...",
        "aws_session_token": "...",
    }

    assert expiry is None


def test_lru_cache() -> None:
    def _test(cache: LRUCache[int, str]) -> None:
        with pytest.raises(ValueError):
            cache.set_max_items(-1)

        assert len(cache) == 0
        assert cache.max_items() == 2

        cache.insert(1, "1")
        cache.insert(2, "2")

        assert cache.get(2) == "2"
        assert cache.get(1) == "1"

        assert cache.contains(1)
        assert cache.contains(2)

        assert list(cache.keys()) == [2, 1]

        cache.insert(3, "3")

        # Note: We have 1, 3 due to cache.get() ordering above.
        # The calls to contains() should not shift the LRU order.
        assert list(cache.keys()) == [1, 3]

        cache.insert(4, "4")

        assert cache.contains(3)
        assert cache.contains(4)

        assert list(cache.keys()) == [3, 4]

        cache.remove(4)
        cache.insert(5, "5")

        assert list(cache.keys()) == [3, 5]

        assert cache.max_items() == 2
        assert len(cache) == 2

        cache.set_max_items(1)
        assert cache.max_items() == 1
        assert len(cache) == 1
        assert list(cache.keys()) == [5]

    cache: LRUCache[int, str] = LRUCache(2)

    _test(cache)

    cache.set_max_items(0)
    assert len(cache) == 0
    assert cache.max_items() == 0

    cache.insert(1, "1")
    assert len(cache.keys()) == 0
    assert not cache.contains(1)

    with pytest.raises(KeyError):
        cache.remove(1)

    cache.set_max_items(2)
    _test(cache)


@pytest.mark.slow
@pytest.mark.parametrize(
    (
        "credential_provider_class",
        "scan_path",
        "initial_credentials",
        "updated_credentials",
    ),
    [
        (
            pl.CredentialProviderAWS,
            "s3://.../...",
            {"aws_access_key_id": "initial", "aws_secret_access_key": "initial"},
            {"aws_access_key_id": "updated", "aws_secret_access_key": "updated"},
        ),
        (
            pl.CredentialProviderAzure,
            "abfss://container@storage_account.dfs.core.windows.net/bucket",
            {"bearer_token": "initial"},
            {"bearer_token": "updated"},
        ),
        (
            pl.CredentialProviderGCP,
            "gs://.../...",
            {"bearer_token": "initial"},
            {"bearer_token": "updated"},
        ),
    ],
)
def test_credential_provider_rebuild_clears_cache(
    credential_provider_class: type[CachingCredentialProvider],
    scan_path: str,
    initial_credentials: dict[str, str],
    updated_credentials: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert initial_credentials != updated_credentials

    monkeypatch.setattr(
        credential_provider_class,
        "retrieve_credentials_impl",
        lambda *_: (initial_credentials, None),
    )

    storage_options = (
        {"aws_endpoint_url": "http://localhost:333"}
        if credential_provider_class == pl.CredentialProviderAWS
        else None
    )

    builder = _init_credential_provider_builder(
        "auto",
        scan_path,
        storage_options=storage_options,
        caller_name="test",
    )

    assert builder is not None

    # This is a separate one for testing local to this function.
    provider_local = credential_provider_class()

    # Set the cache
    provider_local()

    # Now update the the retrieval function to return updated credentials.
    monkeypatch.setattr(
        credential_provider_class,
        "retrieve_credentials_impl",
        lambda *_: (updated_credentials, None),
    )

    # Despite "retrieve_credentials_impl" being updated, the providers should
    # still return the initial credentials, as they were cached with an expiry
    # of None.
    assert provider_local() == (initial_credentials, None)

    q = pl.scan_parquet(
        scan_path,
        storage_options=storage_options,
        credential_provider="auto",
    )

    with pytest.raises(OSError):
        q.collect()

    provider_at_scan = builder.build_credential_provider()

    assert provider_at_scan is not None
    assert provider_at_scan() == (updated_credentials, None)

    assert provider_local() == (initial_credentials, None)

    provider_local.clear_cached_credentials()

    assert provider_local() == (updated_credentials, None)


def test_user_gcp_token_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = UserProvidedGCPToken("A")
    assert provider() == ({"bearer_token": "A"}, None)
    monkeypatch.setenv("POLARS_DISABLE_PYTHON_CREDENTIAL_CACHING", "1")
    assert provider() == ({"bearer_token": "A"}, None)


def test_auto_init_cache_key_memoize(monkeypatch: pytest.MonkeyPatch) -> None:
    get_cache_key_impl = Mock(wraps=AutoInit.get_cache_key_impl)
    monkeypatch.setattr(
        AutoInit,
        "get_cache_key_impl",
        lambda *a, **kw: get_cache_key_impl(*a, **kw),
    )

    v = AutoInit(int)

    assert get_cache_key_impl.call_count == 0

    v.get_or_init_cache_key()
    assert get_cache_key_impl.call_count == 1

    v.get_or_init_cache_key()
    assert get_cache_key_impl.call_count == 1


def test_cached_credential_provider_returns_copied_creds() -> None:
    provider_func = Mock(wraps=lambda: ({"A": "A"}, None))
    provider = CachedCredentialProvider(provider_func)

    assert provider_func.call_count == 0

    provider()
    assert provider() == ({"A": "A"}, None)

    assert provider_func.call_count == 1

    provider()[0]["B"] = "B"

    assert provider() == ({"A": "A"}, None)

    assert provider_func.call_count == 1
