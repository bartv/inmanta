"""
    Copyright 2017 Inmanta

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Contact: code@inmanta.com
"""
from collections import defaultdict, namedtuple
import time
import uuid
from threading import Condition
from itertools import groupby
import logging
import os
import shutil
import subprocess
import asyncio


import pytest
from _pytest.fixtures import fixture

from inmanta import agent, data, const, execute, config
from inmanta.agent.handler import provider, ResourceHandler, SkipResource, HandlerContext
from inmanta.resources import resource, Resource
import inmanta.agent.agent
from inmanta.agent.agent import Agent
from utils import retry_limited, assert_equal_ish, UNKWN
from inmanta.config import Config
from inmanta.ast import CompilerException
from inmanta.server.bootloader import InmantaBootloader
from inmanta.server import SLICE_AGENT_MANAGER
from typing import List, Tuple, Optional, Dict
from inmanta.const import ResourceState

logger = logging.getLogger("inmanta.test.server_agent")

ResourceContainer = namedtuple('ResourceContainer', ['Provider', 'waiter', 'wait_for_done_with_waiters'])


@fixture(scope="function")
def resource_container():

    @resource("test::Resource", agent="agent", id_attribute="key")
    class MyResource(Resource):
        """
            A file on a filesystem
        """
        fields = ("key", "value", "purged")

    @resource("test::Fact", agent="agent", id_attribute="key")
    class FactResource(Resource):
        """
            A file on a filesystem
        """
        fields = ("key", "value", "purged", "skip", "factvalue", 'skipFact')

    @resource("test::Fail", agent="agent", id_attribute="key")
    class FailR(Resource):
        """
            A file on a filesystem
        """
        fields = ("key", "value", "purged")

    @resource("test::Wait", agent="agent", id_attribute="key")
    class WaitR(Resource):
        """
            A file on a filesystem
        """
        fields = ("key", "value", "purged")

    @resource("test::Noprov", agent="agent", id_attribute="key")
    class NoProv(Resource):
        """
            A file on a filesystem
        """
        fields = ("key", "value", "purged")

    @resource("test::FailFast", agent="agent", id_attribute="key")
    class FailFastR(Resource):
        """
            A file on a filesystem
        """
        fields = ("key", "value", "purged")

    @resource("test::BadEvents", agent="agent", id_attribute="key")
    class BadeEventR(Resource):
        """
            A file on a filesystem
        """
        fields = ("key", "value", "purged")

    @provider("test::Resource", name="test_resource")
    class Provider(ResourceHandler):

        def check_resource(self, ctx, resource):
            self.read(resource.id.get_agent_name(), resource.key)
            assert resource.value != const.UNKNOWN_STRING
            current = resource.clone()
            current.purged = not self.isset(resource.id.get_agent_name(), resource.key)

            if not current.purged:
                current.value = self.get(resource.id.get_agent_name(), resource.key)
            else:
                current.value = None

            return current

        def do_changes(self, ctx, resource, changes):
            if self.skip(resource.id.get_agent_name(), resource.key):
                raise SkipResource()

            if self.fail(resource.id.get_agent_name(), resource.key):
                raise Exception("Failed")

            self.touch(resource.id.get_agent_name(), resource.key)

            if "purged" in changes:
                if changes["purged"]["desired"]:
                    self.delete(resource.id.get_agent_name(), resource.key)
                    ctx.set_purged()
                else:
                    self.set(resource.id.get_agent_name(), resource.key, resource.value)
                    ctx.set_created()

            elif "value" in changes:
                self.set(resource.id.get_agent_name(), resource.key, resource.value)
                ctx.set_updated()

            return changes

        def facts(self, ctx, resource):
            return {"length": len(self.get(resource.id.get_agent_name(), resource.key)), "key1": "value1", "key2": "value2"}

        def can_process_events(self) -> bool:
            return True

        def process_events(self, ctx, resource, events):
            self.__class__._EVENTS[resource.id.get_agent_name()][resource.key].append(events)
            super(Provider, self).process_events(ctx, resource, events)

        def can_reload(self) -> bool:
            return True

        def do_reload(self, ctx, resource):
            self.__class__._RELOAD_COUNT[resource.id.get_agent_name()][resource.key] += 1

        _STATE = defaultdict(dict)
        _WRITE_COUNT = defaultdict(lambda: defaultdict(lambda: 0))
        _RELOAD_COUNT = defaultdict(lambda: defaultdict(lambda: 0))
        _READ_COUNT = defaultdict(lambda: defaultdict(lambda: 0))
        _TO_SKIP = defaultdict(lambda: defaultdict(lambda: 0))
        _TO_FAIL = defaultdict(lambda: defaultdict(lambda: 0))

        _EVENTS = defaultdict(lambda: defaultdict(lambda: []))

        @classmethod
        def set_skip(cls, agent, key, skip):
            cls._TO_SKIP[agent][key] = skip

        @classmethod
        def set_fail(cls, agent, key, failcount):
            cls._TO_FAIL[agent][key] = failcount

        @classmethod
        def skip(cls, agent, key):
            doskip = cls._TO_SKIP[agent][key]
            if doskip == 0:
                return False
            cls._TO_SKIP[agent][key] -= 1
            return True

        @classmethod
        def fail(cls, agent, key):
            doskip = cls._TO_FAIL[agent][key]
            if doskip == 0:
                return False
            cls._TO_FAIL[agent][key] -= 1
            return True

        @classmethod
        def touch(cls, agent, key):
            cls._WRITE_COUNT[agent][key] += 1

        @classmethod
        def read(cls, agent, key):
            cls._READ_COUNT[agent][key] += 1

        @classmethod
        def set(cls, agent, key, value):
            cls._STATE[agent][key] = value

        @classmethod
        def get(cls, agent, key):
            if key in cls._STATE[agent]:
                return cls._STATE[agent][key]
            return None

        @classmethod
        def isset(cls, agent, key):
            return key in cls._STATE[agent]

        @classmethod
        def delete(cls, agent, key):
            if cls.isset(agent, key):
                del cls._STATE[agent][key]

        @classmethod
        def changecount(cls, agent, key):
            return cls._WRITE_COUNT[agent][key]

        @classmethod
        def readcount(cls, agent, key):
            return cls._READ_COUNT[agent][key]

        @classmethod
        def getevents(cls, agent, key):
            return cls._EVENTS[agent][key]

        @classmethod
        def reloadcount(cls, agent, key):
            return cls._RELOAD_COUNT[agent][key]

        @classmethod
        def reset(cls):
            cls._STATE = defaultdict(dict)
            cls._EVENTS = defaultdict(lambda: defaultdict(lambda: []))
            cls._WRITE_COUNT = defaultdict(lambda: defaultdict(lambda: 0))
            cls._READ_COUNT = defaultdict(lambda: defaultdict(lambda: 0))
            cls._TO_SKIP = defaultdict(lambda: defaultdict(lambda: 0))
            cls._RELOAD_COUNT = defaultdict(lambda: defaultdict(lambda: 0))

    @provider("test::Fail", name="test_fail")
    class Fail(ResourceHandler):

        def check_resource(self, ctx, resource):
            current = resource.clone()
            current.purged = not Provider.isset(resource.id.get_agent_name(), resource.key)

            if not current.purged:
                current.value = Provider.get(resource.id.get_agent_name(), resource.key)
            else:
                current.value = None

            return current

        def do_changes(self, ctx, resource, changes):
            raise Exception()

    @provider("test::FailFast", name="test_failfast")
    class FailFast(ResourceHandler):

        def check_resource(self, ctx, resource):
            raise Exception()

    @provider("test::Fact", name="test_fact")
    class Fact(ResourceHandler):

        def check_resource(self, ctx, resource):
            current = resource.clone()
            current.purged = not Provider.isset(resource.id.get_agent_name(), resource.key)

            current.value = "that"

            return current

        def do_changes(self, ctx, resource, changes):
            if resource.skip:
                raise SkipResource("can not deploy")
            if "purged" in changes:
                if changes["purged"]["desired"]:
                    Provider.delete(resource.id.get_agent_name(), resource.key)
                    ctx.set_purged()
                else:
                    Provider.set(resource.id.get_agent_name(), resource.key, "x")
                    ctx.set_created()
            else:
                ctx.set_updated()

        def facts(self, ctx: HandlerContext, resource: Resource) -> dict:
            if not Provider.isset(resource.id.get_agent_name(), resource.key):
                return {}
            elif resource.skipFact:
                raise SkipResource("Not ready")
            return {"fact": resource.factvalue}

    @provider("test::BadEvents", name="test_bad_events")
    class BadEvents(ResourceHandler):

        def check_resource(self, ctx, resource):
            current = resource.clone()
            return current

        def do_changes(self, ctx, resource, changes):
            pass

        def can_process_events(self) -> bool:
            return True

        def process_events(self, ctx, resource, events):
            raise Exception()

    waiter = Condition()

    async def wait_for_done_with_waiters(client, env_id, version):
        # unhang waiters
        result = await client.get_version(env_id, version)
        assert result.code == 200
        while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
            result = await client.get_version(env_id, version)
            logger.info("waiting with waiters, %s resources done", result.result["model"]["done"])
            if result.result["model"]["done"] > 0:
                waiter.acquire()
                waiter.notifyAll()
                waiter.release()
            await asyncio.sleep(0.1)

        return result

    @provider("test::Wait", name="test_wait")
    class Wait(ResourceHandler):

        def __init__(self, agent, io=None):
            super().__init__(agent, io)
            self.traceid = uuid.uuid4()

        def check_resource(self, ctx, resource):
            current = resource.clone()
            current.purged = not Provider.isset(resource.id.get_agent_name(), resource.key)

            if not current.purged:
                current.value = Provider.get(resource.id.get_agent_name(), resource.key)
            else:
                current.value = None

            return current

        def do_changes(self, ctx, resource, changes):
            logger.info("Hanging waiter %s", self.traceid)
            waiter.acquire()
            waiter.wait()
            waiter.release()
            logger.info("Releasing waiter %s", self.traceid)
            if "purged" in changes:
                if changes["purged"]["desired"]:
                    Provider.delete(resource.id.get_agent_name(), resource.key)
                    ctx.set_purged()
                else:
                    Provider.set(resource.id.get_agent_name(), resource.key, resource.value)
                    ctx.set_created()

            if "value" in changes:
                Provider.set(resource.id.get_agent_name(), resource.key, resource.value)
                ctx.set_updated()

    return ResourceContainer(Provider=Provider, wait_for_done_with_waiters=wait_for_done_with_waiters, waiter=waiter)


