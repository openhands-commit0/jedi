"""
Imitate the parser representation.
"""
import re
from functools import partial
from inspect import Parameter
from pathlib import Path
from typing import Optional
from jedi import debug
from jedi.inference.utils import to_list
from jedi.cache import memoize_method
from jedi.inference.filters import AbstractFilter
from jedi.inference.names import AbstractNameDefinition, ValueNameMixin, ParamNameInterface
from jedi.inference.base_value import Value, ValueSet, NO_VALUES
from jedi.inference.lazy_value import LazyKnownValue
from jedi.inference.compiled.access import _sentinel
from jedi.inference.cache import inference_state_function_cache
from jedi.inference.helpers import reraise_getitem_errors
from jedi.inference.signature import BuiltinSignature
from jedi.inference.context import CompiledContext, CompiledModuleContext

class CheckAttribute:
    """Raises :exc:`AttributeError` if the attribute X is not available."""

    def __init__(self, check_name=None):
        self.check_name = check_name

    def __call__(self, func):
        self.func = func
        if self.check_name is None:
            self.check_name = func.__name__[2:]
        return self

    def __get__(self, instance, owner):
        if instance is None:
            return self
        instance.access_handle.getattr_paths(self.check_name)
        return partial(self.func, instance)

class CompiledValue(Value):

    def __init__(self, inference_state, access_handle, parent_context=None):
        super().__init__(inference_state, parent_context)
        self.access_handle = access_handle

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self.access_handle.get_repr())

class CompiledModule(CompiledValue):
    file_io = None

class CompiledName(AbstractNameDefinition):

    def __init__(self, inference_state, parent_value, name, is_descriptor):
        self._inference_state = inference_state
        self.parent_context = parent_value.as_context()
        self._parent_value = parent_value
        self.string_name = name
        self.is_descriptor = is_descriptor

    def __repr__(self):
        try:
            name = self.parent_context.name
        except AttributeError:
            name = None
        return '<%s: (%s).%s>' % (self.__class__.__name__, name, self.string_name)

class SignatureParamName(ParamNameInterface, AbstractNameDefinition):

    def __init__(self, compiled_value, signature_param):
        self.parent_context = compiled_value.parent_context
        self._signature_param = signature_param

class UnresolvableParamName(ParamNameInterface, AbstractNameDefinition):

    def __init__(self, compiled_value, name, default):
        self.parent_context = compiled_value.parent_context
        self.string_name = name
        self._default = default

class CompiledValueName(ValueNameMixin, AbstractNameDefinition):

    def __init__(self, value, name):
        self.string_name = name
        self._value = value
        self.parent_context = value.parent_context

class EmptyCompiledName(AbstractNameDefinition):
    """
    Accessing some names will raise an exception. To avoid not having any
    completions, just give Jedi the option to return this object. It infers to
    nothing.
    """

    def __init__(self, inference_state, name):
        self.parent_context = inference_state.builtins_module
        self.string_name = name

class CompiledValueFilter(AbstractFilter):

    def __init__(self, inference_state, compiled_value, is_instance=False):
        self._inference_state = inference_state
        self.compiled_value = compiled_value
        self.is_instance = is_instance

    def _get(self, name, allowed_getattr_callback, in_dir_callback, check_has_attribute=False):
        """
        To remove quite a few access calls we introduced the callback here.
        """
        has_attribute, is_descriptor = allowed_getattr_callback(name)
        if not has_attribute and in_dir_callback(name):
            return iter([])

        if check_has_attribute and not has_attribute:
            return iter([])

        return iter([CompiledName(
            self._inference_state,
            self.compiled_value,
            name,
            is_descriptor
        )])

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self.compiled_value)
docstr_defaults = {'floating point number': 'float', 'character': 'str', 'integer': 'int', 'dictionary': 'dict', 'string': 'str'}

@inference_state_function_cache()
def create_from_access_path(inference_state, access_path):
    """
    Creates a compiled value from an access path.
    """
    value = None
    for name, access in access_path.accesses:
        if value is None:
            value = CompiledValue(inference_state, access)
        else:
            value = value.py__getattribute__(name)[0]
    return value

def _parse_function_doc(doc):
    """
    Takes a function and returns the params and return value as a tuple.
    This is nothing more than a docstring parser.

    TODO docstrings like utime(path, (atime, mtime)) and a(b [, b]) -> None
    TODO docstrings like 'tuple of integers'
    """
    if doc is None:
        return None, None

    doc = doc.strip()
    if not doc:
        return None, None

    # Get rid of multiple spaces
    doc = ' '.join(doc.split())

    # Parse return value
    return_string = None
    arrow_index = doc.find('->')
    if arrow_index != -1:
        return_string = doc[arrow_index + 2:].strip()
        if return_string:
            # Get rid of "Returns" prefix
            return_string = re.sub('^returns\s+', '', return_string.lower())
            # Get rid of punctuation
            return_string = re.sub('[^\w\s]', '', return_string)
            # Handle "floating point number" and the like
            for type_string, actual in docstr_defaults.items():
                return_string = return_string.replace(type_string, actual)
            # Handle "sequence of" and the like
            return_string = re.sub('^sequence of\s+', '', return_string)
            return_string = re.sub('\s*sequence\s*$', '', return_string)

    # Parse parameters
    param_string = doc[:arrow_index if arrow_index != -1 else len(doc)]
    if not param_string.strip():
        return None, return_string

    # Get rid of "Parameters:" prefix
    param_string = re.sub('^parameters:\s*', '', param_string.lower())
    # Get rid of punctuation
    param_string = re.sub('[^\w\s,\[\]]', '', param_string)
    # Split parameters
    params = [p.strip() for p in param_string.split(',')]
    # Get rid of empty parameters
    params = [p for p in params if p]
    # Handle optional parameters
    params = [re.sub(r'\[([^\]]+)\]', r'\1', p) for p in params]

    return params, return_string

def _normalize_create_args(func):
    """The cache doesn't care about keyword vs. normal args."""
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper