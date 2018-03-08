#!/usr/bin/env python
# ******************************************************************************
# Copyright 2017-2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ******************************************************************************
"""
CIFAR CONV with spelled out neon model framework in one file

The motivation is to show the flexibility of ngraph and how user can build a
model without the neon architecture. This may also help with debugging.

Run it using

python examples/cifar10/cifar10_conv.py --data_dir /usr/local/data/CIFAR --output_file out.hd5

"""
from __future__ import division
from __future__ import print_function
from contextlib import closing
import numpy as np
import neon as ng
from neon.frontend import Layer, Affine, Preprocess, Convolution, Pooling, Sequential
from neon.frontend import UniformInit, Rectlin, Softmax, GradientDescentMomentum
from neon.frontend import ax, loop_train
from neon.frontend import NeonArgparser, make_bound_computation, make_default_callbacks
from neon.frontend import ArrayIterator

from neon.frontend import CIFAR10
import neon.transformers as ngt

parser = NeonArgparser(description='Train simple CNN on cifar10 dataset')
parser.add_argument('--use_batch_norm', action='store_true',
                    help='whether to use batch normalization')
args = parser.parse_args()

np.random.seed(args.rng_seed)

# Create the dataloader
train_data, valid_data = CIFAR10(args.data_dir).load_data()
train_set = ArrayIterator(train_data, args.batch_size, total_iterations=args.num_iterations)
valid_set = ArrayIterator(valid_data, args.batch_size)

inputs = train_set.make_placeholders()
ax.Y.length = 10

######################
# Model specification


def cifar_mean_subtract(x):
    bgr_mean = ng.constant(
        const=np.array([104., 119., 127.]),
        axes=[x.axes.channel_axis()])

    return (x - bgr_mean) / 255.


init_uni = UniformInit(-0.1, 0.1)

seq1 = Sequential([Preprocess(functor=cifar_mean_subtract),
                   Convolution((5, 5, 16), filter_init=init_uni, activation=Rectlin(),
                               batch_norm=args.use_batch_norm),
                   Pooling((2, 2), strides=2),
                   Convolution((5, 5, 32), filter_init=init_uni, activation=Rectlin(),
                               batch_norm=args.use_batch_norm),
                   Pooling((2, 2), strides=2),
                   Affine(nout=500, weight_init=init_uni, activation=Rectlin(),
                          batch_norm=args.use_batch_norm),
                   Affine(axes=ax.Y, weight_init=init_uni, activation=Softmax())])

optimizer = GradientDescentMomentum(0.01, 0.9)
train_prob = seq1(inputs['image'], backend=args.backend)
train_loss = ng.cross_entropy_multi(train_prob, ng.one_hot(inputs['label'], axis=ax.Y))
batch_cost = ng.sequential([optimizer(train_loss), ng.mean(train_loss, out_axes=())])
train_outputs = dict(batch_cost=batch_cost)

with Layer.inference_mode_on():
    inference_prob = seq1(inputs['image'], backend=args.backend)
# errors = ng.not_equal(ng.argmax(inference_prob, out_axes=[ax.N]), inputs['label'])
eval_loss = ng.cross_entropy_multi(inference_prob, ng.one_hot(inputs['label'], axis=ax.Y))
eval_outputs = dict(results=inference_prob, cross_ent_loss=eval_loss)

"""
for op in ng.Op.ordered_ops([batch_cost]):
    print(type(op))
for op in ng.Op.ordered_ops([eval_loss]):
    print(type(op))
quit()
"""

# Now bind the computations we are interested in
with closing(ngt.make_transformer()) as transformer:
    train_computation = make_bound_computation(transformer, train_outputs, inputs)
    loss_computation = make_bound_computation(transformer, eval_outputs, inputs)

    cbs = make_default_callbacks(transformer=transformer,
                                 output_file=args.output_file,
                                 frequency=args.iter_interval,
                                 train_computation=train_computation,
                                 total_iterations=args.num_iterations,
                                 eval_set=valid_set,
                                 loss_computation=loss_computation,
                                 enable_top5=True,
                                 use_progress_bar=args.progress_bar)

    loop_train(train_set, train_computation, cbs)
