# mypy: allow-untyped-defs
import dataclasses
import random
import textwrap
import types
from typing import Any, Optional

import sympy

import torch
from torch._higher_order_ops.triton_kernel_wrap import (
    tracing_triton_hopifier_singleton,
    triton_kernel_wrapper_mutation,
)
from torch._library.triton import wrap_triton


aten = torch.ops.aten

from torch._inductor.codecache import PyCodeCache, TritonFuture
from torch._inductor.runtime.triton_heuristics import CachingAutotuner
from torch._inductor.select_algorithm import extern_kernels  # noqa: F401
from torch._inductor.virtualized import V

from .. import config, ir
from ..utils import convert_to_symint, LineContext
from .common import IndentedBuffer, WorkspaceArg
from .wrapper import (
    AllocateLine,
    BufferLike,
    CommBufferAllocateLine,
    CommBufferFreeLine,
    CommBufferLine,
    CommentLine,
    EnterDeviceContextManagerLine,
    EnterSubgraphLine,
    ExitDeviceContextManagerLine,
    ExitSubgraphLine,
    FreeIfNotReusedLine,
    FreeLine,
    Line,
    MemoryPlanningLine,
    MemoryPlanningState,
    NullLine,
    PythonWrapperCodegen,
    ReinterpretLine,
    ReuseLine,
)


def delete(x: torch.Tensor) -> None:
    """
    This function is used as an FX op to delete a tensor.
    """
    del x


"""
Extra wrapper IR nodes for FX codegen.
"""


class WrapperIRLine(MemoryPlanningLine):
    """
    Base class for Wrapper IR nodes that do not participate in memory planning.
    Records the call args of the underlying codegen function.
    """

    def plan(self, state: MemoryPlanningState) -> MemoryPlanningLine:
        return self

    def codegen(self, code: IndentedBuffer) -> None:
        raise NotImplementedError("Python codegen not supported")


@dataclasses.dataclass
class ExternKernelLine(WrapperIRLine):
    kernel: str
    out: str
    args: tuple[Any, ...]
    kwargs: dict[Any, Any]


@dataclasses.dataclass
class KernelCallLine(WrapperIRLine):
    kernel_name: str
    call_args: tuple[Any, ...]
    grid: tuple[Any, ...]
    grid_fn: str
    triton: bool


@dataclasses.dataclass
class KernelDefinitionLine(WrapperIRLine):
    kernel_name: str
    kernel_body: str
    metadata: Optional[str] = None


class TritonKernel:
    """
    Stores metadata about Triton kernels for use in FX.
    """

    def __init__(self, tuner: CachingAutotuner):
        self.tuner = tuner
        self.wrapped = wrap_triton(tuner.fn)


