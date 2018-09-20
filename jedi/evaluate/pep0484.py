"""
PEP 0484 ( https://www.python.org/dev/peps/pep-0484/ ) describes type hints
through function annotations. There is a strong suggestion in this document
that only the type of type hinting defined in PEP0484 should be allowed
as annotations in future python versions.

The (initial / probably incomplete) implementation todo list for pep-0484:
v Function parameter annotations with builtin/custom type classes
v Function returntype annotations with builtin/custom type classes
v Function parameter annotations with strings (forward reference)
v Function return type annotations with strings (forward reference)
v Local variable type hints
v Assigned types: `Url = str\ndef get(url:Url) -> str:`
v Type hints in `with` statements
x Stub files support
x support `@no_type_check` and `@no_type_check_decorator`
x support for typing.cast() operator
x support for type hint comments for functions, `# type: (int, str) -> int`.
    See comment from Guido https://github.com/davidhalter/jedi/issues/662
"""

import os
import re

from parso import ParserSyntaxError, parse, split_lines
from parso.python import tree

from jedi._compatibility import unicode, force_unicode
from jedi.evaluate.cache import evaluator_method_cache
from jedi.evaluate import compiled
from jedi.evaluate.base_context import NO_CONTEXTS, ContextSet
from jedi.evaluate.lazy_context import LazyTreeContext
from jedi.evaluate.context import ModuleContext, ClassContext
from jedi.evaluate.context.typing import TypeVar, AnnotatedClass, \
    AnnotatedSubClass
from jedi.evaluate.helpers import is_string, execute_evaluated
from jedi import debug
from jedi import parser_utils


def _evaluate_for_annotation(context, annotation, index=None):
    """
    Evaluates a string-node, looking for an annotation
    If index is not None, the annotation is expected to be a tuple
    and we're interested in that index
    """
    return context.eval_node(_fix_forward_reference(context, annotation))


def _evaluate_annotation_string(context, string, index=None):
    node = _get_forward_reference_node(context, string)
    if node is None:
        return NO_CONTEXTS

    context_set = context.eval_node(node)
    if index is not None:
        context_set = context_set.filter(
            lambda context: context.array_type == u'tuple'  # noqa
                            and len(list(context.py__iter__())) >= index
        ).py__simple_getitem__(index)
    return context_set


def _fix_forward_reference(context, node):
    evaled_nodes = context.eval_node(node)
    if len(evaled_nodes) != 1:
        debug.warning("Eval'ed typing index %s should lead to 1 object, "
                      " not %s" % (node, evaled_nodes))
        return node

    evaled_context = list(evaled_nodes)[0]
    if is_string(evaled_context):
        result = _get_forward_reference_node(context, evaled_context.get_safe_value())
        if result is not None:
            return result

    return node


def _get_forward_reference_node(context, string):
    try:
        new_node = context.evaluator.grammar.parse(
            force_unicode(string),
            start_symbol='eval_input',
            error_recovery=False
        )
    except ParserSyntaxError:
        debug.warning('Annotation not parsed: %s' % string)
        return None
    else:
        module = context.tree_node.get_root_node()
        parser_utils.move(new_node, module.end_pos[0])
        new_node.parent = context.tree_node
        return new_node


def _split_comment_param_declaration(decl_text):
    """
    Split decl_text on commas, but group generic expressions
    together.

    For example, given "foo, Bar[baz, biz]" we return
    ['foo', 'Bar[baz, biz]'].

    """
    try:
        node = parse(decl_text, error_recovery=False).children[0]
    except ParserSyntaxError:
        debug.warning('Comment annotation is not valid Python: %s' % decl_text)
        return []

    if node.type == 'name':
        return [node.get_code().strip()]

    params = []
    try:
        children = node.children
    except AttributeError:
        return []
    else:
        for child in children:
            if child.type in ['name', 'atom_expr', 'power']:
                params.append(child.get_code().strip())

    return params


