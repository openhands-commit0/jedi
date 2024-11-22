import copy
import sys
import re
import os
from itertools import chain
from contextlib import contextmanager
from parso.python import tree

def is_big_annoying_library(value):
    """
    Checks if a value is part of a library that is known to cause issues.
    """
    string = value.get_root_context().py__file__()
    if string is None:
        return False

    # These libraries cause problems, because they have really strange ways of
    # using modules. They are also very big (numpy) and therefore really slow.
    parts = string.split(os.path.sep)
    match = any(x in parts for x in ['numpy', 'scipy', 'tensorflow', 'matplotlib', 'pandas'])
    return match

def reraise_getitem_errors(func):
    """
    Re-throw any SimpleGetItemNotFound errors as KeyError or IndexError.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SimpleGetItemNotFound as e:
            if isinstance(e.args[1], str):
                raise KeyError(e.args[1])
            else:
                raise IndexError(e.args[1])
    return wrapper

def deep_ast_copy(obj):
    """
    Much, much faster than copy.deepcopy, but just for parser tree nodes.
    """
    if isinstance(obj, tree.Leaf):
        obj_copy = copy.copy(obj)
        obj_copy.parent = None
        return obj_copy

    if isinstance(obj, tree.BaseNode):
        new_children = []
        for child in obj.children:
            if isinstance(child, (tree.Leaf, tree.BaseNode)):
                new_children.append(deep_ast_copy(child))
            else:
                new_children.append(child)

        obj_copy = copy.copy(obj)
        obj_copy.children = new_children
        for child in new_children:
            if isinstance(child, (tree.Leaf, tree.BaseNode)):
                child.parent = obj_copy
        obj_copy.parent = None
        return obj_copy

    return obj

def is_string(value):
    """
    Checks if a value is a string.
    """
    return isinstance(value, str)

def get_str_or_none(value):
    """
    Gets a string from a value or returns None if it's not a string.
    """
    if is_string(value):
        return value
    return None

def infer_call_of_leaf(context, leaf, cut_own_trailer=False):
    """
    Creates a "call" node that consist of all ``trailer`` and ``power``
    objects.  E.g. if you call it with ``append``::

        list([]).append(3) or None

    You would get a node with the content ``list([]).append`` back.

    This generates a copy of the original ast node.

    If you're using the leaf, e.g. the bracket `)` it will return ``list([])``.

    We use this function for two purposes. Given an expression ``bar.foo``,
    we may want to
      - infer the type of ``foo`` to offer completions after foo
      - infer the type of ``bar`` to be able to jump to the definition of foo
    The option ``cut_own_trailer`` must be set to true for the second purpose.
    """
    node = leaf
    while node.parent is not None:
        node = node.parent
        if node.type in ('trailer', 'power'):
            if node.type == 'trailer' and cut_own_trailer and node.children[-1] is leaf:
                continue
            node = deep_ast_copy(node)
            context = context.eval_node(node)
            return context
    return context

class SimpleGetItemNotFound(Exception):
    pass