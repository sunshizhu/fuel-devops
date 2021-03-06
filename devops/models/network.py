#    Copyright 2013 - 2015 Mirantis, Inc.
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

from copy import deepcopy

from django.db import IntegrityError
from django.db import models
from django.db import transaction
import jsonfield
from netaddr import IPNetwork

from devops.error import DevopsError
from devops.helpers.helpers import generate_mac
from devops.helpers.network import IpNetworksPool
from devops import logger
from devops.models.base import BaseModel
from devops.models.base import choices
from devops.models.base import ParamedModel
from devops.models.base import ParamField


class AddressPool(ParamedModel, BaseModel):
    """Address pool

    address_pools:
      <address_pool_name>:
        net: <IPNetwork[:prefix]>
        params:  # Optional params for the address pool
          vlan_start: <int>
          vlan_end: <int>
          ip_reserved:
            <'gateway'>:<int|IPAddress>            # Reserved for gateway.
            <'l2_network_device'>:<int|IPAddress>  # Reserved for local IP
                                                   # for libvirt networks.
            ...  # user-defined IPs (for fuel-qa)
          ip_ranges:
            <group_name>: [<int|IPAddress>, <int|IPAddress>]
            ...  # user-defined ranges (for fuel-qa, 'floating' for example)


    Template example (address_pools):
    ---------------------------------

    address_pools:

      fuelweb_admin-pool01:
        net: 172.0.0.0/16:24
        params:
          ip_reserved:
            gateway: 1
            l2_network_device: 1  # l2_network_device will get the
                                  # IP address = 172.0.*.1  (net + 1)
          ip_ranges:
            default: [2, -2]     # admin IP range for 'default' nodegroup name

      public-pool01:
        net: 12.34.56.0/26    # Some WAN routed to the test host.
        params:
          vlan_start: 100
          ip_reserved:
            gateway: 12.34.56.1
            l2_network_device: 12.34.56.62 # l2_network_device will be assumed
                                           # with this IP address.
                                           # It will be used for create libvirt
                                           # network if libvirt driver is used.
          ip_ranges:
            default: [2, 127]   # public IP range for 'default' nodegroup name
            floating: [128, -2] # floating IP range

      storage-pool01:
        net: 172.0.0.0/16:24
        params:
          vlan_start: 101
          ip_reserved:
            l2_network_device: 1  # 172.0.*.1

      management-pool01:
        net: 172.0.0.0/16:24
        params:
          vlan_start: 102
          ip_reserved:
            l2_network_device: 1  # 172.0.*.1

      private-pool01:
        net: 192.168.0.0/24:26
        params:
          vlan_start: 103
          ip_reserved:
            l2_network_device: 1  # 192.168.*.1

    """
    class Meta(object):
        unique_together = ('name', 'environment')
        db_table = 'devops_address_pool'
        app_label = 'devops'

    environment = models.ForeignKey('Environment')
    name = models.CharField(max_length=255)
    net = models.CharField(max_length=255, unique=True)
    vlan_start = ParamField()
    vlan_end = ParamField()
    tag = ParamField()  # DEPRECATED, use vlan_start instead

    # ip_reserved = {'l2_network_device': 'm.m.m.50',
    #                'gateway': 'n.n.n.254', ...}
    ip_reserved = ParamField(default={})

    # ip_ranges = {'range_a': ('x.x.x.x', 'y.y.y.y'),
    #              'range_b': ('a.a.a.a', 'b.b.b.b'), ...}
    ip_ranges = ParamField(default={})

    # NEW. Warning: Old implementation returned self.net
    @property
    def ip_network(self):
        """Return IPNetwork representation of self.ip_network field.

        :return: IPNetwork()
        """
        return IPNetwork(self.net)

    @property
    def gateway(self):
        """Get the default network gateway

        This property returns only default network gateway.

        :return: reserved IP address with key 'gateway', or
                 the first address in the address pool
                 (for fuel-qa compatibility).
        """
        return self.get_ip('gateway') or str(self.ip_network[1])

    def ip_range_start(self, range_name):
        """Return the IP address of start the IP range 'range_name'

        :return: str(IP) or None
        """
        if range_name in self.ip_ranges:
            return str(self.ip_ranges.get(range_name)[0])
        else:
            logger.debug("IP range '{0}' not found in the "
                         "address pool {1}".format(range_name, self.name))
            return None

    def ip_range_end(self, range_name):
        """Return the IP address of end the IP range 'range_name'

        :return: str(IP) or None
        """
        if range_name in self.ip_ranges:
            return str(self.ip_ranges.get(range_name)[1])
        else:
            logger.debug("IP range '{0}' not found in the "
                         "address pool {1}".format(range_name, self.name))
            return None

    def ip_range_set(self, range_name, ip_range_start, ip_range_end):
        """Set IP range in the address pool

        :param range_name: str, name of the range
        :param ip_range_start: str, first IP of the range
        :param ip_range_end: str, last IP of the range
        :rtype: None or exception DevopsError

        If range_name already exists, then DevopsError raises.
        """
        if range_name in self.ip_ranges:
            raise DevopsError(
                "Setting IP range '{0}' for address pool '{1}' failed: range "
                "already exists".format(range_name, self.name))
        self.ip_ranges[range_name] = (ip_range_start, ip_range_end)
        self.save()

    def get_ip(self, ip_name):
        """Return the reserved IP

           For example, 'gateway' is one of the common reserved IPs

        :return: str(IP) or None
        """
        if ip_name in self.ip_reserved:
            return str(self.ip_reserved.get(ip_name))
        else:
            logger.debug("Reserved IP '{0}' not found in the "
                         "address pool {1}".format(ip_name, self.name))
            return None

    def next_ip(self):
        for ip in self.ip_network.iter_hosts():
            # if ip < self.ip_pool_start or ip > self.ip_pool_end:
            # Skip net, gw and broadcast addresses in the address pool
            if ip < self.ip_network[2] or ip > self.ip_network[-2]:
                continue
            already_exists = Address.objects.filter(
                interface__l2_network_device__address_pool=self,
                ip_address=str(ip)).exists()
            if already_exists:
                continue
            return ip
        raise DevopsError("No more free addresses in the address pool {0}"
                          " with CIDR {1}".format(self.name, self.net))

    @classmethod
    def _safe_create_network(cls, name, pool, environment, **params):
        for ip_network in pool:
            if cls.objects.filter(net=str(ip_network)).exists():
                continue

            new_params = deepcopy(params)
            new_params['net'] = ip_network
            try:
                with transaction.atomic():
                    return cls.objects.create(
                        environment=environment,
                        name=name,
                        **new_params
                    )
            except IntegrityError as e:
                logger.debug(e)
                if 'name' in str(e):
                    raise DevopsError(
                        'AddressPool with name "{}" already exists'
                        ''.format(name))
                continue

        raise DevopsError("There is no network pool available for creating "
                          "address pool {}".format(name))

    @classmethod
    def address_pool_create(cls, name, environment, pool=None, **params):
        """Create network

        :rtype : Network
        """
        if pool is None:
            pool = IpNetworksPool(
                networks=[IPNetwork('10.0.0.0/16')],
                prefix=24,
                allocated_networks=environment.get_allocated_networks())

        address_pool = cls._safe_create_network(
            environment=environment,
            name=name,
            pool=pool,
            **params
        )

        # Translate indexes into IP addresses for ip_reserved and ip_ranges
        def _relative_to_ip(ip_network, ip_id):
            """Get an IP from IPNetwork ip's list by index

            :param ip_network: IPNetwork object
            :param ip_id: string, if contains '+' or '-' then it is
                          used as index of an IP address in ip_network,
                          else it is considered as IP address.

            :rtype : str(IP)
            """
            if isinstance(ip_id, int):
                return str(ip_network[int(ip_id)])
            else:
                return str(ip_id)

        if 'ip_reserved' in params:
            for ip_res in params['ip_reserved'].keys():
                ip = _relative_to_ip(address_pool.ip_network,
                                     params['ip_reserved'][ip_res])
                params['ip_reserved'][ip_res] = ip      # Store to template
                address_pool.ip_reserved[ip_res] = ip   # Store to the object

        if 'ip_ranges' in params:
            for ip_range in params['ip_ranges']:
                ipr_start = _relative_to_ip(address_pool.ip_network,
                                            params['ip_ranges'][ip_range][0])
                ipr_end = _relative_to_ip(address_pool.ip_network,
                                          params['ip_ranges'][ip_range][1])
                params['ip_ranges'][ip_range] = (ipr_start, ipr_end)
                address_pool.ip_ranges[ip_range] = (ipr_start, ipr_end)

        address_pool.save()
        return address_pool