@pytest.mark.asyncio(timeout=150)
async def test_dryrun_and_deploy(server_multi, client_multi, resource_container):
    """
        dryrun and deploy a configuration model

        There is a second agent with an undefined resource. The server will shortcut the dryrun and deploy for this resource
        without an agent being present.
    """

    agentmanager = server_multi.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    result = await client_multi.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client_multi.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()

    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key2", "incorrect_value")
    resource_container.Provider.set("agent1", "key3", "value")

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': ['test::Resource[agent1,key=key2],v=%d' % version],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                  'send_event': False,
                  'requires': [],
                  'purged': False,
                  },
                 {'key': 'key3',
                  'value': None,
                  'id': 'test::Resource[agent1,key=key3],v=%d' % version,
                  'send_event': False,
                  'requires': [],
                  'purged': True,
                  },
                 {'key': 'key4',
                  'value': execute.util.Unknown(source=None),
                  'id': 'test::Resource[agent2,key=key4],v=%d' % version,
                  'send_event': False,
                  'requires': [],
                  'purged': False,
                  },
                 {'key': 'key5',
                  'value': "val",
                  'id': 'test::Resource[agent2,key=key5],v=%d' % version,
                  'send_event': False,
                  'requires': ['test::Resource[agent2,key=key4],v=%d' % version],
                  'purged': False,
                  },
                 {'key': 'key6',
                  'value': "val",
                  'id': 'test::Resource[agent2,key=key6],v=%d' % version,
                  'send_event': False,
                  'requires': ['test::Resource[agent2,key=key5],v=%d' % version],
                  'purged': False,
                  }
                 ]

    status = {'test::Resource[agent2,key=key4]': const.ResourceState.undefined}
    result = await client_multi.put_version(tid=env_id, version=version, resources=resources, resource_state=status,
                                            unknowns=[], version_info={})
    assert result.code == 200

    mod_db = await data.ConfigurationModel.get_version(uuid.UUID(env_id), version)
    undep = await mod_db.get_undeployable()
    assert undep == ['test::Resource[agent2,key=key4]']

    undep = await mod_db.get_skipped_for_undeployable()
    assert undep == ['test::Resource[agent2,key=key5]', 'test::Resource[agent2,key=key6]']

    # request a dryrun
    result = await client_multi.dryrun_request(env_id, version)
    assert result.code == 200
    assert result.result["dryrun"]["total"] == len(resources)
    assert result.result["dryrun"]["todo"] == len(resources)

    # get the dryrun results
    result = await client_multi.dryrun_list(env_id, version)
    assert result.code == 200
    assert len(result.result["dryruns"]) == 1

    while result.result["dryruns"][0]["todo"] > 0:
        result = await client_multi.dryrun_list(env_id, version)
        await asyncio.sleep(0.1)

    dry_run_id = result.result["dryruns"][0]["id"]
    result = await client_multi.dryrun_report(env_id, dry_run_id)
    assert result.code == 200

    changes = result.result["dryrun"]["resources"]
    assert changes[resources[0]["id"]]["changes"]["purged"]["current"]
    assert not changes[resources[0]["id"]]["changes"]["purged"]["desired"]
    assert changes[resources[0]["id"]]["changes"]["value"]["current"] is None
    assert changes[resources[0]["id"]]["changes"]["value"]["desired"] == resources[0]["value"]

    assert changes[resources[1]["id"]]["changes"]["value"]["current"] == "incorrect_value"
    assert changes[resources[1]["id"]]["changes"]["value"]["desired"] == resources[1]["value"]

    assert not changes[resources[2]["id"]]["changes"]["purged"]["current"]
    assert changes[resources[2]["id"]]["changes"]["purged"]["desired"]

    # do a deploy
    result = await client_multi.release_version(env_id, version, True)
    assert result.code == 200
    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 6
    assert result.result["model"]["result"] == "deploying"

    result = await client_multi.get_version(env_id, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client_multi.get_version(env_id, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    assert resource_container.Provider.isset("agent1", "key1")
    assert resource_container.Provider.get("agent1", "key1") == "value1"
    assert resource_container.Provider.get("agent1", "key2") == "value2"
    assert not resource_container.Provider.isset("agent1", "key3")

    actions = await data.ResourceAction.get_list()
    assert sum([len(x.resource_version_ids) for x in actions if x.status == const.ResourceState.undefined]) == 1
    assert sum([len(x.resource_version_ids) for x in actions if x.status == const.ResourceState.skipped_for_undefined]) == 2

    agent.stop()


@pytest.mark.asyncio(timeout=100)
async def test_deploy_with_undefined(server_multi, client_multi, resource_container):
    """
         Test deploy of resource with undefined
    """

    # agent backoff makes this test unreliable or slow, so we turn it off
    backoff = inmanta.agent.agent.GET_RESOURCE_BACKOFF
    inmanta.agent.agent.GET_RESOURCE_BACKOFF = 0

    agentmanager = server_multi.get_endpoint(SLICE_AGENT_MANAGER)

    Config.set("config", "agent-interval", "100")

    resource_container.Provider.reset()
    result = await client_multi.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client_multi.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    resource_container.Provider.set_skip("agent2", "key1", 1)

    agent = Agent(
        hostname="node1",
        environment=env_id,
        agent_map={"agent1": "localhost", "agent2": "localhost"},
        code_loader=False
    )
    agent.add_end_point_name("agent2")
    agent.start()

    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent2,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': [],
                  },
                 {'key': 'key2',
                  'value': execute.util.Unknown(source=None),
                  'id': 'test::Resource[agent2,key=key2],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': [],
                  },
                 {'key': 'key4',
                  'value': execute.util.Unknown(source=None),
                  'id': 'test::Resource[agent2,key=key4],v=%d' % version,
                  'send_event': False,
                  'requires': ['test::Resource[agent2,key=key1],v=%d' % version,
                               'test::Resource[agent2,key=key2],v=%d' % version],
                  'purged': False,
                  },
                 {'key': 'key5',
                  'value': "val",
                  'id': 'test::Resource[agent2,key=key5],v=%d' % version,
                  'send_event': False,
                  'requires': ['test::Resource[agent2,key=key4],v=%d' % version],
                  'purged': False,
                  }
                 ]

    status = {'test::Resource[agent2,key=key4]': const.ResourceState.undefined,
              'test::Resource[agent2,key=key2]': const.ResourceState.undefined}
    result = await client_multi.put_version(tid=env_id, version=version, resources=resources, resource_state=status,
                                            unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client_multi.release_version(env_id, version, True)
    assert result.code == 200
    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == len(resources)
    assert result.result["model"]["result"] == "deploying"

    # The server will mark the full version as deployed even though the agent has not done anything yet.
    result = await client_multi.get_version(env_id, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client_multi.get_version(env_id, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)
    assert result.code == 200

    actions = await data.ResourceAction.get_list()
    assert len([x for x in actions if x.status == const.ResourceState.undefined]) >= 1

    result = await client_multi.get_version(env_id, version)
    assert result.code == 200

    assert resource_container.Provider.changecount("agent2", "key4") == 0
    assert resource_container.Provider.changecount("agent2", "key5") == 0
    assert resource_container.Provider.changecount("agent2", "key1") == 0

    assert resource_container.Provider.readcount("agent2", "key4") == 0
    assert resource_container.Provider.readcount("agent2", "key5") == 0
    assert resource_container.Provider.readcount("agent2", "key1") == 1

    # Do a second deploy of the same model on agent2 with undefined resources
    await agent.trigger_update("env_id", "agent2")

    result = await client_multi.get_version(env_id, version, include_logs=True)
    import pprint
    pprint.pprint(result.result)

    def done():
        return resource_container.Provider.changecount("agent2", "key4") == 0 and \
            resource_container.Provider.changecount("agent2", "key5") == 0 and \
            resource_container.Provider.changecount("agent2", "key1") == 1 and \
            resource_container.Provider.readcount("agent2", "key4") == 0 and \
            resource_container.Provider.readcount("agent2", "key5") == 0 and \
            resource_container.Provider.readcount("agent2", "key1") == 2

    await retry_limited(done, 100)

    agent.stop()
    inmanta.agent.agent.GET_RESOURCE_BACKOFF = backoff


@pytest.mark.asyncio(timeout=30)
async def test_server_restart(resource_container, server, mongo_db, client):
    """
        dryrun and deploy a configuration model
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"},
                  code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key2", "incorrect_value")
    resource_container.Provider.set("agent1", "key3", "value")

    server.stop()

    ibl = InmantaBootloader()
    server = ibl.restserver
    ibl.start()
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': ['test::Resource[agent1,key=key2],v=%d' % version],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key3',
                  'value': None,
                  'id': 'test::Resource[agent1,key=key3],v=%d' % version,
                  'requires': [],
                  'purged': True,
                  'send_event': False,
                  }
                 ]

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # request a dryrun
    result = await client.dryrun_request(env_id, version)
    assert result.code == 200
    assert result.result["dryrun"]["total"] == len(resources)
    assert result.result["dryrun"]["todo"] == len(resources)

    # get the dryrun results
    result = await client.dryrun_list(env_id, version)
    assert result.code == 200
    assert len(result.result["dryruns"]) == 1

    while result.result["dryruns"][0]["todo"] > 0:
        result = await client.dryrun_list(env_id, version)
        await asyncio.sleep(0.1)

    dry_run_id = result.result["dryruns"][0]["id"]
    result = await client.dryrun_report(env_id, dry_run_id)
    assert result.code == 200

    changes = result.result["dryrun"]["resources"]
    assert changes[resources[0]["id"]]["changes"]["purged"]["current"]
    assert not changes[resources[0]["id"]]["changes"]["purged"]["desired"]
    assert changes[resources[0]["id"]]["changes"]["value"]["current"] is None
    assert changes[resources[0]["id"]]["changes"]["value"]["desired"] == resources[0]["value"]

    assert changes[resources[1]["id"]]["changes"]["value"]["current"] == "incorrect_value"
    assert changes[resources[1]["id"]]["changes"]["value"]["desired"] == resources[1]["value"]

    assert not changes[resources[2]["id"]]["changes"]["purged"]["current"]
    assert changes[resources[2]["id"]]["changes"]["purged"]["desired"]

    # do a deploy
    result = await client.release_version(env_id, version, True)
    assert result.code == 200
    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 3
    assert result.result["model"]["result"] == "deploying"

    result = await client.get_version(env_id, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(env_id, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    assert resource_container.Provider.isset("agent1", "key1")
    assert resource_container.Provider.get("agent1", "key1") == "value1"
    assert resource_container.Provider.get("agent1", "key2") == "value2"
    assert not resource_container.Provider.isset("agent1", "key3")

    agent.stop()
    ibl.stop()


@pytest.mark.asyncio(timeout=30)
async def test_spontaneous_deploy(resource_container, server, client):
    """
        dryrun and deploy a configuration model
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    Config.set("config", "agent-interval", "2")
    Config.set("config", "agent-splay", "2")

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"},
                  code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key2", "incorrect_value")
    resource_container.Provider.set("agent1", "key3", "value")

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': ['test::Resource[agent1,key=key2],v=%d' % version],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key3',
                  'value': None,
                  'id': 'test::Resource[agent1,key=key3],v=%d' % version,
                  'requires': [],
                  'purged': True,
                  'send_event': False,
                  }
                 ]

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(env_id, version, False)
    assert result.code == 200
    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 3
    assert result.result["model"]["result"] == "deploying"

    result = await client.get_version(env_id, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(env_id, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    assert resource_container.Provider.isset("agent1", "key1")
    assert resource_container.Provider.get("agent1", "key1") == "value1"
    assert resource_container.Provider.get("agent1", "key2") == "value2"
    assert not resource_container.Provider.isset("agent1", "key3")

    agent.stop()


@pytest.mark.asyncio(timeout=30)
async def test_failing_deploy_no_handler(resource_container, server, client):
    """
        dryrun and deploy a configuration model
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]
    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Noprov[agent1,key=key1],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': [],
                  }
                 ]

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(env_id, version, True)
    assert result.code == 200
    assert result.result["model"]["total"] == 1

    result = await client.get_version(env_id, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(env_id, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    result = await client.get_version(env_id, version, include_logs=True)

    final_log = result.result["resources"][0]["actions"][0]["messages"][-1]
    assert "traceback" in final_log["kwargs"]

    agent.stop()


@pytest.mark.asyncio
async def test_dual_agent(resource_container, server, client, environment):
    """
        dryrun and deploy a configuration model
    """
    resource_container.Provider.reset()
    myagent = agent.Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost", "agent2": "localhost"},
                          code_loader=False)
    myagent.add_end_point_name("agent1")
    myagent.add_end_point_name("agent2")
    myagent.start()
    await retry_limited(lambda: len(server.get_endpoint("session")._sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key1", "incorrect_value")
    resource_container.Provider.set("agent2", "key1", "incorrect_value")

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Wait[agent1,key=key1],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': []
                  },
                 {'key': 'key2',
                  'value': 'value1',
                  'id': 'test::Wait[agent1,key=key2],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': ['test::Wait[agent1,key=key1],v=%d' % version]
                  },
                 {'key': 'key1',
                  'value': 'value2',
                  'id': 'test::Wait[agent2,key=key1],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': []
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Wait[agent2,key=key2],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': ['test::Wait[agent2,key=key1],v=%d' % version]
                  }]

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # expire rate limiting
    await asyncio.sleep(0.5)
    # do a deploy
    result = await client.release_version(environment, version, True)
    assert result.code == 200

    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 4

    result = await client.get_version(environment, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        resource_container.waiter.acquire()
        resource_container.waiter.notifyAll()
        resource_container.waiter.release()
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)
    assert result.result["model"]["result"] == const.VersionState.success.name

    assert resource_container.Provider.isset("agent1", "key1")
    assert resource_container.Provider.get("agent1", "key1") == "value1"
    assert resource_container.Provider.get("agent2", "key1") == "value2"
    assert resource_container.Provider.get("agent1", "key2") == "value1"
    assert resource_container.Provider.get("agent2", "key2") == "value2"

    myagent.stop()


@pytest.mark.asyncio
async def test_server_agent_api(resource_container, client, server):
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]
    agent = Agent(environment=env_id, hostname="agent1", agent_map={"agent1": "localhost"}, code_loader=False)
    agent.start()

    agent = Agent(environment=env_id, hostname="agent2", agent_map={"agent2": "localhost"}, code_loader=False)
    agent.start()

    await retry_limited(lambda: len(agentmanager.sessions) == 2, 10)
    assert len(agentmanager.sessions) == 2

    result = await client.list_agent_processes(env_id)
    assert result.code == 200

    while len(result.result["processes"]) != 2:
        result = await client.list_agent_processes(env_id)
        assert result.code == 200
        await asyncio.sleep(0.1)

    assert len(result.result["processes"]) == 2
    agents = ["agent1", "agent2"]
    for proc in result.result["processes"]:
        assert proc["environment"] == env_id
        assert len(proc["endpoints"]) == 1
        assert proc["endpoints"][0]["name"] in agents
        agents.remove(proc["endpoints"][0]["name"])

    assert_equal_ish({'processes': [{'expired': None, 'environment': env_id,
                                     'endpoints': [{'name': UNKWN, 'process': UNKWN, 'id': UNKWN}], 'id': UNKWN,
                                     'hostname': UNKWN, 'first_seen': UNKWN, 'last_seen': UNKWN},
                                    {'expired': None, 'environment': env_id,
                                     'endpoints': [{'name': UNKWN, 'process': UNKWN, 'id': UNKWN}],
                                     'id': UNKWN, 'hostname': UNKWN, 'first_seen': UNKWN, 'last_seen': UNKWN}
                                    ]},
                     result.result, ['name', 'first_seen'])

    agentid = result.result["processes"][0]["id"]
    endpointid = [x["endpoints"][0]["id"] for x in result.result["processes"] if x["endpoints"][0]["name"] == "agent1"][0]

    result = await client.get_agent_process(id=agentid)
    assert result.code == 200

    result = await client.get_agent_process(id=uuid.uuid4())
    assert result.code == 404

    version = int(time.time())

    resources = [{'key': 'key',
                  'value': 'value',
                  'id': 'test::Resource[agent1,key=key],v=%d' % version,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key2',
                  'value': 'value',
                  'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  }]

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    result = await client.list_agents(tid=env_id)
    assert result.code == 200

    shouldbe = {'agents': [
        {'last_failover': UNKWN, 'environment': env_id, 'paused': False,
         'primary': endpointid, 'name': 'agent1', 'state': 'up'}]}

    assert_equal_ish(shouldbe, result.result)

    result = await client.list_agents(tid=uuid.uuid4())
    assert result.code == 404


@pytest.mark.asyncio
async def test_get_facts(resource_container, client, server):
    """
        Test retrieving facts from the agent
    """
    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(server.get_endpoint("session")._sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key", "value")

    version = int(time.time())

    resource_id_wov = "test::Resource[agent1,key=key]"
    resource_id = "%s,v=%d" % (resource_id_wov, version)

    resources = [{'key': 'key',
                  'value': 'value',
                  'id': resource_id,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  }]

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200
    result = await client.release_version(env_id, version, True)
    assert result.code == 200

    result = await client.get_param(env_id, "length", resource_id_wov)
    assert result.code == 503

    env_uuid = uuid.UUID(env_id)
    params = await data.Parameter.get_list(environment=env_uuid, resource_id=resource_id_wov)
    while len(params) < 3:
        params = await data.Parameter.get_list(environment=env_uuid, resource_id=resource_id_wov)
        await asyncio.sleep(0.1)

    result = await client.get_param(env_id, "key1", resource_id_wov)
    assert result.code == 200


@pytest.mark.asyncio
async def test_purged_facts(resource_container, client, server, environment):
    """
        Test if facts are purged when the resource is purged.
    """
    resource_container.Provider.reset()
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(server.get_endpoint("session")._sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key", "value")

    version = 1
    resource_id_wov = "test::Resource[agent1,key=key]"
    resource_id = "%s,v=%d" % (resource_id_wov, version)

    resources = [{'key': 'key',
                  'value': 'value',
                  'id': resource_id,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  }]

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200
    result = await client.release_version(environment, version, True)
    assert result.code == 200

    result = await client.get_param(environment, "length", resource_id_wov)
    assert result.code == 503

    env_uuid = uuid.UUID(environment)
    params = await data.Parameter.get_list(environment=env_uuid, resource_id=resource_id_wov)
    while len(params) < 3:
        params = await data.Parameter.get_list(environment=env_uuid, resource_id=resource_id_wov)
        await asyncio.sleep(0.1)

    result = await client.get_param(environment, "key1", resource_id_wov)
    assert result.code == 200

    # Purge the resource
    version = 2
    resources[0]["id"] = "%s,v=%d" % (resource_id_wov, version)
    resources[0]["purged"] = True
    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200
    result = await client.release_version(environment, version, True)
    assert result.code == 200

    result = await client.get_version(environment, version)
    assert result.code == 200
    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    # The resource facts should be purged
    result = await client.get_param(environment, "length", resource_id_wov)
    assert result.code == 503


@pytest.mark.asyncio
async def test_get_facts_extended(server, client, resource_container, environment):
    """
        dryrun and deploy a configuration model automatically
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)
    # allow very rapid fact refresh
    agentmanager._fact_resource_block = 0.1

    resource_container.Provider.reset()
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    # mark some as existing
    resource_container.Provider.set("agent1", "key1", "value")
    resource_container.Provider.set("agent1", "key2", "value")
    resource_container.Provider.set("agent1", "key4", "value")
    resource_container.Provider.set("agent1", "key5", "value")
    resource_container.Provider.set("agent1", "key6", "value")
    resource_container.Provider.set("agent1", "key7", "value")

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Fact[agent1,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'skip': True,
                  'skipFact': False,
                  'factvalue': "fk1",
                  'requires': [],
                  },
                 {'key': 'key2',
                  'value': 'value1',
                  'id': 'test::Fact[agent1,key=key2],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'skip': False,
                  'skipFact': False,
                  'factvalue': "fk2",
                  'requires': [],
                  },
                 {'key': 'key3',
                  'value': 'value1',
                  'id': 'test::Fact[agent1,key=key3],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'skip': False,
                  'skipFact': False,
                  'factvalue': "fk3",
                  'requires': [],
                  },
                 {'key': 'key4',
                  'value': 'value1',
                  'id': 'test::Fact[agent1,key=key4],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'skip': False,
                  'skipFact': False,
                  'factvalue': "fk4",
                  'requires': [],
                  },
                 {'key': 'key5',
                  'value': 'value1',
                  'id': 'test::Fact[agent1,key=key5],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'skip': False,
                  'skipFact': True,
                  'factvalue': None,
                  'requires': [],
                  },
                 {'key': 'key6',
                  'value': 'value1',
                  'id': 'test::Fact[agent1,key=key6],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'skip': False,
                  'skipFact': False,
                  'factvalue': None,
                  'requires': [],
                  },
                 {'key': 'key7',
                  'value': 'value1',
                  'id': 'test::Fact[agent1,key=key7],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'skip': False,
                  'skipFact': False,
                  'factvalue': "",
                  'requires': [],
                  },
                 ]

    resource_states = {'test::Fact[agent1,key=key4],v=%d' % version: const.ResourceState.undefined,
                       'test::Fact[agent1,key=key5],v=%d' % version: const.ResourceState.undefined}

    async def get_fact(rid, result_code=200, limit=10, lower_limit=2):
        lower_limit = limit - lower_limit
        result = await client.get_param(environment, "fact", rid)

        # add minimal nr of reps or failure cases
        while (result.code != result_code and limit > 0) or limit > lower_limit:
            limit -= 1
            await asyncio.sleep(0.1)
            result = await client.get_param(environment, "fact", rid)

        assert result.code == result_code
        return result

    result = await client.put_version(tid=environment,
                                      version=version,
                                      resources=resources,
                                      unknowns=[],
                                      version_info={},
                                      resource_state=resource_states)
    assert result.code == 200

    await get_fact('test::Fact[agent1,key=key1]')  # undeployable
    await get_fact('test::Fact[agent1,key=key2]')  # normal
    await get_fact('test::Fact[agent1,key=key3]', 503)  # not present
    await get_fact('test::Fact[agent1,key=key4]')  # unknown
    await get_fact('test::Fact[agent1,key=key5]', 503)  # broken
    f6 = await get_fact('test::Fact[agent1,key=key6]')  # normal
    f7 = await get_fact('test::Fact[agent1,key=key7]')  # normal

    assert f6.result["parameter"]["value"] == 'None'
    assert f7.result["parameter"]["value"] == ""

    result = await client.release_version(environment, version, True)
    assert result.code == 200

    result = await client.get_version(environment, version)
    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(0.1)

    await get_fact('test::Fact[agent1,key=key1]')  # undeployable
    await get_fact('test::Fact[agent1,key=key2]')  # normal
    await get_fact('test::Fact[agent1,key=key3]')  # not present -> present
    await get_fact('test::Fact[agent1,key=key4]')  # unknown
    await get_fact('test::Fact[agent1,key=key5]', 503)  # broken

    agent.stop()


