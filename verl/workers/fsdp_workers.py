# Copyright 2024 Bytedance Ltd. and/or its affiliates
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
"""
The main entry point to run the PPO algorithm
"""

import logging
import os
import warnings

import torch
import torch.distributed
from codetiming import Timer
from omegaconf import DictConfig, open_dict
from torch.distributed.device_mesh import init_device_mesh

import verl.utils.torch_functional as verl_F
from verl import DataProto
from verl.single_controller.base import Worker
from verl.single_controller.base.decorator import Dispatch, register
from verl.utils import hf_tokenizer
from verl.utils.checkpoint.fsdp_checkpoint_manager import FSDPCheckpointManager
from verl.utils.debug import log_gpu_memory_usage
from verl.utils.flops_counter import FlopsCounter
from verl.utils.fs import copy_to_local
from verl.utils.fsdp_utils import (
    get_fsdp_wrap_policy,
    get_init_weight_context_manager,
    init_fn,
    load_fsdp_model_to_gpu,
    load_fsdp_optimizer,
    offload_fsdp_model_to_cpu,
    offload_fsdp_optimizer,
)
from verl.utils.import_utils import import_external_libs
from verl.utils.model import compute_position_id_with_mask
from verl.workers.sharding_manager.fsdp_ulysses import FSDPUlyssesShardingManager

import re

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv('VERL_PPO_LOGGING_LEVEL', 'WARN'))