class NetworkPool(BaseModel):
    """Network pools for mapping logical (OpenStack) networks and AddressPools

    This object is not used for environment creation, only for mapping some
    logical networks with AddressPool objects for each node group.

    The same network (for example: 'public') that is used in different node
    groups, can be mapped on the same AddressPool for all node groups, or
    different AddressPools can be specified for each node group:

    Template example (network_pools):
    ---------------------------------

    groups:
     - name: default

       network_pools:  # Address pools for OpenStack networks.
         # Actual names should be used for keys
         # (the same as in Nailgun, for example)

         fuelweb_admin: fuelweb_admin-pool01
         public: public-pool01
         storage: storage-pool01
         management: management-pool01
         private: private-pool01

     - name: second_node_group

       network_pools:
         # The same address pools for admin/PXE and management networks
         fuelweb_admin: fuelweb_admin-pool01
         management: management-pool01

         # Another address pools for public, storage and private
         public: public-pool02
         storage: storage-pool02
         private: private-pool02


    :attribute name: name of one of the OpenStack(Nailgun) networks
    :attribute address_pool: key for the 'address_pool' object
    :attribute group: key for the 'group' object
    """
    class Meta(object):
        db_table = 'devops_network_pool'
        app_label = 'devops'

    group = models.ForeignKey('Group', null=True)
    address_pool = models.ForeignKey('AddressPool', null=True)
    name = models.CharField(max_length=255)

    def ip_range(self, range_name=None, relative_start=2, relative_end=-2):
        """Get IP range for the network pool

        :param range_name: str or None.

        For fuel-qa compatibility, default values are used if the range_name
        was not set:

        :param relative_start:
            int, default value for start of 'range_name'.
            relative from address_pool.ip_network, default=2

        :param relative_end:
            int, default value for end of 'range_name'.
            relative from address_pool.ip_network, default=-2

        :return: tuple of two IPs for the range - ('x.x.x.x', 'y.y.y.y')

        If 'range_name' is None: group.name is used as a default range.
        If 'range_name' not found in self.address_pool.ip_ranges:
        - IPs for the range are calculated using relative_start and
            relative_end values for the self.address_pool.ip_network.

        - Calculated range is stored in self.address_pool.ip_ranges for
            further usage. (for fuel-qa compatibility)
        """
        if range_name is None:
            range_name = self.group.name

        if range_name in self.address_pool.ip_ranges:
            return (self.address_pool.ip_range_start(range_name),
                    self.address_pool.ip_range_end(range_name))
        else:
            ip_range_start = str(self.address_pool.ip_network[relative_start])
            ip_range_end = str(self.address_pool.ip_network[relative_end])
            self.address_pool.ip_range_set(
                range_name, ip_range_start, ip_range_end)
            return ip_range_start, ip_range_end

    @property
    def gateway(self):
        """Get the network gateway

        :return: reserved IP address with key 'gateway', or
                 the first address in the address pool
                 (for fuel-qa compatibility).
        """
        return self.address_pool.gateway

    @property
    def vlan_start(self):
        """Get the network VLAN tag ID or start ID of VLAN range

        :return: int
        """
        return self.address_pool.vlan_start or self.address_pool.tag

    @property
    def vlan_end(self):
        """Get end ID of VLAN range

        :return: int
        """
        return self.address_pool.vlan_end

    # LEGACY, for fuel-qa compatibility if MULTIPLE_NETWORKS enabled
    @property
    def network(self):
        return self.l2_network_device

    @property
    def net(self):
        """Get the network CIDR

        :return: str('x.x.x.x/y')
        """
        return self.address_pool.net


