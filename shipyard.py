#!/usr/bin/env python3

# stdlib imports
import argparse
import base64
import copy
import datetime
import json
import hashlib
import os
import pathlib
import time
from typing import List
# non-stdlib imports
import azure.batch.batch_auth as batchauth
import azure.batch.batch_service_client as batch
import azure.batch.models as batchmodels
import azure.common
import azure.storage.blob as azureblob
import azure.storage.queue as azurequeue
import azure.storage.table as azuretable

# global defines
_STORAGEACCOUNT = os.getenv('STORAGEACCOUNT')
_STORAGEACCOUNTKEY = os.getenv('STORAGEACCOUNTKEY')
_BATCHACCOUNTKEY = os.getenv('BATCHACCOUNTKEY')
_STORAGE_CONTAINERS = {
    'blob_resourcefiles': None,
    'blob_torrents': None,
    'table_dht': None,
    'table_registry': None,
    'table_torrentinfo': None,
    'table_services': None,
    'table_globalresources': None,
    'table_perf': None,
    'queue_globalresources': None,
}
_NODEPREP_FILE = ('nodeprep.sh', 'scripts/nodeprep.sh')
_JOBPREP_FILE = ('jpdockerblock.sh', 'scripts/jpdockerblock.sh')
_CASCADE_FILE = ('cascade.py', 'cascade.py')
_SETUP_PR_FILE = ('setup_private_registry.py', 'setup_private_registry.py')
_PERF_FILE = ('perf.py', 'perf.py')
_REGISTRY_FILE = None
_GENERIC_DOCKER_TASK_PREFIX = 'dockertask-'
_TEMP_DIR = pathlib.Path('/tmp')


def _populate_global_settings(config: dict):
    """Populate global settings from config
    :param dict config: configuration dict
    """
    global _STORAGEACCOUNT, _STORAGEACCOUNTKEY, _BATCHACCOUNTKEY, \
        _REGISTRY_FILE
    _STORAGEACCOUNT = config['credentials']['storage_account']
    _STORAGEACCOUNTKEY = config['credentials']['storage_account_key']
    _BATCHACCOUNTKEY = config['credentials']['batch_account_key']
    try:
        sep = config['storage_entity_prefix']
    except KeyError:
        sep = None
    if sep is None:
        sep = ''
    _STORAGE_CONTAINERS['blob_resourcefiles'] = sep + 'resourcefiles'
    _STORAGE_CONTAINERS['blob_torrents'] = '-'.join(
        (sep + 'torrents',
         config['credentials']['batch_account'].lower(),
         config['pool_specification']['id'].lower()))
    _STORAGE_CONTAINERS['table_dht'] = sep + 'dht'
    _STORAGE_CONTAINERS['table_registry'] = sep + 'registry'
    _STORAGE_CONTAINERS['table_torrentinfo'] = sep + 'torrentinfo'
    _STORAGE_CONTAINERS['table_services'] = sep + 'services'
    _STORAGE_CONTAINERS['table_globalresources'] = sep + 'globalresources'
    _STORAGE_CONTAINERS['table_perf'] = sep + 'perf'
    _STORAGE_CONTAINERS['queue_globalresources'] = '-'.join(
        (sep + 'globalresources',
         config['credentials']['batch_account'].lower(),
         config['pool_specification']['id'].lower()))
    try:
        if config['docker_registry']['private']['enabled']:
            rf = config['docker_registry']['private'][
                'docker_save_registry_file']
            _REGISTRY_FILE = (
                pathlib.Path(rf).name,
                rf,
                config['docker_registry']['private'][
                    'docker_save_registry_image_id']
            )
        else:
            _REGISTRY_FILE = (None, None, None)
    except Exception:
        _REGISTRY_FILE = (None, None, None)


def _wrap_commands_in_shell(commands: List[str], wait: bool=True) -> str:
    """Wrap commands in a shell
    :param list commands: list of commands to wrap
    :param bool wait: add wait for background processes
    :rtype: str
    :return: wrapped commands
    """
    return '/bin/bash -c \'set -e; set -o pipefail; {}{}\''.format(
        ';'.join(commands), '; wait' if wait else '')


