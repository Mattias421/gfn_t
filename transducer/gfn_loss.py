"""
Transducer loss implementation (depends on numba)

Authors
 * Abdelwahab Heba 2020
 * Titouan Parcollet 2023
"""

import logging
import math
import warnings

import torch
from torch.autograd import Function
from torch.nn import Module

from speechbrain.utils.logger import get_logger

NUMBA_VERBOSE = 0

logger = get_logger(__name__)

try:
    from numba import cuda

    # Numba is extra verbose and this may lead to log.txt file of multiple gigabytes... we deactivate
    if not NUMBA_VERBOSE:
        logger.info(
            "Numba verbose is deactivated. To enable it, set NUMBA_VERBOSE to 1."
        )

        nb_logger = logging.getLogger("numba")
        nb_logger.setLevel(logging.ERROR)  # only show error

        from numba.core.errors import NumbaPerformanceWarning

        warnings.simplefilter("ignore", category=NumbaPerformanceWarning)
    else:
        logger.info(
            "Numba verbose is enabled. To deactivate it, set NUMBA_VERBOSE to 0."
        )

except ImportError:
    err_msg = "The optional dependency Numba is needed to use this module\n"
    err_msg += "Cannot import numba. To use Transducer loss\n"
    err_msg += "Please follow the instructions below\n"
    err_msg += "=============================\n"
    err_msg += "If you use your localhost:\n"
    err_msg += "pip install numba\n"
    err_msg += "export NUMBAPRO_LIBDEVICE='/usr/local/cuda/nvvm/libdevice/' \n"
    err_msg += "export NUMBAPRO_NVVM='/usr/local/cuda/nvvm/lib64/libnvvm.so' \n"
    err_msg += "================================ \n"
    err_msg += "If you use conda:\n"
    err_msg += "conda install numba cudatoolkit"
    raise ImportError(err_msg)




@cuda.jit()
def cu_kernel_forward(log_probs, labels, alpha, log_p, log_r, T, U, blank, lock):
    """
    Compute forward pass for the forward-backward algorithm using Numba cuda kernel.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    Arguments
    ---------
    log_probs : torch.Tensor
        4D Tensor of (batch x TimeLength x LabelLength x outputDim) from the Transducer network.
    labels : torch.Tensor
        2D Tensor of (batch x MaxSeqLabelLength) containing targets of the batch with zero padding.
    alpha : torch.Tensor
        3D Tensor of (batch x TimeLength x LabelLength) for forward computation.
    log_p : torch.Tensor
        1D Tensor of (batch) for forward cost computation.
    T : torch.Tensor
        1D Tensor of (batch) containing TimeLength of each target.
    U : torch.Tensor
        1D Tensor of (batch) containing LabelLength of each target.
    blank : int
        Blank index.
    lock : torch.Tensor
        2D Tensor of (batch x LabelLength) containing bool(1-0) lock for parallel computation.
    """

    # parallelize the forward algorithm over batch and target length dim
    b = cuda.blockIdx.x
    u = cuda.threadIdx.x
    t = 0
    if u <= U[b]:
        # for each (B,U) Thread
        # wait the unlock of the previous computation of Alpha[b,U-1,:]
        # Do the computation over the whole Time sequence on alpha[B,U,:]
        # and then unlock the target U+1 for computation
        while t < T[b]:
            if u == 0:
                if t > 0:
                    alpha[b, t, 0] = (
                        alpha[b, t - 1, 0] + log_probs[b, t - 1, 0, blank]
                    )
                cuda.atomic.add(lock, (b, u + 1), -1)
                t += 1
            else:
                if cuda.atomic.add(lock, (b, u), 0) < 0:
                    if t == 0:
                        alpha[b, 0, u] = (
                            alpha[b, 0, u - 1]
                            + log_probs[b, 0, u - 1, labels[b, u - 1]]
                        )
                    else:
                        # compute emission prob
                        emit = (
                            alpha[b, t, u - 1]
                            + log_probs[b, t, u - 1, labels[b, u - 1]]
                        )
                        # compute no_emission prob
                        no_emit = (
                            alpha[b, t - 1, u] + log_probs[b, t - 1, u, blank]
                        )
                        # do logsumexp between log_emit and log_no_emit
                        alpha[b, t, u] = max(no_emit, emit) + math.log1p(
                            math.exp(-abs(no_emit - emit))
                        )
                    if u < U[b]:
                        cuda.atomic.add(lock, (b, u + 1), -1)
                    cuda.atomic.add(lock, (b, u), 1)
                    t += 1

        if u == U[b]:
            # for each thread b (utterance)
            # normalize the loss over time
            # log_p[b] = (
            #     alpha[b, T[b] - 1, U[b]] + log_probs[b, T[b] - 1, U[b], blank]
            # ) / T[b]

            # TODO does loss need to be normalised over time?

            # n = U[b]
            # m = 0
            for m in range(0,U[b]-1):
                for n in range(m+1,U[b]):

                    # R_m + P_n
                    sub_tb_loss = (log_r[b, m] - log_r[b, n]) + 2 * ((alpha[b, T[b] - 1, n] + log_probs[b, T[b] - 1, n, blank])/T[b] - (alpha[b, T[b] - 1, m] + log_probs[b, T[b] - 1, m, blank])/T[b])

                    log_p[b] = log_p[b] + (sub_tb_loss ** 2) # add squared loss


