import numpy as np
import torch
from mpi4py import MPI

from distdl.backends.mpi.exchange_tensor import exchange_tensor_structure
from distdl.utilities.torch import NoneTensor


class BroadcastFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, input, P_send, P_recv, dtype):

        ctx.P_send = P_send
        ctx.P_recv = P_recv
        ctx.dtype = dtype

        # Share the input tensor structure so the output can create space for
        # the data.
        tensor_structure = exchange_tensor_structure(input, P_send, P_recv)
        input_requires_grad = tensor_structure[0]
        tensor_dim = tensor_structure[1]
        tensor_sizes = tensor_structure[2]

        ctx.input_requires_grad = input_requires_grad
        ctx.tensor_dim = tensor_dim
        ctx.tensor_sizes = tensor_sizes

        # This allows all ranks to use the same exit path, so that we can be
        # sure that all requests have cleared.
        output = NoneTensor()

        # return output
        requests = []

        # Send all of the data
        if P_send.active:
            input_numpy = input.detach().numpy()
            req = P_send.comm.Ibcast(input_numpy, root=0)
            requests.append(req)

        if P_recv.active:
            # If I also send, make a copy.
            if P_send == P_recv:
                output = input.clone()
            # If I just receive, receive the broadcast
            else:
                output = np.zeros(tensor_sizes, dtype=dtype)

                req = P_recv.comm.Ibcast(output, root=0)
                req.Wait()
                output = torch.tensor(output, requires_grad=input_requires_grad)

        MPI.Request.Waitall(requests)

        return output

    @staticmethod
    def backward(ctx, grad_output):

        P_send = ctx.P_send
        P_recv = ctx.P_recv
        dtype = ctx.dtype
        input_requires_grad = ctx.input_requires_grad
        tensor_sizes = ctx.tensor_sizes

        # This allows all ranks to use the same exit path, so that we can be
        # sure that all requests have cleared.
        grad_input = NoneTensor()

        requests = []

        # If I received data (either from a remote worker or just from myself)
        # I need to reduce that data.  If I send and receive to myself, this
        # is OK, as the reduction accounts for the copy, unlike the broadcast
        # above.
        if P_recv.active:
            reduced_data = np.zeros(tensor_sizes, dtype=dtype)
            grad_output_numpy = grad_output.detach().numpy()
            req = P_recv.comm.Ireduce(grad_output_numpy, reduced_data, root=0, op=MPI.SUM)
            requests.append(req)

        # If I sent data in the forward, I have to receive it here.  Unless I
        # also received that data, then I already have it from abive.  mpi4py
        # does not allow aliasing of the input, so we have to make a copy of
        # nothing, unfortunately.
        if P_send != P_recv and P_send.active:
            reduced_data = np.zeros(tensor_sizes, dtype=dtype)
            req = P_send.comm.Ireduce(reduced_data.copy(), reduced_data, root=0, op=MPI.SUM)
            requests.append(req)

        MPI.Request.Waitall(requests)

        # If we had to receive data, we need to tensorify it.
        if P_send.active:
            grad_input = torch.tensor(reduced_data, requires_grad=input_requires_grad)

        return grad_input, None, None, None


class Broadcast(torch.nn.Module):

    def __init__(self, P_in, P_out):
        super(Broadcast, self).__init__()

        self.P_in = P_in
        self.P_out = P_out

        # TODO: #25  Make selection of dtype more sensible.
        self.dtype = np.float32

        if P_in == P_out:
            self.identity = True
        else:
            self.identity = False
            bcast_partitions = P_in.create_broadcast_partition_to(P_out)
            self.P_send = bcast_partitions[0]
            self.P_recv = bcast_partitions[1]

    def forward(self, input):

        if self.identity:
            return input.clone()

        return BroadcastFunction.apply(input,
                                       self.P_send,
                                       self.P_recv,
                                       self.dtype)
