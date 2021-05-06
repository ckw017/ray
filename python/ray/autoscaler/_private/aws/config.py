from distutils.version import StrictVersion
from functools import lru_cache
from functools import partial
import itertools
import json
import os
import time
from typing import Any, Dict, List, Optional
import logging

import boto3
from botocore.config import Config
import botocore

from ray.autoscaler._private.constants import BOTO_MAX_RETRIES
from ray.autoscaler.tags import NODE_TYPE_LEGACY_HEAD, NODE_TYPE_LEGACY_WORKER
from ray.autoscaler._private.providers import _PROVIDER_PRETTY_NAMES
from ray.autoscaler._private.aws.utils import LazyDefaultDict, \
    handle_boto_error
from ray.autoscaler._private.cli_logger import cli_logger, cf
from ray.autoscaler._private.event_system import (CreateClusterEvent,
                                                  global_event_system)

logger = logging.getLogger(__name__)

RAY = "ray-autoscaler"
DEFAULT_RAY_INSTANCE_PROFILE = RAY + "-v1"
DEFAULT_RAY_IAM_ROLE = RAY + "-v1"
SECURITY_GROUP_TEMPLATE = RAY + "-{}"

DEFAULT_AMI_NAME = "AWS Deep Learning AMI (Ubuntu 18.04) V30.0"

# Obtained from https://aws.amazon.com/marketplace/pp/B07Y43P7X5 on 8/4/2020.
DEFAULT_AMI = {
    "us-east-1": "ami-029510cec6d69f121",  # US East (N. Virginia)
    "us-east-2": "ami-08bf49c7b3a0c761e",  # US East (Ohio)
    "us-west-1": "ami-0cc472544ce594a19",  # US West (N. California)
    "us-west-2": "ami-0a2363a9cff180a64",  # US West (Oregon)
    "ca-central-1": "ami-0a871851b2ab39f01",  # Canada (Central)
    "eu-central-1": "ami-049fb1ea198d189d7",  # EU (Frankfurt)
    "eu-west-1": "ami-0abcbc65f89fb220e",  # EU (Ireland)
    "eu-west-2": "ami-0755b39fd4dab7cbe",  # EU (London)
    "eu-west-3": "ami-020485d8df1d45530",  # EU (Paris)
    "sa-east-1": "ami-058a6883cbdb4e599",  # SA (Sao Paulo)
}

# todo: cli_logger should handle this assert properly
# this should probably also happens somewhere else
assert StrictVersion(boto3.__version__) >= StrictVersion("1.4.8"), \
    "Boto3 version >= 1.4.8 required, try `pip install -U boto3`"


def key_pair(i, region, key_name):
    """
    If key_name is not None, key_pair will be named after key_name.
    Returns the ith default (aws_key_pair_name, key_pair_path).
    """
    if i == 0:
        key_pair_name = ("{}_{}".format(RAY, region)
                         if key_name is None else key_name)
        return (key_pair_name,
                os.path.expanduser("~/.ssh/{}.pem".format(key_pair_name)))

    key_pair_name = ("{}_{}_{}".format(RAY, i, region)
                     if key_name is None else key_name + "_key-{}".format(i))
    return (key_pair_name,
            os.path.expanduser("~/.ssh/{}.pem".format(key_pair_name)))


# Suppress excessive connection dropped logs from boto
logging.getLogger("botocore").setLevel(logging.WARNING)

_log_info = {}


def reload_log_state(override_log_info):
    _log_info.update(override_log_info)


def get_log_state():
    return _log_info.copy()


def _set_config_info(**kwargs):
    """Record configuration artifacts useful for logging."""

    # todo: this is technically fragile iff we ever use multiple configs

    for k, v in kwargs.items():
        _log_info[k] = v


def _arn_to_name(arn):
    return arn.split(":")[-1].split("/")[-1]