@evaluator_method_cache()
def infer_param(execution_context, param):
    """
    Infers the type of a function parameter, using type annotations.
    """
    annotation = param.annotation
    if annotation is None:
        # If no Python 3-style annotation, look for a Python 2-style comment
        # annotation.
        # Identify parameters to function in the same sequence as they would
        # appear in a type comment.
        all_params = [child for child in param.parent.children
                      if child.type == 'param']

        node = param.parent.parent
        comment = parser_utils.get_following_comment_same_line(node)
        if comment is None:
            return NO_CONTEXTS

        match = re.match(r"^#\s*type:\s*\(([^#]*)\)\s*->", comment)
        if not match:
            return NO_CONTEXTS
        params_comments = _split_comment_param_declaration(match.group(1))

        # Find the specific param being investigated
        index = all_params.index(param)
        # If the number of parameters doesn't match length of type comment,
        # ignore first parameter (assume it's self).
        if len(params_comments) != len(all_params):
            debug.warning(
                "Comments length != Params length %s %s",
                params_comments, all_params
            )
        from jedi.evaluate.context.instance import InstanceArguments
        if isinstance(execution_context.var_args, InstanceArguments):
            if index == 0:
                # Assume it's self, which is already handled
                return NO_CONTEXTS
            index -= 1
        if index >= len(params_comments):
            return NO_CONTEXTS

        param_comment = params_comments[index]
        return _evaluate_annotation_string(
            execution_context.function_context.get_default_param_context(),
            param_comment
        )
    # Annotations are like default params and resolve in the same way.
    context = execution_context.function_context.get_default_param_context()
    return _evaluate_for_annotation(context, annotation)


def py__annotations__(funcdef):
    dct = {}
    for function_param in funcdef.get_params():
        param_annotation = function_param.annotation
        if param_annotation is not None:
            dct[function_param.name.value] = param_annotation

    return_annotation = funcdef.annotation
    if return_annotation:
        dct['return'] = return_annotation
    return dct


@evaluator_method_cache()
def infer_return_types(function_execution_context):
    """
    Infers the type of a function's return value,
    according to type annotations.
    """
    all_annotations = py__annotations__(function_execution_context.tree_node)
    annotation = all_annotations.get("return", None)
    if annotation is None:
        # If there is no Python 3-type annotation, look for a Python 2-type annotation
        node = function_execution_context.tree_node
        comment = parser_utils.get_following_comment_same_line(node)
        if comment is None:
            return NO_CONTEXTS

        match = re.match(r"^#\s*type:\s*\([^#]*\)\s*->\s*([^#]*)", comment)
        if not match:
            return NO_CONTEXTS

        return _evaluate_annotation_string(
            function_execution_context.function_context.get_default_param_context(),
            match.group(1).strip()
        ).execute_annotation()
        if annotation is None:
            return NO_CONTEXTS

    context = function_execution_context.function_context.get_default_param_context()
    unknown_type_vars = list(find_unknown_type_vars(context, annotation))
    if not unknown_type_vars:
        return context.eval_node(annotation).execute_annotation()

    return define_type_vars_for_execution(
        context.eval_node(annotation),
        function_execution_context,
        unknown_type_vars,
    ).execute_annotation()


def define_type_vars_for_execution(to_define_contexts, execution_context,
                                   unknown_type_vars):
    all_annotations = py__annotations__(execution_context.tree_node)
    return _define_type_vars(
        to_define_contexts,
        _infer_type_vars_for_execution(execution_context, all_annotations),
    )


def _infer_type_vars_for_execution(execution_context, annotation_dict):
    """
    Some functions use type vars that are not defined by the class, but rather
    only defined in the function. See for example `iter`. In those cases we
    want to:

    1. Search for undefined type vars.
    2. Infer type vars with the execution state we have.
    3. Return the union of all type vars that have been found.
    """
    context = execution_context.function_context.get_default_param_context()

    annotation_variable_results = {}
    executed_params, _ = execution_context.get_executed_params_and_issues()
    for executed_param in executed_params:
        try:
            annotation_node = annotation_dict[executed_param.string_name]
        except KeyError:
            continue

        annotation_variables = find_unknown_type_vars(context, annotation_node)
        if annotation_variables:
            # Infer unknown type var
            annotation_context_set = context.eval_node(annotation_node)
            star_count = executed_param._param_node.star_count
            actual_context_set = executed_param.infer(use_hints=False)
            if star_count == 1:
                actual_context_set = actual_context_set.merge_types_of_iterate()
            elif star_count == 2:
                # TODO _dict_values is not public.
                actual_context_set = actual_context_set.try_merge('_dict_values')
            for ann in annotation_context_set:
                _merge_type_var_dicts(
                    annotation_variable_results,
                    _infer_type_vars(ann, actual_context_set),
                )

    return annotation_variable_results