@pytest.mark.asyncio
async def test_get_set_param(resource_container, client, server):
    """
        Test getting and setting params
    """
    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    result = await client.set_param(tid=env_id, id="key10", value="value10", source="user")
    assert result.code == 200

    result = await client.get_param(tid=env_id, id="key10")
    assert result.code == 200
    assert result.result["parameter"]["value"] == "value10"

    result = await client.delete_param(tid=env_id, id="key10")
    assert result.code == 200


@pytest.mark.asyncio
async def test_unkown_parameters(resource_container, client, server):
    """
        Test retrieving facts from the agent
    """
    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(server.get_endpoint("session")._sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key", "value")

    version = int(time.time())

    resource_id_wov = "test::Resource[agent1,key=key]"
    resource_id = "%s,v=%d" % (resource_id_wov, version)

    resources = [{'key': 'key',
                  'value': 'value',
                  'id': resource_id,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  }]

    unknowns = [{"resource": resource_id_wov, "parameter": "length", "source": "fact"}]
    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=unknowns,
                                      version_info={})
    assert result.code == 200

    result = await client.release_version(env_id, version, True)
    assert result.code == 200

    await server.get_endpoint("server").renew_expired_facts()

    env_id = uuid.UUID(env_id)
    params = await data.Parameter.get_list(environment=env_id, resource_id=resource_id_wov)
    while len(params) < 3:
        params = await data.Parameter.get_list(environment=env_id, resource_id=resource_id_wov)
        await asyncio.sleep(0.1)

    result = await client.get_param(env_id, "length", resource_id_wov)
    assert result.code == 200