def log_to_cli(config: dict) -> None:
    provider_name = _PROVIDER_PRETTY_NAMES.get("aws", None)

    cli_logger.doassert(provider_name is not None,
                        "Could not find a pretty name for the AWS provider.")

    head_node_type = config["head_node_type"]
    head_node_config = config["available_node_types"][head_node_type][
        "node_config"]

    with cli_logger.group("{} config", provider_name):

        def print_info(resource_string: str,
                       key: str,
                       src_key: str,
                       allowed_tags: Optional[List[str]] = None,
                       list_value: bool = False) -> None:
            if allowed_tags is None:
                allowed_tags = ["default"]

            node_tags = {}

            # set of configurations corresponding to `key`
            unique_settings = set()

            for node_type_key, node_type in config[
                    "available_node_types"].items():
                node_tags[node_type_key] = {}
                tag = _log_info[src_key][node_type_key]
                if tag in allowed_tags:
                    node_tags[node_type_key][tag] = True
                setting = node_type["node_config"].get(key)

                if list_value:
                    unique_settings.add(tuple(setting))
                else:
                    unique_settings.add(setting)

            head_value_str = head_node_config[key]
            if list_value:
                head_value_str = cli_logger.render_list(head_value_str)

            if len(unique_settings) == 1:
                # all node types are configured the same, condense
                # log output
                cli_logger.labeled_value(
                    resource_string + " (all available node types)",
                    "{}",
                    head_value_str,
                    _tags=node_tags[config["head_node_type"]])
            else:
                # do head node type first
                cli_logger.labeled_value(
                    resource_string + f" ({head_node_type})",
                    "{}",
                    head_value_str,
                    _tags=node_tags[head_node_type])

                # go through remaining types
                for node_type_key, node_type in config[
                        "available_node_types"].items():
                    if node_type_key == head_node_type:
                        continue
                    workers_value_str = node_type["node_config"][key]
                    if list_value:
                        workers_value_str = cli_logger.render_list(
                            workers_value_str)
                    cli_logger.labeled_value(
                        resource_string + f" ({node_type_key})",
                        "{}",
                        workers_value_str,
                        _tags=node_tags[node_type_key])

        tags = {"default": _log_info["head_instance_profile_src"] == "default"}
        profile_arn = head_node_config["IamInstanceProfile"].get("Arn")
        profile_name = _arn_to_name(profile_arn) \
            if profile_arn \
            else head_node_config["IamInstanceProfile"]["Name"]
        cli_logger.labeled_value("IAM Profile", "{}", profile_name, _tags=tags)

        if all("KeyName" in node_type["node_config"]
               for node_type in config["available_node_types"].values()):
            print_info("EC2 Key pair", "KeyName", "keypair_src")

        print_info("VPC Subnets", "SubnetIds", "subnet_src", list_value=True)
        print_info(
            "EC2 Security groups",
            "SecurityGroupIds",
            "security_group_src",
            list_value=True)
        print_info("EC2 AMI", "ImageId", "ami_src", allowed_tags=["dlami"])

    cli_logger.newline()


def bootstrap_aws(config):
    # Log warnings if user included deprecated `head_node` or `worker_nodes`
    # fields. Raise error if no `available_node_types`
    _check_legacy_fields(config)

    # The head node needs to have an IAM role that allows it to create further
    # EC2 instances.
    config = _configure_iam_role(config)

    # Configure SSH access, using an existing key pair if possible.
    config = _configure_key_pair(config)
    global_event_system.execute_callback(
        CreateClusterEvent.ssh_keypair_downloaded,
        {"ssh_key_path": config["auth"]["ssh_private_key"]})

    # Pick a reasonable subnet if not specified by the user.
    config = _configure_subnet(config)

    # Cluster workers should be in a security group that permits traffic within
    # the group, and also SSH access from outside.
    config = _configure_security_group(config)

    # Provide a helpful message for missing AMI.
    _check_ami(config)

    return config