def _create_credentials(config: dict) -> tuple:
    """Create authenticated clients
    :param dict config: configuration dict
    :rtype: tuple
    :return: (batch client, blob client, queue client, table client)
    """
    credentials = batchauth.SharedKeyCredentials(
        config['credentials']['batch_account'],
        _BATCHACCOUNTKEY)
    batch_client = batch.BatchServiceClient(
        credentials,
        base_url=config['credentials']['batch_account_service_url'])
    blob_client = azureblob.BlockBlobService(
        account_name=_STORAGEACCOUNT,
        account_key=_STORAGEACCOUNTKEY,
        endpoint_suffix=config['credentials']['storage_endpoint'])
    queue_client = azurequeue.QueueService(
        account_name=_STORAGEACCOUNT,
        account_key=_STORAGEACCOUNTKEY,
        endpoint_suffix=config['credentials']['storage_endpoint'])
    table_client = azuretable.TableService(
        account_name=_STORAGEACCOUNT,
        account_key=_STORAGEACCOUNTKEY,
        endpoint_suffix=config['credentials']['storage_endpoint'])
    return batch_client, blob_client, queue_client, table_client


def compute_md5_for_file_b64(
        file: pathlib.Path, blocksize: int=65536) -> str:
    """Compute MD5 hash for file as base64
    :param pathlib.Path file: file to compute md5 for
    :param int blocksize: block size in bytes
    :rtype: str
    :return: md5 for file base64 encoded
    """
    hasher = hashlib.md5()
    with file.open('rb') as filedesc:
        while True:
            buf = filedesc.read(blocksize)
            if not buf:
                break
            hasher.update(buf)
        return str(base64.b64encode(hasher.digest()), 'ascii')


def upload_resource_files(
        blob_client: azure.storage.blob.BlockBlobService, config: dict,
        files: List[tuple]) -> dict:
    """Upload resource files to blob storage
    :param azure.storage.blob.BlockBlobService blob_client: blob client
    :param dict config: configuration dict
    :rtype: dict
    :return: sas url dict
    """
    sas_urls = {}
    for file in files:
        # skip if no file is specified
        if file[0] is None:
            continue
        upload = True
        if file[0] == _REGISTRY_FILE[0]:
            fp = pathlib.Path(file[1])
            if not fp.exists():
                print('skipping optional docker registry image: {}'.format(
                    _REGISTRY_FILE[0]))
                continue
            else:
                # check if blob exists
                try:
                    prop = blob_client.get_blob_properties(
                        _STORAGE_CONTAINERS['blob_resourcefiles'], file[0])
                    if (prop.name == _REGISTRY_FILE[0] and
                            prop.properties.content_settings.content_md5 ==
                            compute_md5_for_file_b64(fp)):
                        print(('remote file is the same '
                               'for {}, skipping').format(_REGISTRY_FILE[0]))
                        upload = False
                except azure.common.AzureMissingResourceHttpError:
                    pass
        if upload:
            print('uploading file: {}'.format(file[1]))
            blob_client.create_blob_from_path(
                _STORAGE_CONTAINERS['blob_resourcefiles'], file[0], file[1])
        sas_urls[file[0]] = 'https://{}.blob.{}/{}/{}?{}'.format(
            _STORAGEACCOUNT,
            config['credentials']['storage_endpoint'],
            _STORAGE_CONTAINERS['blob_resourcefiles'], file[0],
            blob_client.generate_blob_shared_access_signature(
                _STORAGE_CONTAINERS['blob_resourcefiles'], file[0],
                permission=azureblob.BlobPermissions.READ,
                expiry=datetime.datetime.utcnow() +
                datetime.timedelta(hours=2)
            )
        )
    return sas_urls