@pytest.mark.asyncio()
async def test_fail(resource_container, client, server):
    """
        Test results when a step fails
    """
    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False, poolsize=10)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(server.get_endpoint("session")._sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key", "value")

    version = int(time.time())

    resources = [{'key': 'key',
                  'value': 'value',
                  'id': 'test::Fail[agent1,key=key],v=%d' % version,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key2',
                  'value': 'value',
                  'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                  'requires': ['test::Fail[agent1,key=key],v=%d' % version],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key3',
                  'value': 'value',
                  'id': 'test::Resource[agent1,key=key3],v=%d' % version,
                  'requires': ['test::Fail[agent1,key=key],v=%d' % version],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key4',
                  'value': 'value',
                  'id': 'test::Resource[agent1,key=key4],v=%d' % version,
                  'requires': ['test::Resource[agent1,key=key3],v=%d' % version],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key5',
                  'value': 'value',
                  'id': 'test::Resource[agent1,key=key5],v=%d' % version,
                  'requires': ['test::Resource[agent1,key=key4],v=%d' % version,
                               'test::Fail[agent1,key=key],v=%d' % version],
                  'purged': False,
                  'send_event': False,
                  }]

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # deploy and wait until done
    result = await client.release_version(env_id, version, True)
    assert result.code == 200

    result = await client.get_version(env_id, version)
    assert result.code == 200
    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(env_id, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    states = {x["id"]: x["status"] for x in result.result["resources"]}

    assert states['test::Fail[agent1,key=key],v=%d' % version] == "failed"
    assert states['test::Resource[agent1,key=key2],v=%d' % version] == "skipped"
    assert states['test::Resource[agent1,key=key3],v=%d' % version] == "skipped"
    assert states['test::Resource[agent1,key=key4],v=%d' % version] == "skipped"
    assert states['test::Resource[agent1,key=key5],v=%d' % version] == "skipped"


@pytest.mark.asyncio(timeout=15)
async def test_wait(resource_container, client, server):
    """
        If this test fail due to timeout,
        this is probably due to the mechanism in the agent that prevents pulling resources in very rapid succession.

        If the test server is slow, a get_resources call takes a long time,
        this makes the back-off longer

        this test deploys two models in rapid successions, if the server is slow, this may fail due to the back-off
    """
    resource_container.Provider.reset()

    # setup project
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    # setup env
    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    # setup agent
    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False, poolsize=10)
    agent.add_end_point_name("agent1")
    agent.start()

    # wait for agent
    await retry_limited(lambda: len(server.get_endpoint("session")._sessions) == 1, 10)

    # set the deploy environment
    resource_container.Provider.set("agent1", "key", "value")

    def make_version(offset=0):
        version = int(time.time() + offset)

        resources = [{'key': 'key',
                      'value': 'value',
                      'id': 'test::Wait[agent1,key=key],v=%d' % version,
                      'requires': [],
                      'purged': False,
                      'send_event': False,
                      },
                     {'key': 'key2',
                      'value': 'value',
                      'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                      'requires': ['test::Wait[agent1,key=key],v=%d' % version],
                      'purged': False,
                      'send_event': False,
                      },
                     {'key': 'key3',
                      'value': 'value',
                      'id': 'test::Resource[agent1,key=key3],v=%d' % version,
                      'requires': [],
                      'purged': False,
                      'send_event': False,
                      },
                     {'key': 'key4',
                      'value': 'value',
                      'id': 'test::Resource[agent1,key=key4],v=%d' % version,
                      'requires': ['test::Resource[agent1,key=key3],v=%d' % version],
                      'purged': False,
                      'send_event': False,
                      },
                     {'key': 'key5',
                      'value': 'value',
                      'id': 'test::Resource[agent1,key=key5],v=%d' % version,
                      'requires': ['test::Resource[agent1,key=key4],v=%d' % version,
                                   'test::Wait[agent1,key=key],v=%d' % version],
                      'purged': False,
                      'send_event': False,
                      }]
        return version, resources

    async def wait_for_resources(version, n):
        result = await client.get_version(env_id, version)
        assert result.code == 200

        while result.result["model"]["done"] < n:
            result = await client.get_version(env_id, version)
            await asyncio.sleep(0.1)
        assert result.result["model"]["done"] == n

    logger.info("setup done")

    version1, resources = make_version()
    result = await client.put_version(tid=env_id, version=version1, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    logger.info("first version pushed")

    # deploy and wait until one is ready
    result = await client.release_version(env_id, version1, True)
    assert result.code == 200

    logger.info("first version released")

    await wait_for_resources(version1, 2)

    logger.info("first version, 2 resources deployed")

    version2, resources = make_version(3)
    result = await client.put_version(tid=env_id, version=version2, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    logger.info("second version pushed %f", time.time())

    await asyncio.sleep(1)

    logger.info("wait to expire load limiting%f", time.time())

    # deploy and wait until done
    result = await client.release_version(env_id, version2, True)
    assert result.code == 200

    logger.info("second version released")

    await resource_container.wait_for_done_with_waiters(client, env_id, version2)

    logger.info("second version complete")

    result = await client.get_version(env_id, version2)
    assert result.code == 200
    for x in result.result["resources"]:
        assert x["status"] == const.ResourceState.deployed.name

    result = await client.get_version(env_id, version1)
    assert result.code == 200
    states = {x["id"]: x["status"] for x in result.result["resources"]}

    assert states['test::Wait[agent1,key=key],v=%d' % version1] == const.ResourceState.deployed.name
    assert states['test::Resource[agent1,key=key2],v=%d' % version1] == const.ResourceState.available.name
    assert states['test::Resource[agent1,key=key3],v=%d' % version1] == const.ResourceState.deployed.name
    assert states['test::Resource[agent1,key=key4],v=%d' % version1] == const.ResourceState.deployed.name
    assert states['test::Resource[agent1,key=key5],v=%d' % version1] == const.ResourceState.available.name


@pytest.mark.asyncio(timeout=15)
async def test_multi_instance(resource_container, client, server):
    """
       Test for multi threaded deploy
    """
    resource_container.Provider.reset()

    # setup project
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    # setup env
    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    # setup agent
    agent = Agent(hostname="node1", environment=env_id,
                  agent_map={"agent1": "localhost", "agent2": "localhost", "agent3": "localhost"},
                  code_loader=False, poolsize=1)
    agent.add_end_point_name("agent1")
    agent.add_end_point_name("agent2")
    agent.add_end_point_name("agent3")

    agent.start()

    # wait for agent
    await retry_limited(lambda: len(server.get_endpoint("session")._sessions) == 1, 10)

    # set the deploy environment
    resource_container.Provider.set("agent1", "key", "value")
    resource_container.Provider.set("agent2", "key", "value")
    resource_container.Provider.set("agent3", "key", "value")

    def make_version(offset=0):
        version = int(time.time() + offset)
        resources = []
        for agent in ["agent1", "agent2", "agent3"]:
            resources.extend([{'key': 'key',
                               'value': 'value',
                               'id': 'test::Wait[%s,key=key],v=%d' % (agent, version),
                               'requires': ['test::Resource[%s,key=key3],v=%d' % (agent, version)],
                               'purged': False,
                               'send_event': False,
                               },
                              {'key': 'key2',
                               'value': 'value',
                               'id': 'test::Resource[%s,key=key2],v=%d' % (agent, version),
                               'requires': ['test::Wait[%s,key=key],v=%d' % (agent, version)],
                               'purged': False,
                               'send_event': False,
                               },
                              {'key': 'key3',
                               'value': 'value',
                               'id': 'test::Resource[%s,key=key3],v=%d' % (agent, version),
                               'requires': [],
                               'purged': False,
                               'send_event': False,
                               },
                              {'key': 'key4',
                               'value': 'value',
                               'id': 'test::Resource[%s,key=key4],v=%d' % (agent, version),
                               'requires': ['test::Resource[%s,key=key3],v=%d' % (agent, version)],
                               'purged': False,
                               'send_event': False,
                               },
                              {'key': 'key5',
                               'value': 'value',
                               'id': 'test::Resource[%s,key=key5],v=%d' % (agent, version),
                               'requires': ['test::Resource[%s,key=key4],v=%d' % (agent, version),
                                            'test::Wait[%s,key=key],v=%d' % (agent, version)],
                               'purged': False,
                               'send_event': False,
                               }])
        return version, resources

    async def wait_for_resources(version, n):
        result = await client.get_version(env_id, version)
        assert result.code == 200

        def done_per_agent(result):
            done = [x for x in result.result["resources"] if x["status"] == "deployed"]
            peragent = groupby(done, lambda x: x["agent"])
            return {agent: len([x for x in grp]) for agent, grp in peragent}

        def mindone(result):
            alllist = done_per_agent(result).values()
            if(len(alllist) == 0):
                return 0
            return min(alllist)

        while mindone(result) < n:
            await asyncio.sleep(0.1)
            result = await client.get_version(env_id, version)
        assert mindone(result) >= n

    logger.info("setup done")

    version1, resources = make_version()
    result = await client.put_version(tid=env_id, version=version1, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    logger.info("first version pushed")

    # deploy and wait until one is ready
    result = await client.release_version(env_id, version1, True)
    assert result.code == 200

    logger.info("first version released")
    # timeout on single thread!
    await wait_for_resources(version1, 1)

    await resource_container.wait_for_done_with_waiters(client, env_id, version1)

    logger.info("first version complete")


@pytest.mark.asyncio
async def test_cross_agent_deps(resource_container, server, client):
    """
        deploy a configuration model with cross host dependency
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    # config for recovery mechanism
    Config.set("config", "agent-interval", "10")
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    agent2 = Agent(hostname="node2", environment=env_id, agent_map={"agent2": "localhost"}, code_loader=False)
    agent2.add_end_point_name("agent2")
    agent2.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 2, 10)

    resource_container.Provider.set("agent1", "key2", "incorrect_value")
    resource_container.Provider.set("agent1", "key3", "value")

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': ['test::Wait[agent1,key=key2],v=%d' % version, 'test::Resource[agent2,key=key3],v=%d' % version],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Wait[agent1,key=key2],v=%d' % version,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key3',
                  'value': 'value3',
                  'id': 'test::Resource[agent2,key=key3],v=%d' % version,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  },
                 {'key': 'key4',
                  'value': 'value4',
                  'id': 'test::Resource[agent2,key=key4],v=%d' % version,
                  'requires': [],
                  'purged': False,
                  'send_event': False,
                  }
                 ]

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(env_id, version, True)
    assert result.code == 200
    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 4
    assert result.result["model"]["result"] == const.VersionState.deploying.name

    result = await client.get_version(env_id, version)
    assert result.code == 200

    while result.result["model"]["done"] == 0:
        result = await client.get_version(env_id, version)
        await asyncio.sleep(0.1)

    result = await resource_container.wait_for_done_with_waiters(client, env_id, version)

    assert result.result["model"]["done"] == len(resources)
    assert result.result["model"]["result"] == const.VersionState.success.name

    assert resource_container.Provider.isset("agent1", "key1")
    assert resource_container.Provider.get("agent1", "key1") == "value1"
    assert resource_container.Provider.get("agent1", "key2") == "value2"
    assert resource_container.Provider.get("agent2", "key3") == "value3"

    agent.stop()
    agent2.stop()


@pytest.mark.asyncio(timeout=30)
async def test_dryrun_scale(resource_container, server, client):
    """
        test dryrun scaling
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    resources = []
    for i in range(1, 100):
        resources.append({'key': 'key%d' % i,
                          'value': 'value%d' % i,
                          'id': 'test::Resource[agent1,key=key%d],v=%d' % (i, version),
                          'purged': False,
                          'send_event': False,
                          'requires': [],
                          })

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # request a dryrun
    result = await client.dryrun_request(env_id, version)
    assert result.code == 200
    assert result.result["dryrun"]["total"] == len(resources)
    assert result.result["dryrun"]["todo"] == len(resources)

    # get the dryrun results
    result = await client.dryrun_list(env_id, version)
    assert result.code == 200
    assert len(result.result["dryruns"]) == 1

    while result.result["dryruns"][0]["todo"] > 0:
        result = await client.dryrun_list(env_id, version)
        await asyncio.sleep(0.1)

    dry_run_id = result.result["dryruns"][0]["id"]
    result = await client.dryrun_report(env_id, dry_run_id)
    assert result.code == 200

    agent.stop()


@pytest.mark.asyncio(timeout=30)
async def test_dryrun_failures(resource_container, server, client):
    """
        test dryrun scaling
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    result = await client.create_project("env-test")
    project_id = result.result["project"]["id"]

    result = await client.create_environment(project_id=project_id, name="dev")
    env_id = result.result["environment"]["id"]

    agent = Agent(hostname="node1", environment=env_id, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Noprov[agent1,key=key1],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': [],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::FailFast[agent1,key=key2],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': [],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::DoesNotExist[agent1,key=key2],v=%d' % version,
                  'purged': False,
                  'send_event': False,
                  'requires': [],
                  }
                 ]

    result = await client.put_version(tid=env_id, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # request a dryrun
    result = await client.dryrun_request(env_id, version)
    assert result.code == 200
    assert result.result["dryrun"]["total"] == len(resources)
    assert result.result["dryrun"]["todo"] == len(resources)

    # get the dryrun results
    result = await client.dryrun_list(env_id, version)
    assert result.code == 200
    assert len(result.result["dryruns"]) == 1

    while result.result["dryruns"][0]["todo"] > 0:
        result = await client.dryrun_list(env_id, version)
        print(result.result)
        await asyncio.sleep(0.1)

    dry_run_id = result.result["dryruns"][0]["id"]
    result = await client.dryrun_report(env_id, dry_run_id)
    assert result.code == 200

    resources = result.result["dryrun"]["resources"]

    def assert_handler_failed(resource, msg):
        changes = resources[resource]
        assert "changes" in changes
        changes = changes["changes"]
        assert "handler" in changes
        change = changes["handler"]
        assert change["current"] == "FAILED"
        assert change["desired"] == msg

    assert_handler_failed('test::Noprov[agent1,key=key1],v=%d' % version, "Unable to find a handler")
    assert_handler_failed('test::FailFast[agent1,key=key2],v=%d' % version, "Handler failed")
    assert_handler_failed('test::DoesNotExist[agent1,key=key2],v=%d' % version, "Resource Deserialization Failed")

    agent.stop()


@pytest.mark.asyncio
async def test_send_events(resource_container, environment, server, client):
    """
        Send and receive events within one agent
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    res_id_1 = 'test::Resource[agent1,key=key1],v=%d' % version
    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': res_id_1,
                  'send_event': False,
                  'purged': False,
                  'requires': ['test::Resource[agent1,key=key2],v=%d' % version],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                  'send_event': True,
                  'requires': [],
                  'purged': False,
                  }
                 ]

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(environment, version, True)
    assert result.code == 200

    result = await client.get_version(environment, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(0.1)

    events = resource_container.Provider.getevents("agent1", "key1")
    assert len(events) == 1
    for res_id, res in events[0].items():
        assert res_id.agent_name == "agent1"
        assert res_id.attribute_value == "key2"
        assert res["status"] == const.ResourceState.deployed
        assert res["change"] == const.Change.created

    agent.stop()


@pytest.mark.asyncio
async def test_send_events_cross_agent(resource_container, environment, server, client):
    """
        Send and receive events over agents
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    agent2 = Agent(hostname="node2", environment=environment, agent_map={"agent2": "localhost"}, code_loader=False)
    agent2.add_end_point_name("agent2")
    agent2.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 2, 10)

    version = int(time.time())

    res_id_1 = 'test::Resource[agent1,key=key1],v=%d' % version
    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': res_id_1,
                  'send_event': False,
                  'purged': False,
                  'requires': ['test::Resource[agent2,key=key2],v=%d' % version],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Resource[agent2,key=key2],v=%d' % version,
                  'send_event': True,
                  'requires': [],
                  'purged': False,
                  }
                 ]

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(environment, version, True)
    assert result.code == 200

    result = await client.get_version(environment, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(0.1)

    assert resource_container.Provider.get("agent1", "key1") == "value1"
    assert resource_container.Provider.get("agent2", "key2") == "value2"

    events = resource_container.Provider.getevents("agent1", "key1")
    assert len(events) == 1
    for res_id, res in events[0].items():
        assert res_id.agent_name == "agent2"
        assert res_id.attribute_value == "key2"
        assert res["status"] == const.ResourceState.deployed
        assert res["change"] == const.Change.created

    agent.stop()
    agent2.stop()


@pytest.mark.asyncio(timeout=15)
async def test_send_events_cross_agent_restart(resource_container, environment, server, client):
    """
        Send and receive events over agents with agents starting after deploy
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    agent2 = Agent(hostname="node2", environment=environment, agent_map={"agent2": "localhost"}, code_loader=False)
    agent2.add_end_point_name("agent2")
    agent2.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    res_id_1 = 'test::Resource[agent1,key=key1],v=%d' % version
    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': res_id_1,
                  'send_event': False,
                  'purged': False,
                  'requires': ['test::Resource[agent2,key=key2],v=%d' % version],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Resource[agent2,key=key2],v=%d' % version,
                  'send_event': True,
                  'requires': [],
                  'purged': False,
                  }
                 ]

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(environment, version, True)
    assert result.code == 200

    result = await client.get_version(environment, version)
    assert result.code == 200

    # wait for agent 2 to finish
    while (result.result["model"]["total"] - result.result["model"]["done"]) > 1:
        result = await client.get_version(environment, version)
        await asyncio.sleep(1)

    assert resource_container.Provider.get("agent2", "key2") == "value2"

    # start agent 1 and wait for it to finish
    Config.set("config", "agent-splay", "0")
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 2, 10)

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(1)

    assert resource_container.Provider.get("agent1", "key1") == "value1"

    events = resource_container.Provider.getevents("agent1", "key1")
    assert len(events) == 1
    for res_id, res in events[0].items():
        assert res_id.agent_name == "agent2"
        assert res_id.attribute_value == "key2"
        assert res["status"] == const.ResourceState.deployed
        assert res["change"] == const.Change.created

    agent.stop()
    agent2.stop()


