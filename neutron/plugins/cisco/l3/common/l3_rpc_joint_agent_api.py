# Copyright 2014 Cisco Systems, Inc.  All rights reserved.
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
#

from neutron.common import constants
from neutron.common import topics
from neutron.common import utils
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common.rpc import proxy
from neutron.plugins.cisco.l3.common import constants as cl3_constants
from neutron.plugins.common import constants as service_constants

LOG = logging.getLogger(__name__)


AGENT_TYPE = {topics.L3_AGENT: " ",
              cl3_constants.CFG_AGENT: " cfg "}


class L3JointAgentNotifyAPI(proxy.RpcProxy):
    """API for plugin to notify Cisco cfg agent and L3 agent."""
    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic=topics.L3_AGENT):
        super(L3JointAgentNotifyAPI, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)

    def _notification_host(self, context, method, payload, host,
                           topic=topics.L3_AGENT):
        """Notify the agent that is hosting the router."""
        LOG.debug(_('Notify agent at %(host)s the message '
                    '%(method)s'), {'host': host,
                                    'method': method})
        self.cast(context,
                  self.make_msg(method, payload=payload),
                  topic='%s.%s' % (topic, host))

    def _agent_notification(self, context, method, routers,
                            operation, data):
        """Notify individual L3 agents and Cisco cfg agents."""
        adminContext = context.is_admin and context or context.elevated()
        plugin = manager.NeutronManager.get_service_plugins().get(
            service_constants.L3_ROUTER_NAT)
        for router in routers:
            if router['router_type'] == cl3_constants.NAMESPACE_ROUTER_TYPE:
                agents = plugin.get_l3_agents_hosting_routers(
                    adminContext, [router['id']],
                    admin_state_up=True,
                    active=True)
            elif (router['hosting_device'] is not None and
                  utils.is_extension_supported(
                    plugin, constants.L3_AGENT_SCHEDULER_EXT_ALIAS)):
                agents = plugin.get_cfg_agents_for_hosting_devices(
                    adminContext, [router['hosting_device']['id']],
                    admin_state_up=True,
                    active=True)
            else:
                agents = []
            for agent in agents:
                LOG.debug(_('Notify l3%(type)sagent at %(topic)s.%(host)s the '
                            'message %(method)s'),
                          {'type': AGENT_TYPE[agent.topic],
                           'topic': agent.topic,
                           'host': agent.host,
                           'method': method})
                self.cast(context,
                          self.make_msg(method, routers=[router['id']]),
                          topic='%s.%s' % (agent.topic, agent.host),
                          version='1.1')

    def _notification(self, context, method, routers, operation, data):
        """Notify all or individual L3 agents and Cisco cfg agents."""
        plugin = manager.NeutronManager.get_service_plugins().get(
            service_constants.L3_ROUTER_NAT)
        if utils.is_extension_supported(
                plugin, constants.L3_AGENT_SCHEDULER_EXT_ALIAS):
            adminContext = (context.is_admin and
                            context or context.elevated())
            # This is where a hosting device gets scheduled to a
            # Cisco cfg agent and where network namespace-based
            # routers get scheduled to a l3 agent.
            plugin.schedule_routers(adminContext, routers)
            self._agent_notification(
                context, method, routers, operation, data)
        else:
            self.fanout_cast(
                context, self.make_msg(method,
                                       routers=[r['id'] for r in routers]),
                topic=topics.L3_AGENT)

    def _notification_fanout(self, context, method, router_id):
        """Fanout the deleted router to all L3 agents."""
        LOG.debug(_('Fanout notify agent at %(topic)s the message '
                    '%(method)s on router %(router_id)s'),
                  {'topic': topics.DHCP_AGENT,
                   'method': method,
                   'router_id': router_id})
        self.fanout_cast(context,
                         self.make_msg(method, router_id=router_id),
                         topic=topics.L3_AGENT)

    def agent_updated(self, context, admin_state_up, host):
        """Updates agent on host to enable or disable it."""
        #TODO(bobmel): Ensure correct topic is used for Cisco cfg agent
        self._notification_host(context, 'agent_updated',
                                {'admin_state_up': admin_state_up},
                                host)

    def router_deleted(self, context, router):
        """Notifies agents about a deleted router."""
        if router['router_type'] == cl3_constants.NAMESPACE_ROUTER_TYPE:
            self._notification_fanout(context, 'router_deleted', router['id'])
        else:
            self._agent_notification(context, 'router_deleted', [router],
                                     operation=None, data=None)

    def routers_updated(self, context, routers, operation=None, data=None):
        """Notifies agents about configuration changes to routers.

        This includes operations performed on the router like when a
        router interface is added or removed.

        L3 agent or Cisco configuration agent are receiver of the
        notification.
        """
        if routers:
            self._notification(context, 'routers_updated', routers,
                               operation, data)

    def router_removed_from_agent(self, context, router_id, host):
        """Notifies L3 agent on host that router has been removed from it."""
        self._notification_host(context, 'router_removed_from_agent',
                                {'router_id': router_id}, host,
                                topic=topics.L3_AGENT)

    def router_added_to_agent(self, context, routers, host):
        """Notifies L3 agent on host that router has been added to it."""
        self._notification_host(context, 'router_added_to_agent',
                                routers, host,
                                topic=topics.L3_AGENT)

    def router_removed_from_hosting_device(self, context, router_id, host):
        """Notification that router has been removed from hosting device.

        A Cisco configuration agent is the receiver of these notifications.
        """
        self._notification_host(context, 'router_removed_from_hosting_device',
                                {'router_id': router_id}, host,
                                topic=cl3_constants.CFG_AGENT)

    def router_added_to_hosting_device(self, context, routers, host):
        """Notification that router has been added to hosting device.

        A Cisco configuration agent is the receiver of these notifications.
        """
        self._notification_host(context, 'router_added_to_hosting_device',
                                routers, host,
                                topic=cl3_constants.CFG_AGENT)

    def hosting_device_removed(self, context, hosting_data, deconfigure,
                               cfg_agent):
        """Notification that a hosting device has been removed.

        A Cisco configuration agent is the receiver of these notifications.
        """
        if hosting_data:
            self._notification_host(context, 'hosting_device_removed',
                                    {'hosting_data': hosting_data,
                                     'deconfigure': deconfigure}, cfg_agent,
                                    topic=cl3_constants.CFG_AGENT)


L3JointAgentNotify = L3JointAgentNotifyAPI()