@cuda.jit()
def cu_kernel_compute_grad(log_probs, labels, alpha, grads, T, U, blank):
    """
    Compute gradient for the forward-backward algorithm using Numba cuda kernel.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    Arguments
    ---------
    log_probs : torch.Tensor
        4D Tensor of (batch x TimeLength x LabelLength x outputDim) from the Transducer network.
    labels : torch.Tensor
        2D Tensor of (batch x MaxSeqLabelLength) containing targets of the batch with zero padding.
    alpha : torch.Tensor
        3D Tensor of (batch x TimeLength x LabelLength) for backward computation.
    beta : torch.Tensor
        3D Tensor of (batch x TimeLength x LabelLength) for backward computation.
    grads : torch.Tensor
        Grads for backward computation.
    T : torch.Tensor
        1D Tensor of (batch) containing TimeLength of each target.
    U : torch.Tensor
        1D Tensor of (batch) containing LabelLength of each target.
    blank : int
        Blank index.
    """
    # parallelize the gradient computation over batch and timeseq length dim
    t = cuda.blockIdx.x
    b = cuda.threadIdx.x
    if t < T[b]:
        # compute the gradient for no_emit prob
        if t == 0:
            grads[b, T[b] - 1, U[b], blank] = -math.exp(
                alpha[b, T[b] - 1, U[b]]
                + log_probs[b, T[b] - 1, U[b], blank]
                - beta[b, 0, 0]
            )

        if t < T[b] - 1:
            for u in range(U[b] + 1):
                grads[b, t, u, blank] = alpha[b, t, u] + beta[b, t + 1, u]
                grads[b, t, u, blank] = -math.exp(
                    grads[b, t, u, blank]
                    + log_probs[b, t, u, blank]
                    - beta[b, 0, 0]
                )
        # compute the gradient for emit prob
        for u, l in enumerate(labels[b]):
            if u < U[b]:
                grads[b, t, u, l] = alpha[b, t, u] + beta[b, t, u + 1]
                grads[b, t, u, l] = -math.exp(
                    grads[b, t, u, l] + log_probs[b, t, u, l] - beta[b, 0, 0]
                )



