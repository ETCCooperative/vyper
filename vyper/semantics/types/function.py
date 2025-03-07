import re
import warnings
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple

from vyper import ast as vy_ast
from vyper.ast.validation import validate_call_args
from vyper.exceptions import (
    ArgumentException,
    CallViolation,
    CompilerPanic,
    FunctionDeclarationException,
    InvalidType,
    NamespaceCollision,
    StateAccessViolation,
    StructureException,
)
from vyper.semantics.analysis.base import (
    DataLocation,
    FunctionVisibility,
    StateMutability,
    StorageSlot,
)
from vyper.semantics.analysis.utils import check_kwargable, validate_expected_type
from vyper.semantics.namespace import get_namespace
from vyper.semantics.types.base import KwargSettings, VyperType
from vyper.semantics.types.primitives import UINT256_T, BoolT
from vyper.semantics.types.subscriptable import TupleT
from vyper.semantics.types.utils import type_from_abi, type_from_annotation
from vyper.utils import keccak256


class ContractFunction(VyperType):
    """
    Contract function type.

    Functions compare false against all types and so cannot be assigned without
    being called. Calls are validated by `fetch_call_return`, check the call
    arguments against `arguments`, and return `return_type`.

    Attributes
    ----------
    name : str
        The name of the function.
    arguments : OrderedDict
        Function input arguments as {'name': VyperType}
    min_arg_count : int
        The minimum number of required input arguments.
    max_arg_count : int
        The maximum number of required input arguments. When a function has no
        default arguments, this value is the same as `min_arg_count`.
    kwarg_keys : List
        List of optional input argument keys.
    function_visibility : FunctionVisibility
        enum indicating the external visibility of a function.
    state_mutability : StateMutability
        enum indicating the authority a function has to mutate it's own state.
    nonreentrant : str
        Re-entrancy lock name.
    """

    _is_callable = True

    def __init__(
        self,
        name: str,
        arguments: OrderedDict,
        # TODO rename to something like positional_args, keyword_args
        min_arg_count: int,
        max_arg_count: int,
        return_type: Optional[VyperType],
        function_visibility: FunctionVisibility,
        state_mutability: StateMutability,
        nonreentrant: Optional[str] = None,
    ) -> None:
        super().__init__()

        self.name = name
        self.arguments = arguments
        self.min_arg_count = min_arg_count
        self.max_arg_count = max_arg_count
        self.return_type = return_type
        self.kwarg_keys = []
        if min_arg_count < max_arg_count:
            self.kwarg_keys = list(self.arguments)[min_arg_count:]
        self.visibility = function_visibility
        self.mutability = state_mutability
        self.nonreentrant = nonreentrant

        # a list of internal functions this function calls
        self.called_functions: Set["ContractFunction"] = set()

        # special kwargs that are allowed in call site
        self.call_site_kwargs = {
            "gas": KwargSettings(UINT256_T, "gas"),
            "value": KwargSettings(UINT256_T, 0),
            "skip_contract_check": KwargSettings(BoolT(), False, require_literal=True),
            "default_return_value": KwargSettings(return_type, None),
        }

    def __repr__(self):
        arg_types = ",".join(repr(a) for a in self.arguments.values())
        return f"contract function {self.name}({arg_types})"

    # override parent implementation. function type equality does not
    # make too much sense.
    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return hash(id(self))

    @classmethod
    def from_abi(cls, abi: Dict) -> "ContractFunction":
        """
        Generate a `ContractFunction` object from an ABI interface.

        Arguments
        ---------
        abi : dict
            An object from a JSON ABI interface, representing a function.

        Returns
        -------
        ContractFunction object.
        """

        arguments = OrderedDict()
        for item in abi["inputs"]:
            arguments[item["name"]] = type_from_abi(item)
        return_type = None
        if len(abi["outputs"]) == 1:
            return_type = type_from_abi(abi["outputs"][0])
        elif len(abi["outputs"]) > 1:
            return_type = TupleT(tuple(type_from_abi(i) for i in abi["outputs"]))
        return cls(
            abi["name"],
            arguments,
            len(arguments),
            len(arguments),
            return_type,
            function_visibility=FunctionVisibility.EXTERNAL,
            state_mutability=StateMutability.from_abi(abi),
        )

    @classmethod
    def from_FunctionDef(
        cls, node: vy_ast.FunctionDef, is_interface: Optional[bool] = False
    ) -> "ContractFunction":
        """
        Generate a `ContractFunction` object from a `FunctionDef` node.

        Arguments
        ---------
        node : FunctionDef
            Vyper ast node to generate the function definition from.
        is_interface: bool, optional
            Boolean indicating if the function definition is part of an interface.

        Returns
        -------
        ContractFunction
        """
        kwargs: Dict[str, Any] = {}
        if is_interface:
            # FunctionDef with stateMutability in body (Interface defintions)
            if (
                len(node.body) == 1
                and isinstance(node.body[0], vy_ast.Expr)
                and isinstance(node.body[0].value, vy_ast.Name)
                and StateMutability.is_valid_value(node.body[0].value.id)
            ):
                # Interfaces are always public
                kwargs["function_visibility"] = FunctionVisibility.EXTERNAL
                kwargs["state_mutability"] = StateMutability(node.body[0].value.id)
            elif len(node.body) == 1 and node.body[0].get("value.id") in ("constant", "modifying"):
                if node.body[0].value.id == "constant":
                    expected = "view or pure"
                else:
                    expected = "payable or nonpayable"
                raise StructureException(
                    f"State mutability should be set to {expected}", node.body[0]
                )
            else:
                raise StructureException(
                    "Body must only contain state mutability label", node.body[0]
                )

        else:

            # FunctionDef with decorators (normal functions)
            for decorator in node.decorator_list:

                if isinstance(decorator, vy_ast.Call):
                    if "nonreentrant" in kwargs:
                        raise StructureException(
                            "nonreentrant decorator is already set with key: "
                            f"{kwargs['nonreentrant']}",
                            node,
                        )

                    if decorator.get("func.id") != "nonreentrant":
                        raise StructureException("Decorator is not callable", decorator)
                    if len(decorator.args) != 1 or not isinstance(decorator.args[0], vy_ast.Str):
                        raise StructureException(
                            "@nonreentrant name must be given as a single string literal", decorator
                        )

                    if node.name == "__init__":
                        msg = "Nonreentrant decorator disallowed on `__init__`"
                        raise FunctionDeclarationException(msg, decorator)

                    kwargs["nonreentrant"] = decorator.args[0].value

                elif isinstance(decorator, vy_ast.Name):
                    if FunctionVisibility.is_valid_value(decorator.id):
                        if "function_visibility" in kwargs:
                            raise FunctionDeclarationException(
                                f"Visibility is already set to: {kwargs['function_visibility']}",
                                node,
                            )
                        kwargs["function_visibility"] = FunctionVisibility(decorator.id)

                    elif StateMutability.is_valid_value(decorator.id):
                        if "state_mutability" in kwargs:
                            raise FunctionDeclarationException(
                                f"Mutability is already set to: {kwargs['state_mutability']}", node
                            )
                        kwargs["state_mutability"] = StateMutability(decorator.id)

                    else:
                        if decorator.id == "constant":
                            warnings.warn(
                                "'@constant' decorator has been removed (see VIP2040). "
                                "Use `@view` instead.",
                                DeprecationWarning,
                            )
                        raise FunctionDeclarationException(
                            f"Unknown decorator: {decorator.id}", decorator
                        )

                else:
                    raise StructureException("Bad decorator syntax", decorator)

        if "function_visibility" not in kwargs:
            raise FunctionDeclarationException(
                f"Visibility must be set to one of: {', '.join(FunctionVisibility.values())}", node
            )

        if node.name == "__default__":
            if kwargs["function_visibility"] != FunctionVisibility.EXTERNAL:
                raise FunctionDeclarationException(
                    "Default function must be marked as `@external`", node
                )
            if node.args.args:
                raise FunctionDeclarationException(
                    "Default function may not receive any arguments", node.args.args[0]
                )

        if "state_mutability" not in kwargs:
            # Assume nonpayable if not set at all (cannot accept Ether, but can modify state)
            kwargs["state_mutability"] = StateMutability.NONPAYABLE

        if kwargs["state_mutability"] == StateMutability.PURE and "nonreentrant" in kwargs:
            raise StructureException("Cannot use reentrancy guard on pure functions", node)

        if node.name == "__init__":
            if (
                kwargs["state_mutability"] in (StateMutability.PURE, StateMutability.VIEW)
                or kwargs["function_visibility"] == FunctionVisibility.INTERNAL
            ):
                raise FunctionDeclarationException(
                    "Constructor cannot be marked as `@pure`, `@view` or `@internal`", node
                )

            # call arguments
            if node.args.defaults:
                raise FunctionDeclarationException(
                    "Constructor may not use default arguments", node.args.defaults[0]
                )

        arguments = OrderedDict()
        max_arg_count = len(node.args.args)
        min_arg_count = max_arg_count - len(node.args.defaults)
        defaults = [None] * min_arg_count + node.args.defaults

        namespace = get_namespace()
        for arg, value in zip(node.args.args, defaults):
            if arg.arg in ("gas", "value", "skip_contract_check", "default_return_value"):
                raise ArgumentException(
                    f"Cannot use '{arg.arg}' as a variable name in a function input", arg
                )
            if arg.arg in arguments:
                raise ArgumentException(f"Function contains multiple inputs named {arg.arg}", arg)
            if arg.arg in namespace:
                raise NamespaceCollision(arg.arg, arg)

            if arg.annotation is None:
                raise ArgumentException(f"Function argument '{arg.arg}' is missing a type", arg)

            type_ = type_from_annotation(arg.annotation)

            if value is not None:
                if not check_kwargable(value):
                    raise StateAccessViolation(
                        "Value must be literal or environment variable", value
                    )
                validate_expected_type(value, type_)

            arguments[arg.arg] = type_

        # return types
        if node.returns is None:
            return_type = None
        elif node.name == "__init__":
            raise FunctionDeclarationException(
                "Constructor may not have a return type", node.returns
            )
        elif isinstance(node.returns, (vy_ast.Name, vy_ast.Call, vy_ast.Subscript)):
            return_type = type_from_annotation(node.returns)
        elif isinstance(node.returns, vy_ast.Tuple):
            tuple_types: Tuple = ()
            for n in node.returns.elements:
                tuple_types += (type_from_annotation(n),)
            return_type = TupleT(tuple_types)
        else:
            raise InvalidType("Function return value must be a type name or tuple", node.returns)

        return cls(node.name, arguments, min_arg_count, max_arg_count, return_type, **kwargs)

    def set_reentrancy_key_position(self, position: StorageSlot) -> None:
        if hasattr(self, "reentrancy_key_position"):
            raise CompilerPanic("Position was already assigned")
        if self.nonreentrant is None:
            raise CompilerPanic(f"No reentrant key {self}")
        # sanity check even though implied by the type
        if position._location != DataLocation.STORAGE:
            raise CompilerPanic("Non-storage reentrant key")
        self.reentrancy_key_position = position

    @classmethod
    def getter_from_VariableDecl(cls, node: vy_ast.VariableDecl) -> "ContractFunction":
        """
        Generate a `ContractFunction` object from an `VariableDecl` node.

        Used to create getter functions for public variables.

        Arguments
        ---------
        node : VariableDecl
            Vyper ast node to generate the function definition from.

        Returns
        -------
        ContractFunction
        """
        if not node.is_public:
            raise CompilerPanic("getter generated for non-public function")
        type_ = type_from_annotation(node.annotation)
        arguments, return_type = type_.getter_signature
        args_dict: OrderedDict = OrderedDict()
        for item in arguments:
            args_dict[f"arg{len(args_dict)}"] = item

        return cls(
            node.target.id,
            args_dict,
            len(arguments),
            len(arguments),
            return_type,
            function_visibility=FunctionVisibility.EXTERNAL,
            state_mutability=StateMutability.VIEW,
        )

    @property
    # convenience property for compare_signature, as it would
    # appear in a public interface
    def _iface_sig(self) -> Tuple[Tuple, Optional[VyperType]]:
        return tuple(self.arguments.values()), self.return_type

    def compare_signature(self, other: "ContractFunction") -> bool:
        """
        Compare the signature of this function with another function.

        Used when determining if an interface has been implemented. This method
        should not be directly implemented by any inherited classes.
        """

        if not self.is_external:
            return False

        arguments, return_type = self._iface_sig
        other_arguments, other_return_type = other._iface_sig

        if len(arguments) != len(other_arguments):
            return False
        for atyp, btyp in zip(arguments, other_arguments):
            if not atyp.compare_type(btyp):
                return False
        if return_type and not return_type.compare_type(other_return_type):  # type: ignore
            return False

        return True

    @property
    def is_external(self) -> bool:
        return self.visibility == FunctionVisibility.EXTERNAL

    @property
    def is_internal(self) -> bool:
        return self.visibility == FunctionVisibility.INTERNAL

    @property
    def method_ids(self) -> Dict[str, int]:
        """
        Dict of `{signature: four byte selector}` for this function.

        * For functions without default arguments the dict contains one item.
        * For functions with default arguments, there is one key for each
          function signature.
        """
        arg_types = [i.canonical_abi_type for i in self.arguments.values()]

        if not self.has_default_args:
            return _generate_method_id(self.name, arg_types)

        method_ids = {}
        for i in range(self.min_arg_count, self.max_arg_count + 1):
            method_ids.update(_generate_method_id(self.name, arg_types[:i]))
        return method_ids

    # for caller-fills-args calling convention
    def get_args_buffer_offset(self) -> int:
        """
        Get the location of the args buffer in the function frame (caller sets)
        """
        return 0

    # TODO is this needed?
    def get_args_buffer_len(self) -> int:
        """
        Get the length of the argument buffer in the function frame
        """
        return sum(arg_t.size_in_bytes() for arg_t in self.arguments.values())

    @property
    def is_constructor(self) -> bool:
        return self.name == "__init__"

    @property
    def is_fallback(self) -> bool:
        return self.name == "__default__"

    @property
    def has_default_args(self) -> bool:
        return self.min_arg_count < self.max_arg_count

    def fetch_call_return(self, node: vy_ast.Call) -> Optional[VyperType]:
        if node.get("func.value.id") == "self" and self.visibility == FunctionVisibility.EXTERNAL:
            raise CallViolation("Cannot call external functions via 'self'", node)

        # for external calls, include gas and value as optional kwargs
        kwarg_keys = self.kwarg_keys.copy()
        if node.get("func.value.id") != "self":
            kwarg_keys += list(self.call_site_kwargs.keys())
        validate_call_args(node, (self.min_arg_count, self.max_arg_count), kwarg_keys)

        if self.mutability < StateMutability.PAYABLE:
            kwarg_node = next((k for k in node.keywords if k.arg == "value"), None)
            if kwarg_node is not None:
                raise CallViolation("Cannot send ether to nonpayable function", kwarg_node)

        for arg, expected in zip(node.args, self.arguments.values()):
            validate_expected_type(arg, expected)

        # TODO this should be moved to validate_call_args
        for kwarg in node.keywords:
            if kwarg.arg in self.call_site_kwargs:
                kwarg_settings = self.call_site_kwargs[kwarg.arg]
                if kwarg.arg == "default_return_value" and self.return_type is None:
                    raise ArgumentException(
                        f"`{kwarg.arg}=` specified but {self.name}() does not return anything",
                        kwarg.value,
                    )
                validate_expected_type(kwarg.value, kwarg_settings.typ)
                if kwarg_settings.require_literal:
                    if not isinstance(kwarg.value, vy_ast.Constant):
                        raise InvalidType(
                            f"{kwarg.arg} must be literal {kwarg_settings.typ}", kwarg.value
                        )
            else:
                # Generate the modified source code string with the kwarg removed
                # as a suggestion to the user.
                kwarg_pattern = rf"{kwarg.arg}\s*=\s*{re.escape(kwarg.value.node_source_code)}"
                modified_line = re.sub(
                    kwarg_pattern, kwarg.value.node_source_code, node.node_source_code
                )
                error_suggestion = (
                    f"\n(hint: Try removing the kwarg: `{modified_line}`)"
                    if modified_line != node.node_source_code
                    else ""
                )

                raise ArgumentException(
                    (
                        "Usage of kwarg in Vyper is restricted to "
                        + ", ".join([f"{k}=" for k in self.call_site_kwargs.keys()])
                        + f". {error_suggestion}"
                    ),
                    kwarg,
                )

        return self.return_type

    def to_toplevel_abi_dict(self):
        abi_dict: Dict = {"stateMutability": self.mutability.value}

        if self.is_fallback:
            abi_dict["type"] = "fallback"
            return [abi_dict]

        if self.is_constructor:
            abi_dict["type"] = "constructor"
        else:
            abi_dict["type"] = "function"
            abi_dict["name"] = self.name

        abi_dict["inputs"] = [v.to_abi_arg(name=k) for k, v in self.arguments.items()]

        typ = self.return_type
        if typ is None:
            abi_dict["outputs"] = []
        elif isinstance(typ, TupleT) and len(typ.value_type) > 1:  # type: ignore
            abi_dict["outputs"] = [t.to_abi_arg() for t in typ.value_type]  # type: ignore
        else:
            abi_dict["outputs"] = [typ.to_abi_arg()]

        if self.has_default_args:
            # for functions with default args, return a dict for each possible arg count
            result = []
            for i in range(self.min_arg_count, self.max_arg_count + 1):
                result.append(abi_dict.copy())
                result[-1]["inputs"] = result[-1]["inputs"][:i]
            return result
        else:
            return [abi_dict]