def create_device_mesh(world_size, fsdp_size):
    if fsdp_size < 0 or fsdp_size >= world_size:
        device_mesh = init_device_mesh('cuda', mesh_shape=(world_size,), mesh_dim_names=['fsdp'])
    else:
        raise ValueError(
            'HSDP is not supported yet because it produces incorrect results for now. Please set fsdp_size=-1')
        assert world_size % fsdp_size == 0
        device_mesh = init_device_mesh('cuda',
                                       mesh_shape=(world_size // fsdp_size, fsdp_size),
                                       mesh_dim_names=['ddp', 'fsdp'])
    return device_mesh


def get_sharding_strategy(device_mesh):
    from torch.distributed.fsdp import ShardingStrategy
    if device_mesh.ndim == 1:
        sharding_strategy = ShardingStrategy.FULL_SHARD
    elif device_mesh.ndim == 2:
        sharding_strategy = ShardingStrategy.HYBRID_SHARD
    else:
        raise NotImplementedError(f"Get device mesh ndim={device_mesh.ndim}, but only support 1 or 2")
    return sharding_strategy


class ActorRolloutRefWorker(Worker):
    """
    This worker can be instantiated as a standalone actor or a standalone rollout or a standalone reference policy
    or a hybrid engine based on the config.rollout
    """

    def __init__(self, config: DictConfig, role: str):
        super().__init__()
        self.config = config
        import torch.distributed
        if not torch.distributed.is_initialized():
            torch.distributed.init_process_group(backend="nccl")

        # build device mesh for FSDP
        world_size = torch.distributed.get_world_size()
        # TODO(sgm): support FSDP hybrid shard for larger model
        self.device_mesh = create_device_mesh(world_size=world_size, fsdp_size=self.config.actor.fsdp_config.fsdp_size)

        # build device mesh for Ulysses Sequence Parallel
        self.ulysses_device_mesh = None
        self.ulysses_sequence_parallel_size = self.config.actor.get('ulysses_sequence_parallel_size', 1)
        dp = world_size // self.ulysses_sequence_parallel_size
        if self.ulysses_sequence_parallel_size > 1:
            self.ulysses_device_mesh = init_device_mesh('cuda',
                                                        mesh_shape=(dp, self.ulysses_sequence_parallel_size),
                                                        mesh_dim_names=['dp', 'sp'])

        self.ulysses_sharding_manager = FSDPUlyssesShardingManager(self.ulysses_device_mesh)

        self.role = role
        assert self.role in ['actor', 'rollout', 'ref', 'actor_rollout', 'actor_rollout_ref']

        self._is_actor = self.role in ['actor', 'actor_rollout', 'actor_rollout_ref']
        self._is_rollout = self.role in ['rollout', 'actor_rollout', 'actor_rollout_ref']
        self._is_ref = self.role in ['ref', 'actor_rollout_ref']

        self._is_offload_param = False
        self._is_offload_optimizer = False
        if self._is_actor:
            self._is_offload_param = self.config.actor.fsdp_config.get('param_offload', False)
            self._is_offload_optimizer = self.config.actor.fsdp_config.get('optimizer_offload', False)
        elif self._is_ref:
            # TODO: it seems that manual offload is slowly than FSDP offload
            self._is_offload_param = self.config.ref.fsdp_config.get('param_offload', False)

        # normalize config
        if self._is_actor:
            self.config.actor.ppo_mini_batch_size *= self.config.rollout.n
            self.config.actor.ppo_mini_batch_size //= (self.device_mesh.shape[0] // self.ulysses_sequence_parallel_size)
            # micro bsz
            if self.config.actor.ppo_micro_batch_size is not None:
                self.config.actor.ppo_micro_batch_size //= (self.device_mesh.shape[0] //
                                                            self.ulysses_sequence_parallel_size)
                self.config.actor.ppo_micro_batch_size_per_gpu = self.config.actor.ppo_micro_batch_size
                assert self.config.actor.ppo_mini_batch_size % self.config.actor.ppo_micro_batch_size_per_gpu == 0, \
                    f'normalized ppo_mini_batch_size {self.config.actor.ppo_mini_batch_size} should be divisible by ppo_micro_batch_size_per_gpu {self.config.actor.ppo_micro_batch_size_per_gpu}'
                assert self.config.actor.ppo_mini_batch_size // self.config.actor.ppo_micro_batch_size_per_gpu > 0, \
                    f'normalized ppo_mini_batch_size {self.config.actor.ppo_mini_batch_size} should be larger than ppo_micro_batch_size_per_gpu {self.config.actor.ppo_micro_batch_size_per_gpu}'

        # normalize rollout config
        if self._is_rollout and self.config.rollout.log_prob_micro_batch_size is not None:
            self.config.rollout.log_prob_micro_batch_size //= (self.device_mesh.shape[0] //
                                                               self.ulysses_sequence_parallel_size)
            self.config.rollout.log_prob_micro_batch_size_per_gpu = self.config.rollout.log_prob_micro_batch_size
        # normalize ref config
        if self._is_ref and self.config.ref.log_prob_micro_batch_size is not None:
            self.config.ref.log_prob_micro_batch_size //= (self.device_mesh.shape[0] //
                                                           self.ulysses_sequence_parallel_size)
            self.config.ref.log_prob_micro_batch_size_per_gpu = self.config.ref.log_prob_micro_batch_size

    def _build_model_optimizer(self,
                               model_path,
                               fsdp_config,
                               optim_config,
                               override_model_config,
                               use_remove_padding=False,
                               enable_gradient_checkpointing=False,
                               trust_remote_code=False,
                               use_liger=False,
                               role='actor'):
        from torch import optim
        from torch.distributed.fsdp import CPUOffload
        from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
        from torch.distributed.fsdp import MixedPrecision, ShardingStrategy
        from transformers import AutoConfig, AutoModelForCausalLM

        from verl.utils.model import (
            get_generation_config,
            print_model_size,
            update_model_config,
        )
        from verl.utils.torch_dtypes import PrecisionType

        assert role in ['actor', 'ref']

        log_gpu_memory_usage('Before init from HF AutoModel', logger=logger)
        local_path = copy_to_local(model_path)

        # note that we have to create model in fp32. Otherwise, the optimizer is in bf16, which is incorrect
        # TODO(zhangchi.usc1992): 1. support create from random initialized model. 2. Support init with FSDP directly
        self.tokenizer = hf_tokenizer(local_path, trust_remote_code=trust_remote_code)

        torch_dtype = fsdp_config.get('model_dtype', None)
        if torch_dtype is None:
            torch_dtype = torch.float32 if self._is_actor else torch.bfloat16
        else:
            torch_dtype = PrecisionType.to_dtype(torch_dtype)

        # override model kwargs
        actor_model_config = AutoConfig.from_pretrained(local_path, trust_remote_code=trust_remote_code)

        self.generation_config = get_generation_config(local_path, trust_remote_code=trust_remote_code)

        if use_remove_padding:
            from verl.models.registry import check_model_support_rmpad
            check_model_support_rmpad(actor_model_config.model_type)

        if use_remove_padding and self.ulysses_sequence_parallel_size > 1:
            from verl.models.transformers.monkey_patch import apply_monkey_patch
            apply_monkey_patch(actor_model_config, verbose=True)

        override_config_kwargs = {
            'bos_token_id': self.tokenizer.bos_token_id,
            'eos_token_id': self.tokenizer.eos_token_id,
            'pad_token_id': self.tokenizer.pad_token_id,
        }
        override_config_kwargs.update(override_model_config)
        update_model_config(actor_model_config, override_config_kwargs=override_config_kwargs)
        if self.rank == 0:
            print(f'Model config after override: {actor_model_config}')

        # NOTE(fix me): tie_word_embedding causes meta_tensor init to hang
        init_context = get_init_weight_context_manager(use_meta_tensor=not actor_model_config.tie_word_embeddings)

        with init_context(), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            actor_module = AutoModelForCausalLM.from_pretrained(pretrained_model_name_or_path=local_path,
                                                                torch_dtype=torch_dtype,
                                                                config=actor_model_config,
                                                                attn_implementation='flash_attention_2',
                                                                trust_remote_code=trust_remote_code)
            # Apply Liger kernel to the model if use_liger is set to True
            if use_liger:
                from liger_kernel.transformers.monkey_patch import (
                    _apply_liger_kernel_to_instance,
                )
                _apply_liger_kernel_to_instance(model=actor_module)

            # some parameters may not in torch_dtype. TODO(zhangchi.usc1992) remove this after we switch to fsdp2
            actor_module.to(torch_dtype)

            if enable_gradient_checkpointing:
                actor_module.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
        torch.distributed.barrier()

        if self.rank == 0:
            print_model_size(actor_module)

        log_gpu_memory_usage('After init from HF AutoModel', logger=logger)

        # We wrap FSDP for rollout as well
        mixed_precision_config = fsdp_config.get('mixed_precision', None)
        if mixed_precision_config is not None:
            param_dtype = PrecisionType.to_dtype(mixed_precision_config.get('param_dtype', 'bf16'))
            reduce_dtype = PrecisionType.to_dtype(mixed_precision_config.get('reduce_dtype', 'fp32'))
            buffer_dtype = PrecisionType.to_dtype(mixed_precision_config.get('buffer_dtype', 'fp32'))
        else:
            param_dtype = torch.bfloat16
            reduce_dtype = torch.float32
            buffer_dtype = torch.float32

        mixed_precision = MixedPrecision(param_dtype=param_dtype, reduce_dtype=reduce_dtype, buffer_dtype=buffer_dtype)

        auto_wrap_policy = get_fsdp_wrap_policy(module=actor_module, config=fsdp_config.get('wrap_policy', None))

        if self._is_rollout and self.config.rollout.name == 'hf':
            # TODO(zhangchi.usc1992, shengguangming) fix me. Current, auto_wrap_policy causes HFRollout to hang in Gemma
            auto_wrap_policy = None

        # print(f'wrap_policy: {auto_wrap_policy}')

        fsdp_mesh = self.device_mesh
        sharding_strategy = get_sharding_strategy(fsdp_mesh)

        # TODO: add transformer policy
        # We force reference policy to use CPUOffload to save memory.
        # We force turn off CPUOffload for actor because it causes incorrect results when using grad accumulation
        cpu_offload = None if role == 'actor' else CPUOffload(offload_params=True)
        actor_module_fsdp = FSDP(
            actor_module,
            cpu_offload=cpu_offload,
            param_init_fn=init_fn,
            use_orig_params=False,
            auto_wrap_policy=auto_wrap_policy,
            device_id=torch.cuda.current_device(),
            sharding_strategy=sharding_strategy,  # zero3
            mixed_precision=mixed_precision,
            sync_module_states=True,
            device_mesh=self.device_mesh,
            forward_prefetch=False)

        log_gpu_memory_usage('After Actor FSDP init', logger=logger)

        # TODO: add more optimizer args into config
        if role == 'actor':
            from verl.utils.torch_functional import get_constant_schedule_with_warmup
            actor_optimizer = optim.AdamW(actor_module_fsdp.parameters(),
                                          lr=optim_config.lr,
                                          betas=optim_config.get('betas', (0.9, 0.999)),
                                          weight_decay=optim_config.get('weight_decay', 1e-2))

            total_steps = optim_config.get('total_training_steps', 0)
            num_warmup_steps_ratio = optim_config.get('lr_warmup_steps_ratio', 0.)
            num_warmup_steps = int(num_warmup_steps_ratio * total_steps)

            print(f'Total steps: {total_steps}, num_warmup_steps: {num_warmup_steps}')

            actor_lr_scheduler = get_constant_schedule_with_warmup(optimizer=actor_optimizer,
                                                                   num_warmup_steps=num_warmup_steps)
        else:
            actor_optimizer = None
            actor_lr_scheduler = None

        log_gpu_memory_usage('After actor optimizer init', logger=logger)

        return actor_module_fsdp, actor_optimizer, actor_lr_scheduler, actor_model_config

    def _build_rollout(self):
        from torch.distributed.device_mesh import init_device_mesh

        # TODO(sgm): support FSDP hybrid shard for larger model
        infer_tp = self.config.rollout.tensor_model_parallel_size
        dp = self.world_size // infer_tp
        assert self.world_size % infer_tp == 0, f'rollout world_size: {self.world_size} is not divisible by infer_tp: {infer_tp}'
        rollout_device_mesh = init_device_mesh('cuda', mesh_shape=(dp, infer_tp), mesh_dim_names=['dp', 'infer_tp'])

        if self.config.rollout.name == 'hf':
            from verl.workers.rollout import HFRollout
            from verl.workers.sharding_manager import BaseShardingManager
            rollout = HFRollout(module=self.actor_module_fsdp, config=self.config.rollout)
            rollout_sharding_manager = BaseShardingManager()
            # TODO: a sharding manager that do nothing?
        elif self.config.rollout.name == 'vllm':
            if self.config.rollout.use_fire_sampling:
                from verl.workers.rollout.vllm_rollout import (
                    FIREvLLMRollout as vLLMRollout,
                )
                from verl.workers.rollout.vllm_rollout import vllm_mode
            else:
                from verl.workers.rollout.vllm_rollout import vLLMRollout, vllm_mode
            from verl.workers.sharding_manager import FSDPVLLMShardingManager
            log_gpu_memory_usage('Before building vllm rollout', logger=None)
            local_path = copy_to_local(self.config.model.path)
            if vllm_mode == 'customized':
                rollout = vLLMRollout(actor_module=self.actor_module_fsdp,
                                      config=self.config.rollout,
                                      tokenizer=self.tokenizer,
                                      model_hf_config=self.actor_model_config)
            elif vllm_mode == 'spmd':
                rollout = vLLMRollout(model_path=local_path,
                                      config=self.config.rollout,
                                      tokenizer=self.tokenizer,
                                      model_hf_config=self.actor_model_config,
                                      device_mesh=rollout_device_mesh)
            else:
                raise NotImplementedError("vllm_mode must be 'customized' or 'spmd'")
            log_gpu_memory_usage('After building vllm rollout', logger=None)
            if torch.distributed.get_world_size() == 1:
                self.config.rollout.load_format = 'dummy_hf'
            rollout_sharding_manager = FSDPVLLMShardingManager(module=self.actor_module_fsdp,
                                                               inference_engine=rollout.inference_engine,
                                                               model_config=self.actor_model_config,
                                                               full_params='hf' in self.config.rollout.load_format,
                                                               device_mesh=rollout_device_mesh)
            log_gpu_memory_usage('After building sharding manager', logger=None)

        return rollout, rollout_sharding_manager

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        from verl.workers.actor import DataParallelPPOActor

        # This is used to import external_lib into the huggingface systems
        import_external_libs(self.config.model.get('external_lib', None))

        from omegaconf import OmegaConf
        override_model_config = OmegaConf.to_container(self.config.model.get('override_config', OmegaConf.create()))

        use_remove_padding = self.config.model.get('use_remove_padding', False)

        if self._is_actor or self._is_rollout:
            # we need the model for actor and rollout
            if self._is_actor:
                optim_config = self.config.actor.optim
                fsdp_config = self.config.actor.fsdp_config
            else:
                optim_config = None
                fsdp_config = OmegaConf.create()
            self.actor_module_fsdp, self.actor_optimizer, self.actor_lr_scheduler, self.actor_model_config = self._build_model_optimizer(
                model_path=self.config.model.path,
                fsdp_config=fsdp_config,
                optim_config=optim_config,
                override_model_config=override_model_config,
                use_remove_padding=use_remove_padding,
                enable_gradient_checkpointing=self.config.model.get('enable_gradient_checkpointing', False),
                trust_remote_code=self.config.model.get('trust_remote_code', False),
                use_liger=self.config.model.get('use_liger', False),
                role='actor')

            # get the original unwrapped module
            self.actor_module = self.actor_module_fsdp._fsdp_wrapped_module

            if self._is_offload_optimizer:
                offload_fsdp_optimizer(optimizer=self.actor_optimizer)
                log_gpu_memory_usage('After offload actor optimizer during init', logger=logger)
        # load from checkpoint
        if self._is_actor:
            OmegaConf.set_struct(self.config.actor, True)
            with open_dict(self.config.actor):
                self.config.actor.use_remove_padding = use_remove_padding
            self.actor = DataParallelPPOActor(config=self.config.actor,
                                              actor_module=self.actor_module_fsdp,
                                              actor_optimizer=self.actor_optimizer)

        if self._is_rollout:
            self.rollout, self.rollout_sharding_manager = self._build_rollout()

        if self._is_ref:
            self.ref_module_fsdp = self._build_model_optimizer(model_path=self.config.model.path,
                                                               fsdp_config=self.config.ref.fsdp_config,
                                                               optim_config=None,
                                                               override_model_config=override_model_config,
                                                               use_remove_padding=use_remove_padding,
                                                               trust_remote_code=self.config.model.get(
                                                                   'trust_remote_code', False),
                                                               use_liger=self.config.model.get('use_liger', False),
                                                               role='ref')[0]
            OmegaConf.set_struct(self.config.ref, True)
            with open_dict(self.config.ref):
                self.config.ref.use_remove_padding = use_remove_padding
            self.ref_policy = DataParallelPPOActor(config=self.config.ref, actor_module=self.ref_module_fsdp)

        if self._is_actor:
            self.flops_counter = FlopsCounter(self.actor_model_config)
            self.checkpoint_manager = FSDPCheckpointManager(model=self.actor_module_fsdp,
                                                            optimizer=self.actor.actor_optimizer,
                                                            lr_scheduler=self.actor_lr_scheduler,
                                                            tokenizer=self.tokenizer)

        torch.cuda.empty_cache()

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def update_actor(self, data: DataProto):
        data = data.to('cuda')

        assert self._is_actor
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.actor_module_fsdp)
        if self._is_offload_optimizer:
            load_fsdp_optimizer(optimizer=self.actor_optimizer, device_id=torch.cuda.current_device())

        data.batch = data.batch.cuda()

        log_gpu_memory_usage('Before update policy', logger=logger)

        with self.ulysses_sharding_manager:
            data = self.ulysses_sharding_manager.preprocess_data(data=data)
            # perform training
            with Timer(name='update_policy', logger=None) as timer:
                metrics = self.actor.update_policy(data=data)
            delta_time = timer.last
            global_num_tokens = data.meta_info['global_token_num']
            estimated_flops, promised_flops = self.flops_counter.estimate_flops(global_num_tokens, delta_time)
            metrics['mfu/actor'] = estimated_flops * self.config.actor.ppo_epochs / promised_flops / self.world_size

            self.actor_lr_scheduler.step()
            lr = self.actor_lr_scheduler.get_last_lr()[0]
            metrics['actor/lr'] = lr

            log_gpu_memory_usage('After update policy', logger=logger)

            # TODO: here, we should return all metrics
            output = DataProto(meta_info={'metrics': metrics})

            output = self.ulysses_sharding_manager.postprocess_data(data=output)
            output = output.to('cpu')

        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.actor_module_fsdp)
        if self._is_offload_optimizer:
            offload_fsdp_optimizer(optimizer=self.actor_optimizer)
        torch.cuda.empty_cache()
        return output

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def generate_sequences(self, prompts: DataProto):
        prompts = prompts.to('cuda')

        assert self._is_rollout
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.actor_module_fsdp)

        prompts.batch = prompts.batch.cuda()
        meta_info = {
            'eos_token_id':
                self.generation_config.eos_token_id
                if self.generation_config is not None else self.tokenizer.eos_token_id,
            'pad_token_id':
                self.generation_config.pad_token_id
                if self.generation_config is not None else self.tokenizer.pad_token_id,
        }
        prompts.meta_info.update(meta_info)
        with self.rollout_sharding_manager:

            # after parameters sync with rollout, offload actor model to CPU
            if self._is_offload_param:
                offload_fsdp_model_to_cpu(self.actor_module_fsdp)
            if self._is_offload_optimizer:
                offload_fsdp_optimizer(optimizer=self.actor_optimizer)

            log_gpu_memory_usage('After entering rollout sharding manager', logger=logger)

            prompts = self.rollout_sharding_manager.preprocess_data(prompts)
            output = self.rollout.generate_sequences(prompts=prompts)

            log_gpu_memory_usage('After rollout generation', logger=logger)

            output = self.rollout_sharding_manager.postprocess_data(output)

        output = output.to('cpu')

        # clear kv cache
        torch.cuda.empty_cache()
        log_gpu_memory_usage('After recompute log prob', logger=logger)
        return output

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_log_prob(self, data: DataProto):
        assert self._is_actor
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.actor_module_fsdp)
        data = data.to('cuda')
        # we should always recompute old_log_probs when it is HybridEngine
        data.meta_info['micro_batch_size'] = self.config.rollout.log_prob_micro_batch_size_per_gpu
        data.meta_info['max_token_len'] = self.config.rollout.log_prob_max_token_len_per_gpu
        data.meta_info['use_dynamic_bsz'] = self.config.rollout.log_prob_use_dynamic_bsz
        data.meta_info['temperature'] = self.config.rollout.temperature
        # perform recompute log_prob
        with self.ulysses_sharding_manager:
            data = self.ulysses_sharding_manager.preprocess_data(data)
            output = self.actor.compute_log_prob(data=data)
            output = DataProto.from_dict(tensors={'old_log_probs': output},
                                         meta_info={'temperature': self.config.rollout.temperature})
            output = self.ulysses_sharding_manager.postprocess_data(output)

        output = output.to('cpu')

        # https://pytorch.org/docs/stable/notes/fsdp.html#fsdp-notes
        # unshard the root FSDP module
        if self.world_size > 1:
            self.actor.actor_module._handle.reshard(True)

        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.actor_module_fsdp)

        # clear kv cache
        torch.cuda.empty_cache()
        log_gpu_memory_usage('After compute_log_prob', logger=logger)
        return output

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_ref_log_prob(self, data: DataProto):
        assert self._is_ref

        data = data.to('cuda')

        micro_batch_size = self.config.ref.log_prob_micro_batch_size_per_gpu
        data.meta_info['micro_batch_size'] = micro_batch_size
        data.meta_info['temperature'] = self.config.rollout.temperature
        data.meta_info['max_token_len'] = self.config.ref.log_prob_max_token_len_per_gpu
        data.meta_info['use_dynamic_bsz'] = self.config.ref.log_prob_use_dynamic_bsz
        with self.ulysses_sharding_manager:
            data = self.ulysses_sharding_manager.preprocess_data(data)
            output = self.ref_policy.compute_log_prob(data=data)
            output = DataProto.from_dict(tensors={'ref_log_prob': output})
            output = self.ulysses_sharding_manager.postprocess_data(output)

        output = output.to('cpu')

        # https://pytorch.org/docs/stable/notes/fsdp.html#fsdp-notes
        # unshard the root FSDP module
        if self.world_size > 1:
            self.ref_policy.actor_module._handle.reshard(True)

        torch.cuda.empty_cache()
        return output

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def save_checkpoint(self, local_path, hdfs_path=None, global_step=0, remove_previous_ckpt=False):
        # only support save and load ckpt for actor
        assert self._is_actor
        import torch
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.actor_module_fsdp)

        self.checkpoint_manager.save_checkpoint(local_path=local_path,
                                                hdfs_path=hdfs_path,
                                                global_step=global_step,
                                                remove_previous_ckpt=remove_previous_ckpt)

        torch.distributed.barrier()
        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.actor_module_fsdp)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_checkpoint(self, path, del_local_after_load=False):
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.actor_module_fsdp)

        self.checkpoint_manager.load_checkpoint(path=path, del_local_after_load=del_local_after_load)

        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.actor_module_fsdp)

        if self._is_offload_optimizer:
            offload_fsdp_optimizer(self.actor_optimizer)


