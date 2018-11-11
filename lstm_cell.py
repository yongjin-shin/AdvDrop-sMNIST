import numpy as np
import tensorflow as tf
import collections
from tensorflow.python.ops.rnn_cell import RNNCell
from tensorflow.python.framework import ops

_LSTMStateTuple = collections.namedtuple("LSTMStateTuple", ("c", "h"))

# keep_dims -> keepdims
class LSTMStateTuple(_LSTMStateTuple):
    """Tuple used by LSTM Cells for `state_size`, `zero_state`, and output state.
    Stores two elements: `(c, h)`, in that order.
    Only used when `state_is_tuple=True`.
    """
    __slots__ = ()

    @property
    def dtype(self):
        (c, h) = self
        if c.dtype != h.dtype:
            raise TypeError("Inconsistent internal state: %s vs %s" %
                            (str(c.dtype), str(h.dtype)))
        return c.dtype


class BasicLSTMCell(RNNCell):
    def __init__(self, num_units, W_both, W_b, forget_bias=1.0,
                 state_is_tuple=True, activation=None, reuse=None):

        super(BasicLSTMCell, self).__init__(_reuse=reuse)
        if not state_is_tuple:
            logging.warn("%s: Using a concatenated state is slower and will soon be "
                         "deprecated.  Use state_is_tuple=True.", self)
        self._num_units = num_units
        self.W_both = W_both
        self.W_b = W_b
        self._forget_bias = forget_bias
        self._state_is_tuple = state_is_tuple
        self._activation = activation or tf.tanh

    @property
    def state_size(self):
        return (LSTMStateTuple(self._num_units, self._num_units) if self._state_is_tuple else 2 * self._num_units)

    @property
    def output_size(self):
        return self._num_units

    def call(self, inputs, state):
        """Long short-term memory cell (LSTM).
        Args:
          inputs: `2-D` tensor with shape `[batch_size x input_size]`.
          state: An `LSTMStateTuple` of state tensors, each shaped
            `[batch_size x self.state_size]`, if `state_is_tuple` has been set to
            `True`.  Otherwise, a `Tensor` shaped
            `[batch_size x 2 * self.state_size]`.
        Returns:
          A pair containing the new hidden state, and the new state (either a
            `LSTMStateTuple` or a concatenated state, depending on
            `state_is_tuple`).
        """
        sigmoid = tf.sigmoid
        # Parameters of gates are concatenated into one multiply for efficiency.
        if self._state_is_tuple:
            c, h = state
        else:
            c, h = tf.split(value=state, num_or_size_splits=2, axis=1)

        concat = tf.matmul(tf.concat([inputs, h], 1), self.W_both)
        concat = tf.nn.bias_add(concat, self.W_b)

        # i = input_gate, j = new_input, f = forget_gate, o = output_gate
        i, j, f, o = tf.split(value=concat, num_or_size_splits=4, axis=1)

        new_c = (
            c * sigmoid(f + self._forget_bias) + sigmoid(i) * self._activation(j))
        new_h = self._activation(new_c) * sigmoid(o)

        if self._state_is_tuple:
            new_state = LSTMStateTuple(new_c, new_h)
        else:
            new_state = tf.concat([new_c, new_h], 1)
        return new_h, new_state


class LSTMCell(RNNCell):
    '''Vanilla LSTM implemented with same initializations as BN-LSTM'''

    def __init__(self, num_units, W_xh, W_hh, bias):
        self.num_units = num_units
        self.W_xh = W_xh
        self.W_hh = W_hh
        self.bias = bias

    @property
    def state_size(self):
        return (self.num_units, self.num_units)

    @property
    def output_size(self):
        return self.num_units

    def __call__(self, x, state, scope=None):
        with tf.variable_scope(scope or type(self).__name__):
            c, h = state

            # hidden = tf.matmul(x, W_xh) + tf.matmul(h, W_hh) + bias
            # improve speed by concat.
            concat = tf.concat(1, [x, h])
            W_both = tf.concat(0, [self.W_xh, self.W_hh])
            hidden = tf.matmul(concat, W_both) + self.bias

            i, j, f, o = tf.split(1, 4, hidden)

            new_c = c * tf.sigmoid(f) + tf.sigmoid(i) * tf.tanh(j)
            new_h = tf.tanh(new_c) * tf.sigmoid(o)

            return new_h, (new_c, new_h)