@pytest.mark.asyncio
async def test_auto_deploy(server, client, resource_container, environment):
    """
        dryrun and deploy a configuration model automatically
    """
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"}, code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    resource_container.Provider.set("agent1", "key2", "incorrect_value")
    resource_container.Provider.set("agent1", "key3", "value")

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': ['test::Resource[agent1,key=key2],v=%d' % version],
                  },
                 {'key': 'key2',
                  'value': 'value2',
                  'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                  'send_event': False,
                  'requires': [],
                  'purged': False,
                  },
                 {'key': 'key3',
                  'value': None,
                  'id': 'test::Resource[agent1,key=key3],v=%d' % version,
                  'send_event': False,
                  'requires': [],
                  'purged': True,
                  }
                 ]

    # set auto deploy and push
    result = await client.set_setting(environment, data.AUTO_DEPLOY, True)
    assert result.code == 200
    result = await client.set_setting(environment, data.PUSH_ON_AUTO_DEPLOY, True)
    assert result.code == 200

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # check deploy
    result = await client.get_version(environment, version)
    assert result.code == 200
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 3
    assert result.result["model"]["result"] == "deploying"

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    assert resource_container.Provider.isset("agent1", "key1")
    assert resource_container.Provider.get("agent1", "key1") == "value1"
    assert resource_container.Provider.get("agent1", "key2") == "value2"
    assert not resource_container.Provider.isset("agent1", "key3")

    agent.stop()


