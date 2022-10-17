import functools as ft
from typing import Callable, Optional, Sequence, Union

import equinox as eqx
import jax
import numpy as np
from chex import PRNGKey
from equinox.compile_utils import Static
from jax import numpy as jnp
from jax import random as jrandom

from haliax.util import is_jax_array_like


def shaped_rng_split(key, split_shape: Union[int, Sequence[int]] = 2) -> jrandom.KeyArray:
    if isinstance(split_shape, int):
        num_splits = split_shape
        split_shape = (num_splits,) + key.shape
    else:
        num_splits = np.prod(split_shape)
        split_shape = tuple(split_shape) + key.shape

    if num_splits == 1:
        return jnp.reshape(key, split_shape)

    unshaped = maybe_rng_split(key, num_splits)
    return jnp.reshape(unshaped, split_shape)


def maybe_rng_split(key: Optional[PRNGKey], num: int = 2):
    """Splits a random key into multiple random keys. If the key is None, then it replicates the None. Also handles
    num == 1 case"""
    if key is None:
        return [None] * num
    elif num == 1:
        return jnp.reshape(key, (1,) + key.shape)
    else:
        return jrandom.split(key, num)


def filter_eval_shape(fun: Callable, *args, **kwargs):
    """As `jax.eval_shape`, but allows any Python object as inputs and outputs, including
    GlobalDeviceArrays (which equinox.filter_eval_shape does not support).
    """
    # TODO: file a bug

    def _fn(_static, _dynamic):
        _args, _kwargs = eqx.combine(_static, _dynamic)
        _out = fun(*_args, **_kwargs)
        _dynamic_out, _static_out = eqx.partition(_out, is_jax_array_like)
        return _dynamic_out, Static(_static_out)

    dynamic, static = eqx.partition((args, kwargs), is_jax_array_like)
    dynamic_out, static_out = jax.eval_shape(ft.partial(_fn, static), dynamic)
    return eqx.combine(dynamic_out, static_out.value)