class CriticWorker(Worker):

    def __init__(self, config):
        super().__init__()
        import torch.distributed
        if not torch.distributed.is_initialized():
            torch.distributed.init_process_group(backend="nccl")
        self.config = config

        # build device mesh for Ulysses Sequence Parallel
        world_size = torch.distributed.get_world_size()
        from torch.distributed.device_mesh import init_device_mesh

        fsdp_size = self.config.model.fsdp_config.fsdp_size
        self.device_mesh = create_device_mesh(world_size=world_size, fsdp_size=fsdp_size)

        self.ulysses_device_mesh = None
        self.ulysses_sequence_parallel_size = self.config.get('ulysses_sequence_parallel_size', 1)
        dp = world_size // self.ulysses_sequence_parallel_size
        if self.ulysses_sequence_parallel_size > 1:
            self.ulysses_device_mesh = init_device_mesh('cuda',
                                                        mesh_shape=(dp, self.ulysses_sequence_parallel_size),
                                                        mesh_dim_names=['dp', 'sp'])

        self.ulysses_sharding_manager = FSDPUlyssesShardingManager(self.ulysses_device_mesh)

        # set FSDP offload params
        self._is_offload_param = self.config.model.fsdp_config.param_offload
        self._is_offload_optimizer = self.config.model.fsdp_config.optimizer_offload

        # normalize config
        self.config.ppo_mini_batch_size //= (torch.distributed.get_world_size() // self.ulysses_sequence_parallel_size)
        if self.config.ppo_micro_batch_size is not None:
            self.config.ppo_micro_batch_size //= (torch.distributed.get_world_size() //
                                                  self.ulysses_sequence_parallel_size)
            self.config.forward_micro_batch_size //= (torch.distributed.get_world_size() //
                                                      self.ulysses_sequence_parallel_size)
            self.config.ppo_micro_batch_size_per_gpu = self.config.ppo_micro_batch_size
            self.config.forward_micro_batch_size_per_gpu = self.config.forward_micro_batch_size
            assert self.config.ppo_mini_batch_size % self.config.ppo_micro_batch_size_per_gpu == 0, \
                f'normalized ppo_mini_batch_size {self.config.ppo_mini_batch_size} should be divisible by ppo_micro_batch_size_per_gpu {self.config.ppo_micro_batch_size_per_gpu}'
            assert self.config.ppo_mini_batch_size // self.config.ppo_micro_batch_size_per_gpu > 0, \
                f'normalized ppo_mini_batch_size {self.config.ppo_mini_batch_size} should be larger than ppo_micro_batch_size_per_gpu {self.config.ppo_micro_batch_size_per_gpu}'

    def _build_critic_model_optimizer(self, config):
        # the following line is necessary
        from torch import optim
        from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
        from torch.distributed.fsdp import MixedPrecision, ShardingStrategy

        from verl.utils.model import LambdaLayer, print_model_size, squeeze
        from verl.utils.torch_dtypes import PrecisionType

        local_path = copy_to_local(config.model.path)
        # note that the tokenizer between actor and critic may be different. So override tokenizer info with actor info
        # using random initialized model from any architecture. May not be the same as Actor.

        tokenizer_path = copy_to_local(config.model.tokenizer_path)
        self.tokenizer = hf_tokenizer(tokenizer_path, trust_remote_code=config.model.get('trust_remote_code', False))

        from omegaconf import OmegaConf
        override_config = OmegaConf.to_container(self.config.model.get('override_config', OmegaConf.create()))
        override_config_kwargs = {
            'bos_token_id': self.tokenizer.bos_token_id,
            'eos_token_id': self.tokenizer.eos_token_id,
            'pad_token_id': self.tokenizer.pad_token_id,
        }
        override_config_kwargs.update(override_config)
        if self.rank == 0:
            print(f'Critic overriding config {override_config_kwargs}')

        torch_dtype = self.config.model.fsdp_config.get('model_dtype', 'fp32')
        torch_dtype = PrecisionType.to_dtype(torch_dtype)

        from torch import nn
        from transformers import AutoConfig, AutoModelForTokenClassification

        trust_remote_code = False
        critic_model_config = AutoConfig.from_pretrained(local_path, trust_remote_code=trust_remote_code)
        critic_model_config.num_labels = 1

        use_remove_padding = config.model.get('use_remove_padding', False)
        if use_remove_padding:
            from verl.models.registry import check_model_support_rmpad
            check_model_support_rmpad(critic_model_config.model_type)

        if use_remove_padding and self.ulysses_sequence_parallel_size > 1:
            from verl.models.transformers.monkey_patch import apply_monkey_patch
            apply_monkey_patch(critic_model_config, verbose=True)

        init_context = get_init_weight_context_manager()
        with init_context(), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            setattr(critic_model_config, 'classifier_dropout', 0.)
            setattr(critic_model_config, 'hidden_dropout', '0')
            critic_module = AutoModelForTokenClassification.from_pretrained(pretrained_model_name_or_path=local_path,
                                                                            torch_dtype=torch_dtype,
                                                                            config=critic_model_config,
                                                                            attn_implementation='flash_attention_2',
                                                                            trust_remote_code=trust_remote_code)

            # some parameters may not in torch_dtype
            critic_module.to(torch_dtype)

            if config.model.get('enable_gradient_checkpointing', False):
                critic_module.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
        if self.rank == 0:
            print_model_size(critic_module)

        self.critic_model_config = critic_model_config

        fsdp_config = self.config.model.fsdp_config
        mixed_precision_config = fsdp_config.get('mixed_precision', None)
        if mixed_precision_config is not None:
            param_dtype = PrecisionType.to_dtype(mixed_precision_config.get('param_dtype', 'bf16'))
            reduce_dtype = PrecisionType.to_dtype(mixed_precision_config.get('reduce_dtype', 'fp32'))
            buffer_dtype = PrecisionType.to_dtype(mixed_precision_config.get('buffer_dtype', 'fp32'))
        else:
            param_dtype = torch.bfloat16
            reduce_dtype = torch.float32
            buffer_dtype = torch.float32

        mixed_precision = MixedPrecision(param_dtype=param_dtype, reduce_dtype=reduce_dtype, buffer_dtype=buffer_dtype)

        auto_wrap_policy = get_fsdp_wrap_policy(module=critic_module, config=fsdp_config.get('wrap_policy', None))

        log_gpu_memory_usage('Before critic FSDP', logger=None)

        fsdp_mesh = self.device_mesh
        sharding_strategy = get_sharding_strategy(fsdp_mesh)

        # Note: We force turn off CPUOffload for critic because it causes incorrect results when using grad accumulation
        critic_module = FSDP(critic_module,
                             param_init_fn=init_fn,
                             use_orig_params=False,
                             auto_wrap_policy=auto_wrap_policy,
                             device_id=torch.cuda.current_device(),
                             sharding_strategy=sharding_strategy,
                             mixed_precision=mixed_precision,
                             sync_module_states=True,
                             forward_prefetch=False,
                             device_mesh=self.device_mesh,
                             cpu_offload=None)

        log_gpu_memory_usage('After critic FSDP', logger=None)

        critic_optimizer = optim.AdamW(critic_module.parameters(),
                                       lr=config.optim.lr,
                                       betas=config.optim.get('betas', (0.9, 0.999)),
                                       weight_decay=config.optim.get('weight_decay', 1e-2))

        total_steps = config.optim.get('total_training_steps', 0)
        num_warmup_steps_ratio = config.optim.get('lr_warmup_steps_ratio', 0.)
        num_warmup_steps = int(num_warmup_steps_ratio * total_steps)

        print(f'Total steps: {total_steps}, num_warmup_steps: {num_warmup_steps}')

        from verl.utils.torch_functional import get_constant_schedule_with_warmup
        critic_lr_scheduler = get_constant_schedule_with_warmup(optimizer=critic_optimizer,
                                                                num_warmup_steps=num_warmup_steps)

        return critic_module, critic_optimizer, critic_lr_scheduler

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        # This is used to import external_lib into the huggingface systems
        import_external_libs(self.config.model.get('external_lib', None))

        from verl.workers.critic import DataParallelPPOCritic
        self.critic_module, self.critic_optimizer, self.critic_lr_scheduler = self._build_critic_model_optimizer(
            self.config)

        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.critic_module)
        if self._is_offload_optimizer:
            offload_fsdp_optimizer(optimizer=self.critic_optimizer)

        self.critic = DataParallelPPOCritic(config=self.config,
                                            critic_module=self.critic_module,
                                            critic_optimizer=self.critic_optimizer)

        self.flops_counter = FlopsCounter(self.critic_model_config)
        self.checkpoint_manager = FSDPCheckpointManager(model=self.critic_module,
                                                        optimizer=self.critic_optimizer,
                                                        lr_scheduler=self.critic_lr_scheduler,
                                                        tokenizer=self.tokenizer)

        torch.cuda.empty_cache()

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_values(self, data: DataProto):
        data = data.to('cuda')

        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.critic_module)
        micro_batch_size = self.config.forward_micro_batch_size_per_gpu
        data.meta_info['micro_batch_size'] = micro_batch_size
        data.meta_info['max_token_len'] = self.config.forward_max_token_len_per_gpu
        data.meta_info['use_dynamic_bsz'] = self.config.use_dynamic_bsz
        # perform forward computation
        with self.ulysses_sharding_manager:
            data = self.ulysses_sharding_manager.preprocess_data(data=data)
            values = self.critic.compute_values(data=data)
            output = DataProto.from_dict(tensors={'values': values})
            output = self.ulysses_sharding_manager.postprocess_data(data=output)

        output = output.to('cpu')
        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.critic_module)
        return output

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def update_critic(self, data: DataProto):
        data = data.to('cuda')
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.critic_module)
        if self._is_offload_optimizer:
            load_fsdp_optimizer(optimizer=self.critic_optimizer, device_id=torch.cuda.current_device())

        # perform forward computation
        with self.ulysses_sharding_manager:
            data = self.ulysses_sharding_manager.preprocess_data(data=data)

            with Timer(name='update_critic', logger=None) as timer:
                metrics = self.critic.update_critic(data=data)
            delta_time = timer.last

            global_num_tokens = data.meta_info['global_token_num']
            estimated_flops, promised_flops = self.flops_counter.estimate_flops(global_num_tokens, delta_time)
            metrics['mfu/critic'] = estimated_flops * self.config.ppo_epochs / promised_flops / self.world_size

            self.critic_lr_scheduler.step()
            lr = self.critic_lr_scheduler.get_last_lr()[0]
            metrics['critic/lr'] = lr

            output = DataProto(batch=None, meta_info={'metrics': metrics})
            output = self.ulysses_sharding_manager.postprocess_data(data=output)

        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.critic_module)
        if self._is_offload_optimizer:
            offload_fsdp_optimizer(optimizer=self.critic_optimizer)
        torch.cuda.empty_cache()
        output = output.to('cpu')
        return output

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def save_checkpoint(self, local_path, hdfs_path=None, global_step=0, remove_previous_ckpt=False):
        import torch
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.critic_module)

        self.checkpoint_manager.save_checkpoint(local_path=local_path,
                                                hdfs_path=hdfs_path,
                                                global_step=global_step,
                                                remove_previous_ckpt=remove_previous_ckpt)

        torch.distributed.barrier()
        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.critic_module)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_checkpoint(self, path, del_local_after_load=True):
        import torch
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.critic_module)

        self.checkpoint_manager.load_checkpoint(path=path, del_local_after_load=del_local_after_load)

        torch.distributed.barrier()
        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.critic_module)

        if self._is_offload_optimizer:
            offload_fsdp_optimizer(self.critic_optimizer)