def _check_legacy_fields(config):
    # log warning if non-empty head_node field
    if "head_node" in config and config["head_node"]:
        cli_logger.warning(
            "The `head_node` field is deprecated and will be ignored. "
            "Use `head_node_type` and `available_node_types` instead.")
    # log warning if non-empty worker_nodes field
    if "worker_nodes" in config and config["worker_nodes"]:
        cli_logger.warning(
            "The `worker_nodes` field is deprecated and will be ignored."
            "Use `available_node_types` instead.")
    if "available_node_types" not in config:
        cli_logger.error("`available_node_types` not specified in config")
        raise ValueError("`available_node_types` not specified in config")
    if "head_node_type" not in config:
        cli_logger.error("`head_node_type` not specified in config")
        raise ValueError("`head_node_type` not specified in config")


def _configure_iam_role(config):
    head_node_type = config["head_node_type"]
    head_node_config = config["available_node_types"][head_node_type][
        "node_config"]
    if "IamInstanceProfile" in head_node_config:
        _set_config_info(head_instance_profile_src="config")
        return config
    _set_config_info(head_instance_profile_src="default")

    profile = _get_instance_profile(DEFAULT_RAY_INSTANCE_PROFILE, config)

    if profile is None:
        cli_logger.verbose(
            "Creating new IAM instance profile {} for use as the default.",
            cf.bold(DEFAULT_RAY_INSTANCE_PROFILE))
        client = _client("iam", config)
        client.create_instance_profile(
            InstanceProfileName=DEFAULT_RAY_INSTANCE_PROFILE)
        profile = _get_instance_profile(DEFAULT_RAY_INSTANCE_PROFILE, config)
        time.sleep(15)  # wait for propagation

    cli_logger.doassert(profile is not None,
                        "Failed to create instance profile.")  # todo: err msg
    assert profile is not None, "Failed to create instance profile"

    if not profile.roles:
        role = _get_role(DEFAULT_RAY_IAM_ROLE, config)
        if role is None:
            cli_logger.verbose(
                "Creating new IAM role {} for "
                "use as the default instance role.",
                cf.bold(DEFAULT_RAY_IAM_ROLE))
            iam = _resource("iam", config)
            iam.create_role(
                RoleName=DEFAULT_RAY_IAM_ROLE,
                AssumeRolePolicyDocument=json.dumps({
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {
                                "Service": "ec2.amazonaws.com"
                            },
                            "Action": "sts:AssumeRole",
                        },
                    ],
                }))
            role = _get_role(DEFAULT_RAY_IAM_ROLE, config)

            cli_logger.doassert(role is not None,
                                "Failed to create role.")  # todo: err msg
            assert role is not None, "Failed to create role"
        role.attach_policy(
            PolicyArn="arn:aws:iam::aws:policy/AmazonEC2FullAccess")
        role.attach_policy(
            PolicyArn="arn:aws:iam::aws:policy/AmazonS3FullAccess")
        profile.add_role(RoleName=role.name)
        time.sleep(15)  # wait for propagation

    head_node_config["IamInstanceProfile"] = {"Arn": profile.arn}

    return config


