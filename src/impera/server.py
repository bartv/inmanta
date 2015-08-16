"""
    Copyright 2015 Impera

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Contact: bart@impera.io
"""

import datetime
import logging
import os
import difflib
from threading import RLock
import subprocess
import re
import threading

from mongoengine import connect, errors
from impera import methods
from impera import protocol
from impera import env
from impera import data
from impera.config import Config
from impera.loader import CodeLoader
from impera.resources import Id
import uuid
import tornado
from collections import defaultdict


LOGGER = logging.getLogger(__name__)


class Server(protocol.ServerEndpoint):
    """
        The central Impera server that communicates with clients and agents and persists configuration
        information

        :param code_loader Load code deployed from configuration modules
        :param usedb Use a database to store data. If not, only facts are persisted in a yaml file.
    """
    def __init__(self, code_loader=True, usedb=True):
        super().__init__("server", role="server")
        LOGGER.info("Starting server endpoint")
        self._server_storage = self.check_storage()
        self.check_keys()

        self._db = None
        if usedb:
            self._db = connect(Config.get("database", "name", "impera"), host=Config.get("database", "host", "localhost"))
            LOGGER.info("Connected to mongodb database")

        if code_loader:
            self._env = env.VirtualEnv(self._server_storage["env"])
            self._env.use_virtual_env()
            self._loader = CodeLoader(self._server_storage["code"])
        else:
            self._loader = None

        self._fact_expire = int(Config.get("config", "fact-expire", 3600))
        self.add_end_point_name(self.node_name)

        self._db_lock = RLock()

        self.schedule(self.renew_expired_facts, self._fact_expire)

        self._requests = defaultdict(dict)

    def check_keys(self):
        """
            Check if the ssh key(s) credentials of this server are configured properly
        """
        # TODO

    def check_storage(self):
        """
            Check if the server storage is configured and ready to use.
        """
        if "config" not in Config.get() or "state-dir" not in Config.get()["config"]:
            raise Exception("The Impera server requires a state directory to be configured")

        state_dir = Config.get()["config"]["state-dir"]

        if not os.path.exists(state_dir):
            os.mkdir(state_dir)

        server_state_dir = os.path.join(state_dir, "server")

        if not os.path.exists(server_state_dir):
            os.mkdir(server_state_dir)

        dir_map = {"server": server_state_dir}

        file_dir = os.path.join(server_state_dir, "files")
        dir_map["files"] = file_dir
        if not os.path.exists(file_dir):
            os.mkdir(file_dir)

        db_dir = os.path.join(server_state_dir, "database")
        dir_map["db"] = db_dir
        if not os.path.exists(db_dir):
            os.mkdir(db_dir)

        code_dir = os.path.join(server_state_dir, "code")
        dir_map["code"] = code_dir
        if not os.path.exists(code_dir):
            os.mkdir(code_dir)

        env_dir = os.path.join(server_state_dir, "env")
        dir_map["env"] = env_dir
        if not os.path.exists(env_dir):
            os.mkdir(env_dir)

        environments_dir = os.path.join(server_state_dir, "environments")
        dir_map["environments"] = environments_dir
        if not os.path.exists(environments_dir):
            os.mkdir(environments_dir)

        return dir_map

    def queue_request(self, environment, agent, request):
        """
            Queue a request for the agent in the given environment
        """
        LOGGER.debug("Queueing request for agent %s in environment %s", agent, environment)
        if agent not in self._requests[environment]:
            self._requests[environment][agent] = []

        self._requests[environment][agent].append(request)

    def _request_parameter(self, param):
        """
            Request the value of a parameter from an agent
        """
        resource_id = param.resource_id
        tid = str(param.environment.id)
        env = param.environment

        if resource_id is not None and resource_id != "":
            # get the latest version
            versions = data.ConfigurationModel.\
                objects(environment=env, release_status__gt=0).order_by("-version").limit(1)  # @UndefinedVariable

            if len(versions) == 0:
                return 404, {"message": "The environment associated with this parameter does not have any releases."}

            version = versions[0]

            # get the associated resource
            resources = data.Resource.objects(environment=env, resource_id=resource_id)  # @UndefinedVariable

            if len(resources) == 0:
                return 404, {"message": "The parameter does not exist."}

            resource = resources[0]

            # get a resource version
            rvs = data.ResourceVersion.objects(environment=env, model=version, resource=resource)  # @UndefinedVariable

            if len(rvs) == 0:
                return 404, {"message": "The parameter does not exist."}

            self.queue_request(tid, resource.agent, {"method": "fact", "resource_id": resource_id, "environment": tid,
                                                     "name": param.name, "resource": rvs[0].to_dict()})

            return 503, {"message": "Agents queried for resource parameter."}

        return 404, {"message": "The parameter does not exist."}

    def renew_expired_facts(self):
        """
            Send out requests to renew expired facts
        """
        LOGGER.info("Renewing expired parameters")

        updated_before = datetime.datetime.now() - datetime.timedelta(0, self._fact_expire)
        expired_params = data.Parameter.objects(updated__lt=updated_before)  # @UndefinedVariable

        for param in expired_params:
            LOGGER.debug("Requesting new parameter value for %s of resource %s in env %s", param.name, param.resource_id,
                         param.environment.id)
            self._request_parameter(param)

    @protocol.handle(methods.ParameterMethod.get_param)
    def get_param(self, tid, id, resource_id=None):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        params = data.Parameter.objects(environment=env, name=id, resource_id=resource_id)  # @UndefinedVariable

        if len(params) == 0:
            if resource_id is not None and resource_id != "":
                # get the latest version
                versions = data.ConfigurationModel.\
                    objects(environment=env, release_status__gt=0).order_by("-version").limit(1)  # @UndefinedVariable

                if len(versions) == 0:
                    return 404, {"message": "The parameter does not exist."}

                version = versions[0]

                # get the associated resource
                resources = data.Resource.objects(environment=env, resource_id=resource_id)  # @UndefinedVariable

                if len(resources) == 0:
                    return 404, {"message": "The parameter does not exist."}

                resource = resources[0]

                # get a resource version
                rvs = data.ResourceVersion.objects(environment=env, model=version, resource=resource)  # @UndefinedVariable

                if len(rvs) == 0:
                    return 404, {"message": "The parameter does not exist."}

                self.queue_request(tid, resource.agent, {"method": "fact", "resource_id": resource_id, "environment": tid,
                                                         "name": id, "resource": rvs[0].to_dict()})

                return 503, {"message": "Agents queried for resource parameter."}

            return 404, {"message": "The parameter does not exist."}

        param = params[0]
        # check if it was expired
        now = datetime.datetime.now()
        if (param.updated + datetime.timedelta(0, self._fact_expire)) > now:
            return 200, params[0].to_dict()

        return 410

    @protocol.handle(methods.ParameterMethod.set_param)
    def set_param(self, tid, id, source, value, resource_id=""):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        params = data.Parameter.objects(environment=env, name=id, resource_id=resource_id)  # @UndefinedVariable

        if len(params) == 0:
            param = data.Parameter(environment=env, name=id, resource_id=resource_id, value=value, source=source,
                                   updated=datetime.datetime.now())
            param.save()

        else:
            param = params[0]
            param.source = source
            param.value = value
            param.save()

        # check if the parameter is an unknown
        params = data.UnknownParameter.objects(environment=env, name=id, resource_id=resource_id)  # @UndefinedVariable
        if len(params) > 0:
            LOGGER.info("Received values for unknown parameters %s, triggering a recompile",
                        ", ".join([x.name for x in params]))
            for p in params:
                p.delete()

            threading.Thread(target=self._recompile_environment, args=(tid, False)).start()

        return 200, param.to_dict()

    @protocol.handle(methods.ParameterMethod.list_params)
    def list_param(self, tid):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        params = data.Parameter.objects(environment=env)  # @UndefinedVariable

        return_value = []
        for p in params:
            d = p.to_dict()
            del d["value"]
            return_value.append(d)

        return 200, return_value

    @protocol.handle(methods.FileMethod.upload_file)
    def upload_file(self, id, content):
        file_name = os.path.join(self._server_storage["files"], id)

        if os.path.exists(file_name):
            return 500, {"message": "A file with this id already exists."}

        with open(file_name, "wb+") as fd:
            fd.write(tornado.escape.utf8(content))

        return 200

    @protocol.handle(methods.FileMethod.stat_file)
    def stat_file(self, id):
        file_name = os.path.join(self._server_storage["files"], id)

        if os.path.exists(file_name):
            return 200
        else:
            return 404

    @protocol.handle(methods.FileMethod.get_file)
    def get_file(self, id):
        file_name = os.path.join(self._server_storage["files"], id)

        if not os.path.exists(file_name):
            return 404

        else:
            with open(file_name, "rb") as fd:
                return 200, {"content": fd.read().decode()}

    @protocol.handle(methods.FileMethod.stat_files)
    def stat_files(self, files):
        """
            Return which files in the list exist on the server
        """
        response = []
        for f in files:
            f_path = os.path.join(self._server_storage["files"], f)
            if not os.path.exists(f_path):
                response.append(f)

        return 200, {"files": response}

    @protocol.handle(methods.FileDiff.diff)
    def file_diff(self, operation, body):
        """
            Diff the two files identified with the two hashes
        """
        if body["a"] == "" or body["a"] == 0:
            a_lines = []
        else:
            a_path = os.path.join(self._server_storage["files"], body["a"])
            with open(a_path, "r") as fd:
                a_lines = fd.readlines()

        if body["b"] == "" or body["b"] == 0:
            b_lines = []
        else:
            b_path = os.path.join(self._server_storage["files"], body["b"])
            with open(b_path, "r") as fd:
                b_lines = fd.readlines()

        try:
            diff = difflib.unified_diff(a_lines, b_lines, fromfile=body["a"], tofile=body["b"])
        except FileNotFoundError:
            return 404

        return 200, "".join(diff)

    @protocol.handle(methods.HeartBeatMethod.heartbeat)
    def heartbeat(self, endpoint_names, nodename, role, interval, environment):
        try:
            env = data.Environment.objects().get(id=environment)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        now = datetime.datetime.now()
        try:
            node = data.Node.objects().get(hostname=nodename)  # @UndefinedVariable
            node.last_seen = now
            node.save()
        except errors.DoesNotExist:
            node = data.Node(hostname=nodename, last_seen=now)
            node.save()

        response = []
        for nh in endpoint_names:
            agent = data.Agent.objects(name=nh, node=node, environment=env)  # @UndefinedVariable
            if len(agent) == 0:
                agent = data.Agent(name=nh, node=node, role=role, interval=interval, last_seen=now, environment=env)
                agent.save()
            else:
                agent[0].interval = interval
                agent[0].last_seen = now
                agent[0].save()

            # check if there is something we need to push to the client
            if environment in self._requests and nh in self._requests[environment]:
                response.append({"items": self._requests[environment][nh], "agent": nh})
                del self._requests[environment][nh]

        return 200, {"requests": response, "environment": environment}

    @protocol.handle(methods.NodeMethod.get_agent)
    def get_agent(self, id):
        try:
            node = data.Node.objects().get(hostname=id)  # @UndefinedVariable
            return 200, {"node": node.to_dict(),
                         "agents": [a.to_dict() for a in node.agents]
                         }
        except errors.DoesNotExist:
            return 404

    @protocol.handle(methods.NodeMethod.list_agents)
    def list_agent(self):
        response = []
        for node in data.Node.objects():  # @UndefinedVariable
            agents = data.Agent.objects(node=node)  # @UndefinedVariable
            node_dict = node.to_dict()
            node_dict["agents"] = [{"environment": str(a.environment.id), "name": a.name, "role": a.role} for a in agents]

            response.append(node_dict)

        return 200, response

    @protocol.handle(methods.ResourceMethod.get_resource)
    def get_resource_state(self, tid, id, logs):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        resv = data.ResourceVersion.objects(environment=env, rid=id)  # @UndefinedVariable
        if len(resv) == 0:
            return 404, {"message": "The resource with the given id does not exist in the given environment"}

        ra = data.ResourceAction(resource_version=resv[0], action="pull", level="INFO", timestamp=datetime.datetime.now(),
                                 message="Individual resource version pulled by client")
        ra.save()

        action_list = []
        if bool(logs):
            actions = data.ResourceAction.objects(resource_version=resv[0])  # @UndefinedVariable
            for action in actions:
                action_list.append(action.to_dict())

        return 200, {"resource": resv[0].to_dict(), "logs": action_list}

    @protocol.handle(methods.ResourceMethod.get_resources_for_agent)
    def get_resources_for_agent(self, tid, agent):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        deploy_model = []
        versions = data.ConfigurationModel.\
            objects(environment=env, release_status__gt=0).order_by("-version").limit(1)  # @UndefinedVariable

        if len(versions) == 0:
            return 404

        cm = versions[0]

        resources = data.ResourceVersion.objects(environment=env, model=cm)  # @UndefinedVariable
        for rv in resources:
            if rv.resource.agent == agent:
                deploy_model.append(rv.to_dict())
                ra = data.ResourceAction(resource_version=rv, action="pull", level="INFO", timestamp=datetime.datetime.now(),
                                         message="Resource version pulled by client for agent %s state" % agent)
                ra.save()

        return 200, {"environment": tid, "agent": agent, "version": cm.version, "resources": deploy_model,
                     "release_status": data.RELEASE_STATUS[cm.release_status]}

    @protocol.handle(methods.CMVersionMethod.list_versions)
    def list_version(self, tid):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        models = data.ConfigurationModel.objects(environment=env).order_by("-version")  # @UndefinedVariable
        return 200, [m.to_dict() for m in models]

    @protocol.handle(methods.CMVersionMethod.get_version)
    def get_version(self, tid, id):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        try:
            version = data.ConfigurationModel.objects().get(version=id)  # @UndefinedVariable
            resources = data.ResourceVersion.objects(model=version)  # @UndefinedVariable

            return 200, {"model": version.to_dict(), "resources": [x.to_dict() for x in resources]}
        except errors.DoesNotExist:
            return 404, {"message": "The given configuration model does not exist yet."}

    @protocol.handle(methods.CMVersionMethod.put_version)
    def put_version(self, tid, version, resources, unknowns):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        try:
            data.ConfigurationModel.objects().get(version=version)  # @UndefinedVariable
            return 500, {"message": "The given version is already defined. Versions should be unique."}
        except errors.DoesNotExist:
            pass

        cm = data.ConfigurationModel(environment=env, version=version, date=datetime.datetime.now(),
                                     release_status=0)
        cm.save()

        for res_dict in resources:
            resource_obj = Id.parse_id(res_dict['id'])
            resource_id = resource_obj.resource_str()

            resource = data.Resource.objects(environment=env, resource_id=resource_id)  # @UndefinedVariable
            if len(resource) > 0:
                if len(resource) == 1:
                    resource = resource[0]

                else:
                    raise Exception("A resource id should be unique in an environment! (env=%s, resource=%s" %
                                    (tid, resource_id))

            else:
                resource = data.Resource(environment=env, resource_id=resource_id,
                                         resource_type=resource_obj.get_entity_type(),
                                         agent=resource_obj.get_agent_name(),
                                         attribute_name=resource_obj.get_attribute(),
                                         attribute_value=resource_obj.get_attribute_value())
                resource.save()

            attributes = {}
            for field, value in res_dict.items():
                if field != "id":
                    attributes[field] = value

            rv = data.ResourceVersion(environment=env, rid=res_dict['id'], resource=resource, model=cm, attributes=attributes)
            rv.save()

            ra = data.ResourceAction(resource_version=rv, action="store", level="INFO", timestamp=datetime.datetime.now())
            ra.save()

        for uk in unknowns:
            up = data.UnknownParameter(resource_id=uk["resource"], name=uk["parameter"], source=uk["source"], environment=env,
                                       version=version)
            up.save()

        LOGGER.debug("Successfully stored version %d" % version)

        return 200

    @protocol.handle(methods.CMVersionMethod.release_version)
    def release_version(self, tid, id, dryrun, push):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        models = data.ConfigurationModel.objects(environment=env, version=id)  # @UndefinedVariable
        if len(models) == 0:
            return 404, {"message": "The request version does not exist."}

        model = models[0]  # there can only be one per id/tid

        changed = False
        if dryrun:
            if model.release_status >= 1:
                return 500, {"message": "A dry run was already requested for this version."}
            else:
                model.release_status = 1
                model.save()
                changed = True

        else:
            if model.release_status >= 2:
                return 500, {"message": "A deploy was already requested for this version."}
            else:
                model.release_status = 2
                model.save()
                changed = True

        if push and changed:
            # fetch all resource in this cm and create a list of distinct agents
            rvs = data.ResourceVersion.objects(model=model, environment=env)  # @UndefinedVariable
            agents = set()
            for rv in rvs:
                agents.add(rv.resource.agent)

            for agent in agents:
                self.queue_request(tid, agent, {"method": "version", "version": id, "environment": tid})

        return 200, model.to_dict()

    @protocol.handle(methods.CodeMethod.upload_code)
    def upload_code(self, tid, id, sources, requires):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The provided environment id does not match an existing environment."}

        code = data.Code.objects(environment=env, version=str(id))  # @UndefinedVariable
        if len(code) > 0:
            return 500, {"message": "Code for this version has already been uploaded."}

        code = data.Code(environment=env, version=str(id), sources=sources, requires=requires)
        code.save()

        return 200

    @protocol.handle(methods.CodeMethod.get_code)
    def get_code(self, tid, id):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The provided environment id does not match an existing environment."}

        code = data.Code.objects(environment=env, version=str(id))  # @UndefinedVariable
        if len(code) == 0:
            return 404, {"message": "The version of the code does not exist."}

        return 200, {"version": id, "environment": tid, "sources": code[0].sources, "requires": code[0].requires}

    @protocol.handle(methods.ResourceMethod.resource_updated)
    def resource_updated(self, tid, id, level, action, message, extra_data):
        try:
            env = data.Environment.objects().get(id=tid)  # @UndefinedVariable
        except errors.DoesNotExist:
            return 404, {"message": "The given environment id does not exist!"}

        resv = data.ResourceVersion.objects(environment=env, rid=id)  # @UndefinedVariable
        if len(resv) == 0:
            return 404, {"message": "The resource with the given id does not exist in the given environment"}

        ra = data.ResourceAction(resource_version=resv[0], action=action, message=message, data=extra_data, level=level,
                                 timestamp=datetime.datetime.now())

        ra.save()

        return 200

    # Project handlers
    @protocol.handle(methods.Project.create_project)
    def create_project(self, name):
        try:
            project = data.Project(name=name, id=uuid.uuid4())
            project.save()
        except errors.NotUniqueError:
            return 500, {"message": "A project with name %s already exists." % name}

        return 200, project.to_dict()

    @protocol.handle(methods.Project.delete_project)
    def delete_project(self, id):
        try:
            # delete all environments first
            envs = data.Environment.objects(project=id)  # @UndefinedVariable
            for env in envs:
                self.delete_environment(env.id)

            # now delete the project itself
            project = data.Project.objects().get(id=id)  # @UndefinedVariable
            project.delete()
        except errors.DoesNotExist:
            return 404, {"message": "The project with given id does not exist."}

        return 200

    @protocol.handle(methods.Project.modify_project)
    def modify_project(self, id, name):
        try:
            project = data.Project.objects().get(id=id)  # @UndefinedVariable
            project.name = name
            project.save()

            return 200, project.to_dict()
        except errors.DoesNotExist:
            return 404, {"message": "The project with given id does not exist."}

        except errors.NotUniqueError:
            return 500, {"message": "A project with name %s already exists." % name}

        return 500

    @protocol.handle(methods.Project.list_projects)
    def list_projects(self):
        return 200, [x.to_dict() for x in data.Project.objects()]  # @UndefinedVariable

    @protocol.handle(methods.Project.get_project)
    def get_project(self, id):
        try:
            project = data.Project.objects().get(id=id)  # @UndefinedVariable
            environments = data.Environment.objects(project=project.id)  # @UndefinedVariable

            project_dict = project.to_dict()
            project_dict["environments"] = [str(e.id) for e in environments]

            return 200, project_dict
        except errors.DoesNotExist:
            return 404, {"message": "The project with given id does not exist."}

        return 500

    # Environment handlers
    @protocol.handle(methods.Environment.create_environment)
    def create_environment(self, project_id, name, repository, branch):
        if (repository is None and branch is not None) or (repository is not None and branch is None):
            return 500, {"message": "Repository and branch should be set together."}

        # fetch the project first
        try:
            project = data.Project.objects().get(id=project_id)  # @UndefinedVariable
            env = data.Environment(id=uuid.uuid4(), name=name, project=project)
            env.repo_url = repository
            env.repo_branch = branch
            env.save()

            return 200, env.to_dict()
        except errors.DoesNotExist:
            return 500, {"message": "The project id for the environment does not exist."}

        except errors.NotUniqueError:
            return 500, {"message": "Project %s (id=%s) already has an environment with name %s" %
                         (project.name, project.id, name)}

    @protocol.handle(methods.Environment.modify_environment)
    def modify_environment(self, id, name, repository, branch):
        try:
            env = data.Environment.objects().get(id=id)  # @UndefinedVariable
            env.name = name
            if repository is not None:
                env.repo_url = repository

            if branch is not None:
                env.repo_branch = branch

            env.save()

            return 200, env.to_dict()

        except errors.DoesNotExist:
            return 404, {"message": "The environment id does not exist."}

    @protocol.handle(methods.Environment.get_environment)
    def get_environment(self, id):
        try:
            env = data.Environment.objects().get(id=id)  # @UndefinedVariable
            env.save()

            return 200, env.to_dict()

        except errors.DoesNotExist:
            return 404, {"message": "The environment id does not exist."}

    @protocol.handle(methods.Environment.list_environments)
    def list_environments(self):
        return 200, [x.to_dict() for x in data.Environment.objects()]  # @UndefinedVariable

    @protocol.handle(methods.Environment.delete_environment)
    def delete_environment(self, id):
        try:
            # delete everything with a reference to this environment
            # delete the environment
            project = data.Environment.objects().get(id=id)  # @UndefinedVariable
            project.delete()
        except errors.DoesNotExist:
            return 404, {"message": "The environment with given id does not exist."}

        return 200

    @protocol.handle(methods.NotifyMethod.notify_change)
    def notify_change(self, id):
        LOGGER.info("Received change notification for environment %s", id)
        threading.Thread(target=self._recompile_environment, args=(id, True)).start()

        return 200

    def _recompile_environment(self, environment_id, update_repo=False):
        """
            Recompile an environment

            TODO: store logs
        """
        project_dir = os.path.join(self._server_storage["environments"], environment_id)

        try:
            env = data.Environment.objects().get(id=environment_id)  # @UndefinedVariable
        except errors.DoesNotExist:
            LOGGER.error("Environment %s does not exist.", environment_id)
            return

        if not os.path.exists(project_dir):
            LOGGER.info("Creating project directory for environment %s at %s", environment_id, project_dir)
            os.mkdir(project_dir)

        # checkout repo
        if not os.path.exists(os.path.join(project_dir, ".git")):
            LOGGER.info("Cloning repository into environment directory %s", project_dir)
            proc = subprocess.Popen(["git", "clone", env.repo_url, "."], cwd=project_dir)
            proc.wait()

        elif update_repo:
            LOGGER.info("Fetching changes from repo %s", env.repo_url)
            proc = subprocess.Popen(["git", "fetch", env.repo_url], cwd=project_dir)
            proc.wait()

        # verify if branch is correct
        proc = subprocess.Popen(["git", "branch"], cwd=project_dir, stdout=subprocess.PIPE)
        out, _ = proc.communicate()

        o = re.search("\* ([^\s]+)$", out.decode(), re.MULTILINE)
        branch_name = o.group(1)

        if env.repo_branch != branch_name:
            LOGGER.info("Repository is at %s branch, switching to %s", branch_name, env.repo_branch)
            proc = subprocess.Popen(["git", "checkout", env.repo_branch], cwd=project_dir)
            proc.wait()

        proc = subprocess.Popen(["git", "pull"], cwd=project_dir)
        proc.wait()

        LOGGER.info("Installing and updating modules")
        subprocess.Popen(["impera", "modules", "install"], cwd=project_dir).wait()
        subprocess.Popen(["impera", "modules", "update"], cwd=project_dir).wait()

        LOGGER.info("Recompiling configuration model")
        proc = subprocess.Popen(["impera", "export", "-e", environment_id, "--server_address", "localhost",
                                 "--server_port", "8888"], cwd=project_dir)
        proc.wait()