# TODO(sgm): we may need to extract it to dp_reward_model.py
class RewardModelWorker(Worker):
    """
    Note that we only implement the reward model that is subclass of AutoModelForTokenClassification.
    """

    def __init__(self, config):
        super().__init__()
        import torch.distributed
        if not torch.distributed.is_initialized():
            torch.distributed.init_process_group(backend="nccl")
        self.config = config

        # build device mesh for Ulysses Sequence Parallel
        world_size = torch.distributed.get_world_size()
        from torch.distributed.device_mesh import init_device_mesh

        fsdp_size = self.config.model.fsdp_config.fsdp_size
        self.device_mesh = create_device_mesh(world_size=world_size, fsdp_size=fsdp_size)

        self.ulysses_device_mesh = None
        self.ulysses_sequence_parallel_size = self.config.get('ulysses_sequence_parallel_size', 1)
        dp = world_size // self.ulysses_sequence_parallel_size
        if self.ulysses_sequence_parallel_size > 1:
            self.ulysses_device_mesh = init_device_mesh('cuda',
                                                        mesh_shape=(dp, self.ulysses_sequence_parallel_size),
                                                        mesh_dim_names=['dp', 'sp'])

        self.ulysses_sharding_manager = FSDPUlyssesShardingManager(self.ulysses_device_mesh)

        self.use_remove_padding = self.config.model.get('use_remove_padding', False)

        # normalize config
        if self.config.micro_batch_size is not None:
            self.config.micro_batch_size //= torch.distributed.get_world_size()
            self.config.micro_batch_size_per_gpu = self.config.micro_batch_size

    def _build_model(self, config):
        # the following line is necessary
        from torch.distributed.fsdp import CPUOffload
        from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
        from torch.distributed.fsdp import ShardingStrategy
        from transformers import AutoConfig, AutoModelForTokenClassification

        # download the checkpoint from hdfs
        local_path = copy_to_local(config.model.path)

        if self.config.model.input_tokenizer is None:
            self._do_switch_chat_template = False
        else:
            self._do_switch_chat_template = True
            input_tokenizer_local_path = copy_to_local(config.model.input_tokenizer)
            self.input_tokenizer = hf_tokenizer(input_tokenizer_local_path,
                                                trust_remote_code=config.model.get('trust_remote_code', False))
            self.tokenizer = hf_tokenizer(local_path, trust_remote_code=config.model.get('trust_remote_code', False))

        trust_remote_code = config.model.get('trust_remote_code', False)
        model_config = AutoConfig.from_pretrained(local_path, trust_remote_code=trust_remote_code)
        model_config.num_labels = 1

        use_remove_padding = config.model.get('use_remove_padding', False)
        if use_remove_padding:
            from verl.models.registry import check_model_support_rmpad
            check_model_support_rmpad(model_config.model_type)

        if use_remove_padding and self.ulysses_sequence_parallel_size > 1:
            from verl.models.transformers.monkey_patch import apply_monkey_patch
            apply_monkey_patch(model_config, verbose=True)

        # note that we have to create model in fp32. Otherwise, the optimizer is in bf16, which is incorrect
        init_context = get_init_weight_context_manager(use_meta_tensor=not model_config.tie_word_embeddings)

        with init_context(), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            setattr(model_config, 'classifier_dropout', 0.)
            reward_module = AutoModelForTokenClassification.from_pretrained(pretrained_model_name_or_path=local_path,
                                                                            config=model_config,
                                                                            torch_dtype=torch.bfloat16,
                                                                            attn_implementation='flash_attention_2',
                                                                            trust_remote_code=trust_remote_code)
            reward_module.to(torch.bfloat16)
        auto_wrap_policy = get_fsdp_wrap_policy(module=reward_module, config=self.config.model.fsdp_config)

        fsdp_mesh = self.device_mesh
        sharding_strategy = get_sharding_strategy(fsdp_mesh)

        reward_module = FSDP(
            reward_module,
            param_init_fn=init_fn,
            use_orig_params=False,
            auto_wrap_policy=auto_wrap_policy,
            device_id=torch.cuda.current_device(),
            sharding_strategy=sharding_strategy,  # zero3
            sync_module_states=True,
            cpu_offload=CPUOffload(offload_params=True),
            forward_prefetch=False,
            device_mesh=self.device_mesh)

        return reward_module

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        # This is used to import external_lib into the huggingface systems
        import_external_libs(self.config.model.get('external_lib', None))
        self.reward_module = self._build_model(config=self.config)
        torch.cuda.empty_cache()

    def _forward_micro_batch(self, micro_batch):
        from flash_attn.bert_padding import (
            index_first_axis,
            pad_input,
            rearrange,
            unpad_input,
        )

        from verl.utils.ulysses import (
            gather_outpus_and_unpad,
            ulysses_pad_and_slice_inputs,
        )

        with torch.no_grad(), torch.autocast(device_type='cuda', dtype=torch.bfloat16):
            input_ids = micro_batch['input_ids']
            batch_size, seqlen = input_ids.shape
            attention_mask = micro_batch['attention_mask']
            position_ids = micro_batch['position_ids']

            if self.use_remove_padding:
                input_ids_rmpad, indices, *_ = unpad_input(input_ids.unsqueeze(-1),
                                                           attention_mask)  # input_ids_rmpad (total_nnz, ...)
                input_ids_rmpad = input_ids_rmpad.transpose(0, 1)  # (1, total_nnz)

                # unpad the position_ids to align the rotary
                position_ids_rmpad = index_first_axis(rearrange(position_ids.unsqueeze(-1), "b s ... -> (b s) ..."),
                                                      indices).transpose(0, 1)

                # pad and slice the inputs if sp > 1
                if self.ulysses_sequence_parallel_size > 1:
                    input_ids_rmpad, position_ids_rmpad, pad_size = ulysses_pad_and_slice_inputs(input_ids_rmpad, \
                                                                                                position_ids_rmpad, \
                                                                                                sp_size=self.ulysses_sequence_parallel_size)

                # only pass input_ids and position_ids to enable flash_attn_varlen
                output = self.reward_module(input_ids=input_ids_rmpad,
                                            attention_mask=None,
                                            position_ids=position_ids_rmpad,
                                            use_cache=False)  # prevent model thinks we are generating
                reward_rmpad = output.logits
                reward_rmpad = reward_rmpad.squeeze(0)  # (total_nnz)

                # gather output if sp > 1
                if self.ulysses_sequence_parallel_size > 1:
                    reward_rmpad = gather_outpus_and_unpad(reward_rmpad,
                                                           gather_dim=0,
                                                           unpad_dim=0,
                                                           padding_size=pad_size)

                # pad it back
                rm_score = pad_input(reward_rmpad, indices=indices, batch=batch_size, seqlen=seqlen).squeeze(-1)
            else:
                output = self.reward_module(input_ids=input_ids,
                                            attention_mask=attention_mask,
                                            position_ids=position_ids)
                rm_score = output.logits  # (batch_size, seq_len, 1)
                rm_score = rm_score.squeeze(-1)

            # extract the result of the last valid token
            eos_mask_idx = torch.argmax(position_ids * attention_mask, dim=-1)  # (bsz,)
            rm_score = rm_score[torch.arange(batch_size), eos_mask_idx]
            return rm_score

    def _expand_to_token_level(self, data: DataProto, scores: torch.Tensor):
        batch_size = data.batch.batch_size[0]
        # expand as token_level_reward
        attention_mask = data.batch['attention_mask']
        position_ids = data.batch['position_ids']
        response_length = data.batch['responses'].shape[-1]
        eos_mask_idx = torch.argmax(position_ids * attention_mask, dim=-1)  # (bsz,)
        token_level_scores = torch.zeros_like(attention_mask, dtype=scores.dtype)  # (bsz, seqlen)
        token_level_scores[torch.arange(batch_size), eos_mask_idx] = scores

        # select the response part
        token_level_scores = token_level_scores[:, -response_length:]

        return token_level_scores

    def _switch_chat_template(self, data: DataProto):
        src_max_length = data.batch['attention_mask'].shape[-1]

        src_tokenizer = self.input_tokenizer
        target_tokenizer = self.tokenizer

        rm_input_ids = []
        rm_attention_mask = []

        for i in range(data.batch.batch_size[0]):
            # extract raw prompt
            chat: list = data.non_tensor_batch['raw_prompt'][i].tolist()

            # extract response
            response_ids = data.batch['responses'][i]
            response_length = response_ids.shape[-1]
            valid_response_length = data.batch['attention_mask'][i][-response_length:].sum()
            valid_response_ids = response_ids[:valid_response_length]

            # decode
            response = src_tokenizer.decode(valid_response_ids)
            # remove bos and eos
            response = response.replace(src_tokenizer.eos_token, '')

            chat.append({'role': 'assistant', 'content': response})

            prompt_with_chat_template = target_tokenizer.apply_chat_template(chat,
                                                                             add_generation_prompt=False,
                                                                             tokenize=False)
            if self.rank == 0 and i == 0:
                # for debugging purpose
                print(f'Switch template. chat: {prompt_with_chat_template}')

            # the maximum length is actually determined by the reward model itself
            max_length = self.config.get('max_length', src_max_length)
            if max_length is None:
                max_length = src_max_length
            input_ids, attention_mask = verl_F.tokenize_and_postprocess_data(
                prompt=prompt_with_chat_template,
                tokenizer=target_tokenizer,
                max_length=max_length,
                pad_token_id=target_tokenizer.pad_token_id,
                left_pad=False,  # right padding
                truncation=self.config.get('truncation', 'right'))  # truncate from the right

            rm_input_ids.append(input_ids)
            rm_attention_mask.append(attention_mask)

        rm_input_ids = torch.cat(rm_input_ids, dim=0)
        rm_attention_mask = torch.cat(rm_attention_mask, dim=0)

        rm_position_ids = compute_position_id_with_mask(rm_attention_mask)

        rm_inputs = {'input_ids': rm_input_ids, 'attention_mask': rm_attention_mask, 'position_ids': rm_position_ids}

        return DataProto.from_dict(rm_inputs)

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_rm_score(self, data: DataProto):
        import itertools

        from verl.utils.seqlen_balancing import get_reverse_idx, rearrange_micro_batches
        data = data.to('cuda')
        if self._do_switch_chat_template:
            rm_data = self._switch_chat_template(data)

        rm_data.batch = rm_data.batch.cuda()

        # perform forward computation
        with self.ulysses_sharding_manager:
            rm_data = self.ulysses_sharding_manager.preprocess_data(data=rm_data)
            data = self.ulysses_sharding_manager.preprocess_data(data=data)

            use_dynamic_bsz = self.config.use_dynamic_bsz
            if use_dynamic_bsz:
                max_token_len = self.config.forward_max_token_len_per_gpu * self.ulysses_sequence_parallel_size
                micro_batches, indices = rearrange_micro_batches(batch=rm_data.batch, max_token_len=max_token_len)
            else:
                micro_batches = rm_data.batch.split(self.config.micro_batch_size_per_gpu)
            output = []
            for micro_batch in micro_batches:
                rm_score = self._forward_micro_batch(micro_batch)
                output.append(rm_score)
            scores = torch.cat(output, dim=0)  # (batch_size)

            if use_dynamic_bsz:
                indices = list(itertools.chain.from_iterable(indices))
                assert len(indices) == scores.size(0), f"{len(indices)} vs. {scores.size()}"
                revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
                scores = scores[revert_indices]

            token_level_scores = self._expand_to_token_level(data, scores)
            # Note that this is only the scores, may not be the final rewards used to train RL
            output = DataProto.from_dict(tensors={'rm_scores': token_level_scores})
            output = self.ulysses_sharding_manager.postprocess_data(data=output)

        # https://pytorch.org/docs/stable/notes/fsdp.html#fsdp-notes
        # unshard the root FSDP module
        self.reward_module._handle.reshard(True)

        output = output.to('cpu')
        torch.cuda.empty_cache()
        return output