class Transducer(Function):
    """
    This class implements the Transducer loss computation with forward-backward algorithm
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    This class use torch.autograd.Function. In fact of using the forward-backward algorithm,
    we need to compute the gradient manually.

    This class can't be instantiated, please refer to TransducerLoss class

    It is also possible to use this class directly by using Transducer.apply
    """


    @staticmethod
    def forward(ctx, log_probs, labels, log_r, T, U, blank, reduction):
        """Computes the transducer loss."""
        log_probs = log_probs.detach()
        B, maxT, maxU, A = log_probs.shape
        grads = torch.zeros(
            (B, maxT, maxU, A), dtype=log_probs.dtype, device=log_probs.device
        )
        alpha = torch.zeros(
            (B, maxT, maxU), device=log_probs.device, dtype=log_probs.dtype
        )
        lock = torch.zeros(
            (B, maxU), dtype=torch.int32, device=log_probs.device
        )
        log_p_alpha = torch.zeros(
            (B,), device=log_probs.device, dtype=log_probs.dtype
        )
        cu_kernel_forward[B, maxU](
            log_probs, labels, alpha, log_p_alpha, log_r, T, U, blank, lock
        )
        lock = lock * 0
        cu_kernel_compute_grad[maxT, B](
            log_probs, labels, alpha, log_r, grads, T, U, blank
        )
        ctx.grads = grads
        del alpha, lock, T, U, log_probs, labels
        torch.cuda.empty_cache()
        if reduction == "mean":
            return log_p_alpha.mean()
        elif reduction == "sum":
            return sum(log_p_alpha)
        elif reduction == "none":
            return log_p_alpha
        else:
            raise Exception("Unexpected reduction {}".format(reduction))



    # @staticmethod
    # def backward(ctx, grad_output):
    #     """Backward computations for the transducer loss."""
    #     grad_output = grad_output.view(-1, 1, 1, 1).to(ctx.grads)
    #     return ctx.grads.mul_(grad_output), None, None, None, None, None, None





class TransducerLoss(Module):
    """
    This class implements the Transduce loss computation with forward-backward algorithm.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    The TransducerLoss(nn.Module) use Transducer(autograd.Function)
    to compute the forward-backward loss and gradients.

    Input tensors must be on a cuda device.

    Arguments
    ---------
    blank : int
        Token to use as blank token.
    reduction : str
        Type of reduction to use, default "mean"

    Example
    -------
    >>> import torch
    >>> loss = TransducerLoss(blank=0)
    >>> logits = torch.randn((1,2,3,5)).cuda().requires_grad_()
    >>> labels = torch.Tensor([[1,2]]).cuda().int()
    >>> act_length = torch.Tensor([2]).cuda().int()
    >>> # U = label_length+1
    >>> label_length = torch.Tensor([2]).cuda().int()
    >>> l = loss(logits, labels, act_length, label_length)
    >>> l.backward()
    """

    def __init__(self, blank=0, reduction="mean"):
        super().__init__()
        self.blank = blank
        self.reduction = reduction
        self.loss = Transducer.apply
        try:
            cuda.cuda_paths
        except ImportError:
            err_msg = "cannot import numba. To use Transducer loss\n"
            err_msg += "=============================\n"
            err_msg += "If you use your localhost:\n"
            err_msg += "pip install numba\n"
            err_msg += (
                "export NUMBAPRO_LIBDEVICE='/usr/local/cuda/nvvm/libdevice/' \n"
            )
            err_msg += "export NUMBAPRO_NVVM='/usr/local/cuda/nvvm/lib64/libnvvm.so' \n"
            err_msg += "================================ \n"
            err_msg += "If you use conda:\n"
            err_msg += "conda install numba cudatoolkit=XX (XX is your cuda toolkit version)"
            raise ImportError(err_msg)


    def forward(self, logits, labels, T, U):
        """Computes the transducer loss."""
        # Transducer.apply function take log_probs tensor.
        if all(t.is_cuda for t in (logits, labels, T, U)):
            log_probs = logits.log_softmax(-1)
            return self.loss(
                log_probs, labels, T, U, self.blank, self.reduction
            )
        else:
            raise ValueError(
                f"Found inputs tensors to be on {[logits.device, labels.device, T.device, U.device]} while needed to be on a 'cuda' device to use the transducer loss."
            )