def add_pool(
        batch_client: azure.batch.batch_service_client.BatchServiceClient,
        blob_client: azure.storage.blob.BlockBlobService, config: dict):
    """Add a Batch pool to account
    :param azure.batch.batch_service_client.BatchServiceClient: batch client
    :param azure.storage.blob.BlockBlobService blob_client: blob client
    :param dict config: configuration dict
    """
    publisher = config['pool_specification']['publisher']
    offer = config['pool_specification']['offer']
    sku = config['pool_specification']['sku']
    # peer-to-peer settings
    try:
        p2p = config['data_replication']['peer_to_peer']['enabled']
    except KeyError:
        p2p = True
    if p2p:
        nonp2pcd = False
        try:
            p2psbias = config['data_replication'][
                'peer_to_peer']['direct_download_seed_bias']
        except KeyError:
            p2psbias = 3
        try:
            p2pcomp = config[
                'data_replication']['peer_to_peer']['compression']
        except KeyError:
            p2pcomp = True
    else:
        try:
            nonp2pcd = config[
                'data_replication']['non_peer_to_peer_concurrent_downloading']
        except KeyError:
            nonp2pcd = True
    # private registry settings
    try:
        preg = config['docker_registry']['private']['enabled']
        pcont = config['docker_registry']['private']['container']
    except KeyError:
        preg = False
    if preg:
        preg = ' -r {}:{}:{}'.format(
            pcont, _REGISTRY_FILE[0], _REGISTRY_FILE[2])
    else:
        preg = ''
    # docker settings
    try:
        dockeruser = config['docker_registry']['login']['username']
        dockerpw = config['docker_registry']['login']['password']
    except KeyError:
        dockeruser = None
        dockerpw = None
    # prefix settings
    try:
        prefix = config['storage_entity_prefix']
        if len(prefix) == 0:
            prefix = None
    except KeyError:
        prefix = None
    # TODO for now, only support Ubuntu 16.04
    if (publisher != 'Canonical' or offer != 'UbuntuServer' or
            sku < '16.04.0-LTS'):
        raise ValueError('Unsupported Docker Host VM Config')
    # pick latest sku
    node_agent_skus = batch_client.account.list_node_agent_skus()
    skus_to_use = [
        (nas, image_ref) for nas in node_agent_skus for image_ref in sorted(
            nas.verified_image_references, key=lambda item: item.sku)
        if image_ref.publisher.lower() == publisher.lower() and
        image_ref.offer.lower() == offer.lower() and
        image_ref.sku.lower() == sku.lower()
    ]
    sku_to_use, image_ref_to_use = skus_to_use[-1]
    # upload resource files
    sas_urls = upload_resource_files(
        blob_client, config, [
            _NODEPREP_FILE, _JOBPREP_FILE, _CASCADE_FILE, _SETUP_PR_FILE,
            _PERF_FILE, _REGISTRY_FILE
        ]
    )
    # create pool param
    pool = batchmodels.PoolAddParameter(
        id=config['pool_specification']['id'],
        virtual_machine_configuration=batchmodels.VirtualMachineConfiguration(
            image_reference=image_ref_to_use,
            node_agent_sku_id=sku_to_use.id),
        vm_size=config['pool_specification']['vm_size'],
        target_dedicated=config['pool_specification']['vm_count'],
        enable_inter_node_communication=True,
        start_task=batchmodels.StartTask(
            command_line='{} -o {} -s {}{}{}{}{}'.format(
                _NODEPREP_FILE[0],
                offer,
                sku,
                preg,
                ' -p {}'.format(prefix) if prefix else '',
                ' -t {}:{}'.format(p2pcomp, p2psbias) if p2p else '',
                ' -c' if nonp2pcd else '',
            ),
            run_elevated=True,
            wait_for_success=True,
            environment_settings=[
                batchmodels.EnvironmentSetting('LC_ALL', 'en_US.UTF-8'),
                batchmodels.EnvironmentSetting('CASCADE_SA', _STORAGEACCOUNT),
                batchmodels.EnvironmentSetting(
                    'CASCADE_SAKEY', _STORAGEACCOUNTKEY),
            ],
            resource_files=[],
        ),
    )
    for rf in sas_urls:
        pool.start_task.resource_files.append(
            batchmodels.ResourceFile(
                file_path=rf,
                blob_source=sas_urls[rf])
        )
    if preg:
        pool.start_task.environment_settings.append(
            batchmodels.EnvironmentSetting(
                'PRIVATE_REGISTRY_SA',
                config['docker_registry']['private']['storage_account'])
        )
        pool.start_task.environment_settings.append(
            batchmodels.EnvironmentSetting(
                'PRIVATE_REGISTRY_SAKEY',
                config['docker_registry']['private']['storage_account_key'])
        )
    if (dockeruser is not None and len(dockeruser) > 0 and
            dockerpw is not None and len(dockerpw) > 0):
        pool.start_task.environment_settings.append(
            batchmodels.EnvironmentSetting('DOCKER_LOGIN_USERNAME', dockeruser)
        )
        pool.start_task.environment_settings.append(
            batchmodels.EnvironmentSetting('DOCKER_LOGIN_PASSWORD', dockerpw)
        )
    # create pool if not exists
    try:
        print('Attempting to create pool:', pool.id)
        batch_client.pool.add(pool)
        print('Created pool:', pool.id)
    except batchmodels.BatchErrorException as e:
        if e.error.code != 'PoolExists':
            raise
        else:
            print('Pool {!r} already exists'.format(pool.id))
    # wait for pool idle
    node_state = frozenset(
        (batchmodels.ComputeNodeState.starttaskfailed,
         batchmodels.ComputeNodeState.unusable,
         batchmodels.ComputeNodeState.idle)
    )
    print('waiting for all nodes in pool {} to reach one of: {!r}'.format(
        pool.id, node_state))
    i = 0
    while True:
        # refresh pool to ensure that there is no resize error
        pool = batch_client.pool.get(pool.id)
        if pool.resize_error is not None:
            raise RuntimeError(
                'resize error encountered for pool {}: code={} msg={}'.format(
                    pool.id, pool.resize_error.code,
                    pool.resize_error.message))
        nodes = list(batch_client.compute_node.list(pool.id))
        if (len(nodes) >= pool.target_dedicated and
                all(node.state in node_state for node in nodes)):
            break
        i += 1
        if i % 3 == 0:
            print('waiting for {} nodes to reach desired state:'.format(
                pool.target_dedicated))
            for node in nodes:
                print(' > {}: {}'.format(node.id, node.state))
        time.sleep(10)
    get_remote_login_settings(batch_client, pool.id, nodes)
    if any(node.state != batchmodels.ComputeNodeState.idle for node in nodes):
        raise RuntimeError('node(s) of pool {} not in idle state'.format(
            pool.id))