@pytest.mark.asyncio(timeout=15)
async def test_auto_deploy_no_splay(server, client, resource_container, environment):
    """
        dryrun and deploy a configuration model automatically with agent autostart
    """
    resource_container.Provider.reset()
    env = await data.Environment.get_by_id(uuid.UUID(environment))
    await env.set(data.AUTOSTART_AGENT_MAP, {"agent1": ""})
    await env.set(data.AUTOSTART_ON_START, True)

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': ['test::Resource[agent1,key=key2],v=%d' % version],
                  },
                 ]

    # set auto deploy and push
    result = await client.set_setting(environment, data.AUTO_DEPLOY, True)
    assert result.code == 200
    result = await client.set_setting(environment, data.PUSH_ON_AUTO_DEPLOY, True)
    assert result.code == 200
    result = await client.set_setting(environment, data.AUTOSTART_SPLAY, 0)
    assert result.code == 200

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # check deploy
    result = await client.get_version(environment, version)
    assert result.code == 200
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 1
    assert result.result["model"]["result"] == "deploying"

    # check if agent 1 is started by the server
    # deploy will fail because handler code is not uploaded to the server
    result = await client.list_agents(tid=environment)
    assert result.code == 200

    while len(result.result["agents"]) == 0 or result.result["agents"][0]["state"] == "down":
        result = await client.list_agents(tid=environment)
        await asyncio.sleep(0.1)

    assert len(result.result["agents"]) == 1
    assert result.result["agents"][0]["name"] == "agent1"


