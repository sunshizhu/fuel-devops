#    Copyright 2013 - 2016 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import time
from warnings import warn

from django.conf import settings
from django.db import IntegrityError
from django.db import models
from netaddr import IPNetwork
from paramiko import Agent
from paramiko import RSAKey

from devops.error import DevopsEnvironmentError
from devops.error import DevopsError
from devops.error import DevopsObjNotFound
from devops.helpers.network import IpNetworksPool
from devops.helpers.ssh_client import SSHAuth
from devops.helpers.ssh_client import SSHClient
from devops.helpers.templates import create_devops_config
from devops.helpers.templates import get_devops_config
from devops import logger
from devops.models.base import BaseModel
from devops.models.driver import Driver
from devops.models.group import Group
from devops.models.network import AddressPool
from devops.models.network import L2NetworkDevice
from devops.models.node import Node


def _numhosts(self):
    msg = (
        'numhosts property is temporary compatibility spike '
        'and will be dropped soon! '
        'Replace by len(IPNetwork()) if required.'
    )
    logger.warning(msg)
    warn(msg, DeprecationWarning)
    return len(self)

IPNetwork.numhosts = property(
    fget=_numhosts,
    doc="""Temporary compatibility layer for numhosts property support.""")


class Environment(BaseModel):
    class Meta(object):
        db_table = 'devops_environment'
        app_label = 'devops'

    name = models.CharField(max_length=255, unique=True, null=False)

    hostname = 'nailgun'
    domain = 'test.domain.local'
    nat_interface = ''  # INTERFACES.get('admin')
    # TODO(akostrikov) As we providing admin net names in fuel-qa/settings,
    # we should create constant and use it in fuel-qa or
    # pass admin net names to Environment from fuel-qa.
    admin_net = 'admin'
    admin_net2 = 'admin2'

    def __repr__(self):
        return 'Environment(name={name!r})'.format(name=self.name)

    def get_allocated_networks(self):
        allocated_networks = []
        for group in self.get_groups():
            allocated_networks += group.get_allocated_networks()
        return allocated_networks

    def get_address_pool(self, **kwargs):
        try:
            return self.addresspool_set.get(**kwargs)
        except AddressPool.DoesNotExist:
            raise DevopsObjNotFound(AddressPool, **kwargs)

    def get_address_pools(self, **kwargs):
        return self.addresspool_set.filter(**kwargs).order_by('id')

    def get_group(self, **kwargs):
        try:
            return self.group_set.get(**kwargs)
        except Group.DoesNotExist:
            raise DevopsObjNotFound(Group, **kwargs)

    def get_groups(self, **kwargs):
        return self.group_set.filter(**kwargs).order_by('id')

    def add_groups(self, groups):
        for group_data in groups:
            driver_data = group_data['driver']
            if driver_data['name'] == 'devops.driver.libvirt.libvirt_driver':
                warn(
                    "Driver 'devops.driver.libvirt.libvirt_driver' "
                    "has been renamed to 'devops.driver.libvirt', "
                    "please update the tests!",
                    DeprecationWarning)
                logger.warning(
                    "Driver 'devops.driver.libvirt.libvirt_driver' "
                    "has been renamed to 'devops.driver.libvirt', "
                    "please update the tests!")
                driver_data['name'] = 'devops.driver.libvirt'
            self.add_group(
                group_name=group_data['name'],
                driver_name=driver_data['name'],
                **driver_data.get('params', {})
            )

    def add_group(self, group_name, driver_name, **driver_params):
        driver = Driver.driver_create(
            name=driver_name,
            **driver_params
        )
        return Group.group_create(
            name=group_name,
            environment=self,
            driver=driver,
        )

    def add_address_pools(self, address_pools):
        for name, data in address_pools.items():
            self.add_address_pool(
                name=name,
                net=data['net'],
                **data.get('params', {})
            )

    def add_address_pool(self, name, net, **params):

        networks, prefix = net.split(':')
        ip_networks = [IPNetwork(x) for x in networks.split(',')]

        pool = IpNetworksPool(
            networks=ip_networks,
            prefix=int(prefix),
            allocated_networks=self.get_allocated_networks())

        return AddressPool.address_pool_create(
            environment=self,
            name=name,
            pool=pool,
            **params
        )

    @classmethod
    def create(cls, name):
        """Create Environment instance with given name.

        :rtype: devops.models.Environment
        """
        try:
            return cls.objects.create(name=name)
        except IntegrityError:
            raise DevopsError('Environment with name {!r} already exists'
                              ''.format(name))

    @classmethod
    def get(cls, *args, **kwargs):
        try:
            return cls.objects.get(*args, **kwargs)
        except Environment.DoesNotExist:
            raise DevopsObjNotFound(Environment, *args, **kwargs)

    @classmethod
    def list_all(cls):
        return cls.objects.all()

    # LEGACY
    def has_snapshot(self, name):
        if self.get_nodes():
            return all(n.has_snapshot(name) for n in self.get_nodes())
        else:
            return False

    def define(self):
        for group in self.get_groups():
            group.define_networks()
        for group in self.get_groups():
            group.define_volumes()
        for group in self.get_groups():
            group.define_nodes()

    def start(self, nodes=None):
        for group in self.get_groups():
            group.start_networks()
        for group in self.get_groups():
            group.start_nodes(nodes)

    def destroy(self):
        for group in self.get_groups():
            group.destroy()

    def erase(self):
        for group in self.get_groups():
            group.erase()
        self.delete()

    def suspend(self, **kwargs):
        for node in self.get_nodes():
            node.suspend()

    def resume(self, **kwargs):
        for node in self.get_nodes():
            node.resume()

    def snapshot(self, name=None, description=None, force=False):
        if name is None:
            name = str(int(time.time()))
        for node in self.get_nodes():
            node.snapshot(name=name, description=description, force=force,
                          external=settings.SNAPSHOTS_EXTERNAL)

    def revert(self, name=None, flag=True):
        if flag and not self.has_snapshot(name):
            raise Exception("some nodes miss snapshot,"
                            " test should be interrupted")
        for node in self.get_nodes():
            node.revert(name)

        for group in self.get_groups():
            for l2netdev in group.get_l2_network_devices():
                l2netdev.unblock()

    # TO REWRITE FOR LIBVIRT DRIVER ONLY
    @classmethod
    def synchronize_all(cls):
        driver = cls.get_driver()
        nodes = {driver._get_name(e.name, n.name): n
                 for e in cls.list_all()
                 for n in e.get_nodes()}
        domains = set(driver.node_list())

        # FIXME (AWoodward) This willy nilly wacks domains when you run this
        #  on domains that are outside the scope of devops, if anything this
        #  should cause domains to be imported into db instead of undefined.
        #  It also leaves network and volumes around too
        #  Disabled until a safer implementation arrives

        # Undefine domains without devops nodes
        #
        # domains_to_undefine = domains - set(nodes.keys())
        # for d in domains_to_undefine:
        #    driver.node_undefine_by_name(d)

        # Remove devops nodes without domains
        nodes_to_remove = set(nodes.keys()) - domains
        for n in nodes_to_remove:
            nodes[n].delete()
        cls.erase_empty()

        logger.info('Undefined domains: {0}, removed nodes: {1}'.format(
            0, len(nodes_to_remove)
        ))

    # LEGACY
    @classmethod
    def describe_environment(cls, boot_from='cdrom'):
        """This method is DEPRECATED.

           Reserved for backward compatibility only.
           Please use self.create_environment() instead.
        """
        warn(
            'describe_environment is deprecated in favor of'
            ' create_environment', DeprecationWarning)
        if settings.DEVOPS_SETTINGS_TEMPLATE:
            config = get_devops_config(
                settings.DEVOPS_SETTINGS_TEMPLATE)
        else:
            config = create_devops_config(
                boot_from=boot_from,
                env_name=settings.ENV_NAME,
                admin_vcpu=settings.HARDWARE["admin_node_cpu"],
                admin_memory=settings.HARDWARE["admin_node_memory"],
                admin_sysvolume_capacity=settings.ADMIN_NODE_VOLUME_SIZE,
                admin_iso_path=settings.ISO_PATH,
                nodes_count=settings.NODES_COUNT,
                numa_nodes=settings.HARDWARE['numa_nodes'],
                slave_vcpu=settings.HARDWARE["slave_node_cpu"],
                slave_memory=settings.HARDWARE["slave_node_memory"],
                slave_volume_capacity=settings.NODE_VOLUME_SIZE,
                second_volume_capacity=settings.NODE_VOLUME_SIZE,
                third_volume_capacity=settings.NODE_VOLUME_SIZE,
                use_all_disks=settings.USE_ALL_DISKS,
                multipath_count=settings.SLAVE_MULTIPATH_DISKS_COUNT,
                ironic_nodes_count=settings.IRONIC_NODES_COUNT,
                networks_bonding=settings.BONDING,
                networks_bondinginterfaces=settings.BONDING_INTERFACES,
                networks_multiplenetworks=settings.MULTIPLE_NETWORKS,
                networks_nodegroups=settings.NODEGROUPS,
                networks_interfaceorder=settings.INTERFACE_ORDER,
                networks_pools=settings.POOLS,
                networks_forwarding=settings.FORWARDING,
                networks_dhcp=settings.DHCP,
                driver_enable_acpi=settings.DRIVER_PARAMETERS['enable_acpi'],
                driver_enable_nwfilers=settings.ENABLE_LIBVIRT_NWFILTERS,
            )

        environment = cls.create_environment(config)
        return environment

    @classmethod
    def create_environment(cls, full_config):
        """Create a new environment using full_config object

        :param full_config: object that describes all the parameters of
                            created environment

        :rtype: Environment
        """

        config = full_config['template']['devops_settings']
        environment = cls.create(config['env_name'])

        # create groups and drivers
        groups = config['groups']
        environment.add_groups(groups)

        # create address pools
        address_pools = config['address_pools']
        environment.add_address_pools(address_pools)

        # process group items
        for group_data in groups:
            group = environment.get_group(name=group_data['name'])

            # add l2_network_devices
            group.add_l2_network_devices(
                group_data.get('l2_network_devices', {}))

            # add network_pools
            group.add_network_pools(
                group_data.get('network_pools', {}))

        # Connect nodes to already created networks
        for group_data in groups:
            group = environment.get_group(name=group_data['name'])

            # add group volumes
            group.add_volumes(
                group_data.get('group_volumes', []))

            # add nodes
            group.add_nodes(
                group_data.get('nodes', []))

        return environment

    # LEGACY - TO MODIFY BY GROUPS
    @classmethod
    def erase_empty(cls):
        for env in cls.list_all():
            if env.get_nodes().count() == 0:
                env.erase()

    # TO L2_NETWORK_device, LEGACY
    # Rename it to default_gw and move to models.Network class
    def router(self, router_name=None):  # Alternative name: get_host_node_ip
        router_name = router_name or self.admin_net
        if router_name == self.admin_net2:
            return str(self.get_network(name=router_name).ip[2])
        return str(self.get_network(name=router_name).ip[1])

    # LEGACY, for fuel-qa compatibility
    # @logwrap
    def get_admin_remote(self,
                         login=settings.SSH_CREDENTIALS['login'],
                         password=settings.SSH_CREDENTIALS['password']):
        """SSH to admin node

        :rtype : SSHClient
        """
        admin = sorted(
            list(self.get_nodes(role__contains='master')),
            key=lambda node: node.name
        )[0]
        return admin.remote(
            self.admin_net, auth=SSHAuth(
                username=login,
                password=password))

    # LEGACY,  for fuel-qa compatibility
    # @logwrap
    def get_ssh_to_remote(self, ip,
                          login=settings.SSH_SLAVE_CREDENTIALS['login'],
                          password=settings.SSH_SLAVE_CREDENTIALS['password']):
        warn('LEGACY,  for fuel-qa compatibility', DeprecationWarning)
        keys = []
        remote = self.get_admin_remote()
        for key_string in ['/root/.ssh/id_rsa',
                           '/root/.ssh/bootstrap.rsa']:
            if remote.isfile(key_string):
                with remote.open(key_string) as f:
                    keys.append(RSAKey.from_private_key(f))

        return SSHClient(
            ip,
            auth=SSHAuth(
                username=login,
                password=password,
                keys=keys))

    # LEGACY,  for fuel-qa compatibility
    # @logwrap
    @staticmethod
    def get_ssh_to_remote_by_key(ip, keyfile):
        warn('LEGACY,  for fuel-qa compatibility', DeprecationWarning)
        try:
            with open(keyfile) as f:
                keys = [RSAKey.from_private_key(f)]
        except IOError:
            logger.warning('Loading of SSH key from file failed. Trying to use'
                           ' SSH agent ...')
            keys = Agent().get_keys()
        return SSHClient(ip, auth=SSHAuth(keys=keys))

    # LEGACY, TO REMOVE (for fuel-qa compatibility)
    def nodes(self):  # migrated from EnvironmentModel.nodes()
        warn(
            'environment.nodes is deprecated in favor of'
            ' environment.get_nodes', DeprecationWarning)
        # DEPRECATED. Please use environment.get_nodes() instead.

        class Nodes(object):
            def __init__(self, environment):
                self.admins = sorted(
                    list(environment.get_nodes(role__contains='master')),
                    key=lambda node: node.name
                )
                self.others = sorted(
                    list(environment.get_nodes(role='fuel_slave')),
                    key=lambda node: node.name
                )
                self.ironics = sorted(
                    list(environment.get_nodes(role='ironic')),
                    key=lambda node: node.name
                )
                self.slaves = self.others
                self.all = self.slaves + self.admins + self.ironics
                if len(self.admins) == 0:
                    raise DevopsEnvironmentError(
                        "No nodes with role 'fuel_master' found in the "
                        "environment {env_name}, please check environment "
                        "configuration".format(
                            env_name=environment.name
                        ))
                self.admin = self.admins[0]

            def __iter__(self):
                return self.all.__iter__()

        return Nodes(self)

    # BACKWARD COMPATIBILITY LAYER
    def _create_network_object(self, l2_network_device):
        class LegacyNetwork(object):
            def __init__(self):
                self.id = l2_network_device.id
                self.name = l2_network_device.name
                self.uuid = l2_network_device.uuid
                self.environment = self
                self.has_dhcp_server = l2_network_device.dhcp
                self.has_pxe_server = l2_network_device.has_pxe_server
                self.has_reserved_ips = True
                self.tftp_root_dir = ''
                self.forward = l2_network_device.forward.mode
                self.net = l2_network_device.address_pool.net
                self.ip_network = l2_network_device.address_pool.net
                self.ip = l2_network_device.address_pool.ip_network
                self.ip_pool_start = (
                    l2_network_device.address_pool.ip_network[2])
                self.ip_pool_end = (
                    l2_network_device.address_pool.ip_network[-2])
                self.netmask = (
                    l2_network_device.address_pool.ip_network.netmask)
                self.default_gw = l2_network_device.address_pool.ip_network[1]

        return LegacyNetwork()

    def get_env_l2_network_device(self, **kwargs):
        try:
            return L2NetworkDevice.objects.get(
                group__environment=self, **kwargs)
        except L2NetworkDevice.DoesNotExist:
            raise DevopsObjNotFound(L2NetworkDevice, **kwargs)

    def get_env_l2_network_devices(self, **kwargs):
        return L2NetworkDevice.objects.filter(
            group__environment=self, **kwargs).order_by('id')

    # LEGACY, TO CHECK IN fuel-qa / PROXY
    def get_network(self, **kwargs):
        l2_network_device = self.get_env_l2_network_device(
            address_pool__isnull=False, **kwargs)
        return self._create_network_object(l2_network_device)

    # LEGACY, TO CHECK IN fuel-qa / PROXY
    def get_networks(self, **kwargs):
        l2_network_devices = self.get_env_l2_network_devices(
            address_pool__isnull=False, **kwargs)
        return [self._create_network_object(x) for x in l2_network_devices]

    # LEGACY, for fuel-qa compatibility
    def get_node(self, *args, **kwargs):
        try:
            return Node.objects.get(*args, group__environment=self, **kwargs)
        except Node.DoesNotExist:
            raise DevopsObjNotFound(Node, *args, **kwargs)

    # LEGACY, for fuel-qa compatibility
    def get_nodes(self, *args, **kwargs):
        return Node.objects.filter(
            *args, group__environment=self, **kwargs).order_by('id')
