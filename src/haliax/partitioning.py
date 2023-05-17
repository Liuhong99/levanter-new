import contextlib
import functools
import threading
import typing
from math import prod
from typing import Mapping, Optional, Sequence, TypeVar, Union

import equinox as eqx
import jax
import jax.numpy as jnp
from equinox.compile_utils import compile_cache, get_fun_names, hashable_combine, hashable_partition
from jax.experimental.pjit import pjit, with_sharding_constraint
from jax.interpreters.pxla import PartitionSpec
from jaxlib.xla_client import SingleDeviceSharding
from jaxtyping import PyTree

from .core import NamedArray
from .jax_utils import filter_eval_shape, is_jax_array_like
from .types import Axis, AxisSelection, AxisSelector
from .util import StringHolderEnum, ensure_tuple, is_named_array


LogicalAxisName = str
PhysicalAxis = str
PhysicalAxisSpec = Union[PhysicalAxis, Sequence[PhysicalAxis]]
ResourceMapping = Mapping[LogicalAxisName, PhysicalAxisSpec]
"""Mapping from logical axis names to physical axis names"""


class ResourceAxis(StringHolderEnum):
    """Standard names for physical axes"""

    MODEL = "model"
    DATA = "data"


class _ResourceMappingHolder:
    """Global resource mapping, used with a context manager to give dynamic scoping to resource mappings"""

    def __init__(self):
        self.thread_data = threading.local()
        self.thread_data.resource_mapping = None


_mapping_holder = _ResourceMappingHolder()


@contextlib.contextmanager
def axis_mapping(mapping: ResourceMapping, *, merge: bool = False, **kwargs):
    """Context manager for setting the global resource mapping"""
    mapping = dict(mapping)

    old_mapping = _mapping_holder.thread_data.resource_mapping
    if merge:
        mapping.update(old_mapping or {})

    if len(kwargs):
        mapping.update(kwargs)

    _mapping_holder.thread_data.resource_mapping = mapping
    try:
        yield
    finally:
        _mapping_holder.thread_data.resource_mapping = old_mapping


T = TypeVar("T", bound=PyTree)


def auto_sharded(x: T) -> T:
    """
    Shard a PyTree using the global axis mapping. NamedArrays in the PyTree are sharded using the axis mapping
     and the names in the tree.

    If there is no axis mapping, the global axis mapping, this function is a no-op.
    """
    mapping = _mapping_holder.thread_data.resource_mapping

    if mapping is None:
        return x

    return shard_with_axis_mapping(x, mapping)


def shard_with_axis_mapping(x: T, mapping: ResourceMapping) -> T:
    """
    Shard a PyTree using the provided axis mapping. NamedArrays in the PyTree are sharded using the axis mapping.
    Other arrays are not sharded (unless they're already sharded).

    Inside of a jit context, this method grounds out in calls to `with_sharding_constraint`. Outside of a jit
    context, this method grounds out in either device_put or make_array_from_callback, depending on whether the
    resulting sharding spans more than one host.

    :param x:
    :param mapping:
    :return:
    """

    if _is_jit_context():

        def _shard_leaf(x):
            if isinstance(x, NamedArray):
                pspec = pspec_for_axis(x.axes, mapping)
                return with_sharding_constraint(x, pspec)
            else:
                return x

        return jax.tree_util.tree_map(_shard_leaf, x, is_leaf=is_named_array)
    else:
        # use device_put or make_array_from_callback instead
        mesh = _get_mesh()

        def _do_device_put(x):
            if not is_named_array(x):
                return x

            pspec = pspec_for_axis(x.axes, mapping)
            sharding = jax.sharding.NamedSharding(mesh, pspec)

            raw_x = x.array
            current_sharding = raw_x.sharding

            if current_sharding == sharding:
                return x
            elif sharding.is_fully_addressable:
                raw_x = jax.device_put(raw_x, sharding)
                return NamedArray(raw_x, x.axes)
            else:
                # if the sharding is not fully addressable, we can't use device_put, so we use this hacky workaround.
                # TODO: we lose "src" information, but i think that's only for autodiff, and this isn't an autodiff
                # context, I think?
                shape = raw_x.shape
                raw_x = jax.make_array_from_callback(shape, sharding, lambda index: raw_x[index])
                return NamedArray(raw_x, x.axes)

        return jax.tree_util.tree_map(_do_device_put, x, is_leaf=is_named_array)