def _define_type_vars(annotation_contexts, type_var_dict):
    def remap_type_vars(cls):
        for type_var in cls.list_type_vars():
            yield type_var_dict.get(type_var.py__name__(), NO_CONTEXTS)

    if not type_var_dict:
        return annotation_contexts

    context_set = ContextSet()
    for annotation_context in annotation_contexts:
        if isinstance(annotation_context, ClassContext):
            context_set |= ContextSet.from_iterable([
                AnnotatedSubClass(
                    annotation_context.evaluator,
                    annotation_context.parent_context,
                    annotation_context.tree_node,
                    given_types=tuple(remap_type_vars(annotation_context))
                )
            ])
    return context_set


def _merge_type_var_dicts(base_dict, new_dict):
    for type_var_name, contexts in new_dict.items():
        try:
            base_dict[type_var_name] |= contexts
        except KeyError:
            base_dict[type_var_name] = contexts


def _infer_type_vars(annotation_context, context_set):
    """
    This function tries to find information about undefined type vars and
    returns a dict from type var name to context set.

    This is for example important to understand what `iter([1])` returns.
    According to typeshed, `iter` returns an `Iterator[_T]`:

        def iter(iterable: Iterable[_T]) -> Iterator[_T]: ...

    This functions would generate `int` for `_T` in this case, because it
    unpacks the `Iterable`.
    """
    type_var_dict = {}
    if isinstance(annotation_context, TypeVar):
        return {annotation_context.py__name__(): context_set.py__class__()}
    elif isinstance(annotation_context, AnnotatedClass):
        name = annotation_context.py__name__()
        if name == 'Iterable':
            given = annotation_context.get_given_types()
            if given:
                for nested_annotation_context in given[0]:
                    _merge_type_var_dicts(
                        type_var_dict,
                        _infer_type_vars(
                            nested_annotation_context,
                            context_set.merge_types_of_iterate()
                        )
                    )
        elif name == 'Mapping':
            given = annotation_context.get_given_types()
            if len(given) == 2:
                for context in context_set:
                    try:
                        method = context.get_mapping_item_contexts
                    except AttributeError:
                        continue
                    key_contexts, value_contexts = method()

                    for nested_annotation_context in given[0]:
                        _merge_type_var_dicts(
                            type_var_dict,
                            _infer_type_vars(
                                nested_annotation_context,
                                key_contexts,
                            )
                        )
                    for nested_annotation_context in given[1]:
                        _merge_type_var_dicts(
                            type_var_dict,
                            _infer_type_vars(
                                nested_annotation_context,
                                value_contexts,
                            )
                        )
    return type_var_dict


_typing_module = None
_typing_module_code_lines = None


class TypingModuleContext(ModuleContext):
    """
    TODO this is currently used for recursion checks. We should just completely
    refactor the typing module integration.
    """
    pass


def _get_typing_replacement_module(grammar):
    """
    The idea is to return our jedi replacement for the PEP-0484 typing module
    as discussed at https://github.com/davidhalter/jedi/issues/663
    """
    global _typing_module, _typing_module_code_lines
    if _typing_module is None:
        typing_path = \
            os.path.abspath(os.path.join(__file__, "../jedi_typing.py"))
        with open(typing_path) as f:
            code = unicode(f.read())
        _typing_module = grammar.parse(code)
        _typing_module_code_lines = split_lines(code, keepends=True)
    return _typing_module, _typing_module_code_lines


