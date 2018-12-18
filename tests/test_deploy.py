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
import os
import collections

from inmanta import deploy
from tornado import process, ioloop
import pytest


@pytest.mark.skip(reason="very unstable test")
@pytest.mark.asyncio(timeout=60)
async def test_deploy(snippetcompiler, tmpdir, mongo_db, motor):
    file_name = tmpdir.join("test_file")
    snippetcompiler.setup_for_snippet(
        """
    host = std::Host(name="test", os=std::linux)
    file = std::Symlink(host=host, source="/dev/null", target="%s")
    """
        % file_name
    )

    os.chdir(snippetcompiler.project_dir)
    Options = collections.namedtuple("Options", ["no_agent_log", "dryrun", "map", "agent"])
    options = Options(no_agent_log=False, dryrun=False, map="", agent="")

    run = deploy.Deploy(ioloop.IOLoop.current(), mongoport=mongo_db.port)
    try:
        run.run(options, only_setup=True)
        await run.do_deploy(False, "")
        assert file_name.exists()
    except (KeyboardInterrupt, deploy.FinishedException):
        # This is how the deploy command ends
        pass

    finally:
        run.stop()


@pytest.mark.asyncio(timeout=10)
async def test_fork(server):
    """
        This test should not fail. Some Subprocess'es can make the ioloop hang, this tests fails when that happens.
    """
    i = 0
    while i < 5:
        i += 1
        sub_process = process.Subprocess(["true"])
        await sub_process.wait_for_exit(raise_error=False)
