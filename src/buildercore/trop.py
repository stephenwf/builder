"""
trop.py is a module that uses the Troposphere library to build up 
a AWS Cloudformation (CFN) template dynamically, using values from 
the projects file and a bunch of sensible defaults.

It's job is to return the correct CFN JSON given a dictionary of 
data called a `context`.

`cfngen.py` is in charge of constructing this data struct and writing 
it to the correct file etc."""

import re
from . import utils
from troposphere import GetAtt, Output, Ref, Template, ec2, rds, sns, sqs, Base64, route53, Parameter

from functools import partial
import logging
from .decorators import osissuefn
from .utils import first

LOG = logging.getLogger(__name__)

osissuefn("embarassing code. some of these constants should be pulled form project config or given better names.")

SECURITY_GROUP_TITLE = "StackSecurityGroup"
EC2_TITLE = 'EC2Instance'
RDS_TITLE = "AttachedDB"
RDS_SG_ID = "DBSecurityGroup"
DBSUBNETGROUP_TITLE = 'AttachedDBSubnet'
EXT_TITLE = "ExtraStorage"
EXT_MP_TITLE = "MountPoint"
R53_EXT_TITLE = "ExtDNS"
R53_EXT_PROD_TITLE = "ExtDNSProd"
R53_INT_TITLE = "IntDNS"

KEYPAIR = "KeyName"

def ingress(port, end_port=None, protocol='tcp', cidr='0.0.0.0/0'):
    if not end_port:
        end_port = port
    return ec2.SecurityGroupRule(**{
        'FromPort': port,
        'ToPort': end_port,
        'IpProtocol': protocol,
        'CidrIp': cidr
    })

def complex_ingress(struct):
    # it's just not that simple
    if not isinstance(struct, dict):
        port = struct
        return ingress(port)
    assert len(struct.items()) == 1, "port mapping struct must contain a single key: %r" % struct
    port, struct = first(struct.items())
    default_end_port = port
    default_cidr_ip = '0.0.0.0/0'
    default_protocol = 'tcp'
    return ingress(port, **{
        # TODO: rename 'guest' in project file to something less wrong
        'end_port': struct.get('guest', default_end_port),
        'protocol': struct.get('protocol', default_protocol),
        'cidr': struct.get('cidr-ip', default_cidr_ip),
    })

def security_group(group_id, vpc_id, ingress_structs, description=""):
    return ec2.SecurityGroup(group_id, **{
        'GroupDescription': description or 'security group',
        'VpcId': vpc_id,
        'SecurityGroupIngress': map(complex_ingress, ingress_structs)
    })

def ec2_security(context):
    assert 'ports' in context['project']['aws'], "Missing `ports` configuration in `aws` for '%s'" % context['stackname']

    return security_group(
        SECURITY_GROUP_TITLE,
        context['project']['aws']['vpc-id'],
        context['project']['aws']['ports']
    ) # list of strings or dicts

def rds_security(context):
    "returns a security group for the rds instance. this security group only allows access within the subnet"
    engine_ports = {
        'postgres': 5432,
        'mysql': 3306
    }
    ingress_ports = [engine_ports[context['project']['aws']['rds']['engine']]]
    return security_group("VPCSecurityGroup", \
       context['project']['aws']['vpc-id'], \
       ingress_ports, \
       "RDS DB security group")

#
#
#


def instance_tags(context):
    return [
        ec2.Tag('Name', context['stackname']),
        ec2.Tag('Owner', context['author']),
        ec2.Tag('Project', context['project_name']),
    ]

def ec2instance(context):
    lu = partial(utils.lu, context)
    project_ec2 = {
        "ImageId": lu('project.aws.ami'),
        "InstanceType": lu('project.aws.type'), # t2.small, m1.medium, etc
        "KeyName": Ref(KEYPAIR),
        "SecurityGroupIds": [Ref(SECURITY_GROUP_TITLE)],
        "SubnetId": lu('project.aws.subnet-id'), # ll: "subnet-1d4eb46a"
        "Tags": instance_tags(context),

        "UserData": Base64("""#!/bin/bash
echo %(build_vars)s > /etc/build-vars.json.b64""" % context),
    }
    return ec2.Instance(EC2_TITLE, **project_ec2)