class ProcessRewardModelWorker(Worker):
    """
    Note that we only implement the process reward model that is subclass of AutoModelForTokenClassification.
    """

    def __init__(self, config):
        super().__init__()
        import torch.distributed
        if not torch.distributed.is_initialized():
            torch.distributed.init_process_group(backend="nccl")
        self.config = config

        # build device mesh for Ulysses Sequence Parallel
        world_size = torch.distributed.get_world_size()
        from torch.distributed.device_mesh import init_device_mesh

        fsdp_size = self.config.model.fsdp_config.fsdp_size
        self.device_mesh = create_device_mesh(world_size=world_size, fsdp_size=fsdp_size)

        self.ulysses_device_mesh = None
        self.ulysses_sequence_parallel_size = self.config.get('ulysses_sequence_parallel_size', 1)
        dp = world_size // self.ulysses_sequence_parallel_size
        if self.ulysses_sequence_parallel_size > 1:
            self.ulysses_device_mesh = init_device_mesh('cuda',
                                                        mesh_shape=(dp, self.ulysses_sequence_parallel_size),
                                                        mesh_dim_names=['dp', 'sp'])

        self.ulysses_sharding_manager = FSDPUlyssesShardingManager(self.ulysses_device_mesh)

        self.use_remove_padding = self.config.model.get('use_remove_padding', False)
        self._is_offload_param = self.config.model.fsdp_config.param_offload
        self._is_offload_optimizer = self.config.model.fsdp_config.optimizer_offload

        credit_assignment = self.config.get('credit_assignment', 0.1)
        if credit_assignment in ['gamma-decay', 'strict min-form']:
            self.disable_approx_min_form_credit_assignment = True
        else:
            self.disable_approx_min_form_credit_assignment = False
            self.temperature = credit_assignment

        # TODO: online training of PRM
        assert not self.config.training, "Not support yet."
        # normalize config
        # self.config.ppo_mini_batch_size //= (torch.distributed.get_world_size() // self.ulysses_sequence_parallel_size)

    def _build_prm_optimizer(self, config):
        from torch import optim
        from torch.distributed.fsdp import CPUOffload
        from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
        from torch.distributed.fsdp import ShardingStrategy
        from transformers import AutoConfig, AutoModelForTokenClassification

        from verl.utils.model import print_model_size

        trust_remote_code = config.model.get('trust_remote_code', False)
        local_path = copy_to_local(config.model.path)
        self.tokenizer = hf_tokenizer(local_path, trust_remote_code=trust_remote_code)

        torch_dtype = torch.bfloat16
        model_config = AutoConfig.from_pretrained(local_path, trust_remote_code=trust_remote_code)
        model_config.num_labels = 2

        use_remove_padding = config.model.get('use_remove_padding', False)
        if use_remove_padding:
            from verl.models.registry import check_model_support_rmpad
            check_model_support_rmpad(model_config.model_type)

        if use_remove_padding and self.ulysses_sequence_parallel_size > 1:
            from verl.models.transformers.monkey_patch import apply_monkey_patch
            apply_monkey_patch(model_config, verbose=True)

        init_context = get_init_weight_context_manager(use_meta_tensor=not model_config.tie_word_embeddings)
        
        with init_context(), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            setattr(model_config, 'classifier_dropout', 0.)
            process_reward_module = AutoModelForTokenClassification.from_pretrained(
                pretrained_model_name_or_path=local_path,
                torch_dtype=torch_dtype,
                config=model_config,
                attn_implementation='flash_attention_2',
                trust_remote_code=trust_remote_code,
            )

            # some parameters may not in torch_dtype
            process_reward_module.to(torch_dtype)

            # if config.model.get('enable_gradient_checkpointing', False):
            #     process_reward_module.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
        
        if self.rank == 0:
            print_model_size(process_reward_module)

        fsdp_config = self.config.model.fsdp_config
        auto_wrap_policy = get_fsdp_wrap_policy(
            module=process_reward_module, 
            config=fsdp_config.get('wrap_policy', None),
        )

        sharding_strategy = get_sharding_strategy(self.device_mesh)

        process_reward_module = FSDP(
            process_reward_module,
            param_init_fn=init_fn,
            use_orig_params=False,
            auto_wrap_policy=auto_wrap_policy,
            device_id=torch.cuda.current_device(),
            sharding_strategy=sharding_strategy,
            sync_module_states=True,
            forward_prefetch=False,
            device_mesh=self.device_mesh,
            cpu_offload=CPUOffload(offload_params=True),
        )

        log_gpu_memory_usage('After PRM FSDP', logger=None)

        if self.config.training:
            prm_optimizer = optim.AdamW(
                process_reward_module.parameters(),
                lr=config.optim.lr,
                betas=config.optim.get('betas', (0.9, 0.999)),
                weight_decay=config.optim.get('weight_decay', 1e-2),
            )

            total_steps = config.optim.get('total_training_steps', 0)
            num_warmup_steps_ratio = config.optim.get('lr_warmup_steps_ratio', 0.)
            num_warmup_steps = int(num_warmup_steps_ratio * total_steps)

            print(f'Total steps: {total_steps}, num_warmup_steps: {num_warmup_steps}')

            from verl.utils.torch_functional import get_constant_schedule_with_warmup
            prm_lr_scheduler = get_constant_schedule_with_warmup(
                optimizer=prm_optimizer,
                num_warmup_steps=num_warmup_steps,
            )

            return process_reward_module, prm_optimizer, prm_lr_scheduler
        return process_reward_module

    def _init_separator(self, config):
        # split response into steps based on what character
        split_step_char = config.get('split_step_char', '\n\n')
        self.split_step_tokens = []
        # all tokens which end with "\n\n"
        for i in range(len(self.tokenizer)):
            if self.tokenizer.decode(i).endswith(split_step_char):
                self.split_step_tokens.append(i)
        self.split_step_tokens = torch.LongTensor(
            self.split_step_tokens, 
        ).to(device=torch.cuda.current_device())

        # token for reward prediction
        step_separator = config.get('step_separator', '\n')
        self.step_separator_token = self.tokenizer.encode(
            step_separator, 
            return_tensors='pt',
            add_special_tokens=False,
        ).squeeze(0).to(device=torch.cuda.current_device())
    
    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        import_external_libs(self.config.model.get('external_lib', None))

        results = self._build_prm_optimizer(self.config)
        if self.config.training:
            self.process_reward_module, self.prm_optimizer, self.prm_lr_scheduler = results
        else:
            self.process_reward_module = results
        
        self._init_separator(self.config)

        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.process_reward_module)
        if self.config.training and self._is_offload_optimizer:
            offload_fsdp_optimizer(optimizer=self.prm_optimizer)

        if self.config.training:
            # initialize:
            # DataParallel: forward, loss, backward, optim.step
            # CheckpointManager: save, load ckpt
            raise NotImplementedError

        torch.cuda.empty_cache()

    def _split_steps(self, data):
        bs, problem_length = data.batch['prompts'].size()
        action_mask = data.batch['attention_mask'][:, problem_length:]
        num_actions = action_mask.size(1)
        solution_tokens = data.batch['responses']

        # find step separator, typically '\n\n'
        row_ids, column_ids = torch.where(
            torch.isin(solution_tokens, self.split_step_tokens)
        )
        # +1 for the last step with eos instead of step separator
        max_num_steps = max(
            [column_ids[row_ids==i].numel() for i in range(bs)]) + 1
        # end index of each step, shape: (B, max_num_steps), type: long
        score_ids = torch.full(
            (bs, max_num_steps), -1, dtype=torch.long, 
            device=torch.cuda.current_device(),
        )
        # whether end of step, shape: (B, max_response_tokens), type: bool
        reward_mask = torch.zeros_like(solution_tokens, dtype=torch.bool)
        eos_indices = num_actions - 1 - action_mask.long().fliplr().argmax(1)
        for j in range(bs):
            step_separators_per_data = column_ids[row_ids==j]
            num_intermediate_steps = step_separators_per_data.numel()
            # intermediate steps
            score_ids[j, :num_intermediate_steps] = step_separators_per_data
            reward_mask[j, step_separators_per_data] = True
            # last step
            score_ids[j, num_intermediate_steps] = eos_indices[j]
            reward_mask[j, eos_indices[j]] = True
        
        score_mask = score_ids != -1
        # score_ids, score_mask, reward_mask for data.batch['responses'],
        # not for data.batch['input_ids']
        output = dict(
            score_ids=score_ids,
            score_mask=score_mask,
            reward_mask=reward_mask,
            num_steps=score_mask.float().sum(dim=-1),
        )
        return DataProto.from_dict(tensors=output)
    
    def _build_inputs_for_prm(self, data):
        from torch.nn.utils.rnn import pad_sequence
        from torch.nn import functional as F

        # fetch var
        problem_ids = data.batch['prompts']
        attention_mask = data.batch['attention_mask']
        solution_tokens = data.batch['responses']
        score_ids = data.batch['score_ids']
        score_mask = data.batch['score_mask']
        bs, problem_length = problem_ids.shape
        total_length = data.batch['input_ids'].size(-1)
        problem_attn_mask = attention_mask[:, :problem_length]
        solution_attn_mask = attention_mask[:, problem_length:]
        device = problem_ids.device

        # build input_ids, attn_mask, and position_ids for PRM
        # (optional) remove '\n\n' at the end of each step, 
        # then add '\n' for each step to predict process reward
        input_ids = []
        attn_mask = []
        for i in range(bs):
            input_ids_per_data = problem_ids[i]
            attn_mask_per_data = problem_attn_mask[i]
            # split tokens of each step
            for idx, j in enumerate(score_ids[i][score_mask[i]]):
                # j -> '\n\n'
                if idx == 0:
                    start_idx = 0
                else:
                    start_idx = score_ids[i, idx - 1] + 1
                # slicer [..., :j] means drop the last '\n\n' of each step
                step_tokens = solution_tokens[i, start_idx:j]
                step_attn_mask = solution_attn_mask[i, start_idx:j]
                # add '\n' after each step to predict process reward
                input_ids_per_data = torch.cat(
                    (input_ids_per_data, step_tokens, self.step_separator_token)
                )
                attn_mask_per_data = torch.cat(
                    (attn_mask_per_data, step_attn_mask, torch.ones(
                        1, device=device, dtype=attn_mask_per_data.dtype
                    ))
                )
            input_ids.append(input_ids_per_data)
            attn_mask.append(attn_mask_per_data)
        # gather into batch
        input_ids = pad_sequence(input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id)
        attn_mask = pad_sequence(attn_mask, batch_first=True, padding_value=0)
        # pad to total_length at dim=1
        input_ids = F.pad(input_ids, (0, total_length - input_ids.size(-1)), value=self.tokenizer.pad_token_id)
        attn_mask = F.pad(attn_mask, (0, total_length - attn_mask.size(-1)), value=0)
        position_ids = compute_position_id_with_mask(attn_mask)

        # for forward of PRM
        output = dict(
            input_ids=input_ids,
            attention_mask=attn_mask,
            position_ids=position_ids,
        )
        # for adv baseline, rather than forward of PRM
        output = DataProto.from_dict(tensors=output)
        return output

    def _forward_micro_batch(self, micro_batch):
        from flash_attn.bert_padding import (
            index_first_axis,
            pad_input,
            rearrange,
            unpad_input,
        )

        from verl.utils.ulysses import (
            gather_outpus_and_unpad,
            ulysses_pad_and_slice_inputs,
        )
        
        response_length = micro_batch['responses'].size(-1)

        with torch.no_grad(), torch.autocast(device_type='cuda', dtype=torch.bfloat16):
            input_ids = micro_batch['input_ids']
            batch, seqlen = input_ids.shape
            attention_mask = micro_batch['attention_mask']
            position_ids = micro_batch['position_ids']
            reward_mask = micro_batch['reward_mask']

            if self.use_remove_padding:
                input_ids_rmpad, indices, *_ = unpad_input(input_ids.unsqueeze(-1),
                                                           attention_mask)  # input_ids_rmpad (total_nnz, ...)
                input_ids_rmpad = input_ids_rmpad.transpose(0, 1)  # (1, total_nnz)

                # unpad the position_ids to align the rotary
                position_ids_rmpad = index_first_axis(rearrange(position_ids.unsqueeze(-1), "b s ... -> (b s) ..."),
                                                      indices).transpose(0, 1)

                # pad and slice the inputs if sp > 1
                if self.ulysses_sequence_parallel_size > 1:
                    input_ids_rmpad, position_ids_rmpad, pad_size = ulysses_pad_and_slice_inputs(
                        input_ids_rmpad,
                        position_ids_rmpad,
                        sp_size=self.ulysses_sequence_parallel_size,
                    )

                # only pass input_ids and position_ids to enable flash_attn_varlen
                output = self.process_reward_module(
                    input_ids=input_ids_rmpad,
                    attention_mask=None,
                    position_ids=position_ids_rmpad,
                    use_cache=False,
                )  # prevent model thinks we are generating
                reward_rmpad = output.logits
                reward_rmpad = reward_rmpad.squeeze(0)  # (total_nnz)

                # gather output if sp > 1
                if self.ulysses_sequence_parallel_size > 1:
                    reward_rmpad = gather_outpus_and_unpad(reward_rmpad,
                                                           gather_dim=0,
                                                           unpad_dim=0,
                                                           padding_size=pad_size)

                # pad it back
                rm_score = pad_input(reward_rmpad, indices=indices, batch=batch, seqlen=seqlen).squeeze(-1)
            else:
                output = self.process_reward_module(input_ids=input_ids,
                                                    attention_mask=attention_mask,
                                                    position_ids=position_ids)
                rm_score = output.logits  # (batch_size, seq_len, 2)
        
        rm_score = rm_score[:, -response_length:]
        rm_score = rm_score.softmax(dim=-1)
        rm_score = (rm_score[..., 1] - rm_score[..., 0]) * reward_mask  # (batch_size, seq_len)
        
        if not self.disable_approx_min_form_credit_assignment:
            weight = torch.softmax(
                -rm_score.masked_fill(
                    ~reward_mask, float('inf')
                ) / self.temperature,
                dim=-1,
            )
            rm_score *= weight
        
        return rm_score

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_rm_score(self, data:DataProto):
        import itertools

        from verl.utils.seqlen_balancing import get_reverse_idx, rearrange_micro_batches

        data = data.to('cuda')
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.process_reward_module)

        data.union(self._split_steps(data))
        prm_data = self._build_inputs_for_prm(data)
        prm_data = prm_data.to('cuda')
        prm_data.union(data.select(batch_keys=['reward_mask', 'responses']))
        
        with self.ulysses_sharding_manager:
            prm_data = self.ulysses_sharding_manager.preprocess_data(data=prm_data)

            self.process_reward_module.eval()
            batch = prm_data.batch

            use_dynamic_bsz = self.config.use_dynamic_bsz
            if use_dynamic_bsz:
                max_token_len = self.config.forward_max_token_len_per_gpu * self.ulysses_sequence_parallel_size
                micro_batches, indices = rearrange_micro_batches(batch=batch, max_token_len=max_token_len)
            else:
                micro_batches = batch.split(self.config.micro_batch_size_per_gpu)
            
            output = []
            for micro_batch in micro_batches:
                rm_score = self._forward_micro_batch(micro_batch)
                output.append(rm_score)
            token_level_scores = torch.cat(output, dim=0)
            
            if use_dynamic_bsz:
                indices = list(itertools.chain.from_iterable(indices))
                assert len(indices) == token_level_scores.size(0), f"{len(indices)} vs. {token_level_scores.size()}"
                revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
                token_level_scores = token_level_scores[revert_indices]

            output = DataProto.from_dict(tensors={'rm_scores': token_level_scores})
            output = self.ulysses_sharding_manager.postprocess_data(data=output)

        # https://pytorch.org/docs/stable/notes/fsdp.html#fsdp-notes
        # unshard the root FSDP module
        if self.world_size > 1:
            self.process_reward_module._handle.reshard(True)
        
        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.process_reward_module)

        output = output.to('cpu')
        torch.cuda.empty_cache()
        return output