def _configure_key_pair(config):
    node_types = config["available_node_types"]

    # map from node type key -> source of KeyName field
    key_pair_src_info = {}
    _set_config_info(keypair_src=key_pair_src_info)

    if "ssh_private_key" in config["auth"]:
        for node_type_key in node_types:
            # keypairs should be provided in the config
            key_pair_src_info[node_type_key] = "config"

        # If the key is not configured via the cloudinit
        # UserData, it should be configured via KeyName or
        # else we will risk starting a node that we cannot
        # SSH into:

        for node_type in node_types:
            node_config = node_types[node_type]["node_config"]
            if "UserData" not in node_config:
                cli_logger.doassert("KeyName" in node_config,
                                    _key_assert_msg(node_type))
                assert "KeyName" in node_config

        return config

    for node_type_key in node_types:
        key_pair_src_info[node_type_key] = "default"

    ec2 = _resource("ec2", config)

    # Writing the new ssh key to the filesystem fails if the ~/.ssh
    # directory doesn't already exist.
    os.makedirs(os.path.expanduser("~/.ssh"), exist_ok=True)

    # Try a few times to get or create a good key pair.
    MAX_NUM_KEYS = 30
    for i in range(MAX_NUM_KEYS):

        key_name = config["provider"].get("key_pair", {}).get("key_name")

        key_name, key_path = key_pair(i, config["provider"]["region"],
                                      key_name)
        key = _get_key(key_name, config)

        # Found a good key.
        if key and os.path.exists(key_path):
            break

        # We can safely create a new key.
        if not key and not os.path.exists(key_path):
            cli_logger.verbose(
                "Creating new key pair {} for use as the default.",
                cf.bold(key_name))
            key = ec2.create_key_pair(KeyName=key_name)

            # We need to make sure to _create_ the file with the right
            # permissions. In order to do that we need to change the default
            # os.open behavior to include the mode we want.
            with open(key_path, "w", opener=partial(os.open, mode=0o600)) as f:
                f.write(key.key_material)
            break

    if not key:
        cli_logger.abort(
            "No matching local key file for any of the key pairs in this "
            "account with ids from 0..{}. "
            "Consider deleting some unused keys pairs from your account.",
            key_name)  # todo: err msg
        raise ValueError(
            "No matching local key file for any of the key pairs in this "
            "account with ids from 0..{}. ".format(key_name) +
            "Consider deleting some unused keys pairs from your account.")

    cli_logger.doassert(
        os.path.exists(key_path), "Private key file " + cf.bold("{}") +
        " not found for " + cf.bold("{}"), key_path, key_name)  # todo: err msg
    assert os.path.exists(key_path), \
        "Private key file {} not found for {}".format(key_path, key_name)

    config["auth"]["ssh_private_key"] = key_path
    for node_type in node_types.values():
        node_config = node_type["node_config"]
        node_config["KeyName"] = key_name

    return config


def _key_assert_msg(node_type: str) -> str:
    if node_type == NODE_TYPE_LEGACY_WORKER:
        return "`KeyName` missing for worker nodes."
    elif node_type == NODE_TYPE_LEGACY_HEAD:
        return "`KeyName` missing for head node."
    else:
        return ("`KeyName` missing from the `node_config` of"
                f" node type `{node_type}`.")