def resize_pool(batch_client, pool_id, vm_count):
    print('Resizing pool {} to {}'.format(pool_id, vm_count))
    batch_client.pool.resize(
        pool_id=pool_id,
        pool_resize_parameter=batchmodels.PoolResizeParameter(
            target_dedicated=vm_count,
            resize_timeout=datetime.timedelta(minutes=20),
        )
    )


def del_pool(batch_client, pool_id):
    print('Deleting pool: {}'.format(pool_id))
    batch_client.pool.delete(pool_id)


def del_node(batch_client, pool_id, node_id):
    print('Deleting node {} from pool {}'.format(node_id, pool_id))
    batch_client.pool.remove_nodes(
        pool_id=pool_id,
        node_remove_parameter=batchmodels.NodeRemoveParameter(
            node_list=[node_id],
        )
    )


def add_job(batch_client, blob_client, config):
    pool_id = config['pool_specification']['id']
    global_resources = []
    for gr in config['global_resources']['docker_images']:
        global_resources.append(gr)
    # TODO add global resources for non-docker resources
    jpcmdline = _wrap_commands_in_shell([
        '$AZ_BATCH_NODE_SHARED_DIR/{} {}'.format(
            _JOBPREP_FILE[0], ' '.join(global_resources))])
    for jobspec in config['job_specifications']:
        job = batchmodels.JobAddParameter(
            id=jobspec['id'],
            pool_info=batchmodels.PoolInformation(pool_id=pool_id),
            job_preparation_task=batchmodels.JobPreparationTask(
                command_line=jpcmdline,
                wait_for_success=True,
                run_elevated=True,
                rerun_on_node_reboot_after_success=False,
            )
        )
        print('adding job: {}'.format(job.id))
        try:
            batch_client.job.add(job)
        except batchmodels.batch_error.BatchErrorException as ex:
            if 'The specified job already exists' not in ex.message.value:
                raise
        # add all tasks under job
        for task in jobspec['tasks']:
            image = task['image']
            # get or generate task id
            try:
                task_id = task['id']
                if task_id is None:
                    raise KeyError()
            except KeyError:
                # get filtered, sorted list of generic docker task ids
                try:
                    tasklist = sorted(
                        filter(lambda x: x.id.startswith(
                            _GENERIC_DOCKER_TASK_PREFIX), list(
                                batch_client.task.list(job.id))),
                        key=lambda y: y.id)
                    tasknum = int(tasklist[-1].id.split('-')[-1]) + 1
                except (batchmodels.batch_error.BatchErrorException,
                        IndexError):
                    tasknum = 0
                task_id = '{0}{1:03d}'.format(
                    _GENERIC_DOCKER_TASK_PREFIX, tasknum)
            # get generic run opts
            try:
                run_opts = task['additional_docker_run_options']
            except KeyError:
                run_opts = []
            # parse remove container option
            try:
                rm_container = task['remove_container_after_exit']
            except KeyError:
                pass
            else:
                if rm_container and '--rm' not in run_opts:
                    run_opts.append('--rm')
                del rm_container
            # parse name option
            try:
                name = task['name']
                if name is not None:
                    run_opts.append('-n {}'.format(name))
                del name
            except KeyError:
                pass
            # parse labels option
            try:
                labels = task['labels']
                if labels is not None and len(labels) > 0:
                    for label in labels:
                        run_opts.append('-l {}'.format(label))
                del labels
            except KeyError:
                pass
            # parse ports option
            try:
                ports = task['ports']
                if ports is not None and len(ports) > 0:
                    for port in ports:
                        run_opts.append('-p {}'.format(port))
                del ports
            except KeyError:
                pass
            # parse entrypoint
            try:
                entrypoint = task['entrypoint']
                if entrypoint is not None:
                    run_opts.append('--entrypoint {}'.format(entrypoint))
                del entrypoint
            except KeyError:
                pass
            # parse data volumes
            try:
                data_volumes = task['data_volumes']
            except KeyError:
                pass
            else:
                if data_volumes is not None and len(data_volumes) > 0:
                    for key in data_volumes:
                        dvspec = config[
                            'global_resources']['docker_volumes'][
                                'data_volumes'][key]
                        try:
                            hostpath = dvspec['host_path']
                        except KeyError:
                            hostpath = None
                        if hostpath is not None:
                            run_opts.append('-v {}:{}'.format(
                                hostpath, dvspec['container_path']))
                        else:
                            run_opts.append('-v {}'.format(
                                dvspec['container_path']))
            # parse shared data volumes
            try:
                shared_data_volumes = task['shared_data_volumes']
            except KeyError:
                pass
            else:
                if (shared_data_volumes is not None and
                        len(shared_data_volumes) > 0):
                    for key in shared_data_volumes:
                        dvspec = config[
                            'global_resources']['docker_volumes'][
                                'shared_data_volumes'][key]
                        run_opts.append('-v {}:{}'.format(
                            key, dvspec['container_path']))
            # get command
            try:
                command = task['command']
            except KeyError:
                command = None
            # get and create env var file
            envfile = '{}.env.list'.format(task_id)
            sas_urls = None
            try:
                env_vars = task['environment_variables']
            except KeyError:
                pass
            else:
                if env_vars is not None and len(env_vars) > 0:
                    envfiletmp = _TEMP_DIR / envfile
                    envfileloc = '{}taskrf-{}/{}'.format(
                        config['storage_entity_prefix'], job.id, envfile)
                    with envfiletmp.open('w') as f:
                        for key in env_vars:
                            f.write('{}={}{}'.format(
                                key, env_vars[key], os.linesep))
                    # upload env var file if exists
                    sas_urls = upload_resource_files(
                        blob_client, config, [(envfileloc, str(envfiletmp))])
                    envfiletmp.unlink()
            # always add option for envfile
            run_opts.append('--env-file {}'.format(envfile))
            # mount batch root dir
            run_opts.append(
                '-v $AZ_BATCH_NODE_ROOT_DIR:$AZ_BATCH_NODE_ROOT_DIR')
            # set working directory
            run_opts.append('-w $AZ_BATCH_TASK_WORKING_DIR')
            # add task
            task_commands = [
                'env | grep AZ_BATCH_ >> {}'.format(envfile),
                'docker run {} {}{}'.format(
                    ' '.join(run_opts),
                    image,
                    ' {}'.format(command) if command else '')
            ]
            task = batchmodels.TaskAddParameter(
                id=task_id,
                command_line=_wrap_commands_in_shell(task_commands),
                run_elevated=True,
                resource_files=[],
            )
            for rf in sas_urls:
                task.resource_files.append(
                    batchmodels.ResourceFile(
                        file_path=str(pathlib.Path(rf).name),
                        blob_source=sas_urls[rf])
                )
            print('adding task {}: {}'.format(
                task_id, task.command_line))
            batch_client.task.add(job_id=job.id, task=task)