def mkoutput(title, desc, val):
    if isinstance(val, tuple):
        val = GetAtt(val[0], val[1])
    return Output(title, Description=desc, Value=val)

def _ec2_outputs():
    # http://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/intrinsic-function-reference-getatt.html
    return [
        mkoutput("AZ", "Availability Zone of the newly created EC2 instance", ("EC2Instance", "AvailabilityZone")),
        mkoutput("InstanceId", "InstanceId of the newly created EC2 instance", Ref(EC2_TITLE)),

        # these values are generated by AWS
        mkoutput("PrivateIP", "Private IP address of the newly created EC2 instance", ("EC2Instance", "PrivateIp")),
        mkoutput("PublicIP", "Public IP address of the newly created EC2 instance", ("EC2Instance", "PublicIp")),
        mkoutput("PublicDNS", "Public DNSName of the newly created EC2 instance", ("EC2Instance", "PublicDnsName")),
        mkoutput("PrivateDNS", "Private DNSName of the newly created EC2 instance", ("EC2Instance", "PrivateDnsName")),
    ]

def rdsinstance(context):
    lu = partial(utils.lu, context)

    # db subnet *group*
    # it's expected the db subnets themselves are already created within the VPC
    # you just need to plug their ids into the project file.
    # not really sure if a subnet group is anything more meaningful than 'a collection of subnet ids'
    rsn = rds.DBSubnetGroup(DBSUBNETGROUP_TITLE, **{
        "DBSubnetGroupDescription": "a group of subnets for this rds instance.",
        "SubnetIds": lu('project.aws.rds.subnets'),
    })

    # rds security group. uses the ec2 security group
    vpcdbsg = rds_security(context)

    # db instance
    data = {
        'DBName': lu('rds_dbname'), # dbname generated from instance id.
        'DBInstanceIdentifier': lu('rds_instance_id'), # ll: 'lax-2015-12-31' from 'lax--2015-12-31'
        'PubliclyAccessible': False,
        'AllocatedStorage': lu('project.aws.rds.storage'),
        'StorageType': 'Standard',
        'MultiAZ': lu('project.aws.rds.multi-az'),
        'VPCSecurityGroups': [Ref(vpcdbsg)],
        'DBSubnetGroupName': Ref(rsn),
        'DBInstanceClass': lu('project.aws.rds.type'),
        'Engine': lu('project.aws.rds.engine'),
        'MasterUsername': lu('rds_username'), # pillar data is now UNavailable
        'MasterUserPassword': lu('rds_password'),
        'BackupRetentionPeriod': lu('project.aws.rds.backup-retention'),
        'DeletionPolicy': 'Snapshot',
        "Tags": instance_tags(context),
        "AllowMajorVersionUpgrade": False, # default? not specified.
        "AutoMinorVersionUpgrade": True, # default
        # something is converting this value to an int :(
        "EngineVersion": str(lu('project.aws.rds.version')), # 'defaults.aws.rds.storage')),
    }
    rdbi = rds.DBInstance(RDS_TITLE, **data)
    return rsn, rdbi, vpcdbsg

def ext_volume(context_ext):
    vtype = context_ext.get('type', 'standard')
    # who cares what gp2 stands for? everyone knows what 'ssd' and 'standard' mean ...
    if vtype == 'ssd':
        vtype = 'gp2'
    
    args = {
        "Size": str(context_ext['size']),
        "AvailabilityZone": GetAtt(EC2_TITLE, "AvailabilityZone"),
        "VolumeType": vtype,
    }
    ec2v = ec2.Volume(EXT_TITLE, **args)
    
    args = {
        "InstanceId": Ref(EC2_TITLE),
        "VolumeId": Ref(ec2v),
        "Device": context_ext['device'],
    }
    ec2va = ec2.VolumeAttachment(EXT_MP_TITLE, **args)
    return ec2v, ec2va

