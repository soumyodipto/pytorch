# Owner(s): ["oncall: distributed"]

import functools
import sys
from typing import Optional, Tuple, Type

import torch
import torch.nn as nn
from torch import distributed as dist
from torch.distributed.fsdp import (
    BackwardPrefetch,
    CPUOffload,
    FullyShardedDataParallel as FSDP,
    ShardingStrategy,
)
from torch.distributed.fsdp.fully_sharded_data_parallel import clean_tensor_name
from torch.distributed.fsdp.wrap import always_wrap_policy, transformer_auto_wrap_policy
from torch.nn import TransformerDecoderLayer, TransformerEncoderLayer
from torch.nn.parallel.distributed import DistributedDataParallel as DDP
from torch.testing._internal.common_distributed import skip_if_lt_x_gpu
from torch.testing._internal.common_fsdp import (
    CUDAInitMode,
    FSDPInitMode,
    FSDPTest,
    TransformerWithSharedParams,
)
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
    run_tests,
    TEST_WITH_DEV_DBG_ASAN,
)

if not dist.is_available():
    print("Distributed not available, skipping tests", file=sys.stderr)
    sys.exit(0)

if TEST_WITH_DEV_DBG_ASAN:
    print(
        "Skip dev-asan as torch + multiprocessing spawn have known issues",
        file=sys.stderr,
    )
    sys.exit(0)


