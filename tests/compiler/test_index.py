"""
    Copyright 2018 Inmanta

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
import pytest

import re

from inmanta.ast import IndexException
from inmanta.ast import NotFoundException, TypingException
from inmanta.ast import RuntimeException, DuplicateException, TypeNotFoundException
import inmanta.compiler as compiler


def test_issue_121_non_matching_index(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        """
        a=std::Host[name="test"]
        """
    )

    try:
        compiler.do_compile()
        raise AssertionError("Should get exception")
    except NotFoundException as e:
        assert e.location.lnr == 2


def test_issue_122_index_inheritance(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        """
entity Repository extends std::File:
    string name
    bool gpgcheck=false
    bool enabled=true
    string baseurl
    string gpgkey=""
    number metadata_expire=7200
    bool send_event=true
end

implementation redhatRepo for Repository:
    self.mode = 644
    self.owner = "root"
    self.group = "root"

    self.path = "/etc/yum.repos.d/{{ name }}.repo"
    self.content = "{{name}}"
end

implement Repository using redhatRepo

h1 = std::Host(name="test", os=std::linux)

Repository(host=h1, name="flens-demo",
                           baseurl="http://people.cs.kuleuven.be/~wouter.deborger/repo/")

Repository(host=h1, name="flens-demo",
                           baseurl="http://people.cs.kuleuven.be/~wouter.deborger/repo/")
        """
    )

    try:
        compiler.do_compile()
        raise AssertionError("Should get exception")
    except TypingException as e:
        assert e.location.lnr == 25


def test_issue_140_index_error(snippetcompiler):
    try:
        snippetcompiler.setup_for_snippet(
            """
        h = std::Host(name="test", os=std::linux)
        test = std::Service[host=h, path="test"]"""
        )
        compiler.do_compile()
        raise AssertionError("Should get exception")
    except NotFoundException as e:
        assert re.match(".*No index defined on std::Service for this lookup:.*", str(e))


def test_issue_745_index_on_nullable(snippetcompiler):
    with pytest.raises(IndexException):
        snippetcompiler.setup_for_snippet(
            """
entity A:
    string name
    string? opt
end

index A(name,opt)
"""
        )
        compiler.do_compile()


def test_issue_745_index_on_optional(snippetcompiler):
    with pytest.raises(IndexException):
        snippetcompiler.setup_for_snippet(
            """
entity A:
    string name
end

A.opt [0:1] -- A

index A(name,opt)
"""
        )
        compiler.do_compile()


def test_issue_745_index_on_multi(snippetcompiler):
    with pytest.raises(IndexException):
        snippetcompiler.setup_for_snippet(
            """
entity A:
    string name
end

A.opt [1:] -- A

index A(name,opt)
"""
        )
        compiler.do_compile()


def test_issue_index_on_not_existing(snippetcompiler):
    with pytest.raises(TypeNotFoundException):
        snippetcompiler.setup_for_snippet(
            """
index A(name)
"""
        )
        compiler.do_compile()


def test_issue_212_bad_index_defintion(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        """
entity Test1:
    string x
end
index Test1(x,y)
"""
    )
    with pytest.raises(RuntimeException):
        compiler.do_compile()


def test_index_on_subtype(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        """
        host = std::Host(name="a",os=std::linux)
        a=std::DefaultDirectory(host=host,path="/etc")
        b=std::DefaultDirectory(host=host,path="/etc")
    """
    )

    (_, scopes) = compiler.do_compile()

    root = scopes.get_child("__config__")
    a = root.lookup("a")
    b = root.lookup("b")

    assert a.get_value() == b.get_value()


def test_index_on_subtype2(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        """
        host = std::Host(name="a",os=std::linux)
        a=std::DefaultDirectory(host=host,path="/etc")
        b=std::Directory(host=host,path="/etc",mode=755 ,group="root",owner="root" )
    """
    )
    with pytest.raises(DuplicateException):
        compiler.do_compile()


diamond = """
entity A:
    string at = "a"