def _configure_subnet(config):
    ec2 = _resource("ec2", config)
    use_internal_ips = config["provider"].get("use_internal_ips", False)

    # If head or worker security group is specified, filter down to subnets
    # belonging to the same VPC as the security group.
    sg_ids = []
    for node_type in config["available_node_types"].values():
        node_config = node_type["node_config"]
        sg_ids.extend(node_config.get("SecurityGroupIds", []))
    if sg_ids:
        vpc_id_of_sg = _get_vpc_id_of_sg(sg_ids, config)
    else:
        vpc_id_of_sg = None

    try:
        candidate_subnets = ec2.subnets.all()
        if vpc_id_of_sg:
            candidate_subnets = [
                s for s in candidate_subnets if s.vpc_id == vpc_id_of_sg
            ]
        subnets = sorted(
            (s for s in candidate_subnets if s.state == "available" and (
                use_internal_ips or s.map_public_ip_on_launch)),
            reverse=True,  # sort from Z-A
            key=lambda subnet: subnet.availability_zone)
    except botocore.exceptions.ClientError as exc:
        handle_boto_error(exc, "Failed to fetch available subnets from AWS.")
        raise exc

    if not subnets:
        cli_logger.abort(
            "No usable subnets found, try manually creating an instance in "
            "your specified region to populate the list of subnets "
            "and trying this again.\n"
            "Note that the subnet must map public IPs "
            "on instance launch unless you set `use_internal_ips: true` in "
            "the `provider` config.")  # todo: err msg
        raise Exception(
            "No usable subnets found, try manually creating an instance in "
            "your specified region to populate the list of subnets "
            "and trying this again. Note that the subnet must map public IPs "
            "on instance launch unless you set 'use_internal_ips': True in "
            "the 'provider' config.")
    if "availability_zone" in config["provider"]:
        azs = config["provider"]["availability_zone"].split(",")
        subnets = [s for s in subnets if s.availability_zone in azs]
        if not subnets:
            cli_logger.abort(
                "No usable subnets matching availability zone {} found.\n"
                "Choose a different availability zone or try "
                "manually creating an instance in your specified region "
                "to populate the list of subnets and trying this again.",
                config["provider"]["availability_zone"])  # todo: err msg
            raise Exception(
                "No usable subnets matching availability zone {} "
                "found. Choose a different availability zone or try "
                "manually creating an instance in your specified region "
                "to populate the list of subnets and trying this again.".
                format(config["provider"]["availability_zone"]))

    # Use subnets in only one VPC, so that _configure_security_groups only
    # needs to create a security group in this one VPC. Otherwise, we'd need
    # to set up security groups in all of the user's VPCs and set up networking
    # rules to allow traffic between these groups.
    # See https://github.com/ray-project/ray/pull/14868.
    subnet_ids = [
        s.subnet_id for s in subnets if s.vpc_id == subnets[0].vpc_id
    ]
    # map from node type key -> source of SubnetIds field
    subnet_src_info = {}
    _set_config_info(subnet_src=subnet_src_info)
    for key, node_type in config["available_node_types"].items():
        node_config = node_type["node_config"]
        if "SubnetIds" not in node_config:
            subnet_src_info[key] = "default"
            node_config["SubnetIds"] = subnet_ids
        else:
            subnet_src_info[key] = "config"

    return config


def _get_vpc_id_of_sg(sg_ids: List[str], config: Dict[str, Any]) -> str:
    """Returns the VPC id of the security groups with the provided security
    group ids.

    Errors if the provided security groups belong to multiple VPCs.
    Errors if no security group with any of the provided ids is identified.
    """
    sg_ids = list(set(sg_ids))

    ec2 = _resource("ec2", config)
    filters = [{"Name": "group-id", "Values": sg_ids}]
    security_groups = ec2.security_groups.filter(Filters=filters)
    vpc_ids = [sg.vpc_id for sg in security_groups]
    vpc_ids = list(set(vpc_ids))

    multiple_vpc_msg = "All security groups specified in the cluster config "\
        "should belong to the same VPC."
    cli_logger.doassert(len(vpc_ids) <= 1, multiple_vpc_msg)
    assert len(vpc_ids) <= 1, multiple_vpc_msg

    no_sg_msg = "Failed to detect a security group with id equal to any of "\
        "the configured SecurityGroupIds."
    cli_logger.doassert(len(vpc_ids) > 0, no_sg_msg)
    assert len(vpc_ids) > 0, no_sg_msg

    return vpc_ids[0]


def _configure_security_group(config):
    # map from node type key -> source of SecurityGroupIds field
    security_group_info_src = {}
    _set_config_info(security_group_src=security_group_info_src)

    for node_type_key in config["available_node_types"]:
        security_group_info_src[node_type_key] = "config"

    node_types_to_configure = [
        node_type_key
        for node_type_key, node_type in config["available_node_types"].items()
        if "SecurityGroupIds" not in node_type["node_config"]
    ]
    if not node_types_to_configure:
        return config  # have user-defined groups
    head_node_type = config["head_node_type"]
    if config["head_node_type"] in node_types_to_configure:
        # configure head node security group last for determinism
        # in tests
        node_types_to_configure.remove(head_node_type)
        node_types_to_configure.append(head_node_type)
    security_groups = _upsert_security_groups(config, node_types_to_configure)

    for node_type_key in node_types_to_configure:
        node_config = config["available_node_types"][node_type_key][
            "node_config"]
        sg = security_groups[node_type_key]
        node_config["SecurityGroupIds"] = [sg.id]
        security_group_info_src[node_type_key] = "default"

    return config


