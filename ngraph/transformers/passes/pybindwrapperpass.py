# ----------------------------------------------------------------------------
# Copyright 2017 Nervana Systems Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
from __future__ import division
from ngraph.transformers.passes.passes import PeepholeGraphPass
from ngraph.util.generics import generic_method
from ngraph.op_graph.op_graph import Op, Add, Multiply, BroadcastOp, TensorValueOp, \
    DotOp, LogOp, ExpOp, Sum, Greater, Maximum, ReductionOp, AssignableTensorOp, ReorderAxes, \
    OneHotOp, Divide, Subtract, NegativeOp, ReciprocalOp, TensorSizeOp, MapRolesOp, Minimum, \
    Less, Max, SequentialOp, AssignOp, ParallelOp

from pyngraph import Type
from pyngraph.op import Parameter
from pyngraph.op import Constant
from pyngraph.op import Sum as PyngSum
from pyngraph.op import Maximum as PyngMaximum
from pyngraph.op import Minimum as PyngMinimum
from pyngraph.op import Greater as PyngGreater
from pyngraph.op import Less as PyngLess
from pyngraph.op import Broadcast as PyngBroadcast
from pyngraph.op import Dot as PyngDot
from pyngraph.op import Log as PyngLog
from pyngraph.op import Exp as PyngExp
from pyngraph.op import Reshape as PyngReshape
from pyngraph.op import OneHot as PyngOneHot
from pyngraph.op import Negative as PyngNegative
from pyngraph.op import Convert as PyngConvert
from pyngraph.op import Reduce as PyngReduce
from pyngraph import Function as Function


class PybindScopePass:
    """
    Graph pass mark Variable version scope
    Track AssignOp, SequentionOp and ParallelOp

    Arguments
        transformer (obj:`Transformer`): The associated transformer.
    """

    def __init__(self, computation, **kwargs):
        self.computation = computation

    # scope rules:
    # - Do a pre-order traversal of op_graph
    #     - Default scope is "root"
    #     - Other scopes are formed by appending enclosing ParallelOp and SequentialOp
    #       to default scope like a posix path
    #     - Tag all ops with the enclosing scope name
    def recordscope(self, results):

        def extend_scope(scope, leaf):
            return scope + '/' + leaf

        def new_seq_scope(scope):
            new_scope = extend_scope(scope, 'seq' + str(self.computation.seqcount))
            self.computation.seqcount += 1
            return new_scope

        def new_par_scope(scope):
            new_scope = extend_scope(scope, 'par' + str(self.computation.parcount))
            self.computation.parcount += 1
            return new_scope

        def visit_pre_order(scope, op):
            if op not in self.computation.scopevisited:
                if isinstance(op, SequentialOp):
                    childscope = new_seq_scope(scope)
                    children = op.ops
                elif isinstance(op, ParallelOp):
                    childscope = new_par_scope(scope)
                    children = op.control_deps
                else:
                    childscope = scope
                    children = op.args
                self.computation.scopevisited.add(op)
                self.computation.scopemark[op] = scope
                for child in children:
                    visit_pre_order(childscope, child)

        for op in results:
            visit_pre_order('root', op)

        # for key, val in self.computation.scopemark.items():
        #    print(key.name, val)

    def __call__(self, results):
        self.recordscope(results)


