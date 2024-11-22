""" A universal module with functions / classes without dependencies. """
import functools
import re
import os
_sep = os.path.sep
if os.path.altsep is not None:
    _sep += os.path.altsep
_path_re = re.compile('(?:\\.[^{0}]+|[{0}]__init__\\.py)$'.format(re.escape(_sep)))
del _sep

def unite(iterable):
    """Turns a two dimensional array into a one dimensional."""
    return set(x for objects in iterable for x in objects)

class UncaughtAttributeError(Exception):
    """
    Important, because `__getattr__` and `hasattr` catch AttributeErrors
    implicitly. This is really evil (mainly because of `__getattr__`).
    Therefore this class originally had to be derived from `BaseException`
    instead of `Exception`.  But because I removed relevant `hasattr` from
    the code base, we can now switch back to `Exception`.

    :param base: return values of sys.exc_info().
    """

def reraise_uncaught(func):
    """
    Re-throw uncaught `AttributeError`.

    Usage:  Put ``@rethrow_uncaught`` in front of the function
    which does **not** suppose to raise `AttributeError`.

    AttributeError is easily get caught by `hasattr` and another
    ``except AttributeError`` clause.  This becomes problem when you use
    a lot of "dynamic" attributes (e.g., using ``@property``) because you
    can't distinguish if the property does not exist for real or some code
    inside of the "dynamic" attribute through that error.  In a well
    written code, such error should not exist but getting there is very
    difficult.  This decorator is to help us getting there by changing
    `AttributeError` to `UncaughtAttributeError` to avoid unexpected catch.
    This helps us noticing bugs earlier and facilitates debugging.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwds):
        try:
            return func(*args, **kwds)
        except AttributeError as e:
            raise UncaughtAttributeError(e) from e
    return wrapper

def safe_property(func):
    """
    Property decorator that wraps the getter in a reraise_uncaught decorator.
    """
    return property(reraise_uncaught(func))

def to_list(func):
    """
    Decorator that ensures that a function returns a list.
    """
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if isinstance(result, (list, tuple)):
            return list(result)
        elif result is None:
            return []
        else:
            return [result]
    return wrapper

class PushBackIterator:

    def __init__(self, iterator):
        self.pushes = []
        self.iterator = iterator
        self.current = None

    def __iter__(self):
        return self

    def __next__(self):
        if self.pushes:
            self.current = self.pushes.pop()
        else:
            self.current = next(self.iterator)
        return self.current