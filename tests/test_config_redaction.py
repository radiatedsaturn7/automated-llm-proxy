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