class PybindWrapperGenerator(PeepholeGraphPass):
    """
    Graph pass to generate the PybindWrapper's by visiting all the Op's
    needed to compute the results.

    Arguments
        transformer (obj:`Transformer`): The associated transformer.
    """

    def __init__(self, transformer, computation, **kwargs):
        super(PybindWrapperGenerator, self).__init__(**kwargs)
        self.transformer = transformer
        self.computation = computation

    def np_reduction_axis(self, op):
        """
        Returns numpy reduction axis of an op

        Args:
            op: instance of ReductionOp

        Returns:
            tuple of numpy reduction axis
        """
        if not isinstance(op, ReductionOp):
            raise ValueError("Op %s must be an instance of ReductionOp" % op)
        input_axes = op.args[0].axes
        reduction_axes = op.reduction_axes
        try:
            np_axis = tuple([input_axes.index(axis) for axis in reduction_axes])
        except ValueError:
            np_axis = tuple([0, ])
        return np_axis[0] if len(np_axis) == 1 else np_axis

    def get_reduction_axis(self, op):
        """
        Returns int value which is proportional to the same axes shared by the input tensors
        :param op:
        :return: int value
        """
        count_common_axis = 0
        reduction_axes = []
        input1_axes = op.args[0].axes.names
        input2_axes = op.args[1].axes.names
        for axis in input1_axes:
            if axis in input2_axes:
                count_common_axis += 1
                reduction_axes.append(axis)
        return (count_common_axis, tuple(reduction_axes))

    def get_shape_from_axes_order(self, axes_order, input_shape):
        """
        returns the shape of the input for transpose based on the given axes_order
        :param axes_order:
        :param input_shape:
        :return:
        """
        # determine the axis order for the reshape
        reorder_shape = []
        for index in axes_order:
            reorder_shape.append(input_shape[index])
        return reorder_shape

    def get_axes_order_from_axes_name(self, input_axes, reshape_axes):
        reshape_axis_order = []
        for pos, val in enumerate(reshape_axes):
            reshape_axis_order.append(input_axes.index(val))

        return reshape_axis_order

    @generic_method(dispatch_base_type=Op)
    def visit(self, op, *args):
        self.computation.set_op_rank(op)

    @visit.on_type(Add)
    def visit(self, op, x, y):
        self.computation.set_op_rank(op)
        ngraph_cpp_add_op = self.computation.lookup_cpp_op(x.tensor) \
            + self.computation.lookup_cpp_op(y.tensor)

        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_add_op

    @visit.on_type(Divide)
    def visit(self, op, x, y):
        self.computation.set_op_rank(op)
        ngraph_cpp_div_op = self.computation.lookup_cpp_op(x.tensor) \
            / self.computation.lookup_cpp_op(y.tensor)

        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_div_op

    @visit.on_type(Multiply)
    def visit(self, op, x, y):
        self.computation.set_op_rank(op)
        ngraph_cpp_mul_op = self.computation.lookup_cpp_op(x.tensor) \
            * self.computation.lookup_cpp_op(y.tensor)

        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_mul_op

    @visit.on_type(Subtract)
    def visit(self, op, x, y):
        self.computation.set_op_rank(op)
        ngraph_cpp_sub_op = self.computation.lookup_cpp_op(x.tensor) \
            - self.computation.lookup_cpp_op(y.tensor)

        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_sub_op

    @visit.on_type(BroadcastOp)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        axis_set = set()
        element_type = Type.f32
        # check if the op.args already have Paramterized view type.
        if self.computation.lookup_cpp_op(op.args[0].tensor) is not None:
            op_element_type = self.computation.lookup_cpp_op(op.args[0].tensor)
        else:
            op_element_type = Parameter(
                element_type, list(op.args[0].axes.lengths))
        # build axis_set
        broadcast_axes = op.axes.lengths
        broadcast_args_axes = op.args[0].axes.lengths

        for pos, axis in enumerate(broadcast_axes):
            if axis not in broadcast_args_axes:
                axis_set.add(pos)

        self.computation.ngraph_cpp_ops[op.tensor] = \
            PyngBroadcast(op_element_type, list(op.axes.lengths), axis_set)

    def flatten(self, container):
        if isinstance(container, (list, tuple)):
            for i in container:
                if isinstance(i, (list, tuple)):
                    for j in self.flatten(i):
                        yield j
                else:
                    yield i
        else:
            yield container

    @visit.on_type(TensorValueOp)
    def visit(self, op):
        self.computation.set_op_rank(op)
        if self.computation.lookup_cpp_op(op) is None:
            if op.tensor.is_constant:
                # FIXME: make tensors based on data type
                constant_op = Constant(Type.f32,
                                       list(op.axes.lengths),
                                       list(self.flatten(op.tensor.const.tolist())))

                self.computation.ngraph_cpp_ops[op.tensor] = constant_op
            else:
                op_element_type = Parameter(Type.f32, list(op.axes.lengths))
                self.computation.ngraph_cpp_ops[op.tensor] = op_element_type

    @visit.on_type(AssignableTensorOp)
    def visit(self, op):
        raise RuntimeError("Should not visit AssignableTensorOp")

    @visit.on_type(DotOp)
    def visit(self, op, input1, input2):
        self.computation.set_op_rank(op)
        # determine the reduction_axes count
        reduction_axes_count, reduction_axes = self.get_reduction_axis(op)

        # check if the input1/input2 needs to be Transposed and if yes, Transpose
        if (len(input1.axes.names) != 0 and len(input2.axes.names) != 0) \
                and (input1.axes.names[-1] != input2.axes.names[0]):

            input1_reshape_axes = list((op.x_out_axes + op.reduction_axes).names)
            input2_reshape_axes = list((op.reduction_axes + op.y_out_axes).names)
            input1_axes_order = self.get_axes_order_from_axes_name(
                input1.axes.names, input1_reshape_axes)
            input1_reorder_op = PyngReshape(
                self.computation.lookup_cpp_op(input1.tensor),
                input1_axes_order,
                self.get_shape_from_axes_order(
                    input1_axes_order,
                    input1.axes.lengths))
            input2_axes_order = self.get_axes_order_from_axes_name(
                input2.axes.names, input2_reshape_axes)
            input2_reorder_op = PyngReshape(
                self.computation.lookup_cpp_op(input2.tensor),
                input2_axes_order,
                self.get_shape_from_axes_order(
                    input2_axes_order,
                    input2.axes.lengths))
            ngraph_cpp_dot_op = PyngDot(input1_reorder_op, input2_reorder_op,
                                        reduction_axes_count)
        else:
            ngraph_cpp_dot_op = PyngDot(
                self.computation.lookup_cpp_op(input1.tensor),
                self.computation.lookup_cpp_op(input2.tensor),
                reduction_axes_count)

        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_dot_op

    @visit.on_type(LogOp)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        ngraph_cpp_log_op = PyngLog(self.computation.lookup_cpp_op(input.tensor))
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_log_op

    @visit.on_type(ExpOp)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        ngraph_cpp_exp_op = PyngExp(self.computation.lookup_cpp_op(input.tensor))
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_exp_op

    @visit.on_type(Greater)
    def visit(self, op, input1, input2):
        self.computation.set_op_rank(op)
        ngraph_cpp_greater_op = PyngGreater(
            self.computation.lookup_cpp_op(input1.tensor),
            self.computation.lookup_cpp_op(input2.tensor))
        # convert the element back from bool to float type
        element_result_type = Type.f32
        greater_result_op = PyngConvert(ngraph_cpp_greater_op, element_result_type)
        self.computation.ngraph_cpp_ops[op.tensor] = greater_result_op

    @visit.on_type(Less)
    def visit(self, op, input1, input2):
        self.computation.set_op_rank(op)
        ngraph_cpp_less_op = PyngLess(
            self.computation.lookup_cpp_op(input1.tensor),
            self.computation.lookup_cpp_op(input2.tensor))
        # convert the element back from bool to float type
        element_result_type = Type.f32
        less_result_op = PyngConvert(ngraph_cpp_less_op, element_result_type)
        self.computation.ngraph_cpp_ops[op.tensor] = less_result_op

    @visit.on_type(Sum)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        if isinstance(self.np_reduction_axis(op), tuple):
            axis_set = self.np_reduction_axis(op)
        else:
            axis_set = tuple()
            axis_set += (self.np_reduction_axis(op),)

        ngraph_cpp_sum_op = PyngSum(
            self.computation.lookup_cpp_op(input.tensor),
            set(axis_set))
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_sum_op

    @visit.on_type(Maximum)
    def visit(self, op, input1, input2):
        self.computation.set_op_rank(op)
        ngraph_cpp_maximum_op = PyngMaximum(
            self.computation.lookup_cpp_op(input1.tensor),
            self.computation.lookup_cpp_op(input2.tensor))
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_maximum_op

    @visit.on_type(Minimum)
    def visit(self, op, input1, input2):
        self.computation.set_op_rank(op)
        ngraph_cpp_minimum_op = PyngMinimum(
            self.computation.lookup_cpp_op(input1.tensor),
            self.computation.lookup_cpp_op(input2.tensor))
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_minimum_op

    @visit.on_type(ReorderAxes)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        axis_order = []
        reorder_axes = list(op.axes.lengths)
        input_axes = list(op.args[0].axes.lengths)

        # determine the axis order for the reshape
        for pos, val in enumerate(input_axes):
            axis_order.append(reorder_axes.index(val))
        ngraph_cpp_reorder_op = PyngReshape(
            self.computation.lookup_cpp_op(op.args[0].tensor),
            axis_order,
            reorder_axes)
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_reorder_op

    @visit.on_type(OneHotOp)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        onehot_shape = list(op.axes.lengths)
        one_hot_axis = (op.axes).index(op.axis)
        ngraph_cpp_onehot_op = PyngOneHot(
            self.computation.lookup_cpp_op(op.args[0].tensor),
            onehot_shape,
            one_hot_axis)
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_onehot_op

    @visit.on_type(NegativeOp)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        ngraph_cpp_neg_op = PyngNegative(
            self.computation.lookup_cpp_op(input.tensor))
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_neg_op

    @visit.on_type(ReciprocalOp)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        input_axes = list(input.axes.lengths)
        constant_op = Constant(Type.f32, input_axes, [1])
        ngraph_cpp_reciprocal_op = constant_op \
            / self.computation.lookup_cpp_op(input.tensor)
        self.computation.ngraph_cpp_ops[op.tensor] = ngraph_cpp_reciprocal_op

    @visit.on_type(TensorSizeOp)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        # TODO - is treating TensorSizeOp as constants, okay?
        # Construct constant list with number of elements = reduction axes size
        constant_tensor = [op.reduction_axes.size]
        constant_op = Constant(Type.f32,
                               [], constant_tensor)
        self.computation.ngraph_cpp_ops[op.tensor] = constant_op

    @visit.on_type(MapRolesOp)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        # TODO - made it as workaround, need to check if this acceptable ?
        self.computation.ngraph_cpp_ops[op.tensor] = \
            self.computation.lookup_cpp_op(op.args[0].tensor)

    @visit.on_type(Max)
    def visit(self, op, input):
        self.computation.set_op_rank(op)
        # Define the reduction function handle
        element_type = Type.f32
        shape = []
        f_a = Parameter(element_type, shape)
        f_b = Parameter(element_type, shape)
        ngraph_cpp_min_op = PyngMaximum(f_a, f_b)
        fn = Function([ngraph_cpp_min_op], [f_a, f_b], "ReductionOp")

        # define the reduction op with the above defined Function handle
        if isinstance(self.np_reduction_axis(op), tuple):
            axis_set = self.np_reduction_axis(op)
        else:
            axis_set = tuple()
            axis_set += (self.np_reduction_axis(op),)
        g_a = self.computation.lookup_cpp_op(input.tensor)
        const_max_default_value = [float('-inf')]
        g_b = Constant(Type.f32, [], const_max_default_value)
        self.computation.ngraph_cpp_ops[op.tensor] = \
            PyngReduce(g_a, g_b, fn, set(axis_set))

    @visit.on_type(SequentialOp)
    def visit(self, op):
        self.computation.set_op_rank(op)
        # Legal child patterns
        # 1. (AssignOp,)+, (~(AssignOp|SequentialOp|ParallelOp))
        # 2. ParallelOp, (~(AssignOp|SequentialOp|ParallelOp))
        # 3. SequentialOp, (~(AssignOp|SequentialOp|ParallelOp))

        # Output node is the last child op

    @visit.on_type(ParallelOp)
    def visit(self, op):
        self.computation.set_op_rank(op)
        # Legal child pattern
        # (AssignOp,)+

        # ParallelOp has no output node

    @visit.on_type(AssignOp)
    def visit(self, op, lhs, rhs):
        self.computation.set_op_rank(op)
        variable = lhs.tensor
        self.computation.set_op_rank(variable)
        if variable not in self.computation.variables_cpp_op:
            self.computation.variables_cpp_op[variable] = \
                (self.computation.scopemark[op], self.computation.ngraph_cpp_ops[rhs])
        else:
            raise RuntimeError("Variable updated more than once!")