def transducer_loss(
    logits,
    targets,
    log_r,
    input_lens,
    target_lens,
    blank_index,
    reduction="mean",
    use_torchaudio=True,
):
    """Transducer loss, see `speechbrain/nnet/loss/transducer_loss.py`.

    Arguments
    ---------
    logits : torch.Tensor
        Predicted tensor, of shape [batch, maxT, maxU, num_labels].
    targets : torch.Tensor
        Target tensor, without any blanks, of shape [batch, target_len].
    input_lens : torch.Tensor
        Length of each utterance.
    target_lens : torch.Tensor
        Length of each target sequence.
    blank_index : int
        The location of the blank symbol among the label indices.
    reduction : str
        Specifies the reduction to apply to the output: 'mean' | 'batchmean' | 'sum'.
    use_torchaudio: bool
        If True, use Transducer loss implementation from torchaudio, otherwise,
        use Speechbrain Numba implementation.

    Returns
    -------
    The computed transducer loss.
    """
    input_lens = (input_lens * logits.shape[1]).round().int()
    target_lens = (target_lens * targets.shape[1]).round().int()

    if use_torchaudio:
        try:
            from torchaudio.functional import rnnt_loss
        except ImportError:
            err_msg = "The dependency torchaudio >= 0.10.0 is needed to use Transducer Loss\n"
            err_msg += "Cannot import torchaudio.functional.rnnt_loss.\n"
            err_msg += "To use it, please install torchaudio >= 0.10.0\n"
            err_msg += "==================\n"
            err_msg += "Otherwise, you can use our numba implementation, set `use_torchaudio=False`.\n"
            raise ImportError(err_msg)

        return rnnt_loss(
            logits,
            targets.int(),
            input_lens,
            target_lens,
            blank=blank_index,
            reduction=reduction,
        )
    else:

        # Transducer.apply function take log_probs tensor.
        log_probs = logits.log_softmax(-1)
        return Transducer.apply(
            log_probs, targets, log_r, input_lens, target_lens, blank_index, reduction
        )

@cuda.jit()
def cu_kernel_forward_only(log_probs, labels, alpha, log_p, log_r, emit, no_emit, T, U, blank, lock):
    """
    Compute forward pass for the forward-backward algorithm using Numba cuda kernel.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    Arguments
    ---------
    log_probs : torch.Tensor
        4D Tensor of (batch x TimeLength x LabelLength x outputDim) from the Transducer network.
    labels : torch.Tensor
        2D Tensor of (batch x MaxSeqLabelLength) containing targets of the batch with zero padding.
    alpha : torch.Tensor
        3D Tensor of (batch x TimeLength x LabelLength) for forward computation.
    log_p : torch.Tensor
        1D Tensor of (batch) for forward cost computation.
    T : torch.Tensor
        1D Tensor of (batch) containing TimeLength of each target.
    U : torch.Tensor
        1D Tensor of (batch) containing LabelLength of each target.
    blank : int
        Blank index.
    lock : torch.Tensor
        2D Tensor of (batch x LabelLength) containing bool(1-0) lock for parallel computation.
    """

    # parallelize the forward algorithm over batch and target length dim
    b = cuda.blockIdx.x
    u = cuda.threadIdx.x
    t = 0
    if u <= U[b]:
        # for each (B,U) Thread
        # wait the unlock of the previous computation of Alpha[b,U-1,:]
        # Do the computation over the whole Time sequence on alpha[B,U,:]
        # and then unlock the target U+1 for computation
        while t < T[b]:
            if u == 0:
                if t > 0:
                    alpha[b, t, 0] = (
                        alpha[b, t - 1, 0] + log_probs[b, t - 1, 0, blank]
                    )
                cuda.atomic.add(lock, (b, u + 1), -1)
                t += 1
            else:
                if cuda.atomic.add(lock, (b, u), 0) < 0:
                    if t == 0:
                        alpha[b, 0, u] = (
                            alpha[b, 0, u - 1]
                            + log_probs[b, 0, u - 1, labels[b, u - 1]]
                        )
                    else:
                        # compute emission prob
                        emit[b,t,u] = (
                            alpha[b, t, u - 1]
                            + log_probs[b, t, u - 1, labels[b, u - 1]]
                        )
                        # compute no_emission prob
                        no_emit[b,t,u] = (
                            alpha[b, t - 1, u] + log_probs[b, t - 1, u, blank]
                        )
                        # do logsumexp between log_emit and log_no_emit
                        alpha[b, t, u] = max(no_emit[b,t,u], emit[b,t,u]) + math.log1p(
                            math.exp(-abs(no_emit[b,t,u] - emit[b,t,u]))
                        )
                    if u < U[b]:
                        cuda.atomic.add(lock, (b, u + 1), -1)
                    cuda.atomic.add(lock, (b, u), 1)
                    t += 1

        # save probability of T column, and normalize by T
        log_p[b,u] = (
            alpha[b, T[b] - 1, u] + log_probs[b, T[b] - 1, u, blank]
        ) / T[b]

