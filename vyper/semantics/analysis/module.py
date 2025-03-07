import importlib
import pkgutil
from typing import Optional, Union

import vyper.builtins.interfaces
from vyper import ast as vy_ast
from vyper.exceptions import (
    CallViolation,
    CompilerPanic,
    ExceptionList,
    InvalidLiteral,
    InvalidType,
    NamespaceCollision,
    StateAccessViolation,
    StructureException,
    SyntaxException,
    UndeclaredDefinition,
    UnexpectedNodeType,
    VariableDeclarationException,
    VyperException,
)
from vyper.semantics.analysis.base import DataLocation, VarInfo
from vyper.semantics.analysis.common import VyperNodeVisitorBase
from vyper.semantics.analysis.levenshtein_utils import get_levenshtein_error_suggestions
from vyper.semantics.analysis.utils import (
    check_constant,
    validate_expected_type,
    validate_unique_method_ids,
)
from vyper.semantics.namespace import get_namespace
from vyper.semantics.types import EnumT, EventT, InterfaceT, StructT
from vyper.semantics.types.function import ContractFunction
from vyper.semantics.types.utils import type_from_annotation
from vyper.typing import InterfaceDict


def add_module_namespace(vy_module: vy_ast.Module, interface_codes: InterfaceDict) -> None:
    """
    Analyze a Vyper module AST node, add all module-level objects to the
    namespace and validate top-level correctness
    """

    namespace = get_namespace()
    ModuleNodeVisitor(vy_module, interface_codes, namespace)


def _find_cyclic_call(fn_names: list, self_members: dict) -> Optional[list]:
    if fn_names[-1] not in self_members:
        return None
    internal_calls = self_members[fn_names[-1]].internal_calls
    for name in internal_calls:
        if name in fn_names:
            return fn_names + [name]
        sequence = _find_cyclic_call(fn_names + [name], self_members)
        if sequence:
            return sequence
    return None