@pytest.mark.asyncio(timeout=15)
async def test_autostart_mapping(server, client, resource_container, environment):
    """
        Test autostart mapping and restart agents when the map is modified
    """
    resource_container.Provider.reset()
    env = await data.Environment.get_by_id(uuid.UUID(environment))
    await env.set(data.AUTOSTART_AGENT_MAP, {"agent1": ""})
    await env.set(data.AUTO_DEPLOY, True)
    await env.set(data.PUSH_ON_AUTO_DEPLOY, True)
    await env.set(data.AUTOSTART_SPLAY, 0)
    await env.set(data.AUTOSTART_ON_START, True)

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': [],
                  },
                 {'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent2,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': [],
                  },
                 ]

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # check deploy
    result = await client.get_version(environment, version)
    assert result.code == 200
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 2
    assert result.result["model"]["result"] == "deploying"

    result = await client.list_agents(tid=environment)
    assert result.code == 200

    while len([x for x in result.result["agents"] if x["state"] == "up"]) < 1:
        result = await client.list_agents(tid=environment)
        await asyncio.sleep(0.1)

    assert len(result.result["agents"]) == 2
    assert len([x for x in result.result["agents"] if x["state"] == "up"]) == 1

    result = await client.set_setting(environment, data.AUTOSTART_AGENT_MAP, {"agent1": "", "agent2": ""})
    assert result.code == 200

    result = await client.list_agents(tid=environment)
    assert result.code == 200
    while len([x for x in result.result["agents"] if x["state"] == "up"]) < 2:
        result = await client.list_agents(tid=environment)
        await asyncio.sleep(0.1)


@pytest.mark.asyncio(timeout=15)
async def test_autostart_clear_environment(server_multi, client_multi, resource_container, environment):
    """
        Test clearing an environment with autostarted agents. After clearing, autostart should still work
    """
    resource_container.Provider.reset()
    env = await data.Environment.get_by_id(uuid.UUID(environment))
    await env.set(data.AUTOSTART_AGENT_MAP, {"agent1": ""})
    await env.set(data.AUTO_DEPLOY, True)
    await env.set(data.PUSH_ON_AUTO_DEPLOY, True)
    await env.set(data.AUTOSTART_SPLAY, 0)
    await env.set(data.AUTOSTART_ON_START, True)

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': [],
                  }
                 ]

    client = client_multi
    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # check deploy
    result = await client.get_version(environment, version)
    assert result.code == 200
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 1
    assert result.result["model"]["result"] == "deploying"

    result = await client.list_agents(tid=environment)
    assert result.code == 200

    while len([x for x in result.result["agents"] if x["state"] == "up"]) < 1:
        result = await client.list_agents(tid=environment)
        await asyncio.sleep(0.1)

    assert len(result.result["agents"]) == 1
    assert len([x for x in result.result["agents"] if x["state"] == "up"]) == 1

    # clear environment
    await client.clear_environment(environment)

    items = await data.ConfigurationModel.get_list()
    assert len(items) == 0
    items = await data.Resource.get_list()
    assert len(items) == 0
    items = await data.ResourceAction.get_list()
    assert len(items) == 0
    items = await data.Code.get_list()
    assert len(items) == 0
    items = await data.Agent.get_list()
    assert len(items) == 0
    items = await data.AgentInstance.get_list()
    assert len(items) == 0
    items = await data.AgentProcess.get_list()
    assert len(items) == 0

    # Do a deploy again
    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'send_event': False,
                  'purged': False,
                  'requires': [],
                  }
                 ]

    result = await client.put_version(tid=environment, version=version, resources=resources, unknowns=[], version_info={})
    assert result.code == 200

    # check deploy
    result = await client.get_version(environment, version)
    assert result.code == 200
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 1
    assert result.result["model"]["result"] == "deploying"

    result = await client.list_agents(tid=environment)
    assert result.code == 200

    while len([x for x in result.result["agents"] if x["state"] == "up"]) < 1:
        result = await client.list_agents(tid=environment)
        await asyncio.sleep(0.1)

    assert len(result.result["agents"]) == 1
    assert len([x for x in result.result["agents"] if x["state"] == "up"]) == 1


@pytest.mark.asyncio
async def test_export_duplicate(resource_container, snippetcompiler):
    """
        The exported should provide a compilation error when a resource is defined twice in a model
    """
    snippetcompiler.setup_for_snippet("""
        import test

        test::Resource(key="test", value="foo")
        test::Resource(key="test", value="bar")
    """)

    with pytest.raises(CompilerException) as exc:
        snippetcompiler.do_export()

    assert "exists more than once in the configuration model" in str(exc.value)


