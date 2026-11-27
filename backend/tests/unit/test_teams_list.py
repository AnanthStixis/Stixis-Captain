from types import SimpleNamespace as N

from captain.api.admin_queries import same_team, team_names


class Q:
    def principals(self):
        return [N(team_id="Payments"), N(team_id=None), N(team_id=" payments ")]

    def keys(self, principal_id=None):
        return [N(project_id="HR"), N(project_id="general")]


def test_one_spelling_per_team():
    assert team_names(Q()) == ["general", "HR", "Payments"]
    assert same_team(Q(), "PAYMENTS") == "Payments"
    assert same_team(Q(), "Sales") == "Sales"
    assert same_team(Q(), "  ") is None
