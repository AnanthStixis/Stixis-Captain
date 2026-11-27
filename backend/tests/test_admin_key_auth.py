import pytest
from captain.api.admin_auth import AdminKeyAuthenticator
from captain.core.decision_api import AuthenticationError
def test_admin_key():
    k = "x"*40
    a = AdminKeyAuthenticator(k).authenticate(k, "10.0.0.5")
    assert "platform-admin" in a.roles
    with pytest.raises(AuthenticationError):
        AdminKeyAuthenticator(k).authenticate("y"*40, "10.0.0.5")
    with pytest.raises(ValueError):
        AdminKeyAuthenticator("short")
