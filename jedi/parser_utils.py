import re
import textwrap
from ast import literal_eval
from inspect import cleandoc
from weakref import WeakKeyDictionary
from parso.python import tree
from parso.cache import parser_cache
from parso import split_lines
_EXECUTE_NODES = {'funcdef', 'classdef', 'import_from', 'import_name', 'test', 'or_test', 'and_test', 'not_test', 'comparison', 'expr', 'xor_expr', 'and_expr', 'shift_expr', 'arith_expr', 'atom_expr', 'term', 'factor', 'power', 'atom'}
_FLOW_KEYWORDS = ('try', 'except', 'finally', 'else', 'if', 'elif', 'with', 'for', 'while')

def _get_parent_scope_cache(func):
    """
    This is a cache to avoid multiple lookups of parent scopes.
    """
    cache = WeakKeyDictionary()

    def wrapper(node, *args, **kwargs):
        try:
            return cache[node]
        except KeyError:
            result = cache[node] = func(node, *args, **kwargs)
            return result

    return wrapper

def _function_is_x_method(name, other_name=None):
    def wrapper(func):
        decorators = func.get_decorators()
        if not decorators:
            return False

        for decorator in decorators:
            dotted_name = decorator.children[1]
            if not isinstance(dotted_name, tree.Name):
                continue

            value = dotted_name.value
            if value == name or other_name is not None and value == other_name:
                return True
        return False
    return wrapper

function_is_staticmethod = _function_is_x_method('staticmethod')
function_is_classmethod = _function_is_x_method('classmethod')
function_is_property = _function_is_x_method('property', 'cached_property')

def get_executable_nodes(node, last_added=False):
    """
    For static analysis. Returns a generator of nodes that are executed in
    order.
    """
    def check_last(node):
        if node.type == 'decorated':
            if 'async' in node.children[0].type:
                node = node.children[1]
            else:
                node = node.children[0]
        if node.type in _EXECUTE_NODES and not last_added:
            yield node
        else:
            for child in node.children:
                if hasattr(child, 'children'):
                    for result in check_last(child):
                        yield result

    for node in node.children:
        if node.type == 'suite':
            for child in node.children:
                if child.type in _FLOW_KEYWORDS:
                    # Try/except/else/finally.
                    for result in get_executable_nodes(child, last_added=True):
                        yield result
                else:
                    for result in get_executable_nodes(child):
                        yield result
        else:
            if hasattr(node, 'children'):
                for result in check_last(node):
                    yield result

def for_stmt_defines_one_name(for_stmt):
    """
    Returns True if only one name is returned: ``for x in y``.
    Returns False if the for loop is more complicated: ``for x, z in y``.

    :returns: bool
    """
    exprlist = for_stmt.children[1]
    return len(exprlist.children) == 1 and exprlist.children[0].type == 'name'

def clean_scope_docstring(scope_node):
    """ Returns a cleaned version of the docstring token. """
    node = scope_node.get_doc_node()
    if node is None:
        return ''

    if node.type == 'string':
        cleaned = cleandoc(literal_eval(node.value))
    else:
        cleaned = cleandoc(node.value)
    return cleaned

def get_signature(funcdef, width=72, call_string=None, omit_first_param=False, omit_return_annotation=False):
    """
    Generate a string signature of a function.

    :param width: Fold lines if a line is longer than this value.
    :type width: int
    :arg func_name: Override function name when given.
    :type func_name: str

    :rtype: str
    """
    if call_string is None:
        call_string = funcdef.name.value

    params = funcdef.get_params()
    if omit_first_param and params:
        params = params[1:]

    param_strs = []
    for p in params:
        code = p.get_code().strip()
        # Remove comments:
        comment_start = code.find('#')
        if comment_start != -1:
            code = code[:comment_start].strip()
        param_strs.append(code)
    param_str = ', '.join(param_strs)

    return_annotation = ''
    if not omit_return_annotation:
        return_annotation = funcdef.annotation
        if return_annotation:
            return_annotation = ' -> ' + return_annotation.get_code()

    code = call_string + '(' + param_str + ')' + return_annotation

    if len(code) > width:
        # Try to shorten the code
        code = call_string + '(\n    ' + ',\n    '.join(param_strs) + '\n)' + return_annotation
    return code

def move(node, line_offset):
    """
    Move the `Node` start_pos.
    """
    node.start_pos = (node.start_pos[0] + line_offset, node.start_pos[1])
    node.end_pos = (node.end_pos[0] + line_offset, node.end_pos[1])

    for child in node.children:
        move(child, line_offset)

def get_following_comment_same_line(node):
    """
    returns (as string) any comment that appears on the same line,
    after the node, including the #
    """
    end_pos = node.end_pos
    end_line = end_pos[0]
    end_col = end_pos[1]

    prefix = node.get_following_whitespace()
    lines = split_lines(prefix, keepends=True)
    if not lines:
        return None

    first_line = lines[0]
    comment_start = first_line.find('#')
    if comment_start == -1:
        return None

    return first_line[comment_start:].rstrip('\n\r')

def get_parent_scope(node, include_flows=False):
    """
    Returns the underlying scope.
    """
    scope = node.parent
    while scope is not None:
        if include_flows and scope.type in ('if_stmt', 'for_stmt', 'while_stmt', 'try_stmt'):
            return scope
        if scope.type in ('classdef', 'funcdef', 'file_input'):
            return scope
        scope = scope.parent
    return None
get_cached_parent_scope = _get_parent_scope_cache(get_parent_scope)

def get_cached_code_lines(grammar, path):
    """
    Basically access the cached code lines in parso. This is not the nicest way
    to do this, but we avoid splitting all the lines again.
    """
    module_node = parser_cache[grammar._hashed][path]
    return module_node.lines

def get_parso_cache_node(grammar, path):
    """
    This is of course not public. But as long as I control parso, this
    shouldn't be a problem. ~ Dave

    The reason for this is mostly caching. This is obviously also a sign of a
    broken caching architecture.
    """
    return parser_cache[grammar._hashed][path]

def cut_value_at_position(leaf, position):
    """
    Cuts of the value of the leaf at position
    """
    if leaf.type == 'string':
        matches = re.match(r'(\'{3}|"{3}|\'|")', leaf.value)
        quote = matches.group(0)
        if leaf.line == position[0] and position[1] < leaf.column + len(quote):
            return ''
    return leaf.value[:position[1] - leaf.column]

def _get_parent_scope_cache(func):
    """
    This is a cache to avoid multiple lookups of parent scopes.
    """
    cache = WeakKeyDictionary()

    def wrapper(node, *args, **kwargs):
        try:
            return cache[node]
        except KeyError:
            result = cache[node] = func(node, *args, **kwargs)
            return result

    return wrapper

def _function_is_x_method(name, other_name=None):
    def wrapper(func):
        decorators = func.get_decorators()
        if not decorators:
            return False

        for decorator in decorators:
            dotted_name = decorator.children[1]
            if not isinstance(dotted_name, tree.Name):
                continue

            value = dotted_name.value
            if value == name or other_name is not None and value == other_name:
                return True
        return False
    return wrapper

def expr_is_dotted(node):
    """
    Checks if a path looks like `name` or `name.foo.bar` and not `name()`.
    """
    if node.type == 'name':
        return True
    if node.type == 'atom' and node.children[0].value == '(':
        return False
    if node.type == 'atom_expr':
        if node.children[-1].type == 'trailer' and node.children[-1].children[0].value == '(':
            return False
        return True
    if node.type == 'power':
        if node.children[-1].type == 'trailer' and node.children[-1].children[0].value == '(':
            return False
        return True
    return False