def del_job(batch_client, job_id):
    print('Deleting job: {}'.format(job_id))
    batch_client.job.delete(job_id)


def terminate_job(batch_client, job_id):
    print('Terminating job: {}'.format(job_id))
    batch_client.job.terminate(job_id)


def del_all_jobs(batch_client):
    print('Listing jobs...')
    jobs = batch_client.job.list()
    for job in jobs:
        del_job(batch_client, job.id)


def get_remote_login_settings(batch_client, pool_id, nodes=None):
    if nodes is None:
        nodes = batch_client.compute_node.list(pool_id)
    for node in nodes:
        rls = batch_client.compute_node.get_remote_login_settings(
            pool_id, node.id)
        print('node {}: {}'.format(node.id, rls))


def delete_storage_containers(blob_client, queue_client, table_client, config):
    for key in _STORAGE_CONTAINERS:
        if key.startswith('blob_'):
            blob_client.delete_container(_STORAGE_CONTAINERS[key])
        elif key.startswith('table_'):
            table_client.delete_table(_STORAGE_CONTAINERS[key])
        elif key.startswith('queue_'):
            queue_client.delete_queue(_STORAGE_CONTAINERS[key])


def _clear_blobs(blob_client, container):
    print('deleting blobs: {}'.format(container))
    blobs = blob_client.list_blobs(container)
    for blob in blobs:
        blob_client.delete_blob(container, blob.name)


