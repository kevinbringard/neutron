# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
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
# @author: Kyle Mestery, Cisco Systems, Inc.

from oslo.config import cfg
import requests

from neutron.extensions import portbindings
from neutron.openstack.common import jsonutils
from neutron.openstack.common import log
from neutron.plugins.common import constants
from neutron.plugins.ml2 import driver_api as api

LOG = log.getLogger(__name__)

odl_opts = [
    cfg.StrOpt('url',
               help=_("HTTP URL of OpenDaylight REST interface.")),
    cfg.StrOpt('username',
               help=_("HTTP username for authentication")),
    cfg.StrOpt('password', secret=True,
               help=_("HTTP password for authentication")),
    cfg.IntOpt('timeout', default=10,
               help=_("HTTP timeout in seconds."))
]

cfg.CONF.register_opts(odl_opts, "ml2_odl")


class OpenDaylightMechanismDriver(api.MechanismDriver):

    """Mechanism Driver for OpenDaylight.

    This driver was a port from the Tail-F NCS MechanismDriver.  The API
    exposed by ODL is slightly different from the API exposed by NCS,
    but the general concepts are the same.
    """

    out_of_sync = True

    def initialize(self):
        self.url = cfg.CONF.ml2_odl.url
        self.timeout = cfg.CONF.ml2_odl.timeout
        self.username = cfg.CONF.ml2_odl.username
        self.password = cfg.CONF.ml2_odl.password
        self.vif_type = portbindings.VIF_TYPE_OVS
        self.cap_port_filter = False

    # Postcommit hooks are used to trigger synchronization.

    def create_network_postcommit(self, context):
        self.synchronize('create', 'networks', context)

    def update_network_postcommit(self, context):
        self.synchronize('update', 'networks', context)

    def delete_network_postcommit(self, context):
        self.synchronize('delete', 'networks', context)

    def create_subnet_postcommit(self, context):
        self.synchronize('create', 'subnets', context)

    def update_subnet_postcommit(self, context):
        self.synchronize('update', 'subnets', context)

    def delete_subnet_postcommit(self, context):
        self.synchronize('delete', 'subnets', context)

    def create_port_postcommit(self, context):
        self.synchronize('create', 'ports', context)

    def update_port_postcommit(self, context):
        self.synchronize('update', 'ports', context)

    def delete_port_postcommit(self, context):
        self.synchronize('delete', 'ports', context)

    def synchronize(self, operation, object_type, context):
        """Synchronize ODL with Neutron following a configuration change."""
        if self.out_of_sync:
            self.sync_full(context)
        else:
            self.sync_object(operation, object_type, context)

    def sync_full(self, context):
        """Resync the entire database to ODL.
        Transition to the in-sync state on success.
        """
        dbcontext = context._plugin_context
        networks = context._plugin.get_networks(dbcontext)
        subnets = context._plugin.get_subnets(dbcontext)
        ports = context._plugin.get_ports(dbcontext)

        nets = []
        for network in networks:
            try:
                urlpath = 'networks' + '/' + network['id']
                self.sendjson('get', urlpath, None)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    for d in ['status', 'subnets']:
                        if d in network:
                            del network[d]
                    nets.append(network)
                continue

        if len(nets) > 1:
            self.sendjson('post', 'networks', {'networks': nets})
        elif len(nets) == 1:
            self.sendjson('post', 'networks', {'network': nets})

        snets = []
        for subnet in subnets:
            try:
                urlpath = 'subnets' + '/' + subnet['id']
                self.sendjson('get', urlpath, None)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    snets.append(subnet)
                continue

        if len(snets) > 1:
            self.sendjson('post', 'subnets', {'subnets': snets})
        elif len(snets) == 1:
            self.sendjson('post', 'subnets', {'subnet': snets})

        pports = []
        for port in ports:
            try:
                urlpath = 'ports' + '/' + port['id']
                self.sendjson('get', urlpath, None)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    self.add_security_groups(context, dbcontext, port)
                    # TODO(kmestery): Converting to uppercase due to ODL bug
                    port['mac_address'] = port['mac_address'].upper()
                    if 'status' in port:
                        del port['status']
                    pports.append(port)
                continue

        if len(pports) > 1:
            self.sendjson('post', 'ports', {'ports': pports})
        elif len(pports) == 1:
            self.sendjson('post', 'ports', {'port': pports})
        self.out_of_sync = False

    def sync_object(self, operation, object_type, context):
        """Synchronize the single modified record to ODL.
        """
        self.out_of_sync = True
        dbcontext = context._plugin_context
        id = context.current['id']
        if operation == 'delete':
            urlpath = object_type + '/' + id
            self.sendjson('delete', urlpath, None)
        else:
            assert operation == 'create' or operation == 'update'
            if operation == 'create':
                urlpath = object_type
                method = 'post'
            else:
                urlpath = object_type + '/' + id
                method = 'put'
            if object_type == 'networks':
                network = context._plugin.get_network(dbcontext, id)
                # Remove the following for update calls
                if operation == 'update':
                    for d in ['id', 'status', 'subnets', 'tenant_id']:
                        if d in network:
                            del network[d]
                elif operation == 'create':
                    for d in ['status', 'subnets']:
                        if d in network:
                            del network[d]
                self.sendjson(method, urlpath, {'network': network})
            elif object_type == 'subnets':
                subnet = context._plugin.get_subnet(dbcontext, id)
                # Remove the following for update calls
                if operation == 'update':
                    for d in ['id', 'network_id', 'ip_version', 'cidr',
                              'allocation_pools', 'tenant_id']:
                        if d in subnet:
                            del subnet[d]
                self.sendjson(method, urlpath, {'subnet': subnet})
            else:
                assert object_type == 'ports'
                port = context._plugin.get_port(dbcontext, id)
                self.add_security_groups(context, dbcontext, port)
                if operation == 'create':
                    # TODO(kmestery): Converting to uppercase due to ODL bug
                    port['mac_address'] = port['mac_address'].upper()
                    if 'status' in port:
                        del port['status']
                elif operation == 'update':
                    # Remove the following for update calls
                    for d in ['network_id', 'id', 'status', 'mac_address',
                              'tenant_id', 'fixed_ips']:
                        if d in port:
                            del port[d]
                self.sendjson(method, urlpath, {'port': port})
        self.out_of_sync = False

    def add_security_groups(self, context, dbcontext, port):
        """Populate the 'security_groups' field with entire records."""
        groups = [context._plugin.get_security_group(dbcontext, sg)
                  for sg in port['security_groups']]
        port['security_groups'] = groups

    def sendjson(self, method, urlpath, obj):
        headers = {'Content-Type': 'application/json'}
        if obj is None:
            data = None
        else:
            data = jsonutils.dumps(obj, indent=2)
        auth = None
        if self.username and self.password:
            auth = (self.username, self.password)
        if self.url:
            url = '/'.join([self.url, urlpath])
            LOG.debug(_('ODL-----> sending URL (%s) <-----ODL') % url)
            LOG.debug(_('ODL-----> sending JSON (%s) <-----ODL') % obj)
            r = requests.request(method, url=url,
                                 headers=headers, data=data,
                                 auth=auth, timeout=self.timeout)
            r.raise_for_status()

    def bind_port(self, context):
        LOG.debug(_("Attempting to bind port %(port)s on "
                    "network %(network)s"),
                  {'port': context.current['id'],
                   'network': context.network.current['id']})
        for segment in context.network.network_segments:
            if self.check_segment(segment):
                context.set_binding(segment[api.ID],
                                    self.vif_type,
                                    self.cap_port_filter)
                LOG.debug(_("Bound using segment: %s"), segment)
                return
            else:
                LOG.error(_("Failed binding port for segment ID %(id)s, "
                            "segment %(seg)s, phys net %(physnet)s, and "
                            "network type %(nettype)s"),
                          {'id': segment[api.ID],
                           'seg': segment[api.SEGMENTATION_ID],
                           'physnet': segment[api.PHYSICAL_NETWORK],
                           'nettype': segment[api.NETWORK_TYPE]})

    def validate_port_binding(self, context):
        if self.check_segment(context.bound_segment):
            LOG.debug(_('Binding valid.'))
            return True
        LOG.warning(_("Binding invalid for port: %s"), context.current)
        return False

    def unbind_port(self, context):
        LOG.debug(_("Unbinding port %(port)s on "
                    "network %(network)s"),
                  {'port': context.current['id'],
                   'network': context.network.current['id']})

    def check_segment(self, segment):
        """Verify a segment is valid for the OpenDaylight MechanismDriver

        Verify the requested segment is supported by ODL and return True or
        False to indicate this to callers.
        """
        network_type = segment[api.NETWORK_TYPE]
        if network_type in [constants.TYPE_LOCAL, constants.TYPE_FLAT,
                            constants.TYPE_VLAN, constants.TYPE_GRE,
                            constants.TYPE_VXLAN]:
            #TODO(mestery): Validate these
            return True
        else:
            return False
