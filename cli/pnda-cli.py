#!/usr/bin/python
# -*- coding: utf-8 -*-
#
#   Copyright (c) 2016 Cisco and/or its affiliates.
#   This software is licensed to you under the terms of the Apache License, Version 2.0
#   (the "License").
#   You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#   The code, technical concepts, and all information contained herein, are the property of
#   Cisco Technology, Inc.and/or its affiliated entities, under various laws including copyright,
#   international treaties, patent, and/or contract.
#   Any use of the material herein must be in accordance with the terms of the License.
#   All rights not expressly granted by the License are reserved.
#   Unless required by applicable law or agreed to separately in writing, software distributed
#   under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
#   ANY KIND, either express or implied.
#
#   Purpose: Script to create PNDA on Amazon Web Services EC2

import re
import subprocess
import sys
import os
import os.path
import json
import time
import logging
import atexit
import traceback
import datetime
from threading import Thread

import requests
import argparse
from argparse import RawTextHelpFormatter
import boto.cloudformation
import boto.ec2

import subprocess_to_log


os.chdir(os.path.dirname(os.path.abspath(__file__)))

LOG_FILE_NAME = 'logs/pnda-cli.%s.log' % time.time()
logging.basicConfig(filename=LOG_FILE_NAME,
                    level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
LOG = logging.getLogger('everything')
CONSOLE = logging.getLogger('console')
CONSOLE.addHandler(logging.StreamHandler())

NAME_REGEX = r"^[\.a-zA-Z0-9-]+$"
VALIDATION_RULES = None
START = datetime.datetime.now()

def banner():
    print "🐼  🐼  🐼  🐼  🐼  🐼  🐼"
    print "   P N D A - C L I"
    print "🐼  🐼  🐼  🐼  🐼  🐼  🐼"

def run_cmd(cmd):
    print cmd
    os.spawnvpe(os.P_WAIT, cmd[0], cmd, os.environ)

@atexit.register
def display_elasped():
    blue = '\033[94m'
    reset = '\033[0m'
    elapsed = datetime.datetime.now() - START
    CONSOLE.info("%sTotal execution time: %s%s", blue, str(elapsed), reset)

def generate_template_file(filepath, datanodes, opentsdbs, kafkas, zookeepers):
    with open(filepath, 'r') as template_file:
        template_data = json.loads(template_file.read())
        instance_cdh_dn = json.dumps(template_data['Resources'].pop('instanceCdhDn'))
        instance_open_tsdb = json.dumps(template_data['Resources'].pop('instanceOpenTsdb'))
        instance_kafka = json.dumps(template_data['Resources'].pop('instanceKafka'))
        instance_zookeeper = json.dumps(template_data['Resources'].pop('instanceZookeeper'))

        for datanode in range(1, datanodes + 1):
            instance_cdh_dn_n = instance_cdh_dn.replace('$node_idx$', str(datanode))
            template_data['Resources']['instanceCdhDn%s' % datanode] = json.loads(instance_cdh_dn_n)

        for opentsdb in range(1, opentsdbs + 1):
            instance_open_tsdb_n = instance_open_tsdb.replace('$node_idx$', str(opentsdb))
            template_data['Resources']['instanceOpenTsdb%s' % opentsdb] = json.loads(instance_open_tsdb_n)

        for kafka in range(1, kafkas + 1):
            instance_kafka_n = instance_kafka.replace('$node_idx$', str(kafka))
            template_data['Resources']['instanceKafka%s' % kafka] = json.loads(instance_kafka_n)

        for zookeeper in range(1, zookeepers + 1):
            instance_zookeeper_n = instance_zookeeper.replace('$node_idx$', str(zookeeper))
            template_data['Resources']['instanceZookeeper%s' % zookeeper] = json.loads(instance_zookeeper_n)

    return json.dumps(template_data)

def get_instance_map(cluster):
    CONSOLE.debug('Checking details of created instances')
    region = os.environ['AWS_REGION']
    ec2 = boto.ec2.connect_to_region(region)
    reservations = ec2.get_all_reservations()
    instance_map = {}
    for reservation in reservations:
        for instance in reservation.instances:
            if 'pnda_cluster' in instance.tags and instance.tags['pnda_cluster'] == cluster and instance.state == 'running':
                CONSOLE.debug(instance.private_ip_address, ' ', instance.tags['Name'])
                instance_map[instance.tags['Name']] = {
                    "public_dns": instance.public_dns_name,
                    "ip_address": instance.ip_address,
                    "private_ip_address":instance.private_ip_address,
                    "name": instance.tags['Name'],
                    "node_idx": instance.tags['node_idx'],
                    "node_type": instance.tags['node_type']
                }
    return instance_map

def get_current_node_counts(cluster):
    CONSOLE.debug('Counting existing instances')
    node_counts = {}
    for _, instance in get_instance_map(cluster).iteritems():
        if instance['node_type'] in node_counts:
            current_count = node_counts[instance['node_type']]
        else:
            current_count = 0
        node_counts[instance['node_type']] = current_count + 1
    return node_counts

def scp(files, host):
    cmd = "scp -F cli/ssh_config %s %s:%s" % (' '.join(files), host, '/tmp')
    CONSOLE.debug(cmd)
    ret_val = subprocess_to_log.call(cmd.split(' '), LOG, host)
    if ret_val != 0:
        raise Exception("Error transfering files to new host %s via SCP. See debug log (%s) for details." % (host, LOG_FILE_NAME))

def ssh(cmds, host):
    cmd = "ssh -F cli/ssh_config %s" % host
    parts = cmd.split(' ')
    parts.append(';'.join(cmds))
    CONSOLE.debug(parts)
    ret_val = subprocess_to_log.call(parts, LOG, host, scan_for_errors=['lost connection'])
    if ret_val != 0:
        raise Exception("Error running ssh commands on host %s. See debug log (%s) for details." % (host, LOG_FILE_NAME))

def bootstrap(instance, saltmaster, cluster, flavour):
    ret_val = None
    try:
        ip_address = instance['private_ip_address']
        CONSOLE.debug('bootstrapping %s', ip_address)
        node_type = instance['node_type']
        type_script = 'bootstrap-scripts/%s/%s.sh' % (flavour, node_type)
        if not os.path.isfile(type_script):
            type_script = 'bootstrap-scripts/%s.sh' % (node_type)
        node_idx = instance['node_idx']
        scp(['pnda_env.sh', 'bootstrap-scripts/base.sh', type_script], ip_address)
        ssh(['source /tmp/pnda_env.sh',
             'export PNDA_SALTMASTER_IP=%s' % saltmaster,
             'export PNDA_CLUSTER=%s' % cluster,
             'export PNDA_FLAVOR=%s' % flavour,
             'sudo chmod a+x /tmp/base.sh',
             'sudo -E /tmp/base.sh',
             'sudo chmod a+x /tmp/%s.sh' % node_type,
             'sudo -E /tmp/%s.sh %s' % (node_type, node_idx)], ip_address)
    except:
        ret_val = 'Error for host %s. %s' % (instance['name'], traceback.format_exc())
    return ret_val

def check_environment_variables():
    try:
        region = os.environ['AWS_REGION']
        CONSOLE.debug('AWS region is %s', region)
        CONSOLE.info('Env variables.... OK')
    except:
        CONSOLE.info('Env variables.... ERROR')
        CONSOLE.error('Missing required environment variables, run "source ../client_env.sh" and try again.')
        sys.exit(1)

def check_keypair(keyname, keyfile):
    if not os.path.isfile(keyfile):
        CONSOLE.info('Keyfile.......... ERROR')
        CONSOLE.error('Did not find local file named %s' % keyfile)
        sys.exit(1)

    try:
        region = os.environ['AWS_REGION']
        ec2 = boto.ec2.connect_to_region(region)
        stored_key = ec2.get_key_pair(keyname)
        if stored_key is None:
            raise Exception("Key not found %s" % keyname)
        CONSOLE.info('Keyfile.......... OK')
    except:
        CONSOLE.info('Keyfile.......... ERROR')
        CONSOLE.error('Failed to find key %s in ec2.' % keyname)
        CONSOLE.error(traceback.format_exc())
        sys.exit(1)


def check_aws_connection():
    region = os.environ['AWS_REGION']
    conn = boto.cloudformation.connect_to_region(region)
    if conn is None:
        CONSOLE.info('AWS connection... ERROR')        
        CONSOLE.error('Failed to query cloud formation API, verify config in "client_env.sh" and try again.')
        sys.exit(1)

    try:
        conn.list_stacks()
        CONSOLE.info('AWS connection... OK')
    except:
        CONSOLE.info('AWS connection... ERROR')
        CONSOLE.error('Failed to query cloud formation API, verify config in "client_env.sh" and try again.')
        CONSOLE.error(traceback.format_exc())
        sys.exit(1)

def check_java_mirror(pnda_env):
    try:
        java_mirror = pnda_env['JAVA_MIRROR']
        response = requests.head(java_mirror)
        response.raise_for_status()
        CONSOLE.info('Java mirror...... OK')
    except KeyError:
        CONSOLE.info('Java mirror...... WARN')
        CONSOLE.warning('Java mirror was not defined in pnda_env.sh,' +
                        ' provisioning will be more reliable and quicker if you host this in the same AWS availability zone.')
    except:
        CONSOLE.info('Java mirror...... ERROR')
        CONSOLE.error('Failed to connect to java mirror. Verify connection to %s, update config in pnda_env.sh if required and try again.', '')
        CONSOLE.error(traceback.format_exc())
        sys.exit(1)

def check_package_server(pnda_env):
    try:
        package_uri = '%s/%s' % (pnda_env['PACKAGES_SERVER_URI'], 'platform/releases/')
        response = requests.head(package_uri)
        if response.status_code != 403 and response.status_code != 200:
            raise Exception("Unexpected status code from %s: %s" % (package_uri, response.status_code))
        CONSOLE.info('Package server... OK')
    except:
        CONSOLE.info('Package server... ERROR')
        CONSOLE.error('Failed to connect to package server. Verify connection to %s, update URL in pnda_env.sh if required and try again.', package_uri)
        CONSOLE.error(traceback.format_exc())
        sys.exit(1)

def write_ssh_config(bastion_ip, os_user, keyfile):
    with open('cli/ssh_config', 'w') as config_file:
        config_file.write('host *\n')
        config_file.write('    User %s\n' % os_user)
        config_file.write('    IdentityFile %s\n' % keyfile)
        config_file.write('    StrictHostKeyChecking no\n')
        config_file.write('    UserKnownHostsFile /dev/null\n')
        config_file.write('    ProxyCommand ssh -i %s -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null %s@%s exec nc %%h %%p\n'
                          % (keyfile, os_user, bastion_ip))

    with open('cli/socks_proxy', 'w') as config_file:
        config_file.write('eval `ssh-agent`\n')
        config_file.write('ssh-add %s\n' % keyfile)
        config_file.write('ssh -i %s -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -A -D 9999 %s@%s\n' % (keyfile, os_user, bastion_ip))

def create(template_data, cluster, flavour, keyname, no_config_check):
    keyfile = '%s.pem' % keyname
    #load these from env variables from client_env.sh

    if not no_config_check:
        CONSOLE.info('Checking configuration...')
        check_environment_variables()

    region = os.environ['AWS_REGION']
    image_id = os.environ['AWS_IMAGE_ID']
    whitelist = os.environ['AWS_ACCESS_WHITELIST']

    if not no_config_check:
        settings_file_name = 'pnda_env.sh'
        settings_file_contents = subprocess.Popen(['bash', '-c', 'source {} && env'.format(settings_file_name)], stdout=subprocess.PIPE).stdout
        pnda_env = {entry_parts[0].strip(): entry_parts[1].strip() for entry_parts in [entry.strip().split('=', 1) for entry in settings_file_contents]}
        check_aws_connection()
        check_keypair(keyname, keyfile)
        check_package_server(pnda_env)
        check_java_mirror(pnda_env)

    CONSOLE.info('Creating Cloud Formation stack')
    conn = boto.cloudformation.connect_to_region(region)
    stack_status = 'CREATING'
    conn.create_stack(cluster,
                      template_body=template_data,
                      parameters=[('imageId', image_id),
                                  ('keyName', keyname),
                                  ('pndaCluster', cluster),
                                  ('whitelistSshAccess', whitelist),
                                  ('whitelistUiAccess', whitelist)])

    while stack_status in ['CREATE_IN_PROGRESS', 'CREATING']:
        time.sleep(5)
        CONSOLE.info('Stack is: ' + stack_status)
        stacks = conn.describe_stacks(cluster)
        if len(stacks) > 0:
            stack_status = stacks[0].stack_status

    if stack_status != 'CREATE_COMPLETE':
        CONSOLE.error('Stack did not come up, status is: ' + stack_status)
        sys.exit(1)

    instance_map = get_instance_map(cluster)
    write_ssh_config(instance_map[cluster+'-bastion']['ip_address'], os.environ['OS_USER'], os.path.abspath(keyfile))
    CONSOLE.debug('The PNDA console will come up on: http://%s', instance_map[cluster+'-cdh-edge']['private_ip_address'])

    CONSOLE.info('Bootstrapping saltmaster. Expect this to take a few minutes, check the debug log for progress (%s).', LOG_FILE_NAME)
    saltmaster = instance_map[cluster+'-saltmaster']['private_ip_address']
    scp(['bootstrap-scripts/saltmaster.sh', 'pnda_env.sh', 'client_env.sh', 'git.pem'], instance_map[cluster+'-saltmaster']['private_ip_address'])
    ssh(['source /tmp/client_env.sh',
         'source /tmp/pnda_env.sh',
         'export PNDA_SALTMASTER_IP=%s' % saltmaster,
         'export PNDA_CLUSTER=%s' % cluster,
         'export PNDA_FLAVOR=%s' % flavour,
         'sudo chmod a+x /tmp/saltmaster.sh',
         'sudo -E /tmp/saltmaster.sh'],
        instance_map[cluster+'-saltmaster']['private_ip_address'])

    CONSOLE.info('Bootstrapping other instances. Expect this to take a few minutes, check the debug log for progress (%s).', LOG_FILE_NAME)
    bootstrap_threads = []
    for key, instance in instance_map.iteritems():
        if 'saltmaster' not in key:
            thread = Thread(target=bootstrap, args=[instance, saltmaster, cluster, flavour])
            bootstrap_threads.append(thread)

    for thread in bootstrap_threads:
        thread.start()
        time.sleep(2)

    for thread in bootstrap_threads:
        ret_val = thread.join()
        if ret_val is not None:
            raise Exception("Error bootstrapping host, error msg: %s. See debug log (%s) for details." % (ret_val, LOG_FILE_NAME))

    time.sleep(30)

    CONSOLE.info('Running salt to install software. Expect this to take 45 minutes or more, check the debug log for progress (%s).', LOG_FILE_NAME)
    ssh(['sudo salt -v --log-level=debug --state-output=mixed "*" state.highstate',
         'sudo CLUSTER=%s salt-run --log-level=debug state.orchestrate orchestrate.pnda' % cluster,
         'sudo salt "*-bastion" state.sls hostsfile'],
        instance_map[cluster+'-saltmaster']['private_ip_address'])
    return instance_map[cluster+'-cdh-edge']['private_ip_address']

def expand(template_data, cluster, flavour, old_datanodes, old_kafka, keyname):
    keyfile = '%s.pem' % keyname
    #load these from env variables from client_env.sh
    region = os.environ['AWS_REGION']
    image_id = os.environ['AWS_IMAGE_ID']
    whitelist = os.environ['AWS_ACCESS_WHITELIST']

    CONSOLE.info('Updating Cloud Formation stack')
    conn = boto.cloudformation.connect_to_region(region)
    stack_status = 'UPDATING'
    conn.update_stack(cluster,
                      template_body=template_data,
                      parameters=[('imageId', image_id),
                                  ('keyName', keyname),
                                  ('pndaCluster', cluster),
                                  ('whitelistSshAccess', whitelist),
                                  ('whitelistUiAccess', whitelist)])

    while stack_status in ['UPDATE_IN_PROGRESS', 'UPDATING', 'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS']:
        time.sleep(5)
        CONSOLE.info('Stack is: ' + stack_status)
        stacks = conn.describe_stacks(cluster)
        if len(stacks) > 0:
            stack_status = stacks[0].stack_status

    if stack_status != 'UPDATE_COMPLETE':
        CONSOLE.error('Stack did not come up, status is: ' + stack_status)
        sys.exit(1)

    instance_map = get_instance_map(cluster)
    write_ssh_config(instance_map[cluster+'-bastion']['ip_address'], os.environ['OS_USER'], os.path.abspath(keyfile))
    saltmaster = instance_map[cluster+'-saltmaster']['private_ip_address']

    CONSOLE.info('Bootstrapping new instances. Expect this to take a few minutes, check the debug log for progress. (%s)', LOG_FILE_NAME)
    bootstrap_threads = []
    for _, instance in instance_map.iteritems():
        if ((instance['node_type'] == 'cdh-dn' and int(instance['node_idx']) > old_datanodes
             or instance['node_type'] == 'kafka' and int(instance['node_idx']) > old_kafka)):
            thread = Thread(target=bootstrap, args=[instance, saltmaster, cluster, flavour])
            bootstrap_threads.append(thread)

    for thread in bootstrap_threads:
        thread.start()
        time.sleep(2)

    for thread in bootstrap_threads:
        ret_val = thread.join()
        if ret_val is not None:
            raise Exception("Error bootstrapping host, error msg: %s. See debug log (%s) for details." % (ret_val, LOG_FILE_NAME))

    time.sleep(30)

    CONSOLE.info('Running salt to install software. Expect this to take 10 - 20 minutes, check the debug log for progress. (%s)', LOG_FILE_NAME)
    ssh(['sudo salt -v --log-level=debug --state-output=mixed "*" state.highstate',
         'sudo CLUSTER=%s salt-run --log-level=debug state.orchestrate orchestrate.pnda-expand' % cluster,
         'sudo salt "*-bastion" state.sls hostsfile'],
        instance_map[cluster+'-saltmaster']['private_ip_address'])
    return instance_map[cluster+'-cdh-edge']['private_ip_address']

def destroy(cluster):
    CONSOLE.info('Deleting Cloud Formation stack')
    region = os.environ['AWS_REGION']
    conn = boto.cloudformation.connect_to_region(region)

    stack_status = 'DELETING'
    conn.delete_stack(cluster)
    while stack_status in ['DELETE_IN_PROGRESS', 'DELETING']:
        time.sleep(5)
        CONSOLE.info('Stack is: ' + stack_status)
        try:
            stacks = conn.describe_stacks(cluster)
        except:
            stacks = []

        if len(stacks) > 0:
            stack_status = stacks[0].stack_status
        else:
            stack_status = None

def name_string(value):
    try:
        return re.match(NAME_REGEX, value).group(0)
    except:
        raise argparse.ArgumentTypeError("String '%s' may contain only  a-z 0-9 and '-'" % value)

def get_validation(param_name):
    return VALIDATION_RULES[param_name]

def check_validation(restriction, value):
    if restriction.startswith("<="):
        return value <= int(restriction[2:])

    if restriction.startswith(">="):
        return value > int(restriction[2:])

    if restriction.startswith("<"):
        return value < int(restriction[1:])

    if restriction.startswith(">"):
        return value > int(restriction[1:])

    if "-" in restriction:
        restrict_min = int(restriction.split('-')[0])
        restrict_max = int(restriction.split('-')[1])
        return value >= restrict_min and value <= restrict_max

    return value == int(restriction)

def validate_size(param_name, value):
    restrictions = get_validation(param_name)
    for restriction in restrictions.split(','):
        if check_validation(restriction, value):
            return True
    return False

def node_limit(param_name, value):
    as_num = None
    try:
        as_num = int(value)
    except:
        raise argparse.ArgumentTypeError("'%s' must be an integer, %s found" % (param_name, value))

    if not validate_size(param_name, as_num):
        raise argparse.ArgumentTypeError("'%s' is not in valid range %s" % (as_num, get_validation(param_name)))

    return as_num

def get_args():
    epilog = """examples:
  - create new cluster, prompting for values:
    pnda-cli.py create
  - destroy existing cluster:
    pnda-cli.py destroy -e squirrel-land
  - expand existing cluster:
    pnda-cli.py expand -e squirrel-land -f standard -s keyname -n 10 -k 5
    Either, or both, kafka (k) and datanodes (n) can be changed. The value specifies the new total number of nodes. Shrinking is not supported - this must be done very carefully to avoid data loss.
  - create cluster without user input:
    pnda-cli.py create -s mykeyname -e squirrel-land -f standard -n 5 -o 1 -k 2 -z 3"""
    parser = argparse.ArgumentParser(formatter_class=RawTextHelpFormatter, description='PNDA CLI', epilog=epilog)
    banner()

    parser.add_argument('command', help='Mode of operation', choices=['create', 'expand', 'destroy'])
    parser.add_argument('-e', '--pnda-cluster', type=name_string, help='Namespaced environment for machines in this cluster')
    parser.add_argument('-n', '--datanodes', type=int, help='How many datanodes for the hadoop cluster')
    parser.add_argument('-o', '--opentsdb-nodes', type=int, help='How many Open TSDB nodes for the hadoop cluster')
    parser.add_argument('-k', '--kafka-nodes', type=int, help='How many kafka nodes for the databus cluster')
    parser.add_argument('-z', '--zk-nodes', type=int, help='How many zookeeper nodes for the databus cluster')
    parser.add_argument('-f', '--flavour', help='PNDA flavour: "standard"', choices=['standard'])
    parser.add_argument('-s', '--keyname', help='Keypair name')
    parser.add_argument('-x', '--no-config-check', action='store_true', help='Skip config verifiction checks')

    args = parser.parse_args()
    return args

def main():
    args = get_args()
    print 'Saving debug log to %s' % LOG_FILE_NAME
    pnda_cluster = args.pnda_cluster
    datanodes = args.datanodes
    tsdbnodes = args.opentsdb_nodes
    kafkanodes = args.kafka_nodes
    zknodes = args.zk_nodes
    flavour = args.flavour
    keyname = args.keyname
    no_config_check = args.no_config_check
    os.chdir('../')
    if not os.path.isfile('git.pem'):
        with open('git.pem', 'w') as git_key_file:
            git_key_file.write('If authenticated acess to the platform-salt git repository is required then' +
                               ' replace this file with a key that grants access to the git server.\n')

    if args.command == 'destroy':
        if pnda_cluster is not None:
            destroy(pnda_cluster)
            sys.exit(0)
        else:
            print 'destroy command must specify pnda_cluster, e.g.\npnda-cli.py destroy -e squirrel-land'
            sys.exit(1)

    while pnda_cluster is None:
        pnda_cluster = raw_input("Enter a name for the pnda cluster (e.g. squirrel-land): ")
        if not re.match(NAME_REGEX, pnda_cluster):
            print "pnda cluster name may contain only  a-z 0-9 and '-'"
            pnda_cluster = None

    while flavour is None:
        flavour = raw_input("Enter a flavour (standard): ")
        if not re.match("^(standard)$", flavour):
            print "Not a valid flavour"
            flavour = None

    while keyname is None:
        keyname = raw_input("Enter a keypair name to use for ssh access to instances: ")

    global VALIDATION_RULES
    validation_file = file('cloud-formation/%s/validation.json' % flavour)
    VALIDATION_RULES = json.load(validation_file)
    validation_file.close()

    if args.command == 'expand':
        if pnda_cluster is not None:
            node_counts = get_current_node_counts(pnda_cluster)

            if datanodes is None:
                datanodes = node_counts['cdh-dn']
            if kafkanodes is None:
                kafkanodes = node_counts['kafka']

            if not validate_size("datanodes", datanodes):
                print "Consider choice of datanodes again, limits are: %s" % get_validation("datanodes")
                sys.exit(1)
            if not validate_size("kafka-nodes", kafkanodes):
                print "Consider choice of kafkanodes again, limits are: %s" % get_validation("kafka-nodes")
                sys.exit(1)

            if datanodes < node_counts['cdh-dn']:
                print "You cannot shrink the cluster using this CLI, existing number of datanodes is: %s" % node_counts['cdh-dn']
                sys.exit(1)
            elif datanodes > node_counts['cdh-dn']:
                print "Increasing the number of datanodes from %s to %s" % (node_counts['cdh-dn'], datanodes)
            if kafkanodes < node_counts['kafka']:
                print "You cannot shrink the cluster using this CLI, existing number of kafkanodes is: %s" % node_counts['kafka']
                sys.exit(1)
            elif  kafkanodes > node_counts['kafka']:
                print "Increasing the number of kafkanodes from %s to %s" % (node_counts['kafka'], kafkanodes)

            template_data = generate_template_file('cloud-formation/%s/cf-tmpl.json' % flavour,
                                                   datanodes, node_counts['opentsdb'], kafkanodes, node_counts['zk'])
            expand(template_data, pnda_cluster, flavour, node_counts['cdh-dn'], node_counts['kafka'], keyname)
            sys.exit(0)
        else:
            print 'expand command must specify pnda_cluster, e.g.\npnda-cli.py expand -e squirrel-land -f standard -s keyname -n 5'
            sys.exit(1)

    while datanodes is None:
        datanodes = raw_input("Enter how many Hadoop data nodes (%s): " % get_validation("datanodes"))
        try:
            datanodes = int(datanodes)
        except:
            print "Not a number"
            datanodes = None

        if not validate_size("datanodes", datanodes):
            print "Consider choice again, limits are: %s" % get_validation("datanodes")
            datanodes = None

    while tsdbnodes is None:
        tsdbnodes = raw_input("Enter how many Open TSDB nodes (%s): " % get_validation("opentsdb-nodes"))
        try:
            tsdbnodes = int(tsdbnodes)
        except:
            print "Not a number"
            tsdbnodes = None

        if not validate_size("opentsdb-nodes", tsdbnodes):
            print "Consider choice again, limits are: %s" % get_validation("opentsdb-nodes")
            tsdbnodes = None

    while kafkanodes is None:
        kafkanodes = raw_input("Enter how many Kafka nodes (%s): " % get_validation("kafka-nodes"))
        try:
            kafkanodes = int(kafkanodes)
        except:
            print "Not a number"
            kafkanodes = None

        if not validate_size("kafka-nodes", kafkanodes):
            print "Consider choice again, limits are: %s" % get_validation("kafka-nodes")
            kafkanodes = None

    while zknodes is None:
        zknodes = raw_input("Enter how many Zookeeper nodes (%s): " % get_validation("zk-nodes"))
        try:
            zknodes = int(zknodes)
        except:
            print "Not a number"
            zknodes = None

        if not validate_size("zk-nodes", zknodes):
            print "Consider choice again, limits are: %s" % get_validation("zk-nodes")
            zknodes = None

    node_limit("datanodes", datanodes)
    node_limit("opentsdb-nodes", tsdbnodes)
    node_limit("kafka-nodes", kafkanodes)
    node_limit("zk-nodes", zknodes)

    template_data = generate_template_file('cloud-formation/%s/cf-tmpl.json' % flavour, datanodes, tsdbnodes, kafkanodes, zknodes)
    console_dns = create(template_data, pnda_cluster, flavour, keyname, no_config_check)
    CONSOLE.info('Use the PNDA console to get started: http://%s', console_dns)
    CONSOLE.info(' Access hints:')
    CONSOLE.info('  - Set up a socks proxy with: ./socks_proxy')
    CONSOLE.info('  - ssh to a node with: ssh -F ssh_config <private_ip>')

if __name__ == "__main__":
    try:
        main()
    except Exception as exception:
        CONSOLE.error(exception)
        CONSOLE.error(traceback.format_exc())
        raise