@cuda.jit()
def cu_kernel_forward_only_grad(d_log_p, d_log_probs, labels, d_alpha, emit, no_emit, T, U, blank, lock):
    """
    Compute forward pass for the forward-backward algorithm using Numba cuda kernel.
    Sequence Transduction with naive implementation : https://arxiv.org/pdf/1211.3711.pdf

    Arguments
    ---------
    log_probs : torch.Tensor
        4D Tensor of (batch x TimeLength x LabelLength x outputDim) from the Transducer network.
    labels : torch.Tensor
        2D Tensor of (batch x MaxSeqLabelLength) containing targets of the batch with zero padding.
    alpha : torch.Tensor
        3D Tensor of (batch x TimeLength x LabelLength) for forward computation.
    log_p : torch.Tensor
        1D Tensor of (batch) for forward cost computation.
    T : torch.Tensor
        1D Tensor of (batch) containing TimeLength of each target.
    U : torch.Tensor
        1D Tensor of (batch) containing LabelLength of each target.
    blank : int
        Blank index.
    lock : torch.Tensor
        2D Tensor of (batch x LabelLength) containing bool(1-0) lock for parallel computation.
    """

    # parallelize the forward algorithm over batch and target length dim
    b = cuda.blockIdx.x
    u = cuda.threadIdx.x
    t = T[b] - 1

    if u <= U[b]:
        # for each (B,U) Thread
        # wait the unlock of the previous computation of Alpha[b,U+1,:]
        # Do the computation over the whole Time sequence on alpha[B,U,:]
        # and then unlock the target U-1 for computation
        if u == U[b] and cuda.atomic.add(lock, (b,u),0) == 0:
            # unlock top row
            cuda.atomic.add(lock, (b, u), -1)

        while t > 0:

            if u == 0 and cuda.atomic.add(lock, (b, 0), 0) < 0:
                if t > 0:
                    cuda.atomic.add(d_alpha, (b,t-1,0), d_alpha[b,t,0])
                    cuda.atomic.add(d_log_probs, (b,t-1,0,blank), d_alpha[b,t,0])
                t -= 1
                cuda.atomic.add(lock, (b, U[b]), -1)
                cuda.atomic.add(lock, (b, u), 1)

            else:
                if cuda.atomic.add(lock, (b, u), 0) < 0:

                    if t == T[b] - 1:
                        cuda.atomic.add(d_alpha, (b,t-1,u), d_log_p[b,u] / T[b])
                        cuda.atomic.add(d_log_probs, (b,t-1,u,blank), d_log_p[b,u] / T[b])

                    max_val = max(emit[b,t,u], no_emit[b,t,u])
                    exp_emit = math.exp(emit[b,t,u] - max_val)
                    exp_no_emit = math.exp(no_emit[b,t,u] - max_val)
                    norm =  exp_emit + exp_no_emit
                    d_emit = d_alpha[b,t,u] * (exp_emit / norm)
                    d_no_emit = d_alpha[b,t,u] * (exp_no_emit / norm)

                    cuda.atomic.add(d_alpha, (b,t,u-1), d_emit)
                    cuda.atomic.add(d_alpha, (b,t-1,u), d_no_emit)

                    # print("norm",norm)
                    # print("no_emit", no_emit[b,t,u])
                    # print("emit", emit[b,t,u])
                    # print("d_no_emit", d_no_emit)
                    # print("d_emit", d_emit)
                    # print("before no_emit",d_log_probs[b,t-1,u,blank])
                    cuda.atomic.add(d_log_probs, (b,t,u-1, labels[b,u-1]), d_emit)
                    cuda.atomic.add(d_log_probs, (b,t-1,u,blank), d_no_emit)
                    # print("after no_emit",d_log_probs[b,t-1,u,blank])
                    # print(d_log_probs[b,t-1,u,blank])

                    t -= 1
                    # unlock u - 1 and lock u
                    # if u < U[b]:
                    cuda.atomic.add(lock, (b, u - 1), -1)
                    cuda.atomic.add(lock, (b, u), 1)