def _clear_table(table_client, table_name, config):
    print('clearing table: {}'.format(table_name))
    ents = table_client.query_entities(
        table_name, filter='PartitionKey eq \'{}${}\''.format(
            config['credentials']['batch_account'],
            config['pool_specification']['id'])
    )
    # batch delete entities
    i = 0
    bet = azuretable.TableBatch()
    for ent in ents:
        bet.delete_entity(ent['PartitionKey'], ent['RowKey'])
        i += 1
        if i == 100:
            table_client.commit_batch(table_name, bet)
            bet = azuretable.TableBatch()
            i = 0
    if i > 0:
        table_client.commit_batch(table_name, bet)


def clear_storage_containers(blob_client, queue_client, table_client, config):
    for key in _STORAGE_CONTAINERS:
        if key.startswith('blob_'):
            # TODO this is temp to preserve registry upload
            if key != 'blob_resourcefiles':
                _clear_blobs(blob_client, _STORAGE_CONTAINERS[key])
        elif key.startswith('table_'):
            _clear_table(table_client, _STORAGE_CONTAINERS[key], config)
        elif key.startswith('queue_'):
            print('clearing queue: {}'.format(_STORAGE_CONTAINERS[key]))
            queue_client.clear_messages(_STORAGE_CONTAINERS[key])


def create_storage_containers(blob_client, queue_client, table_client, config):
    for key in _STORAGE_CONTAINERS:
        if key.startswith('blob_'):
            print('creating container: {}'.format(_STORAGE_CONTAINERS[key]))
            blob_client.create_container(_STORAGE_CONTAINERS[key])
        elif key.startswith('table_'):
            print('creating table: {}'.format(_STORAGE_CONTAINERS[key]))
            table_client.create_table(_STORAGE_CONTAINERS[key])
        elif key.startswith('queue_'):
            print('creating queue: {}'.format(_STORAGE_CONTAINERS[key]))
            queue_client.create_queue(_STORAGE_CONTAINERS[key])


def _add_global_resource(
        queue_client, table_client, config, pk, p2pcsd, grtype):
    try:
        for gr in config['global_resources'][grtype]:
            if grtype == 'docker_images':
                prefix = 'docker'
            else:
                raise NotImplementedError()
            resource = '{}:{}'.format(prefix, gr)
            table_client.insert_or_replace_entity(
                _STORAGE_CONTAINERS['table_globalresources'],
                {
                    'PartitionKey': pk,
                    'RowKey': hashlib.sha1(
                        resource.encode('utf8')).hexdigest(),
                    'Resource': resource,
                }
            )
            for _ in range(0, p2pcsd):
                queue_client.put_message(
                    _STORAGE_CONTAINERS['queue_globalresources'], resource)
    except KeyError:
        pass