def infer_resource_partitions(tree: PyTree, resource_mapping: Optional[ResourceMapping] = None) -> PyTree:
    """
    Infer the resource partitions for a module, to be used with pjit.
    The basic idea is to tree all NamedArrays as leaves for the purposes of this function,
    and to create PartitionSpecs from those names plus the resource_mapping.

    If resource_mapping is not provided, this function attempts to use the global resource mapping.
    """
    if resource_mapping is None:
        resource_mapping = _mapping_holder.thread_data.resource_mapping

    if resource_mapping is None:
        raise ValueError("No resource mapping found")

    def partition_spec(node: typing.Any):
        if isinstance(node, NamedArray):
            return NamedArray(
                pspec_for_axis(node.axes, resource_mapping),  # type: ignore
                node.axes,
            )
        # elif isinstance(node, GlobalDeviceArray):
        #     return FROM_GDA
        elif hasattr(node, "sharding"):
            sharding = node.sharding
            # these are usually replicated. Is there a better way to tell?
            if isinstance(sharding, SingleDeviceSharding):
                return None
            else:
                return sharding
        else:
            return None

    return jax.tree_util.tree_map(partition_spec, tree, is_leaf=is_named_array)


def named_pjit(
    fn=None,
    axis_resources: Optional[ResourceMapping] = None,
    *,
    in_axis_resources: Optional[ResourceMapping] = None,
    out_axis_resources: Optional[ResourceMapping] = None,
    donate_args: Optional[PyTree] = None,
    donate_kwargs: Optional[PyTree] = None,
    **pjit_args,
):
    """
    A version of pjit that uses NamedArrays and the provided resource mapping to infer the
    resource partitions.

    If no resource mapping is provided, this function attempts to use the global resource mapping.
    axis_resources will be used for a context-specific resource mapping as well as in_axis_resources and out_axis_resources
    if they are not provided.

    :param fn: The function to be pjit'd
    :param axis_resources: A mapping from logical axis names to physical axis names
    :param in_axis_resources: A mapping from logical axis names to physical axis names for arguments, defaults to axis_resources
    :param out_axis_resources: A mapping from logical axis names to physical axis names for the result, defaults to axis_resources
    :param donate_args: A PyTree of booleans or function leaf->bool, indicating whether to donate arguments to the
     computation
    :param donate_kwargs: A PyTree of booleans or function leaf->bool, indicating whether to donate keyword arguments to
        the computation
    """
    # TODO: support jax.Array

    if fn is None:
        return functools.partial(
            named_pjit,
            axis_resources=axis_resources,
            in_axis_resources=in_axis_resources,
            out_axis_resources=out_axis_resources,
            donate_args=donate_args,
            donate_kwargs=donate_kwargs,
            **pjit_args,
        )

    if axis_resources is None:
        axis_resources = _mapping_holder.thread_data.resource_mapping

    if in_axis_resources is None:
        in_axis_resources = axis_resources

    if out_axis_resources is None:
        out_axis_resources = axis_resources

    if axis_resources is None and (in_axis_resources is None or out_axis_resources is None):
        raise ValueError(
            "Must provide axis_resources, or in_axis_resources and out_axis_resources,"
            " or have a global mapping via axis_mapping"
        )

    dynamic_fun, static_fun = hashable_partition(fn, is_jax_array_like)

    @functools.wraps(fn)
    def f(*args, **kwargs):
        dynamic_argspec, static_argspec = hashable_partition((args, kwargs), is_jax_array_like)
        dynamic = (dynamic_fun, dynamic_argspec)

        if donate_args is not None or donate_kwargs is not None:
            if donate_args is None:
                dargs = (False,) * len(args)
            elif isinstance(donate_args, bool):
                dargs = (donate_args,) * len(args)
            elif not isinstance(donate_args, tuple):
                dargs = tuple(donate_args)
            else:
                dargs = donate_args
            dkwargs = donate_kwargs or {k: False for k in kwargs}
            dkwargs = {k: dkwargs.get(k, False) for k in kwargs}
            dynamic_donated, dynamic_reserved = eqx.partition(dynamic, (False, (dargs, dkwargs)))
        else:
            dynamic_donated = jax.tree_util.tree_map(lambda _: None, dynamic)
            dynamic_reserved = dynamic

        static = (static_fun, static_argspec)

        output_shape = _cached_filter_eval_shape(fn, *args, **kwargs)
        # TODO: with new jax.Array I shouldn't have to specify shardings, but I do...
        in_resources = infer_resource_partitions((dynamic_donated, dynamic_reserved), in_axis_resources)
        out_resources = infer_resource_partitions(output_shape, out_axis_resources)

        my_pjit_args = dict(**pjit_args)
        my_pjit_args["in_axis_resources"] = in_resources
        my_pjit_args["out_axis_resources"] = out_resources
        with axis_mapping(axis_resources or {}):
            cached_pjitted_fun = _named_pjit_cache(get_fun_names(fn), **my_pjit_args)
            return cached_pjitted_fun(dynamic_donated, dynamic_reserved, static)

    return f