@pytest.mark.asyncio(timeout=90)
async def test_server_recompile(server_multi, client_multi, environment_multi):
    """
        Test a recompile on the server and verify recompile triggers
    """
    config.Config.set("server", "auto-recompile-wait", "0")
    client = client_multi
    server = server_multi
    environment = environment_multi

    async def wait_for_version(cnt):
        # Wait until the server is no longer compiling
        # wait for it to finish
        await asyncio.sleep(0.1)
        code = 200
        while code == 200:
            compiling = await client.is_compiling(environment)
            code = compiling.code
            await asyncio.sleep(0.1)
        # wait for it to appear
        versions = await client.list_versions(environment)

        while versions.result["count"] < cnt:
            logger.info(versions.result)
            versions = await client.list_versions(environment)
            await asyncio.sleep(0.1)

        return versions.result

    project_dir = os.path.join(server.get_endpoint("server")._server_storage["environments"], str(environment))
    project_source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "project")

    shutil.copytree(project_source, project_dir)
    subprocess.check_output(["git", "init"], cwd=project_dir)
    subprocess.check_output(["git", "add", "*"], cwd=project_dir)
    subprocess.check_output(["git", "config", "user.name", "Unit"], cwd=project_dir)
    subprocess.check_output(["git", "config", "user.email", "unit@test.example"], cwd=project_dir)
    subprocess.check_output(["git", "commit", "-m", "unit test"], cwd=project_dir)

    # add main.cf
    with open(os.path.join(project_dir, "main.cf"), "w") as fd:
        fd.write("""
        host = std::Host(name="test", os=std::linux)
        std::ConfigFile(host=host, path="/etc/motd", content="1234")
""")

    logger.info("request a compile")
    await client.notify_change(environment)

    logger.info("wait for 1")
    versions = await wait_for_version(1)
    assert versions["versions"][0]["total"] == 1
    assert versions["versions"][0]["version_info"]["export_metadata"]["type"] == "api"

    # get compile reports
    reports = await client.get_reports(environment)
    assert len(reports.result["reports"]) == 1

    # set a parameter without requesting a recompile
    await client.set_param(environment, id="param1", value="test", source="plugin")
    versions = await wait_for_version(1)
    assert versions["count"] == 1

    logger.info("request second compile")
    # set a new parameter and request a recompile
    await client.set_param(environment, id="param2", value="test", source="plugin", recompile=True)
    logger.info("wait for 2")
    versions = await wait_for_version(2)
    assert versions["versions"][0]["version_info"]["export_metadata"]["type"] == "param"
    assert versions["count"] == 2

    # update the parameter to the same value -> no compile
    await client.set_param(environment, id="param2", value="test", source="plugin", recompile=True)
    versions = await wait_for_version(2)
    assert versions["count"] == 2

    # update the parameter to a new value
    await client.set_param(environment, id="param2", value="test2", source="plugin", recompile=True)
    versions = await wait_for_version(3)
    logger.info("wait for 3")
    assert versions["count"] == 3


class ResourceProvider(object):

    def __init__(self, index, name, producer, state=None):
        self.name = name
        self.producer = producer
        self.state = state
        self.index = index

    def get_resource(self,
                     resource_container: ResourceContainer,
                     agent: str,
                     key: str,
                     version: str,
                     requires: List[str]) -> Tuple[Dict[str, str], Optional[ResourceState]]:
        base = {'key': key,
                'value': 'value1',
                'id': 'test::Resource[%s,key=%s],v=%d' % (agent, key, version),
                'send_event': True,
                'purged': False,
                'requires': requires,
                }

        self.producer(resource_container.Provider, agent, key)

        state = None
        if self.state is not None:
            state = ('test::Resource[%s,key=%s]' % (agent, key), self.state)

        return base, state

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name


# for events, self is the consuming node
# dep is the producer/required node
self_states = [
    ResourceProvider(0, "skip", lambda p, a, k:p.set_skip(a, k, 1)),
    ResourceProvider(1, "fail", lambda p, a, k:p.set_fail(a, k, 1)),
    ResourceProvider(2, "success", lambda p, a, k: None),
    ResourceProvider(3, "undefined", lambda p, a, k: None, const.ResourceState.undefined),
]

dep_states = [
    ResourceProvider(0, "skip", lambda p, a, k:p.set_skip(a, k, 1)),
    ResourceProvider(1, "fail", lambda p, a, k:p.set_fail(a, k, 1)),
    ResourceProvider(2, "success", lambda p, a, k: None),
]


def make_matrix(matrix, valueparser):
    """
    Expect matrix of the form

        header1    header2     header3
    row1    y    y    n
    """
    unparsed = [
        [v for v in row.split()][1:]
        for row in matrix.strip().split("\n")
    ][1:]

    return [[valueparser(nsv) for nsv in nv] for nv in unparsed]


# self state on X axis
# dep state on the Y axis
dorun = make_matrix("""
        skip    fail    success    undef
skip    n    n    n    n
fail    n    n    n    n
succ    y    y    y    n
""", lambda x: x == "y")

dochange = make_matrix("""
        skip    fail    success    undef
skip    n    n    n    n
fail    n    n    n    n
succ    n    n    y    n
""", lambda x: x == "y")

doevents = make_matrix("""
        skip    fail    success    undef
skip    2    2    2    0
fail    2    2    2    0
succ    2    2    2    0
""", lambda x: int(x))


@pytest.mark.parametrize("self_state", self_states, ids=lambda x: x.name)
@pytest.mark.parametrize("dep_state", dep_states, ids=lambda x: x.name)
@pytest.mark.asyncio
async def test_deploy_and_events(client, server, environment, resource_container, self_state, dep_state):

    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"},
                  code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    (dep, dep_status) = dep_state.get_resource(resource_container, "agent1", "key2", version, [])
    (own, own_status) = self_state.get_resource(resource_container, "agent1", "key3", version, [
        'test::Resource[agent1,key=key2],v=%d' % version, 'test::Resource[agent1,key=key1],v=%d' % version])

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'send_event': True,
                  'purged': False,
                  'requires': [],
                  },
                 dep,
                 own
                 ]

    status = {x[0]: x[1] for x in [dep_status, own_status] if x is not None}
    result = await client.put_version(tid=environment, version=version, resources=resources, resource_state=status,
                                      unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(environment, version, True)
    assert result.code == 200
    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 3
    assert result.result["model"]["result"] == "deploying"

    result = await client.get_version(environment, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    # verify against result matrices
    assert dorun[dep_state.index][self_state.index] == (resource_container.Provider.readcount("agent1", "key3") > 0)
    assert dochange[dep_state.index][self_state.index] == (resource_container.Provider.changecount("agent1", "key3") > 0)

    events = resource_container.Provider.getevents("agent1", "key3")
    expected_events = doevents[dep_state.index][self_state.index]
    if expected_events == 0:
        assert len(events) == 0
    else:
        assert len(events) == 1
        assert len(events[0]) == expected_events


@pytest.mark.asyncio
async def test_deploy_and_events_failed(client, server, environment, resource_container):
    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"},
                  code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    resources = [{'key': 'key1',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key1],v=%d' % version,
                  'send_event': True,
                  'purged': False,
                  'requires': [],
                  },
                 {'key': 'key2',
                  'value': 'value1',
                  'id': 'test::BadEvents[agent1,key=key2],v=%d' % version,
                  'send_event': True,
                  'purged': False,
                  'requires': ['test::Resource[agent1,key=key1],v=%d' % version],
                  },
                 ]

    result = await client.put_version(tid=environment, version=version, resources=resources, resource_state={},
                                      unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(environment, version, True)
    assert result.code == 200
    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 2
    assert result.result["model"]["result"] == "deploying"

    result = await client.get_version(environment, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)


dep_states_reload = [
    ResourceProvider(0, "skip", lambda p, a, k:p.set_skip(a, k, 1)),
    ResourceProvider(0, "fail", lambda p, a, k:p.set_fail(a, k, 1)),
    ResourceProvider(0, "nochange", lambda p, a, k: p.set(a, k, "value1")),
    ResourceProvider(1, "changed", lambda p, a, k: None)
]


@pytest.mark.parametrize("dep_state", dep_states_reload, ids=lambda x: x.name)
@pytest.mark.asyncio(timeout=5000)
async def test_reload(client, server, environment, resource_container, dep_state):

    agentmanager = server.get_endpoint(SLICE_AGENT_MANAGER)

    resource_container.Provider.reset()
    agent = Agent(hostname="node1", environment=environment, agent_map={"agent1": "localhost"},
                  code_loader=False)
    agent.add_end_point_name("agent1")
    agent.start()
    await retry_limited(lambda: len(agentmanager.sessions) == 1, 10)

    version = int(time.time())

    (dep, dep_status) = dep_state.get_resource(resource_container, "agent1", "key1", version, [])

    resources = [{'key': 'key2',
                  'value': 'value1',
                  'id': 'test::Resource[agent1,key=key2],v=%d' % version,
                  'send_event': True,
                  'purged': False,
                  'requires': ['test::Resource[agent1,key=key1],v=%d' % version],
                  },
                 dep
                 ]

    status = {x[0]: x[1] for x in [dep_status] if x is not None}
    result = await client.put_version(tid=environment, version=version, resources=resources, resource_state=status,
                                      unknowns=[], version_info={})
    assert result.code == 200

    # do a deploy
    result = await client.release_version(environment, version, True)
    assert result.code == 200
    assert not result.result["model"]["deployed"]
    assert result.result["model"]["released"]
    assert result.result["model"]["total"] == 2
    assert result.result["model"]["result"] == "deploying"

    result = await client.get_version(environment, version)
    assert result.code == 200

    while (result.result["model"]["total"] - result.result["model"]["done"]) > 0:
        result = await client.get_version(environment, version)
        await asyncio.sleep(0.1)

    assert result.result["model"]["done"] == len(resources)

    assert dep_state.index == resource_container.Provider.reloadcount("agent1", "key2")
