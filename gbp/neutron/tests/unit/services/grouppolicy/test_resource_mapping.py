# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import itertools
import netaddr

import mock
from neutron.api.rpc.agentnotifiers import dhcp_rpc_agent_api
from neutron.common import constants as cst
from neutron import context as nctx
from neutron.extensions import external_net as external_net
from neutron.extensions import securitygroup as ext_sg
from neutron import manager
from neutron.notifiers import nova
from neutron.openstack.common import uuidutils
from neutron.tests.unit import test_extension_security_group
from neutron.tests.unit import test_l3_plugin
import webob.exc

from gbp.neutron.db import servicechain_db
from gbp.neutron.services.grouppolicy.common import constants as gconst
from gbp.neutron.services.grouppolicy import config
from gbp.neutron.services.grouppolicy.drivers import resource_mapping
from gbp.neutron.tests.unit.services.grouppolicy import test_grouppolicy_plugin

SERVICECHAIN_NODES = 'servicechain/servicechain_nodes'
SERVICECHAIN_SPECS = 'servicechain/servicechain_specs'
SERVICECHAIN_INSTANCES = 'servicechain/servicechain_instances'


class NoL3NatSGTestPlugin(
        test_l3_plugin.TestNoL3NatPlugin,
        test_extension_security_group.SecurityGroupTestPlugin):

    supported_extension_aliases = ["external-net", "security-group"]


CORE_PLUGIN = ('gbp.neutron.tests.unit.services.grouppolicy.'
               'test_resource_mapping.NoL3NatSGTestPlugin')


class ResourceMappingTestCase(
        test_grouppolicy_plugin.GroupPolicyPluginTestCase):

    def setUp(self):
        config.cfg.CONF.set_override('policy_drivers',
                                     ['implicit_policy', 'resource_mapping'],
                                     group='group_policy')
        config.cfg.CONF.set_override('servicechain_drivers',
                                     ['dummy'],
                                     group='servicechain')
        config.cfg.CONF.set_override('allow_overlapping_ips', True)
        super(ResourceMappingTestCase, self).setUp(core_plugin=CORE_PLUGIN)
        res = mock.patch('neutron.db.l3_db.L3_NAT_dbonly_mixin.'
                         '_check_router_needs_rescheduling').start()
        res.return_value = None
        self.__plugin = manager.NeutronManager.get_plugin()
        self.__context = nctx.get_admin_context()

    def get_plugin_context(self):
        return self.__plugin, self.__context

    def _create_network(self, fmt, name, admin_state_up, **kwargs):
        """Override the routine for allowing the router:external attribute."""
        # attributes containing a colon should be passed with
        # a double underscore
        new_args = dict(itertools.izip(map(lambda x: x.replace('__', ':'),
                                           kwargs),
                                       kwargs.values()))
        arg_list = new_args.pop('arg_list', ()) + (external_net.EXTERNAL,)
        return super(ResourceMappingTestCase, self)._create_network(
            fmt, name, admin_state_up, arg_list=arg_list, **new_args)

    def _get_sg_rule(self, **filters):
        plugin = manager.NeutronManager.get_plugin()
        context = nctx.get_admin_context()
        return plugin.get_security_group_rules(
            context, filters)

    def _get_prs_mapping(self, prs_id):
        ctx = nctx.get_admin_context()
        return (resource_mapping.ResourceMappingDriver.
                _get_policy_rule_set_sg_mapping(ctx.session, prs_id))

    def _create_ssh_allow_rule(self):
        return self._create_tcp_allow_rule('22')

    def _create_http_allow_rule(self):
        return self._create_tcp_allow_rule('80')

    def _create_tcp_allow_rule(self, port_range):
        action = self.create_policy_action(
            action_type='allow')['policy_action']
        classifier = self.create_policy_classifier(
            protocol='TCP', port_range=port_range,
            direction='bi')['policy_classifier']
        return self.create_policy_rule(
            policy_classifier_id=classifier['id'],
            policy_actions=[action['id']])['policy_rule']

    def _calculate_expected_external_cidrs(self, es, l3p_list):
        external_ipset = netaddr.IPSet([x['destination']
                                        for x in es['external_routes']])
        if l3p_list:
            result = external_ipset - netaddr.IPSet([x['ip_pool']
                                                     for x in l3p_list])
        else:
            result = external_ipset

        return set(str(x) for x in result.iter_cidrs())


class TestPolicyTarget(ResourceMappingTestCase):

    def test_implicit_port_lifecycle(self):
        # Create policy_target group.
        ptg = self.create_policy_target_group(name="ptg1")
        ptg_id = ptg['policy_target_group']['id']

        # Create policy_target with implicit port.
        pt = self.create_policy_target(name="pt1",
                                       policy_target_group_id=ptg_id)
        pt_id = pt['policy_target']['id']
        port_id = pt['policy_target']['port_id']
        self.assertIsNotNone(port_id)

        # Create policy_target in shared policy_target group
        l3p = self.create_l3_policy(shared=True, ip_pool='11.0.0.0/8')
        l2p = self.create_l2_policy(l3_policy_id=l3p['l3_policy']['id'],
                                    shared=True)
        s_ptg = self.create_policy_target_group(name="s_ptg", shared=True,
                                           l2_policy_id=l2p['l2_policy']['id'])
        s_ptg_id = s_ptg['policy_target_group']['id']
        pt = self.create_policy_target(name="ep1",
                                       policy_target_group_id=s_ptg_id)
        self.assertIsNotNone(pt['policy_target']['port_id'])

        # TODO(rkukura): Verify implicit port belongs to policy_target
        # group's subnet.

        # Verify deleting policy_target cleans up port.
        req = self.new_delete_request('policy_targets', pt_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
        req = self.new_show_request('ports', port_id, fmt=self.fmt)
        res = req.get_response(self.api)
        self.assertEqual(res.status_int, webob.exc.HTTPNotFound.code)

    def test_explicit_port_lifecycle(self):
        # Create policy_target group.
        ptg = self.create_policy_target_group(name="ptg1")
        ptg_id = ptg['policy_target_group']['id']
        subnet_id = ptg['policy_target_group']['subnets'][0]
        req = self.new_show_request('subnets', subnet_id)
        subnet = self.deserialize(self.fmt, req.get_response(self.api))

        # Create policy_target with explicit port.
        with self.port(subnet=subnet) as port:
            port_id = port['port']['id']
            pt = self.create_policy_target(
                name="pt1", policy_target_group_id=ptg_id, port_id=port_id)
            pt_id = pt['policy_target']['id']
            self.assertEqual(port_id, pt['policy_target']['port_id'])

            # Verify deleting policy_target does not cleanup port.
            req = self.new_delete_request('policy_targets', pt_id)
            res = req.get_response(self.ext_api)
            self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
            req = self.new_show_request('ports', port_id, fmt=self.fmt)
            res = req.get_response(self.api)
            self.assertEqual(res.status_int, webob.exc.HTTPOk.code)

    def test_explicit_port_deleted(self):
        # Create policy_target group.
        ptg = self.create_policy_target_group(name="ptg1")
        ptg_id = ptg['policy_target_group']['id']
        subnet_id = ptg['policy_target_group']['subnets'][0]
        req = self.new_show_request('subnets', subnet_id)
        subnet = self.deserialize(self.fmt, req.get_response(self.api))

        # Create policy_target with explicit port.
        with self.port(subnet=subnet) as port:
            port_id = port['port']['id']
            pt = self.create_policy_target(
                name="pt1", policy_target_group_id=ptg_id, port_id=port_id)
            pt_id = pt['policy_target']['id']

            req = self.new_delete_request('ports', port_id)
            res = req.get_response(self.api)
            self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
            # Verify deleting policy_target does not cleanup port.
            req = self.new_delete_request('policy_targets', pt_id)
            res = req.get_response(self.ext_api)
            self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)

    def test_missing_ptg_rejected(self):
        data = self.create_policy_target(
            name="pt1", expected_res_status=webob.exc.HTTPBadRequest.code)
        self.assertEqual('PolicyTargetRequiresPolicyTargetGroup',
                         data['NeutronError']['type'])

    def test_explicit_port_subnet_mismatches_ptg_subnet_rejected(self):
        ptg1 = self.create_policy_target_group(name="ptg1")
        ptg1_id = ptg1['policy_target_group']['id']

        # Create a subnet that is different from ptg1 subnet (by
        # creating a new L2 policy and subnet)
        l2p = self.create_l2_policy(name="l2p1")
        network_id = l2p['l2_policy']['network_id']
        req = self.new_show_request('networks', network_id)
        network = self.deserialize(self.fmt, req.get_response(self.api))
        with self.subnet(network=network, cidr='10.10.1.0/24') as subnet:
            with self.port(subnet=subnet) as port:
                port_id = port['port']['id']

                data = self.create_policy_target(name="ep1",
                        policy_target_group_id=ptg1_id,
                        port_id=port_id,
                        expected_res_status=webob.exc.HTTPBadRequest.code)

                self.assertEqual('InvalidPortForPTG',
                         data['NeutronError']['type'])

    def test_missing_explicit_port_ptg_rejected(self):
        ptg1 = self.create_policy_target_group(name="ptg1")
        ptg1_id = ptg1['policy_target_group']['id']

        port_id = uuidutils.generate_uuid()
        data = self.create_policy_target(name="pt1",
                        policy_target_group_id=ptg1_id,
                        port_id=port_id,
                        expected_res_status=webob.exc.HTTPServerError.code)

        # TODO(krishna-sunitha): Need to change the below to the correct
        # exception after
        # https://bugs.launchpad.net/group-based-policy/+bug/1394000 is fixed
        self.assertEqual('HTTPInternalServerError',
                         data['NeutronError']['type'])

    def test_ptg_update_rejected(self):
        # Create two policy_target groups.
        ptg1 = self.create_policy_target_group(name="ptg1")
        ptg1_id = ptg1['policy_target_group']['id']
        ptg2 = self.create_policy_target_group(name="ptg2")
        ptg2_id = ptg2['policy_target_group']['id']

        # Create policy_target.
        pt = self.create_policy_target(name="pt1",
                                       policy_target_group_id=ptg1_id)
        pt_id = pt['policy_target']['id']

        # Verify updating policy_target group rejected.
        data = {'policy_target': {'policy_target_group_id': ptg2_id}}
        req = self.new_update_request('policy_targets', data, pt_id)
        data = self.deserialize(self.fmt, req.get_response(self.ext_api))
        self.assertEqual('PolicyTargetGroupUpdateOfPolicyTargetNotSupported',
                         data['NeutronError']['type'])