def _check_ami(config):
    """Provide helpful message for missing ImageId for node configuration."""

    # map from node type key -> source of ImageId field
    ami_src_info = {key: "config" for key in config["available_node_types"]}
    _set_config_info(ami_src=ami_src_info)

    region = config["provider"]["region"]
    default_ami = DEFAULT_AMI.get(region)
    if not default_ami:
        # If we do not provide a default AMI for the given region, noop.
        return

    for key, node_type in config["available_node_types"].items():
        node_config = node_type["node_config"]
        node_ami = node_config.get("ImageId", "").lower()
        if node_ami in ["", "latest_dlami"]:
            node_config["ImageId"] = default_ami
            ami_src_info[key] = "dlami"


def _upsert_security_groups(config, node_types):
    security_groups = _get_or_create_vpc_security_groups(config, node_types)
    _upsert_security_group_rules(config, security_groups)

    return security_groups


def _get_or_create_vpc_security_groups(conf, node_types):
    # Figure out which VPC each node_type is in...
    ec2 = _resource("ec2", conf)
    node_type_to_vpc = {
        node_type: _get_vpc_id_or_die(
            ec2,
            conf["available_node_types"][node_type]["node_config"]["SubnetIds"]
            [0],
        )
        for node_type in node_types
    }

    # Generate the name of the security group we're looking for...
    expected_sg_name = conf["provider"] \
        .get("security_group", {}) \
        .get("GroupName", SECURITY_GROUP_TEMPLATE.format(conf["cluster_name"]))

    # Figure out which security groups with this name exist for each VPC...
    vpc_to_existing_sg = {
        sg.vpc_id: sg
        for sg in _get_security_groups(
            conf,
            node_type_to_vpc.values(),
            [expected_sg_name],
        )
    }

    # Lazily create any security group we're missing for each VPC...
    vpc_to_sg = LazyDefaultDict(
        partial(_create_security_group, conf, group_name=expected_sg_name),
        vpc_to_existing_sg,
    )

    # Then return a mapping from each node_type to its security group...
    return {
        node_type: vpc_to_sg[vpc_id]
        for node_type, vpc_id in node_type_to_vpc.items()
    }


@lru_cache()
def _get_vpc_id_or_die(ec2, subnet_id):
    subnet = list(
        ec2.subnets.filter(Filters=[{
            "Name": "subnet-id",
            "Values": [subnet_id]
        }]))

    # TODO: better error message
    cli_logger.doassert(len(subnet) == 1, "Subnet ID not found: {}", subnet_id)
    assert len(subnet) == 1, "Subnet ID not found: {}".format(subnet_id)
    subnet = subnet[0]
    return subnet.vpc_id


def _get_security_group(config, vpc_id, group_name):
    security_group = _get_security_groups(config, [vpc_id], [group_name])
    return None if not security_group else security_group[0]


def _get_security_groups(config, vpc_ids, group_names):
    unique_vpc_ids = list(set(vpc_ids))
    unique_group_names = set(group_names)

    ec2 = _resource("ec2", config)
    existing_groups = list(
        ec2.security_groups.filter(Filters=[{
            "Name": "vpc-id",
            "Values": unique_vpc_ids
        }]))
    filtered_groups = [
        sg for sg in existing_groups if sg.group_name in unique_group_names
    ]
    return filtered_groups


def _create_security_group(config, vpc_id, group_name):
    client = _client("ec2", config)
    client.create_security_group(
        Description="Auto-created security group for Ray workers",
        GroupName=group_name,
        VpcId=vpc_id)
    security_group = _get_security_group(config, vpc_id, group_name)
    cli_logger.doassert(security_group,
                        "Failed to create security group")  # err msg

    cli_logger.verbose(
        "Created new security group {}",
        cf.bold(security_group.group_name),
        _tags=dict(id=security_group.id))
    cli_logger.doassert(security_group,
                        "Failed to create security group")  # err msg
    assert security_group, "Failed to create security group"
    return security_group