class MemberFunctionT(VyperType):
    """
    Member function type definition.

    This class has no corresponding primitive.

    (examples for (x <DynArray[int128, 3]>).append(1))

    Arguments:
        underlying_type: the type this method is attached to. ex. DynArray[int128, 3]
        name: the name of this method. ex. "append"
        arg_types: the argument types this method accepts. ex. [int128]
        return_type: the return type of this method. ex. None
    """

    _is_callable = True

    # keep LGTM linter happy
    def __eq__(self, other):
        return super().__eq__(other)

    def __init__(
        self,
        underlying_type: VyperType,
        name: str,
        arg_types: List[VyperType],
        return_type: Optional[VyperType],
        is_modifying: bool,
    ) -> None:
        super().__init__()

        self.underlying_type = underlying_type
        self.name = name
        self.arg_types = arg_types
        self.return_type = return_type
        self.is_modifying = is_modifying

    def __repr__(self):
        return f"{self.underlying_type._id} member function '{self.name}'"

    def fetch_call_return(self, node: vy_ast.Call) -> Optional[VyperType]:
        validate_call_args(node, len(self.arg_types))

        assert len(node.args) == len(self.arg_types)  # validate_call_args postcondition
        for arg, expected_type in zip(node.args, self.arg_types):
            # CMC 2022-04-01 this should probably be in the validation module
            validate_expected_type(arg, expected_type)

        return self.return_type


def _generate_method_id(name: str, canonical_abi_types: List[str]) -> Dict[str, int]:
    function_sig = f"{name}({','.join(canonical_abi_types)})"
    selector = keccak256(function_sig.encode())[:4].hex()
    return {function_sig: int(selector, 16)}
