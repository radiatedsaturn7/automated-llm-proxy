import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from gateway.config import Settings


@pytest.mark.parametrize("invalid", [{"db_pool_size": 0}, {"oidc_group_mapping": "[]"}])
def test_invalid_runtime_configuration_does_not_print_secret_inputs(invalid):
    secret='not-for-startup-traceback-'+'x'*40
    with pytest.raises(ValidationError) as error:
        Settings(gateway_master_key=Fernet.generate_key().decode(),gateway_signing_key=secret,
                 bootstrap_token=secret,**invalid)
    assert secret not in str(error.value)
    assert 'input_value' not in str(error.value)


@pytest.mark.parametrize('environment', ['prod', 'Production', '', 'staging'])
def test_unknown_environment_cannot_skip_production_checks(environment):
    with pytest.raises(ValidationError):
        Settings(gateway_master_key=Fernet.generate_key().decode(),
                 gateway_signing_key='s'*40, bootstrap_token='b'*40, environment=environment)


@pytest.mark.parametrize('url', ['https://', 'file:///tmp', 'https://user:secret@example.com',
                                 'https://example.com?token=secret', 'https://example.com#secret'])
def test_public_url_rejects_missing_origin_or_embedded_credentials(url):
    with pytest.raises(ValidationError):
        Settings(gateway_master_key=Fernet.generate_key().decode(),
                 gateway_signing_key='s'*40, bootstrap_token='b'*40, public_url=url)