class TestPolicyTargetGroup(ResourceMappingTestCase):

    def _test_implicit_subnet_lifecycle(self, shared=False):
        # Use explicit L2 policy so network and subnet not deleted
        # with policy_target group.
        l2p = self.create_l2_policy(shared=shared)
        l2p_id = l2p['l2_policy']['id']

        # Create policy_target group with implicit subnet.
        ptg = self.create_policy_target_group(name="ptg1", l2_policy_id=l2p_id,
                                         shared=shared)
        ptg_id = ptg['policy_target_group']['id']
        subnets = ptg['policy_target_group']['subnets']
        self.assertIsNotNone(subnets)
        self.assertEqual(len(subnets), 1)
        subnet_id = subnets[0]

        # TODO(rkukura): Verify implicit subnet belongs to L2 policy's
        # network, is within L3 policy's ip_pool, and was added as
        # router interface.

        # Verify deleting policy_target group cleans up subnet.
        req = self.new_delete_request('policy_target_groups', ptg_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
        req = self.new_show_request('subnets', subnet_id, fmt=self.fmt)
        res = req.get_response(self.api)
        self.assertEqual(res.status_int, webob.exc.HTTPNotFound.code)

        # TODO(rkukura): Verify implicit subnet was removed as router
        # interface.

    def test_implicit_subnet_lifecycle(self):
        self._test_implicit_subnet_lifecycle()

    def test_implicit_subnet_lifecycle_shared(self):
        self._test_implicit_subnet_lifecycle(True)

    def test_explicit_subnet_lifecycle(self):
        # Create L3 policy.
        l3p = self.create_l3_policy(name="l3p1", ip_pool='10.0.0.0/8')
        l3p_id = l3p['l3_policy']['id']

        # Create L2 policy.
        l2p = self.create_l2_policy(name="l2p1", l3_policy_id=l3p_id)
        l2p_id = l2p['l2_policy']['id']
        network_id = l2p['l2_policy']['network_id']
        req = self.new_show_request('networks', network_id)
        network = self.deserialize(self.fmt, req.get_response(self.api))

        # Create policy_target group with explicit subnet.
        with self.subnet(network=network, cidr='10.10.1.0/24') as subnet:
            subnet_id = subnet['subnet']['id']
            ptg = self.create_policy_target_group(
                name="ptg1", l2_policy_id=l2p_id, subnets=[subnet_id])
            ptg_id = ptg['policy_target_group']['id']
            subnets = ptg['policy_target_group']['subnets']
            self.assertIsNotNone(subnets)
            self.assertEqual(len(subnets), 1)
            self.assertEqual(subnet_id, subnets[0])

            # TODO(rkukura): Verify explicit subnet was added as
            # router interface.

            # Verify deleting policy_target group does not cleanup subnet.
            req = self.new_delete_request('policy_target_groups', ptg_id)
            res = req.get_response(self.ext_api)
            self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
            req = self.new_show_request('subnets', subnet_id, fmt=self.fmt)
            res = req.get_response(self.api)
            self.assertEqual(res.status_int, webob.exc.HTTPOk.code)

            # TODO(rkukura): Verify explicit subnet was removed as
            # router interface.

    def test_add_subnet(self):
        # Create L3 policy.
        l3p = self.create_l3_policy(name="l3p1", ip_pool='10.0.0.0/8')
        l3p_id = l3p['l3_policy']['id']

        # Create L2 policy.
        l2p = self.create_l2_policy(name="l2p1", l3_policy_id=l3p_id)
        l2p_id = l2p['l2_policy']['id']
        network_id = l2p['l2_policy']['network_id']
        req = self.new_show_request('networks', network_id)
        network = self.deserialize(self.fmt, req.get_response(self.api))

        # Create policy_target group with explicit subnet.
        with contextlib.nested(
                self.subnet(network=network, cidr='10.10.1.0/24'),
                self.subnet(network=network, cidr='10.10.2.0/24')
        ) as (subnet1, subnet2):
            subnet1_id = subnet1['subnet']['id']
            subnet2_id = subnet2['subnet']['id']
            subnets = [subnet1_id]
            ptg = self.create_policy_target_group(
                l2_policy_id=l2p_id, subnets=subnets)
            ptg_id = ptg['policy_target_group']['id']

            # Add subnet.
            subnets = [subnet1_id, subnet2_id]
            data = {'policy_target_group': {'subnets': subnets}}
            req = self.new_update_request('policy_target_groups', data, ptg_id)
            res = req.get_response(self.ext_api)
            self.assertEqual(res.status_int, webob.exc.HTTPOk.code)

    def test_add_subnet_negative(self):
        # Create L2P
        l2p = self.create_l2_policy()['l2_policy']

        with self.network() as net:
            with self.subnet(network=net) as sub:
                # Asserted just for clarity
                self.assertNotEqual(net['network']['id'], l2p['network_id'])
                res = self.create_policy_target_group(
                    l2_policy_id=l2p['id'], subnets=[sub['subnet']['id']],
                    expected_res_status=400)
                self.assertEqual('InvalidSubnetForPTG',
                                 res['NeutronError']['type'])
                # Create valid PTG
                ptg = self.create_policy_target_group(
                    l2_policy_id=l2p['id'],
                    expected_res_status=201)['policy_target_group']
                res = self._update_gbp_resource_full_response(
                    ptg['id'], 'policy_target_group', 'policy_target_groups',
                    expected_res_status=400,
                    subnets=ptg['subnets'] + [sub['subnet']['id']])
                self.assertEqual('InvalidSubnetForPTG',
                                 res['NeutronError']['type'])

    def test_remove_subnet_rejected(self):
        # Create L3 policy.
        l3p = self.create_l3_policy(name="l3p1", ip_pool='10.0.0.0/8')
        l3p_id = l3p['l3_policy']['id']

        # Create L2 policy.
        l2p = self.create_l2_policy(name="l2p1", l3_policy_id=l3p_id)
        l2p_id = l2p['l2_policy']['id']
        network_id = l2p['l2_policy']['network_id']
        req = self.new_show_request('networks', network_id)
        network = self.deserialize(self.fmt, req.get_response(self.api))

        # Create policy_target group with explicit subnets.
        with contextlib.nested(
                self.subnet(network=network, cidr='10.10.1.0/24'),
                self.subnet(network=network, cidr='10.10.2.0/24')
        ) as (subnet1, subnet2):
            subnet1_id = subnet1['subnet']['id']
            subnet2_id = subnet2['subnet']['id']
            subnets = [subnet1_id, subnet2_id]
            ptg = self.create_policy_target_group(
                l2_policy_id=l2p_id, subnets=subnets)
            ptg_id = ptg['policy_target_group']['id']

            # Verify removing subnet rejected.
            data = {'policy_target_group': {'subnets': [subnet2_id]}}
            req = self.new_update_request('policy_target_groups', data, ptg_id)
            data = self.deserialize(self.fmt, req.get_response(self.ext_api))
            self.assertEqual('PolicyTargetGroupSubnetRemovalNotSupported',
                             data['NeutronError']['type'])

    def test_subnet_allocation(self):
        ptg1 = self.create_policy_target_group(name="ptg1")
        subnets = ptg1['policy_target_group']['subnets']
        req = self.new_show_request('subnets', subnets[0], fmt=self.fmt)
        subnet1 = self.deserialize(self.fmt, req.get_response(self.api))

        ptg2 = self.create_policy_target_group(name="ptg2")
        subnets = ptg2['policy_target_group']['subnets']
        req = self.new_show_request('subnets', subnets[0], fmt=self.fmt)
        subnet2 = self.deserialize(self.fmt, req.get_response(self.api))

        self.assertNotEqual(subnet1['subnet']['cidr'],
                            subnet2['subnet']['cidr'])

    def test_no_extra_subnets_created(self):
        count = len(self._get_all_subnets())
        self.create_policy_target_group()
        self.create_policy_target_group()
        new_count = len(self._get_all_subnets())
        self.assertEqual(count + 2, new_count)

    def _get_all_subnets(self):
        req = self.new_list_request('subnets', fmt=self.fmt)
        return self.deserialize(self.fmt,
                                req.get_response(self.api))['subnets']

    def test_default_security_group_allows_intra_ptg(self):
        # Create PTG and retrieve subnet
        ptg = self.create_policy_target_group()['policy_target_group']
        subnets = ptg['subnets']
        req = self.new_show_request('subnets', subnets[0], fmt=self.fmt)
        subnet = self.deserialize(self.fmt,
                                  req.get_response(self.api))['subnet']
        #Create PT and retrieve port
        pt = self.create_policy_target(ptg['id'])['policy_target']
        req = self.new_show_request('ports', pt['port_id'], fmt=self.fmt)
        port = self.deserialize(self.fmt, req.get_response(self.api))['port']

        ip_v = {4: cst.IPv4, 6: cst.IPv6}

        # Verify Port's SG has all the right rules
        # Allow all ingress traffic from same ptg subnet
        filters = {'tenant_id': [ptg['tenant_id']],
                   'security_group_id': [port['security_groups'][0]],
                   'ethertype': [ip_v[subnet['ip_version']]],
                   'remote_ip_prefix': [subnet['cidr']],
                   'direction': ['ingress']}
        sg_rule = self._get_sg_rule(**filters)
        self.assertTrue(len(sg_rule) == 1)
        self.assertIsNone(sg_rule[0]['protocol'])
        self.assertIsNone(sg_rule[0]['port_range_max'])
        self.assertIsNone(sg_rule[0]['port_range_min'])

    def test_default_security_group_allows_intra_ptg_update(self):
        # Create ptg and retrieve subnet and network
        ptg = self.create_policy_target_group()['policy_target_group']
        subnets = ptg['subnets']
        req = self.new_show_request('subnets', subnets[0], fmt=self.fmt)
        subnet1 = self.deserialize(self.fmt,
                                   req.get_response(self.api))['subnet']
        req = self.new_show_request('networks', subnet1['network_id'],
                                    fmt=self.fmt)
        network = self.deserialize(self.fmt,
                                   req.get_response(self.api))
        with self.subnet(network=network, cidr='192.168.0.0/24') as subnet2:
            # Add subnet
            subnet2 = subnet2['subnet']
            subnets = [subnet1['id'], subnet2['id']]
            data = {'policy_target_group': {'subnets': subnets}}
            req = self.new_update_request('policy_target_groups', data,
                                          ptg['id'])
            ptg = self.deserialize(
                self.fmt, req.get_response(
                    self.ext_api))['policy_target_group']
            #Create PT and retrieve port
            pt = self.create_policy_target(ptg['id'])['policy_target']
            req = self.new_show_request('ports', pt['port_id'], fmt=self.fmt)
            port = self.deserialize(self.fmt,
                                    req.get_response(self.api))['port']
            ip_v = {4: cst.IPv4, 6: cst.IPv6}
            # Verify all the expected rules are set
            for subnet in [subnet1, subnet2]:
                filters = {'tenant_id': [ptg['tenant_id']],
                           'security_group_id': [port['security_groups'][0]],
                           'ethertype': [ip_v[subnet['ip_version']]],
                           'remote_ip_prefix': [subnet['cidr']],
                           'direction': ['ingress']}
                sg_rule = self._get_sg_rule(**filters)
                self.assertTrue(len(sg_rule) == 1)
                self.assertIsNone(sg_rule[0]['protocol'])
                self.assertIsNone(sg_rule[0]['port_range_max'])
                self.assertIsNone(sg_rule[0]['port_range_min'])

    def test_shared_ptg_create_negative(self):
        l2p = self.create_l2_policy(shared=True)
        l2p_id = l2p['l2_policy']['id']
        for shared in [True, False]:
            res = self.create_policy_target_group(
                name="ptg1", tenant_id='other', l2_policy_id=l2p_id,
                shared=shared, expected_res_status=400)

            self.assertEqual(
                'CrossTenantPolicyTargetGroupL2PolicyNotSupported',
                res['NeutronError']['type'])

    # TODO(rkukura): Test ip_pool exhaustion.


class TestL2Policy(ResourceMappingTestCase):

    def _test_implicit_network_lifecycle(self, shared=False):
        l3p = self.create_l3_policy(shared=shared)
        # Create L2 policy with implicit network.
        l2p = self.create_l2_policy(name="l2p1",
                                    l3_policy_id=l3p['l3_policy']['id'],
                                    shared=shared)
        l2p_id = l2p['l2_policy']['id']
        network_id = l2p['l2_policy']['network_id']
        self.assertIsNotNone(network_id)
        req = self.new_show_request('networks', network_id, fmt=self.fmt)
        res = self.deserialize(self.fmt, req.get_response(self.api))
        self.assertEqual(shared, res['network']['shared'])

        # Verify deleting L2 policy cleans up network.
        req = self.new_delete_request('l2_policies', l2p_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
        req = self.new_show_request('networks', network_id, fmt=self.fmt)
        res = req.get_response(self.api)
        self.assertEqual(res.status_int, webob.exc.HTTPNotFound.code)

    def _test_explicit_network_lifecycle(self, shared=False):
        # Create L2 policy with explicit network.
        with self.network(shared=shared) as network:
            network_id = network['network']['id']
            l2p = self.create_l2_policy(name="l2p1", network_id=network_id,
                                        shared=shared)
            l2p_id = l2p['l2_policy']['id']
            self.assertEqual(network_id, l2p['l2_policy']['network_id'])

            # Verify deleting L2 policy does not cleanup network.
            req = self.new_delete_request('l2_policies', l2p_id)
            res = req.get_response(self.ext_api)
            self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
            req = self.new_show_request('networks', network_id, fmt=self.fmt)
            res = req.get_response(self.api)
            self.assertEqual(res.status_int, webob.exc.HTTPOk.code)

    def test_implicit_network_lifecycle(self):
        self._test_implicit_network_lifecycle()

    def test_implicit_network_lifecycle_shared(self):
        self._test_implicit_network_lifecycle(True)

    def test_explicit_network_lifecycle(self):
        self._test_explicit_network_lifecycle()

    def test_explicit_network_lifecycle_shared(self):
        self._test_explicit_network_lifecycle(True)

    def test_shared_l2_policy_create_negative(self):
        l3p = self.create_l3_policy(shared=True)
        for shared in [True, False]:
            res = self.create_l2_policy(name="l2p1", tenant_id='other',
                                        l3_policy_id=l3p['l3_policy']['id'],
                                        shared=shared, expected_res_status=400)
            self.assertEqual('CrossTenantL2PolicyL3PolicyNotSupported',
                             res['NeutronError']['type'])

        with self.network() as network:
            network_id = network['network']['id']
            res = self.create_l2_policy(name="l2p1", network_id=network_id,
                                        shared=True, expected_res_status=400)
            self.assertEqual('NonSharedNetworkOnSharedL2PolicyNotSupported',
                             res['NeutronError']['type'])


class TestL3Policy(ResourceMappingTestCase):

    def test_implicit_router_lifecycle(self):
        # Create L3 policy with implicit router.
        l3p = self.create_l3_policy(name="l3p1")
        l3p_id = l3p['l3_policy']['id']
        routers = l3p['l3_policy']['routers']
        self.assertIsNotNone(routers)
        self.assertEqual(len(routers), 1)
        router_id = routers[0]

        # Verify deleting L3 policy cleans up router.
        req = self.new_delete_request('l3_policies', l3p_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
        req = self.new_show_request('routers', router_id, fmt=self.fmt)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNotFound.code)

    def test_explicit_router_lifecycle(self):
        # Create L3 policy with explicit router.
        with self.router() as router:
            router_id = router['router']['id']
            l3p = self.create_l3_policy(name="l3p1", routers=[router_id])
            l3p_id = l3p['l3_policy']['id']
            routers = l3p['l3_policy']['routers']
            self.assertIsNotNone(routers)
            self.assertEqual(len(routers), 1)
            self.assertEqual(router_id, routers[0])

            # Verify deleting L3 policy does not cleanup router.
            req = self.new_delete_request('l3_policies', l3p_id)
            res = req.get_response(self.ext_api)
            self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
            req = self.new_show_request('routers', router_id, fmt=self.fmt)
            res = req.get_response(self.ext_api)
            self.assertEqual(res.status_int, webob.exc.HTTPOk.code)

    def test_multiple_routers_rejected(self):
        # Verify update l3 policy with explicit router rejected.
        with contextlib.nested(self.router(),
                               self.router()) as (router1, router2):
            router1_id = router1['router']['id']
            router2_id = router2['router']['id']
            data = self.create_l3_policy(name="l3p1",
                                         routers=[router1_id, router2_id],
                                         expected_res_status=
                                         webob.exc.HTTPBadRequest.code)
            self.assertEqual('L3PolicyMultipleRoutersNotSupported',
                             data['NeutronError']['type'])

    def test_router_update_rejected(self):
        # Create L3 policy with implicit router.
        l3p = self.create_l3_policy(name="l3p1")
        l3p_id = l3p['l3_policy']['id']

        # Verify update l3 policy with explicit router rejected.
        with self.router() as router:
            router_id = router['router']['id']
            data = {'l3_policy': {'routers': [router_id]}}
            req = self.new_update_request('l3_policies', data, l3p_id)
            data = self.deserialize(self.fmt, req.get_response(self.ext_api))
            self.assertEqual('L3PolicyRoutersUpdateNotSupported',
                             data['NeutronError']['type'])

    def test_overlapping_pools_per_tenant(self):
        # Verify overlaps are ok on different tenant
        ip_pool = '192.168.0.0/16'
        self.create_l3_policy(ip_pool=ip_pool, tenant_id='Tweedledum',
                              expected_res_status=201)
        self.create_l3_policy(ip_pool=ip_pool, tenant_id='Tweedledee',
                              expected_res_status=201)
        # Verify overlap fails on same tenant
        super_ip_pool = '192.160.0.0/8'
        sub_ip_pool = '192.168.10.0/24'
        for ip_pool in sub_ip_pool, super_ip_pool:
            res = self.create_l3_policy(
                ip_pool=ip_pool, tenant_id='Tweedledum',
                expected_res_status=400)
            self.assertEqual('OverlappingIPPoolsInSameTenantNotAllowed',
                             res['NeutronError']['type'])

    def test_create_l3p_es(self):
        # Simple test to verify l3p created with 1-N ES
        with contextlib.nested(
                 self.network(router__external=True),
                 self.network(router__external=True)) as (net1, net2):
            with contextlib.nested(
                    self.subnet(cidr='10.10.1.0/24', network=net1),
                    self.subnet(cidr='10.10.2.0/24', network=net2)) as (
                    sub1, sub2):
                es1 = self.create_external_segment(
                    subnet_id=sub1['subnet']['id'])['external_segment']
                es2 = self.create_external_segment(
                    subnet_id=sub2['subnet']['id'])['external_segment']
                external_segments = {es1['id']: []}
                l3p = self.create_l3_policy(
                    ip_pool='192.168.0.0/16', expected_res_status=201,
                    external_segments=external_segments)
                req = self.new_delete_request('l3_policies',
                                              l3p['l3_policy']['id'])
                req.get_response(self.ext_api)
                external_segments.update({es2['id']: []})
                res = self.create_l3_policy(
                    ip_pool='192.168.0.0/16', expected_res_status=400,
                    external_segments=external_segments)
                self.assertEqual('MultipleESPerL3PolicyNotSupported',
                                 res['NeutronError']['type'])

    def test_update_l3p_es(self):
        # Simple test to verify l3p updated with 1-N ES
        with contextlib.nested(
                 self.network(router__external=True),
                 self.network(router__external=True)) as (net1, net2):
            with contextlib.nested(
                    self.subnet(cidr='10.10.1.0/24', network=net1),
                    self.subnet(cidr='10.10.2.0/24', network=net2)) as (
                    sub1, sub2):
                es1 = self.create_external_segment(
                    subnet_id=sub1['subnet']['id'])['external_segment']
                es2 = self.create_external_segment(
                    subnet_id=sub2['subnet']['id'])['external_segment']
                # None to es1, es1 to es2
                l3p = self.create_l3_policy(
                    ip_pool='192.168.0.0/16')['l3_policy']
                for external_segments in [{es1['id']: []}, {es2['id']: []}]:
                    self._update_gbp_resource(
                        l3p['id'], 'l3_policy', 'l3_policies',
                        expected_res_status=200,
                        external_segments=external_segments)
                # es2 to [es1, es2]
                external_segments = {es2['id']: [], es1['id']: []}
                res = self._update_gbp_resource_full_response(
                    l3p['id'], 'l3_policy', 'l3_policies',
                    expected_res_status=400,
                    external_segments=external_segments)
                self.assertEqual('MultipleESPerL3PolicyNotSupported',
                                 res['NeutronError']['type'])

    def test_es_router_plumbing(self):
        with contextlib.nested(
                 self.network(router__external=True),
                 self.network(router__external=True)) as (net1, net2):
            with contextlib.nested(
                    self.subnet(cidr='10.10.1.0/24', network=net1),
                    self.subnet(cidr='10.10.2.0/24', network=net2)) as (
                    subnet1, subnet2):
                subnet1 = subnet1['subnet']
                subnet2 = subnet2['subnet']
                es1 = self.create_external_segment(
                    subnet_id=subnet1['id'])['external_segment']
                es2 = self.create_external_segment(
                    subnet_id=subnet2['id'])['external_segment']
                es_dict = {es1['id']: ['10.10.1.3']}
                l3p = self.create_l3_policy(
                    ip_pool='192.168.0.0/16',
                    external_segments=es_dict)['l3_policy']
                req = self.new_show_request('routers', l3p['routers'][0],
                                            fmt=self.fmt)
                res = self.deserialize(self.fmt, req.get_response(
                    self.ext_api))['router']
                self.assertEqual(
                    subnet1['network_id'],
                    res['external_gateway_info']['network_id'])
                # Verify auto assigned addresses propagated to L3P
                es_dict = {es2['id']: []}
                l3p = self._update_gbp_resource(
                    l3p['id'], 'l3_policy', 'l3_policies',
                    external_segments=es_dict, expected_res_status=200)
                req = self.new_show_request('routers', l3p['routers'][0],
                                            fmt=self.fmt)
                res = self.deserialize(self.fmt, req.get_response(
                    self.ext_api))['router']
                self.assertEqual(
                    subnet2['network_id'],
                    res['external_gateway_info']['network_id'])
                self.assertEqual(
                    [x['ip_address'] for x in
                     res['external_gateway_info']['external_fixed_ips']],
                    l3p['external_segments'][es2['id']])
                # Verify that the implicit assignment is persisted
                req = self.new_show_request('l3_policies', l3p['id'],
                                            fmt=self.fmt)
                l3p = self.deserialize(self.fmt, req.get_response(
                    self.ext_api))['l3_policy']
                self.assertEqual(
                    [x['ip_address'] for x in
                     res['external_gateway_info']['external_fixed_ips']],
                    l3p['external_segments'][es2['id']])

    def test_es_routes(self):
        routes1 = [{'destination': '0.0.0.0/0', 'nexthop': '10.10.1.1'},
                   {'destination': '172.0.0.0/16', 'nexthop': '10.10.1.1'}]
        routes2 = [{'destination': '0.0.0.0/0', 'nexthop': '10.10.2.1'},
                   {'destination': '172.0.0.0/16', 'nexthop': '10.10.2.1'}]
        with contextlib.nested(
                 self.network(router__external=True),
                 self.network(router__external=True)) as (net1, net2):
            with contextlib.nested(
                    self.subnet(cidr='10.10.1.0/24', network=net1),
                    self.subnet(cidr='10.10.2.0/24', network=net2)) as (
                    sub1, sub2):
                es1 = self.create_external_segment(
                    cidr='10.10.1.0/24',
                    subnet_id=sub1['subnet']['id'],
                    external_routes=routes1)['external_segment']
                es2 = self.create_external_segment(
                    cidr='10.10.2.0/24',
                    subnet_id=sub2['subnet']['id'],
                    external_routes=routes2)['external_segment']
                es_dict = {es1['id']: []}
                l3p = self.create_l3_policy(
                    ip_pool='192.168.0.0/16', external_segments=es_dict,
                    expected_res_status=201)['l3_policy']
                req = self.new_show_request('routers', l3p['routers'][0],
                                            fmt=self.fmt)
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                self.assertEqual(routes1, res['router']['routes'])
                es_dict = {es2['id']: []}
                self._update_gbp_resource(
                    l3p['id'], 'l3_policy', 'l3_policies',
                    external_segments=es_dict, expected_res_status=200)
                req = self.new_show_request('routers', l3p['routers'][0],
                                            fmt=self.fmt)
                res = self.deserialize(self.fmt,
                                       req.get_response(self.ext_api))
                self.assertEqual(routes2, res['router']['routes'])


class NotificationTest(ResourceMappingTestCase):

    def test_dhcp_notifier(self):
        with mock.patch.object(dhcp_rpc_agent_api.DhcpAgentNotifyAPI,
                               'notify') as dhcp_notifier:
            ptg = self.create_policy_target_group(name="ptg1")
            ptg_id = ptg['policy_target_group']['id']
            pt = self.create_policy_target(
                name="pt1", policy_target_group_id=ptg_id)
            self.assertEqual(
                pt['policy_target']['policy_target_group_id'], ptg_id)
            # REVISIT(rkukura): Check dictionaries for correct id, etc..
            dhcp_notifier.assert_any_call(mock.ANY, mock.ANY,
                                          "router.create.end")
            dhcp_notifier.assert_any_call(mock.ANY, mock.ANY,
                                          "network.create.end")
            dhcp_notifier.assert_any_call(mock.ANY, mock.ANY,
                                          "subnet.create.end")
            dhcp_notifier.assert_any_call(mock.ANY, mock.ANY,
                                          "port.create.end")

    def test_nova_notifier(self):
        with mock.patch.object(nova.Notifier,
                               'send_network_change') as nova_notifier:
            ptg = self.create_policy_target_group(name="ptg1")
            ptg_id = ptg['policy_target_group']['id']
            pt = self.create_policy_target(
                name="pt1", policy_target_group_id=ptg_id)
            self.assertEqual(
                pt['policy_target']['policy_target_group_id'], ptg_id)
            # REVISIT(rkukura): Check dictionaries for correct id, etc..
            nova_notifier.assert_any_call("create_router", {}, mock.ANY)
            nova_notifier.assert_any_call("create_network", {}, mock.ANY)
            nova_notifier.assert_any_call("create_subnet", {}, mock.ANY)
            nova_notifier.assert_any_call("create_port", {}, mock.ANY)


# TODO(ivar): We need a UT that verifies that the PT's ports have the default
# SG when there are no policy_rule_sets involved, that the default SG is
# properly # created and shared, and that it has the right content.
class TestPolicyRuleSet(ResourceMappingTestCase):

    def _get_sg(self, sg_id):
        plugin, context = self.get_plugin_context()
        return plugin.get_security_group(context, sg_id)

    def test_policy_rule_set_creation(self):
        # Create policy_rule_sets
        classifier = self.create_policy_classifier(
            name="class1", protocol="tcp", direction="out",
            port_range="50:100")
        classifier_id = classifier['policy_classifier']['id']
        action = self.create_policy_action(name="action1")
        action_id = action['policy_action']['id']
        action_id_list = [action_id]
        policy_rule = self.create_policy_rule(
            name='pr1', policy_classifier_id=classifier_id,
            policy_actions=action_id_list)
        policy_rule_id = policy_rule['policy_rule']['id']
        policy_rule_list = [policy_rule_id]
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=policy_rule_list)
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        ptg = self.create_policy_target_group(
            name="ptg1", provided_policy_rule_sets={
                policy_rule_set_id: None})
        ptg_id = ptg['policy_target_group']['id']
        pt = self.create_policy_target(
            name="pt1", policy_target_group_id=ptg_id)

        # verify SG bind to port
        port_id = pt['policy_target']['port_id']
        res = self.new_show_request('ports', port_id)
        port = self.deserialize(self.fmt, res.get_response(self.api))
        security_groups = port['port'][ext_sg.SECURITYGROUPS]
        self.assertEqual(len(security_groups), 2)

    # TODO(ivar): we also need to verify that those security groups have the
    # right rules
    def test_consumed_policy_rule_set(self):
        classifier = self.create_policy_classifier(
            name="class1", protocol="tcp", direction="in", port_range="20:90")
        classifier_id = classifier['policy_classifier']['id']
        action = self.create_policy_action(name="action1",
                                           action_type=gconst.GP_ACTION_ALLOW)
        action_id = action['policy_action']['id']
        action_id_list = [action_id]
        policy_rule = self.create_policy_rule(
            name='pr1', policy_classifier_id=classifier_id,
            policy_actions=action_id_list)
        policy_rule_id = policy_rule['policy_rule']['id']
        policy_rule_list = [policy_rule_id]
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=policy_rule_list)
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        self.create_policy_target_group(
            name="ptg1", provided_policy_rule_sets={policy_rule_set_id: None})
        consumed_ptg = self.create_policy_target_group(
            name="ptg2", consumed_policy_rule_sets={policy_rule_set_id: None})
        ptg_id = consumed_ptg['policy_target_group']['id']
        pt = self.create_policy_target(
            name="pt2", policy_target_group_id=ptg_id)

        # verify SG bind to port
        port_id = pt['policy_target']['port_id']
        res = self.new_show_request('ports', port_id)
        port = self.deserialize(self.fmt, res.get_response(self.api))
        security_groups = port['port'][ext_sg.SECURITYGROUPS]
        self.assertEqual(len(security_groups), 2)

    # Test update and delete of PTG, how it affects SG mapping
    def test_policy_target_group_update(self):
        # create two policy_rule_sets: bind one to an PTG, update with
        # adding another one (increase SG count on PT on PTG)
        classifier1 = self.create_policy_classifier(
            name="class1", protocol="tcp", direction="bi", port_range="30:80")
        classifier2 = self.create_policy_classifier(
            name="class2", protocol="udp", direction="out", port_range="20:30")
        classifier1_id = classifier1['policy_classifier']['id']
        classifier2_id = classifier2['policy_classifier']['id']
        action = self.create_policy_action(name="action1",
                                           action_type=gconst.GP_ACTION_ALLOW)
        action_id = action['policy_action']['id']
        action_id_list = [action_id]
        policy_rule1 = self.create_policy_rule(
            name='pr1', policy_classifier_id=classifier1_id,
            policy_actions=action_id_list)
        policy_rule2 = self.create_policy_rule(
            name='pr2', policy_classifier_id=classifier2_id,
            policy_actions=action_id_list)
        policy_rule1_id = policy_rule1['policy_rule']['id']
        policy_rule1_list = [policy_rule1_id]
        policy_rule2_id = policy_rule2['policy_rule']['id']
        policy_rule2_list = [policy_rule2_id]
        policy_rule_set1 = self.create_policy_rule_set(
            name="c1", policy_rules=policy_rule1_list)
        policy_rule_set1_id = policy_rule_set1['policy_rule_set']['id']
        policy_rule_set2 = self.create_policy_rule_set(
            name="c2", policy_rules=policy_rule2_list)
        policy_rule_set2_id = policy_rule_set2['policy_rule_set']['id']
        ptg1 = self.create_policy_target_group(
            name="ptg1", provided_policy_rule_sets={policy_rule_set1_id: None})
        ptg2 = self.create_policy_target_group(
            name="ptg2", consumed_policy_rule_sets={policy_rule_set1_id: None})
        ptg1_id = ptg1['policy_target_group']['id']
        ptg2_id = ptg2['policy_target_group']['id']

        # policy_target pt1 now with ptg2 consumes policy_rule_set1_id
        pt1 = self.create_policy_target(
            name="pt1", policy_target_group_id=ptg2_id)
        # pt1's port should have 2 SG associated
        port_id = pt1['policy_target']['port_id']
        res = self.new_show_request('ports', port_id)
        port = self.deserialize(self.fmt, res.get_response(self.api))
        security_groups = port['port'][ext_sg.SECURITYGROUPS]
        self.assertEqual(len(security_groups), 2)

        # now add a policy_rule_set to PTG
        # First we update policy_rule_set2 to be provided by consumed_ptg
        data = {'policy_target_group':
                {'provided_policy_rule_sets': {policy_rule_set2_id: None}}}
        req = self.new_update_request('policy_target_groups', data, ptg2_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPOk.code)
        # set ptg1 to provide policy_rule_set1 and consume policy_rule_set2
        # policy_rule_set2 now maps to SG which has ptg2's subnet as CIDR on
        # rules
        data = {'policy_target_group':
                {'consumed_policy_rule_sets': {policy_rule_set2_id: None}}}
        req = self.new_update_request('policy_target_groups', data, ptg1_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPOk.code)
        port_id = pt1['policy_target']['port_id']
        res = self.new_show_request('ports', port_id)
        port = self.deserialize(self.fmt, res.get_response(self.api))
        security_groups = port['port'][ext_sg.SECURITYGROUPS]
        self.assertEqual(len(security_groups), 3)

    # Test update of policy rules
    def test_policy_rule_update(self):
        classifier1 = self.create_policy_classifier(
            name="class1", protocol="tcp", direction="bi", port_range="50:100")
        classifier2 = self.create_policy_classifier(
            name="class2", protocol="udp", direction="out",
            port_range="30:100")
        classifier1_id = classifier1['policy_classifier']['id']
        classifier2_id = classifier2['policy_classifier']['id']
        action = self.create_policy_action(name="action1",
                                           action_type=gconst.GP_ACTION_ALLOW)
        action_id = action['policy_action']['id']
        action_id_list = [action_id]
        policy_rule = self.create_policy_rule(
            name='pr1', policy_classifier_id=classifier1_id,
            policy_actions=action_id_list)
        policy_rule_id = policy_rule['policy_rule']['id']
        policy_rule_list = [policy_rule_id]
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=policy_rule_list)
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        ptg = self.create_policy_target_group(
            name="ptg1", provided_policy_rule_sets={policy_rule_set_id: None})
        ptg_id = ptg['policy_target_group']['id']
        pt = self.create_policy_target(
            name="pt1", policy_target_group_id=ptg_id)

        # now updates the policy rule with new classifier
        data = {'policy_rule':
                {'policy_classifier_id': classifier2_id}}
        req = self.new_update_request('policy_rules', data, policy_rule_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPOk.code)
        port_id = pt['policy_target']['port_id']
        res = self.new_show_request('ports', port_id)
        port = self.deserialize(self.fmt, res.get_response(self.api))
        security_groups = port['port']['security_groups']
        udp_rules = []
        for sgid in security_groups:
            sg = self._get_sg(sgid)
            sg_rules = sg['security_group_rules']
            udp_rules.extend([r for r in sg_rules if r['protocol'] == 'udp'])

        self.assertEqual(len(udp_rules), 1)
        udp_rule = udp_rules[0]
        self.assertEqual(udp_rule['port_range_min'], 30)
        self.assertEqual(udp_rule['port_range_max'], 100)

    # Test update of policy classifier
    def test_policy_classifier_update(self):
        classifier = self.create_policy_classifier(
            name="class1", protocol="tcp", direction="bi", port_range="30:100")
        classifier_id = classifier['policy_classifier']['id']
        action = self.create_policy_action(name="action1",
                                           action_type=gconst.GP_ACTION_ALLOW)
        action_id = action['policy_action']['id']
        action_id_list = [action_id]
        policy_rule = self.create_policy_rule(
            name='pr1', policy_classifier_id=classifier_id,
            policy_actions=action_id_list)
        policy_rule_id = policy_rule['policy_rule']['id']
        policy_rule_list = [policy_rule_id]
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=policy_rule_list)
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        ptg = self.create_policy_target_group(
            name="ptg1", provided_policy_rule_sets={policy_rule_set_id: None})
        ptg_id = ptg['policy_target_group']['id']
        pt = self.create_policy_target(
            name="pt1", policy_target_group_id=ptg_id)

        # now updates the policy classifier with new protocol field
        data = {'policy_classifier':
                {'protocol': 'udp', 'port_range': '50:150'}}
        req = self.new_update_request('policy_classifiers', data,
            classifier_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPOk.code)
        port_id = pt['policy_target']['port_id']
        res = self.new_show_request('ports', port_id)
        port = self.deserialize(self.fmt, res.get_response(self.api))
        security_groups = port['port']['security_groups']
        udp_rules = []
        for sgid in security_groups:
            sg = self._get_sg(sgid)
            sg_rules = sg['security_group_rules']
            udp_rules.extend([r for r in sg_rules if r['protocol'] == 'udp'])

        self.assertEqual(len(udp_rules), 1)
        udp_rule = udp_rules[0]
        self.assertEqual(udp_rule['port_range_min'], 50)
        self.assertEqual(udp_rule['port_range_max'], 150)

    def test_redirect_to_chain(self):
        data = {'servicechain_node': {'service_type': "LOADBALANCER",
                                      'tenant_id': self._tenant_id,
                                      'config': "{}"}}
        scn_req = self.new_create_request(SERVICECHAIN_NODES, data, self.fmt)
        node = self.deserialize(self.fmt, scn_req.get_response(self.ext_api))
        scn_id = node['servicechain_node']['id']
        data = {'servicechain_spec': {'tenant_id': self._tenant_id,
                                      'nodes': [scn_id]}}
        scs_req = self.new_create_request(SERVICECHAIN_SPECS, data, self.fmt)
        spec = self.deserialize(self.fmt, scs_req.get_response(self.ext_api))
        scs_id = spec['servicechain_spec']['id']
        classifier = self.create_policy_classifier(
            name="class1", protocol="tcp", direction="in", port_range="20:90")
        classifier_id = classifier['policy_classifier']['id']
        action = self.create_policy_action(
            name="action1", action_type=gconst.GP_ACTION_REDIRECT,
            action_value=scs_id)
        action_id = action['policy_action']['id']
        action_id_list = [action_id]
        policy_rule = self.create_policy_rule(
            name='pr1', policy_classifier_id=classifier_id,
            policy_actions=action_id_list)
        policy_rule_id = policy_rule['policy_rule']['id']
        policy_rule_list = [policy_rule_id]
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=policy_rule_list)
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        provider_ptg = self.create_policy_target_group(
            name="ptg1", provided_policy_rule_sets={policy_rule_set_id: None})
        provider_ptg_id = provider_ptg['policy_target_group']['id']
        consumer_ptg = self.create_policy_target_group(
            name="ptg2",
            consumed_policy_rule_sets={policy_rule_set_id: None})
        consumer_ptg_id = consumer_ptg['policy_target_group']['id']
        sc_node_list_req = self.new_list_request(SERVICECHAIN_INSTANCES)
        res = sc_node_list_req.get_response(self.ext_api)
        sc_instances = self.deserialize(self.fmt, res)
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]
        self.assertEqual(sc_instance['provider_ptg_id'], provider_ptg_id)
        self.assertEqual(sc_instance['consumer_ptg_id'], consumer_ptg_id)
        req = self.new_delete_request(
            'policy_target_groups', consumer_ptg_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
        sc_node_list_req = self.new_list_request(SERVICECHAIN_INSTANCES)
        res = sc_node_list_req.get_response(self.ext_api)
        sc_instances = self.deserialize(self.fmt, res)
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

    def test_action_spec_value_update(self):
        data = {'servicechain_node': {'service_type': "LOADBALANCER",
                                      'tenant_id': self._tenant_id,
                                      'config': "{}"}}
        scn_req = self.new_create_request(SERVICECHAIN_NODES, data, self.fmt)
        node = self.deserialize(self.fmt, scn_req.get_response(self.ext_api))
        scn_id = node['servicechain_node']['id']
        data = {'servicechain_spec': {'tenant_id': self._tenant_id,
                                      'nodes': [scn_id]}}
        scs_req = self.new_create_request(SERVICECHAIN_SPECS, data, self.fmt)
        spec = self.deserialize(self.fmt, scs_req.get_response(self.ext_api))
        scs_id = spec['servicechain_spec']['id']
        classifier = self.create_policy_classifier(
            name="class1", protocol="tcp", direction="in", port_range="20:90")
        classifier_id = classifier['policy_classifier']['id']
        action = self.create_policy_action(
            name="action1", action_type=gconst.GP_ACTION_REDIRECT,
            action_value=scs_id)
        action_id = action['policy_action']['id']
        action_id_list = [action_id]
        policy_rule = self.create_policy_rule(
            name='pr1', policy_classifier_id=classifier_id,
            policy_actions=action_id_list)
        policy_rule_id = policy_rule['policy_rule']['id']
        policy_rule_list = [policy_rule_id]
        policy_rule_set = self.create_policy_rule_set(
            name="c1", policy_rules=policy_rule_list)
        policy_rule_set_id = policy_rule_set['policy_rule_set']['id']
        provider_ptg = self.create_policy_target_group(
            name="ptg1", provided_policy_rule_sets={policy_rule_set_id: None})
        provider_ptg_id = provider_ptg['policy_target_group']['id']
        consumer_ptg = self.create_policy_target_group(
            name="ptg2",
            consumed_policy_rule_sets={policy_rule_set_id: None})
        consumer_ptg_id = consumer_ptg['policy_target_group']['id']
        sc_instance_list_req = self.new_list_request(SERVICECHAIN_INSTANCES)
        res = sc_instance_list_req.get_response(self.ext_api)
        sc_instances = self.deserialize(self.fmt, res)
        # We should have one service chain instance created now
        self.assertEqual(len(sc_instances['servicechain_instances']), 1)
        sc_instance = sc_instances['servicechain_instances'][0]
        self.assertEqual(sc_instance['provider_ptg_id'], provider_ptg_id)
        self.assertEqual(sc_instance['consumer_ptg_id'], consumer_ptg_id)
        self.assertEqual(scs_id, sc_instance['servicechain_spec'])

        data = {'servicechain_node': {'service_type': "FIREWALL",
                                      'tenant_id': self._tenant_id,
                                      'config': "{}"}}
        scn_req = self.new_create_request(SERVICECHAIN_NODES, data, self.fmt)
        new_node = self.deserialize(
                    self.fmt, scn_req.get_response(self.ext_api))
        new_scn_id = new_node['servicechain_node']['id']
        data = {'servicechain_spec': {'tenant_id': self._tenant_id,
                                      'nodes': [new_scn_id]}}
        scs_req = self.new_create_request(SERVICECHAIN_SPECS, data, self.fmt)
        new_spec = self.deserialize(
                    self.fmt, scs_req.get_response(self.ext_api))
        new_scs_id = new_spec['servicechain_spec']['id']
        action = {'policy_action': {'action_value': new_scs_id}}
        req = self.new_update_request('policy_actions', action, action_id)
        action = self.deserialize(self.fmt,
                                  req.get_response(self.ext_api))

        sc_instance_list_req = self.new_list_request(SERVICECHAIN_INSTANCES)
        res = sc_instance_list_req.get_response(self.ext_api)
        new_sc_instances = self.deserialize(self.fmt, res)
        # We should have one service chain instance created now
        self.assertEqual(len(new_sc_instances['servicechain_instances']), 1)
        new_sc_instance = new_sc_instances['servicechain_instances'][0]
        self.assertEqual(sc_instance['id'], new_sc_instance['id'])
        self.assertEqual(new_scs_id, new_sc_instance['servicechain_spec'])

        req = self.new_delete_request(
                'policy_target_groups', consumer_ptg_id)
        res = req.get_response(self.ext_api)
        self.assertEqual(res.status_int, webob.exc.HTTPNoContent.code)
        sc_instance_list_req = self.new_list_request(SERVICECHAIN_INSTANCES)
        res = sc_instance_list_req.get_response(self.ext_api)
        sc_instances = self.deserialize(self.fmt, res)
        self.assertEqual(len(sc_instances['servicechain_instances']), 0)

    def test_shared_policy_rule_set_create_negative(self):
        self.create_policy_rule_set(shared=True, expected_res_status=400)

    def test_external_rules_set(self):
        # Define the routes
        routes = [{'destination': '0.0.0.0/0', 'nexthop': None}]
        pr = self._create_ssh_allow_rule()
        prs = self.create_policy_rule_set(
            policy_rules=[pr['id']])['policy_rule_set']
        with self.network(router__external=True) as net:
            with self.subnet(cidr='10.10.1.0/24', network=net) as sub:
                es = self.create_external_segment(
                    subnet_id=sub['subnet']['id'],
                    external_routes=routes)['external_segment']
                l3p = self.create_l3_policy(
                    ip_pool='192.168.0.0/16',
                    external_segments={es['id']: []})['l3_policy']
                self.create_external_policy(
                    external_segments=[es['id']],
                    provided_policy_rule_sets={prs['id']: ''})
                expected_cidrs = [str(x) for x in (
                    netaddr.IPSet(['0.0.0.0/0']) -
                    netaddr.IPSet([l3p['ip_pool']])).iter_cidrs()]
                mapping = self._get_prs_mapping(prs['id'])
                # Since EP provides, the consumed SG will have ingress rules
                # based on the difference between the L3P and the external
                # world
                attrs = {'security_group_id': [mapping.consumed_sg_id],
                         'direction': ['ingress'],
                         'protocol': ['tcp'],
                         'port_range_min': [22],
                         'port_range_max': [22],
                         'remote_ip_prefix': None}
                for cidr in expected_cidrs:
                    attrs['remote_ip_prefix'] = [cidr]
                    self.assertTrue(self._get_sg_rule(**attrs))

                # Add one rule to the PRS
                pr2 = self._create_http_allow_rule()
                self._update_gbp_resource(
                    prs['id'], 'policy_rule_set', 'policy_rule_sets',
                    expected_res_status=200,
                    policy_rules=[pr['id'], pr2['id']])
                # Verify new rules correctly set
                attrs = {'security_group_id': [mapping.consumed_sg_id],
                         'direction': ['ingress'],
                         'protocol': ['tcp'],
                         'port_range_min': [80],
                         'port_range_max': [80],
                         'remote_ip_prefix': None}
                for cidr in expected_cidrs:
                    attrs['remote_ip_prefix'] = [cidr]
                    self.assertTrue(self._get_sg_rule(**attrs))
                # Remove all the rules, verify that none exist any more
                self._update_gbp_resource(prs['id'], 'policy_rule_set',
                                          'policy_rule_sets',
                                          expected_res_status=200,
                                          policy_rules=[])
                attrs = {'security_group_id': [mapping.consumed_sg_id],
                         'direction': ['ingress'],
                         'protocol': ['tcp'],
                         'port_range_min': [80, 22],
                         'port_range_max': [80, 22],
                         'remote_ip_prefix': expected_cidrs}
                self.assertFalse(self._get_sg_rule(**attrs))


class TestExternalSegment(ResourceMappingTestCase):

    def test_implicit_subnet_rejected(self):
        res = self.create_external_segment(expected_res_status=400)
        self.assertEqual('ImplicitSubnetNotSupported',
                         res['NeutronError']['type'])

    def test_explicit_subnet_lifecycle(self):

        with self.network(router__external=True) as net:
            with self.subnet(cidr='10.10.1.0/24', network=net) as sub:
                es = self.create_external_segment(
                    subnet_id=sub['subnet']['id'])['external_segment']
                subnet_id = es['subnet_id']
                self.assertIsNotNone(subnet_id)
                res = self.new_show_request('subnets', subnet_id)
                subnet = self.deserialize(self.fmt, res.get_response(self.api))

                self.assertEqual(subnet['subnet']['cidr'], es['cidr'])
                self.assertEqual(subnet['subnet']['ip_version'],
                                 es['ip_version'])

    def test_update(self):
        with self.network(router__external=True) as net:
            with self.subnet(cidr='10.10.1.0/24', network=net) as sub:
                changes = {'port_address_translation': True}
                es = self.create_external_segment(
                    subnet_id=sub['subnet']['id'])['external_segment']
                for k, v in changes.iteritems():
                    res = self._update_gbp_resource_full_response(
                        es['id'], 'external_segment',
                        'external_segments', expected_res_status=400, **{k: v})
                    self.assertEqual('InvalidAttributeUpdateForES',
                                     res['NeutronError']['type'])
                # Verify route updated correctly
                route = {'destination': '0.0.0.0/0', 'nexthop': None}
                self._update_gbp_resource(
                    es['id'], 'external_segment', 'external_segments',
                    expected_res_status=200, external_routes=[route])
                pr = self._create_ssh_allow_rule()
                prs = self.create_policy_rule_set(
                    policy_rules=[pr['id']])['policy_rule_set']
                l3p1 = self.create_l3_policy(
                    ip_pool='192.168.0.0/16')['l3_policy']
                l3p2 = self.create_l3_policy(
                    ip_pool='192.128.0.0/16')['l3_policy']
                self.create_external_policy(
                    external_segments=[es['id']],
                    provided_policy_rule_sets={prs['id']: ''})
                expected_cidrs = self._calculate_expected_external_cidrs(
                    es, [l3p1, l3p2])
                mapping = self._get_prs_mapping(prs['id'])
                attrs = {'security_group_id': [mapping.consumed_sg_id],
                         'direction': ['ingress'],
                         'protocol': ['tcp'],
                         'port_range_min': [22],
                         'port_range_max': [22],
                         'remote_ip_prefix': None}
                for cidr in expected_cidrs:
                    attrs['remote_ip_prefix'] = [cidr]
                    self.assertTrue(self._get_sg_rule(**attrs))
                # Update the route and verify the SG rules changed
                route = {'destination': '172.0.0.0/8', 'nexthop': None}
                es = self._update_gbp_resource(
                    es['id'], 'external_segment', 'external_segments',
                    expected_res_status=200, external_routes=[route])

                # Verify the old rules have been deleted
                new_cidrs = self._calculate_expected_external_cidrs(
                    es, [l3p1, l3p2])
                removed = set(expected_cidrs) - set(new_cidrs)
                for cidr in removed:
                    attrs['remote_ip_prefix'] = [cidr]
                    self.assertFalse(self._get_sg_rule(**attrs))

                expected_cidrs = new_cidrs
                # Verify new rules exist
                for cidr in expected_cidrs:
                    attrs['remote_ip_prefix'] = [cidr]
                    self.assertTrue(self._get_sg_rule(**attrs))

                # Creating a new L3P changes the definition of what's external
                # and what is not
                l3p3 = self.create_l3_policy(
                    ip_pool='192.64.0.0/16')['l3_policy']
                new_cidrs = self._calculate_expected_external_cidrs(es,
                                                           [l3p1, l3p2, l3p3])

                # Verify removed rules
                removed = set(expected_cidrs) - set(new_cidrs)
                for cidr in removed:
                    attrs['remote_ip_prefix'] = [cidr]
                    self.assertFalse(self._get_sg_rule(**attrs))

                expected_cidrs = new_cidrs
                # Verify new rules exist
                for cidr in expected_cidrs:
                    attrs['remote_ip_prefix'] = [cidr]
                    self.assertTrue(self._get_sg_rule(**attrs))

    def test_implicit_es(self):
        with self.network(router__external=True) as net:
            with self.subnet(cidr='192.168.0.0/24', network=net) as sub:
                es = self.create_external_segment(
                    name="default",
                    subnet_id=sub['subnet']['id'])['external_segment']
                l3p = self.create_l3_policy()['l3_policy']
                self.assertEqual(es['id'], l3p['external_segments'].keys()[0])


class TestExternalPolicy(ResourceMappingTestCase):

    def test_create(self):
        with self.network(router__external=True) as net:
            with contextlib.nested(
                    self.subnet(cidr='10.10.1.0/24', network=net),
                    self.subnet(cidr='10.10.2.0/24', network=net)) as (
                    sub1, sub2):
                es1 = self.create_external_segment(
                    subnet_id=sub1['subnet']['id'],
                    shared=True)['external_segment']
                es2 = self.create_external_segment(
                    subnet_id=sub2['subnet']['id'])['external_segment']
                # Shared Rejected
                res = self.create_external_policy(
                    expected_res_status=400, external_segments=[es1['id']],
                    shared=True)
                self.assertEqual('InvalidSharedResource',
                                 res['NeutronError']['type'])
                # Multiple ES reject
                res = self.create_external_policy(
                    expected_res_status=400,
                    external_segments=[es1['id'], es2['id']])
                self.assertEqual('MultipleESPerEPNotSupported',
                                 res['NeutronError']['type'])
                # No ES reject
                res = self.create_external_policy(
                    expected_res_status=400, external_segments=[])
                self.assertEqual('ESIdRequiredWhenCreatingEP',
                                 res['NeutronError']['type'])

                # Multiple EP per tenant rejected
                self.create_external_policy(external_segments=[es1['id']],
                                            expected_res_status=201)
                res = self.create_external_policy(
                    expected_res_status=400, external_segments=[es2['id']])
                self.assertEqual('OnlyOneEPPerTenantAllowed',
                                 res['NeutronError']['type'])

    def test_update(self):
        with self.network(router__external=True) as net:
            with contextlib.nested(
                    self.subnet(cidr='10.10.1.0/24', network=net),
                    self.subnet(cidr='10.10.2.0/24', network=net)) as (
                    sub1, sub2):
                route = {'destination': '172.0.0.0/8', 'nexthop': None}
                es1 = self.create_external_segment(
                    subnet_id=sub1['subnet']['id'],
                    external_routes=[route])['external_segment']
                es2 = self.create_external_segment(
                    subnet_id=sub2['subnet']['id'])['external_segment']
                ep = self.create_external_policy(
                    external_segments=[es1['id']], expected_res_status=201)
                ep = ep['external_policy']
                # ES update rejectes
                res = self._update_gbp_resource_full_response(
                    ep['id'], 'external_policy', 'external_policies',
                    external_segments=[es2['id']], expected_res_status=400)
                self.assertEqual('ESUpdateNotSupportedForEP',
                                 res['NeutronError']['type'])
                # Rules changed when changing PRS
                pr_ssh = self._create_ssh_allow_rule()
                pr_http = self._create_http_allow_rule()

                prs_ssh = self.create_policy_rule_set(
                    policy_rules=[pr_ssh['id']])['policy_rule_set']
                prs_http = self.create_policy_rule_set(
                    policy_rules=[pr_http['id']])['policy_rule_set']

                self._update_gbp_resource(
                    ep['id'], 'external_policy', 'external_policies',
                    provided_policy_rule_sets={prs_ssh['id']: ''},
                    consumed_policy_rule_sets={prs_ssh['id']: ''},
                    expected_res_status=200)

                expected_cidrs = self._calculate_expected_external_cidrs(
                    es1, [])
                self.assertTrue(len(expected_cidrs) > 0)
                mapping = self._get_prs_mapping(prs_ssh['id'])
                ssh_attrs = {'security_group_id': None,
                             'direction': ['ingress'],
                             'protocol': ['tcp'],
                             'port_range_min': [22],
                             'port_range_max': [22],
                             'remote_ip_prefix': None}
                for sg in [mapping.consumed_sg_id, mapping.provided_sg_id]:
                    for cidr in expected_cidrs:
                        ssh_attrs['security_group_id'] = [sg]
                        ssh_attrs['remote_ip_prefix'] = [cidr]
                        self.assertTrue(self._get_sg_rule(**ssh_attrs))

                # Now swap the contract
                self._update_gbp_resource(
                    ep['id'], 'external_policy', 'external_policies',
                    provided_policy_rule_sets={prs_http['id']: ''},
                    consumed_policy_rule_sets={prs_http['id']: ''},
                    expected_res_status=200)

                # SSH rules removed
                for sg in [mapping.consumed_sg_id, mapping.provided_sg_id]:
                    for cidr in expected_cidrs:
                        ssh_attrs['security_group_id'] = [sg]
                        ssh_attrs['remote_ip_prefix'] = [cidr]
                        self.assertFalse(self._get_sg_rule(**ssh_attrs))

                # HTTP Added
                mapping = self._get_prs_mapping(prs_http['id'])
                http_attrs = {'security_group_id': None,
                              'direction': ['ingress'],
                              'protocol': ['tcp'],
                              'port_range_min': [80],
                              'port_range_max': [80],
                              'remote_ip_prefix': None}
                for sg in [mapping.consumed_sg_id, mapping.provided_sg_id]:
                    for cidr in expected_cidrs:
                        http_attrs['security_group_id'] = [sg]
                        http_attrs['remote_ip_prefix'] = [cidr]
                        self.assertTrue(self._get_sg_rule(**http_attrs))

                # All removed
                self._update_gbp_resource(
                    ep['id'], 'external_policy', 'external_policies',
                    provided_policy_rule_sets={},
                    consumed_policy_rule_sets={},
                    expected_res_status=200)
                for sg in [mapping.consumed_sg_id, mapping.provided_sg_id]:
                    for cidr in expected_cidrs:
                        ssh_attrs['security_group_id'] = [sg]
                        ssh_attrs['remote_ip_prefix'] = [cidr]
                        self.assertFalse(self._get_sg_rule(**http_attrs))


class TestPolicyAction(ResourceMappingTestCase):

    def test_shared_create_reject_non_shared_spec(self):
        with mock.patch.object(servicechain_db.ServiceChainDbPlugin,
                               'get_servicechain_specs') as sc_spec_get:
            uuid = uuidutils.generate_uuid()
            sc_spec_get.return_value = [{'id': uuid}]
            res = self.create_policy_action(expected_res_status=400,
                                            shared=True,
                                            action_value=uuid)
            self.assertEqual('InvalidSharedResource',
                             res['NeutronError']['type'])