end
implement A using std::none

entity B:
    string at = "a"
end
implement B using std::none


entity C extends A,B:
end
implement C using std::none
"""


def test_index_on_subtype_diamond(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        diamond
        + """
    index A(at)
    index B(at)

    a = A(at="a")
    b = C(at="a")
    """
    )

    with pytest.raises(DuplicateException):
        compiler.do_compile()


def test_index_on_subtype_diamond_2(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        diamond
        + """
    index A(at)
    index B(at)

    a = A(at="a")
    b = B(at="a")
    """
    )
    compiler.do_compile()


def test_index_on_subtype_diamond_3(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        diamond
        + """
    index A(at)
    index B(at)

    a = A(at="a")
    b = B(at="ab")
    """
    )
    compiler.do_compile()


def test_index_on_subtype_diamond_4(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        diamond
        + """
    index A(at)
    index B(at)

    a = C(at="a")
    b = C(at="a")
    a=b
    """
    )
    (types, _) = compiler.do_compile()
    c = types["__config__::C"]
    assert len(c.get_indices()) == 1


def test_394_short_index(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        """implementation none for std::Entity:

end

entity Host:
    string name
    string blurp
end

entity File:
    string name
end

implement Host using none
implement File using none

Host host [1] -- [0:] File files

index Host(name)
index File(host, name)

h1 = Host(name="h1", blurp="blurp1")
f1h1=File(host=h1,name="f1")
f2h1=File(host=h1,name="f2")

z = h1.files[name="f1"]
"""
    )
    (_, scopes) = compiler.do_compile()
    root = scopes.get_child("__config__")
    z = root.lookup("z").get_value()
    f1h1 = root.lookup("f1h1").get_value()
    assert z is f1h1


def test_511_index_on_default(snippetcompiler):
    snippetcompiler.setup_for_snippet(
        """
entity Test:
    string a="a"
    string b
end

index Test(a, b)

implement Test using std::none

Test(b="b")
"""
    )
    compiler.do_compile()


def test_index_undefined_attribute(snippetcompiler):
    snippetcompiler.setup_for_error(
        """
        index std::Entity(foo, bar)
    """,
        "Attribute 'foo' referenced in index is not defined in entity std::Entity (reported in index "
        "std::Entity(foo, bar) ({dir}/main.cf:2))",
    )


def test_747_index_collisions(snippetcompiler):
    snippetcompiler.setup_for_error(
        """
        entity Test:
            string name
            string value
        end

        implementation none for Test:
        end

        implement Test using none

        index Test(name)
        Test(name="A", value="a")
        Test(name="A", value="b")

        """,
        """Could not set attribute `value` on instance `__config__::Test (instantiated at {dir}/main.cf:13,{dir}/main.cf:14)` (reported in Construct(Test) ({dir}/main.cf:14))
caused by:
  value set twice: 
\told value: a
\t\tset at {dir}/main.cf:13
\tnew value: b
\t\tset at {dir}/main.cf:14
 (reported in Construct(Test) ({dir}/main.cf:14))""",  # nopep8
    )


def test_747_index_collisions_invisible(snippetcompiler):
    snippetcompiler.setup_for_error(
        """
        entity Test:
            string name
            string value
        end

        implementation none for Test:
        end

        implement Test using none

        index Test(name)

        for v in ["a","b"]:
            Test(name="A", value=v)
        end

        """,
        """Could not set attribute `value` on instance `__config__::Test (instantiated at {dir}/main.cf:15,{dir}/main.cf:15)` (reported in Construct(Test) ({dir}/main.cf:15))
caused by:
  value set twice: 
\told value: a
\t\tset at {dir}/main.cf:15:34
\tnew value: b
\t\tset at {dir}/main.cf:15:34
 (reported in Construct(Test) ({dir}/main.cf:15))""",  # nopep8
    )