class LLMJudgeProcessRewardWorker(Worker):
    """
    LLM-as-a-Judge Process Reward Worker that uses an LLM to evaluate process rewards.
    This worker uses a generative LLM to judge the quality of reasoning steps.
    """

    def __init__(self, config):
        super().__init__()
        if not torch.distributed.is_initialized():
            torch.distributed.init_process_group(backend="nccl")
        self.config = config
        
        # build device mesh for Ulysses Sequence Parallel
        world_size = torch.distributed.get_world_size()
        # Note: self.world_size and self.rank are properties from the base Worker class
        from torch.distributed.device_mesh import init_device_mesh

        fsdp_size = self.config.model.fsdp_config.fsdp_size
        self.device_mesh = create_device_mesh(world_size=world_size, fsdp_size=fsdp_size)

        self.ulysses_device_mesh = None
        self.ulysses_sequence_parallel_size = self.config.get('ulysses_sequence_parallel_size', 1)
        dp = world_size // self.ulysses_sequence_parallel_size
        if self.ulysses_sequence_parallel_size > 1:
            self.ulysses_device_mesh = init_device_mesh('cuda',
                                                        mesh_shape=(dp, self.ulysses_sequence_parallel_size),
                                                        mesh_dim_names=['dp', 'sp'])

        self.ulysses_sharding_manager = FSDPUlyssesShardingManager(self.ulysses_device_mesh)

        self.use_remove_padding = self.config.model.get('use_remove_padding', False)
        self._is_offload_param = self.config.model.fsdp_config.param_offload
        self._is_offload_optimizer = self.config.model.fsdp_config.optimizer_offload


        # Judge prompt template
        self.judge_prompt_template = self.config.get('judge_prompt_template', None)
        if self.judge_prompt_template is None:
            # Default prompt template for judging reasoning steps
            self.judge_prompt_template = """You are an expert evaluator. Please evaluate the quality of the following reasoning step in solving a mathematical problem.

Problem: {problem}

Previous steps:
{previous_steps}

Current step being evaluated:
{current_step}

Please rate this step on a scale from 0 to 1, where:
- 0: Completely incorrect or harmful to the solution
- 1: Perfectly correct and helpful for solving the problem

Consider:
- Mathematical accuracy
- Logical consistency with previous steps
- Progress towards the solution
- Clarity of reasoning

Please provide your evaluation and place your final score in \\boxed{{}} format. For example: \\boxed{{0.8}}"""

        # Set micro batch size to 1 to prevent OOM as requested
        self.judge_micro_batch_size = (torch.distributed.get_world_size() // self.ulysses_sequence_parallel_size)

    def _build_judge_model(self, config):
        """Build the LLM model used for judging"""
        from torch.distributed.fsdp import CPUOffload
        from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
        from transformers import AutoConfig, AutoModelForCausalLM

        from verl.utils.model import print_model_size

        trust_remote_code = config.model.get('trust_remote_code', False)
        local_path = copy_to_local(config.model.path)
        self.tokenizer = hf_tokenizer(local_path, trust_remote_code=trust_remote_code)

        torch_dtype = torch.bfloat16
        model_config = AutoConfig.from_pretrained(local_path, trust_remote_code=trust_remote_code)

        use_remove_padding = config.model.get('use_remove_padding', False)
        if use_remove_padding:
            from verl.models.registry import check_model_support_rmpad
            check_model_support_rmpad(model_config.model_type)

        if use_remove_padding and self.ulysses_sequence_parallel_size > 1:
            from verl.models.transformers.monkey_patch import apply_monkey_patch
            apply_monkey_patch(model_config, verbose=True)

        init_context = get_init_weight_context_manager(use_meta_tensor=not model_config.tie_word_embeddings)
        
        with init_context(), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            judge_model = AutoModelForCausalLM.from_pretrained(
                pretrained_model_name_or_path=local_path,
                torch_dtype=torch_dtype,
                config=model_config,
                attn_implementation='flash_attention_2',
                trust_remote_code=trust_remote_code,
            )
            judge_model.to(torch_dtype)

        torch.distributed.barrier()
        
        if self.rank == 0:
            print_model_size(judge_model)

        fsdp_config = self.config.model.fsdp_config


        sharding_strategy = get_sharding_strategy(self.device_mesh)

        judge_model = FSDP(
            judge_model,
            param_init_fn=init_fn,
            use_orig_params=False,
            auto_wrap_policy=None,  # HFrollout
            device_id=torch.cuda.current_device(),
            sharding_strategy=sharding_strategy,
            sync_module_states=True,
            forward_prefetch=False,
            device_mesh=self.device_mesh,
            cpu_offload=CPUOffload(offload_params=True),
        )

        log_gpu_memory_usage('After Judge LLM FSDP', logger=None)
        return judge_model

    def _init_separator(self, config):
        """Initialize step separators for splitting reasoning steps"""
        # split response into steps based on what character
        split_step_char = config.get('split_step_char', '\n\n')
        self.split_step_tokens = []
        # all tokens which end with "\n\n"
        for i in range(len(self.tokenizer)):
            if self.tokenizer.decode(i).endswith(split_step_char):
                self.split_step_tokens.append(i)
        self.split_step_tokens = torch.LongTensor(
            self.split_step_tokens, 
        ).to(device=torch.cuda.current_device())

        # token for reward prediction
        step_separator = config.get('step_separator', '\n')
        self.step_separator_token = self.tokenizer.encode(
            step_separator, 
            return_tensors='pt',
            add_special_tokens=False,
        ).squeeze(0).to(device=torch.cuda.current_device())
    
    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        """Initialize the judge model"""
        import_external_libs(self.config.model.get('external_lib', None))
        
        self.judge_model = self._build_judge_model(self.config)

        from verl.workers.rollout import HFRollout
        from verl.workers.sharding_manager import BaseShardingManager
        self.rollout = HFRollout(module=self.judge_model, config=self.config.rollout)
        self.rollout_sharding_manager = BaseShardingManager()


        self._init_separator(self.config)

        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.judge_model)

        torch.cuda.empty_cache()

    def _split_steps(self, data):
        """Split responses into reasoning steps"""
        bs, problem_length = data.batch['prompts'].size()
        action_mask = data.batch['attention_mask'][:, problem_length:]
        num_actions = action_mask.size(1)
        solution_tokens = data.batch['responses']

        # find step separator, typically '\n\n'
        row_ids, column_ids = torch.where(
            torch.isin(solution_tokens, self.split_step_tokens)
        )
        # +1 for the last step with eos instead of step separator
        max_num_steps = max(
            [column_ids[row_ids==i].numel() for i in range(bs)]) + 1
        # end index of each step, shape: (B, max_num_steps), type: long
        score_ids = torch.full(
            (bs, max_num_steps), -1, dtype=torch.long, 
            device=torch.cuda.current_device(),
        )
        # whether end of step, shape: (B, max_response_tokens), type: bool
        reward_mask = torch.zeros_like(solution_tokens, dtype=torch.bool)
        eos_indices = num_actions - 1 - action_mask.long().fliplr().argmax(1)
        
        for j in range(bs):
            step_separators_per_data = column_ids[row_ids==j]
            num_intermediate_steps = step_separators_per_data.numel()
            # intermediate steps
            score_ids[j, :num_intermediate_steps] = step_separators_per_data
            reward_mask[j, step_separators_per_data] = True
            # last step
            score_ids[j, num_intermediate_steps] = eos_indices[j]
            reward_mask[j, eos_indices[j]] = True
        
        score_mask = score_ids != -1
        output = dict(
            score_ids=score_ids,
            score_mask=score_mask,
            reward_mask=reward_mask,
            num_steps=score_mask.float().sum(dim=-1),
        )
        return DataProto.from_dict(tensors=output)
    
    def _extract_score_from_response(self, response_text):
        """Extract numerical score from LLM judge response"""
        import re
        
        # First try to extract from \boxed{} format
        boxed_pattern = r'\\boxed\{([^}]+)\}'
        boxed_matches = re.findall(boxed_pattern, response_text)
        
        if boxed_matches:
            try:
                # Try to parse the content inside \boxed{}
                boxed_content = boxed_matches[-1].strip()  # Take the last match
                score = float(boxed_content)
                return max(0.0, min(1.0, score))  # Clamp to [0, 1]
            except ValueError:
                # If the boxed content is not a valid number, continue to fallback
                pass
        
        # Fallback: Try to extract a decimal number between 0 and 1
        # Look for decimal numbers first (more specific), then integers
        decimal_pattern = r'\b(0\.\d+|1\.0)\b'
        decimal_matches = re.findall(decimal_pattern, response_text)
        
        if decimal_matches:
            try:
                score = float(decimal_matches[-1])  # Take the last match
                return max(0.0, min(1.0, score))  # Clamp to [0, 1]
            except ValueError:
                pass
        
        # If no decimal found, look for integers 0 or 1
        integer_pattern = r'\b(0|1)\b'
        integer_matches = re.findall(integer_pattern, response_text)
        
        if integer_matches:
            try:
                score = float(integer_matches[-1])  # Take the last match
                return max(0.0, min(1.0, score))  # Clamp to [0, 1]
            except ValueError:
                pass
        
        # Default score if parsing fails
        return 0.5

    def _llm_judge_step(self, problem_text, previous_steps_text, current_step_text):
        """Use LLM to judge a single reasoning step"""

        # Format the prompt
        prompt = self.judge_prompt_template.format(
            problem=problem_text,
            previous_steps=previous_steps_text,
            current_step=current_step_text
        )
        
        # Prepare input for the model
        messages = [{'role': 'user', 'content': prompt}]
        input_text = self.tokenizer.apply_chat_template(
            messages, 
            add_generation_prompt=True, 
            tokenize=False
        )
        
        # Tokenize
        inputs = self.tokenizer(
            input_text,
            return_tensors="pt",
            truncation=True,
            max_length=self.config.get('max_judge_input_length', 2048)
        )
        # import pdb;pdb.set_trace()
        prompts = DataProto.from_single_dict({
            "input_ids": inputs['input_ids'],
            "attention_mask": inputs['attention_mask'],
            "position_ids": inputs.get('position_ids', None) if 'position_ids' in inputs else None,
            "raw_prompts": messages, 
        }
             
        )
        
        # # 确保所有输入张量都在GPU上
        # for key in inputs:
        #     if isinstance(inputs[key], torch.Tensor):
        #         inputs[key] = inputs[key].cuda()

        # 确保模型参数已经正确加载到GPU - 关键修复

        # if self._is_offload_param:
        #     # 如果模型被offload，确保它被正确加载到GPU
        #     load_fsdp_model_to_gpu(self.judge_model)
        
        # # 确保模型在评估模式
        # self.judge_model.eval()
        with self.rollout_sharding_manager:

            # after parameters sync with rollout, offload actor model to CPU
            if self._is_offload_param:
                offload_fsdp_model_to_cpu(self.judge_model)

            log_gpu_memory_usage('After entering PRM rollout sharding manager', logger=logger)

            prompts = self.rollout_sharding_manager.preprocess_data(prompts)
            import pdb;pdb.set_trace()
        
            output = self.rollout.generate_sequences(prompts=prompts)

            log_gpu_memory_usage('After rollout generation', logger=logger)

            output = self.rollout_sharding_manager.postprocess_data(output)

        # # Generate response
        # with torch.no_grad(), torch.autocast(device_type='cuda', dtype=torch.bfloat16):
        #     import pdb;pdb.set_trace()
        #     outputs = self.judge_model.generate(
        #         **inputs,
        #         max_new_tokens=self.config.get('max_judge_output_length', 50),
        #         temperature=0.1,  # Low temperature for consistent scoring
        #         do_sample=True,
        #         pad_token_id=self.tokenizer.pad_token_id,
        #         eos_token_id=self.tokenizer.eos_token_id,
        #         # synced_gpus=False,  # 关键修复：禁用GPU同步，避免分布式通信问题
        #         use_cache=True,
        #     )
        
        # # Decode response
        # response_ids = outputs[0][inputs['input_ids'].shape[1]:]
        # response_text = self.tokenizer.decode(response_ids, skip_special_tokens=True)
        
        # Extract score
        score = self._extract_score_from_response(response_text)
        return score

    def _judge_all_steps(self, data):
        """Judge all steps for all samples in the batch"""
        bs = data.batch['prompts'].size(0)
        solution_tokens = data.batch['responses']
        score_ids = data.batch['score_ids']
        score_mask = data.batch['score_mask']
        reward_mask = data.batch['reward_mask']
        
        # Initialize scores tensor
        step_scores = torch.zeros_like(reward_mask, dtype=torch.float32, device=reward_mask.device)
        
        for batch_idx in range(bs):
            # Extract problem text
            problem_ids = data.batch['prompts'][batch_idx]
            problem_mask = data.batch['attention_mask'][batch_idx][:len(problem_ids)]
            valid_problem_ids = problem_ids[problem_mask.bool()]
            problem_text = self.tokenizer.decode(valid_problem_ids, skip_special_tokens=True)
            
            # Process each step
            previous_steps_text = ""
            valid_scores = score_ids[batch_idx][score_mask[batch_idx]]
            
            for step_idx, step_end_pos in enumerate(valid_scores):
                # Extract current step
                if step_idx == 0:
                    start_pos = 0
                else:
                    start_pos = valid_scores[step_idx - 1] + 1
                
                current_step_ids = solution_tokens[batch_idx, start_pos:step_end_pos]
                current_step_text = self.tokenizer.decode(current_step_ids, skip_special_tokens=True)
                
                # Judge the current step
                score = self._llm_judge_step(problem_text, previous_steps_text, current_step_text)
                
                # Assign score to the step end position
                step_scores[batch_idx, step_end_pos] = score
                
                # Update previous steps for next iteration
                if previous_steps_text:
                    previous_steps_text += "\n" + current_step_text
                else:
                    previous_steps_text = current_step_text
        
        return step_scores

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_rm_score(self, data: DataProto):
        """Main method to compute process rewards using LLM judge"""
        from verl.utils.seqlen_balancing import get_reverse_idx, rearrange_micro_batches

        data = data.to('cuda')
        if self._is_offload_param:
            load_fsdp_model_to_gpu(self.judge_model)

        # Split data into steps
        data.union(self._split_steps(data))


        with self.ulysses_sharding_manager:
            data = self.ulysses_sharding_manager.preprocess_data(data=data)
            self.judge_model.eval()
            
            use_dynamic_bsz = self.config.use_dynamic_bsz
            if use_dynamic_bsz:
                max_token_len = self.config.forward_max_token_len_per_gpu * self.ulysses_sequence_parallel_size
                micro_batches, indices = rearrange_micro_batches(batch=data.batch, max_token_len=max_token_len)
            else:
                micro_batches = data.batch.split(self.config.micro_batch_size_per_gpu)
            

            token_level_scores_list = []
            
            for micro_batch in micro_batches:
                # Create DataProto with single sample
                micro_data = DataProto.from_dict(micro_batch)
                
                # Judge all steps for this sample
                step_scores = self._judge_all_steps(micro_data)
                token_level_scores_list.append(step_scores)
            
            # Concatenate all results
            token_level_scores = torch.cat(token_level_scores_list, dim=0)


            if use_dynamic_bsz:
                indices = list(itertools.chain.from_iterable(indices))
                assert len(indices) == token_level_scores.size(0), f"{len(indices)} vs. {token_level_scores.size()}"
                revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
                token_level_scores = token_level_scores[revert_indices]
            
            # Apply credit assignment if configured
            if not self.disable_approx_min_form_credit_assignment:
                reward_mask = data.batch['reward_mask']
                weight = torch.softmax(
                    -token_level_scores.masked_fill(
                        ~reward_mask, float('inf')
                    ) / self.temperature,
                    dim=-1,
                )
                token_level_scores *= weight

            output = DataProto.from_dict(tensors={'rm_scores': token_level_scores})
            output = self.ulysses_sharding_manager.postprocess_data(data=output)

        # Unshard the root FSDP module
        if self.world_size > 1:
            self.judge_model._handle.reshard(True)
        
        if self._is_offload_param:
            offload_fsdp_model_to_cpu(self.judge_model)

        output = output.to('cpu')
        torch.cuda.empty_cache()
        return output