class TestFSDPUseOrigParamsMultipleParamGroups(FSDPTest):
    """Tests multiple parameter groups."""

    def _get_optim(
        self,
        model: nn.Module,
        optim_class: Type[torch.optim.Optimizer],
        multi_tensor: bool,
    ) -> torch.optim.Optimizer:
        """
        Constructs an Adam optimizer with three parameter groups, one for
        weights, one for biases, and one for everything else, each with
        different weight decay and learning rates.
        """
        param_groups = [
            {"params": [], "weight_decay": 0.1, "lr": 1e-2},
            {"params": [], "weight_decay": 0.01, "lr": 1e-3},
            {"params": []},
        ]
        for param_name, param in model.named_parameters():
            if "weight" in param_name:
                param_groups[0]["params"].append(param)
            elif "bias" in param_name:
                param_groups[1]["params"].append(param)
            else:
                param_groups[2]["params"].append(param)
        return optim_class(param_groups, lr=5e-3, foreach=multi_tensor)

    def _get_ddp_transformer_and_optim(
        self,
        optim_class: Type[torch.optim.Optimizer],
        multi_tensor: bool,
        find_unused_params: bool,
    ) -> Tuple[DDP, torch.optim.Optimizer]:
        """
        Returns a transformer with shared parameters wrapped with DDP and a
        corresponding optimizer.
        """
        model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.NO_FSDP,
            CUDAInitMode.CUDA_BEFORE,
            deterministic=True,
        )
        ddp_model = DDP(
            model,
            device_ids=[self.rank],
            find_unused_parameters=find_unused_params,
        )
        ddp_optim = self._get_optim(ddp_model, optim_class, multi_tensor)
        return ddp_model, ddp_optim

    def _get_fsdp_transformer_and_optim(
        self,
        cuda_init_mode: CUDAInitMode,
        init_optim_before_wrap: bool,
        optim_class: Type[torch.optim.Optimizer],
        multi_tensor: bool,
        sharding_strategy: ShardingStrategy,
        backward_prefetch: Optional[BackwardPrefetch],
        cpu_offload: CPUOffload,
    ) -> Tuple[FSDP, torch.optim.Optimizer]:
        """
        Returns a transformer with shared parameters wrapped with FSDP and a
        corresponding optimizer.
        """
        # Each transformer layer has multiple linear layers, so this policy, in
        # combination with the parameter group construction, ensures different
        # hyperparameter settings within one `FlatParameter`
        fsdp_kwargs = {
            "auto_wrap_policy": functools.partial(
                transformer_auto_wrap_policy,
                transformer_layer_cls={
                    TransformerEncoderLayer,
                    TransformerDecoderLayer,
                },
            ),
            "use_orig_params": True,
            "sharding_strategy": sharding_strategy,
            "backward_prefetch": backward_prefetch,
            "cpu_offload": cpu_offload,
        }
        model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.NO_FSDP,
            cuda_init_mode,
            deterministic=True,
        )
        if init_optim_before_wrap:
            fsdp_optim = self._get_optim(model, optim_class, multi_tensor)
            fsdp_model = FSDP(model, self.process_group, **fsdp_kwargs)
        else:
            fsdp_model = FSDP(model, self.process_group, **fsdp_kwargs)
            fsdp_optim = self._get_optim(fsdp_model, optim_class, multi_tensor)
        if (
            cuda_init_mode == CUDAInitMode.CUDA_AFTER
            and not fsdp_model.cpu_offload.offload_params
        ):
            fsdp_model = fsdp_model.cuda()
        return fsdp_model, fsdp_optim

    def _check_train_parity(
        self,
        ddp_model: DDP,
        ddp_optim: torch.optim.Optimizer,
        fsdp_model: FSDP,
        fsdp_optim: torch.optim.Optimizer,
        set_to_none: bool,
        num_iters: int = 10,
    ):
        """Checks training parity between DDP and FSDP."""
        device = torch.device("cuda")
        for i in range(num_iters):
            iter_losses = []
            for model, optim in ((ddp_model, ddp_optim), (fsdp_model, fsdp_optim)):
                module = model.module
                # Test two different `zero_grad()` timings
                if i % 2 == 0:
                    optim.zero_grad(set_to_none=set_to_none)  # pre-forward
                inp = module.get_input(device)
                output = model(*inp)
                loss = module.get_loss(inp, output).to(device)
                iter_losses.append(loss)
                if i % 2 == 1:
                    optim.zero_grad(set_to_none=set_to_none)  # pre-backward
                module.run_backward(loss)
                # Perform the DDP optimizer step on CPU to match FSDP if needed
                if model is ddp_model and fsdp_model.cpu_offload.offload_params:
                    model.to(torch.device("cpu"))
                optim.step()
                if model is ddp_model and fsdp_model.cpu_offload.offload_params:
                    model.to(torch.device("cuda"))
            torch.testing.assert_close(iter_losses[0], iter_losses[1])
            iter_losses.clear()
        atol = 1e-5 if self.world_size <= 2 else 1e-4
        rtol = 1e-6 if self.world_size <= 2 else 1e-5
        with FSDP.summon_full_params(fsdp_model):
            for p1, p2 in zip(ddp_model.parameters(), fsdp_model.parameters()):
                torch.testing.assert_close(p1, p2, atol=atol, rtol=rtol)

    def _get_sharding_strategy_from_str(self, sharding_strategy_str: str) -> ShardingStrategy:
        if sharding_strategy_str == "no_shard":
            sharding_strategy = ShardingStrategy.NO_SHARD
        elif sharding_strategy_str == "shard_grad_op":
            sharding_strategy = ShardingStrategy.SHARD_GRAD_OP
        elif sharding_strategy_str == "full_shard":
            sharding_strategy = ShardingStrategy.FULL_SHARD
        else:
            raise ValueError(f"Invalid string: {sharding_strategy_str}")
        return sharding_strategy

    @skip_if_lt_x_gpu(2)
    @parametrize(
        "sharding_strategy_str",
        ["no_shard", "shard_grad_op", "full_shard"],
    )
    def test_diff_hyperparams(self, sharding_strategy_str: str):
        """
        Tests FSDP parity with DDP when using multiple parameter groups with
        different hyperparameter settings.
        """
        sharding_strategy = self._get_sharding_strategy_from_str(sharding_strategy_str)
        self.run_subtests(
            {
                "cuda_init_mode": [
                    CUDAInitMode.CUDA_BEFORE,
                    CUDAInitMode.CUDA_AFTER,
                ],
                "init_optim_before_wrap": [False, True],
                "optim_class": [torch.optim.Adam, torch.optim.AdamW],
                "multi_tensor": [False, True],
                "set_to_none": [False, True],
                "backward_prefetch": [
                    None,
                    BackwardPrefetch.BACKWARD_PRE,
                    BackwardPrefetch.BACKWARD_POST,
                ],
            },
            self._test_diff_hyperparams,
            cpu_offload=CPUOffload(offload_params=False),
            sharding_strategy=sharding_strategy,
        )

    @parametrize(
        "sharding_strategy_str",
        ["no_shard", "shard_grad_op", "full_shard"],
    )
    def _test_diff_hyperparams_cpu_offload(self, sharding_strategy_str: str):
        """
        Tests FSDP parity with DDP when using multiple parameter groups with
        different hyperparameter settings with CPU offloading enabled. This is
        separate from :meth:`test_diff_hyperparams` because CPU offloading has
        some issues with subtesting for some specific subtesting configs (e.g.,
        with ``offload_params=False`` followed by ``True`` but not vice versa).
        """
        sharding_strategy = self._get_sharding_strategy_from_str(sharding_strategy_str)
        self._test_diff_hyperparams(
            cuda_init_mode=CUDAInitMode.CUDA_BEFORE,
            init_optim_before_wrap=False,
            optim_class=torch.optim.Adam,
            multi_tensor=False,
            set_to_none=False,
            backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
            cpu_offload=CPUOffload(offload_params=True),
            sharding_strategy=sharding_strategy,
        )

    def _test_diff_hyperparams(
        self,
        cuda_init_mode: CUDAInitMode,
        init_optim_before_wrap: bool,
        optim_class: Type[torch.optim.Optimizer],
        multi_tensor: bool,
        set_to_none: bool,
        backward_prefetch: Optional[BackwardPrefetch],
        cpu_offload: CPUOffload,
        sharding_strategy: ShardingStrategy,
    ):
        """
        Args:
            init_optim_before_wrap (bool): If ``True``, initializes the
                FSDP optimizer before wrapping the model with FSDP; otherwise,
                initializes the FSDP optimizer after wrapping the model with
                FSDP. We permit both forms of initialization to give users
                flexibility.
        """
        if cuda_init_mode == CUDAInitMode.CUDA_AFTER and cpu_offload.offload_params:
            return  # not supported
        ddp_model, ddp_optim = self._get_ddp_transformer_and_optim(
            optim_class=optim_class,
            multi_tensor=multi_tensor,
            find_unused_params=False,
        )
        fsdp_model, fsdp_optim = self._get_fsdp_transformer_and_optim(
            cuda_init_mode=cuda_init_mode,
            init_optim_before_wrap=init_optim_before_wrap,
            optim_class=optim_class,
            multi_tensor=multi_tensor,
            sharding_strategy=sharding_strategy,
            backward_prefetch=backward_prefetch,
            cpu_offload=cpu_offload,
        )
        self._check_train_parity(ddp_model, ddp_optim, fsdp_model, fsdp_optim, set_to_none)

    @skip_if_lt_x_gpu(2)
    def test_diff_trainability(self):
        """
        Tests FSDP parity with DDP when using multiple parameter groups and
        freezing the parameters in one parameter group.
        """
        self.run_subtests(
            {
                "multi_tensor": [False, True],
                "sharding_strategy": [
                    ShardingStrategy.FULL_SHARD,
                    ShardingStrategy.SHARD_GRAD_OP,
                    ShardingStrategy.NO_SHARD,
                ]
            },
            self._test_diff_trainability,
        )

    def _test_diff_trainability(
        self,
        multi_tensor: bool,
        sharding_strategy: ShardingStrategy,
    ):
        optim_class = torch.optim.Adam
        ddp_model, ddp_optim = self._get_ddp_transformer_and_optim(
            optim_class=optim_class,
            multi_tensor=multi_tensor,
            find_unused_params=True,
        )
        fsdp_model, fsdp_optim = self._get_fsdp_transformer_and_optim(
            cuda_init_mode=CUDAInitMode.CUDA_BEFORE,
            init_optim_before_wrap=False,
            optim_class=optim_class,
            multi_tensor=multi_tensor,
            sharding_strategy=sharding_strategy,
            backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
            cpu_offload=None,
        )
        # Freeze all biases (which happen to be in the same parameter group)
        for param_name, param in ddp_model.named_parameters():
            if "bias" in param_name:
                param.requires_grad_(False)
        for param_name, param in fsdp_model.named_parameters():
            if "bias" in param_name:
                param.requires_grad_(False)
        self._check_train_parity(ddp_model, ddp_optim, fsdp_model, fsdp_optim, False)