def _upsert_security_group_rules(conf, security_groups):
    sgids = {sg.id for sg in security_groups.values()}

    # Update sgids to include user-specified security groups.
    # This is necessary if the user specifies the head node type's security
    # groups but not the worker's, or vice-versa.
    for node_type in conf["available_node_types"]:
        sgids.update(conf["available_node_types"][node_type].get(
            "SecurityGroupIds", []))

    # sort security group items for deterministic inbound rule config order
    # (mainly supports more precise stub-based boto3 unit testing)
    for node_type, sg in sorted(security_groups.items()):
        sg = security_groups[node_type]
        if not sg.ip_permissions:
            _update_inbound_rules(sg, sgids, conf)


def _update_inbound_rules(target_security_group, sgids, config):
    extended_rules = config["provider"] \
        .get("security_group", {}) \
        .get("IpPermissions", [])
    ip_permissions = _create_default_inbound_rules(sgids, extended_rules)
    target_security_group.authorize_ingress(IpPermissions=ip_permissions)


def _create_default_inbound_rules(sgids, extended_rules=None):
    if extended_rules is None:
        extended_rules = []
    intracluster_rules = _create_default_intracluster_inbound_rules(sgids)
    ssh_rules = _create_default_ssh_inbound_rules()
    merged_rules = itertools.chain(
        intracluster_rules,
        ssh_rules,
        extended_rules,
    )
    return list(merged_rules)


def _create_default_intracluster_inbound_rules(intracluster_sgids):
    return [{
        "FromPort": -1,
        "ToPort": -1,
        "IpProtocol": "-1",
        "UserIdGroupPairs": [
            {
                "GroupId": security_group_id
            } for security_group_id in sorted(intracluster_sgids)
            # sort security group IDs for deterministic IpPermission models
            # (mainly supports more precise stub-based boto3 unit testing)
        ]
    }]


def _create_default_ssh_inbound_rules():
    return [{
        "FromPort": 22,
        "ToPort": 22,
        "IpProtocol": "tcp",
        "IpRanges": [{
            "CidrIp": "0.0.0.0/0"
        }]
    }]


def _get_role(role_name, config):
    iam = _resource("iam", config)
    role = iam.Role(role_name)
    try:
        role.load()
        return role
    except botocore.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return None
        else:
            handle_boto_error(
                exc, "Failed to fetch IAM role data for {} from AWS.",
                cf.bold(role_name))
            raise exc


def _get_instance_profile(profile_name, config):
    iam = _resource("iam", config)
    profile = iam.InstanceProfile(profile_name)
    try:
        profile.load()
        return profile
    except botocore.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return None
        else:
            handle_boto_error(
                exc,
                "Failed to fetch IAM instance profile data for {} from AWS.",
                cf.bold(profile_name))
            raise exc


def _get_key(key_name, config):
    ec2 = _resource("ec2", config)
    try:
        for key in ec2.key_pairs.filter(Filters=[{
                "Name": "key-name",
                "Values": [key_name]
        }]):
            if key.name == key_name:
                return key
    except botocore.exceptions.ClientError as exc:
        handle_boto_error(exc, "Failed to fetch EC2 key pair {} from AWS.",
                          cf.bold(key_name))
        raise exc


def _client(name, config):
    return _resource(name, config).meta.client


def _resource(name, config):
    region = config["provider"]["region"]
    aws_credentials = config["provider"].get("aws_credentials", {})
    return _resource_cache(name, region, **aws_credentials)


@lru_cache()
def _resource_cache(name, region, **kwargs):
    boto_config = Config(retries={"max_attempts": BOTO_MAX_RETRIES})
    return boto3.resource(
        name,
        region,
        config=boto_config,
        **kwargs,
    )