class WrapperFxCodegen(PythonWrapperCodegen):
    """
    Generate Wrapper FX IR, for use in other backends.
    """

    def __init__(self):
        super().__init__()
        graph = torch.fx.Graph()
        self.gm = torch.fx.GraphModule({}, graph)  # Wrapper FX IR.
        self.buffer_to_node: dict[
            Optional[str], torch.fx.Node
        ] = {}  # Symbol table for codegen.
        self.kernels: dict[str, TritonKernel] = {}  # Table to store Triton kernels.

    @staticmethod
    def create(
        is_subgraph: bool,
        subgraph_name: Optional[str],
        parent_wrapper: Optional[PythonWrapperCodegen],
    ):
        return WrapperFxCodegen()

    def _import_kernel(self, code: str) -> types.ModuleType:
        """
        Imports a kernel as a python module.
        """
        module_code = self.imports.getvalue() + self.header.getvalue() + code
        mod = PyCodeCache.load(module_code)
        return mod

    def define_kernel(
        self,
        kernel_name: str,
        kernel_body: str,
        metadata: Optional[str] = None,
        gpu=True,
    ):
        """
        Generates Wrapper IR for a kernel definition.
        """
        self.writeline(KernelDefinitionLine(self, kernel_name, kernel_body, metadata))

    def _fake_tensor(self, size, stride, **kwargs) -> torch.Tensor:
        with V.fake_mode:
            return torch.empty_strided(
                size,
                stride,
                **kwargs,
            )

    def _create_meta_from_buffer(self, node: torch.fx.Node, buffer: BufferLike) -> None:
        name = buffer.get_name()
        assert name
        node.name = name
        node.meta["val"] = self._fake_tensor(
            tuple(buffer.get_size()),
            tuple(buffer.get_stride()),
            dtype=buffer.get_dtype(),
            device=buffer.get_device(),
        )

    def _record_allocation(self, buffer: BufferLike, node: torch.fx.Node) -> None:
        """
        Updates the symbol table to record that an Inductor buffer maps to the result of
        an FX node.
        """
        assert node not in self.buffer_to_node
        self.buffer_to_node[buffer.name] = node

    def _free(self, buffer: BufferLike) -> None:
        """
        Generates FX IR to delete a buffer.
        Removes the buffer from the symbol table.
        """
        node = self.buffer_to_node[buffer.name]
        self.gm.graph.call_function(delete, args=(node,))
        del self.buffer_to_node[buffer.name]

    def _lookup_args(self, args: tuple[Any, ...]) -> tuple[Any, ...]:
        """
        Maps call args back to FX nodes.
        """
        return tuple(
            self.buffer_to_node[arg] if isinstance(arg, str) else arg for arg in args
        )

    def _get_buffer(self, node: ir.IRNode) -> BufferLike:
        """
        Extract buffer data from an IR node.
        """
        if isinstance(node, (ir.Buffer, WorkspaceArg)):
            return node
        elif isinstance(node, (ir.BaseView, ir.MutableBox)):
            return self._get_buffer(node.data)
        else:
            raise NotImplementedError(f"Unable to extract buffer from node: {node}")

    def _generate_graph_inputs(self) -> None:
        """
        Converts graph inputs to FX placeholders.
        """
        for ir_node in V.graph.graph_inputs.values():
            buffer = self._get_buffer(ir_node)
            assert buffer.name
            node = self.gm.graph.placeholder(buffer.name)
            self._create_meta_from_buffer(node, buffer)
            self._record_allocation(buffer, node)

    def _generate_graph_outputs(self) -> None:
        """
        Convert graph outputs to FX.
        """
        outputs = tuple(
            self.buffer_to_node[self._get_buffer(output).name]
            for output in V.graph.graph_outputs
        )
        self.gm.graph.output(outputs)

    def _generate(self, is_inference):
        # We disable planning during training because it presently increases peak memory consumption.
        # TODO don't duplicate this code. Refactor into a helper in the base class.
        if is_inference and config.memory_planning:
            self.memory_plan()
        else:
            self.memory_plan_reuse()

        # Generate FX IR from Wrapper IR.
        self._generate_graph_inputs()
        for line in self.lines:
            line_type = type(line)
            conversion_func = {
                AllocateLine: self._generate_allocate,
                CommentLine: self._generate_comment,
                EnterDeviceContextManagerLine: self._generate_enter_device_context_manager,
                ExitDeviceContextManagerLine: self._generate_exit_device_context_manager,
                EnterSubgraphLine: self._generate_enter_subgraph,
                ExternKernelLine: self._generate_extern_kernel,
                ExitSubgraphLine: self._generate_exit_subgraph,
                FreeLine: self._generate_free,
                FreeIfNotReusedLine: self._generate_free_if_not_reused,
                LineContext: self._generate_line_context,
                ReinterpretLine: self._generate_reinterpret,
                ReuseLine: self._generate_reuse,
                NullLine: self._generate_null,
                CommBufferLine: self._generate_comm_buffer,
                CommBufferAllocateLine: self._generate_comm_buffer_allocate,
                CommBufferFreeLine: self._generate_comm_buffer_free,
                KernelDefinitionLine: self._generate_kernel_definition,
                KernelCallLine: self._generate_kernel_call,
            }.get(line_type)

            # FX conversion only supports Wrapper IR, not Python/C++ lines.
            if conversion_func is None:
                raise NotImplementedError(
                    textwrap.dedent(
                        f"""
                    Found line of unrecognized type '{line_type}':
                        '{line}'

                    FX conversion only supports Wrapper IR lines.
                    """
                    )
                )

            conversion_func(line)
        self._generate_graph_outputs()

        self.gm.recompile()

    def _generate_allocate(self, line: Line) -> None:
        assert isinstance(line, AllocateLine)
        buffer = line.node
        name = buffer.name
        assert name not in V.graph.removed_buffers

        device = buffer.get_device()
        dtype = buffer.get_dtype()
        shape = tuple(buffer.get_size())
        stride = tuple(buffer.get_stride())

        node = self.gm.graph.call_function(
            torch.empty_strided,
            args=(shape, stride),
            kwargs={"dtype": dtype, "device": device},
        )
        assert name
        node.name = name
        self._create_meta_from_buffer(node, buffer)
        self._record_allocation(buffer, node)

    def _generate_comment(self, line: Line) -> None:
        assert isinstance(line, CommentLine)
        # We ignore comments in FX IR.

    def _generate_enter_device_context_manager(self, line: Line) -> None:
        assert isinstance(line, EnterDeviceContextManagerLine)
        # We ignore the device context in FX IR.

    def _generate_exit_device_context_manager(self, line: Line) -> None:
        assert isinstance(line, ExitDeviceContextManagerLine)
        # We ignore the device context in FX IR.

    def _generate_enter_subgraph(self, line: Line) -> None:
        assert isinstance(line, EnterSubgraphLine)
        raise NotImplementedError("Subgraphs are not yet supported by FX conversion")

    def _generate_exit_subgraph(self, line: Line) -> None:
        assert isinstance(line, ExitSubgraphLine)
        raise NotImplementedError("Subgraphs are not yet supported by FX conversion")

    def _generate_free(self, line: Line) -> None:
        assert isinstance(line, FreeLine)

        buf = line.node

        # No need to free placeholders.
        if self.buffer_to_node[buf.name].op == "placeholder":
            return

        self._free(buf)

    def _generate_free_if_not_reused(self, line: Line) -> None:
        assert isinstance(line, FreeIfNotReusedLine)
        buf = line.node
        assert buf.name not in V.graph.removed_buffers
        if not line.is_reused:
            self._free(buf)

    def _generate_line_context(self, line: Line) -> None:
        assert isinstance(line, LineContext)
        # We ignore line context in FX IR.

    def _generate_reinterpret(self, line: Line) -> None:
        assert isinstance(line, ReinterpretLine)

        buffer = line.node
        layout = buffer.get_output_spec()
        assert isinstance(layout, ir.NonOwningLayout)

        # Look up the input view.
        input_buffer = self._get_buffer(layout.view)
        input_node = self.buffer_to_node[input_buffer.get_name()]

        # Look up output metadata.
        name = buffer.get_name()
        assert name
        size = tuple(layout.size)
        stride = tuple(layout.stride)
        offset = buffer.get_offset() + layout.offset

        # Map ReinterpretView to as_strided.
        result_node = self.gm.graph.call_function(
            torch.as_strided, args=(input_node, size, stride, offset)
        )
        result_node.name = name
        result_node.meta["val"] = self._fake_tensor(
            size,
            stride,
            dtype=buffer.get_dtype(),
            device=buffer.get_device(),
        )
        self._record_allocation(buffer, result_node)

    def _generate_reuse(self, line: Line) -> None:
        assert isinstance(line, ReuseLine)
        old = line.node
        new = line.reused_as
        assert not any(buf.name in V.graph.removed_buffers for buf in (old, new))
        assert old.get_dtype() == new.get_dtype()

        old_node = self.buffer_to_node[old.name]
        result_node = old_node

        # Change shape and stride.
        size = new.get_size()
        stride = new.get_stride()
        offset = new.get_offset()
        if (
            old.get_size() != size
            or old.get_stride() != stride
            or old.get_offset() != offset
        ):
            result_node = self.gm.graph.call_function(
                torch.as_strided, args=(old_node, size, stride, offset)
            )
            self._create_meta_from_buffer(result_node, new)

        self._record_allocation(new, result_node)

        # Free the old buffer, if we allocated a new tensor.
        if (
            old.name not in V.graph.get_output_names()
            and line.delete_old
            and result_node is not old_node
        ):
            self._free(old)

    def _generate_null(self, line: Line) -> None:
        assert isinstance(line, NullLine)
        # Does nothing.

    def _generate_comm_buffer(self, line: Line) -> None:
        assert isinstance(line, CommBufferLine)
        # Does nothing. Comm buffers are handled by the respective allocate/free lines.

    def _generate_comm_buffer_allocate(self, line: Line) -> None:
        assert isinstance(line, CommBufferFreeLine)
        buf = line.node
        assert buf.name not in V.graph.removed_buffers
        device = torch.device(f"cuda:{self.node.get_device().index}")
        dtype = buf.get_dtype()
        shape = tuple(buf.get_size())
        stride = tuple(buf.get_stride())

        if self.comm_buffer_type != ir.CommBufferType.SYMM_MEM:
            raise NotImplementedError(
                f"Unsupported comm buffer type: {self.comm_buffer_type}"
            )

        # Distributed is not always avaliable. Only import it if used.
        from torch._C._distributed_c10d import _SymmetricMemory  # noqa: F401

        alloc_id = random.randint(0, 2**64 - 1)
        node = self.gm.graph.call_function(
            _SymmetricMemory.empty_strided_p2p,
            args=(shape, stride, dtype, device, self.group_name, alloc_id),
        )
        self._create_meta_from_buffer(node, buf)
        self._record_allocation(buf, node)

    def _generate_comm_buffer_free(self, line: Line) -> None:
        assert isinstance(line, CommBufferFreeLine)
        self.gm.graph.free(line.node)

    def _generate_triton_call(self, line: Line) -> None:
        assert isinstance(line, KernelCallLine)

        if line.grid_fn not in ("grid", None):
            raise NotImplementedError(f"Unsupported grid_fn: '{line.grid_fn}'")

        # Collect all kwargs, including autotuned block sizes.
        call_args = self._lookup_args(line.call_args)
        kernel = self.kernels[line.kernel_name]
        call_kwargs = dict(zip(kernel.tuner.triton_meta["signature"], call_args))
        tuned_kwargs = kernel.tuner.compile_results[0].config.kwargs
        call_kwargs.update(tuned_kwargs)

        # Convert sympy expressions to symints.
        for name, val in call_kwargs.items():
            if isinstance(val, sympy.Expr):
                call_kwargs[name] = convert_to_symint(val)

        # Store non-graphable kwargs in the side table.
        (
            call_kwargs,
            constant_args_idx,
        ) = tracing_triton_hopifier_singleton.store_non_graphable_args(call_kwargs)

        self.gm.graph.call_function(
            triton_kernel_wrapper_mutation,
            kwargs={
                "kernel_idx": kernel.wrapped.kernel_idx,
                "constant_args_idx": constant_args_idx,
                "grid": [line.grid],
                "tma_descriptor_metadata": {},
                "kwargs": call_kwargs,
            },
        )

    def generate_extern_kernel_out(
        self,
        kernel: str,
        out: str,
        out_view: Optional[str],
        args: list[str],
        node: ir.ExternKernelOut,
    ) -> None:
        # TODO: refactor the codegen from ir.ExternKernelOut into various wrapper codegen
        # classes. We should only need to pass the node here.

        # Get the call args in their original types.
        tensor_args = tuple(x.codegen_reference() for x in node.inputs)
        call_args = tensor_args + tuple(node.constant_args)

        out_name = out_view if out_view else out
        self.writeline(
            ExternKernelLine(
                self, kernel=kernel, out=out_name, args=call_args, kwargs=node.kwargs
            )
        )

    def generate_kernel_call(
        self,
        kernel_name: str,
        call_args,
        grid=None,
        device_index=None,
        gpu=True,
        triton=True,
        arg_types=None,
        raw_args=None,
        grid_fn: str = "grid",
        triton_meta=None,
        autotune_configs=None,
        grid_extra_kwargs="",
    ) -> None:
        """
        Generates Wrapper IR for a kernel call.
        """
        self.writeline(
            KernelCallLine(
                self,
                kernel_name=kernel_name,
                call_args=call_args,
                grid=tuple(grid),
                grid_fn=grid_fn,
                triton=triton,
            )
        )

    def _generate_extern_kernel(self, line: Line) -> None:
        assert isinstance(line, ExternKernelLine)

        # Look up the kernel function from its name.
        module_name, kernel_name = line.kernel.split(".", 1)
        op = globals()[module_name]  # E.g. extern_kernels, aten, etc.
        for subname in kernel_name.split("."):
            op = getattr(op, subname)  # E.g. extern_kernels.addmm

        out_node = self.buffer_to_node[line.out]

        # Separate args from kwargs
        arg_nodes = self._lookup_args(line.args)
        kwargs = line.kwargs | {"out": out_node}
        self.gm.graph.call_function(op, args=arg_nodes, kwargs=kwargs)

    def _generate_kernel_call(self, line: Line) -> None:
        assert isinstance(line, KernelCallLine)
        if not line.triton:
            raise NotImplementedError("FX conversion only supports Triton kernels.")

        self._generate_triton_call(line)

    def _generate_kernel_definition(self, line: Line) -> None:
        assert isinstance(line, KernelDefinitionLine)

        # Generate code for the kernel.
        kernel_code = super()._format_kernel_definition(
            line.kernel_name, line.kernel_body, line.metadata
        )

        # Import the module and store the JIT kernel.
        mod = self._import_kernel(kernel_code)
        kernel = getattr(mod, line.kernel_name)
        if isinstance(kernel, TritonFuture):
            kernel = kernel.kernel
        assert isinstance(kernel, CachingAutotuner)
        self.kernels[line.kernel_name] = TritonKernel(kernel)