class TestFSDPUseOrigParamsUnshardReshard(FSDPTest):
    """Tests the unshard/reshard flow."""

    def _get_fsdp_models_and_optims(
        self,
        sharding_strategy: ShardingStrategy,
        cpu_offload: CPUOffload,
    ) -> Tuple[FSDP, torch.optim.Optimizer, FSDP, torch.optim.Optimizer]:
        """
        Returns a pair of (FSDP model, optimizer) for ``use_orig_params=False``
        and ``True``, respectively.
        """
        LR = 1e-2
        fsdp_kwargs = {
            "sharding_strategy": sharding_strategy,
            "cpu_offload": cpu_offload,
            "use_orig_params": False,
        }
        fsdp_model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_BEFORE,
            fsdp_kwargs=fsdp_kwargs,
            deterministic=True,
        )
        optim = torch.optim.Adam(fsdp_model.parameters(), lr=LR)
        fsdp_kwargs["use_orig_params"] = True
        fsdp_model_orig_params = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.RECURSIVE,
            CUDAInitMode.CUDA_BEFORE,
            fsdp_kwargs=fsdp_kwargs,
            deterministic=True,
        )
        optim_orig_params = torch.optim.Adam(fsdp_model_orig_params.parameters(), lr=LR)
        return fsdp_model, optim, fsdp_model_orig_params, optim_orig_params

    def _check_fsdp_parameter_parity(self, fsdp1: FSDP, fsdp2: FSDP) -> None:
        """Checks that two FSDP instances have the same model parameters."""
        with FSDP.summon_full_params(fsdp1):
            with FSDP.summon_full_params(fsdp2):
                for (n1, p1), (n2, p2) in zip(
                    fsdp1.named_parameters(),
                    fsdp2.named_parameters(),
                ):
                    self.assertEqual(n1, n2)
                    torch.testing.assert_close(p1, p2)

    def _get_fsdp_parity_subtest_config(self):
        return {
            "sharding_strategy": [
                ShardingStrategy.NO_SHARD,
                ShardingStrategy.SHARD_GRAD_OP,
                ShardingStrategy.FULL_SHARD,
            ],
        }

    @skip_if_lt_x_gpu(2)
    @parametrize("offload_params", [False, True])
    def test_multiple_forward(self, offload_params: bool):
        """
        Tests that ``use_orig_params=True`` has parity with ``False`` when
        running multiple forward passes before a backward pass.
        """
        cpu_offload = CPUOffload(offload_params=offload_params)
        self.run_subtests(
            self._get_fsdp_parity_subtest_config(),
            self._test_multiple_forward,
            cpu_offload=cpu_offload,
        )

    @skip_if_lt_x_gpu(2)
    def _test_multiple_forward(
        self,
        sharding_strategy: ShardingStrategy,
        cpu_offload: CPUOffload,
    ):
        (
            fsdp_model,
            optim,
            fsdp_model_orig_params,
            optim_orig_params,
        ) = self._get_fsdp_models_and_optims(sharding_strategy, cpu_offload)
        device = torch.device("cuda")
        for i in range(3):
            optim.zero_grad()
            optim_orig_params.zero_grad()

            inp1 = fsdp_model.get_input(device)
            loss1 = fsdp_model(*inp1)
            loss_orig_params1 = fsdp_model_orig_params(*inp1)
            self.assertEqual(loss1, loss_orig_params1)

            inp2 = fsdp_model.get_input(device)
            loss2 = fsdp_model(*inp2)
            loss_orig_params2 = fsdp_model_orig_params(*inp2)
            self.assertEqual(loss2, loss_orig_params2)

            loss = (loss1 + loss2).sum()
            loss_orig_params = (loss_orig_params1 + loss_orig_params2).sum()
            fsdp_model.run_backward(loss)
            fsdp_model_orig_params.run_backward(loss_orig_params)
            optim.step()
            optim_orig_params.step()
        self._check_fsdp_parameter_parity(fsdp_model, fsdp_model_orig_params)

    @skip_if_lt_x_gpu(2)
    @parametrize("offload_params", [False, True])
    def test_forward_summon_forward(self, offload_params: bool):
        """
        Tests that ``use_orig_params=True`` has parity with ``False`` when
        running a forward pass, :meth:`summon_full_params()`, and another
        forward pass before a backward pass.
        """
        cpu_offload = CPUOffload(offload_params=offload_params)
        self.run_subtests(
            self._get_fsdp_parity_subtest_config(),
            self._test_forward_summon_forward,
            cpu_offload=cpu_offload,
        )

    def _test_forward_summon_forward(
        self,
        sharding_strategy: ShardingStrategy,
        cpu_offload: CPUOffload,
    ):
        (
            fsdp_model,
            optim,
            fsdp_model_orig_params,
            optim_orig_params,
        ) = self._get_fsdp_models_and_optims(sharding_strategy, cpu_offload)
        device = torch.device("cuda")
        for i in range(3):
            optim.zero_grad()
            optim_orig_params.zero_grad()

            inp1 = fsdp_model.get_input(device)
            loss1 = fsdp_model(*inp1)
            loss_orig_params1 = fsdp_model_orig_params(*inp1)
            self.assertEqual(loss1, loss_orig_params1)

            # Calls into `summon_full_params()`
            self._check_fsdp_parameter_parity(fsdp_model, fsdp_model_orig_params)

            inp2 = fsdp_model.get_input(device)
            loss2 = fsdp_model(*inp2)
            loss_orig_params2 = fsdp_model_orig_params(*inp2)
            self.assertEqual(loss2, loss_orig_params2)

            loss = (loss1 + loss2).sum()
            loss_orig_params = (loss_orig_params1 + loss_orig_params2).sum()
            fsdp_model.run_backward(loss)
            fsdp_model_orig_params.run_backward(loss_orig_params)
            optim.step()
            optim_orig_params.step()
        self._check_fsdp_parameter_parity(fsdp_model, fsdp_model_orig_params)