class DropoutMaskWrapper(RNNCell):
    """Operator adding dropout to inputs and outputs of the given cell."""

    def __init__(self, cell, state_mask, input_size=None, dtype=None):
        """Create a cell with added input, state, and/or output dropout.
        If `variational_recurrent` is set to `True` (**NOT** the default behavior),
        then the same dropout mask is applied at every step, as described in:
        Y. Gal, Z Ghahramani.  "A Theoretically Grounded Application of Dropout in
        Recurrent Neural Networks".  https://arxiv.org/abs/1512.05287
        Otherwise a different dropout mask is applied at every time step.
        Args:
          cell: an RNNCell, a projection to output_size is added to it.
          input_keep_prob: unit Tensor or float between 0 and 1, input keep
            probability; if it is constant and 1, no input dropout will be added.
          output_keep_prob: unit Tensor or float between 0 and 1, output keep
            probability; if it is constant and 1, no output dropout will be added.
          state_keep_prob: unit Tensor or float between 0 and 1, output keep
            probability; if it is constant and 1, no output dropout will be added.
            State dropout is performed on the *output* states of the cell.
          variational_recurrent: Python bool.  If `True`, then the same
            dropout pattern is applied across all time steps per run call.
            If this parameter is set, `input_size` **must** be provided.
          input_size: (optional) (possibly nested tuple of) `TensorShape` objects
            containing the depth(s) of the input tensors expected to be passed in to
            the `DropoutWrapper`.  Required and used **iff**
             `variational_recurrent = True` and `input_keep_prob < 1`.
          dtype: (optional) The `dtype` of the input, state, and output tensors.
            Required and used **iff** `variational_recurrent = True`.
          seed: (optional) integer, the randomness seed.
        Raises:
          TypeError: if cell is not an RNNCell.
          ValueError: if any of the keep_probs are not between 0 and 1.
        """
        # if not _like_rnncell(cell):
        #  raise TypeError("The parameter cell is not a RNNCell.")

        # Set cell, variational_recurrent, seed before running the code below
        self._cell = cell
        self._state_mask = state_mask

        self._recurrent_state_noise = state_mask

        '''
        if variational_recurrent:
          if dtype is None:
            raise ValueError(
                "When variational_recurrent=True, dtype must be provided")
    
          if (not isinstance(self._input_keep_prob, numbers.Real) or
              self._input_keep_prob < 1.0):
            if input_size is None:
              raise ValueError(
                  "When variational_recurrent=True and input_keep_prob < 1.0 or "
                  "is unknown, input_size must be provided")
            self._recurrent_input_noise = _enumerated_map_structure( lambda i, s: batch_noise(s, inner_seed=self._gen_seed("input", i)), input_size)
          
        self._recurrent_state_noise = _enumerated_map_structure( lambda i, s: batch_noise(s, inner_seed=self._gen_seed("state", i)), cell.state_size)
        self._recurrent_output_noise = _enumerated_map_structure( lambda i, s: batch_noise(s, inner_seed=self._gen_seed("output", i)), cell.output_size)
        '''

    def set_random_noises(self):
        self._recurrent_state_noise = _enumerated_map_structure(
            lambda i, s: self.batch_noise(s, inner_seed=self._gen_seed("state", i)), self._cell.state_size)

    def convert_to_batch_shape(self, s):
        # Prepend a 1 for the batch dimension; for recurrent
        # variational dropout we use the same dropout mask for all
        # batch elements.
        return tf.concat(([1], tensor_shape.TensorShape(s).as_list()), 0)

    def batch_noise(self, s, inner_seed):
        shape = self.convert_to_batch_shape(s)
        return tf.python.ops.random_ops.random_uniform(shape, seed=inner_seed, dtype=dtype)

    def _gen_seed(self, salt_prefix, index):
        if self._seed is None:
            return None
        salt = "%s_%d" % (salt_prefix, index)
        string = (str(self._seed) + salt).encode("utf-8")
        return int(hashlib.md5(string).hexdigest()[:8], 16) & 0x7FFFFFFF

    @property
    def state_size(self):
        return self._cell.state_size

    @property
    def output_size(self):
        return self._cell.output_size

    def zero_state(self, batch_size, dtype):
        with ops.name_scope(type(self).__name__ + "ZeroState", values=[batch_size]):
            return self._cell.zero_state(batch_size, dtype)

    def _variational_recurrent_dropout_value(self, index, value, noise):
        """Performs dropout given the pre-calculated noise tensor."""
        # uniform [keep_prob, 1.0 + keep_prob)
        # random_tensor = keep_prob + noise

        # 0. if [keep_prob, 1.0) and 1. if [1.0, 1.0 + keep_prob)
        # binary_tensor = tf.floor(random_tensor)
        binary_tensor = noise
        sum_binary_tensor = tf.reduce_sum(binary_tensor, axis=1, keep_dims=True)
        sum_full_dims = tf.reduce_sum(tf.ones_like(binary_tensor), axis=1, keep_dims=True)
        ratio_of_binary_tensor = sum_binary_tensor / sum_full_dims  # tf.div(sum_binary_tensor, sum_full_dims)

        ret = value / ratio_of_binary_tensor * binary_tensor
        ret.set_shape(value.get_shape())
        return ret

    def _dropout(self, values, salt_prefix, recurrent_noise):
        """Decides whether to perform standard dropout or recurrent dropout."""
        # def dropout(i, v, n):
        #    return self._variational_recurrent_dropout_value(i, v, n)
        # return _enumerated_map_structure(dropout, values, recurrent_noise)
        return self._variational_recurrent_dropout_value(0, values, recurrent_noise)

    def __call__(self, inputs, state, scope=None):
        """Run the cell with the declared dropouts."""

        output, new_state = self._cell(inputs, state, scope)
        c, h = new_state
        new_h = self._dropout(h, "state", self._recurrent_state_noise)
        return output, (c, new_h)


def orthogonal(shape):
    flat_shape = (shape[0], np.prod(shape[1:]))
    a = np.random.normal(0.0, 1.0, flat_shape)
    u, _, v = np.linalg.svd(a, full_matrices=False)
    q = u if u.shape == flat_shape else v
    return q.reshape(shape)


def orthogonal_initializer():
    def _initializer(shape, dtype=tf.float32, partition_info=None):
        return tf.constant(orthogonal(shape), dtype)

    return _initializer