class RemoteLLMJudgeWorker(Worker):
    """
    Remote LLM-as-a-Judge Process Reward Worker that uses remote API calls
    instead of local model inference. This is more efficient and scalable.
    """

    def __init__(self, config):
        super().__init__()
        if not torch.distributed.is_initialized():
            torch.distributed.init_process_group(backend="nccl")
        
        self.config = config
        
        # Remote inference configuration
        self.api_base_url = config.llm_as_judge_api.get('api_base_url', 'http://127.0.0.1:8000')
        self.model_name = config.llm_as_judge_api.get('model_name', 'judge-model')
        self.max_judge_output_length = config.llm_as_judge_api.get('max_judge_output_length', 100)
        self.temperature = config.llm_as_judge_api.get('temperature', 0.6)
        self.request_timeout = config.llm_as_judge_api.get('request_timeout', 30)
        self.max_retries = config.llm_as_judge_api.get('max_retries', 3)
        self.retry_delay = config.llm_as_judge_api.get('retry_delay', 1.0)

        self.top_p = config.llm_as_judge_api.get('top_p', 0.9)
        self.top_k = config.llm_as_judge_api.get('top_k', 50)
        self.min_p = config.llm_as_judge_api.get('min_p', 0.01)
        self.repetition_penalty = config.llm_as_judge_api.get('repetition_penalty', 1.0)
        self.frequency_penalty = config.llm_as_judge_api.get('frequency_penalty', 0.0)
        self.length_penalty = config.llm_as_judge_api.get('length_penalty', 1.0)
        
        # Judge prompt template
        self.judge_prompt_template = config.llm_as_judge_api.get('judge_prompt_template', None)
        if self.judge_prompt_template is None:
            self.judge_prompt_template = """Please evaluate the quality of the following reasoning step in solving the problem.

[Problem]
{problem}

[Previous steps]
{previous_steps}

[Current step being evaluated]
{current_step}

Please rate this step on a scale from 0 to 1, where:
- 0: Completely incorrect or harmful to the solution
- 1: Perfectly correct and helpful for solving the problem

Consider:
- Mathematical accuracy
- Logical consistency with previous steps
- Progress towards the solution

Please provide your evaluation and place your final score in \\boxed{{}} format. For example: \\boxed{{0.87543}}"""

        # Step separation configuration
        self.split_step_char = config.llm_as_judge_api.get('split_step_char', '\n\n')
        self.step_separator = config.llm_as_judge_api.get('step_separator', '\n')
        
        # Credit assignment configuration
        self.disable_approx_min_form_credit_assignment = config.get('disable_approx_min_form_credit_assignment', False)
        self.credit_assignment_temperature = config.get('credit_assignment_temperature', 1.0)
        
        # Initialize tokenizer for text processing
        self._init_tokenizer()
        
        # Initialize HTTP session for connection pooling
        import requests
        self.session = requests.Session()
        
        # Set up request headers
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'RemoteLLMJudgeWorker/1.0'
        })

    def _init_tokenizer(self):
        """Initialize tokenizer for text processing"""
        from verl.utils import hf_tokenizer
        from verl.utils.fs import copy_to_local
        
        # Use a simple tokenizer for text processing (could be same as main model or a fast tokenizer)
        tokenizer_path = self.config.llm_as_judge_api.get('tokenizer_path', "Qwen/Qwen2.5-1.5B-Instruct")
        trust_remote_code = self.config.llm_as_judge_api.get('trust_remote_code', True)
        
        if os.path.exists(tokenizer_path):
            local_path = copy_to_local(tokenizer_path)
        else:
            local_path = tokenizer_path  # Assume it's a model name from HF hub
            
        self.tokenizer = hf_tokenizer(local_path, trust_remote_code=trust_remote_code)
        self.eos_token = self.tokenizer.decode(self.tokenizer.eos_token_id, skip_special_tokens=False)
        
        # Initialize step separator tokens for splitting
        self.split_step_tokens = []
        for i in range(len(self.tokenizer)):
            if self.tokenizer.decode(i).endswith(self.split_step_char):
                self.split_step_tokens.append(i)
        self.split_step_tokens = torch.LongTensor(self.split_step_tokens)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        """Initialize the remote connection (no local model needed)"""
        # Test connection to remote server
        self._test_connection()
        
        # Initialize separator tokens on GPU if needed
        if torch.cuda.is_available():
            self.split_step_tokens = self.split_step_tokens.cuda()

    def _test_connection(self):
        """Test connection to remote API server"""
        import requests
        import time
        
        try:
            health_url = f"{self.api_base_url}/health"
            response = self.session.get(health_url, timeout=5)
            
            if response.status_code == 200:
                logger.info(f"Successfully connected to remote LLM server at {self.api_base_url}")
            else:
                logger.warning(f"Remote server returned status {response.status_code}")
                
        except requests.RequestException as e:
            logger.error(f"Failed to connect to remote LLM server: {e}")
            raise RuntimeError(f"Cannot connect to remote LLM server at {self.api_base_url}")

    def _split_steps(self, data):
        """Split responses into reasoning steps (same as original implementation)"""
        bs, problem_length = data.batch['prompts'].size()
        action_mask = data.batch['attention_mask'][:, problem_length:]
        num_actions = action_mask.size(1)
        solution_tokens = data.batch['responses']

        # Find step separator, typically '\n\n'
        row_ids, column_ids = torch.where(
            torch.isin(solution_tokens, self.split_step_tokens)
        )
        
        # +1 for the last step with eos instead of step separator
        max_num_steps = max([column_ids[row_ids==i].numel() for i in range(bs)]) + 1
        
        # End index of each step, shape: (B, max_num_steps), type: long
        score_ids = torch.full(
            (bs, max_num_steps), -1, dtype=torch.long, 
            device=solution_tokens.device,
        )
        
        # Whether end of step, shape: (B, max_response_tokens), type: bool
        reward_mask = torch.zeros_like(solution_tokens, dtype=torch.bool)
        eos_indices = num_actions - 1 - action_mask.long().fliplr().argmax(1)
        
        for j in range(bs):
            step_separators_per_data = column_ids[row_ids==j]
            num_intermediate_steps = step_separators_per_data.numel()
            # Intermediate steps
            score_ids[j, :num_intermediate_steps] = step_separators_per_data
            reward_mask[j, step_separators_per_data] = True
            # Last step
            score_ids[j, num_intermediate_steps] = eos_indices[j]
            reward_mask[j, eos_indices[j]] = True
        
        score_mask = score_ids != -1
        output = dict(
            score_ids=score_ids,
            score_mask=score_mask,
            reward_mask=reward_mask,
            num_steps=score_mask.float().sum(dim=-1),
        )
        return DataProto.from_dict(tensors=output)

    def _extract_score_from_response(self, response_text):
        """Extract numerical score from LLM judge response (same as original)"""
        import re
        
        # First try to extract from \boxed{} format
        boxed_pattern = r'\\boxed\{([^}]+)\}'
        boxed_matches = re.findall(boxed_pattern, response_text)
        
        if boxed_matches:
            try:
                boxed_content = boxed_matches[-1].strip()
                score = float(boxed_content)
                return max(0.0, min(1.0, score))
            except ValueError:
                pass
        
        # Fallback: Try to extract a decimal number between 0 and 1
        decimal_pattern = r'\b(0\.\d+|1\.0)\b'
        decimal_matches = re.findall(decimal_pattern, response_text)
        
        if decimal_matches:
            try:
                score = float(decimal_matches[-1])
                return max(0.0, min(1.0, score))
            except ValueError:
                pass
        
        # # If no decimal found, look for integers 0 or 1
        # integer_pattern = r'\b(0|1)\b'
        # integer_matches = re.findall(integer_pattern, response_text)
        
        # if integer_matches:
        #     try:
        #         score = float(integer_matches[-1])
        #         return max(0.0, min(1.0, score))
        #     except ValueError:
        #         pass
        
        # Default score if parsing fails
        return 0.0

    # 封装成函数
    def _extract_content_value(self, text):
        pattern = r"'content':\s*'(.*?)',\s*'role'"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1)
        return None

    def _remote_llm_judge_step(self, problem_text, previous_steps_text, current_step_text):
        """Use remote LLM API to judge a single reasoning step"""
        import requests
        import time

        problem_text_advance = self._extract_content_value(problem_text)
        if problem_text_advance is None:
            problem_text_advance = problem_text

        
        # Format the prompt
        prompt = self.judge_prompt_template.format(
            problem=problem_text,
            previous_steps=previous_steps_text,
            current_step=current_step_text
        )
        
        
        # Prepare API request
        messages = [{'role': 'user', 'content': prompt}]
        
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": self.max_judge_output_length,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "min_p": self.min_p,
            "top_k": self.top_k,
            "repetition_penalty": self.repetition_penalty,
            "frequency_penalty": self.frequency_penalty,
            "length_penalty": self.length_penalty,
            "stop": ["\\n\\n", self.eos_token],  # Stop at double newline to prevent overly long responses
            "stream": False,
            
        }

        # Retry logic for robust API calls
        for attempt in range(self.max_retries):
            try:
                start_time = time.time()
                
                response = self.session.post(
                    f"{self.api_base_url}/v1/chat/completions",
                    json=payload,
                    timeout=self.request_timeout
                )
                
                end_time = time.time()

                
                if response.status_code == 200:
                    result = response.json()
                    response_text = result["choices"][0]["message"]["content"]
                    
                    # Extract and return score
                    score = self._extract_score_from_response(response_text)
                    print("[LLM-as-a-Judge score]" + f"{score}" )
                    # print("[LLM-as-a-Judge response]" + f"{response_text}" )

                    if self.rank == 0 and attempt == 0:  # Log only on first successful attempt and rank 0
                        logger.debug(f"Remote judge response time: {end_time - start_time:.3f}s")
                    
                    return score
                
                else:
                    logger.warning(f"API request failed with status {response.status_code}: {response.text}")
                    
            except requests.RequestException as e:
                logger.warning(f"API request attempt {attempt + 1} failed: {e}")
                
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (2 ** attempt))  # Exponential backoff
                    
        # If all retries failed, return default score
        logger.error(f"All {self.max_retries} API request attempts failed, returning default score")
        return 0.5

    def _judge_all_steps(self, data):
        """Judge all steps for all samples in the batch using remote API calls"""
        bs = data.batch['prompts'].size(0)
        solution_tokens = data.batch['responses']
        score_ids = data.batch['score_ids']
        score_mask = data.batch['score_mask']
        reward_mask = data.batch['reward_mask']
        
        # Initialize scores tensor
        step_scores = torch.zeros_like(reward_mask, dtype=torch.float32, device=reward_mask.device)
        
        for batch_idx in range(bs):
            # Extract problem text
            problem_ids = data.batch['prompts'][batch_idx]
            problem_mask = data.batch['attention_mask'][batch_idx][:len(problem_ids)]
            valid_problem_ids = problem_ids[problem_mask.bool()]
            problem_text = self.tokenizer.decode(valid_problem_ids, skip_special_tokens=True)
            
            # Process each step
            previous_steps_text = ""
            valid_scores = score_ids[batch_idx][score_mask[batch_idx]]
            
            for step_idx, step_end_pos in enumerate(valid_scores):
                # Extract current step
                if step_idx == 0:
                    start_pos = 0
                else:
                    start_pos = valid_scores[step_idx - 1] + 1
                
                current_step_ids = solution_tokens[batch_idx, start_pos:step_end_pos]
                current_step_text = self.tokenizer.decode(current_step_ids, skip_special_tokens=True)
                
                # Judge the current step using remote API
                score = self._remote_llm_judge_step(problem_text, previous_steps_text, current_step_text)
                
                # Assign score to the step end position
                step_scores[batch_idx, step_end_pos] = score

                # Update previous steps for next iteration
                if previous_steps_text:
                    previous_steps_text += "\n" + current_step_text
                else:
                    previous_steps_text = current_step_text
        
        return step_scores

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_rm_score(self, data: DataProto):
        """Main method to compute process rewards using remote LLM judge"""
        data = data.to('cuda')
        
        # Split data into steps
        data.union(self._split_steps(data))
        
        # Judge all steps for all samples
        token_level_scores = self._judge_all_steps(data)
        
        # Apply credit assignment if configured
        if not self.disable_approx_min_form_credit_assignment:
            reward_mask = data.batch['reward_mask']
            weight = torch.softmax(
                -token_level_scores.masked_fill(
                    ~reward_mask, float('inf')
                ) / self.credit_assignment_temperature,
                dim=-1,
            )
            token_level_scores *= weight

        output = DataProto.from_dict(tensors={'rm_scores': token_level_scores})
        output = output.to('cpu')
        
        return output

    def close(self):
        """Clean up resources"""
        if hasattr(self, 'session'):
            self.session.close()