class TestFSDPUseOrigParamsParamAccess(FSDPTest):
    """Tests original parameter access."""

    @property
    def world_size(self):
        # Force a world size of 2 since the tests hard code to the FSDP
        # sharding strategy to check sharded parameter parity
        return 2

    @skip_if_lt_x_gpu(2)
    def test_access_params_after_forward(self):
        """
        Tests that accessing the original parameters after the forward but
        before the backward. Notably, this is not supported when
        ``use_orig_params=False``. However, for ``True``, FSDP exposes the
        (flattened) sharded original parameters, making it possible.
        """
        self.run_subtests(
            {
                "sharding_strategy": [
                    ShardingStrategy.NO_SHARD,
                    ShardingStrategy.FULL_SHARD,
                    ShardingStrategy.SHARD_GRAD_OP,
                ],
            },
            self._test_access_params_after_forward,
        )

    def _test_access_params_after_forward(
        self,
        sharding_strategy: ShardingStrategy,
    ):
        class Model(nn.Module):
            def __init__(self):
                super().__init__()
                torch.manual_seed(42)
                # 5 * 5 = 25 numel -> pad to 26 -> 13 on each rank
                self.lin1 = nn.Linear(5, 5, bias=False)
                # 5 * 7 + 7 = 42 numel -> no pad -> 21 on each rank
                # 21 of weight on rank 0; 14 of weight and 7 of bias on rank 1
                self.lin2 = nn.Linear(5, 7)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                z = self.lin1(x)
                z = nn.functional.relu(z)
                z = self.lin2(z)
                return z

            def get_input(self, device: torch.device) -> Tuple[torch.Tensor, ...]:
                return (torch.randn((2, 5)).to(device),)

            def get_loss(self, inp, out):
                return out.sum()

        def check_parameter_parity(ddp_model, fsdp_model):
            assert self.rank in (
                0,
                1,
            ), f"Expects world size of 2 but got {self.world_size}"
            for (n1, p1), (n2, p2) in zip(
                ddp_model.module.named_parameters(),
                fsdp_model.named_parameters(),
            ):
                self.assertEqual(n1, clean_tensor_name(n2))
                if sharding_strategy == ShardingStrategy.NO_SHARD:
                    # For `NO_SHARD`, do nothing since the original parameters
                    # are unflattened
                    pass
                # Otherwise, case on the parameter (see the model definition)
                elif n1 == "lin1.weight":
                    if self.rank == 0:
                        p1 = p1.flatten()[:13]
                    elif self.rank == 1:
                        p1 = p1.flatten()[13:]
                elif n1 == "lin2.weight":
                    if self.rank == 0:
                        p1 = p1.flatten()[:21]
                    elif self.rank == 1:
                        p1 = p1.flatten()[21:]
                elif n1 == "lin2.bias":
                    if self.rank == 0:
                        p1 = torch.empty(0, device=p1.device)
                    elif self.rank == 1:
                        p1 = p1.flatten()
                torch.testing.assert_close(p1, p2)

        ddp_model = DDP(Model().cuda(), device_ids=[self.rank])
        fsdp_model = FSDP(
            Model().cuda(),
            sharding_strategy=sharding_strategy,
            auto_wrap_policy=always_wrap_policy,
            use_orig_params=True,
        )
        LR = 1e-2
        ddp_optim = torch.optim.Adam(ddp_model.parameters(), lr=LR)
        fsdp_optim = torch.optim.Adam(fsdp_model.parameters(), lr=LR)
        device = torch.device("cuda")

        inp = fsdp_model.get_input(device)
        ddp_out = ddp_model(*inp)
        fsdp_out = fsdp_model(*inp)
        check_parameter_parity(ddp_model, fsdp_model)

        ddp_loss = ddp_model.module.get_loss(inp, ddp_out)
        fsdp_loss = fsdp_model.get_loss(inp, fsdp_out)
        ddp_loss.backward()
        fsdp_loss.backward()
        ddp_optim.step()
        fsdp_optim.step()
        check_parameter_parity(ddp_model, fsdp_model)

        inp = fsdp_model.get_input(device)
        ddp_out = ddp_model(*inp)
        fsdp_out = fsdp_model(*inp)
        check_parameter_parity(ddp_model, fsdp_model)


