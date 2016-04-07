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

from abc import ABCMeta, abstractmethod
import re

from impera.ast.variables import Variable, Reference, AttributeVariable
from impera.ast.statements import ReferenceStatement, Literal
from impera.execute.proxy import DynamicProxy
import impera.ast.statements.call
from impera.execute.runtime import ResultVariable


def create_function(expression):
    """
        Function that returns a function that evaluates the given expression.
        The generated function accepts the unbound variables in the expression
        as arguments.
    """
    def function(*args, **kwargs):
        """
            A function that evaluates the expression
        """
        if len(args) != 1:
            raise NotImplementedError()

        return expression.execute({'self': args[0]}, None, None)

    return function


class InvalidNumberOfArgumentsException(Exception):
    """
        This exception is raised if an invalid amount of arguments is passed
        to an operator.
    """

    def __init__(self, msg):
        Exception.__init__(self, msg)


class UnboundVariableException(Exception):
    """
        This execption is raised if an expression is evaluated when not all
        variables have been resolved
    """

    def __init__(self, msg):
        Exception.__init__(self, msg)


class OpMetaClass(ABCMeta):
    """
        This metaclass registers a class with the operator class if it contains
        a string that specifies the op it is able to handle. This metaclass
        only makes sense for subclasses of the Operator class.
    """

    def __init__(self, name, bases, attr_dict):
        attribute = "_%s__op" % name
        if attribute in attr_dict:
            Operator.register_operator(attr_dict[attribute], self)
        super(OpMetaClass, self).__init__(name, bases, attr_dict)


class Operator(ReferenceStatement, metaclass=OpMetaClass):
    """
        This class is an abstract base class for all operators that can be used in expressions
    """
    # A hash to lookup each handler
    __operator = {}

    @classmethod
    def register_operator(cls, operator_string, operator_class):
        """
            Register a new operator
        """
        cls.__operator[operator_string] = operator_class

    @classmethod
    def get_operator_class(cls, oper):
        """
            Get the class that implements the given operator. Returns none of the operator does not exist
        """
        if oper in cls.__operator:
            return cls.__operator[oper]

        return None

    def __init__(self, name, children):
        self.__number_arguments = len(children)
        self._arguments = children
        ReferenceStatement.__init__(self, self._arguments)
        self.__name = name

    def execute(self, requires, resolver, queue):
        return self._op([x.execute(requires, resolver, queue) for x in self._arguments])

    @abstractmethod
    def _op(self, args):
        """
            Abstract method that implements the operator
        """

    def __repr__(self):
        """
            Return a representation of the op
        """
        arg_list = []
        for arg in self._arguments:
            arg_list.append(str(arg))
        return "%s(%s)" % (self.__class__.__name__, ", ".join(arg_list))

    def to_function(self):
        """
            Returns a function that represents this expression
        """
        return create_function(self)


class BinaryOperator(Operator):
    """
        This class represents a binary operator.
    """

    def __init__(self, name, op1, op2):
        Operator.__init__(self, name, [op1, op2])

    def _op(self, args):
        """
            The method that needs to be implemented for this operator
        """
        # pylint: disable-msg=W0142
        return self._bin_op(*args)

    @abstractmethod
    def _bin_op(self, arg1, arg2):
        """
            The implementation of the binary op
        """


class UnaryOperator(Operator):
    """
        This class represents a unary operator
    """

    def __init__(self, name, op1):
        Operator.__init__(self, name, [op1])

    def _op(self, args):
        """
            This method calls the implementation of the operator
        """
        # pylint: disable-msg=W0142
        return self._un_op(*args)

    @abstractmethod
    def _un_op(self, arg):
        """
            The implementation of the operator
        """


class Not(UnaryOperator):
    """
        The negation operator
    """
    __op = "not"

    def __init__(self, arg):
        UnaryOperator.__init__(self, "negation", arg)

    def _un_op(self, arg):
        """
            Return the inverse of the argument

            @see Operator#_op
        """
        return not arg


class Regex(BinaryOperator):
    """
        An operator that does regex matching
    """

    def __init__(self, op1, op2):
        regex = re.compile(op2)
        BinaryOperator.__init__(self, "regex", op1, Literal(regex))

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        if not isinstance(arg1, str):
            raise Exception("Regex can only be match with strings. %s is of type %s" % arg1)

        return arg2.match(arg1) is not None

    def __repr__(self):
        """
            Return a representation of the op
        """
        return "%s(%s, %s)" % (self.__class__.__name__, self._arguments[0],
                               self._arguments[1].value)


class Equals(BinaryOperator):
    """
        The equality operator
    """
    __op = "=="

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "equality", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        return arg1 == arg2


class LessThan(BinaryOperator):
    """
        The less than operator
    """
    __op = "<"

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "less than", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        if not isinstance(arg1, (int, float)) or not isinstance(arg2, (int, float)):
            raise Exception("Can only compare numbers.")
        return arg1 < arg2


class GreaterThan(BinaryOperator):
    """
        The more than operator
    """
    __op = ">"

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "greater than", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        if not isinstance(arg1, (int, float)) or not isinstance(arg2, (int, float)):
            raise Exception("Can only compare numbers.")
        return arg1 > arg2


class LessThanOrEqual(BinaryOperator):
    """
        The less than or equal operator
    """
    __op = "<="

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "less than or equal", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        if not isinstance(arg1, (int, float)) or not isinstance(arg2, (int, float)):
            raise Exception("Can only compare numbers.")
        return arg1 <= arg2


class GreaterThanOrEqual(BinaryOperator):
    """
        The more than or equal operator
    """
    __op = ">="

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "greater than or equal", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        if not isinstance(arg1, (int, float)) or not isinstance(arg2, (int, float)):
            raise Exception("Can only compare numbers.")
        return arg1 >= arg2


class NotEqual(BinaryOperator):
    """
        The not equal operator
    """
    __op = "!="

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "not equal", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        return arg1 != arg2


class And(BinaryOperator):
    """
        The and boolean operator
    """
    __op = "and"

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "and", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        if not isinstance(arg1, bool) or not isinstance(arg2, bool):
            raise Exception("Unable to 'and' two types that are not bool.")

        return arg1 and arg2


class Or(BinaryOperator):
    """
        The or boolean operator
    """
    __op = "or"

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "or", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        if not isinstance(arg1, bool) or not isinstance(arg2, bool):
            raise Exception("Unable to 'or' two types that are not bool.")

        return arg1 or arg2


class In(BinaryOperator):
    """
        The in operator for iterable types
    """
    __op = "in"

    def __init__(self, op1, op2):
        BinaryOperator.__init__(self, "in", op1, op2)

    def _bin_op(self, arg1, arg2):
        """
            @see Operator#_op
        """
        if not (isinstance(arg2, list) or (hasattr(arg2, "type") and arg2.type() == list)):
            raise Exception("Operand two of 'in' can only be a list (%s)" % arg2[0])

        for arg in arg2:
            if arg == arg1:
                return True

        return False