# This is more or less copy-pasted from Equinox's similar functions (pmap, vmap, etc), but
# it's not really explained there so we'll explain it here.
# Many jax functions work by compiling functions to XLA. The compilation process is expensive,
# so we want to cache the compiled functions. However, the compiled functions are tied to the
# "static" arguments to the functions. This is particularly important for a library like Equinox,
# which Haliax is built on top of, because Equinox uses pytrees extensively for modules, and mixes "static"
# configuration with "dynamic" data.
# Thus we need to carefully partition the arguments to the function into "static" and "dynamic" arguments,
# and cache our compiled functions based on the static arguments.
# In Equinox conceptually there are three types of "arguments": positional, named, and the function itself.
# All of these are pytrees, and we need to partition them into static and dynamic arguments.
# Inside the function, we then combine the arguments into a single pytree, and pass that to the original function.
# With pjit we also have "donated" arguments, which are arguments that we promise not to use after the function
# returns. This is useful for conserving memory, but we also have to splice them back in.
# Also recall that a "pytree" can split into leaves and a "treedef", which can then be reconstructed.
@compile_cache
def _named_pjit_cache(fun_names, **jitkwargs):
    def fun_wrapped(dynamic_donated, dynamic_reserved, static):
        dynamic = eqx.combine(dynamic_donated, dynamic_reserved)
        dynamic_fun, dynamic_spec = dynamic
        static_fun, static_spec = static

        fun = hashable_combine(dynamic_fun, static_fun)
        args, kwargs = hashable_combine(dynamic_spec, static_spec)
        out = fun(*args, **kwargs)
        return out

    fun_name, fun_qualname = fun_names
    fun_wrapped.__name__ = fun_name
    fun_wrapped.__qualname__ = fun_qualname

    return pjit(fun_wrapped, donate_argnums=0, static_argnums=2, **jitkwargs)


_eval_shape_cache = {}


def _cached_filter_eval_shape(fun, *args, **kwargs):
    """
    eval_shape is surprisingly expensive, so we cache it. We use this for named_pjit for evaluating resource partitions
    of the output.
    """
    dynamic, static = hashable_partition((fun, args, kwargs), is_jax_array_like)
    if static not in _eval_shape_cache:
        _eval_shape_cache[static] = filter_eval_shape(fun, *args, **kwargs)

    return _eval_shape_cache[static]


def physical_axis_name(axis: AxisSelector, mapping: Optional[ResourceMapping] = None) -> Optional[PhysicalAxisSpec]:
    """Get the physical axis name for a logical axis from the mapping. Returns none if the axis is not mapped."""
    mapping = mapping or _mapping_holder.thread_data.resource_mapping
    if mapping is None:
        return None
    elif isinstance(axis, str):
        return mapping.get(axis, None)
    else:
        return mapping.get(axis.name, None)


def physical_axis_size(axis: AxisSelector, mapping: Optional[ResourceMapping] = None) -> Optional[int]:
    """Get the physical axis size for a logical axis. This is the product of the size of all physical axes
    that this logical axis is mapped to."""
    # TODO: shouldn't be accessing this internal api, but...
    from jax.experimental.maps import thread_resources

    try:
        mesh_shape = thread_resources.env.shape
    except AttributeError:
        raise ValueError("No resource mapping found")

    name: Union[None, str, Sequence[str]] = physical_axis_name(axis, mapping)
    if name is None:
        return None
    elif isinstance(name, str):
        name = (name,)

    return prod([mesh_shape[n] for n in name])


def pspec_for_axis(axis: AxisSelection, mapping: Optional[ResourceMapping] = None) -> PartitionSpec:
    """Get the PartitionSpec for a single axis"""
    axis = ensure_tuple(axis)
    return PartitionSpec(*(physical_axis_name(a, mapping) for a in axis))


def round_axis_for_partitioning(axis: Axis, mapping: Optional[ResourceMapping] = None) -> Axis:
    """Round an axis so that it's divisible by the size of the partition it's on"""
    size = physical_axis_size(axis, mapping)
    if size is None:
        return axis
    else:
        new_size = (axis.size + size - 1) // size * size
        return Axis(axis.name, new_size)


def _get_mesh():
    from jax.experimental.maps import thread_resources

    return thread_resources.env.physical_mesh


def _is_jit_context():
    return isinstance(jnp.zeros(1), jax.core.Tracer)


__all__ = [
    "LogicalAxisName",
    "PhysicalAxis",
    "PhysicalAxisSpec",
    "ResourceAxis",
    "ResourceMapping",
    "axis_mapping",
    "auto_sharded",
    "infer_resource_partitions",
    "named_pjit",
    "physical_axis_name",
    "pspec_for_axis",
    "round_axis_for_partitioning",
]