def py__simple_getitem__(context, typ, node):
    if not typ.get_root_context().name.string_name == "typing":
        return None
    # we assume that any class using [] in a module called
    # "typing" with a name for which we have a replacement
    # should be replaced by that class. This is not 100%
    # airtight but I don't have a better idea to check that it's
    # actually the PEP-0484 typing module and not some other
    if node.type == "subscriptlist":
        nodes = node.children[::2]  # skip the commas
    else:
        nodes = [node]
    del node

    nodes = [_fix_forward_reference(context, node) for node in nodes]
    type_name = typ.name.string_name

    # hacked in Union and Optional, since it's hard to do nicely in parsed code
    if type_name in ("Union", '_Union'):
        # In Python 3.6 it's still called typing.Union but it's an instance
        # called _Union.
        return ContextSet.from_sets(context.eval_node(node) for node in nodes)
    if type_name in ("Optional", '_Optional'):
        # Here we have the same issue like in Union. Therefore we also need to
        # check for the instance typing._Optional (Python 3.6).
        return context.eval_node(nodes[0])

    module_node, code_lines = _get_typing_replacement_module(context.evaluator.latest_grammar)
    typing = TypingModuleContext(
        context.evaluator,
        module_node=module_node,
        path=None,
        code_lines=code_lines,
    )
    factories = typing.py__getattribute__("factory")
    assert len(factories) == 1
    factory = list(factories)[0]
    assert factory
    function_body_nodes = factory.tree_node.children[4].children
    valid_classnames = set(child.name.value
                           for child in function_body_nodes
                           if isinstance(child, tree.Class))
    if type_name not in valid_classnames:
        return None
    compiled_classname = compiled.create_simple_object(context.evaluator, type_name)

    from jedi.evaluate.context.iterable import FakeSequence
    args = FakeSequence(
        context.evaluator,
        u'tuple',
        [LazyTreeContext(context, n) for n in nodes]
    )

    result = execute_evaluated(factory, compiled_classname, args)
    return result


def find_type_from_comment_hint_for(context, node, name):
    return _find_type_from_comment_hint(context, node, node.children[1], name)


def find_type_from_comment_hint_with(context, node, name):
    assert len(node.children[1].children) == 3, \
        "Can only be here when children[1] is 'foo() as f'"
    varlist = node.children[1].children[2]
    return _find_type_from_comment_hint(context, node, varlist, name)


def find_type_from_comment_hint_assign(context, node, name):
    return _find_type_from_comment_hint(context, node, node.children[0], name)


def _find_type_from_comment_hint(context, node, varlist, name):
    index = None
    if varlist.type in ("testlist_star_expr", "exprlist", "testlist"):
        # something like "a, b = 1, 2"
        index = 0
        for child in varlist.children:
            if child == name:
                break
            if child.type == "operator":
                continue
            index += 1
        else:
            return []

    comment = parser_utils.get_following_comment_same_line(node)
    if comment is None:
        return []
    match = re.match(r"^#\s*type:\s*([^#]*)", comment)
    if match is None:
        return []
    return _evaluate_annotation_string(
        context, match.group(1).strip(), index
    ).execute_annotation()


def find_unknown_type_vars(context, node):
    def check_node(node):
        if node.type == 'atom_expr':
            trailer = node.children[-1]
            if trailer.type == 'trailer' and trailer.children[0] == '[':
                for subscript_node in _unpack_subscriptlist(trailer.children[1]):
                    check_node(subscript_node)
        else:
            type_var_set = context.eval_node(node)
            for type_var in type_var_set:
                if isinstance(type_var, TypeVar) and type_var not in found:
                    found.append(type_var)

    found = []  # We're not using a set, because the order matters.
    check_node(node)
    return found


def _unpack_subscriptlist(subscriptlist):
    if subscriptlist.type == 'subscriptlist':
        for subscript in subscriptlist.children[::2]:
            if subscript.type != 'subscript':
                yield subscript
    else:
        if subscriptlist.type != 'subscript':
            yield subscriptlist