def populate_queues(queue_client, table_client, config):
    try:
        preg = config['docker_registry']['private']['enabled']
    except KeyError:
        preg = False
    pk = '{}${}'.format(
        config['credentials']['batch_account'],
        config['pool_specification']['id'])
    # if using docker public hub, then populate registry table with hub
    if not preg:
        table_client.insert_or_replace_entity(
            _STORAGE_CONTAINERS['table_registry'],
            {
                'PartitionKey': pk,
                'RowKey': 'registry.hub.docker.com',
                'Port': 80,
            }
        )
    # get p2pcsd setting
    try:
        p2p = config['data_replication']['peer_to_peer']['enabled']
    except KeyError:
        p2p = True
    if p2p:
        try:
            p2pcsd = config['data_replication']['peer_to_peer'][
                'concurrent_source_downloads']
        except KeyError:
            p2pcsd = 1
    else:
        p2pcsd = 1
    # add global resources
    _add_global_resource(
        queue_client, table_client, config, pk, p2pcsd, 'docker_images')


def merge_dict(dict1, dict2):
    """Recursively merge dictionaries: dict2 on to dict1. This differs
    from dict.update() in that values that are dicts are recursively merged.
    Note that only dict value types are merged, not lists, etc.

    Code adapted from:
    https://www.xormedia.com/recursively-merge-dictionaries-in-python/

    :param dict dict1: dictionary to merge to
    :param dict dict2: dictionary to merge with
    :rtype: dict
    :return: merged dictionary
    """
    if not isinstance(dict1, dict) or not isinstance(dict2, dict):
        raise ValueError('dict1 or dict2 is not a dictionary')
    result = copy.deepcopy(dict1)
    for k, v in dict2.items():
        if k in result and isinstance(result[k], dict):
            result[k] = merge_dict(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def main():
    """Main function"""
    # get command-line args
    args = parseargs()
    args.action = args.action.lower()

    if args.settings is None:
        raise ValueError('global settings not specified')
    if args.config is None:
        raise ValueError('config settings for action not specified')

    with open(args.settings, 'r') as f:
        config = json.load(f)
    with open(args.config, 'r') as f:
        config = merge_dict(config, json.load(f))
    print('config:')
    print(json.dumps(config, indent=4))
    _populate_global_settings(config)

    batch_client, blob_client, queue_client, table_client = \
        _create_credentials(config)

    if args.action == 'addpool':
        create_storage_containers(
            blob_client, queue_client, table_client, config)
        clear_storage_containers(
            blob_client, queue_client, table_client, config)
        populate_queues(queue_client, table_client, config)
        add_pool(batch_client, blob_client, config)
    elif args.action == 'resizepool':
        resize_pool(batch_client, args.poolid, args.numvms)
    elif args.action == 'delpool':
        del_pool(batch_client, args.poolid)
    elif args.action == 'delnode':
        del_node(batch_client, args.poolid, args.nodeid)
    elif args.action == 'addjob':
        add_job(batch_client, blob_client, config)
    elif args.action == 'termjob':
        terminate_job(batch_client, blob_client, args.jobid)
    elif args.action == 'deljob':
        del_job(batch_client, args.jobid)
    elif args.action == 'delalljobs':
        del_all_jobs(batch_client)
    elif args.action == 'grl':
        get_remote_login_settings(batch_client, args.poolid)
    elif args.action == 'delstorage':
        delete_storage_containers(
            blob_client, queue_client, table_client, config)
    elif args.action == 'clearstorage':
        clear_storage_containers(
            blob_client, queue_client, table_client, config)
    else:
        raise ValueError('Unknown action: {}'.format(args.action))


def parseargs():
    """Parse program arguments
    :rtype: argparse.Namespace
    :return: parsed arguments
    """
    parser = argparse.ArgumentParser(
        description='Shipyard: Azure Batch to Docker Bridge')
    parser.add_argument(
        'action', help='action: addpool, addjob, termjob, delpool, '
        'delnode, deljob, delalljobs, grl, delstorage, clearstorage')
    parser.add_argument(
        '--settings',
        help='global settings json file config. required for all actions')
    parser.add_argument(
        '--config',
        help='json file config for option. required for all actions')
    parser.add_argument('--poolid', help='pool id')
    parser.add_argument('--jobid', help='job id')
    parser.add_argument('--nodeid', help='node id')
    return parser.parse_args()

if __name__ == '__main__':
    main()
