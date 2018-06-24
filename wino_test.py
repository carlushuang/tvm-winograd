import os
import numpy as np
import tvm
import topi
import topi.testing
from tvm.contrib.pickle_memoize import memoize
from topi import util
from topi.nn import pad

def reference_direct(batch, in_channel, in_size, num_filter, kernel, stride, padding, device):
    in_height = in_width = in_size

    A = tvm.placeholder((batch, in_channel, in_height, in_width), name='A')
    W = tvm.placeholder((num_filter, in_channel, kernel, kernel), name='W')

    a_shape = util.get_const_tuple(A.shape)
    w_shape = util.get_const_tuple(W.shape)
    dtype = A.dtype
    dilation = 1

    @memoize("topi.tests.test_topi_conv2d_nchw.reference_direct")
    def get_ref_data():
        a_np = np.random.uniform(size=a_shape).astype(dtype)
        w_np = np.random.uniform(size=w_shape).astype(dtype)
        dw_np = topi.testing.dilate_python(w_np, (1, 1, dilation, dilation))
        b_np = topi.testing.conv2d_nchw_python(a_np, dw_np, stride, padding)
        c_np = np.maximum(b_np, 0)
        return a_np, w_np, b_np, c_np

    a_np, w_np, b_np, c_np = get_ref_data()

    ctx = tvm.context(device, 0)
    if not ctx.exist:
        print("Skip because %s is not enabled" % device)
        return
    with tvm.target.create(device):
        dW = topi.nn.dilate(W, (1, 1, dilation, dilation))
        B = topi.nn.conv2d(A, dW, stride, padding, layout='NCHW')
        s1 = topi.generic.schedule_conv2d_nchw([B])
    a = tvm.nd.array(a_np, ctx)
    w = tvm.nd.array(w_np, ctx)
    b = tvm.nd.array(np.zeros(util.get_const_tuple(B.shape), dtype=B.dtype), ctx)
    with tvm.build_config(auto_unroll_max_step=1400,
                          unroll_explicit=(device != "cuda")):
        func = tvm.build(s1, [A, W, B], device, name="conv2d_%d_%d_%d_%d_%d_%d_%d_%d" % (batch, in_channel, in_size, num_filter, kernel, stride, padding, dilation))
        #print(tvm.lower(s1, [A, W, B], simple_mode=True))
        func(a, w, b)
        np.testing.assert_allclose(b.asnumpy(), b_np, rtol=1e-5)
        num_runs = 100
        timer = func.time_evaluator(func.entry_name, ctx, number=num_runs)
        return timer(a, w, b).mean

def const_array(data, name):
    """ convert an const array to tvm tensor"""
    row, col = data.shape
    dtype = str(data.dtype)

    def select_array(i, j):
        now = tvm.const(0.0, dtype)
        for ii in range(row):
            for jj in range(col):
                now = tvm.select(tvm.all(i % row == ii, j % col == jj),
                                 tvm.const(data[ii][jj], dtype),
                                 now)
        return now
    return tvm.compute(data.shape, select_array, name=name)