def external_dns(context):
    dns_records = []
    # The DNS name of an existing Amazon Route 53 hosted zone
    hostedzone = context['domain'] + "." # TRAILING DOT IS IMPORTANT!
    if context['is_prod_instance']:
        dns_records.append(_dns_a_record(
            title=R53_EXT_PROD_TITLE,
            hostname=context['project_hostname'],
            hostedzone=hostedzone,
            comment="External DNS record, canonical project hostname for production"
        ))
    dns_records.append(_dns_a_record(
        title=R53_EXT_TITLE,
        hostname=context['full_hostname'],
        hostedzone=hostedzone,
        comment="External DNS record"
    ))
    return dns_records

def internal_dns(context):
    # TODO: use _dns_a_record()
    # The DNS name of an existing Amazon Route 53 hosted zone
    hostedzone = context['int_domain'] + "." # TRAILING DOT IS IMPORTANT!
    return _dns_a_record(
        title= R53_INT_TITLE,
        hostname = context['int_full_hostname'],
        hostedzone = hostedzone,
        comment = "Internal DNS record"
    )
    
def _dns_a_record(title, hostname, hostedzone, comment=''):
    return route53.RecordSetType(
        title,
        HostedZoneName = hostedzone,
        Comment = comment,
        Name = hostname,
        Type = "A",
        TTL = "900",
        ResourceRecords = [GetAtt(EC2_TITLE, "PublicIp")],
    )

def render(context):
    template = Template()
    cfn_outputs = []

    if context['ec2']:
        secgroup = ec2_security(context)
        instance = ec2instance(context)

        template.add_resource(secgroup)
        template.add_resource(instance)

        template.add_parameter(Parameter(KEYPAIR, **{
            "Type": "String",
            "Description": "EC2 KeyPair that enables SSH access to this instance",
        }))
        cfn_outputs.extend(_ec2_outputs())

    if context['rds_instance_id']:
        map(template.add_resource, rdsinstance(context))
        cfn_outputs.extend([
            mkoutput("RDSHost", "Connection endpoint for the DB cluster", (RDS_TITLE, "Endpoint.Address")),
            mkoutput("RDSPort", "The port number on which the database accepts connections", (RDS_TITLE, "Endpoint.Port")),])
    
    if context['ext']:
        map(template.add_resource, ext_volume(context['ext']))

    for topic_name in context['sns']:
        topic = template.add_resource(sns.Topic(
            _sanitize_title(topic_name) + "Topic",
            TopicName=topic_name
        ))
        template.add_output(Output(
            _sanitize_title(topic_name) + "TopicArn",
            Value=Ref(topic)
        ))

    for queue_name in context['sqs']:
        queue = template.add_resource(sqs.Queue(
            _sanitize_title(queue_name) + "Queue", 
            QueueName=queue_name
        ))
        template.add_output(Output(
            _sanitize_title(queue_name) + "QueueArn",
            Value=GetAtt(queue, "Arn")
        ))

    if context['full_hostname']:
        dns = external_dns(context)
        for resource in dns:
            template.add_resource(resource)
            
        cfn_outputs.extend([
            mkoutput("DomainName", "Domain name of the newly created EC2 instance", Ref(dns[0].title)),
        ])

    if context['int_full_hostname']:
        template.add_resource(internal_dns(context))        
        cfn_outputs.extend([
            mkoutput("IntDomainName", "Domain name of the newly created EC2 instance", Ref(R53_INT_TITLE))
        ])

    map(template.add_output, cfn_outputs)
    return template.to_json()

def _sanitize_title(string):
    return "".join(map(str.capitalize, string.split("-")))