class ComputeMarginalProb(Function):

    @staticmethod
    def forward(ctx, log_probs, labels, log_r, T, U, blank, reduction):
        log_probs = log_probs.detach()
        B, maxT, maxU, A = log_probs.shape

        alpha = torch.zeros(
            (B, maxT, maxU), device=log_probs.device, dtype=log_probs.dtype
        )
        emit = torch.zeros(
            (B, maxT, maxU), device=log_probs.device, dtype=log_probs.dtype
        )
        no_emit = torch.zeros(
            (B, maxT, maxU), device=log_probs.device, dtype=log_probs.dtype
        )
        lock = torch.zeros(
            (B, maxU), dtype=torch.int32, device=log_probs.device
        )
        log_p_alpha = torch.zeros(
            (B,maxU), device=log_probs.device, dtype=log_probs.dtype
        )

        cu_kernel_forward_only[B, maxU](
            log_probs, labels, alpha, log_p_alpha, log_r, emit, no_emit, T, U, blank, lock
        )

        cuda.synchronize()
        # prepare ctx for backward pass
        d_alpha = torch.zeros(
            (B, maxT, maxU), device=log_probs.device, dtype=log_probs.dtype
        )
        lock = torch.zeros(
            (B, maxU), dtype=torch.int32, device=log_probs.device
        )
        d_log_p_alpha = torch.zeros(
            (B,maxU), device=log_probs.device, dtype=log_probs.dtype
        )
        d_log_probs = torch.zeros_like(log_probs)
        ctx.save_for_backward(d_log_probs, labels, d_alpha, d_log_p_alpha, emit, no_emit, T, U, lock)

        ctx.shape = log_probs.shape
        ctx.blank = blank

        return log_p_alpha

    @staticmethod
    def backward(ctx, d_log_p):
        B, maxT, maxU, A = ctx.shape
        blank = ctx.blank
        d_log_probs, labels, d_alpha, d_log_p_alpha, emit, no_emit, T, U, lock = ctx.saved_tensors

        cu_kernel_forward_only_grad[B, maxU](
            d_log_p, d_log_probs, labels, d_alpha, emit, no_emit, T, U, blank, lock
        )
        cuda.synchronize()
        # return gradient w.r.t. log_probs and not to others
        return d_log_probs, None, None, None, None, None, None, None, None, None, None
def gfn_loss(
    logits,
    targets,
    log_r,
    input_lens,
    target_lens,
    blank_index,
    reduction="mean",
    use_torchaudio=True,
):
    """Transducer loss, see `speechbrain/nnet/loss/transducer_loss.py`.

    Arguments
    ---------
    logits : torch.Tensor
        Predicted tensor, of shape [batch, maxT, maxU, num_labels].
    targets : torch.Tensor
        Target tensor, without any blanks, of shape [batch, target_len].
    input_lens : torch.Tensor
        Length of each utterance.
    target_lens : torch.Tensor
        Length of each target sequence.
    blank_index : int
        The location of the blank symbol among the label indices.
    reduction : str
        Specifies the reduction to apply to the output: 'mean' | 'batchmean' | 'sum'.
    use_torchaudio: bool
        If True, use Transducer loss implementation from torchaudio, otherwise,
        use Speechbrain Numba implementation.

    Returns
    -------
    The computed transducer loss.
    """

    input_lens = (input_lens * logits.shape[1]).round().int()
    target_lens = (target_lens * targets.shape[1]).round().int()

    # Transducer.apply function take log_probs tensor.
    log_probs = logits.log_softmax(-1)
    log_p = ComputeMarginalProb.apply(
        log_probs, targets, log_r, input_lens, target_lens, blank_index, reduction
    )
    B = log_p.shape[0]

    m = 0


    loss_batch = torch.zeros((B,))
    for b in range(B):
        # full trajectory
        n = log_r.shape[-1] - 1

        # R_m + P_n
        sub_tb_loss = (log_r[b, m] - log_r[b, n]) + 2 * (log_p[b,n] - log_p[b,m])

        loss_batch[b] = loss_batch[b] + (sub_tb_loss ** 2) # add squared loss

    return loss_batch.mean()
