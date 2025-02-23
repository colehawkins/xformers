# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

import math

import pytest
import torch

from xformers.components import MultiHeadDispatch
from xformers.components.attention import build_attention
from xformers.components.attention.attention_patterns import block_sparsify_tensor
from xformers.triton.utils import get_current_cuda_device

# CREDITS:
# Tests from, very lightly changed
# https://github.com/openai/triton/blob/master/python/test/unit/operators/test_blocksparse.py
# Initially copied here folowing a fork from the matmul kernel


_triton_available = torch.cuda.is_available()
_matmul_types = []

if _triton_available:
    try:
        import triton
        from triton.ops.blocksparse import matmul as blocksparse_matmul
        from triton.ops.blocksparse import softmax as blocksparse_softmax

        from xformers.components.attention import BlockSparseAttention
        from xformers.triton.utils import (
            assert_almost_equal,
            gpu_capabilities_older_than_70,
        )

        _triton_available = not gpu_capabilities_older_than_70()
        _matmul_types = ["sdd", "dsd", "dds"]
    except (ImportError, ModuleNotFoundError) as e:
        import logging

        logging.warning(f"Triton is not available: {e}. Some tests will be skipped")
        _triton_available = False


@pytest.mark.skipif(not _triton_available, reason="Triton requires a recent CUDA gpu")
@pytest.mark.skipif(
    not _triton_available or get_current_cuda_device() == "T4",
    reason="FIXME - blocksparse matmuls are slightly off on T4s",
)
@pytest.mark.parametrize("MODE", _matmul_types)
@pytest.mark.parametrize("TRANS_A", [False, True])
@pytest.mark.parametrize("TRANS_B", [False, True])
@pytest.mark.parametrize("BLOCK", [16, 32, 64])
@pytest.mark.parametrize("DTYPE", [torch.float16])
def test_matmul(MODE, TRANS_A, TRANS_B, BLOCK, DTYPE, Z=32, H=2, M=512, N=384, K=256):
    # set seed
    torch.random.manual_seed(0)

    # create inputs
    a = torch.randn(
        (Z, H, K, M) if TRANS_A else (Z, H, M, K), dtype=DTYPE, device="cuda"
    )
    b = torch.randn(
        (Z, H, N, K) if TRANS_B else (Z, H, K, N), dtype=DTYPE, device="cuda"
    )
    shape = {
        "sdd": (M, N),
        "dsd": (a.shape[2], a.shape[3]),
        "dds": (b.shape[2], b.shape[3]),
    }[MODE]
    layout = torch.randint(2, (H, shape[0] // BLOCK, shape[1] // BLOCK))

    # triton result
    op = blocksparse_matmul(
        layout,
        BLOCK,
        MODE,
        trans_a=TRANS_A,
        trans_b=TRANS_B,
        device=torch.device("cuda"),
    )
    ra = block_sparsify_tensor(a, layout, BLOCK) if MODE == "dsd" else a
    rb = block_sparsify_tensor(b, layout, BLOCK) if MODE == "dds" else b
    rc = triton.testing.catch_oor(lambda: op(ra, rb), pytest)

    # torch result
    ta = triton.testing.mask_tensor(a, layout, BLOCK) if MODE == "dsd" else a
    tb = triton.testing.mask_tensor(b, layout, BLOCK) if MODE == "dds" else b
    ta = ta.transpose(2, 3) if TRANS_A else ta
    tb = tb.transpose(2, 3) if TRANS_B else tb
    tc = torch.matmul(ta, tb)
    tc = triton.testing.mask_tensor(tc, layout, BLOCK) if MODE == "sdd" else tc
    tc = block_sparsify_tensor(tc, layout, BLOCK) if MODE == "sdd" else tc

    # compare
    assert_almost_equal(rc, tc)


@pytest.mark.skipif(not _triton_available, reason="Triton requires a recent CUDA gpu")
@pytest.mark.parametrize("BLOCK", [32, 128])
@pytest.mark.parametrize("WIDTH", [256, 576, 1024, 1792])
@pytest.mark.parametrize("DTYPE", [torch.float16, torch.float32])
def test_softmax(BLOCK, WIDTH, DTYPE):
    # set seed
    torch.random.manual_seed(0)
    Z, H, M, N = 2, 4, WIDTH, WIDTH
    scale = 0.4

    # create inputs
    layout = torch.randint(2, (H, M // BLOCK, N // BLOCK))
    x = torch.randn((Z, H, M, N), dtype=DTYPE, requires_grad=True, device="cuda")

    # triton result
    op = blocksparse_softmax(layout, BLOCK, device=torch.device("cuda"))
    tx = block_sparsify_tensor(x, layout, BLOCK)
    ty = op(tx, scale=scale)

    # torch result
    rx = triton.testing.mask_tensor(x, layout, BLOCK, value=float("-inf"))

    ry = torch.softmax(rx * scale, -1)
    ry = block_sparsify_tensor(ry, layout, BLOCK)

    # compare
    assert_almost_equal(ry, ty)


@pytest.mark.skipif(not _triton_available, reason="Triton requires a recent CUDA gpu")
@pytest.mark.parametrize("block", [32, 43, 128])  # 16, 32,
def test_attention_fwd_bwd(
    block,
    input_scale=1.0,
    scale=1 / 8.0,
    n_ctx=384,
    dtype=torch.float16,
    batch_size=2,
    n_heads=2,
):
    # inputs
    head_dim = 64
    qkv_shape = (batch_size, n_heads, n_ctx, head_dim)
    qkvs = [
        torch.nn.Parameter(input_scale * torch.randn(qkv_shape), requires_grad=True)
        .to(dtype)
        .cuda()
        for _ in range(3)
    ]

    torch.manual_seed(0)
    att_mask = torch.randint(0, 2, [batch_size, n_heads, 1, n_ctx]).bool().cuda()

    def loss_fn(x):
        return (x**2).mean()

    # Triton:
    n_blocks = n_ctx // block
    layout = torch.tril(
        torch.ones([n_heads, n_blocks, n_blocks], dtype=torch.long), diagonal=-1
    )
    query, key, value = [x.clone() for x in qkvs]
    query.retain_grad()
    key.retain_grad()
    value.retain_grad()
    if block not in [16, 32, 64, 128]:
        # Check that unsupported dimensions are caught
        with pytest.raises(AssertionError):
            _ = BlockSparseAttention(layout, block)
    else:
        block_sparse_attention = BlockSparseAttention(layout, block)
        attn_out = block_sparse_attention(
            q=query, k=key, v=value, scale=scale, att_mask=att_mask
        )

        # ad hoc loss
        loss = loss_fn(attn_out)
        loss.backward()
        grads = [query.grad, key.grad, value.grad]

        # Torch version:
        torch_q, torch_k, torch_v = [x.clone() for x in qkvs]
        torch_q = torch_q / math.sqrt(head_dim)
        torch_q.retain_grad()
        torch_k.retain_grad()
        torch_v.retain_grad()
        float_att_mask = 1e6 * (-1.0 + att_mask.half())
        scores = (
            scale * torch.einsum("bhsd,bhtd->bhst", torch_q, torch_k) + float_att_mask
        )
        probs = torch.softmax(scores, dim=-1)
        torch_attn_out = torch.einsum("bhst,bhtd->bhsd", probs, torch_v)

        # ad hoc loss
        torch_loss = loss_fn(torch_attn_out)
        torch_loss.backward()
        torch_grads = [torch_q.grad, torch_k.grad, torch_v.grad]

        # comparison
        assert_almost_equal(
            loss, torch_loss, err_msg=f"Triton loss {loss} and torch loss {torch_loss}"
        )

        for g1, g2 in zip(grads, torch_grads):
            assert_almost_equal(
                torch.norm(g1),
                torch.norm(g2),
                err_msg=f"Triton grad {torch.norm(g1).item()} and torch grad {torch.norm(g2).item()}",
            )


@pytest.mark.skipif(not _triton_available, reason="Triton requires a recent CUDA gpu")
def test_blocksparse_attention_parity():
    def _reset_seeds():
        torch.manual_seed(0)

    seq = 64
    model = 128
    heads = 4
    block_size = 16
    batch_size = 2
    batched_dim = heads * batch_size
    dim_head = model // heads

    test_config = {
        "dropout": 0.0,
        "causal": False,
        "seq_len": seq,
        "num_heads": 4,
        "dim_head": dim_head,
        "block_size": block_size,
        "layout": torch.ones(seq // block_size, seq // block_size, dtype=torch.long),
    }

    inputs = torch.rand(batched_dim, seq, model, device="cuda").half()

    _reset_seeds()
    test_config["name"] = "scaled_dot_product"
    attention_sdp = build_attention(test_config)
    multi_head_sdp = (
        MultiHeadDispatch(
            seq_len=seq,
            dim_model=model,
            residual_dropout=0.0,
            num_heads=heads,
            attention=attention_sdp,
        )
        .cuda()
        .half()
    )
    r_sdp = multi_head_sdp(inputs, inputs, inputs)

    _reset_seeds()
    test_config["name"] = "blocksparse"
    attention_blocksparse = build_attention(test_config)
    multi_head_blocksparse = (
        MultiHeadDispatch(
            seq_len=seq,
            dim_model=model,
            residual_dropout=0.0,
            num_heads=heads,
            attention=attention_blocksparse,
        )
        .cuda()
        .half()
    )
    r_blocksparse = multi_head_blocksparse(inputs, inputs, inputs)

    # FIXME: currently has max diff of .009, perhaps can be improved.
    assert_almost_equal(r_sdp, r_blocksparse)