""" 
Warning: The following resource request cannot be scheduled right now: {'CPU': 1.0, 'GPU': 2.0}. This is likely due to all cluster resources being claimed by actors. Consider creating fewer actors or adding more nodes to this Ray cluster.
需要更多的结点才能调试这个方案！
"""
class RayJudgePRMWorker(Worker):
    """
    Ray-based LLM-as-a-Judge Process Reward Worker (简化版)
    使用Ray张量并行进行推理
    """

    def __init__(self, config):
        super().__init__()
        if not torch.distributed.is_initialized():
            torch.distributed.init_process_group(backend="nccl")
        
        self.config = config
        
        # 配置
        self.model_path = config.get('model_path', '/data/home/scyb224/Workspace/LLMs/Qwen2.5-7B-Instruct')
        self.split_step_char = config.get('split_step_char', '\n\n')
        self.disable_approx_min_form_credit_assignment = config.get('disable_approx_min_form_credit_assignment', False)
        self.credit_assignment_temperature = config.get('credit_assignment_temperature', 1.0)
        
        # Judge模板
        self.judge_prompt_template = config.get('judge_prompt_template', 
            """You are an expert evaluator. Please evaluate the quality of the following reasoning step.

Problem: {problem}

Previous steps:
{previous_steps}

Current step being evaluated:
{current_step}

Please rate this step from 0 to 1 and put your score in \\boxed{{}} format."""
        )
        
        # 初始化tokenizer
        self._init_tokenizer()

    def _init_tokenizer(self):
        """初始化tokenizer"""
        from verl.utils import hf_tokenizer
        from verl.utils.fs import copy_to_local
        
        trust_remote_code = self.config.get('trust_remote_code', False)
        local_path = copy_to_local(self.model_path)
        self.tokenizer = hf_tokenizer(local_path, trust_remote_code=trust_remote_code)
        
        # 分割tokens
        self.split_step_tokens = []
        for i in range(len(self.tokenizer)):
            if self.tokenizer.decode(i).endswith(self.split_step_char):
                self.split_step_tokens.append(i)
        self.split_step_tokens = torch.LongTensor(self.split_step_tokens)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        """初始化Ray服务"""
        import ray
        
        if not ray.is_initialized():
            ray.init()
        
        # 导入API
        import sys
        import os
        ray_api_path = os.path.join("/data/home/scyb224/Workspace/PURE/bash_script/RayLLMBacakend")
        if ray_api_path not in sys.path:
            sys.path.append(ray_api_path)
        
        # 预热服务（通过调用一次来初始化）
        async def init_service():
            from ray_internal_vllm_api import ray_generate
            await ray_generate("Hello")  # 预热
        
        import asyncio
        asyncio.run(init_service())
        
        if torch.cuda.is_available():
            self.split_step_tokens = self.split_step_tokens.cuda()

    def _split_steps(self, data):
        """分割步骤"""
        bs, problem_length = data.batch['prompts'].size()
        action_mask = data.batch['attention_mask'][:, problem_length:]
        num_actions = action_mask.size(1)
        solution_tokens = data.batch['responses']

        row_ids, column_ids = torch.where(
            torch.isin(solution_tokens, self.split_step_tokens)
        )
        
        max_num_steps = max([column_ids[row_ids==i].numel() for i in range(bs)]) + 1
        
        score_ids = torch.full(
            (bs, max_num_steps), -1, dtype=torch.long, 
            device=solution_tokens.device,
        )
        
        reward_mask = torch.zeros_like(solution_tokens, dtype=torch.bool)
        eos_indices = num_actions - 1 - action_mask.long().fliplr().argmax(1)
        
        for j in range(bs):
            step_separators_per_data = column_ids[row_ids==j]
            num_intermediate_steps = step_separators_per_data.numel()
            score_ids[j, :num_intermediate_steps] = step_separators_per_data
            reward_mask[j, step_separators_per_data] = True
            score_ids[j, num_intermediate_steps] = eos_indices[j]
            reward_mask[j, eos_indices[j]] = True
        
        score_mask = score_ids != -1
        return DataProto.from_dict(tensors=dict(
            score_ids=score_ids,
            score_mask=score_mask,
            reward_mask=reward_mask,
            num_steps=score_mask.float().sum(dim=-1),
        ))

    def _extract_score_from_response(self, response_text):
        """提取分数"""
        import re
        
        # 提取 \boxed{} 格式
        boxed_pattern = r'\\boxed\{([^}]+)\}'
        matches = re.findall(boxed_pattern, response_text)
        
        if matches:
            try:
                score = float(matches[-1].strip())
                return max(0.0, min(1.0, score))
            except ValueError:
                pass
        
        # 备用方案
        # decimal_pattern = r'\b(0\.\d+|1\.0)\b' # 匹配0.0到1.0之间的数字
        # matches = re.findall(decimal_pattern, response_text)
        
        # if matches:
        #     try:
        #         score = float(matches[-1])
        #         return max(0.0, min(1.0, score))
        #     except ValueError:
        #         pass
        
        return 0.5

    def _judge_all_steps(self, data):
        """批量判断所有步骤"""
        bs = data.batch['prompts'].size(0)
        solution_tokens = data.batch['responses']
        score_ids = data.batch['score_ids']
        score_mask = data.batch['score_mask']
        reward_mask = data.batch['reward_mask']
        
        step_scores = torch.zeros_like(reward_mask, dtype=torch.float32, device=reward_mask.device)
        
        # 准备批量请求
        batch_requests = []
        request_positions = []
        
        for batch_idx in range(bs):
            problem_ids = data.batch['prompts'][batch_idx]
            problem_mask = data.batch['attention_mask'][batch_idx][:len(problem_ids)]
            valid_problem_ids = problem_ids[problem_mask.bool()]
            problem_text = self.tokenizer.decode(valid_problem_ids, skip_special_tokens=True)
            
            previous_steps_text = ""
            valid_scores = score_ids[batch_idx][score_mask[batch_idx]]
            
            for step_idx, step_end_pos in enumerate(valid_scores):
                if step_idx == 0:
                    start_pos = 0
                else:
                    start_pos = valid_scores[step_idx - 1] + 1
                
                current_step_ids = solution_tokens[batch_idx, start_pos:step_end_pos]
                current_step_text = self.tokenizer.decode(current_step_ids, skip_special_tokens=True)
                
                prompt = self.judge_prompt_template.format(
                    problem=problem_text,
                    previous_steps=previous_steps_text,
                    current_step=current_step_text
                )
                
                batch_requests.append(prompt)
                request_positions.append((batch_idx, step_end_pos))
                
                if previous_steps_text:
                    previous_steps_text += "\n" + current_step_text
                else:
                    previous_steps_text = current_step_text
        
        # 批量推理
        if batch_requests:
            try:
                import asyncio
                from ray_internal_vllm_api import ray_batch_generate
                
                batch_responses = asyncio.run(ray_batch_generate(batch_requests))
                import pdb;pdb.set_trace()
                for (batch_idx, step_end_pos), response_text in zip(request_positions, batch_responses):
                    score = self._extract_score_from_response(response_text)
                    step_scores[batch_idx, step_end_pos] = score
                    
            except Exception as e:
                logger.error(f"批量判断失败: {e}")
                for batch_idx, step_end_pos in request_positions:
                    step_scores[batch_idx, step_end_pos] = 0.5
        
        return step_scores

    @register(dispatch_mode=Dispatch.DP_COMPUTE_PROTO)
    def compute_rm_score(self, data: DataProto):
        """计算奖励分数"""
        data = data.to('cuda')
        
        # 分割步骤
        data.union(self._split_steps(data))
        
        # 判断所有步骤
        token_level_scores = self._judge_all_steps(data)
        
        # 信用分配
        if not self.disable_approx_min_form_credit_assignment:
            reward_mask = data.batch['reward_mask']
            weight = torch.softmax(
                -token_level_scores.masked_fill(
                    ~reward_mask, float('inf')
                ) / self.credit_assignment_temperature,
                dim=-1,
            )
            token_level_scores *= weight

        output = DataProto.from_dict(tensors={'rm_scores': token_level_scores})
        output = output.to('cpu')
        
        return output