class ModuleNodeVisitor(VyperNodeVisitorBase):

    scope_name = "module"

    def __init__(
        self, module_node: vy_ast.Module, interface_codes: InterfaceDict, namespace: dict
    ) -> None:
        self.ast = module_node
        self.interface_codes = interface_codes or {}
        self.namespace = namespace

        module_nodes = module_node.body.copy()
        while module_nodes:
            count = len(module_nodes)
            err_list = ExceptionList()
            for node in list(module_nodes):
                try:
                    self.visit(node)
                    module_nodes.remove(node)
                except (InvalidLiteral, InvalidType, VariableDeclarationException):
                    # these exceptions cannot be caused by another statement not yet being
                    # parsed, so we raise them immediately
                    raise
                except VyperException as e:
                    err_list.append(e)

            # Only raise if no nodes were successfully processed. This allows module
            # level logic to parse regardless of the ordering of code elements.
            if count == len(module_nodes):
                err_list.raise_if_not_empty()

        # generate an `InterfacePrimitive` from the top-level node - used for building the ABI
        interface = InterfaceT.from_ast(module_node)
        module_node._metadata["type"] = interface
        self.interface = interface  # this is useful downstream

        # check for collisions between 4byte function selectors
        # internal functions are intentionally included in this check, to prevent breaking
        # changes in in case of a future change to their calling convention
        self_members = namespace["self"].typ.members
        functions = [i for i in self_members.values() if isinstance(i, ContractFunction)]
        validate_unique_method_ids(functions)

        # get list of internal function calls made by each function
        function_defs = self.ast.get_children(vy_ast.FunctionDef)
        function_names = set(node.name for node in function_defs)
        for node in function_defs:
            calls_to_self = set(
                i.func.attr for i in node.get_descendants(vy_ast.Call, {"func.value.id": "self"})
            )
            # anything that is not a function call will get semantically checked later
            calls_to_self = calls_to_self.intersection(function_names)
            self_members[node.name].internal_calls = calls_to_self
            if node.name in self_members[node.name].internal_calls:
                self_node = node.get_descendants(
                    vy_ast.Attribute, {"value.id": "self", "attr": node.name}
                )[0]
                raise CallViolation(f"Function '{node.name}' calls into itself", self_node)

        for fn_name in sorted(function_names):

            if fn_name not in self_members:
                # the referenced function does not exist - this is an issue, but we'll report
                # it later when parsing the function so we can give more meaningful output
                continue

            # check for circular function calls
            sequence = _find_cyclic_call([fn_name], self_members)
            if sequence is not None:
                nodes = []
                for i in range(len(sequence) - 1):
                    fn_node = self.ast.get_children(vy_ast.FunctionDef, {"name": sequence[i]})[0]
                    call_node = fn_node.get_descendants(
                        vy_ast.Attribute, {"value.id": "self", "attr": sequence[i + 1]}
                    )[0]
                    nodes.append(call_node)

                raise CallViolation("Contract contains cyclic function call", *nodes)

            # get complete list of functions that are reachable from this function
            function_set = set(i for i in self_members[fn_name].internal_calls if i in self_members)
            while True:
                expanded = set(x for i in function_set for x in self_members[i].internal_calls)
                expanded |= function_set
                if expanded == function_set:
                    break
                function_set = expanded

            self_members[fn_name].recursive_calls = function_set

    def visit_AnnAssign(self, node):
        # TODO rename the node class to ImplementsDecl
        name = node.get("target.id")
        # TODO move these checks to AST validation
        if name != "implements":
            raise UnexpectedNodeType("AnnAssign not allowed at module level", node)
        if not isinstance(node.annotation, vy_ast.Name):
            raise UnexpectedNodeType("not an identifier", node.annotation)

        interface_name = node.annotation.id

        other_iface = self.namespace[interface_name]
        other_iface.validate_implements(node)

    def visit_VariableDecl(self, node):
        name = node.get("target.id")
        if name is None:
            raise VariableDeclarationException("Invalid module-level assignment", node)

        if node.is_public:
            # generate function type and add to metadata
            # we need this when building the public getter
            node._metadata["func_type"] = ContractFunction.getter_from_VariableDecl(node)

        if node.is_immutable:
            # mutability is checked automatically preventing assignment
            # outside of the constructor, here we just check a value is assigned,
            # not necessarily where
            assignments = self.ast.get_descendants(
                vy_ast.Assign, filters={"target.id": node.target.id}
            )
            if not assignments:
                # Special error message for common wrong usages via `self.<immutable name>`
                wrong_self_attribute = self.ast.get_descendants(
                    vy_ast.Attribute, {"value.id": "self", "attr": node.target.id}
                )
                message = (
                    "Immutable variables must be accessed without 'self'"
                    if len(wrong_self_attribute) > 0
                    else "Immutable definition requires an assignment in the constructor"
                )
                raise SyntaxException(message, node.node_source_code, node.lineno, node.col_offset)

        data_loc = DataLocation.CODE if node.is_immutable else DataLocation.STORAGE

        type_ = type_from_annotation(node.annotation)
        var_info = VarInfo(
            type_,
            decl_node=node,
            location=data_loc,
            is_constant=node.is_constant,
            is_public=node.is_public,
            is_immutable=node.is_immutable,
        )
        node.target._metadata["varinfo"] = var_info  # TODO maybe put this in the global namespace
        node._metadata["type"] = type_

        if node.is_constant:
            if not node.value:
                raise VariableDeclarationException("Constant must be declared with a value", node)
            if not check_constant(node.value):
                raise StateAccessViolation("Value must be a literal", node.value)

            validate_expected_type(node.value, type_)
            try:
                self.namespace[name] = var_info
            except VyperException as exc:
                raise exc.with_annotation(node) from None
            return

        if node.value:
            var_type = "Immutable" if node.is_immutable else "Storage"
            raise VariableDeclarationException(
                f"{var_type} variables cannot have an initial value", node.value
            )

        if node.is_immutable:
            try:
                # block immutable if storage variable already exists
                if name in self.namespace["self"].typ.members:
                    raise NamespaceCollision(
                        f"Value '{name}' has already been declared", node
                    ) from None
                self.namespace[name] = var_info
            except VyperException as exc:
                raise exc.with_annotation(node) from None
            return

        try:
            self.namespace.validate_assignment(name)
        except NamespaceCollision as exc:
            raise exc.with_annotation(node) from None
        try:
            self.namespace["self"].typ.add_member(name, var_info)
            node.target._metadata["type"] = type_
        except NamespaceCollision:
            raise NamespaceCollision(f"Value '{name}' has already been declared", node) from None
        except VyperException as exc:
            raise exc.with_annotation(node) from None

    def visit_EnumDef(self, node):
        obj = EnumT.from_EnumDef(node)
        try:
            self.namespace[node.name] = obj
        except VyperException as exc:
            raise exc.with_annotation(node) from None

    def visit_EventDef(self, node):
        obj = EventT.from_EventDef(node)
        try:
            self.namespace[node.name] = obj
        except VyperException as exc:
            raise exc.with_annotation(node) from None

    def visit_FunctionDef(self, node):
        func = ContractFunction.from_FunctionDef(node)

        try:
            # TODO sketchy elision of namespace validation
            self.namespace["self"].typ.add_member(func.name, func, skip_namespace_validation=True)
            node._metadata["type"] = func
        except VyperException as exc:
            raise exc.with_annotation(node) from None

    def visit_Import(self, node):
        if not node.alias:
            raise StructureException("Import requires an accompanying `as` statement", node)
        _add_import(node, node.name, node.alias, node.alias, self.interface_codes, self.namespace)

    def visit_ImportFrom(self, node):
        _add_import(
            node,
            node.module,
            node.name,
            node.alias or node.name,
            self.interface_codes,
            self.namespace,
        )

    def visit_InterfaceDef(self, node):
        obj = InterfaceT.from_ast(node)
        try:
            self.namespace[node.name] = obj
        except VyperException as exc:
            raise exc.with_annotation(node) from None

    def visit_StructDef(self, node):
        struct_t = StructT.from_ast_def(node)
        try:
            self.namespace[node.name] = struct_t
        except VyperException as exc:
            raise exc.with_annotation(node) from None


