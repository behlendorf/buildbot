# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Portions Copyright Buildbot Team Members
# Portions Copyright 2014 Longaccess private company

try:
    from moto import mock_ec2
    assert mock_ec2
    import boto
    assert boto
except ImportError:
    boto = None
    ec2 = None

if boto is not None:
    from buildbot.buildslave import ec2

from twisted.trial import unittest


# redefine the mock_ec2 decorator to skip the test if boto isn't installed
def skip_ec2(f):
    f.skip = "boto not installed"
    return f
if boto is None:
    mock_ec2 = skip_ec2


class TestEC2LatentBuildSlave(unittest.TestCase):
    ec2_connection = None

    def setUp(self):
        super(TestEC2LatentBuildSlave, self).setUp()
        if boto is None:
            raise unittest.SkipTest("moto not found")

    def botoSetup(self):
        c = boto.connect_ec2()
        try:
            c.create_key_pair('latent_buildbot_slave')
        except NotImplementedError:
            raise unittest.SkipTest("KeyPairs.create_key_pair not implemented"
                                    " in this version of moto, please update.")
        c.create_security_group('latent_buildbot_slave', 'the security group')
        instance = c.run_instances('foo').instances[0]
        c.create_image(instance.id, "foo", "bar")
        c.terminate_instances([instance.id])
        return c

    @mock_ec2
    def test_constructor_minimal(self):
        c = self.botoSetup()
        amis = c.get_all_images()
        bs = ec2.EC2LatentBuildSlave('bot1', 'sekrit', 'm1.large',
                                     identifier='publickey',
                                     secret_identifier='privatekey',
                                     ami=amis[0].id
                                     )
        self.assertEqual(bs.slavename, 'bot1')
        self.assertEqual(bs.password, 'sekrit')
        self.assertEqual(bs.instance_type, 'm1.large')
        self.assertEqual(bs.ami, amis[0].id)

    @mock_ec2
    def test_constructor_tags(self):
        c = self.botoSetup()
        amis = c.get_all_images()
        tags = {'foo': 'bar'}
        bs = ec2.EC2LatentBuildSlave('bot1', 'sekrit', 'm1.large',
                                     identifier='publickey',
                                     secret_identifier='privatekey',
                                     tags=tags,
                                     ami=amis[0].id
                                     )
        self.assertEqual(bs.tags, tags)

    @mock_ec2
    def test_fail_mixing_classic_and_vpc_ec2_settings(self):
        c = self.botoSetup()
        amis = c.get_all_images()

        def create_slave():
            ec2.EC2LatentBuildSlave('bot1', 'sekrit', 'm1.large',
                                    keypair_name="test_key",
                                    identifier='publickey',
                                    secret_identifier='privatekey',
                                    ami=amis[0].id,
                                    security_name="classic",
                                    subnet_id="sn-1234"
                                    )

        self.assertRaises(ValueError, create_worker)

    @mock_ec2
    def test_start_vpc_instance(self):
        c = self.botoSetup()

        vpc_conn = boto.connect_vpc()
        vpc = vpc_conn.create_vpc("192.168.0.0/24")
        subnet = vpc_conn.create_subnet(vpc.id, "192.168.0.0/24")
        amis = c.get_all_images()

        sg = c.create_security_group("test_sg", "test_sg", vpc.id)
        bs = ec2.EC2LatentBuildSlave('bot1', 'sekrit', 'm1.large',
                                    identifier='publickey',
                                    secret_identifier='privatekey',
                                    keypair_name="test_key",
                                    security_group_ids=[sg.id],
                                    subnet_id=subnet.id,
                                    ami=amis[0].id
                                    )

        instance_id, _, _ = bs._start_instance()
        instances = [i for i in c.get_only_instances()
                     if i.state != "terminated"]

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, instance_id)
        self.assertEqual(instances[0].subnet_id, subnet.id)
        self.assertEqual(len(instances[0].groups), 1)
        self.assertEqual(instances[0].groups[0].id, sg.id)

    @mock_ec2
    def test_start_instance(self):
        c = self.botoSetup()
        amis = c.get_all_images()
        bs = ec2.EC2LatentBuildSlave('bot1', 'sekrit', 'm1.large',
                                     identifier='publickey',
                                     secret_identifier='privatekey',
                                     ami=amis[0].id
                                     )
        instance_id, image_id, start_time = bs._start_instance()
        self.assertTrue(instance_id.startswith('i-'))
        self.assertTrue(image_id.startswith('r-'))
        self.assertTrue(start_time > 0)
        instances = [i for i in c.get_only_instances()
                     if i.state != "terminated"]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, instance_id)
        self.assertEqual(instances[0].tags, {})

    @mock_ec2
    def test_start_instance_volumes(self):
        c = self.botoSetup()
        amis = c.get_all_images()
        bs = ec2.EC2LatentBuildSlave('bot1', 'sekrit', 'm1.large',
                                     identifier='publickey',
                                     secret_identifier='privatekey',
                                     ami=amis[0].id,
                                     block_device_map={
                                         "/dev/xvdb": {
                                             "volume_type": "io1",
                                             "iops": 10,
                                             "size": 20,
                                         },
                                         "/dev/xvdc": {
                                             "volume_type": "gp2",
                                             "size": 30,
                                             "delete_on_termination": False
                                         }
                                         }
                                     )

        # moto does not currently map volumes properly.  below ensures
        # that my conversion code properly composes it, including
        # delete_on_termination default.
        from boto.ec2.blockdevicemapping import BlockDeviceType
        self.assertEqual(set(['/dev/xvdb', '/dev/xvdc']), set(bs.block_device_map.keys()))

        def assertBlockDeviceEqual(a, b):
            self.assertEqual(a.volume_type, b.volume_type)
            self.assertEqual(a.iops, b.iops)
            self.assertEqual(a.size, b.size)
            self.assertEqual(a.delete_on_termination, b.delete_on_termination)

        assertBlockDeviceEqual(
            BlockDeviceType(volume_type='io1', iops=10, size=20, delete_on_termination=True),
            bs.block_device_map['/dev/xvdb'])

        assertBlockDeviceEqual(
            BlockDeviceType(volume_type='gp2', size=30, delete_on_termination=False),
            bs.block_device_map['/dev/xvdc'])

    @mock_ec2
    def test_start_instance_tags(self):
        c = self.botoSetup()
        amis = c.get_all_images()
        tags = {'foo': 'bar'}
        bs = ec2.EC2LatentBuildSlave('bot1', 'sekrit', 'm1.large',
                                     identifier='publickey',
                                     secret_identifier='privatekey',
                                     tags=tags,
                                     ami=amis[0].id
                                     )
        id, _, _ = bs._start_instance()
        instances = [i for i in c.get_only_instances()
                     if i.state != "terminated"]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, id)
        self.assertEqual(instances[0].tags, tags)