class L2NetworkDevice(ParamedModel, BaseModel):
    class Meta(object):
        db_table = 'devops_l2_network_device'
        app_label = 'devops'

    group = models.ForeignKey('Group', null=True)
    address_pool = models.ForeignKey('AddressPool', null=True)
    name = models.CharField(max_length=255)

    @property
    def driver(self):
        return self.group.driver

    @property
    def interfaces(self):
        return self.interface_set.all()

    def define(self):
        self.save()

    def start(self):
        pass

    def destroy(self):
        pass

    def erase(self):
        self.remove()

    def remove(self, **kwargs):
        self.delete()

    @property
    def is_blocked(self):
        """Returns state of network"""
        return False

    def block(self):
        """Block all traffic in network"""
        pass

    def unblock(self):
        """Unblock all traffic in network"""
        pass


class NetworkConfig(models.Model):
    class Meta(object):
        db_table = 'devops_network_config'
        app_label = 'devops'

    node = models.ForeignKey('Node')
    label = models.CharField(max_length=255, null=False)
    networks = jsonfield.JSONField(default=[])
    aggregation = models.CharField(max_length=255, null=True)
    parents = jsonfield.JSONField(default=[])


class Interface(ParamedModel):
    class Meta(object):
        db_table = 'devops_interface'
        app_label = 'devops'

    node = models.ForeignKey('Node')
    l2_network_device = models.ForeignKey('L2NetworkDevice', null=True)
    label = models.CharField(max_length=255, null=True)
    mac_address = models.CharField(max_length=255, unique=True, null=False)
    type = models.CharField(max_length=255, null=False)
    model = choices('virtio', 'e1000', 'pcnet', 'rtl8139', 'ne2k_pci')
    features = ParamField(default=[])

    @property
    def driver(self):
        return self.node.driver

    # LEGACY, for fuel-qa compatibility if MULTIPLE_NETWORKS enabled
    @property
    def network(self):
        return self.l2_network_device

    @property
    def target_dev(self):
        return self.label

    @property
    def addresses(self):
        return self.address_set.all()

    @property
    def network_config(self):
        return self.node.networkconfig_set.get(label=self.label)

    def define(self):
        self.save()

    def remove(self):
        self.delete()

    def add_address(self):
        ip = self.l2_network_device.address_pool.next_ip()
        Address.objects.create(
            ip_address=str(ip),
            interface=self,
        )

    @property
    def is_blocked(self):
        """Show state of interface"""
        return False

    def block(self):
        """Block traffic on interface"""
        pass

    def unblock(self):
        """Unblock traffic on interface"""
        pass

    @classmethod
    def interface_create(cls, l2_network_device, node, label,
                         if_type='network', mac_address=None, model='virtio',
                         features=None):
        """Create interface

        :rtype : Interface
        """
        interface = cls.objects.create(
            l2_network_device=l2_network_device,
            node=node,
            label=label,
            type=if_type,
            mac_address=mac_address or generate_mac(),
            model=model,
            features=features or [])
        if (interface.l2_network_device and
                interface.l2_network_device.address_pool is not None):
            interface.add_address()
        return interface


class Address(models.Model):
    class Meta(object):
        db_table = 'devops_address'
        app_label = 'devops'

    interface = models.ForeignKey('Interface', null=True)
    ip_address = models.GenericIPAddressField()