def _add_import(
    node: Union[vy_ast.Import, vy_ast.ImportFrom],
    module: str,
    name: str,
    alias: str,
    interface_codes: InterfaceDict,
    namespace: dict,
) -> None:
    if module == "vyper.interfaces":
        interface_codes = _get_builtin_interfaces()
    if name not in interface_codes:
        suggestions_str = get_levenshtein_error_suggestions(name, _get_builtin_interfaces(), 1.0)
        raise UndeclaredDefinition(f"Unknown interface: {name}. {suggestions_str}", node)

    if interface_codes[name]["type"] == "vyper":
        interface_ast = vy_ast.parse_to_ast(interface_codes[name]["code"], contract_name=name)
        type_ = InterfaceT.from_ast(interface_ast)
    elif interface_codes[name]["type"] == "json":
        type_ = InterfaceT.from_json_abi(name, interface_codes[name]["code"])  # type: ignore
    else:
        raise CompilerPanic(f"Unknown interface format: {interface_codes[name]['type']}")

    try:
        namespace[alias] = type_
    except VyperException as exc:
        raise exc.with_annotation(node) from None


def _get_builtin_interfaces():
    interface_names = [i.name for i in pkgutil.iter_modules(vyper.builtins.interfaces.__path__)]
    return {
        name: {
            "type": "vyper",
            "code": importlib.import_module(f"vyper.builtins.interfaces.{name}").interface_code,
        }
        for name in interface_names
    }