class TestFSDPUseOrigParamsWriteback(FSDPTest):
    """Tests parameter and gradient writeback."""
    # TODO (awgu): Add test for gradient writeback.

    @property
    def world_size(self):
        # Force a world size of 2 since the tests hard code to the FSDP
        # sharding strategy
        return 2

    @skip_if_lt_x_gpu(2)
    def test_param_writeback(self):
        """Tests that changes to the original parameters are written back."""
        self.run_subtests(
            {
                "change_first_param": [True, False],  # first vs. second parameter
                "change_data": [True, False],  # change `.data` vs. variable itself
            },
            self._test_param_writeback,
        )

    def _test_param_writeback(self, change_first_param: bool, change_data: bool):
        class Model(nn.Module):
            def __init__(self):
                super().__init__()
                torch.manual_seed(42)
                self.lin1 = nn.Linear(5, 5, bias=False)
                self.lin2 = nn.Linear(5, 7, bias=False)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                z = self.lin1(x)
                z = nn.functional.relu(z)
                z = self.lin2(z)
                return z

            def get_input(self, device: torch.device) -> Tuple[torch.Tensor, ...]:
                return (torch.randn((2, 5)).to(device),)

            def get_loss(self, inp, out):
                return out.sum()

        def transform_param(param: nn.Parameter) -> nn.Parameter:
            return nn.Parameter(torch.ones_like(param) * 2)

        # Check that the writeback propagates
        ddp_model = DDP(Model().cuda(), device_ids=[self.rank])
        fsdp_model = FSDP(Model().cuda(), use_orig_params=True)
        ddp = ddp_model.module  # for brevity
        fsdp = fsdp_model.module
        if change_first_param:
            if change_data:
                ddp.lin1.weight.data = transform_param(ddp.lin1.weight)
                fsdp.lin1.weight.data = transform_param(fsdp.lin1.weight)
            else:
                ddp.lin1.weight = transform_param(ddp.lin1.weight)
                fsdp.lin1.weight = transform_param(fsdp.lin1.weight)
        else:
            if change_data:
                ddp.lin2.weight.data = transform_param(ddp.lin2.weight)
                fsdp.lin2.weight.data = transform_param(fsdp.lin2.weight)
            else:
                ddp.lin2.weight = transform_param(ddp.lin2.weight)
                fsdp.lin2.weight = transform_param(fsdp.lin2.weight)
        with FSDP.summon_full_params(fsdp_model):
            for (n1, p1), (n2, p2) in zip(
                ddp_model.module.named_parameters(),
                fsdp_model.named_parameters(),
            ):
                self.assertEqual(n1, n2)
                torch.testing.assert_close(p1, p2)

        # Check that writing back with mismatched shape errors
        assert self.rank in (0, 1), f"Expects world size of 2 but got {self.world_size}"
        with self.assertRaisesRegex(RuntimeError, "Cannot writeback"):
            # Change the parameter to a new one with 1 added to each dimension
            # to force a shape mismatch when writing back
            if self.rank == 0:
                # Change `lin1.weight` since it exists on rank 0
                lin1_weight_shape = list(fsdp.lin1.weight.shape)
                for dim_index in range(len(lin1_weight_shape)):
                    lin1_weight_shape[dim_index] += 1
                fsdp.lin1.weight = nn.Parameter(
                    torch.randn(
                        torch.Size(lin1_weight_shape), device=fsdp.lin1.weight.device
                    )
                )
            elif self.rank == 1:
                # Change `lin2.weight` since it exists on rank 1
                lin2_weight_shape = list(fsdp.lin2.weight.shape)
                for dim_index in range(len(lin2_weight_shape)):
                    lin2_weight_shape[dim_index] += 1
                fsdp.lin2.weight = nn.Parameter(
                    torch.randn(
                        torch.Size(lin2_weight_shape), device=fsdp.lin2.weight.device
                    )
                )
            with FSDP.summon_full_params(fsdp_model):  # trigger a writeback
                ...


instantiate_parametrized_tests(TestFSDPUseOrigParamsMultipleParamGroups)
instantiate_parametrized_tests(TestFSDPUseOrigParamsUnshardReshard)
instantiate_parametrized_tests(TestFSDPUseOrigParamsParamAccess)

if __name__ == "__main__":
    run_tests()