def decl_winograd(data, U, stride, padding, out_dtype):
    """declare winograd fast convolution F(2x2, 3x3) for conv2d"""
    N, CI, H, W = [util.get_const_int(x) for x in data.shape]
    _, _, CO, CI = [util.get_const_int(x) for x in U.shape]
    HPAD, WPAD = 1,1
    if isinstance(stride, (tuple, list)):
        HSTR, WSTR = stride
    else:
        HSTR, WSTR = stride, stride

    assert HSTR == 1 and WSTR == 1 and HPAD == 1 and WPAD == 1
    data_pad = pad(data, (0, 0, HPAD, WPAD), name="data_pad")

    B_data = np.array([
        [1, 0, 0, 0],
        [0, 1, -1, 1],
        [-1, 1, 1, 0],
        [0, 0, 0, -1]
    ], out_dtype)

    A_data = np.array([
        [1, 0],
        [1, 1],
        [1, -1],
        [0, -1],
    ], out_dtype)

    m = 2
    r = 3
    alpha = m + r - 1
    K = CO
    C = CI

    nH, nW = (H + m-1) // m, (W + m-1) // m
    P = N * nH * nW

    # pack input tile
    input_tile = tvm.compute((C, P, alpha, alpha),
                             lambda c, b, eps, nu:
                             tvm.select(b < P, data_pad[b // (nH*nW)][c][b// nW % nH * m + eps][b % nW * m + nu], tvm.const(0, data_pad.dtype)), name='d')

    # transform image
    B = const_array(B_data, 'B')
    r_eps = tvm.reduce_axis((0, alpha), 'r_eps')
    r_nu = tvm.reduce_axis((0, alpha), 'r_nu')
    V = tvm.compute((alpha, alpha, C, P), lambda eps, nu, c, b:
                    tvm.sum(input_tile[c][b][r_eps][r_nu] * B[r_eps][eps] * B[r_nu][nu],
                            axis=[r_eps, r_nu]), name='V')

    # batch gemm
    c = tvm.reduce_axis((0, C), name='c')
    M = tvm.compute((alpha, alpha, K, P), lambda eps, nu, k, b:
                    tvm.sum(U[eps][nu][k][c] *
                            V[eps][nu][c][b], axis=c), name='M')

    # inverse transform and unpack
    A = const_array(A_data, 'A')
    r_eps = tvm.reduce_axis((0, alpha), 'r_eps')
    r_nu = tvm.reduce_axis((0, alpha), 'r_nu')
    Y = tvm.compute((K, P, m, m), lambda k, b, vh, vw:
                    tvm.sum(M[r_eps][r_nu][k][b] * A[r_eps][vh] * A[r_nu][vw],
                            axis=[r_eps, r_nu]), name='Y')

    # unpack output
    output = tvm.compute((N, K, H, W), lambda n, k, h, w:
                         Y[k][n * nH * nW + (h//m) * nW + w//m][h % m][w % m],
                         name='output', tag='winograd_conv_output')

    # output = tvm.compute((N, K, H, W), lambda n, k, h, w:
    #                 tvm.sum(M[r_eps][r_nu][k][n * nH * nW + (h//m) * nW + w//m] * A[r_eps][h % m] * A[r_nu][w % m],
    #                         axis=[r_eps, r_nu]), name='output')

    return output

def schedule_winograd(outs):
    s = tvm.create_schedule([x.op for x in outs])
    op = outs[0].op
    output = op.output(0)
    Y = op.input_tensors[0]

    M, A = s[Y].op.input_tensors
    U, V = s[M].op.input_tensors
    d, B = s[V].op.input_tensors
    data_pad = s[d].op.input_tensors[0]
    data = s[data_pad].op.input_tensors[0]

    s[data_pad].compute_inline()

    # transform image
    s[B].compute_inline()
    VL = s.cache_write(V, "local")

    eps, nu, C, P = s[V].op.axis
    r_eps, r_nu = s[VL].op.reduce_axis
    s[V].reorder(C, P, eps, nu)

    ho, hi = s[V].split(C, factor=16)
    wo, wi = s[V].split(P, factor=16)
    s[V].bind(hi, tvm.thread_axis("threadIdx.y"))
    s[V].bind(wi, tvm.thread_axis("threadIdx.x"))
    s[V].bind(ho, tvm.thread_axis("blockIdx.y"))
    s[V].bind(wo, tvm.thread_axis("blockIdx.x"))

    s[VL].compute_at(s[V], wi)
    s[d].compute_at(s[V], wi)

    UU = s.cache_read(U, 'shared', [M])
    VV = s.cache_read(V, "shared", [M])
    # UL = s.cache_read(UU, "local", [M])
    # VL = s.cache_read(VV, "local", [M])
    ML = s.cache_write(M, "local")

    eps, nu, k, b = s[M].op.axis
    ko, ki = s[M].split(k, factor=16)
    bo, bi = s[M].split(b, factor=16)

    z = s[M].fuse(eps, nu)

    s[M].bind(z, tvm.thread_axis("blockIdx.z"))
    s[M].bind(ko, tvm.thread_axis("blockIdx.y"))
    s[M].bind(ki, tvm.thread_axis("threadIdx.y"))
    s[M].bind(bo, tvm.thread_axis("blockIdx.x"))
    s[M].bind(bi, tvm.thread_axis("threadIdx.x"))
    s[ML].compute_at(s[M], bi)

    k = s[ML].op.reduce_axis[0]
    ko, ki = s[ML].split(k, factor=16)
    s[UU].compute_at(s[ML], ko)
    s[VV].compute_at(s[ML], ko)

    num_thread = 16
    yi, xi, ci, ni = s[UU].op.axis
    ty, ci = s[UU].split(ci, nparts=num_thread)
    tx, ni = s[UU].split(ni, nparts=num_thread)
    s[UU].bind(ty, tvm.thread_axis("threadIdx.y"))
    s[UU].bind(tx, tvm.thread_axis("threadIdx.x"))

    yi, xi, ci, ni = s[VV].op.axis
    ty, ci = s[VV].split(ci, nparts=num_thread)
    tx, ni = s[VV].split(ni, nparts=num_thread)
    s[VV].bind(ty, tvm.thread_axis("threadIdx.y"))
    s[VV].bind(tx, tvm.thread_axis("threadIdx.x"))

    # inverse transform
    s[A].compute_inline()
    k, b, vh, vw = s[Y].op.axis
    MM = s.cache_read(M, "local", [Y])
    YL = s.cache_write(Y, "local")
    r_eps, r_nu = s[YL].op.reduce_axis
    ko, ki = s[Y].split(k, factor=16)
    bo, bi = s[Y].split(b, factor=16)
    s[Y].bind(ki, tvm.thread_axis("threadIdx.y"))
    s[Y].bind(bi, tvm.thread_axis("threadIdx.x"))
    s[Y].bind(ko, tvm.thread_axis("blockIdx.y"))
    s[Y].bind(bo, tvm.thread_axis("blockIdx.x"))
    s[YL].compute_at(s[Y], bi)
    s[MM].compute_at(s[Y], bi)

    # schedule output
    if output.op in s.outputs:  # no bias
        output = output
    else:                       # has bias
        s[output].compute_inline()
        output = s.outputs[0]

    _, k, h, w = s[output].op.axis

    ho, hi = s[output].split(h, factor=16)
    wo, wi = s[output].split(w, factor=16)
    s[output].reorder(k, ho, wo, hi, wi)
    s[output].bind(hi, tvm.thread_axis("threadIdx.y"))
    s[output].bind(wi, tvm.thread_axis("threadIdx.x"))
    fused = s[output].fuse(k, ho, wo)
    s[output].bind(fused, tvm.thread_axis("blockIdx.x"))
    #s[YL].compute_at(s[output], wi)
    return s


def transform_filter(w_np):
    num_filter, in_channel, kernel, kernel = w_np.shape
    G = np.array([
        [1, 0, 0],
        [1.0/2, 1.0/2, 1.0/2],
        [1.0/2, -1.0/2, 1.0/2],
        [0, 0, 1],
    ], w_np.dtype)

    out = np.empty((4, 4, num_filter, in_channel), w_np.dtype)
    for i in range(num_filter):
        for j in range(in_channel):
            out[:, :, i, j] = np.dot(G, np.dot(w_np[i, j], G.transpose()))
    return out


def test_winograd(batch, in_channel, in_size, num_filter, kernel, stride, padding, device):
    in_height = in_width = in_size

    A = tvm.placeholder((batch, in_channel, in_height, in_width), name='A')
    W = tvm.placeholder((num_filter, in_channel, kernel, kernel), name='W')
    U = tvm.placeholder((4, 4, num_filter, in_channel), name='W')

    a_shape = util.get_const_tuple(A.shape)
    w_shape = util.get_const_tuple(W.shape)
    dtype = A.dtype
    dilation = 1

    @memoize("topi.tests.test_topi_conv2d_nchw.wino")
    def get_ref_data():
        a_np = np.random.uniform(size=a_shape).astype(dtype)
        w_np = np.random.uniform(size=w_shape).astype(dtype)
        dw_np = topi.testing.dilate_python(w_np, (1, 1, dilation, dilation))
        b_np = topi.testing.conv2d_nchw_python(a_np, dw_np, stride, padding)
        c_np = np.maximum(b_np, 0)
        return a_np, w_np, b_np, c_np

    a_np, w_np, b_np, c_np = get_ref_data()

    with tvm.target.create(device):
        B = decl_winograd(A, U, stride, padding, dtype)
        s = schedule_winograd([B])

    u_np = transform_filter(w_np)

    ctx = tvm.context(device, 0)
    a = tvm.nd.array(a_np, ctx)
    u = tvm.nd.array(u_np, ctx)
    b = tvm.nd.array(np.zeros(util.get_const_tuple(B.shape), dtype=B.dtype), ctx)
    with tvm.build_config(auto_unroll_max_step=1400,
                          unroll_explicit=(device != "cuda")):
        func = tvm.build(s, [A, U, B], device)
        #print(tvm.lower(s, [A, U, B], simple_mode=True))
        func(a, u, b)
        num_runs = 10
        timer = func.time_evaluator(func.entry_name, ctx, number=num_runs)

        np.testing.assert_allclose(b.asnumpy(), b_np, rtol=1e-5)
        #print(func.imported_modules[0].get_source())
        return timer(a, u, b).mean


workloads = [(1, 128, 122, 128, 3, 1, 1),
             (1, 64, 56, 64, 3, 1, 1),
             (1, 64, 64, 32, 3, 1, 1),
             (1, 64, 224, 64, 3, 1, 1),
             (1, 64, 112, 128, 3, 1, 1),
             (1, 512, 28, 512, 3, 1, 1)
            ]

for workload in workloads:
    device = "cuda"
    #device = "rocm"
    t_wino = test_winograd(*workload, device)
    # print(t_wino)
    # break

    # device += " -libs=cudnn"
    # device += " -libs=miopen"
    if workload[1] == 512:
        t_direct = None # tvm cuda conv2d cannot handle this workload
    else:
        t_direct = reference_direct(*workload, device)

    print(t_wino, t_direct)
