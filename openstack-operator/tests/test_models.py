"""Tests for data models."""

from models import (
    Phase,
    ConditionStatus,
    NetworkStatus,
    SecurityGroupStatus,
    Condition,
    ProjectStatus,
)


class TestNetworkStatus:
    """Tests for NetworkStatus dataclass."""

    def test_to_dict_minimal(self):
        status = NetworkStatus(
            name="test-net",
            network_id="net-123",
            subnet_id="subnet-456",
        )
        result = status.to_dict()

        assert result == {
            "name": "test-net",
            "networkId": "net-123",
            "subnetId": "subnet-456",
        }

    def test_to_dict_with_router(self):
        status = NetworkStatus(
            name="test-net",
            network_id="net-123",
            subnet_id="subnet-456",
            router_id="router-789",
        )
        result = status.to_dict()

        assert result["routerId"] == "router-789"

    def test_from_dict(self):
        data = {
            "name": "test-net",
            "networkId": "net-123",
            "subnetId": "subnet-456",
            "routerId": "router-789",
        }
        status = NetworkStatus.from_dict(data)

        assert status.name == "test-net"
        assert status.network_id == "net-123"
        assert status.subnet_id == "subnet-456"
        assert status.router_id == "router-789"


class TestSecurityGroupStatus:
    """Tests for SecurityGroupStatus dataclass."""

    def test_to_dict(self):
        status = SecurityGroupStatus(name="test-sg", id="sg-123")
        result = status.to_dict()

        assert result == {"name": "test-sg", "id": "sg-123"}

    def test_from_dict(self):
        data = {"name": "test-sg", "id": "sg-123"}
        status = SecurityGroupStatus.from_dict(data)

        assert status.name == "test-sg"
        assert status.id == "sg-123"


class TestCondition:
    """Tests for Condition dataclass."""

    def test_to_dict(self):
        condition = Condition(
            type="Ready",
            status=ConditionStatus.TRUE,
            reason="Completed",
            message="All done",
            last_transition_time="2024-01-01T00:00:00+00:00",
        )
        result = condition.to_dict()

        assert result == {
            "type": "Ready",
            "status": "True",
            "reason": "Completed",
            "message": "All done",
            "lastTransitionTime": "2024-01-01T00:00:00+00:00",
        }


class TestProjectStatus:
    """Tests for ProjectStatus dataclass."""

    def test_default_values(self):
        status = ProjectStatus()

        assert status.phase == Phase.PENDING
        assert status.project_id is None
        assert status.group_id is None
        assert status.networks == []
        assert status.security_groups == []
        assert status.conditions == []

    def test_to_dict_minimal(self):
        status = ProjectStatus()
        result = status.to_dict()

        assert result == {"phase": "Pending"}

    def test_to_dict_full(self):
        status = ProjectStatus(
            phase=Phase.READY,
            project_id="proj-123",
            group_id="group-456",
            networks=[NetworkStatus("net", "n1", "s1")],
            security_groups=[SecurityGroupStatus("sg", "sg1")],
            last_sync_time="2024-01-01T00:00:00+00:00",
        )
        result = status.to_dict()

        assert result["phase"] == "Ready"
        assert result["projectId"] == "proj-123"
        assert result["groupId"] == "group-456"
        assert len(result["networks"]) == 1
        assert len(result["securityGroups"]) == 1
        assert result["lastSyncTime"] == "2024-01-01T00:00:00+00:00"

    def test_from_dict(self):
        data = {
            "phase": "Ready",
            "projectId": "proj-123",
            "groupId": "group-456",
        }
        status = ProjectStatus.from_dict(data)

        assert status.phase == Phase.READY
        assert status.project_id == "proj-123"
        assert status.group_id == "group-456"

    def test_from_dict_invalid_phase(self):
        data = {"phase": "InvalidPhase"}
        status = ProjectStatus.from_dict(data)

        # Should default to PENDING for invalid phase
        assert status.phase == Phase.PENDING

    def test_set_condition(self):
        status = ProjectStatus()
        status.set_condition("Ready", ConditionStatus.TRUE, "Done", "Complete")

        assert len(status.conditions) == 1
        assert status.conditions[0].type == "Ready"
        assert status.conditions[0].status == ConditionStatus.TRUE
